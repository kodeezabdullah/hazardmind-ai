"""Phase 1b (science/full-pass): MNDWI (Xu 2006) replaces NDWI for flood.

(B03-B11)/(B03+B11) — SWIR suppresses built-up surfaces, NDWI's documented
false-positive class, while water absorbs SWIR even more strongly than NIR.

Covers:
  1. formula + labels: B11 present -> MNDWI computed exactly, index_type/
     index_units say MNDWI (no label/content mismatch).
  2. built-up suppression: a synthetic concrete pixel that NDWI would call
     water is NOT water under MNDWI.
  3. fallback honesty: B11 absent -> NDWI computed AND labeled NDWI.
  4. cross_validator's physics check accepts MNDWI as a calibrated water
     ratio (index evidence still added — the check must not silently die
     with the index rename).

Offline and deterministic — no network, no CDSE, no LLM.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import processor
from confidence_tracker import ConfidenceTracker
from cross_validator import CrossValidator

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


def _clip(bands: dict):
    shape = next(iter(bands.values())).shape
    return {
        "bands": {k: v for k, v in bands.items()},
        "tci": None,
        "transform": None,
        "crs": None,
        "shape": shape,
        "mask": np.ones(shape, dtype=bool),
    }


def test_mndwi_formula_and_labels():
    rows = cols = 10
    b03 = np.full((rows, cols), 2000.0, dtype="float32")
    b08 = np.full((rows, cols), 1000.0, dtype="float32")
    b11 = np.full((rows, cols), 500.0, dtype="float32")
    res = processor.calculate_indices(_clip({"B03": b03, "B08": b08, "B11": b11}),
                                      "sentinel-2", "flood")
    if res is None:
        bad("calculate_indices returned None")
        return
    expected = (2000.0 - 500.0) / (2000.0 + 500.0)  # 0.6 — MNDWI, not NDWI's 0.333
    if abs(res["mean_value"] - round(expected, 4)) < 1e-6:
        ok(f"MNDWI formula used ((B03-B11)/(B03+B11) = {res['mean_value']})")
    else:
        bad(f"mean {res['mean_value']} — expected MNDWI {round(expected,4)} "
            f"(NDWI would be 0.3333)")
    if res["index_type"] == "MNDWI" and res["index_units"] == "MNDWI_ratio":
        ok("index_type/index_units both say MNDWI — labels match content")
    else:
        bad(f"labels wrong: {res['index_type']}/{res['index_units']}")


def test_builtup_suppressed():
    """Concrete/built-up: moderate green, low NIR (NDWI false positive),
    HIGH SWIR (the discriminator Xu chose)."""
    rows = cols = 10
    b03 = np.full((rows, cols), 1500.0, dtype="float32")
    b08 = np.full((rows, cols), 1000.0, dtype="float32")   # NDWI = +0.2 -> wet_soil class
    b11 = np.full((rows, cols), 2500.0, dtype="float32")   # MNDWI = -0.25 -> land
    ndwi_only = processor.calculate_indices(_clip({"B03": b03, "B08": b08}),
                                            "sentinel-2", "flood")
    mndwi = processor.calculate_indices(_clip({"B03": b03, "B08": b08, "B11": b11}),
                                        "sentinel-2", "flood")
    if ndwi_only and ndwi_only["water_percent"] > 0 and mndwi and mndwi["water_percent"] == 0.0:
        ok(f"built-up false positive suppressed (NDWI {ndwi_only['water_percent']}% "
           f"-> MNDWI {mndwi['water_percent']}%)")
    else:
        bad(f"suppression failed: NDWI {ndwi_only and ndwi_only['water_percent']}%, "
            f"MNDWI {mndwi and mndwi['water_percent']}%")


def test_fallback_labeled_ndwi():
    rows = cols = 6
    b03 = np.full((rows, cols), 3000.0, dtype="float32")
    b08 = np.full((rows, cols), 1000.0, dtype="float32")
    res = processor.calculate_indices(_clip({"B03": b03, "B08": b08}),
                                      "sentinel-2", "flood")
    if res and res["index_type"] == "NDWI" and res["index_units"] == "NDWI_ratio":
        ok("B11 absent: NDWI fallback computed AND labeled NDWI")
    else:
        bad(f"fallback labels wrong: {res and res['index_type']}/{res and res['index_units']}")


class _NoNetValidator(CrossValidator):
    def check_gdacs(self, location):
        return None

    def check_usgs(self, location):
        return None

    def get_featherless_opinion(self, result, validations, tracker):
        return None


def test_cross_validator_accepts_mndwi():
    v = _NoNetValidator()
    trk = ConfidenceTracker()
    payload = {
        "affected_area_km2": 12.0,
        "index_type": "MNDWI",
        "index_calibrated": True,
        "index_units": "MNDWI_ratio",
        "mean_index": -0.2,
        "affected_mean_index": 0.45,
        "water_percent": 14.0,
        "coverage_percent": 100,
    }
    v.validate_all(payload, "flood", {"lat": 39.5, "lon": 22.9}, trk)
    if any(e["source"] == "index_validation" for e in trk.evidence):
        ok("physics check runs on MNDWI (index evidence added)")
    else:
        bad(f"physics check silently skipped MNDWI: {trk.evidence}")


if __name__ == "__main__":
    print("=" * 64)
    print("PHASE 1b — MNDWI REPLACES NDWI")
    print("=" * 64)
    test_mndwi_formula_and_labels()
    test_builtup_suppressed()
    test_fallback_labeled_ndwi()
    test_cross_validator_accepts_mndwi()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
