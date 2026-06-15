"""Impact Assessment Agent — FastAPI local testing server.

For production: run  python agent.py  (Band SDK agent, listens via WebSocket)
For local UI:   run  uvicorn main:app --reload --port 8001

Accepts both:
  1. Raw hazard JSON  {"event_id": ..., "flood_risk": ..., "bbox": [...], ...}
  2. Band message format  {"event_id": ..., "step": "hazard", "data": {...}}
     where data may use `bounds` dict instead of `bbox` list.
"""

import asyncio
import json
import logging
import os
import sys

# Force UTF-8 stdout/stderr so print() works with any Unicode city name on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import traceback
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

from services.band_client import send_impact_result
from services.cost_tracker import cost_tracker
from services.db import write_results
from services.r2_reader import get_satellite_urls
from tasks.infrastructure import run_infrastructure_task
from tasks.population import run_population_task
from tasks.vulnerability import run_vulnerability_task


def _normalise_hazard(raw: dict) -> dict:
    """Accept both raw hazard JSON and Band message wrapper format.

    Band format:  {"event_id": ..., "step": "hazard", "data": {...bounds...}}
    Raw format:   {"event_id": ..., "flood_risk": ..., "bbox": [...]}
    """
    # Band message format — unwrap data field
    if "data" in raw and isinstance(raw["data"], dict):
        data   = dict(raw["data"])
        merged = {**data, "event_id": raw.get("event_id", data.get("event_id"))}
    else:
        merged = dict(raw)

    # Convert bounds dict → bbox list
    if "bounds" in merged and "bbox" not in merged:
        b = merged["bounds"]
        merged["bbox"] = [
            b.get("west", 0),
            b.get("south", 0),
            b.get("east", 1),
            b.get("north", 1),
        ]

    # Normalise risk level → flood_risk if only risk_level present
    if "flood_risk" not in merged and "risk_level" in merged:
        merged["flood_risk"] = merged["risk_level"]

    return merged


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Impact Assessment local server starting")
    yield
    logger.info("Impact Assessment local server shutting down")


app = FastAPI(
    title="HazardMind Impact Assessment",
    version="3.0.0",
    description=(
        "Local testing UI for Agent 3. "
        "Accepts raw hazard JSON or Band message format. "
        "For production: run python agent.py"
    ),
    lifespan=lifespan,
)


class AssessImpactRequest(BaseModel):
    hazard_data: Optional[dict] = None


class ImpactResponse(BaseModel):
    agent: str
    status: str
    event_id: str
    satellite_urls: dict
    # Population
    population_affected: int
    total_affected: int
    high_risk_people: int
    medium_risk_people: int
    vulnerable_population: int
    # Infrastructure
    hospitals_at_risk: int
    schools_at_risk: int
    roads_blocked_km: float
    roads_blocked: float
    bridges_at_risk: int
    # Vulnerability
    vulnerability_score: float
    estimated_evacuation_time: str
    evacuation_routes: list
    priority_zones: list
    # Meta
    criticality_levels: dict
    models_used: dict
    llm_reasoning: str
    cost_summary: dict


