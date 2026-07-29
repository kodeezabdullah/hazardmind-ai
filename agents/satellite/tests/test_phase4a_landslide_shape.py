"""Phase 4a (science/full-pass): bi-temporal NDVI landslide scar detection.

Covers the properties that make the method better than single-scene
thresholding, not merely that it runs:
  1. always-bare terrain (desert/rock/urban) is NOT flagged — it differences
     to ~0 between the two scenes, which single-scene absolute thresholding
     could never distinguish from damage;
  2. no pre-event scene -> status="insufficient_reference", explicitly NOT a
     fall back to single-scene thresholding;
  3. a real elongated downslope scar on steep ground IS detected;
  4. a circular NDVI drop on flat ground (harvested field) is REJECTED by
     the shape filter even though its NDVI drop is identical;
  5. the rejection reasons and every threshold are recorded.

Offline and deterministic — no network.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import landslide_detection as ld

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


def _hillside_dem(shape=(200, 200), relief_m=1400.0):
    """A DEM sloping steeply down toward +y (south).

    1400 m of relief over 200 px at 10 m (2 km) is ~35 deg — genuinely
    landslide-prone terrain, comfortably above the 15 deg minimum. An
    earlier 400 m version was only ~11 deg and the slope filter correctly
    rejected the scar on it, which was a fixture flaw, not a code flaw."""
    rows, cols = shape
    return np.tile(
        np.linspace(relief_m, 0.0, rows).reshape(-1, 1), (1, cols)
    ).astype("float32")


def test_always_bare_not_flagged():
    """The exact failure of single-scene thresholding: terrain that was ALWAYS
    bare (rock/desert/urban) has a low absolute NDVI in the post scene but no
    CHANGE, so bi-temporal differencing must ignore it."""
    pre = np.full((200, 200), 0.7, dtype="float32")
    post = np.full((200, 200), 0.7, dtype="float32")
    # A permanently bare rock outcrop: low NDVI in BOTH scenes.
    pre[20:60, 20:60] = 0.05
    post[20:60, 20:60] = 0.05
    res = ld.detect_landslide_scars(post, pre, dem=_hillside_dem())
    if res["status"] != "complete":
        bad(f"run failed: {res}")
        return
    if res["scar_count"] == 0 and res["affected_percent"] == 0.0:
        ok("always-bare terrain not flagged (single-scene thresholding "
           "would have called this damage)")
    else:
        bad(f"always-bare terrain flagged: {res['scar_count']} scars")


def test_no_pre_event_refuses():
    post = np.full((50, 50), 0.2, dtype="float32")
    res = ld.detect_landslide_scars(post, None)
    if res["status"] == "insufficient_reference" and res["scar_mask"] is None:
        ok("no pre-event NDVI -> insufficient_reference, no mask")
    else:
        bad(f"expected refusal, got {res['status']}")
    if "NOT substituted" in res.get("reason", ""):
        ok("refusal documents why single-scene thresholding is not used")
    else:
        bad("refusal reason missing the no-fallback rule")


def test_real_scar_detected():
    """Elongated, downslope-aligned, on steep ground, tapering."""
    pre = np.full((200, 200), 0.75, dtype="float32")
    post = pre.copy()
    # A scar running down-slope (along +y), narrowing toward the bottom.
    for r in range(60, 160):
        half = max(2, int(10 - (r - 60) * 0.07))
        post[r, 100 - half:100 + half] = 0.12
    res = ld.detect_landslide_scars(post, pre, dem=_hillside_dem())
    if res["status"] != "complete":
        bad(f"run failed: {res}")
        return
    if res["scar_count"] >= 1:
        s = res["scars"][0]
        ok(f"downslope scar detected (elongation {s['elongation']}, "
           f"slope {s['mean_slope_deg']} deg, drop {s['mean_ndvi_drop']})")
    else:
        bad(f"real scar missed: {res['rejected_by']}")


def test_circular_drop_on_flat_ground_rejected():
    """A harvested field: identical NDVI drop, wrong shape AND flat ground.
    This is the case an NDVI drop alone cannot distinguish."""
    pre = np.full((200, 200), 0.75, dtype="float32")
    post = pre.copy()
    yy, xx = np.ogrid[:200, :200]
    circle = (yy - 100) ** 2 + (xx - 100) ** 2 <= 30**2
    post[circle] = 0.12
    flat_dem = np.full((200, 200), 100.0, dtype="float32")
    res = ld.detect_landslide_scars(post, pre, dem=flat_dem)
    if res["status"] != "complete":
        bad(f"run failed: {res}")
        return
    if res["scar_count"] == 0:
        rej = res["rejected_by"]
        ok(f"circular drop on flat ground rejected by shape/slope filters "
           f"({rej})")
    else:
        bad(f"harvested-field pattern accepted as a landslide: {res['scars']}")
    if res["candidates_before_shape_filter"] >= 1:
        ok("the NDVI drop WAS a candidate — shape filtering is what rejected "
           "it, proving the filter does the work")
    else:
        bad("no candidate produced; test does not exercise the shape filter")


def test_audit_trail_present():
    pre = np.full((100, 100), 0.7, dtype="float32")
    post = pre.copy()
    post[40:60, 48:52] = 0.1
    res = ld.detect_landslide_scars(post, pre, dem=_hillside_dem((100, 100)))
    required = [
        "method", "ndvi_drop_threshold", "min_elongation",
        "orientation_tolerance_deg", "min_slope_deg", "min_scar_area_px",
        "min_taper_ratio", "thresholds_basis", "rejected_by",
        "candidates_before_shape_filter",
    ]
    missing = [k for k in required if k not in res]
    if not missing:
        ok("every threshold + the rejection breakdown recorded in the result")
    else:
        bad(f"audit fields missing: {missing}")
    if "NOT calibrated against a landslide inventory" in res["thresholds_basis"]:
        ok("thresholds_basis states honestly that they are uncalibrated")
    else:
        bad(f"basis text overclaims: {res['thresholds_basis']}")


if __name__ == "__main__":
    print("=" * 64)
    print("PHASE 4a — BI-TEMPORAL NDVI + SHAPE-FILTERED SCAR DETECTION")
    print("=" * 64)
    test_always_bare_not_flagged()
    test_no_pre_event_refuses()
    test_real_scar_detected()
    test_circular_drop_on_flat_ground_rejected()
    test_audit_trail_present()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
