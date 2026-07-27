"""LangGraph node wrapper around the hazard pipeline.

Thin adapter between PipelineState and agent.analyze_hazard: reads the
satellite result from state, runs the existing deterministic multi-hazard
analysis unchanged (flood NDWI, earthquake USGS, landslide DEM slope math,
hazard_zones DB write all happen inside analyze_hazard exactly as before),
and returns the partial state update.
"""

import logging

from agent import analyze_hazard

from shared.pipeline_state import PipelineState

logger = logging.getLogger(__name__)


async def hazard_node(state: PipelineState) -> dict:
    """Run hazard analysis for the event and return the state update.

    Reads event_id/satellite_result from state, calls the pipeline, and
    writes hazard_result + status/progress. Errors are appended to
    state["errors"] rather than raising, so the graph can decide how to
    proceed.
    """
    event_id = state["event_id"]
    satellite_result = state.get("satellite_result") or {}
    disaster_type = state.get("disaster_type") or "flood"

    result = await analyze_hazard(satellite_result, event_id, disaster_type=disaster_type)

    if result.get("status") == "error":
        logger.error("Hazard stage failed for %s: %s", event_id, result)
        return {
            "hazard_result": result,
            "status": "failed",
            "current_step": "hazard",
            "errors": (state.get("errors") or [])
            + [{"stage": "hazard", "error": result.get("error") or result.get("status")}],
        }

    confidence_scores = dict(state.get("confidence_scores") or {})
    hazard_confidences = (result.get("hazard") or {}).get("confidence_scores") or {}
    if hazard_confidences:
        avg = sum(hazard_confidences.values()) / len(hazard_confidences)
        confidence_scores["hazard"] = avg

    errors = list(state.get("errors") or [])
    anomalies = list(state.get("anomalies") or [])
    if result.get("status") == "insufficient_data":
        # A plumbing failure (e.g. invalid bbox), not a real disaster verdict
        # (H#3). Do NOT halt the graph — impact's own no-significant-disaster
        # gate already treats UNKNOWN risk honestly (reports zero impact
        # rather than inventing numbers), so the pipeline can safely continue.
        # The failure itself must still be visible in pipeline_log, not
        # silently swallowed at this boundary.
        logger.warning("Hazard stage got insufficient data for %s: %s", event_id, result.get("error"))
        errors.append({"stage": "hazard", "error": result.get("error") or "insufficient_data"})
        anomalies.append({
            "stage": "hazard",
            "message": (
                "Hazard analysis could not be completed (insufficient_data) — "
                f"{result.get('error') or 'no further detail'}. overall_severity "
                "is UNKNOWN, not a real disaster verdict."
            ),
        })

    return {
        "hazard_result": result,
        "status": "impact",
        "current_step": "hazard",
        "progress": 50,
        "confidence_scores": confidence_scores,
        "errors": errors,
        "anomalies": anomalies,
    }
