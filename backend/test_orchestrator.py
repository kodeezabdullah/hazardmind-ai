"""Tests for OrchestratorAgent and the wired-up POST /analyze.

Run from the backend/ directory:  python test_orchestrator.py

Stubs the DB and Band REST helpers so no live Neon or Band connection is
needed, then verifies:
  - start_pipeline() sets status -> processing/satellite and mentions satellite
  - monitor_progress() steps the DB hazard -> impact -> report -> complete as
    each "<agent> complete" marker appears in the room transcript
  - handle_failure() marks the event failed and posts an alert
  - POST /analyze runs start_pipeline and returns the new job_id
"""
import asyncio
import uuid

from fastapi.testclient import TestClient

import orchestrator as orch_module
import router as router_module
from main import app


def _patch(module, **attrs):
    for name, value in attrs.items():
        setattr(module, name, value)


def test_start_pipeline_moves_to_satellite() -> None:
    updates = []
    notified = {}

    async def fake_update(event_id, status, step):
        updates.append((event_id, status, step))

    async def fake_notify(event_id, location, disaster_type, magnitude):
        notified.update(
            event_id=event_id,
            location=location,
            disaster_type=disaster_type,
            magnitude=magnitude,
        )
        return {}

    _patch(orch_module, update_event_status=fake_update, notify_satellite=fake_notify)

    agent = orch_module.OrchestratorAgent()
    event_id = str(uuid.uuid4())
    data = {"location": "Lahore", "disaster_type": "flood", "magnitude": 6.1}

    asyncio.run(agent.start_pipeline(event_id, data))

    assert updates == [(event_id, "processing", "satellite")], updates
    assert notified["event_id"] == event_id, notified
    assert notified["location"] == "Lahore", notified
    print("[ok] start_pipeline -> processing/satellite + satellite notified")


def test_monitor_progress_steps_to_complete() -> None:
    updates = []
    event_id = str(uuid.uuid4())

    # Transcript reveals one more agent's completion on each poll.
    transcripts = [
        [{"content": f"event_id: {event_id}\nsatellite complete"}],
        [{"content": f"event_id: {event_id}\nsatellite complete\nhazard complete"}],
        [{"content": f"event_id: {event_id}\nimpact complete"}],
        [{"content": f"event_id: {event_id}\nreport complete"}],
    ]
    calls = {"n": 0}

    async def fake_update(event_id, status, step):
        updates.append((status, step))

    async def fake_get_room_messages(room_id, ev_id):
        idx = min(calls["n"], len(transcripts) - 1)
        calls["n"] += 1
        return transcripts[idx]

    async def fake_sleep(_seconds):
        return None

    _patch(
        orch_module,
        update_event_status=fake_update,
        get_room_messages=fake_get_room_messages,
    )
    orig_sleep = orch_module.asyncio.sleep
    orch_module.asyncio.sleep = fake_sleep
    try:
        agent = orch_module.OrchestratorAgent()
        final = asyncio.run(agent.monitor_progress(event_id))
    finally:
        orch_module.asyncio.sleep = orig_sleep

    assert final == "complete", final
    assert updates == [
        ("processing", "hazard"),
        ("processing", "impact"),
        ("processing", "report"),
        ("complete", "complete"),
    ], updates
    print("[ok] monitor_progress -> hazard/impact/report/complete")


def test_handle_failure_marks_failed_and_alerts() -> None:
    updates = []
    alerts = []

    async def fake_update(event_id, status, step):
        updates.append((status, step))

    async def fake_send(content, mention_ids, room_id=None):
        alerts.append(content)
        return {}

    _patch(orch_module, update_event_status=fake_update, send_band_message=fake_send)

    agent = orch_module.OrchestratorAgent()
    event_id = str(uuid.uuid4())
    asyncio.run(agent.handle_failure(event_id, "hazard", "boom"))

    assert updates == [("failed", "failed")], updates
    assert len(alerts) == 1, alerts
    assert "hazard" in alerts[0] and event_id in alerts[0], alerts[0]
    print("[ok] handle_failure -> failed + Band alert")


def test_analyze_starts_pipeline() -> None:
    created = {}
    started = {}

    async def fake_create(event_id, location, disaster_type, magnitude):
        created.update(event_id=event_id, location=location)

    async def fake_start_pipeline(event_id, disaster_data):
        started.update(event_id=event_id, data=disaster_data)

    router_module.create_disaster_event = fake_create
    router_module.orchestrator.start_pipeline = fake_start_pipeline

    client = TestClient(app)
    resp = client.post(
        "/analyze",
        json={"location": "Karachi", "disaster_type": "earthquake", "magnitude": 5.4},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "processing", body
    assert body["job_id"] == started["event_id"] == created["event_id"], body
    assert started["data"]["location"] == "Karachi", started
    print("[ok] POST /analyze -> start_pipeline + job_id")


if __name__ == "__main__":
    test_start_pipeline_moves_to_satellite()
    test_monitor_progress_steps_to_complete()
    test_handle_failure_marks_failed_and_alerts()
    test_analyze_starts_pipeline()
    print("[done] orchestrator verified")
