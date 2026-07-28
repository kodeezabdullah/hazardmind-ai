"""Endpoint test for GET /results/{job_id}/evidence (feat/durable-evidence-trail).

Run from the backend/ directory: python test_evidence_endpoint.py

Stubs db.get_event_evidence so no live Neon connection is needed, then
exercises the endpoint via FastAPI's TestClient:
  - known job_id -> 200 with satellite/hazard_zones/impact diagnostics
  - a NULL confidence field survives as None (not coerced to 0)
  - unknown job_id -> 404
"""
import uuid

from fastapi.testclient import TestClient

import router as router_module
from main import app

client = TestClient(app)


def test_unknown_job_id_returns_404() -> None:
    async def fake_get_event_evidence(event_id: str):
        return None

    router_module.get_event_evidence = fake_get_event_evidence

    job_id = str(uuid.uuid4())
    resp = client.get(f"/results/{job_id}/evidence")
    assert resp.status_code == 404, resp.text
    print("[ok] unknown job_id -> 404")


def test_known_job_id_returns_full_evidence_trail() -> None:
    job_id = str(uuid.uuid4())

    async def fake_get_event_evidence(event_id: str):
        return {
            "event_id": uuid.UUID(event_id),
            "disaster_type": "flood",
            "location": "Rawalpindi, Pakistan",
            "status": "complete",
            "step": "complete",
            "satellite": {
                "satellite_type": "sentinel-2",
                "confidence": 0.82,
                "confidence_basis": "evidence_supports",
                "evidence_count": 4,
                "coverage_percent": 96.5,
                "coverage_status": "target_met",
                "index_calibrated": True,
                "index_units": "NDWI_ratio",
                "selection_reason": "aoi_scl_measured",
                "scene_cloud_percent": 22.0,
                "aoi_cloud_percent": 4.5,
                "scl_reused": True,
                # A NULL confidence on an OLD row means "not recorded" --
                # simulated here on a different field to prove the endpoint
                # doesn't coerce None to 0/False anywhere along the way.
                "diagnostics": {"gap_count": 0, "processing_level": "L2A"},
            },
            "hazard_zones": [
                {
                    "hazard_type": "flood",
                    "risk_level": "LOW",
                    "overall_confidence": None,  # NULL -- pre-migration row
                    "diagnostics": None,
                },
                {
                    "hazard_type": "earthquake",
                    "risk_level": "LOW",
                    "overall_confidence": 0.85,
                    "diagnostics": {
                        "primary_hazard_risk": "LOW",
                        "raw": {"usgs_query": {"url": "https://example"}},
                    },
                },
            ],
            "impact": {
                "overall_confidence": 0.42,
                "diagnostics": {
                    "population_confidence": 0.8,
                    "infrastructure_confidence": 0.75,
                    "vulnerability_confidence": None,
                },
            },
        }

    router_module.get_event_evidence = fake_get_event_evidence

    resp = client.get(f"/results/{job_id}/evidence")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == job_id, body
    assert body["disaster_type"] == "flood", body
    assert body["satellite"]["confidence"] == 0.82, body
    assert body["satellite"]["diagnostics"]["gap_count"] == 0, body
    print("[ok] known job_id -> 200 with full evidence trail")

    # NULL survival: a NULL overall_confidence must come back as None/null,
    # never coerced to 0 (which would misleadingly read as "total certainty
    # this is not a hazard" instead of "not recorded").
    flood_row = body["hazard_zones"][0]
    assert flood_row["overall_confidence"] is None, (
        f"expected NULL confidence to survive as None, got {flood_row['overall_confidence']!r}"
    )
    assert flood_row["diagnostics"] is None, flood_row
    print("[ok] NULL confidence/diagnostics on a pre-migration row survives as None, not 0")

    vuln_conf = body["impact"]["diagnostics"]["vulnerability_confidence"]
    assert vuln_conf is None, f"expected None, got {vuln_conf!r}"
    print("[ok] NULL vulnerability_confidence inside diagnostics survives as None")


def test_evidence_available_regardless_of_status() -> None:
    """Unlike /results, /results/{id}/evidence does not gate on status ==
    'complete' -- a partially-run event's satellite/hazard rows are still
    useful for debugging why a later stage failed."""
    job_id = str(uuid.uuid4())

    async def fake_get_event_evidence(event_id: str):
        return {
            "event_id": uuid.UUID(event_id),
            "disaster_type": "flood",
            "location": "Test City",
            "status": "failed",
            "step": "hazard",
            "satellite": {"satellite_type": "sentinel-2", "confidence": 0.7},
            "hazard_zones": None,
            "impact": None,
        }

    router_module.get_event_evidence = fake_get_event_evidence

    resp = client.get(f"/results/{job_id}/evidence")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed", body
    assert body["satellite"]["confidence"] == 0.7, body
    print("[ok] evidence available for a failed/in-progress event, not gated on status==complete")


if __name__ == "__main__":
    test_unknown_job_id_returns_404()
    test_known_job_id_returns_full_evidence_trail()
    test_evidence_available_regardless_of_status()
    print("[done] /results/{id}/evidence endpoint verified")
