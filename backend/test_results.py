"""Endpoint test for GET /results/{job_id}.

Run from the backend/ directory:  python test_results.py

Stubs db.get_event_results so no live Neon connection is needed, then
exercises the two documented cases via FastAPI's TestClient:
  - known job_id, still processing -> 202 {"status": "processing", ...}
  - unknown job_id                 -> 404
"""
import uuid

from fastapi.testclient import TestClient

import router as router_module
from main import app

client = TestClient(app)


def test_unknown_job_id_returns_404() -> None:
    async def fake_get_event_results(event_id: str):
        return None

    router_module.get_event_results = fake_get_event_results

    job_id = str(uuid.uuid4())
    resp = client.get(f"/results/{job_id}")
    assert resp.status_code == 404, resp.text
    print("[ok] unknown job_id -> 404")


def test_known_job_id_still_processing_returns_202() -> None:
    job_id = str(uuid.uuid4())

    async def fake_get_event_results(event_id: str):
        return {
            "event_id": uuid.UUID(event_id),
            "status": "processing",
            "step": "satellite",
            "satellite": None,
            "hazard": None,
            "impact": None,
            "report": None,
        }

    router_module.get_event_results = fake_get_event_results

    resp = client.get(f"/results/{job_id}")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "processing", body
    assert body["step"] == "satellite", body
    assert body["message"] == "Pipeline still running", body
    print("[ok] known job_id (processing) -> 202")


if __name__ == "__main__":
    test_unknown_job_id_returns_404()
    test_known_job_id_still_processing_returns_202()
    print("[done] /results endpoint verified")
