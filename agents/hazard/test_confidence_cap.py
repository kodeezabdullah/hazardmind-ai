"""Correctness tests for satellite->hazard confidence propagation (2026-07-27).

Root cause: analyzer.run_parallel_analysis never read the satellite's own
confidence at all, so a flood conclusion could report higher confidence than
the data it rested on. Live 2026-07-26 e2e: satellite confidence 0.0, hazard
still landed at 0.83.

Fix: flood confidence is capped at the satellite confidence carried in
satellite_data["analysis"]["confidence"] (populated by agent.py's
_normalise_satellite_payload from the satellite's self-reported "confidence").
Earthquake and landslide are NOT capped — they self-source from USGS/DEM and
are intentionally independent of satellite confidence.

Offline and deterministic: fetch_gdacs/fetch_usgs/fetch_slope and the LLM call
are monkeypatched so no network/LLM calls happen. run_parallel_analysis's LLM
call for flood is forced to fail (returns None) so the deterministic fallback
path (area/flood_index-driven confidence) is what gets capped -- this is also
the path that produced the highest fallback confidence (0.7), making it the
sharpest test of the cap.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyzer

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


async def _no_gdacs(bbox):
    return {"events": [], "count": 0, "source": "gdacs"}


async def _no_usgs(bbox, days=7):
    return {"earthquakes": [], "count": 0, "source": "usgs"}


async def _flat_slope(bbox):
    return {"available": True, "slope_estimate": 5.0, "source": "test"}


async def _no_llm(prompt, system="", criticality="normal"):
    return None  # forces every analyze_* deterministic fallback


def _satellite_data(confidence, affected_area_km2=500.0, mean_value=0.6):
    """A flood satellite payload whose deterministic fallback would normally
    score CRITICAL/0.7 confidence (area>200 and flood_index>0.5) absent a cap."""
    return {
        "event_id": "test-event",
        "boundaries": {"bbox": [70.0, 33.0, 71.0, 34.0], "risk_cities": ["TestCity"]},
        "analysis": {
            "affected_area_km2": affected_area_km2,
            "mean_value": mean_value,
            "water_percent": 40.0,
            "index_type": "NDWI",
            "confidence": confidence,
        },
        "satellite": {"type": "sentinel-2"},
    }


def _run(coro):
    return asyncio.run(coro)


def test_flood_confidence_capped_by_low_satellite_confidence():
    analyzer.fetch_gdacs = _no_gdacs
    analyzer.fetch_usgs = _no_usgs
    analyzer.fetch_slope = _flat_slope
    analyzer.smart_llm_call = _no_llm

    result = _run(analyzer.run_parallel_analysis(_satellite_data(confidence=0.0)))

    flood_conf = result["confidence_scores"]["flood"]
    if flood_conf == 0.0:
        ok(f"flood confidence capped to satellite confidence 0.0 (got {flood_conf})")
    else:
        bad(f"flood confidence NOT capped: expected 0.0, got {flood_conf}")

    if result.get("confidence_cap_applied") is True:
        ok("confidence_cap_applied recorded True for visibility in pipeline_log")
    else:
        bad(f"confidence_cap_applied not recorded (got {result.get('confidence_cap_applied')})")


def test_flood_confidence_uncapped_when_satellite_confident():
    analyzer.fetch_gdacs = _no_gdacs
    analyzer.fetch_usgs = _no_usgs
    analyzer.fetch_slope = _flat_slope
    analyzer.smart_llm_call = _no_llm

    result = _run(analyzer.run_parallel_analysis(_satellite_data(confidence=0.95)))

    flood_conf = result["confidence_scores"]["flood"]
    # Deterministic fallback for area=500/index=0.6 -> CRITICAL/0.7, below the
    # satellite's 0.95, so the cap must NOT kick in and 0.7 must ride through.
    if flood_conf == 0.7:
        ok(f"flood confidence NOT capped when satellite is more confident (got {flood_conf})")
    else:
        bad(f"unexpected flood confidence {flood_conf} (expected uncapped 0.7)")

    if result.get("confidence_cap_applied") is False:
        ok("confidence_cap_applied recorded False when cap did not trigger")
    else:
        bad(f"confidence_cap_applied should be False (got {result.get('confidence_cap_applied')})")


def test_earthquake_and_landslide_not_capped():
    """Earthquake/landslide are deterministic from USGS/DEM, independent of
    satellite confidence -- a near-zero satellite confidence must NOT drag
    them down."""
    analyzer.fetch_gdacs = _no_gdacs
    analyzer.fetch_usgs = _no_usgs
    analyzer.fetch_slope = _flat_slope  # slope 5.0 -> LOW risk, confidence 0.8
    analyzer.smart_llm_call = _no_llm

    result = _run(analyzer.run_parallel_analysis(_satellite_data(confidence=0.0)))

    eq_conf = result["confidence_scores"]["earthquake"]
    ls_conf = result["confidence_scores"]["landslide"]

    if eq_conf == 0.85:
        ok(f"earthquake confidence uncapped by satellite confidence 0.0 (got {eq_conf})")
    else:
        bad(f"earthquake confidence unexpectedly affected: {eq_conf}")

    if ls_conf == 0.8:
        ok(f"landslide confidence uncapped by satellite confidence 0.0 (got {ls_conf})")
    else:
        bad(f"landslide confidence unexpectedly affected: {ls_conf}")


def test_missing_satellite_confidence_does_not_crash():
    """If the satellite payload has no confidence key at all (older payload
    shape / a stage that failed to self-report), the cap must be skipped
    cleanly rather than crashing or wrongly zeroing flood confidence."""
    analyzer.fetch_gdacs = _no_gdacs
    analyzer.fetch_usgs = _no_usgs
    analyzer.fetch_slope = _flat_slope
    analyzer.smart_llm_call = _no_llm

    data = _satellite_data(confidence=None)
    result = _run(analyzer.run_parallel_analysis(data))

    flood_conf = result["confidence_scores"]["flood"]
    if flood_conf == 0.7:
        ok(f"flood confidence unaffected when satellite confidence absent (got {flood_conf})")
    else:
        bad(f"unexpected flood confidence with no satellite confidence: {flood_conf}")
    if result.get("confidence_cap_applied") is False:
        ok("confidence_cap_applied False when satellite confidence is absent")
    else:
        bad(f"confidence_cap_applied should be False when absent (got {result.get('confidence_cap_applied')})")


if __name__ == "__main__":
    print("=" * 60)
    print("TEST: satellite -> hazard confidence propagation (flood cap)")
    print("=" * 60)
    test_flood_confidence_capped_by_low_satellite_confidence()
    test_flood_confidence_uncapped_when_satellite_confident()
    test_earthquake_and_landslide_not_capped()
    test_missing_satellite_confidence_does_not_crash()
    print("=" * 60)
    print(f"SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 60)
    if FAIL:
        sys.exit(1)
