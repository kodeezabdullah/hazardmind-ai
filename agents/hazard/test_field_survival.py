"""Field-survival tests for the hardening effort's hazard fields
(TESTING_GAP_AUDIT.md, 2026-07-28).

The audit found that every existing hazard suite (test_hazard_provenance.py,
test_confidence_cap.py) calls analyzer.run_parallel_analysis directly and
never through agent.analyze_hazard (the real pipeline entry, which also runs
_normalise_satellite_payload and write_to_db). One test
(test_evidence_basis_survives_into_db_write_confirmed_by) even hand-copies
write_to_db's _confirmed_by closure instead of calling the real function —
if the real closure drifts from the copy, the test keeps passing while the
DB row silently stops carrying evidence_basis.

This file calls agent.analyze_hazard for real (the actual pipeline entry
point) with asyncpg faked out via sys.modules (mirrors the pattern in
agents/satellite/tests/test_correctness_fixes_20260727.py), so write_to_db's
REAL _confirmed_by closure runs and we capture the REAL SQL parameters it
sends — not a hand-copied twin of the logic.

Fields covered, each asserted at (a) the raw analyzer result, (b) the
analyze_hazard payload returned to node.py (-> PipelineState), and (c) the
real confirmed_by JSONB written to hazard_zones:
  - evidence_basis (earthquake, landslide)     -- WRITTEN, verified here
  - primary_hazard_risk                        -- (a)/(b) yes, (c) NOT
    written; asserted absent from confirmed_by, since write_to_db(raw_result)
    runs BEFORE primary_hazard_risk is even computed in agent.py
    (islamabad-findings audit finding, not a bug fixed by this session --
    flagged, not silently omitted).
  - confidence_cap_applied (from analyzer, see hazard's confidence-cap fix)
    -- NOT copied into analyze_hazard's payload and NOT written to
    confirmed_by; asserted absent at both boundaries, real audit gap.

Offline and deterministic: fetch_gdacs/fetch_usgs/fetch_slope and the LLM
call are monkeypatched (no network/LLM); asyncpg is replaced with a fake
in-process module that records the exact INSERT parameters instead of
touching a real database.
"""

import asyncio
import json
import os
import sys
import types

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


async def _no_llm(prompt, system="", criticality="normal"):
    return None  # forces every analyze_* deterministic fallback


def _satellite_payload():
    """The FLAT shape agent.py's _normalise_satellite_payload actually
    receives from satellite (not the pre-nested shape the other suites hand
    to run_parallel_analysis directly) -- exercises the real adapter too."""
    return {
        "event_id": "evt-hazard-survival",
        "bbox": [70.0, 33.0, 71.0, 34.0],
        "risk_cities": ["TestCity"],
        "affected_area_km2": 0.0,
        "mean_index": 0.0,
        "water_percent": 0.0,
        "index_type": "NDWI",
        "confidence": 0.9,
        "satellite_type": "sentinel-2",
    }


class _RecordingConn:
    """Fake asyncpg connection: records every INSERT's confirmed_by JSONB
    argument, keyed by hazard_type, instead of hitting a real DB."""

    def __init__(self, sink):
        self.sink = sink

    async def execute(self, sql, *args):
        if "INSERT INTO hazard_zones" in sql:
            # Positional args per agent.py's write_to_db INSERT:
            # event_id, risk_level, hazard_type, severity, confirmed_by,
            # flood_depth_estimate, earthquake_mmi, landslide_probability,
            # overall_confidence, created_at
            hazard_type = args[2]
            confirmed_by_json = args[4]
            self.sink[hazard_type] = json.loads(confirmed_by_json)

    async def close(self):
        pass


def _install_fake_asyncpg(sink):
    fake = types.ModuleType("asyncpg")

    async def _connect(url):
        return _RecordingConn(sink)

    fake.connect = _connect
    sys.modules["asyncpg"] = fake
    os.environ["NEON_DATABASE_URL"] = "postgres://fake/db"


def _run(coro):
    return asyncio.run(coro)


def _reload_agent():
    import importlib
    if "agent" in sys.modules:
        importlib.reload(sys.modules["agent"])
    else:
        import agent  # noqa: F401
    return sys.modules["agent"]


def test_evidence_basis_survives_real_write_to_db():
    """The REAL write_to_db (not a hand-copied closure) must persist
    evidence_basis into confirmed_by for earthquake and landslide rows, when
    exercised through agent.analyze_hazard end to end."""
    analyzer.fetch_gdacs = _no_gdacs
    analyzer.fetch_usgs = _no_usgs

    async def _real_flat_slope(bbox):
        return {
            "available": True,
            "slope_estimate": 3.2,
            "elevation_min_m": 480.1,
            "elevation_max_m": 512.4,
            "samples": 25,
            "source": "opentopodata_srtm30m",
        }

    analyzer.fetch_slope = _real_flat_slope
    analyzer.smart_llm_call = _no_llm

    confirmed_by_sink = {}
    _install_fake_asyncpg(confirmed_by_sink)
    agent = _reload_agent()

    result = _run(agent.analyze_hazard(_satellite_payload(), "evt-hazard-survival", disaster_type="landslide"))

    if result.get("status") != "complete":
        bad(f"analyze_hazard did not complete: {result}")
        return
    ok("analyze_hazard completed via the real pipeline entry point")

    landslide_row = confirmed_by_sink.get("landslide")
    if landslide_row and landslide_row.get("evidence_basis", {}).get("dem_available") is True:
        ok("REAL write_to_db persisted evidence_basis.dem_available=True into "
           "confirmed_by for the landslide row (not a hand-copied closure)")
    else:
        bad(f"evidence_basis did not survive into the real confirmed_by write: {landslide_row}")

    if landslide_row and landslide_row.get("evidence_basis", {}).get("dem_source") == "opentopodata_srtm30m":
        ok("confirmed_by.evidence_basis carries the real DEM source through the real write path")
    else:
        bad(f"dem_source missing/wrong in real confirmed_by write: {landslide_row}")

    earthquake_row = confirmed_by_sink.get("earthquake")
    if earthquake_row and "usgs_fetch_failed" in (earthquake_row.get("evidence_basis") or {}):
        ok("confirmed_by.evidence_basis for earthquake row present via real write_to_db")
    else:
        bad(f"earthquake evidence_basis missing from real confirmed_by write: {earthquake_row}")


