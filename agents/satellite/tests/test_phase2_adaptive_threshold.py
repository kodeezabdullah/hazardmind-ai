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
    """Flooded farmland at MNDWI ~ +0.15 — BELOW the fixed wet_soil floor of
    0.0? No: 0.15 > 0. Use a mode at -0.05 (wet but sub-zero: invisible to
    the fixed scheme) so the derived cut is what detects it."""
    rng = np.random.default_rng(5)
    rows, cols = 200, 200
    # land mode ~ -0.45, flood mode ~ -0.05 (sub-zero: fixed scheme misses it
    # entirely; KI must find the cut between the modes).
    mndwi = rng.normal(-0.45, 0.05, (rows, cols)).astype("float32")
    mndwi[:, :40] = rng.normal(-0.05, 0.04, (rows, 40)).astype("float32")  # 20% flood
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
    if -0.4 < res["derived_threshold"] < -0.1:
        ok("derived cut sits between the land and flood modes")
    else:
        bad(f"derived cut {res['derived_threshold']} not between modes")
    if abs(res["water_percent"] - 20.0) < 3.0:
        ok(f"sub-zero flood mode detected via adaptive cut "
           f"(water={res['water_percent']}% ~ 20%) — fixed 0.0 cut would see ~0%")
    else:
        bad(f"water_percent {res['water_percent']} (expected ~20)")


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
    test_integration_dry_falls_back()
    test_sar_untouched()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
