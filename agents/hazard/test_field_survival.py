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
    """Fake asyncpg connection: records every INSERT's confirmed_by AND
    diagnostics JSONB arguments, keyed by hazard_type, instead of hitting a
    real DB."""

    def __init__(self, sink, diagnostics_sink=None):
        self.sink = sink
        self.diagnostics_sink = diagnostics_sink if diagnostics_sink is not None else {}

    async def execute(self, sql, *args):
        if "INSERT INTO hazard_zones" in sql:
            # Positional args per agent.py's write_to_db INSERT
            # (feat/durable-evidence-trail): event_id, risk_level,
            # hazard_type, severity, confirmed_by, flood_depth_estimate,
            # earthquake_mmi, landslide_probability, overall_confidence,
            # diagnostics, created_at
            hazard_type = args[2]
            confirmed_by_json = args[4]
            diagnostics_json = args[9]
            self.sink[hazard_type] = json.loads(confirmed_by_json)
            self.diagnostics_sink[hazard_type] = json.loads(diagnostics_json)

    async def close(self):
        pass


def _install_fake_asyncpg(sink, diagnostics_sink=None):
    fake = types.ModuleType("asyncpg")

    async def _connect(url):
        return _RecordingConn(sink, diagnostics_sink)

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


def test_primary_hazard_risk_reaches_payload_and_diagnostics():
    """primary_hazard_risk (H#10) must reach analyze_hazard's returned payload
    (PipelineState-bound). UPDATED (feat/durable-evidence-trail): the ordering
    bug (write_to_db(raw_result) ran BEFORE primary_hazard_risk was even
    computed in agent.py) is now FIXED -- agent.py computes it first and
    threads it into write_to_db's new `diagnostics` param, so it now reaches
    the real hazard_zones.diagnostics column too. It still correctly does
    NOT appear in confirmed_by (that JSONB's shape is unchanged -- only
    confidence_scores + evidence_basis), which this test also confirms, so a
    future refactor that accidentally duplicates it into confirmed_by is a
    visible, deliberate change."""
    analyzer.fetch_gdacs = _no_gdacs
    analyzer.fetch_usgs = _no_usgs

    async def _flat_slope(bbox):
        return {"available": True, "slope_estimate": 5.0, "source": "test"}

    analyzer.fetch_slope = _flat_slope
    analyzer.smart_llm_call = _no_llm

    confirmed_by_sink = {}
    diagnostics_sink = {}
    _install_fake_asyncpg(confirmed_by_sink, diagnostics_sink)
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

    # ORDERING BUG FIXED (feat/durable-evidence-trail): primary_hazard_risk
    # now reaches the real hazard_zones.diagnostics column on every row,
    # confirmed via the real write_to_db() call (not source inspection).
    expected = result["hazard"]["primary_hazard_risk"]
    all_rows_ok = all(
        diagnostics_sink.get(ht, {}).get("primary_hazard_risk") == expected
        for ht in ("flood", "earthquake", "landslide")
    )
    if all_rows_ok:
        ok(f"primary_hazard_risk={expected!r} now survives into the real "
           f"hazard_zones.diagnostics column on every row (ordering bug "
           f"fixed: agent.py now computes it before calling write_to_db)")
    else:
        bad(f"primary_hazard_risk missing/wrong in diagnostics: {diagnostics_sink}")

    # confirmed_by's shape is unchanged by this fix -- primary_hazard_risk
    # must still NOT appear there (it belongs in diagnostics, not
    # confirmed_by, which stays confidence_scores + evidence_basis only).
    leaked = [
        ht for ht, row in confirmed_by_sink.items()
        if "primary_hazard_risk" in json.dumps(row)
    ]
    if not leaked:
        ok("primary_hazard_risk correctly still absent from confirmed_by "
           "(it lives in the new diagnostics column instead, confirmed_by's "
           "shape is unchanged by this fix)")
    else:
        bad(f"primary_hazard_risk unexpectedly present in confirmed_by: {leaked}")


def test_confidence_cap_applied_reaches_diagnostics_not_confirmed_by():
    """confidence_cap_applied (hazard's satellite-confidence-cap fix) is
    real and tested at the analyzer level (test_confidence_cap.py). UPDATED
    (feat/durable-evidence-trail): it now reaches the real
    hazard_zones.diagnostics column (via write_to_db's new diagnostics
    param) but still does NOT cross into analyze_hazard's own returned
    payload dict, and still correctly does not appear in confirmed_by
    (whose shape is unchanged). This is the exact defect class
    TESTING_GAP_AUDIT.md is about: a field computed correctly needs an
    explicit, asserted path to persistence, not an assumption that it
    arrives somewhere just because it's real."""
    analyzer.fetch_gdacs = _no_gdacs
    analyzer.fetch_usgs = _no_usgs

    async def _flat_slope(bbox):
        return {"available": True, "slope_estimate": 5.0, "source": "test"}

    analyzer.fetch_slope = _flat_slope
    analyzer.smart_llm_call = _no_llm

    payload = _satellite_payload()
    payload["confidence"] = 0.0  # forces the flood confidence cap to engage

    confirmed_by_sink = {}
    diagnostics_sink = {}
    _install_fake_asyncpg(confirmed_by_sink, diagnostics_sink)
    agent = _reload_agent()

    result = _run(agent.analyze_hazard(payload, "evt-hazard-survival-3", disaster_type="flood"))

    if result.get("status") != "complete":
        bad(f"analyze_hazard did not complete: {result}")
        return

    if "confidence_cap_applied" not in (result.get("hazard") or {}):
        ok("confidence_cap_applied still absent from analyze_hazard's own "
           "payload dict (unchanged by this fix -- the payload dict's shape "
           "was not touched)")
    else:
        bad("confidence_cap_applied unexpectedly present in payload -- "
            "update this test to also assert PipelineState survival")

    # NEW: confidence_cap_applied now DOES reach the real diagnostics column
    # (confirmed via a real write_to_db() call, not source inspection).
    all_rows_have_cap_flag = all(
        diagnostics_sink.get(ht, {}).get("confidence_cap_applied") is True
        for ht in ("flood", "earthquake", "landslide")
    )
    if all_rows_have_cap_flag:
        ok("confidence_cap_applied=True now survives into the real "
           "hazard_zones.diagnostics column on every row")
    else:
        bad(f"confidence_cap_applied missing/wrong in diagnostics: {diagnostics_sink}")

    leaked = [
        ht for ht, row in confirmed_by_sink.items()
        if "confidence_cap_applied" in json.dumps(row)
    ]
    if not leaked:
        ok("confidence_cap_applied correctly still absent from confirmed_by "
           "(lives in diagnostics instead; confirmed_by's shape is unchanged)")
    else:
        bad(f"confidence_cap_applied unexpectedly present in confirmed_by: {leaked}")


