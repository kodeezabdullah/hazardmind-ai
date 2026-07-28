"""HazardMind satellite agent — continuous confidence scoring.

The satellite agent is not a dumb pipeline: it constantly evaluates its own
confidence as evidence accumulates from every source it touches. ``ConfidenceTracker``
is the running ledger for one event — every cross-validation, cloud check, index
reading and expert opinion adds *evidence* (a 0..1 value with a weight) or raises
a *concern* (a severity that erodes confidence).

The overall confidence is a weighted average of the evidence, reduced by a
penalty per outstanding concern. Two thresholds drive behaviour in the pipeline:

* ``needs_verification()`` — confidence below 0.70: the agent should ask the
  orchestrator before handing off.
* ``should_alert_team()`` — any CRITICAL concern: alert the room immediately.

This module has no I/O and no LLM calls; it is pure bookkeeping so it can be
unit-tested deterministically and reasoned about cheaply.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Confidence below this means we are not sure enough to hand off silently —
# the agent should ask the orchestrator to confirm / suggest a different source.
VERIFICATION_THRESHOLD = 0.70

# How much each concern erodes the base (weighted-average) confidence.
_SEVERITY_PENALTY = {
    "LOW": 0.05,
    "MEDIUM": 0.10,
    "HIGH": 0.20,
    "CRITICAL": 0.35,
}

_VALID_SEVERITIES = tuple(_SEVERITY_PENALTY)


def _now() -> str:
    """An ISO-8601 UTC timestamp for evidence/concern bookkeeping."""
    return datetime.now(timezone.utc).isoformat()


class ConfidenceTracker:
    """Running confidence ledger for a single disaster analysis.

    Construct one per event. Feed it evidence and concerns as the pipeline and
    cross-validation proceed; query ``overall_confidence`` /
    ``needs_verification`` / ``should_alert_team`` to decide what to do next, and
    ``get_report`` for a compact snapshot to log or send to the room.
    """

    def __init__(self) -> None:
        # Kept for API parity with the spec; not currently read but useful for
        # callers that want to stash per-source latest values.
        self.scores: dict[str, float] = {}
        self.evidence: list[dict[str, Any]] = []
        self.concerns: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def add_evidence(self, source: str, value: float, weight: float) -> None:
        """Record a piece of evidence supporting (or undercutting) confidence.

        ``source`` — where it came from (satellite, gdacs, usgs, copernicus,
        cloud_check, index_validation, featherless_expert, ...).
        ``value`` — 0.0 (this source says the result is unreliable) .. 1.0 (this
        source strongly confirms the result). Clamped to [0, 1].
        ``weight`` — how much this source matters relative to the others.
        Non-positive weights are ignored (they would not contribute to the
        weighted average and a zero total weight is meaningless).
        """
        try:
            value = float(value)
            weight = float(weight)
        except (TypeError, ValueError):
            logger.warning("Ignoring non-numeric evidence from %s", source)
            return
        if weight <= 0:
            logger.warning("Ignoring non-positive-weight evidence from %s", source)
            return
        value = max(0.0, min(1.0, value))
        self.scores[source] = value
        self.evidence.append(
            {
                "source": source,
                "value": value,
                "weight": weight,
                "timestamp": _now(),
            }
        )

    def add_concern(self, concern: str, severity: str) -> None:
        """Record a concern that erodes confidence.

        ``severity`` — one of LOW / MEDIUM / HIGH / CRITICAL. An unknown
        severity is coerced to MEDIUM (and logged) so a typo never crashes the
        ledger or escapes the penalty model.
        """
        sev = (severity or "").strip().upper()
        if sev not in _VALID_SEVERITIES:
            logger.warning("Unknown severity %r for concern %r; using MEDIUM", severity, concern)
            sev = "MEDIUM"
        self.concerns.append({"concern": concern, "severity": sev, "timestamp": _now()})

    # ------------------------------------------------------------------ #
    # Scoring
    # ------------------------------------------------------------------ #
    def overall_confidence(self) -> float:
        """Weighted average of all evidence, reduced by concern penalties.

        Returns 0.0 when there is no evidence yet (we cannot be confident about
        nothing). The result is clamped to [0, 1].
        """
        if not self.evidence:
            return 0.0

        weighted_sum = sum(e["value"] * e["weight"] for e in self.evidence)
        total_weight = sum(e["weight"] for e in self.evidence)
        if total_weight <= 0:
            return 0.0
        base = weighted_sum / total_weight

        for concern in self.concerns:
            base -= _SEVERITY_PENALTY[concern["severity"]]

        return max(0.0, min(1.0, base))

    def needs_verification(self) -> bool:
        """True when confidence is too low to hand off without asking first."""
        return self.overall_confidence() < VERIFICATION_THRESHOLD

    def should_alert_team(self) -> bool:
        """True when there is at least one CRITICAL concern."""
        return any(c["severity"] == "CRITICAL" for c in self.concerns)

    def critical_concerns(self) -> list[dict[str, Any]]:
        """The subset of concerns at CRITICAL severity."""
        return [c for c in self.concerns if c["severity"] == "CRITICAL"]

    def confidence_basis(self) -> str:
        """Why the confidence number is what it is — legibility, not calibration.

        ``overall_confidence()`` alone cannot tell "we gathered no evidence"
        from "the evidence contradicts the result" — both read as near-zero to
        a caller that only sees the float. This makes that distinction
        explicit without changing the arithmetic:

        * ``"insufficient_evidence"`` — no evidence was ever gathered (every
          reachable source was unreachable/skipped). The pipeline could not
          say anything, favourable or not.
        * ``"evidence_contradicts"`` — at least one CRITICAL concern fired:
          something actively disagrees with the result (e.g. 0% water
          classified against a GDACS RED alert, >60% cloud on an optical
          run). Phase 0b (science/full-pass): this label previously ALSO
          fired whenever confidence merely sat below the verification
          threshold — conflating "we are not sure" with "the evidence
          disagrees", which made the basis field fire on 100% of baseline
          runs regardless of outcome quality (BASELINE_REPORT_2 §5) and
          uninformative. A merely-low score is now ``"evidence_weak"``.
        * ``"evidence_weak"`` — evidence was gathered, nothing actively
          contradicts the result, but the weighted score is below the
          verification threshold (sparse/low-quality evidence, stale scene,
          accumulated non-critical concerns). Verify before acting; nothing
          is known to disagree.
        * ``"evidence_supports"`` — evidence was gathered and the result reads
          as reliable.
        """
        if not self.evidence:
            return "insufficient_evidence"
        if self.should_alert_team():
            return "evidence_contradicts"
        if self.needs_verification():
            return "evidence_weak"
        return "evidence_supports"

    def get_report(self) -> dict[str, Any]:
        """A compact, JSON-serialisable snapshot of the current state."""
        return {
            "overall": round(self.overall_confidence(), 4),
            "evidence_count": len(self.evidence),
            "confidence_basis": self.confidence_basis(),
            "concerns": self.concerns,
            "needs_verification": self.needs_verification(),
            "should_alert": self.should_alert_team(),
        }


if __name__ == "__main__":
    # Tiny deterministic self-check (no network).
    logging.basicConfig(level=logging.INFO)
    t = ConfidenceTracker()
    print("empty ->", t.overall_confidence())
    t.add_evidence("gdacs", 0.9, 0.3)
    t.add_evidence("cloud_check", 0.95, 0.2)
    print("two pieces ->", round(t.overall_confidence(), 3))
    t.add_concern("Satellite 3x larger than GDACS", "HIGH")
    print("with HIGH concern ->", round(t.overall_confidence(), 3))
    t.add_concern("High cloud cover — optical unreliable", "CRITICAL")
    import json as _json

    print(_json.dumps(t.get_report(), indent=2))