@app.get("/docs-redirect", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")


@app.post("/assess-impact", response_model=ImpactResponse)
async def assess_impact(request: AssessImpactRequest = None):
    """Run the full impact assessment pipeline.

    Supply `hazard_data` in the request body (raw JSON or Band message format),
    or omit to load from mock file.
    """
    try:
        return await _run_assessment(request)
    except HTTPException:
        raise
    except Exception as exc:
        print("=== 500 ERROR ===", flush=True)
        print(traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail=str(exc))


async def _run_assessment(request: AssessImpactRequest):
    # ── 0. Load + normalise hazard data ─────────────────────────────────────
    try:
        if request and request.hazard_data:
            raw_hazard = request.hazard_data
        else:
            from services.band_client import receive_hazard_data
            raw_hazard = await receive_hazard_data()

        hazard_data = _normalise_hazard(raw_hazard)
    except Exception as exc:
        logger.error("Failed to load hazard data: %s", exc)
        raise HTTPException(status_code=503, detail=f"Could not load hazard data: {exc}") from exc

    event_id = hazard_data.get("event_id")
    if not event_id:
        raise HTTPException(
            status_code=400,
            detail="event_id is required — never generate own",
        )
    cost_tracker.reset()

    logger.info(
        "Starting assessment — event_id=%s bbox=%s flood_risk=%s cities=%s",
        event_id, hazard_data.get("bbox"), hazard_data.get("flood_risk"),
        hazard_data.get("risk_cities"),
    )

    satellite_urls = get_satellite_urls(event_id)

    # ── 1+2. Population + Infrastructure in parallel ─────────────────────────
    try:
        logger.info("Launching Task 1 (population) and Task 2 (infrastructure) in parallel")
        population_result, infrastructure_result = await asyncio.gather(
            run_population_task(hazard_data, event_id),
            run_infrastructure_task(hazard_data, event_id),
        )
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Parallel tasks failed:\n%s", tb)
        raise HTTPException(status_code=500, detail=f"Task 1/2 failed: {exc}") from exc

    # ── 3. Vulnerability (sequential) ────────────────────────────────────────
    try:
        logger.info("Launching Task 3 (vulnerability)")
        vulnerability_result = await run_vulnerability_task(
            hazard_data, population_result, infrastructure_result
        )
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Vulnerability task failed:\n%s", tb)
        raise HTTPException(status_code=500, detail=f"Task 3 failed: {exc}") from exc

    # ── 4. DB write (non-fatal) ───────────────────────────────────────────────
    if os.environ.get("NEON_DATABASE_URL"):
        try:
            await write_results(
                event_id, hazard_data, population_result, infrastructure_result, vulnerability_result
            )
        except Exception as exc:
            logger.error("DB write failed (non-fatal): %s", exc)
    else:
        logger.warning("NEON_DATABASE_URL not set — skipping DB write")

    # ── 5. Build response ─────────────────────────────────────────────────────
    pop_count  = int(population_result.get("population_affected", 0) or 0)
    hospitals  = int(infrastructure_result.get("hospitals_at_risk", 0) or 0)
    roads_km   = float(infrastructure_result.get("roads_blocked_km", 0) or 0)
    score      = float(vulnerability_result.get("vulnerability_score", 0) or 0)
    evac_time  = (
        infrastructure_result.get("estimated_evacuation_time")
        or vulnerability_result.get("estimated_evacuation_time", "unknown")
    )

    llm_reasoning = " | ".join(
        r for r in [
            population_result.get("llm_reasoning"),
            infrastructure_result.get("llm_reasoning"),
            vulnerability_result.get("llm_reasoning"),
        ] if r
    )

    output = {
        "agent":                  "hazardmind-impact",
        "status":                 "complete",
        "event_id":               event_id,
        "satellite_urls":         satellite_urls,
        "population_affected":    pop_count,
        "total_affected":         pop_count,
        "high_risk_people":       int(population_result.get("high_risk_people", int(pop_count * 0.2)) or int(pop_count * 0.2)),
        "medium_risk_people":     int(population_result.get("medium_risk_people", int(pop_count * 0.5)) or int(pop_count * 0.5)),
        "vulnerable_population":  int(population_result.get("vulnerable_estimate", 0) or 0),
        "hospitals_at_risk":      hospitals,
        "schools_at_risk":        int(infrastructure_result.get("schools_at_risk", 0) or 0),
        "roads_blocked_km":       roads_km,
        "roads_blocked":          round(float(roads_km or 0), 1),
        "bridges_at_risk":        int(infrastructure_result.get("bridges_at_risk", 0) or 0),
        "vulnerability_score":    score,
        "estimated_evacuation_time": evac_time,
        "evacuation_routes":      vulnerability_result.get("evacuation_routes", []),
        "priority_zones":         vulnerability_result.get("priority_zones", []),
        "criticality_levels": {
            "population":      population_result.get("criticality", "unknown"),
            "infrastructure":  infrastructure_result.get("criticality", "unknown"),
            "vulnerability":   vulnerability_result.get("criticality", "unknown"),
        },
        "models_used": {
            "population":      population_result.get("model_used", "unknown"),
            "infrastructure":  infrastructure_result.get("model_used", "unknown"),
            "vulnerability":   vulnerability_result.get("model_used", "unknown"),
        },
        "llm_reasoning":  llm_reasoning,
        "cost_summary":   cost_tracker.get_summary(),
    }

    logger.info(
        "Assessment complete — event_id=%s population=%d hospitals=%d score=%.1f cost=$%.4f",
        event_id, pop_count, hospitals, score,
        output["cost_summary"].get("estimated_cost_usd", 0),
    )

    # ── 6. Band send (non-fatal) ──────────────────────────────────────────────
    try:
        await send_impact_result(output)
    except Exception as exc:
        logger.error("Band send failed (non-fatal): %s", exc)

    return output


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "agent": "hazardmind-impact",
        "version": "3.0.0",
        "mode": "mock" if os.environ.get("USE_MOCK_BAND", "true").lower() == "true" else "live",
        "band_agent_id": os.environ.get("BAND_AGENT_ID", "not-set"),
    }


_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
