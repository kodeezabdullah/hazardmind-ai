"""Correctness tests for the SAR-as-NDWI mislabeling fix (2026-07-27).

Root cause: agent.py's validation_input dict used to hardcode
"mean_ndwi": result.get("mean_index") regardless of what index was actually
computed, so a Sentinel-1 SAR run's raw uncalibrated dB value got read as an
NDWI ratio by cross_validator's physics check — adding false
confidence-boosting "evidence" from a value that was never an NDWI ratio.

Covers:
  - cross_validator only applies NDWI thresholds when index_type == "NDWI"
    and index_calibrated is not False.
  - a SAR result (index_type="SAR", index_calibrated=False) never triggers
    the NDWI evidence/concern branches, regardless of its raw mean_index value.
  - the validation_input contract itself: index_type must be present and must
    equal whatever the pipeline computed (exercised via the assertion added in
    agent.py's run_pipeline, tested here by asserting cross_validator's
    behavior is driven entirely by index_type/index_calibrated, not a
    separately-set label).

Offline and deterministic — no network, no CDSE, no LLM.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from confidence_tracker import ConfidenceTracker
from cross_validator import CrossValidator

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


class _NoNetworkValidator(CrossValidator):
    """No GDACS/USGS/Featherless — isolates the index-physics check (#4)."""

    def check_gdacs(self, location):
        return None

    def check_usgs(self, location):
        return None

    def get_featherless_opinion(self, result, validations, tracker):
        return None


def test_sar_never_labeled_ndwi():
    """A SAR result must never produce NDWI evidence/concerns, even when its
    raw dB value would satisfy an NDWI threshold numerically (e.g. 23.65 > 0.3
    — the exact live value observed on the 2026-07-26 e2e run)."""
    v = _NoNetworkValidator()
    trk = ConfidenceTracker()
    sar_result = {
        "affected_area_km2": 100.0,
        "cloud_cover": 5,
        "index_type": "SAR",
        "index_calibrated": False,
        "index_units": "dB_uncalibrated",
        "mean_index": 23.6485,  # the live 2026-07-26 raw SAR value
        "water_percent": 0.0,
        "coverage_percent": 100,
    }
    findings = v.validate_all(sar_result, "flood", {"lat": 33.6, "lon": 73.0}, trk)

    index_findings = [f for f in findings if f["source"] == "INDEX"]
    if any(f["status"] == "SKIPPED" for f in index_findings):
        ok("SAR result: index check explicitly SKIPPED, not evaluated as NDWI")
    else:
        bad(f"SAR result: expected a SKIPPED index finding, got {index_findings}")

    sources = [e["source"] for e in trk.evidence]
    if "index_validation" not in sources:
        ok(f"SAR result: index_validation absent from evidence ledger ({sources})")
    else:
        bad(f"SAR result: index_validation was added despite SAR input ({sources})")


def test_ndwi_still_validated_when_calibrated():
    """A genuine calibrated NDWI result must still be evaluated (the fix must
    not silently disable the check for the case it's actually meant for)."""
    v = _NoNetworkValidator()
    trk = ConfidenceTracker()
    ndwi_result = {
        "affected_area_km2": 100.0,
        "cloud_cover": 5,
        "index_type": "NDWI",
        "index_calibrated": True,
        "index_units": "NDWI_ratio",
        "mean_index": 0.4,
        "water_percent": 30.0,
        "coverage_percent": 100,
    }
    findings = v.validate_all(ndwi_result, "flood", {"lat": 33.6, "lon": 73.0}, trk)
    sources = [e["source"] for e in trk.evidence]
    if "index_validation" in sources:
        ok("Calibrated NDWI result: index_validation evidence still added")
    else:
        bad(f"Calibrated NDWI result: index_validation missing ({sources}, findings={findings})")


def test_validation_input_index_type_matches_result():
    """agent.py's validation_input dict (built right before cross_validator
    is called, agent.py ~L922) must carry the SAME index_type
    process_satellite_imagery actually computed — never a separately-set/
    defaulted value.

    NOTE (TESTING_GAP_AUDIT.md, 2026-07-28): this test used to hand-build a
    dict mirroring agent.py's construction and assert the assertion logic
    works on the simulation — it never called agent.run_pipeline, so a
    divergence in the REAL dict construction (exactly the islamabad-findings
    #1 defect class: a field landing under the wrong key) would not have
    been caught. Rewritten to call the real agent.run_pipeline() with
    cross_validator.validate_all patched to capture the REAL
    validation_input it receives, for both a SAR and an NDWI run."""
    import asyncio
    import json
    import os
    import sys as _sys

    tests_dir = os.path.dirname(os.path.abspath(__file__))
    if tests_dir not in _sys.path:
        _sys.path.insert(0, tests_dir)
    import test_verify_islamabad_fixes as t
    import agent as satellite_agent

    for computed_index_type, index_calibrated in (("SAR", False), ("NDWI", True)):
        captured = {}

        class _CapturingValidator:
            def validate_all(self, validation_input, disaster_type, bbox, tracker):
                captured["validation_input"] = validation_input
                tracker.add_evidence("stub", 0.9, weight=1.0)
                return [{"source": "STUB", "status": "CONFIRMED", "detail": "stub"}]

        search_calls = []
        mocks = t._common_mocks(search_calls)
        mocks["process_satellite_imagery"] = lambda *a, **k: t._process_result(
            index_type=computed_index_type, index_calibrated=index_calibrated,
        )
        mocks["cross_validator"] = _CapturingValidator()
        restore = t._install(mocks)
        try:
            params = satellite_agent.ProcessDisasterInput(
                event_id=f"evt-index-label-{computed_index_type}",
                location="Islamabad, Pakistan", disaster_type="flood", magnitude=0,
            )
            raw = asyncio.run(satellite_agent.run_pipeline(params))
        finally:
            restore()

        result = json.loads(raw)
        if result.get("status") != "complete":
            bad(f"pipeline did not complete for {computed_index_type}: {result}")
            continue

        validation_input = captured.get("validation_input") or {}
        if validation_input.get("index_type") == computed_index_type:
            ok(f"the REAL validation_input passed to cross_validator carries "
               f"index_type={computed_index_type!r}, matching what "
               f"process_satellite_imagery actually computed")
        else:
            bad(f"validation_input.index_type diverged for "
                f"{computed_index_type!r}: got {validation_input.get('index_type')!r}")


if __name__ == "__main__":
    print("=" * 60)
    print("TEST: index label integrity (SAR-as-NDWI fix)")
    print("=" * 60)
    test_sar_never_labeled_ndwi()
    test_ndwi_still_validated_when_calibrated()
    test_validation_input_index_type_matches_result()
    print("=" * 60)
    print(f"SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 60)
    if FAIL:
        sys.exit(1)
