"""HazardMind Impact Assessment Agent — pipeline entry point.

Runs the 3-task impact pipeline (population + infrastructure in parallel,
then vulnerability) and writes to Neon DB. Called directly as a LangGraph
node (see node.py) with a full event_id and the hazard result dict — no
transport-layer indirection.
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

from services.db import write_impact_data
from tasks.infrastructure import run_infrastructure_task
from tasks.population import run_population_task
from tasks.vulnerability import run_vulnerability_task


def _no_significant_disaster(risk_level: str, overall_confidence: float) -> bool:
    """True when the hazard verdict means there is no real disaster to assess.

    A NEUTRAL verification ("is there flooding?") can legitimately answer "no".
    We treat the event as no-significant-disaster when the hazard risk is
    LOW / NONE / UNKNOWN. (UNKNOWN means hazard could not confirm a hazard — the
    honest impact is zero, not an invented population.) Overridable via
    IMPACT_FORCE_ASSESS=true for debugging.
    """
    if os.getenv("IMPACT_FORCE_ASSESS", "").lower() == "true":
        return False
    rl = str(risk_level or "").strip().upper()
    return rl in ("LOW", "NONE", "UNKNOWN", "", "MINIMAL", "NEGLIGIBLE")


async def _emit_no_impact(
    event_id: str, risk_cities: list, risk_level: str, overall_confidence: float
) -> str:
    """Emit an honest zero-impact completion (no fabricated population)."""
    city = risk_cities[0] if risk_cities else event_id
    json_data = {
        "event_id": event_id,
        "agent": "hazardmind-impact",
        "from": "hazardmind-impact",
        "to": "hazardmind-report",
        "status": "complete",
        "step": "impact",
        "anomalies": [],
        "data": {
            "total_affected": 0,
            "high_risk_people": 0,
            "medium_risk_people": 0,
            "hospitals_at_risk": 0,
            "schools_at_risk": 0,
            "roads_blocked": 0.0,
            "bridges_at_risk": 0,
            "vulnerability_score": "0",
            "evacuation_routes": [],
            "estimated_evacuation_time": "N/A",
            "overall_confidence": overall_confidence,
            "no_significant_impact": True,
            "assessment_note": (
                f"Hazard risk assessed as {str(risk_level).upper()} for {city}; "
                "no significant disaster impact detected. No population or "
                "infrastructure reported at risk."
            ),
        },
    }
    # Persist the honest zero row too (so the DB reflects "assessed, no impact").
    if os.environ.get("NEON_DATABASE_URL"):
        try:
            await write_impact_data(
                event_id,
                {"population_affected": 0, "high_risk_people": 0, "medium_risk_people": 0},
                {"hospitals_at_risk": 0, "schools_at_risk": 0, "roads_blocked_km": 0, "bridges_at_risk": 0},
                {"vulnerability_score": "0", "priority_zones": [], "estimated_evacuation_time": "N/A"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[agent] no-impact DB write failed (non-fatal): %s", exc)
    logger.info("[agent] No-impact completion — event_id=%s", event_id)
    return json.dumps(json_data)


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
        event_id: Unique event identifier from the backend — never generate your own.
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

    # ── DECISION GATE (Solution 4): no-significant-disaster verdict ───────────
    # This is a NEUTRAL verification pipeline. If the hazard stage found no real
    # hazard (risk LOW/UNKNOWN/NONE), the honest answer is "no significant impact
    # — 0 affected", NOT a fabricated population. Without this gate the LLM is
    # prompted to always invent affected people, turning a clean "no flood"
    # result into a phantom disaster.
    if _no_significant_disaster(risk_level, overall_confidence):
        logger.info(
            "[agent] No-significant-disaster verdict (risk=%s conf=%.2f) — "
            "reporting zero impact honestly for event_id=%s",
            risk_level, overall_confidence, event_id,
        )
        return await _emit_no_impact(
            event_id, risk_cities, risk_level, overall_confidence
        )

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
    anomalies = []
    if hospitals > 10:
        anomalies.append(
            f"CRITICAL: {hospitals} hospitals in disaster zone for event {event_id}. "
            "Immediate NDMA Level-3 response recommended."
        )
    if overall_confidence < 0.7:
        anomalies.append(
            f"Low confidence ({overall_confidence:.2f}) on impact data for event {event_id}. "
            "Proceeding with caution — recommend field verification."
        )

    # ── Derive city name + build completion payload ──────────────────────────
    pop_count = int(pop.get("population_affected", 0) or 0)
    vuln_score = vuln.get("vulnerability_score", 0)

    json_data = {
        "event_id": event_id,
        "agent": "hazardmind-impact",
        "from": "hazardmind-impact",
        "to": "hazardmind-report",
        "status": "complete",
        "step": "impact",
        "anomalies": anomalies,
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

    logger.info(
        "[agent] Impact analysis complete — event_id=%s pop=%d hospitals=%d score=%s",
        event_id, pop_count, hospitals, vuln_score,
    )
    return json.dumps(json_data)
