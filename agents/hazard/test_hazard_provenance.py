"""Tests for landslide/earthquake evidence provenance (islamabad-findings #3).

Root cause: analyze_landslide received the full slope_data dict (available/
source/elevation_min_m/elevation_max_m/samples) but only ever extracted
slope_estimate — a genuinely flat DEM reading and a failed-DEM conservative
default (10.0 degrees) both produce identical LOW output with no way to tell
them apart downstream. analyze_earthquake had the same gap: a USGS fetch
failure degrades to the same {"count": 0} shape as a genuine no-seismicity
result.

Fix: both functions now return an `evidence_basis` dict alongside `risk`/
`confidence`/`reasoning`, and run_parallel_analysis carries both under a
top-level `evidence_basis` key so a DEM/USGS failure is distinguishable from
a genuine flat/quiet reading in the persisted hazard_zones row (via
agent.py's write_to_db -> confirmed_by JSONB).

Offline and deterministic: fetch_gdacs/fetch_usgs/fetch_slope and the LLM
call are monkeypatched so no network/LLM calls happen.
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


async def _no_llm(prompt, system="", criticality="normal"):
    return None  # forces every analyze_* deterministic fallback


def _satellite_data():
    return {
        "event_id": "test-event",
        "boundaries": {"bbox": [70.0, 33.0, 71.0, 34.0], "risk_cities": ["TestCity"]},
        "analysis": {
            "affected_area_km2": 0.0,
            "mean_value": 0.0,
            "water_percent": 0.0,
            "index_type": "NDWI",
            "confidence": 0.9,
        },
        "satellite": {"type": "sentinel-2"},
    }


def _run(coro):
    return asyncio.run(coro)


def test_flat_terrain_dem_available_distinguishable_from_failed_dem():
    """A real DEM reading of genuinely flat terrain must carry
    dem_available=True with real elevation/sample data, unlike a failed-DEM
    default which must carry dem_available=False."""
    analyzer.fetch_gdacs = _no_gdacs

    async def _real_flat_slope(bbox):
        return {
            "available": True,
            "slope_estimate": 3.2,
            "elevation_min_m": 480.1,
            "elevation_max_m": 512.4,
            "samples": 25,
            "source": "opentopodata_srtm30m",
        }

    async def _no_usgs(bbox, days=7):
        return {"earthquakes": [], "count": 0, "source": "usgs"}

    analyzer.fetch_usgs = _no_usgs
    analyzer.fetch_slope = _real_flat_slope
    analyzer.smart_llm_call = _no_llm

    result = _run(analyzer.run_parallel_analysis(_satellite_data()))
    landslide_evidence = result.get("evidence_basis", {}).get("landslide")

    if result["landslide_risk"] == "LOW":
        ok("flat terrain (slope 3.2°) -> LOW landslide risk")
    else:
        bad(f"expected LOW landslide risk, got {result['landslide_risk']}")

    if landslide_evidence and landslide_evidence.get("dem_available") is True:
        ok("evidence_basis.landslide.dem_available=True for a real DEM reading")
    else:
        bad(f"dem_available not True for a real DEM reading: {landslide_evidence}")

    if landslide_evidence and landslide_evidence.get("sample_count") == 25:
        ok("evidence_basis.landslide carries the real sample_count (25)")
    else:
        bad(f"sample_count missing/wrong: {landslide_evidence}")

    if landslide_evidence and landslide_evidence.get("dem_source") == "opentopodata_srtm30m":
        ok("evidence_basis.landslide carries the real DEM source")
    else:
        bad(f"dem_source missing/wrong: {landslide_evidence}")


def test_failed_dem_default_distinguishable_from_flat_terrain():
    """A failed DEM lookup (conservative 10.0-degree default) must be
    distinguishable from the real-flat-terrain case above, even though both
    currently produce the same LOW risk verdict."""
    analyzer.fetch_gdacs = _no_gdacs

    async def _failed_dem(bbox):
        return {
            "available": False,
            "slope_estimate": 10.0,
            "source": "no_dem_conservative_default",
        }

    async def _no_usgs(bbox, days=7):
        return {"earthquakes": [], "count": 0, "source": "usgs"}

    analyzer.fetch_usgs = _no_usgs
    analyzer.fetch_slope = _failed_dem
    analyzer.smart_llm_call = _no_llm

    result = _run(analyzer.run_parallel_analysis(_satellite_data()))
    landslide_evidence = result.get("evidence_basis", {}).get("landslide")

    if result["landslide_risk"] == "LOW":
        ok("failed DEM (conservative default) -> also LOW landslide risk "
           "(same verdict as real flat terrain, by design)")
    else:
        bad(f"expected LOW landslide risk, got {result['landslide_risk']}")

    if landslide_evidence and landslide_evidence.get("dem_available") is False:
        ok("evidence_basis.landslide.dem_available=False for a failed DEM lookup")
    else:
        bad(f"dem_available not False for a failed DEM lookup: {landslide_evidence}")

    if landslide_evidence and landslide_evidence.get("dem_source") == "no_dem_conservative_default":
        ok("evidence_basis.landslide.dem_source names the conservative default")
    else:
        bad(f"dem_source missing/wrong: {landslide_evidence}")

    if landslide_evidence and landslide_evidence.get("sample_count") is None:
        ok("evidence_basis.landslide.sample_count is None (no real samples) "
           "for a failed DEM lookup")
    else:
        bad(f"sample_count should be None for a failed DEM lookup: {landslide_evidence}")


def test_genuine_no_seismicity_distinguishable_from_usgs_fetch_failure():
    """A genuine 'USGS queried, zero recent earthquakes' result must be
    distinguishable from a USGS fetch failure that degraded to the same
    {"count": 0} shape."""
    analyzer.fetch_gdacs = _no_gdacs

    async def _flat_slope(bbox):
        return {"available": True, "slope_estimate": 5.0, "source": "test"}

    analyzer.fetch_slope = _flat_slope
    analyzer.smart_llm_call = _no_llm

    async def _genuine_no_quakes(bbox, days=7):
        return {"earthquakes": [], "count": 0, "source": "usgs"}

    analyzer.fetch_usgs = _genuine_no_quakes
    result = _run(analyzer.run_parallel_analysis(_satellite_data()))
    eq_evidence = result.get("evidence_basis", {}).get("earthquake")

    if result["earthquake_risk"] == "LOW":
        ok("genuine no-seismicity -> LOW earthquake risk")
    else:
        bad(f"expected LOW earthquake risk, got {result['earthquake_risk']}")

    if eq_evidence and eq_evidence.get("usgs_fetch_failed") is False:
        ok("evidence_basis.earthquake.usgs_fetch_failed=False for a genuine "
           "no-seismicity result")
    else:
        bad(f"usgs_fetch_failed not False for a genuine result: {eq_evidence}")


def test_usgs_fetch_failure_flagged_distinctly():
    analyzer.fetch_gdacs = _no_gdacs

    async def _flat_slope(bbox):
        return {"available": True, "slope_estimate": 5.0, "source": "test"}

    analyzer.fetch_slope = _flat_slope
    analyzer.smart_llm_call = _no_llm

    async def _usgs_fetch_failed(bbox, days=7):
        return {"earthquakes": [], "count": 0, "source": "usgs", "error": "timeout"}

    analyzer.fetch_usgs = _usgs_fetch_failed
    result = _run(analyzer.run_parallel_analysis(_satellite_data()))
    eq_evidence = result.get("evidence_basis", {}).get("earthquake")

    if result["earthquake_risk"] == "LOW":
        ok("USGS fetch failure -> also LOW earthquake risk (same verdict, "
           "by design)")
    else:
        bad(f"expected LOW earthquake risk, got {result['earthquake_risk']}")

    if eq_evidence and eq_evidence.get("usgs_fetch_failed") is True:
        ok("evidence_basis.earthquake.usgs_fetch_failed=True for a fetch "
           "failure")
    else:
        bad(f"usgs_fetch_failed not True for a fetch failure: {eq_evidence}")

    if eq_evidence and eq_evidence.get("usgs_error") == "timeout":
        ok("evidence_basis.earthquake.usgs_error carries the real error text")
    else:
        bad(f"usgs_error missing/wrong: {eq_evidence}")


def test_evidence_basis_survives_into_db_write_confirmed_by():
    """agent.write_to_db's confirmed_by JSONB must carry evidence_basis per
    hazard_type row, not just confidence_scores.

    NOTE (TESTING_GAP_AUDIT.md, 2026-07-28): this test previously imported
    `agent as hazard_agent` but never called it — it defined a local
    `_confirmed_by` closure that hand-copied write_to_db's inner closure
    logic and tested the COPY, not the real function. If the real
    write_to_db's _confirmed_by ever drifted from this hand-copied twin,
    this test would keep passing while the DB row silently stopped carrying
    evidence_basis (the exact CHANGE 6 defect class). The real end-to-end
    check — calling agent.analyze_hazard for real with a faked asyncpg that
    records the REAL confirmed_by JSONB write_to_db sends — now lives in
    agents/hazard/test_field_survival.py's
    test_evidence_basis_survives_real_write_to_db. This test is retired in
    favour of that one rather than deleted outright, so the retirement
    reason stays discoverable in this suite's own history."""
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import test_field_survival as real_test

    real_test.PASS.clear()
    real_test.FAIL.clear()
    real_test.test_evidence_basis_survives_real_write_to_db()

    if real_test.FAIL:
        bad(f"real evidence_basis survival check failed: {real_test.FAIL}")
    else:
        for msg in real_test.PASS:
            ok(f"[via test_field_survival.py] {msg}")


if __name__ == "__main__":
    print("=" * 60)
    print("TEST: landslide/earthquake evidence provenance")
    print("=" * 60)
    test_flat_terrain_dem_available_distinguishable_from_failed_dem()
    test_failed_dem_default_distinguishable_from_flat_terrain()
    test_genuine_no_seismicity_distinguishable_from_usgs_fetch_failure()
    test_usgs_fetch_failure_flagged_distinctly()
    test_evidence_basis_survives_into_db_write_confirmed_by()
    print("=" * 60)
    print(f"SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 60)
    if FAIL:
        sys.exit(1)
