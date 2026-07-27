"""Correctness tests for report confidence aggregation (2026-07-27).

Root cause: db_client._collect_confidence_values never included satellite
confidence at all, and calculate_confidence_level averaged the collected
values -- a flat average lets one high-confidence stage mask a near-zero one.
Live 2026-07-26 e2e: satellite confidence 0.0, hazard/impact both ~0.83,
final_reports.confidence_level came out HIGH.

Fix: calculate_confidence_level is now min-dominant (overall = min(values),
not mean), and _collect_confidence_values reads a satellite confidence key
when a caller supplies one, so it's no longer silently dropped once one
becomes available.

Offline and deterministic -- no DB, no network.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_client import calculate_confidence_level, _collect_confidence_values

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


def _live_incident_report():
    """Reconstructs the exact live-observed shape from the 2026-07-26 e2e run:
    satellite confidence 0.0, but hazard (flood/earthquake/landslide, all
    self-generated pre-fix) and impact both landed at 0.83."""
    return {
        "confidence": {"satellite_confidence": 0.0},
        "hazard": {"confidence_scores": {"overall": 0.83, "flood": 0.83, "earthquake": 0.83, "landslide": 0.83}},
        "impact": {"overall_confidence": 0.83},
    }


def test_satellite_zero_confidence_cannot_yield_high():
    """The exact live-observed failure: satellite 0.0, hazard/impact ~0.83."""
    report = _live_incident_report()
    level = calculate_confidence_level(report)
    if level == "LOW":
        ok(f"satellite confidence 0.0 -> overall LOW (not HIGH), got {level}")
    else:
        bad(f"expected LOW, got {level}")


def test_old_behavior_would_have_masked_this():
    """Sanity check that the PRE-FIX bug really would have produced HIGH for
    this exact case (guards against the regression test being vacuous). The
    pre-fix bug was two-fold: (1) satellite confidence was never collected at
    all, and (2) the collected values were flat-averaged. Reproduce both to
    confirm the old code path would have missed this."""
    report = _live_incident_report()
    # Simulate pre-fix collection: no satellite key read at all.
    pre_fix_values = [
        v for v in _collect_confidence_values(report) if v != 0.0
    ]  # drop the satellite 0.0 the OLD code never collected
    old_style_average = sum(pre_fix_values) / len(pre_fix_values)
    if old_style_average >= 0.8:
        ok(f"confirmed pre-fix collection+average ({old_style_average:.2f}) over {pre_fix_values} would have read HIGH -- regression guard is meaningful")
    else:
        bad(f"pre-fix-style average {old_style_average:.2f} over {pre_fix_values} unexpectedly not HIGH -- test may not be meaningful")


def test_all_high_confidence_yields_high():
    report = {
        "hazard": {"confidence_scores": {"overall": 0.9, "flood": 0.9, "earthquake": 0.9, "landslide": 0.9}},
        "impact": {"overall_confidence": 0.9},
    }
    level = calculate_confidence_level(report)
    if level == "HIGH":
        ok(f"all-high inputs -> HIGH, got {level}")
    else:
        bad(f"expected HIGH, got {level}")


def test_no_values_yields_unknown():
    level = calculate_confidence_level({})
    if level == "UNKNOWN":
        ok(f"empty report -> UNKNOWN, got {level}")
    else:
        bad(f"expected UNKNOWN, got {level}")


def test_mid_range_yields_medium():
    report = {
        "hazard": {"confidence_scores": {"overall": 0.65, "flood": 0.65, "earthquake": 0.7, "landslide": 0.7}},
        "impact": {"overall_confidence": 0.65},
    }
    level = calculate_confidence_level(report)
    if level == "MEDIUM":
        ok(f"mid-range inputs (min 0.65) -> MEDIUM, got {level}")
    else:
        bad(f"expected MEDIUM, got {level}")


def test_satellite_confidence_key_is_collected():
    report = {"confidence": {"satellite_confidence": 0.42}}
    values = _collect_confidence_values(report)
    if 0.42 in values:
        ok(f"satellite_confidence 0.42 collected into values {values}")
    else:
        bad(f"satellite_confidence not collected: {values}")


def test_satellite_nested_key_is_collected():
    report = {"satellite": {"confidence": 0.13}}
    values = _collect_confidence_values(report)
    if 0.13 in values:
        ok(f"report.satellite.confidence 0.13 collected into values {values}")
    else:
        bad(f"nested satellite confidence not collected: {values}")


def test_satellite_zero_via_dedicated_channel_ONLY_cannot_yield_high():
    """H#6 strengthening: prove the guarantee holds via the channel fa0d9bd
    was ACTUALLY built for (confidence.satellite_confidence /
    report.satellite.confidence), with the previously-load-bearing redundant
    channel (hazard_scores["flood"]) masked out entirely -- flood confidence
    here is a normal, un-capped 0.83, same as earthquake/landslide/impact.
    Before agents/report/node.py was wired to pass PipelineState's
    confidence_scores as incoming_payload (H#6 fix), this exact scenario
    would have read HIGH, because the ONLY zero in the whole values list
    lived in a channel report_node never populated on the live call path.
    This test would have FAILED under that pre-fix code and must keep
    failing if a future refactor removes the dedicated channel while also
    "cleaning up" the redundant hazard_scores["flood"] read -- catching
    exactly the fragility SYSTEM_ANALYSIS.md F.6/H#6 warned about.
    """
    report = {
        "confidence": {"satellite_confidence": 0.0},
        "satellite": {"confidence": 0.0},
        # flood confidence deliberately NOT capped here (0.83, same as the
        # other two hazard types) -- the old redundant channel is masked out
        # by construction, not by deleting code, so this test independently
        # verifies the dedicated channel alone is sufficient.
        "hazard": {"confidence_scores": {"overall": 0.83, "flood": 0.83, "earthquake": 0.83, "landslide": 0.83}},
        "impact": {"overall_confidence": 0.83},
    }
    level = calculate_confidence_level(report)
    if level == "LOW":
        ok("satellite 0.0 via the DEDICATED channel alone (hazard_scores['flood'] masked out at 0.83) -> LOW, not HIGH")
    else:
        bad(f"expected LOW using only the dedicated satellite_confidence channel, got {level} -- the fa0d9bd-intended channel is not load-bearing")


if __name__ == "__main__":
    print("=" * 60)
    print("TEST: report confidence aggregation (min-dominant)")
    print("=" * 60)
    test_satellite_zero_confidence_cannot_yield_high()
    test_old_behavior_would_have_masked_this()
    test_all_high_confidence_yields_high()
    test_no_values_yields_unknown()
    test_mid_range_yields_medium()
    test_satellite_confidence_key_is_collected()
    test_satellite_nested_key_is_collected()
    test_satellite_zero_via_dedicated_channel_ONLY_cannot_yield_high()
    print("=" * 60)
    print(f"SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 60)
    if FAIL:
        sys.exit(1)