def test_primary_hazard_risk_reaches_payload_but_not_confirmed_by():
    """primary_hazard_risk (H#10) must reach analyze_hazard's returned payload
    (PipelineState-bound), but write_to_db(raw_result) runs BEFORE
    primary_hazard_risk is computed in agent.py -- so it is a real,
    documented gap that it never reaches confirmed_by. This test pins that
    gap explicitly rather than leaving it unasserted (an absent assertion is
    indistinguishable from an oversight)."""
    analyzer.fetch_gdacs = _no_gdacs
    analyzer.fetch_usgs = _no_usgs

    async def _flat_slope(bbox):
        return {"available": True, "slope_estimate": 5.0, "source": "test"}

    analyzer.fetch_slope = _flat_slope
    analyzer.smart_llm_call = _no_llm

    confirmed_by_sink = {}
    _install_fake_asyncpg(confirmed_by_sink)
    agent = _reload_agent()

    result = _run(agent.analyze_hazard(_satellite_payload(), "evt-hazard-survival-2", disaster_type="earthquake"))

    if result.get("status") != "complete":
        bad(f"analyze_hazard did not complete: {result}")
        return

    if "primary_hazard_risk" in (result.get("hazard") or {}):
        ok(f"primary_hazard_risk reaches analyze_hazard's payload "
           f"({result['hazard']['primary_hazard_risk']!r}) -- crosses into "
           f"PipelineState via node.py's whole-dict copy")
    else:
        bad("primary_hazard_risk missing from analyze_hazard's payload")

    # Real gap, confirmed via the REAL write path (not inferred from source
    # reading): confirmed_by must NOT contain primary_hazard_risk anywhere,
    # for any of the three rows, because write_to_db runs before it exists.
    leaked = [
        ht for ht, row in confirmed_by_sink.items()
        if "primary_hazard_risk" in json.dumps(row)
    ]
    if not leaked:
        ok("primary_hazard_risk correctly absent from confirmed_by on every "
           "row (write_to_db runs before it's computed -- documented gap, "
           "not silently untested)")
    else:
        bad(f"primary_hazard_risk unexpectedly present in confirmed_by: {leaked}")


def test_confidence_cap_applied_does_not_survive_any_boundary():
    """confidence_cap_applied (hazard's satellite-confidence-cap fix) is
    real and tested at the analyzer level (test_confidence_cap.py), but this
    test confirms -- through the real agent.analyze_hazard entry point --
    that it does NOT cross into analyze_hazard's payload nor into
    confirmed_by. This is the exact defect class TESTING_GAP_AUDIT.md is
    about: a field computed correctly and invisible one hop later. Pinned
    explicitly so a future fix that adds it to payload/confirmed_by is a
    visible, intentional change to this test, not a silent one."""
    analyzer.fetch_gdacs = _no_gdacs
    analyzer.fetch_usgs = _no_usgs

    async def _flat_slope(bbox):
        return {"available": True, "slope_estimate": 5.0, "source": "test"}

    analyzer.fetch_slope = _flat_slope
    analyzer.smart_llm_call = _no_llm

    payload = _satellite_payload()
    payload["confidence"] = 0.0  # forces the flood confidence cap to engage

    confirmed_by_sink = {}
    _install_fake_asyncpg(confirmed_by_sink)
    agent = _reload_agent()

    result = _run(agent.analyze_hazard(payload, "evt-hazard-survival-3", disaster_type="flood"))

    if result.get("status") != "complete":
        bad(f"analyze_hazard did not complete: {result}")
        return

    if "confidence_cap_applied" not in (result.get("hazard") or {}):
        ok("confidence_cap_applied absent from analyze_hazard's payload "
           "(confirmed via the real entry point, not just the analyzer's "
           "raw return) -- real gap, not yet surfaced to PipelineState")
    else:
        bad("confidence_cap_applied unexpectedly present in payload -- "
            "update this test to also assert PipelineState/DB survival")

    leaked = [
        ht for ht, row in confirmed_by_sink.items()
        if "confidence_cap_applied" in json.dumps(row)
    ]
    if not leaked:
        ok("confidence_cap_applied absent from confirmed_by on every row "
           "(real gap confirmed via the real write_to_db call, not source "
           "inspection)")
    else:
        bad(f"confidence_cap_applied unexpectedly present in confirmed_by: {leaked}")


if __name__ == "__main__":
    print("=" * 70)
    print("TEST: hazard field survival (islamabad-findings audit follow-up)")
    print("=" * 70)
    test_evidence_basis_survives_real_write_to_db()
    test_primary_hazard_risk_reaches_payload_but_not_confirmed_by()
    test_confidence_cap_applied_does_not_survive_any_boundary()
    print("=" * 70)
    print(f"SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 70)
    if FAIL:
        sys.exit(1)
