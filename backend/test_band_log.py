"""Endpoint test for GET /band-log/{job_id}.

Run from the backend/ directory:  python test_band_log.py

Stubs db.get_event_status and band_client.get_room_messages so no live Neon
or Band connection is needed, then exercises the two documented cases via
FastAPI's TestClient:
  - known job_id   -> 200 with a mapped messages list
  - unknown job_id -> 404
"""
import uuid

from fastapi.testclient import TestClient

import router as router_module
from main import app

client = TestClient(app)


def test_unknown_job_id_returns_404() -> None:
    async def fake_get_event_status(event_id: str):
        return None

    router_module.get_event_status = fake_get_event_status

    job_id = str(uuid.uuid4())
    resp = client.get(f"/band-log/{job_id}")
    assert resp.status_code == 404, resp.text
    print("[ok] unknown job_id -> 404")


def test_known_job_id_returns_messages() -> None:
    job_id = str(uuid.uuid4())

    async def fake_get_event_status(event_id: str):
        return {
            "event_id": uuid.UUID(event_id),
            "status": "satellite",
            "step": "satellite",
            "progress": 20,
        }

    async def fake_get_room_messages(room_id, event_id):
        return [
            {
                "content": f"event_id: {event_id}\nStarting analysis.",
                "created_at": "2026-06-15T10:00:00Z",
                "type": "text",
                "sender": {"handle": "hazardmind-satellite"},
            }
        ]

    router_module.get_event_status = fake_get_event_status
    router_module.get_room_messages = fake_get_room_messages

    resp = client.get(f"/band-log/{job_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == job_id, body
    assert len(body["messages"]) == 1, body
    msg = body["messages"][0]
    assert msg["agent"] == "hazardmind-satellite", msg
    assert msg["type"] == "text", msg
    assert msg["timestamp"] == "2026-06-15T10:00:00Z", msg
    print("[ok] known job_id -> messages list")


if __name__ == "__main__":
    test_unknown_job_id_returns_404()
    test_known_job_id_returns_messages()
    print("[done] /band-log endpoint verified")
