"""Tests for OrchestratorAgent.start_pipeline().

Run from the backend/ directory:  python test_orchestrator.py

Stubs db.update_event_status / db.update_pipeline_log and the compiled graph's
ainvoke so no live Neon connection or real agent pipeline is needed. Replaces
the deleted Band-era test_orchestrator.py.
"""
import asyncio
import uuid

import orchestrator as orch_module


def _patch(module, **attrs):
    for name, value in attrs.items():
        setattr(module, name, value)


def test_start_pipeline_success_marks_complete() -> None:
    updates = []
    logs = []

    async def fake_update_status(event_id, status, step):
        updates.append((status, step))

    async def fake_update_log(event_id, log):
        logs.append(log)

    class FakeGraph:
        async def ainvoke(self, state):
            return {
                **state,
                "status": "complete",
                "satellite_result": {"ok": True},
                "hazard_result": {"ok": True},
                "impact_result": {"ok": True},
                "report_result": {"ok": True},
                "errors": [],
                "anomalies": [],
                "confidence_scores": {"satellite": 0.9},
            }

    _patch(
        orch_module,
        update_event_status=fake_update_status,
        update_pipeline_log=fake_update_log,
    )

    agent = orch_module.OrchestratorAgent()
    agent._graph = FakeGraph()
    event_id = str(uuid.uuid4())
    data = {"location": "Lahore", "disaster_type": "flood", "magnitude": 6.1}

    asyncio.run(agent.start_pipeline(event_id, data))

    assert updates == [("processing", "satellite"), ("complete", "complete")], updates
    assert logs == [
        {"errors": [], "anomalies": [], "confidence_scores": {"satellite": 0.9}}
    ], logs
    print("[ok] start_pipeline success -> processing/satellite then complete/complete")


def test_start_pipeline_node_failure_marks_failed() -> None:
    updates = []

    async def fake_update_status(event_id, status, step):
        updates.append((status, step))

    async def fake_update_log(event_id, log):
        pass

    class FakeGraph:
        async def ainvoke(self, state):
            return {
                **state,
                "status": "failed",
                "errors": [{"stage": "hazard", "error": "boom"}],
            }

    _patch(
        orch_module,
        update_event_status=fake_update_status,
        update_pipeline_log=fake_update_log,
    )

    agent = orch_module.OrchestratorAgent()
    agent._graph = FakeGraph()
    event_id = str(uuid.uuid4())
    data = {"location": "Karachi", "disaster_type": "earthquake", "magnitude": 5.4}

    asyncio.run(agent.start_pipeline(event_id, data))

    assert updates == [("processing", "satellite"), ("failed", "failed")], updates
    print("[ok] start_pipeline node failure -> processing/satellite then failed/failed")


def test_start_pipeline_crash_marks_failed() -> None:
    updates = []

    async def fake_update_status(event_id, status, step):
        updates.append((status, step))

    class FakeGraph:
        async def ainvoke(self, state):
            raise RuntimeError("graph blew up")

    _patch(orch_module, update_event_status=fake_update_status)

    agent = orch_module.OrchestratorAgent()
    agent._graph = FakeGraph()
    event_id = str(uuid.uuid4())
    data = {"location": "Dhaka", "disaster_type": "flood", "magnitude": None}

    asyncio.run(agent.start_pipeline(event_id, data))

    assert updates == [("processing", "satellite"), ("failed", "failed")], updates
    print("[ok] start_pipeline raised exception -> failed/failed, no crash propagated")


if __name__ == "__main__":
    test_start_pipeline_success_marks_complete()
    test_start_pipeline_node_failure_marks_failed()
    test_start_pipeline_crash_marks_failed()
    print("[done] orchestrator verified")
