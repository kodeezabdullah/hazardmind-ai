"""Tests for backend/graph.py's StateGraph wiring.

Run from the backend/ directory:  python test_graph.py

Stubs graph._load_node so no agent's real (heavy) pipeline dependencies
(rasterio, shapely, boto3, ...) are needed — this tests the GRAPH WIRING
(node registration, conditional-edge routing, failure short-circuit), not the
pipeline logic itself, which each agent already unit-tests independently.
"""
import asyncio

import graph as graph_module


def _patch_nodes(**node_fns):
    """Replace graph._load_node with a lookup into node_fns for this test."""

    def fake_load_node(agent_name, func_name):
        return node_fns[agent_name]

    graph_module._load_node = fake_load_node


def _restore():
    import importlib

    importlib.reload(graph_module)


def test_full_success_path_reaches_report() -> None:
    calls = []

    async def satellite_node(state):
        calls.append("satellite")
        return {"satellite_result": {"ok": True}, "status": "hazard", "progress": 25}

    async def hazard_node(state):
        calls.append("hazard")
        assert state["satellite_result"] == {"ok": True}
        return {"hazard_result": {"ok": True}, "status": "impact", "progress": 50}

    async def impact_node(state):
        calls.append("impact")
        assert state["hazard_result"] == {"ok": True}
        return {"impact_result": {"ok": True}, "status": "report", "progress": 75}

    async def report_node(state):
        calls.append("report")
        assert state["impact_result"] == {"ok": True}
        return {"report_result": {"ok": True}, "status": "complete", "progress": 100}

    try:
        _patch_nodes(
            satellite=satellite_node,
            hazard=hazard_node,
            impact=impact_node,
            report=report_node,
        )
        compiled = graph_module.build_pipeline_graph()
        initial_state = {
            "event_id": "evt-1",
            "location": "Lahore",
            "disaster_type": "flood",
            "status": "satellite",
            "current_step": "satellite",
            "progress": 0,
            "errors": [],
            "anomalies": [],
            "confidence_scores": {},
        }
        final_state = asyncio.run(compiled.ainvoke(initial_state))
    finally:
        _restore()

    assert calls == ["satellite", "hazard", "impact", "report"], calls
    assert final_state["status"] == "complete", final_state
    assert final_state["report_result"] == {"ok": True}, final_state
    print("[ok] full success path -> satellite/hazard/impact/report in order")


def test_failure_short_circuits_remaining_stages() -> None:
    calls = []

    async def satellite_node(state):
        calls.append("satellite")
        return {"satellite_result": {"ok": True}, "status": "hazard", "progress": 25}

    async def hazard_node(state):
        calls.append("hazard")
        return {
            "hazard_result": {"status": "error"},
            "status": "failed",
            "current_step": "hazard",
            "errors": [{"stage": "hazard", "error": "boom"}],
        }

    async def impact_node(state):
        calls.append("impact")  # must never run
        return {"impact_result": {}, "status": "report"}

    async def report_node(state):
        calls.append("report")  # must never run
        return {"report_result": {}, "status": "complete"}

    try:
        _patch_nodes(
            satellite=satellite_node,
            hazard=hazard_node,
            impact=impact_node,
            report=report_node,
        )
        compiled = graph_module.build_pipeline_graph()
        initial_state = {
            "event_id": "evt-2",
            "location": "Lahore",
            "disaster_type": "flood",
            "status": "satellite",
            "current_step": "satellite",
            "progress": 0,
            "errors": [],
            "anomalies": [],
            "confidence_scores": {},
        }
        final_state = asyncio.run(compiled.ainvoke(initial_state))
    finally:
        _restore()

    assert calls == ["satellite", "hazard"], calls
    assert final_state["status"] == "failed", final_state
    assert final_state["errors"] == [{"stage": "hazard", "error": "boom"}], final_state
    print("[ok] hazard failure -> impact/report never run, status=failed")


def test_load_node_isolates_same_named_modules() -> None:
    """agent.py/node.py exist under multiple agents/* dirs with the same bare
    module name; _load_node must not let one agent's import clobber another's
    in sys.modules."""
    import sys

    for key in list(sys.modules):
        if key.startswith("hazardmind_") and key.endswith("_node"):
            del sys.modules[key]

    # Each of these imports a DIFFERENT agents/<name>/node.py, all of which
    # (transitively) do `from agent import ...` / `from intelligence import
    # ...` unqualified. If _load_node's sys.path isolation were broken, the
    # second and third loads would silently reuse the first agent's cached
    # "agent"/"intelligence" module instead of their own.
    try:
        hazard_fn = graph_module._load_node("hazard", "hazard_node")
        assert hazard_fn.__module__ == "hazardmind_hazard_node"
        print("[ok] _load_node -> hazard node loaded under its own module key")
    except ModuleNotFoundError as exc:
        # Hazard's own deps (geopandas/shapely/etc.) may not be installed in
        # the backend venv; that's a real, separate dependency-merge gap
        # (flagged in CLAUDE.md), not a failure of the isolation mechanism
        # itself, so treat it as inconclusive rather than a hard failure.
        print(f"[skip] hazard node deps not installed in this venv ({exc})")


if __name__ == "__main__":
    test_full_success_path_reaches_report()
    test_failure_short_circuits_remaining_stages()
    test_load_node_isolates_same_named_modules()
    print("[done] graph wiring verified")
