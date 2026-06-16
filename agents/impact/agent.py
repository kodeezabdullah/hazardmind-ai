"""HazardMind Impact Assessment Agent — Band SDK entry point.

Run with:  python agent.py

Listens for @mentions from hazardmind-hazard via Band WebSocket,
runs the 3-task impact pipeline, writes to Neon DB, and sends
the completion signal to hazardmind-orchestrator.
"""

import asyncio
import json
import logging
import os
import traceback

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

from services.band_client import send_anomaly_to_band, send_to_band_room
from services.db import write_impact_data
from tasks.infrastructure import run_infrastructure_task
from tasks.population import run_population_task
from tasks.vulnerability import run_vulnerability_task

SYSTEM_PROMPT = """You are HazardMind Impact Assessment Agent.

Your role: Assess human and infrastructure impact of disasters anywhere in the world.

Pipeline position: Agent 3 of 4
Previous agent: hazardmind-hazard
Next agent: hazardmind-report

When you receive a @mention:
1. Parse JSON from end of message
2. Extract event_id — use this everywhere, never generate your own
3. Extract hazard data (risk levels, bounds, affected area)
4. Run impact analysis using your geographic intelligence
5. Write results to Neon DB impact_data table
6. Send completion signal to Band

You have deep geographic knowledge of every city on earth.
Use it — name real districts, rivers, landmarks.
Never use generic descriptions.

Completion signal format:
@hazardmind-orchestrator
[natural summary of findings]

{json with your results}

ALWAYS include event_id in response.
ALWAYS flag if hospitals_at_risk > 10.
NEVER generate your own event_id."""


