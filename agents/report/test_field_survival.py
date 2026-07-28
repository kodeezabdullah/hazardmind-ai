"""Field-survival test for report confidence aggregation and index_type/
index_calibrated propagation (TESTING_GAP_AUDIT.md follow-up, 2026-07-28).

test_confidence_aggregation.py (audited) calls calculate_confidence_level /
_collect_confidence_values directly with hand-built report dicts. Its own
test_satellite_zero_via_dedicated_channel_ONLY_cannot_yield_high names
agents/report/node.py's wiring as the thing it's protecting, then tests a
hand-built dict shaped like what that wiring is *supposed* to produce --
never invoking node.py or run_report_pipeline to confirm the wiring itself
still does that.

This file calls agents.report.pipeline.run_report_pipeline directly (the
real function agents/report/node.py's report_node calls) with
fetch_from_db=False/upload_r2=False/write_db=False/use_llm=False (the same
offline contract-test mode agent.py's --contract-test --no-llm CLI flag
uses -- confirmed working via `python agent.py demo-peshawar-flood
--contract-test --no-llm`), and a real incoming_payload carrying
confidence_scores the way report_node.py actually builds it
(`{"confidence_scores": state.get("confidence_scores") or {}}`). This
exercises the REAL _merge_incoming_payload_into_context ->
calculate_confidence_level chain, not a hand-built report dict.

Offline and deterministic: no DB, no R2, no LLM (use_llm=False triggers the
existing deterministic template path already used by the CLI contract-test
mode).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


def _run(coro):
    return asyncio.run(coro)


def test_satellite_zero_confidence_via_real_run_report_pipeline_cannot_yield_high():
    """The exact H#6 scenario, exercised through the real entry point: a
    near-zero satellite confidence passed the way report_node.py actually
    passes it (incoming_payload={"confidence_scores": {...}}) must not let
    MOCK_EVENT_DATA's hazard (0.91/0.67/0.54) or impact confidence mask it
    into a HIGH/MEDIUM confidence_level."""
    print("\n[Field survival] satellite confidence_scores['satellite']=0.0, "
          "passed via run_report_pipeline's real incoming_payload contract, "
          "cannot yield a non-LOW confidence_level")

    from pipeline import run_report_pipeline

    result = _run(run_report_pipeline(
        event_id="evt-report-confidence-survival",
        fetch_from_db=False,
        upload_r2=False,
        write_db=False,
        use_llm=False,
        allow_contract_side_effects=False,
        incoming_payload={"confidence_scores": {"satellite": 0.0}},
    ))

    if result.get("status") not in ("complete", "complete_with_warnings"):
        bad(f"run_report_pipeline did not complete: {result}")
        return
    ok(f"run_report_pipeline completed via the real entry point "
       f"(status={result.get('status')})")

    confidence_level = result.get("confidence_level")
    if confidence_level == "LOW":
        ok(f"confidence_level=LOW with satellite confidence 0.0 threaded "
           f"through the REAL incoming_payload -> "
           f"_merge_incoming_payload_into_context -> "
           f"calculate_confidence_level chain (not a hand-built report dict)")
    else:
        bad(f"expected LOW, got {confidence_level!r} -- satellite's 0.0 was "
            f"masked by MOCK_EVENT_DATA's hazard/impact confidence "
            f"(0.91/0.67/0.54) despite the real wiring")


def test_satellite_confidence_value_is_actually_read_not_ignored():
    """Non-vacuousness check for the test above: prove satellite confidence
    is actually READ into the min()-dominant calculation (not silently
    ignored so the LOW result above would happen regardless of the value),
    without relying on it single-handedly flipping the verdict -- min()
    over MOCK_EVENT_DATA's own hazard scores (0.91/0.67/0.54) already floors
    confidence_level at LOW/MEDIUM (landslide=0.54 alone caps it below HIGH),
    so asserting satellite=0.95 forces HIGH would be a false expectation
    about the aggregation rule, not a real regression to catch.

    Calls db_client._collect_confidence_values directly on the REAL context
    dict run_report_pipeline builds (via _merge_incoming_payload_into_context)
    -- not a hand-built dict -- to confirm the satellite value the real
    pipeline produced is actually one of the collected values."""
    print("\n[Field survival] the satellite confidence value survives into "
          "_collect_confidence_values's real input, not just always LOW "
          "regardless of what's passed (non-vacuousness check)")

    from copy import deepcopy
    from generator import MOCK_EVENT_DATA
    from pipeline import _merge_incoming_payload_into_context
    from db_client import _collect_confidence_values

    context = deepcopy(MOCK_EVENT_DATA)
    _merge_incoming_payload_into_context(context, {"confidence_scores": {"satellite": 0.95}})

    if context.get("confidence", {}).get("satellite_confidence") == 0.95:
        ok("the real _merge_incoming_payload_into_context wrote satellite "
           "confidence 0.95 into context['confidence']['satellite_confidence']")
    else:
        bad(f"satellite confidence not merged into context as expected: "
            f"{context.get('confidence')}")

    values = _collect_confidence_values(context)
    if 0.95 in values:
        ok(f"0.95 is present among the real collected confidence values "
           f"{values} -- satellite confidence is genuinely read, not "
           f"ignored (the LOW verdict in the test above is driven by "
           f"min() over ALL stages, including MOCK_EVENT_DATA's own "
           f"landslide=0.54, not because satellite is never read)")
    else:
        bad(f"0.95 missing from collected confidence values: {values}")


def test_roads_blocked_km_precision_not_lost_via_legacy_column():
    """Coherence-pass fix (feat/durable-evidence-trail, 2026-07-28):
    build_structured_report_context previously read impact.get("roads_blocked")
    (the legacy INTEGER mirror, int(round(roads_blocked_km))) and labelled it
    "roads_blocked_km" in the report context -- silently truncating precision
    (a real 4.6 km value would report as 5). roads_blocked_km is CANONICAL
    (see root CLAUDE.md's "roads_blocked vs roads_blocked_km RESOLVED" entry
    and shared/db/schema.sql's column comment); this proves the real
    db_client.build_structured_report_context function now prefers it, via a
    realistic row dict shaped like what impact_data actually returns (both
    columns present, written from the same source per H#14), not a
    hand-built context simulating the fix."""
    from db_client import build_structured_report_context, db_context_to_report_context

    impact_row = {
        "event_id": "evt-roads-blocked-precision",
        "total_affected": 100,
        "high_risk_people": 20,
        "medium_risk_people": 50,
        "hospitals_at_risk": 1,
        "schools_at_risk": 2,
        "roads_blocked": 5,        # legacy mirror: int(round(4.6)) = 5
        "roads_blocked_km": 4.6,   # canonical
        "bridges_at_risk": 0,
        "vulnerability_score": "MEDIUM",
        "evacuation_routes": [],
        "estimated_evacuation_time": "2 hours",
        "overall_confidence": 0.7,
    }

    db_context = build_structured_report_context(
        event_id="evt-roads-blocked-precision",
        event={"event_id": "evt-roads-blocked-precision"},
        satellite_results=[],
        hazard_zones=[],
        impact_data=impact_row,
    )
    context = db_context_to_report_context(db_context)

    got = context["impact"]["roads_blocked_km"]
    if got == 4.6:
        ok(f"roads_blocked_km={got!r} preserved from the canonical column "
           f"via the real build_structured_report_context (not truncated "
           f"to the legacy roads_blocked=5 mirror)")
    else:
        bad(f"expected roads_blocked_km=4.6 (canonical), got {got!r} "
            f"-- precision lost via the legacy roads_blocked mirror")

    # Pre-H#14 row: roads_blocked_km was never written (column added after
    # roads_blocked existed) -- must fall back to the legacy column rather
    # than reporting 0/None and silently discarding a real historical value.
    old_row = dict(impact_row)
    old_row["roads_blocked_km"] = None
    old_row["roads_blocked"] = 9

    old_db_context = build_structured_report_context(
        event_id="evt-roads-blocked-legacy-fallback",
        event={"event_id": "evt-roads-blocked-legacy-fallback"},
        satellite_results=[],
        hazard_zones=[],
        impact_data=old_row,
    )
    old_context = db_context_to_report_context(old_db_context)
    got_old = old_context["impact"]["roads_blocked_km"]
    if got_old == 9:
        ok(f"pre-H#14 row (roads_blocked_km=NULL) correctly falls back to "
           f"the legacy roads_blocked=9 column via the real function")
    else:
        bad(f"expected fallback to legacy roads_blocked=9 for a pre-H#14 "
            f"row, got {got_old!r}")


if __name__ == "__main__":
    print("=" * 70)
    print("TEST: report field survival (islamabad-findings audit follow-up)")
    print("=" * 70)
    test_satellite_zero_confidence_via_real_run_report_pipeline_cannot_yield_high()
    test_satellite_confidence_value_is_actually_read_not_ignored()
    test_roads_blocked_km_precision_not_lost_via_legacy_column()
    print("=" * 70)
    print(f"SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 70)
    if FAIL:
        sys.exit(1)
