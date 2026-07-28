"""Phase 0b (science/full-pass): evidence_contradicts fix tests.

The index-physics check previously judged the WHOLE-AOI mean NDWI against
water thresholds — a comparison that fires on every realistic partial flood
(the whole-AOI mean only turns positive at ~43% flooded fraction). The fix
compares the mean over the CLASSIFIED WATER pixels (`affected_mean_index`)
and treats the whole-AOI mean as context only.

Covers:
  1. calculate_indices produces `affected_mean_index` (value case + the
     None-when-nothing-classified case) — computation correctness.
  2. `_render_clip`-level survival: process-side merge carries the field
     (asserted structurally via the calculate_indices -> result-dict path the
     renderer uses; full pipeline survival is verified live by the harness
     rerun whose stored evidence carries the field).
  3. cross_validator behavior:
     - a low-fraction flood (14% water, within-water mean 0.42, whole-AOI
       mean negative) raises NO contradiction concern and adds strong
       evidence;
     - zero water classified + GDACS RED still raises the CRITICAL
       contradiction (the one legitimately contradictory case);
     - zero water classified + no GDACS RED raises nothing;
     - a non-positive within-water mean (possible once Phase 2's adaptive
       threshold can move the class floor) raises the internal-inconsistency
       concern.

Offline and deterministic — no network, no CDSE, no LLM.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from confidence_tracker import ConfidenceTracker
from cross_validator import CrossValidator
import processor

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


class _NoNetValidator(CrossValidator):
    def __init__(self, gdacs=None):
        super().__init__()
        self._gdacs = gdacs

    def check_gdacs(self, location):
        return self._gdacs

    def check_usgs(self, location):
        return None

    def get_featherless_opinion(self, result, validations, tracker):
        return None


def _s2_flood_clip(water_cols: int, total_cols: int = 10, rows: int = 10):
    """Synthetic clipped dict: left `water_cols` columns are open water
    (NDWI ~ +0.5), the rest dry land (NDWI ~ -0.3)."""
    b03 = np.full((rows, total_cols), 1000.0, dtype="float32")
    b08 = np.full((rows, total_cols), 1000.0, dtype="float32")
    # water: (b03-b08)/(b03+b08) = 0.5  -> b03=3, b08=1 (x1000)
    b03[:, :water_cols] = 3000.0
    b08[:, :water_cols] = 1000.0
    # land: -0.3 -> b03=0.7, b08=1.3
    b03[:, water_cols:] = 700.0
    b08[:, water_cols:] = 1300.0
    return {
        "bands": {"B03": b03, "B08": b08},
        "tci": None,
        "transform": None,
        "crs": None,
        "shape": (rows, total_cols),
        "mask": np.ones((rows, total_cols), dtype=bool),
    }


def test_affected_mean_index_computed():
    # 20% of pixels water at NDWI 0.5; whole-AOI mean is negative.
    clip = _s2_flood_clip(water_cols=2)
    res = processor.calculate_indices(clip, "sentinel-2", "flood")
    if res is None:
        bad("calculate_indices returned None on synthetic flood clip")
        return
    amv = res.get("affected_mean_index")
    if amv is not None and amv > 0.3:
        ok(f"affected_mean_index computed over water pixels ({amv})")
    else:
        bad(f"affected_mean_index wrong: {amv} (expected ~0.5)")
    if res["mean_value"] < 0:
        ok(f"whole-AOI mean is negative at 20% flood ({res['mean_value']}) — "
           "the exact geometry the old check misread")
    else:
        bad(f"synthetic geometry broken: whole-AOI mean {res['mean_value']}")


def test_affected_mean_index_none_when_dry():
    clip = _s2_flood_clip(water_cols=0)
    res = processor.calculate_indices(clip, "sentinel-2", "flood")
    if res is None:
        bad("calculate_indices returned None on dry clip")
        return
    if res.get("water_percent") == 0.0 and res.get("affected_mean_index") is None:
        ok("affected_mean_index is None (not 0.0) when nothing classified")
    else:
        bad(f"dry clip: water={res.get('water_percent')} "
            f"affected_mean_index={res.get('affected_mean_index')}")


def test_low_fraction_flood_no_contradiction():
    v = _NoNetValidator(gdacs={"alert": "RED", "area": None, "distance_km": 10})
    trk = ConfidenceTracker()
    payload = {
        "affected_area_km2": 12.0,
        "cloud_cover": 5,
        "index_type": "NDWI",
        "index_calibrated": True,
        "index_units": "NDWI_ratio",
        "mean_index": -0.31,          # whole-AOI mean, negative — expected
        "affected_mean_index": 0.42,  # within-water mean — water territory
        "water_percent": 14.0,        # Kanalia-like flooded fraction
        "coverage_percent": 100,
    }
    findings = v.validate_all(payload, "flood", {"lat": 39.5, "lon": 22.9}, trk)
    contradictions = [f for f in findings
                      if f["source"] == "INDEX" and f["status"] == "CONTRADICTION"]
    if not contradictions:
        ok("14% flood + negative whole-AOI mean + GDACS RED: no contradiction")
    else:
        bad(f"low-fraction flood still flagged contradictory: {contradictions}")
    if not trk.concerns:
        ok("no concern raised for expected partial-flood geometry")
    else:
        bad(f"unexpected concerns: {trk.concerns}")
    idx_ev = [e for e in trk.evidence if e["source"] == "index_validation"]
    if idx_ev and idx_ev[0]["value"] >= 0.9:
        ok(f"strong index evidence from within-water mean ({idx_ev[0]['value']})")
    else:
        bad(f"expected strong index evidence, got {idx_ev}")
    if trk.confidence_basis() != "evidence_contradicts":
        ok(f"confidence_basis is {trk.confidence_basis()!r}, not "
           "'evidence_contradicts'")
    else:
        bad("confidence_basis still 'evidence_contradicts' on a sound "
            "partial-flood run")


def test_zero_water_gdacs_red_still_contradicts():
    v = _NoNetValidator(gdacs={"alert": "RED", "area": None, "distance_km": 10})
    trk = ConfidenceTracker()
    payload = {
        "affected_area_km2": 0.0,
        "cloud_cover": 5,
        "index_type": "NDWI",
        "index_calibrated": True,
        "mean_index": -0.35,
        "affected_mean_index": None,
        "water_percent": 0.0,
        "coverage_percent": 100,
    }
    findings = v.validate_all(payload, "flood", {"lat": 39.5, "lon": 22.9}, trk)
    contradictions = [f for f in findings
                      if f["source"] == "INDEX" and f["status"] == "CONTRADICTION"]
    crits = [c for c in trk.concerns if c["severity"] == "CRITICAL"]
    if contradictions and crits:
        ok("0% water + GDACS RED still raises the CRITICAL contradiction")
    else:
        bad(f"missing legitimate contradiction: findings={findings} "
            f"concerns={trk.concerns}")


def test_zero_water_no_gdacs_is_quiet():
    v = _NoNetValidator(gdacs=None)
    trk = ConfidenceTracker()
    payload = {
        "affected_area_km2": 0.0,
        "index_type": "NDWI",
        "index_calibrated": True,
        "mean_index": -0.35,
        "affected_mean_index": None,
        "water_percent": 0.0,
        "coverage_percent": 100,
    }
    v.validate_all(payload, "flood", {"lat": 39.5, "lon": 22.9}, trk)
    if not trk.concerns:
        ok("0% water with no external alert raises no concern")
    else:
        bad(f"unexpected concerns on quiet dry run: {trk.concerns}")


def test_nonpositive_within_water_mean_flags_inconsistency():
    v = _NoNetValidator(gdacs=None)
    trk = ConfidenceTracker()
    payload = {
        "affected_area_km2": 5.0,
        "index_type": "NDWI",
        "index_calibrated": True,
        "mean_index": -0.2,
        "affected_mean_index": -0.05,  # classified water not looking like water
        "water_percent": 3.0,
        "coverage_percent": 100,
    }
    findings = v.validate_all(payload, "flood", {"lat": 39.5, "lon": 22.9}, trk)
    contradictions = [f for f in findings
                      if f["source"] == "INDEX" and f["status"] == "CONTRADICTION"]
    highs = [c for c in trk.concerns if c["severity"] == "HIGH"]
    if contradictions and highs:
        ok("non-positive within-water mean flags internal inconsistency")
    else:
        bad(f"inconsistency not flagged: findings={findings} "
            f"concerns={trk.concerns}")


def test_confidence_basis_weak_vs_contradicts():
    """Phase 0b-2: a merely-low score with nothing actively contradicting is
    'evidence_weak', not 'evidence_contradicts'; a CRITICAL concern is what
    'contradicts' means."""
    weak = ConfidenceTracker()
    weak.add_evidence("cloud_check", 0.5, weight=0.2)
    weak.add_concern("scene is 13 days old", "MEDIUM")
    if weak.confidence_basis() == "evidence_weak":
        ok("low score, no CRITICAL -> evidence_weak")
    else:
        bad(f"expected evidence_weak, got {weak.confidence_basis()!r}")

    contra = ConfidenceTracker()
    contra.add_evidence("cloud_check", 0.5, weight=0.2)
    contra.add_concern("0% water classified but GDACS RED", "CRITICAL")
    if contra.confidence_basis() == "evidence_contradicts":
        ok("CRITICAL concern -> evidence_contradicts")
    else:
        bad(f"expected evidence_contradicts, got {contra.confidence_basis()!r}")

    strong = ConfidenceTracker()
    strong.add_evidence("index_validation", 0.95, weight=0.3)
    strong.add_evidence("cloud_check", 0.95, weight=0.2)
    if strong.confidence_basis() == "evidence_supports":
        ok("high score, no concerns -> evidence_supports")
    else:
        bad(f"expected evidence_supports, got {strong.confidence_basis()!r}")


if __name__ == "__main__":
    print("=" * 64)
    print("PHASE 0b — WITHIN-WATER MEAN / evidence_contradicts FIX")
    print("=" * 64)
    test_affected_mean_index_computed()
    test_affected_mean_index_none_when_dry()
    test_low_fraction_flood_no_contradiction()
    test_zero_water_gdacs_red_still_contradicts()
    test_zero_water_no_gdacs_is_quiet()
    test_nonpositive_within_water_mean_flags_inconsistency()
    test_confidence_basis_weak_vs_contradicts()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