def test_raw_evidence_trace_reaches_diagnostics():
    """The raw third-party evidence trace (dem_query/dem_samples/
    slope_computation for landslide; usgs_query/events_returned/
    max_magnitude/magnitude_type for earthquake; the flood fallback's
    branch/index/threshold detail) must be re-derivable from the DB row
    alone -- verified here via a real analyze_hazard() -> write_to_db()
    call, not by re-reading analyzer.py's source."""
    analyzer.fetch_gdacs = _no_gdacs

    async def _usgs_with_event(bbox, days=7):
        return {
            "earthquakes": [
                {
                    "id": "us70008dx6",
                    "properties": {"mag": 6.8, "magType": "mww", "time": 1700000000000},
                    "geometry": {"type": "Point", "coordinates": [70.5, 33.5, 10.0]},
                }
            ],
            "count": 1,
            "source": "usgs",
            "query": {"url": "https://usgs.example/query", "http_status": 200, "latency_ms": 42.0},
        }

    analyzer.fetch_usgs = _usgs_with_event

    async def _real_flat_slope(bbox):
        return {
            "available": True,
            "slope_estimate": 3.2,
            "elevation_min_m": 480.1,
            "elevation_max_m": 512.4,
            "samples": 25,
            "source": "opentopodata_srtm30m",
            "dem_query": {"endpoint": "https://api.opentopodata.org/v1/srtm30m", "http_status": 200},
            "dem_samples": [{"lat": 33.0, "lon": 70.0, "elevation_m": 490.0}] * 25,
            "dem_response_raw": {"requested_count": 25, "returned_count": 25, "all_points_returned": True},
            "slope_computation": {"statistic": "mean", "resulting_value_deg": 3.2},
            "fallback_used": False,
        }

    analyzer.fetch_slope = _real_flat_slope
    analyzer.smart_llm_call = _no_llm

    confirmed_by_sink = {}
    diagnostics_sink = {}
    _install_fake_asyncpg(confirmed_by_sink, diagnostics_sink)
    agent = _reload_agent()

    result = _run(agent.analyze_hazard(_satellite_payload(), "evt-hazard-raw-trace", disaster_type="earthquake"))
    if result.get("status") != "complete":
        bad(f"analyze_hazard did not complete: {result}")
        return
    ok("analyze_hazard completed via the real pipeline entry point (raw evidence trace test)")

    eq_raw = (diagnostics_sink.get("earthquake") or {}).get("raw") or {}
    if eq_raw.get("max_magnitude_driving_event_id") == "us70008dx6" and eq_raw.get("magnitude_type") == "mww":
        ok(f"earthquake diagnostics.raw carries max_magnitude_driving_event_id "
           f"and magnitude_type from the real USGS fetch: {eq_raw.get('max_magnitude_driving_event_id')!r}/{eq_raw.get('magnitude_type')!r}")
    else:
        bad(f"earthquake raw evidence trace missing/wrong: {eq_raw}")

    if eq_raw.get("usgs_query", {}).get("http_status") == 200:
        ok("earthquake diagnostics.raw carries the real usgs_query http_status")
    else:
        bad(f"usgs_query missing from earthquake diagnostics.raw: {eq_raw}")

    ls_raw = (diagnostics_sink.get("landslide") or {}).get("raw") or {}
    if ls_raw.get("slope_computation", {}).get("statistic") == "mean" and ls_raw.get("dem_response_raw", {}).get("all_points_returned") is True:
        ok(f"landslide diagnostics.raw carries slope_computation.statistic and "
           f"dem_response_raw.all_points_returned from the real DEM fetch")
    else:
        bad(f"landslide raw evidence trace missing/wrong: {ls_raw}")

    if len(ls_raw.get("dem_samples") or []) == 25:
        ok("landslide diagnostics.raw carries all 25 DEM grid samples (small enough to store whole)")
    else:
        bad(f"dem_samples count wrong: {len(ls_raw.get('dem_samples') or [])}")


if __name__ == "__main__":
    print("=" * 70)
    print("TEST: hazard field survival (islamabad-findings audit follow-up)")
    print("=" * 70)
    test_evidence_basis_survives_real_write_to_db()
    test_primary_hazard_risk_reaches_payload_and_diagnostics()
    test_confidence_cap_applied_reaches_diagnostics_not_confirmed_by()
    test_raw_evidence_trace_reaches_diagnostics()
    print("=" * 70)
    print(f"SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 70)
    if FAIL:
        sys.exit(1)
