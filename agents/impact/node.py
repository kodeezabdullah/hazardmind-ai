"""LangGraph node wrapper around the impact pipeline.

Thin adapter between PipelineState and agent.run_impact_analysis: unpacks the
hazard result from state, runs the existing 3-task impact pipeline unchanged
(the no-significant-disaster gate, parallel population+infrastructure via
asyncio.gather, sequential vulnerability, impact_data DB write all happen
inside run_impact_analysis exactly as before), and returns the partial state
update.
"""

import json
import logging

from agent import run_impact_analysis

from shared.pipeline_state import PipelineState

logger = logging.getLogger(__name__)


async def impact_node(state: PipelineState) -> dict:
    """Run impact analysis for the event and return the state update.

    Reads event_id/hazard_result from state, calls the pipeline, and writes
    impact_result + status/progress. Errors are appended to state["errors"]
    rather than raising, so the graph can decide how to proceed.
    """
    event_id = state["event_id"]
    disaster_type = state.get("disaster_type") or "flood"
    hazard_result = state.get("hazard_result") or {}
    hazard = hazard_result.get("hazard") or {}

    # Prefer hazard's explicit primary_hazard_risk (keyed by the REAL
    # disaster_type being assessed) over the old flood_risk-first fallback
    # chain, which silently read LOW/UNKNOWN on a genuine earthquake/landslide
    # event and masked a real HIGH/CRITICAL overall_severity (H#10). The old
    # chain is kept as a fallback for hazard results that predate this field
    # (e.g. cached/replayed payloads).
    risk_level = (
        hazard.get("primary_hazard_risk")
        or hazard.get("flood_risk")
        or hazard.get("overall_severity")
        or hazard.get("risk_level")
        or "UNKNOWN"
    )
    severity = hazard.get("overall_severity") or hazard.get("severity") or risk_level
    conf = hazard.get("confidence_scores") or {}
    if isinstance(conf, dict) and conf:
        try:
            overall_conf = sum(float(v) for v in conf.values()) / len(conf)
        except (TypeError, ValueError, ZeroDivisionError):
            overall_conf = float(hazard.get("overall_confidence", 0.0) or 0.0)
    else:
        overall_conf = float(hazard.get("overall_confidence", 0.0) or 0.0)

    bounds = hazard_result.get("bounds") or hazard.get("bounds") or {}
    risk_cities = hazard_result.get("risk_cities") or hazard.get("risk_cities") or []

    raw = await run_impact_analysis(
        event_id=event_id,
        bounds=bounds if isinstance(bounds, dict) else {},
        risk_level=str(risk_level).upper(),
        severity=str(severity),
        hazard_zones_geojson=hazard.get("risk_polygons") or {},
        flood_depth_estimate=float(hazard.get("flood_depth_estimate", 0.0) or 0.0),
        overall_confidence=overall_conf,
        risk_cities=risk_cities,
        disaster_type=disaster_type,
        flood_risk=str(hazard.get("flood_risk") or "UNKNOWN").upper(),
        earthquake_risk=str(hazard.get("earthquake_risk") or "UNKNOWN").upper(),
        landslide_risk=str(hazard.get("landslide_risk") or "UNKNOWN").upper(),
    )
    result = json.loads(raw)

    if result.get("status") != "complete":
        logger.error("Impact stage failed for %s: %s", event_id, result)
        return {
            "impact_result": result,
            "status": "failed",
            "current_step": "impact",
            "errors": (state.get("errors") or [])
            + [{"stage": "impact", "error": result.get("error") or result.get("status")}],
        }

    confidence_scores = dict(state.get("confidence_scores") or {})
    if result.get("data", {}).get("overall_confidence") is not None:
        confidence_scores["impact"] = result["data"]["overall_confidence"]

    anomalies = list(state.get("anomalies") or [])
    for a in result.get("anomalies") or []:
        anomalies.append({"stage": "impact", "message": a})

    return {
        "impact_result": result,
        "status": "report",
        "current_step": "impact",
        "progress": 75,
        "confidence_scores": confidence_scores,
        "anomalies": anomalies,
    }
