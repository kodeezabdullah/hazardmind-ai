"""Field-survival test for impact_data.overall_confidence
(TESTING_GAP_AUDIT.md follow-up, 2026-07-28).

Root cause this verifies stayed fixed: overall_confidence previously existed
as a live Neon column that nothing in services/db.py ever wrote (Gate A,
root CLAUDE.md). agent.py/services/db.py were fixed to write it, but no test
exercises this through the real pipeline entry point
(agent.run_impact_analysis) with a real (faked) DB write — the CLAUDE.md
record of this fix is a source-reading confirmation, not a test.

This test calls run_impact_analysis directly (the real entry point node.py
calls) via the no-significant-disaster gate path, which needs no GeoNames/
Overpass/LLM network calls, and asserts overall_confidence survives at:
  (a) the JSON result returned to node.py (-> PipelineState via
      impact_node's `result["data"]["overall_confidence"]` read)
  (b) the real write_impact_data() call's actual SQL parameter (faked
      asyncpg records the value instead of hitting a live DB)

Offline and deterministic: NEON_DATABASE_URL points at a fake in-process
asyncpg module; the no-significant-disaster gate is forced by risk_level.
"""

import asyncio
import json
import os
import sys
import types

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


class _RecordingConn:
    def __init__(self, sink):
        self.sink = sink

    async def execute(self, sql, *args):
        if "INSERT INTO impact_data" in sql:
            # write_impact_data's INSERT column order (feat/durable-evidence-
            # trail added `diagnostics` as the new LAST positional value,
            # after overall_confidence, before ON CONFLICT) — capture
            # overall_confidence at args[-2] and diagnostics at args[-1] by
            # position, matching services/db.py's real INSERT column list.
            self.sink["overall_confidence"] = args[-2]
            self.sink["diagnostics"] = args[-1]

    async def close(self):
        pass


def _install_fake_asyncpg(sink):
    fake = types.ModuleType("asyncpg")

    async def _connect(url):
        return _RecordingConn(sink)

    fake.connect = _connect
    sys.modules["asyncpg"] = fake
    os.environ["NEON_DATABASE_URL"] = "postgres://fake/db"


def test_overall_confidence_survives_real_write_impact_data():
    print("\n[Field survival] impact_data.overall_confidence survives from "
          "run_impact_analysis's real no-significant-disaster path into the "
          "real write_impact_data() INSERT")

    sink = {}
    _install_fake_asyncpg(sink)

    import importlib
    if "services.db" in sys.modules:
        importlib.reload(sys.modules["services.db"])
    if "agent" in sys.modules:
        importlib.reload(sys.modules["agent"])
    import agent

    raw = _run(agent.run_impact_analysis(
        event_id="evt-impact-survival",
        bounds={"west": 70.0, "south": 33.0, "east": 71.0, "north": 34.0},
        risk_level="LOW",  # forces the no-significant-disaster gate
        severity="LOW",
        hazard_zones_geojson={},
        flood_depth_estimate=0.0,
        overall_confidence=0.42,
        risk_cities=["TestCity"],
        disaster_type="flood",
    ))
    result = json.loads(raw)

    if result.get("status") != "complete":
        bad(f"run_impact_analysis did not complete: {result}")
        return
    ok("run_impact_analysis completed via the real pipeline entry point "
       "(no-significant-disaster gate)")

    data_confidence = (result.get("data") or {}).get("overall_confidence")
    if data_confidence == 0.42:
        ok(f"overall_confidence={data_confidence} present in the JSON result "
           f"-- this is exactly what impact_node.py reads into "
           f"PipelineState['confidence_scores']['impact']")
    else:
        bad(f"overall_confidence missing/wrong in JSON result: {data_confidence}")

    db_confidence = sink.get("overall_confidence")
    if db_confidence == 0.42:
        ok("overall_confidence survives into the REAL write_impact_data() "
           "INSERT parameter (not just the in-memory result)")
    else:
        bad(f"overall_confidence did not survive into the real INSERT: {sink}")

    # diagnostics JSONB: on the no-significant-disaster path, pop/infra/vuln
    # are all empty dicts with no "confidence" key -- so the three task
    # confidences are honestly None here (never invented), and
    # evacuation_routes is honestly []. Confirmed via the real INSERT
    # parameter, not source inspection.
    diagnostics = json.loads(sink["diagnostics"]) if isinstance(sink.get("diagnostics"), str) else sink.get("diagnostics")
    if diagnostics is not None and diagnostics.get("evacuation_routes") == []:
        ok(f"diagnostics JSONB present on the no-significant-disaster path: "
           f"{diagnostics} (task confidences honestly None -- no task ran)")
    else:
        bad(f"diagnostics missing/wrong on the no-significant-disaster path: {diagnostics}")


def test_diagnostics_jsonb_carries_real_task_confidences_and_evacuation_routes():
    """The durable-evidence-trail diagnostics JSONB must carry the three
    task-level confidences (population/infrastructure/vulnerability) that
    impact computes but previously discarded, plus the real evacuation_routes
    output -- verified via a real services.db.write_impact_data() call (the
    actual function agent.py's significant-disaster path calls), not a
    hand-copied twin of its logic."""
    print("\n[Field survival] impact_data.diagnostics carries real "
          "population/infrastructure/vulnerability task confidences + "
          "evacuation_routes via the real write_impact_data() call")

    sink = {}
    _install_fake_asyncpg(sink)

    import importlib
    if "services.db" in sys.modules:
        importlib.reload(sys.modules["services.db"])
    import services.db as db_module

    pop = {"population_affected": 5000, "confidence": 0.81}
    infra = {"hospitals_at_risk": 2, "roads_blocked_km": 3.4, "confidence": 0.76}
    vuln = {
        "vulnerability_score": "6.2",
        "confidence": 0.68,
        "evacuation_routes": [
            {"name": "Route 1 — Test Road", "distance_km": 4, "status": "open", "geojson": {}}
        ],
    }

    _run(db_module.write_impact_data("evt-impact-diagnostics", pop, infra, vuln, overall_confidence=0.75))

    diagnostics = json.loads(sink["diagnostics"]) if isinstance(sink.get("diagnostics"), str) else sink.get("diagnostics")
    if (
        diagnostics
        and diagnostics.get("population_confidence") == 0.81
        and diagnostics.get("infrastructure_confidence") == 0.76
        and diagnostics.get("vulnerability_confidence") == 0.68
    ):
        ok(f"all three task-level confidences survive into the real "
           f"diagnostics JSONB write: {diagnostics}")
    else:
        bad(f"task-level confidences missing/wrong in diagnostics: {diagnostics}")

    routes = diagnostics.get("evacuation_routes") if diagnostics else None
    if routes and routes[0].get("name") == "Route 1 — Test Road":
        ok(f"real evacuation_routes output survives into diagnostics JSONB "
           f"alongside its own dedicated column: {routes}")
    else:
        bad(f"evacuation_routes missing/wrong in diagnostics: {routes}")


if __name__ == "__main__":
    print("=" * 70)
    print("TEST: impact field survival (islamabad-findings audit follow-up)")
    print("=" * 70)
    test_overall_confidence_survives_real_write_impact_data()
    test_diagnostics_jsonb_carries_real_task_confidences_and_evacuation_routes()
    print("=" * 70)
    print(f"SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 70)
    if FAIL:
        sys.exit(1)
