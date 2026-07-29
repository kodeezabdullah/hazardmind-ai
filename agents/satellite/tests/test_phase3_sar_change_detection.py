"""Phase 3 (science/full-pass): S1 change-detection flood mapping.

Covers the properties that make the method valid, not just that it runs:
  1. calibration cancellation — a scene scaled by an arbitrary calibration
     factor produces an IDENTICAL flood mask (the whole basis of doing this
     on uncalibrated GRD);
  2. no same-orbit reference -> status="insufficient_reference", and
     explicitly NOT a fall back to absolute thresholding;
  3. log-ratio detects a real backscatter drop and ignores unchanged land;
  4. median baseline rejects a single transient wet acquisition;
  5. HAND masking removes a radar-shadow false positive on high terrain;
  6. morphological cleanup removes isolated speckle pixels, keeps real
     contiguous flood;
  7. Refined Lee reduces speckle variance while preserving an edge;
  8. the audit trail (filter/window/baseline count/threshold/method) is
     present in the result.

Offline and deterministic — no network.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sar_change_detection as scd

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


def _scene(rng, shape=(300, 300), land=500.0, speckle=0.15):
    """Synthetic linear-intensity SAR scene with multiplicative speckle."""
    return (land * rng.gamma(1 / speckle, speckle, shape)).astype("float32")


def _flooded(base, rows, cols, drop_db=8.0):
    out = base.copy()
    out[rows, cols] *= 10 ** (-drop_db / 10.0)
    return out


def test_calibration_cancels():
    """The decisive property: an arbitrary per-scene calibration factor must
    not change the answer. Verified live against CDSE LUTs (same-orbit
    sigmaNought agrees to ~0.003%); this asserts the CODE honours it."""
    rng = np.random.default_rng(3)
    pre = [_scene(rng) for _ in range(3)]
    post = _flooded(_scene(rng), slice(50, 150), slice(50, 150))
    a = scd.detect_flood_change(post, pre)
    k = 7.3  # arbitrary calibration factor applied to EVERY scene
    b = scd.detect_flood_change(post * k, [p * k for p in pre])
    if a["status"] == "complete" and b["status"] == "complete":
        same = np.array_equal(a["flood_mask"], b["flood_mask"])
        if same:
            ok("calibration factor cancels — identical flood mask at k=7.3")
        else:
            diff = int((a["flood_mask"] != b["flood_mask"]).sum())
            bad(f"calibration did NOT cancel: {diff} pixels differ")
    else:
        bad(f"runs failed: {a['status']}/{b['status']}")


def test_no_reference_refuses():
    rng = np.random.default_rng(4)
    res = scd.detect_flood_change(_scene(rng), [])
    if res["status"] == "insufficient_reference" and res["flood_mask"] is None:
        ok("no same-orbit reference -> insufficient_reference, no mask")
    else:
        bad(f"expected refusal, got {res['status']}")
    if "absolute thresholding is not used as a fallback" in res.get("reason", ""):
        ok("refusal states why absolute thresholding is not substituted")
    else:
        bad("refusal reason does not document the no-fallback rule")


def test_detects_drop_ignores_unchanged():
    rng = np.random.default_rng(5)
    pre = [_scene(rng) for _ in range(3)]
    post = _flooded(_scene(rng), slice(100, 200), slice(100, 200))
    res = scd.detect_flood_change(post, pre)
    if res["status"] != "complete":
        bad(f"run failed: {res}")
        return
    mask = res["flood_mask"]
    inside = mask[120:180, 120:180].mean()
    outside = mask[10:60, 10:60].mean()
    if inside > 0.85 and outside < 0.05:
        ok(f"8 dB drop detected ({inside:.0%} inside) and unchanged land "
           f"ignored ({outside:.1%} outside)")
    else:
        bad(f"detection wrong: inside={inside:.2f} outside={outside:.2f}")
    if res["mean_change_db"] is not None and res["mean_change_db"] < -3.0:
        ok(f"mean change over flood pixels is a real drop ({res['mean_change_db']} dB)")
    else:
        bad(f"mean_change_db {res['mean_change_db']}")


def test_median_baseline_rejects_transient():
    """One anomalously wet pre-event acquisition must not become the
    reference — that is exactly what the median is for."""
    rng = np.random.default_rng(6)
    normal = [_scene(rng) for _ in range(2)]
    transient = _scene(rng) * 10 ** (-6.0 / 10.0)  # one wet day, 6 dB down
    base = scd.build_baseline(normal + [transient])
    med = float(np.nanmedian(base["baseline"]))
    normal_level = float(np.nanmedian(np.stack(normal)))
    if abs(med - normal_level) / normal_level < 0.25:
        ok(f"median baseline tracks the normal level, not the transient "
           f"({med:.0f} vs normal {normal_level:.0f})")
    else:
        bad(f"transient moved the baseline: {med:.0f} vs {normal_level:.0f}")
    if base["scene_count"] == 3 and base["confidence_penalty"] == 0.0:
        ok("3-scene baseline: no confidence penalty")
    else:
        bad(f"baseline meta wrong: {base}")
    thin = scd.build_baseline(normal[:1])
    if thin["scene_count"] == 1 and thin["confidence_penalty"] > 0:
        ok(f"1-scene baseline records a confidence penalty ({thin['confidence_penalty']})")
    else:
        bad(f"thin baseline penalty missing: {thin}")


def test_hand_masks_shadow_false_positive():
    """A dark patch high above drainage is radar shadow, not water."""
    rng = np.random.default_rng(7)
    pre = [_scene(rng) for _ in range(3)]
    post = _scene(rng)
    # Two dark patches: one in a valley (real flood), one on a ridge (shadow).
    post[40:80, 40:80] *= 10 ** (-8.0 / 10.0)    # valley
    post[220:260, 220:260] *= 10 ** (-8.0 / 10.0)  # ridge
    dem = np.full((300, 300), 100.0, dtype="float32")
    dem[200:300, 200:300] = 400.0  # a 300 m ridge — HAND far above drainage
    res = scd.detect_flood_change(post, pre, dem=dem)
    if res["status"] != "complete":
        bad(f"run failed: {res}")
        return
    m = res["flood_mask"]
    valley = m[50:70, 50:70].mean()
    ridge = m[230:250, 230:250].mean()
    if valley > 0.8 and ridge < 0.2:
        ok(f"HAND removed the ridge false positive ({ridge:.1%}) and kept "
           f"the valley flood ({valley:.0%})")
    else:
        bad(f"HAND masking wrong: valley={valley:.2f} ridge={ridge:.2f}")
    if res.get("hand_max_m") == scd.HAND_MAX_M and "hand" in res.get("masks_applied", []):
        ok(f"HAND threshold recorded in result ({res['hand_max_m']} m)")
    else:
        bad(f"HAND audit fields missing: {res.get('hand_max_m')} {res.get('masks_applied')}")


def test_morphological_cleanup():
    flood = np.zeros((100, 100), dtype=bool)
    flood[20:40, 20:40] = True            # real contiguous patch (400 px)
    rng = np.random.default_rng(8)
    idx = rng.integers(60, 99, size=(2, 40))
    flood[idx[0], idx[1]] = True          # scattered speckle
    cleaned = scd.morphological_cleanup(flood)
    if cleaned[25:35, 25:35].all() and cleaned[60:, 60:].sum() < 5:
        ok("morphology keeps the contiguous patch, drops isolated speckle")
    else:
        bad(f"cleanup wrong: patch={cleaned[25:35,25:35].all()} "
            f"speckle_left={cleaned[60:,60:].sum()}")


def test_refined_lee_reduces_speckle_preserves_edge():
    rng = np.random.default_rng(9)
    img = np.empty((200, 200), dtype="float32")
    img[:, :100] = 500.0 * rng.gamma(1 / 0.2, 0.2, (200, 100))
    img[:, 100:] = 100.0 * rng.gamma(1 / 0.2, 0.2, (200, 100))
    filt = scd.refined_lee(img)
    cv_before = float(np.std(img[:, :100]) / np.mean(img[:, :100]))
    cv_after = float(np.std(filt[:, :100]) / np.mean(filt[:, :100]))
    left, right = float(np.mean(filt[:, 20:80])), float(np.mean(filt[:, 120:180]))
    if cv_after < cv_before * 0.7:
        ok(f"Refined Lee cut speckle CV {cv_before:.2f} -> {cv_after:.2f}")
    else:
        bad(f"speckle not reduced: {cv_before:.2f} -> {cv_after:.2f}")
    if left / right > 3.5:
        ok(f"edge contrast preserved ({left:.0f} vs {right:.0f})")
    else:
        bad(f"edge smeared: {left:.0f} vs {right:.0f}")


def test_audit_trail_present():
    rng = np.random.default_rng(10)
    res = scd.detect_flood_change(
        _flooded(_scene(rng), slice(50, 150), slice(50, 150)),
        [_scene(rng) for _ in range(3)],
    )
    required = [
        "method", "speckle_filter", "speckle_window", "enl",
        "baseline_scene_count", "baseline_confidence_penalty",
        "threshold_db", "threshold_method", "bimodal_tiles", "tiles_tested",
        "min_flood_patch_px", "index_calibrated", "index_units",
    ]
    missing = [k for k in required if k not in res]
    if not missing:
        ok(f"audit trail complete (filter={res['speckle_filter']}/"
           f"{res['speckle_window']}, thr={res['threshold_db']} dB via "
           f"{res['threshold_method']})")
    else:
        bad(f"audit fields missing: {missing}")


if __name__ == "__main__":
    print("=" * 64)
    print("PHASE 3 — S1 CHANGE DETECTION")
    print("=" * 64)
    test_calibration_cancels()
    test_no_reference_refuses()
    test_detects_drop_ignores_unchanged()
    test_median_baseline_rejects_transient()
    test_hand_masks_shadow_false_positive()
    test_morphological_cleanup()
    test_refined_lee_reduces_speckle_preserves_edge()
    test_audit_trail_present()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