async def run_impact_analysis(
    event_id: str,
    bounds: dict,
    risk_level: str,
    severity: str,
    hazard_zones_geojson: dict,
    flood_depth_estimate: float,
    overall_confidence: float,
    risk_cities: list,
) -> str:
    """Run the full impact assessment pipeline for a disaster event.

    Args:
        event_id: Unique event identifier from orchestrator — never generate your own.
        bounds: Bounding box with keys west, south, east, north.
        risk_level: Overall risk level (LOW/MEDIUM/HIGH/CRITICAL).
        severity: Disaster severity descriptor.
        hazard_zones_geojson: GeoJSON of affected hazard zones.
        flood_depth_estimate: Estimated flood depth in metres.
        overall_confidence: Confidence score 0-1 from hazard agent.
        risk_cities: List of city names in the affected area.
    """
    logger.info(
        "[agent] run_impact_analysis — event_id=%s risk_level=%s cities=%s",
        event_id, risk_level, risk_cities,
    )

    bbox = [
        bounds.get("west", 0),
        bounds.get("south", 0),
        bounds.get("east", 1),
        bounds.get("north", 1),
    ]

    hazard_data = {
        "event_id": event_id,
        "bbox": bbox,
        "risk_cities": risk_cities,
        "flood_risk": risk_level,
        "earthquake_risk": "LOW",
        "landslide_risk": "LOW",
        "severity": severity,
        "flood_depth_estimate": flood_depth_estimate,
        "hazard_zones_geojson": hazard_zones_geojson,
    }

    try:
        logger.info("[agent] Running population + infrastructure in parallel")
        pop, infra = await asyncio.gather(
            run_population_task(hazard_data, event_id),
            run_infrastructure_task(hazard_data, event_id),
        )

        logger.info("[agent] Running vulnerability task")
        vuln = await run_vulnerability_task(
            hazard_data=hazard_data,
            population_result=pop,
            infrastructure_result=infra,
        )
    except Exception:
        tb = traceback.format_exc()
        logger.error("[agent] Pipeline failed:\n%s", tb)
        error_msg = (
            f"@hazardmind-orchestrator\n"
            f"ERROR: Impact assessment failed for event {event_id}.\n"
            f"```\n{tb[-800:]}\n```"
        )
        await send_to_band_room(error_msg)
        return json.dumps({"event_id": event_id, "status": "error", "error": str(tb[-400:])})

    # ── DB write (non-fatal) ─────────────────────────────────────────────────
    if os.environ.get("NEON_DATABASE_URL"):
        try:
            await write_impact_data(event_id, pop, infra, vuln)
            logger.info("[agent] DB write complete for event_id=%s", event_id)
        except Exception as exc:
            logger.error("[agent] DB write failed (non-fatal): %s", exc)
    else:
        logger.warning("[agent] NEON_DATABASE_URL not set — skipping DB write")

    # ── Anomaly flags ────────────────────────────────────────────────────────
    hospitals = int(infra.get("hospitals_at_risk", 0) or 0)
    if hospitals > 10:
        await send_anomaly_to_band(
            f"@hazardmind-orchestrator\n"
            f"CRITICAL: {hospitals} hospitals in disaster zone for event {event_id}.\n"
            f"Immediate NDMA Level-3 response recommended."
        )

    if overall_confidence < 0.7:
        await send_anomaly_to_band(
            f"@hazardmind-orchestrator\n"
            f"Low confidence ({overall_confidence:.2f}) on impact data for event {event_id}.\n"
            f"Proceeding with caution — recommend field verification."
        )

    # ── Derive city name for completion signal ───────────────────────────────
    city = (risk_cities[0] if risk_cities else event_id)
    pop_count = int(pop.get("population_affected", 0) or 0)
    vuln_score = vuln.get("vulnerability_score", 0)

    natural_text = (
        f"@hazardmind-orchestrator\n"
        f"Impact assessment complete for {city}.\n"
        f"{pop_count:,} population in affected zones.\n"
        f"{hospitals} hospitals at risk — "
        + ("CRITICAL: Immediate NDMA notification recommended." if hospitals > 10 else "monitoring required.")
        + f"\nVulnerability score: {vuln_score}/10\n"
        f"Handing off to report agent."
    )

    json_data = {
        "event_id": event_id,
        "agent": "hazardmind-impact",
        "status": "complete",
        "step": "impact",
        "data": {
            "total_affected": pop_count,
            "high_risk_people": int(pop.get("high_risk_people", int(pop_count * 0.2)) or int(pop_count * 0.2)),
            "medium_risk_people": int(pop.get("medium_risk_people", int(pop_count * 0.5)) or int(pop_count * 0.5)),
            "hospitals_at_risk": hospitals,
            "schools_at_risk": int(infra.get("schools_at_risk", 0) or 0),
            "roads_blocked": round(float(infra.get("roads_blocked_km", 0) or 0), 1),
            "bridges_at_risk": int(infra.get("bridges_at_risk", 0) or 0),
            "vulnerability_score": str(vuln_score),
            "evacuation_routes": vuln.get("priority_zones", []),
            "estimated_evacuation_time": (
                infra.get("estimated_evacuation_time")
                or vuln.get("estimated_evacuation_time", "4-6 hours")
            ),
            "overall_confidence": overall_confidence,
        },
    }

    message = f"{natural_text}\n\n{json.dumps(json_data, indent=2)}"
    await send_to_band_room(message)

    logger.info(
        "[agent] Completion signal sent — event_id=%s pop=%d hospitals=%d score=%s",
        event_id, pop_count, hospitals, vuln_score,
    )
    return json.dumps(json_data)


async def main() -> None:
    load_dotenv()

    try:
        from band import Agent
        from band.adapters.langgraph import LangGraphAdapter
        from langchain_openai import ChatOpenAI
        from langgraph.checkpoint.memory import InMemorySaver
    except ImportError:
        logger.error(
            "band-sdk not installed. Run: pip install band-sdk[langgraph] langchain-openai"
        )
        raise

    # Band per-turn LLM runs on the LangGraph adapter backed by Featherless
    # (OpenAI-compatible /v1/chat/completions). The intelligence layer
    # (shared/utils/llm_fallback.py) keeps its own AIML/GPT last-resort chain.
    llm = ChatOpenAI(
        model="moonshotai/Kimi-K2.6",
        api_key=os.getenv("FEATHERLESS_API_KEY", ""),
        base_url="https://api.featherless.ai/v1",
    )
    adapter = LangGraphAdapter(
        llm=llm,
        checkpointer=InMemorySaver(),
        custom_section=SYSTEM_PROMPT,
        additional_tools=[run_impact_analysis],
    )

    agent = Agent.create(
        adapter=adapter,
        agent_id=os.getenv("BAND_AGENT_ID", ""),
        api_key=os.getenv("BAND_API_KEY", ""),
    )

    logger.info(
        "[agent] HazardMind Impact Agent starting — handle=%s agent_id=%s",
        os.getenv("BAND_HANDLE", "unknown"),
        os.getenv("BAND_AGENT_ID", "unknown"),
    )
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
