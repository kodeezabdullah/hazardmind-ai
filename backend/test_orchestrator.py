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


async def _noop(*_args, **_kwargs):
    return {}


def test_start_pipeline_moves_to_satellite() -> None:
    updates = []
    notified = {}
    thoughts = []
    tasks = []

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

    async def fake_thought(content, room_id=None):
        thoughts.append(content)
        return {}

    async def fake_task(task_name, status, result=None, room_id=None):
        tasks.append((task_name, status))
        return {}

    _patch(
        orch_module,
        update_event_status=fake_update,
        notify_satellite=fake_notify,
        send_thought=fake_thought,
        send_task_update=fake_task,
    )

    agent = orch_module.OrchestratorAgent()
    event_id = str(uuid.uuid4())
    data = {"location": "Lahore", "disaster_type": "flood", "magnitude": 6.1}

    asyncio.run(agent.start_pipeline(event_id, data))

    assert updates == [(event_id, "processing", "satellite")], updates
    assert notified["event_id"] == event_id, notified
    assert notified["location"] == "Lahore", notified
    assert any("flood" in t and "Lahore" in t for t in thoughts), thoughts
    assert ("Pipeline", "started") in tasks, tasks
    print("[ok] start_pipeline -> processing/satellite + thought + task + notify")


def test_monitor_progress_steps_to_complete() -> None:
    import band_client as bc

    updates = []
    event_id = str(uuid.uuid4())

    # On each sleep (poll boundary) one more agent reports complete into the
    # inbound store — the new source of truth for _agent_completed.
    completions = ["satellite", "hazard", "impact", "report"]
    fed = {"n": 0}

    async def fake_update(event_id, status, step):
        updates.append((status, step))

    async def fake_sleep(_seconds):
        if fed["n"] < len(completions):
            agent = completions[fed["n"]]
            bc.inbound_store.add(
                {"content": f"event_id: {event_id}\n{agent} complete"}
            )
            fed["n"] += 1

    # Reset the shared store and seed the first completion before monitoring.
    bc.inbound_store._messages.clear()
    bc.inbound_store.add({"content": f"event_id: {event_id}\nsatellite complete"})
    fed["n"] = 1  # satellite already fed

    _patch(
        orch_module,
        update_event_status=fake_update,
        send_thought=_noop,
        send_task_update=_noop,
        send_text_message=_noop,
    )
    orig_sleep = orch_module.asyncio.sleep
    orch_module.asyncio.sleep = fake_sleep
    try:
        agent = orch_module.OrchestratorAgent()
        final = asyncio.run(agent.monitor_progress(event_id))
    finally:
        orch_module.asyncio.sleep = orig_sleep
        bc.inbound_store._messages.clear()

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
    events = []

    async def fake_update(event_id, status, step):
        updates.append((status, step))

    async def fake_text(content, mentions=None, room_id=None):
        alerts.append(content)
        return {}

    async def fake_event(event_type, title, data, room_id=None):
        events.append((event_type, data))
        return {}

    _patch(
        orch_module,
        update_event_status=fake_update,
        send_text_message=fake_text,
        send_event=fake_event,
        send_task_update=_noop,
    )

    agent = orch_module.OrchestratorAgent()
    event_id = str(uuid.uuid4())
    asyncio.run(agent.handle_failure(event_id, "hazard", "boom"))

    assert updates == [("failed", "failed")], updates
    assert len(alerts) == 1, alerts
    assert "hazard" in alerts[0] and event_id in alerts[0], alerts[0]
    assert events and events[0][0] == "error", events
    assert events[0][1]["agent"] == "hazard" and events[0][1]["error"] == "boom", events
    print("[ok] handle_failure -> failed + error event + Band alert")


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


def test_parse_incoming_message_text_and_event() -> None:
    import json

    import band_client as bc

    # Plain text with an event_id line.
    text_msg = {
        "content": "event_id: abc-123\nsatellite complete",
        "type": "text",
        "sender": {"handle": "hazardmind-satellite"},
    }
    parsed = bc.parse_incoming_message(text_msg)
    assert parsed["type"] == "text", parsed
    assert parsed["event_id"] == "abc-123", parsed
    assert parsed["agent"] == "hazardmind-satellite", parsed
    assert parsed["data"] is None, parsed

    # Structured event whose content is a JSON body.
    event_msg = {
        "type": "thought",
        "content": json.dumps(
            {
                "event": "thought",
                "title": "Orchestrator thought",
                "data": {"content": "Cloud cover 42%, switching to SAR"},
            }
        ),
    }
    parsed = bc.parse_incoming_message(event_msg)
    assert parsed["type"] == "thought", parsed
    assert parsed["content"] == "Cloud cover 42%, switching to SAR", parsed
    assert parsed["data"]["content"].startswith("Cloud cover"), parsed
    print("[ok] parse_incoming_message -> handles text + event")


def test_monitor_detects_structured_completion() -> None:
    """_agent_completed should fire on a structured {step,status} signal too."""
    import json

    import band_client as bc

    event_id = str(uuid.uuid4())
    bc.inbound_store._messages.clear()
    bc.inbound_store.add(
        {
            "type": "task",
            "content": json.dumps(
                {
                    "event": "complete",
                    "data": {
                        "event_id": event_id,
                        "step": "satellite",
                        "status": "complete",
                    },
                }
            ),
        }
    )
    try:
        agent = orch_module.OrchestratorAgent()
        done = asyncio.run(agent._agent_completed(event_id, "satellite"))
    finally:
        bc.inbound_store._messages.clear()
    assert done is True, "structured completion not detected"
    print("[ok] _agent_completed -> detects structured completion signal")


def test_recording_adapter_records_inbound() -> None:
    """The recording adapter buffers inbound Band messages into the store."""
    import band_client as bc

    bc.inbound_store._messages.clear()
    from band.adapters import AnthropicAdapter

    adapter = orch_module._record_only(AnthropicAdapter)(
        model=orch_module.ANTHROPIC_MODEL
    )

    class FakeMsg:
        id = "m1"
        content = "event_id: JOB-1\nsatellite complete"
        message_type = "text"
        sender_id = "sat-id"
        sender_name = "HazardMind Satellite"
        created_at = "2026-06-15T07:00:00Z"

    class FakeHistory:
        def convert(self, _converter):
            return self

    class FakeInput:
        msg = FakeMsg()
        tools = None
        history = FakeHistory()
        participants_msg = None
        contacts_msg = None
        is_session_bootstrap = False
        room_id = "room-1"

    # Stub the Anthropic super().on_event so no live LLM call is made.
    async def noop(self, inp):
        return None

    type(adapter).__bases__[0].on_event = noop
    try:
        asyncio.run(adapter.on_event(FakeInput()))
        stored = bc.inbound_store.all()
        assert len(stored) == 1, stored
        assert stored[0]["agent"] == "HazardMind Satellite", stored[0]
        assert stored[0]["event_id"] == "JOB-1", stored[0]
    finally:
        bc.inbound_store._messages.clear()
    print("[ok] recording adapter -> records inbound message into store")


if __name__ == "__main__":
    test_parse_incoming_message_text_and_event()
    test_recording_adapter_records_inbound()
    test_monitor_detects_structured_completion()
    test_start_pipeline_moves_to_satellite()
    test_monitor_progress_steps_to_complete()
    test_handle_failure_marks_failed_and_alerts()
    test_analyze_starts_pipeline()
    print("[done] orchestrator verified")
