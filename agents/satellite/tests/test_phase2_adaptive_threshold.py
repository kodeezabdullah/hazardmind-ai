"""Phase 2 (science/full-pass): KI adaptive thresholding in the S2 flood path.

Covers:
  1. adaptive_threshold unit behavior: bimodal sample -> KI cut between the
     modes; tiny-water sample -> class-fraction guard trips -> fixed fallback
     with the reason recorded.
  2. calculate_indices integration: bimodal flood clip classifies with the
     DERIVED cut (audit fields present: threshold_method/derived_threshold/
     affected_cut/ki_diagnostics); near-dry clip falls back to the fixed
     scheme and records why.
  3. SAR path untouched (KI is S2-calibrated-index only; SAR thresholding is
     Phase 3's change-detection work).

Offline and deterministic — no network.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import processor
from adaptive_threshold import derive_water_threshold

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


def _clip(bands):
    shape = next(iter(bands.values())).shape
    return {
        "bands": bands, "tci": None, "transform": None, "crs": None,
        "shape": shape, "mask": np.ones(shape, dtype=bool),
    }


def test_unit_bimodal_and_guard():
    rng = np.random.default_rng(11)
    land = rng.normal(-0.35, 0.08, 40_000)
    water = rng.normal(0.45, 0.12, 8_000)
    d = derive_water_threshold(np.concatenate([land, water]), 0.0)
    if d["threshold_method"] == "kittler_illingworth" and -0.25 < d["threshold"] < 0.3:
        ok(f"bimodal 17% water: KI cut {d['threshold']} between the modes")
    else:
        bad(f"bimodal case wrong: {d}")
    tiny = np.concatenate([land, rng.normal(0.45, 0.12, 100)])
    d2 = derive_water_threshold(tiny, 0.0)
    if d2["threshold_method"] == "fixed_fallback" and d2["threshold"] == 0.0 and d2["fallback_reason"]:
        ok(f"0.25% water: guard trips, fixed fallback ({d2['fallback_reason'][:40]}...)")
    else:
        bad(f"guard case wrong: {d2}")


def test_integration_bimodal_uses_derived_cut():
    """Real flood (MNDWI ~ +0.35) against dry land (~ -0.45). KI must derive
    a cut BETWEEN the modes. The cut may sit below zero when the water mode
    is broad — that is legitimate; the invariant is that the upper mode is
    genuinely water (positive mean), which is what the guard tests."""
    rng = np.random.default_rng(5)
    rows, cols = 200, 200
    mndwi = rng.normal(-0.45, 0.05, (rows, cols)).astype("float32")
    mndwi[:, :40] = rng.normal(0.35, 0.05, (rows, 40)).astype("float32")  # 20% flood
    # Synthesise B03/B11 that produce exactly this index: fix B03+B11=2000.
    b03 = (1000.0 * (1 + mndwi)).astype("float32")
    b11 = (2000.0 - b03).astype("float32")
    res = processor.calculate_indices(_clip({"B03": b03, "B08": b03.copy(), "B11": b11}),
                                      "sentinel-2", "flood")
    if res is None:
        bad("calculate_indices returned None")
        return
    if res.get("threshold_method") == "kittler_illingworth":
        ok(f"KI engaged (cut {res.get('derived_threshold')}, "
           f"D={res.get('ki_diagnostics', {}).get('ashman_d')})")
    else:
        bad(f"KI not engaged: {res.get('threshold_method')} "
            f"({res.get('ki_fallback_reason')})")
        return
    # The cut itself may legitimately sit below zero when the water mode is
    # broad — what must hold is that it separates the two modes and that the
    # UPPER mode is genuinely water (see the guard in adaptive_threshold).
    upper_mean = max(res["ki_diagnostics"]["means"])
    if -0.45 < res["derived_threshold"] < 0.35 and upper_mean > 0.0:
        ok(f"derived cut {res['derived_threshold']} separates the modes, "
           f"upper mode is water (mean {upper_mean})")
    else:
        bad(f"derived cut {res['derived_threshold']} / upper mean {upper_mean} "
            "implausible")
    if abs(res["water_percent"] - 20.0) < 3.0:
        ok(f"flood detected at the derived cut (water={res['water_percent']}%)")
    else:
        bad(f"water_percent {res['water_percent']} (expected ~20)")


def test_negative_derived_cut_rejected():
    """The Insh regression, caught in production: a stale mostly-dry scene
    can split cleanly into two DRY modes, and KI then returns a NEGATIVE cut
    that manufactures phantom water. A water index is positive over water by
    construction, so a negative cut must never be applied."""
    rng = np.random.default_rng(21)
    rows, cols = 200, 200
    # Two dry-land modes: -0.45 and -0.12. Bimodal, but NO water anywhere.
    mndwi = rng.normal(-0.45, 0.04, (rows, cols)).astype("float32")
    mndwi[:, :50] = rng.normal(-0.12, 0.04, (rows, 50)).astype("float32")
    b03 = (1000.0 * (1 + mndwi)).astype("float32")
    b11 = (2000.0 - b03).astype("float32")
    res = processor.calculate_indices(_clip({"B03": b03, "B08": b03.copy(), "B11": b11}),
                                      "sentinel-2", "flood")
    if res is None:
        bad("calculate_indices returned None")
        return
    if res.get("threshold_method") == "fixed_fallback" and "not_water" in (
        res.get("ki_fallback_reason") or ""
    ):
        ok(f"dry-vs-dry split rejected ({res['ki_fallback_reason'][:52]}...)")
    else:
        bad(f"dry split NOT rejected: method={res.get('threshold_method')} "
            f"reason={res.get('ki_fallback_reason')}")
    # The fixed 0.0 floor still applies, so only genuinely positive-index
    # pixels can classify; two negative modes must yield ~no water.
    if res["water_percent"] < 0.5:
        ok(f"no phantom water from two dry-land modes ({res['water_percent']}%)")
    else:
        bad(f"phantom water: {res['water_percent']}% classified")


def test_integration_dry_falls_back():
    rng = np.random.default_rng(9)
    rows, cols = 100, 100
    mndwi = rng.normal(-0.4, 0.05, (rows, cols)).astype("float32")
    b03 = (1000.0 * (1 + mndwi)).astype("float32")
    b11 = (2000.0 - b03).astype("float32")
    res = processor.calculate_indices(_clip({"B03": b03, "B08": b03.copy(), "B11": b11}),
                                      "sentinel-2", "flood")
    if res and res.get("threshold_method") == "fixed_fallback" and res.get("ki_fallback_reason"):
        ok(f"dry unimodal AOI: fixed fallback, reason recorded "
           f"({res['ki_fallback_reason'][:40]}...)")
    else:
        bad(f"dry case wrong: {res and res.get('threshold_method')} "
            f"{res and res.get('ki_fallback_reason')}")
    if res and res.get("affected_cut") == 0.0:
        ok("affected_cut records the applied fixed boundary (0.0)")
    else:
        bad(f"affected_cut {res and res.get('affected_cut')}")


def test_sar_untouched():
    vv = np.full((60, 60), 220.0, dtype="float32")
    res = processor.calculate_indices(_clip({"VV": vv}), "sentinel-1", "flood")
    if res and res.get("threshold_method") is None and res.get("derived_threshold") is None:
        ok("SAR path: no KI attempt (Phase 3 owns SAR thresholding)")
    else:
        bad(f"SAR affected: {res and res.get('threshold_method')}")


if __name__ == "__main__":
    print("=" * 64)
    print("PHASE 2 — KITTLER-ILLINGWORTH ADAPTIVE THRESHOLD")
    print("=" * 64)
    test_unit_bimodal_and_guard()
    test_integration_bimodal_uses_derived_cut()
    test_negative_derived_cut_rejected()
    test_integration_dry_falls_back()
    test_sar_untouched()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
