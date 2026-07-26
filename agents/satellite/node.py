"""LangGraph node wrapper around the satellite pipeline.

Thin adapter between PipelineState and agent.run_pipeline: unpacks the state
into a ProcessDisasterInput, runs the existing deterministic pipeline
unchanged (imagery processing, DB write, R2 upload all happen inside
run_pipeline exactly as before), and returns the partial state update.
"""

import json
import logging

from agent import ProcessDisasterInput, run_pipeline

from shared.pipeline_state import PipelineState

logger = logging.getLogger(__name__)


async def satellite_node(state: PipelineState) -> dict:
    """Run satellite analysis for the event and return the state update.

    Reads event_id/location/disaster_type/magnitude from state, calls the
    pipeline, and writes satellite_result + status/progress. Errors are
    appended to state["errors"] rather than raising, so the graph can decide
    how to proceed.
    """
    event_id = state["event_id"]
    params = ProcessDisasterInput(
        event_id=event_id,
        location=state["location"],
        disaster_type=state["disaster_type"],
        magnitude=state.get("magnitude"),
    )

    raw = await run_pipeline(params)
    result = json.loads(raw)

    if result.get("status") != "complete":
        logger.error("Satellite stage failed for %s: %s", event_id, result)
        return {
            "satellite_result": result,
            "status": "failed",
            "current_step": "satellite",
            "errors": (state.get("errors") or [])
            + [{"stage": "satellite", "error": result.get("error") or result.get("status")}],
        }

    confidence_scores = dict(state.get("confidence_scores") or {})
    if result.get("confidence") is not None:
        confidence_scores["satellite"] = result["confidence"]

    return {
        "satellite_result": result,
        "status": "hazard",
        "current_step": "satellite",
        "progress": 25,
        "confidence_scores": confidence_scores,
    }
