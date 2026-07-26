"""Endpoint test for GET /pipeline-log/{job_id}.

Run from the backend/ directory:  python test_pipeline_log.py

Replaces the deleted test_band_log.py now that there is no Band transcript to
read — this endpoint reads disaster_events.pipeline_log (written once by
orchestrator.start_pipeline() at the end of a run) instead of a live message
store. Stubs db.get_pipeline_log so no live Neon connection is needed.
"""
import uuid

from fastapi.testclient import TestClient

import router as router_module
from main import app

client = TestClient(app)


def test_unknown_job_id_returns_404() -> None:
    async def fake_get_pipeline_log(event_id: str):
        return None

    router_module.get_pipeline_log = fake_get_pipeline_log

    job_id = str(uuid.uuid4())
    resp = client.get(f"/pipeline-log/{job_id}")
    assert resp.status_code == 404, resp.text
    print("[ok] unknown job_id -> 404")


def test_known_job_id_returns_log() -> None:
    job_id = str(uuid.uuid4())

    async def fake_get_pipeline_log(event_id: str):
        return {
            "event_id": uuid.UUID(event_id),
            "status": "failed",
            "step": "failed",
            "pipeline_log": {
                "errors": [{"stage": "hazard", "error": "boom"}],
                "anomalies": [{"stage": "impact", "message": "low confidence"}],
                "confidence_scores": {"satellite": 0.9},
            },
        }

    router_module.get_pipeline_log = fake_get_pipeline_log

    resp = client.get(f"/pipeline-log/{job_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == job_id, body
    assert body["status"] == "failed", body
    assert body["errors"] == [{"stage": "hazard", "error": "boom"}], body
    assert body["anomalies"] == [{"stage": "impact", "message": "low confidence"}], body
    assert body["confidence_scores"] == {"satellite": 0.9}, body
    print("[ok] known job_id -> pipeline log")


def test_known_job_id_with_no_log_yet_returns_empty_lists() -> None:
    job_id = str(uuid.uuid4())

    async def fake_get_pipeline_log(event_id: str):
        return {
            "event_id": uuid.UUID(event_id),
            "status": "processing",
            "step": "satellite",
            "pipeline_log": None,
        }

    router_module.get_pipeline_log = fake_get_pipeline_log

    resp = client.get(f"/pipeline-log/{job_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["errors"] == [], body
    assert body["anomalies"] == [], body
    assert body["confidence_scores"] == {}, body
    print("[ok] in-progress job (no pipeline_log yet) -> empty lists")


if __name__ == "__main__":
    test_unknown_job_id_returns_404()
    test_known_job_id_returns_log()
    test_known_job_id_with_no_log_yet_returns_empty_lists()
    print("[done] /pipeline-log endpoint verified")
