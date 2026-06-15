"""TEST SUITE 7: Confidence / Cross-validation / Stance.

Covers the four scenarios from the spec for the intelligent expert layer
(confidence_tracker, cross_validator, stance_engine). These run OFFLINE and
DETERMINISTICALLY: the external feeds (GDACS / USGS) and the Featherless model
chain are stubbed, so the suite asserts the agent's *logic* (does it flag a
discrepancy? drop confidence? push back?) without depending on network or
non-deterministic LLM output.

  Test 1: Normal flow         — clean result -> high confidence, no alert.
  Test 2: GDACS discrepancy   — satellite 500 km^2 vs GDACS 120 km^2 -> HIGH
                                concern + DISCREPANCY finding.
  Test 3: Low confidence      — concerns push confidence below 0.70 ->
                                needs_verification() true.
  Test 4: Stance disagreement — orchestrator says "use SAR" at 15% cloud ->
                                agent pushes back with reasoning, will comply.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from confidence_tracker import ConfidenceTracker  # noqa: E402
from cross_validator import CrossValidator  # noqa: E402
from stance_engine import StanceEngine  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL, WARN = [], [], []


def ok(test, msg):
    PASS.append(test)
    print(f"  PASS [{test}]: {msg}")


def bad(test, msg):
    FAIL.append(test)
    print(f"  FAIL [{test}]: {msg}")


def warn(test, msg):
    WARN.append(test)
    print(f"  WARN [{test}]: {msg}")


# --------------------------------------------------------------------------- #
# Stubs — no network, no LLM
# --------------------------------------------------------------------------- #
class _Validator(CrossValidator):
    """CrossValidator with the external feeds + Featherless opinion stubbed."""

    def __init__(self, gdacs=None, usgs=None, opinion=None):
        super().__init__(intelligence=object())  # never used (opinion stubbed)
        self._gdacs = gdacs
        self._usgs = usgs
        self._opinion = opinion

    def check_gdacs(self, location):
        return self._gdacs

    def check_usgs(self, location, days=14, min_magnitude=4.5):
        return self._usgs

    def get_featherless_opinion(self, result, validations, tracker):
        return self._opinion


class _StubIntel:
    """Intelligence layer whose JSON completion is a fixed canned reply."""

    def __init__(self, reply):
        self._reply = reply

    def _complete_json(self, prompt, **kw):
        return self._reply


# --------------------------------------------------------------------------- #
# Test 1 — normal flow
# --------------------------------------------------------------------------- #
def test_1_normal():
    print("\nTest 1: Normal flow (clean result)")
    trk = ConfidenceTracker()
    v = _Validator(
        gdacs={"alert": "ORANGE", "area": 110.0, "distance_km": 8.0},
        opinion={"reliable": True, "confidence": 0.9, "concerns": [], "alert_team": False,
                 "recommendation": "looks solid"},
    )
    res = {"affected_area_km2": 100.0, "cloud_cover": 8, "mean_ndwi": 0.4,
           "water_percent": 30, "coverage_percent": 95}
    findings = v.validate_all(res, "flood", {"lat": 34.0, "lon": 71.5}, trk)

    conf = trk.overall_confidence()
    if conf >= 0.70:
        ok("T7.1", f"confidence {conf:.2f} >= 0.70")
    else:
        bad("T7.1", f"confidence {conf:.2f} unexpectedly low")
    if not trk.should_alert_team():
        ok("T7.1", "no team alert (no CRITICAL concerns)")
    else:
        bad("T7.1", "alerted team on a clean result")
    if any(f["source"] == "GDACS" and f["status"] == "CONFIRMED" for f in findings):
        ok("T7.1", "GDACS area CONFIRMED (within 30%)")
    else:
        bad("T7.1", f"GDACS not confirmed: {findings}")


# --------------------------------------------------------------------------- #
# Test 2 — GDACS discrepancy (satellite 500 vs GDACS 120)
# --------------------------------------------------------------------------- #
def test_2_gdacs_discrepancy():
    print("\nTest 2: GDACS discrepancy (sat 500 km^2 vs GDACS 120 km^2)")
    trk = ConfidenceTracker()
    v = _Validator(
        gdacs={"alert": "RED", "area": 120.0, "distance_km": 5.0},
        opinion=None,
    )
    res = {"affected_area_km2": 500.0, "cloud_cover": 10, "mean_ndwi": 0.4,
           "water_percent": 30, "coverage_percent": 90}
    findings = v.validate_all(res, "flood", {"lat": 34.0, "lon": 71.5}, trk)

    disc = [f for f in findings if f["source"] == "GDACS" and f["status"] == "DISCREPANCY"]
    if disc:
        ok("T7.2", f"flagged DISCREPANCY: {disc[0]['detail']}")
    else:
        bad("T7.2", f"no GDACS discrepancy flagged: {findings}")
    if any(c["severity"] == "HIGH" for c in trk.concerns):
        ok("T7.2", "raised a HIGH concern")
    else:
        bad("T7.2", f"no HIGH concern: {trk.concerns}")
    # 500/120 = 4.17x -> ratio > 2 branch.
    if any("larger" in c["concern"] for c in trk.concerns):
        ok("T7.2", "concern names the over-estimate")
    else:
        warn("T7.2", "concern wording unexpected")


# --------------------------------------------------------------------------- #
# Test 3 — low confidence triggers verification
# --------------------------------------------------------------------------- #
def test_3_low_confidence():
    print("\nTest 3: Low confidence (concerns push below 0.70)")
    trk = ConfidenceTracker()
    # Moderate evidence...
    trk.add_evidence("index_validation", 0.7, weight=0.3)
    trk.add_evidence("gdacs", 0.6, weight=0.3)
    # ...eroded by concerns.
    trk.add_concern("Moderate cloud — partial obscuration", "MEDIUM")
    trk.add_concern("Only 40% AOI coverage", "HIGH")

    conf = trk.overall_confidence()
    if conf < 0.70:
        ok("T7.3", f"confidence {conf:.2f} < 0.70")
    else:
        bad("T7.3", f"confidence {conf:.2f} not low enough")
    if trk.needs_verification():
        ok("T7.3", "needs_verification() is True -> agent should ask orchestrator")
    else:
        bad("T7.3", "needs_verification() False despite low confidence")

    # CRITICAL concern must trip the team-alert flag.
    trk.add_concern("High cloud cover — optical unreliable", "CRITICAL")
    if trk.should_alert_team():
        ok("T7.3", "CRITICAL concern -> should_alert_team() True")
    else:
        bad("T7.3", "CRITICAL concern did not trip alert")


# --------------------------------------------------------------------------- #
# Test 4 — stance disagreement (use SAR at 15% cloud)
# --------------------------------------------------------------------------- #
def test_4_stance_disagreement():
    print("\nTest 4: Stance disagreement (orchestrator: 'use SAR' at 15% cloud)")
    # Canned expert reply: disagree, high self-confidence, will comply if insisted.
    stub = _StubIntel(
        {
            "agree": False,
            "confidence_in_own_position": 0.85,
            "reasoning": "Cloud cover is only 15% — Sentinel-2 optical is reliable and "
            "gives spectral detail SAR cannot.",
            "recommendation": "Stay on Sentinel-2 optical.",
            "response_to_orchestrator": "I'd hold off on SAR here — the sky is clear.",
            "will_comply_if_insisted": True,
        }
    )
    eng = StanceEngine(intelligence=stub)
    trk = ConfidenceTracker()
    trk.add_evidence("cloud_check", 0.95, weight=0.2)
    stance = eng.evaluate_orchestrator_instruction(
        "switch to Sentinel-1 SAR", {"cloud_cover": 15, "satellite_type": "sentinel-2"}, trk
    )

    if stance.get("agree") is False:
        ok("T7.4", "agent pushed back (agree=False)")
    else:
        bad("T7.4", f"agent did not push back: {stance}")
    if (stance.get("reasoning") or "").strip():
        ok("T7.4", "stance carries reasoning")
    else:
        bad("T7.4", "no reasoning in stance")

    msg = eng.form_band_message(stance, "@hazardmind-orchestrator")
    if msg.startswith("@hazardmind-orchestrator"):
        ok("T7.4", "Band message addressed to orchestrator")
    else:
        bad("T7.4", f"message not addressed correctly: {msg[:40]!r}")
    if "85%" in msg and "evidence suggests" in msg:
        ok("T7.4", "message states confidence + evidence")
    else:
        bad("T7.4", f"message missing confidence/evidence: {msg!r}")
    if "insist" in msg.lower():
        ok("T7.4", "message signals it will comply if insisted")
    else:
        bad("T7.4", "message omits the will-comply-if-insisted note")

    # Fallback: when the LLM is unavailable, default to comply (not insubordinate).
    eng_down = StanceEngine(intelligence=_StubIntel(None))
    fb = eng_down.evaluate_orchestrator_instruction("use SAR", {}, trk)
    if fb.get("agree") and fb.get("will_comply_if_insisted"):
        ok("T7.4", "LLM-down fallback defaults to comply")
    else:
        bad("T7.4", f"fallback not safe: {fb}")


if __name__ == "__main__":
    print("=" * 60)
    print("TEST SUITE 7: Confidence / Cross-validation / Stance")
    print("=" * 60)
    test_1_normal()
    test_2_gdacs_discrepancy()
    test_3_low_confidence()
    test_4_stance_disagreement()
    print("\n" + "=" * 60)
    print(f"SUITE 7 SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)} WARN={len(WARN)}")
    if FAIL:
        print("FAILED checks:", FAIL)
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
