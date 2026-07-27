"""LangGraph node wrapper around the report pipeline.

Thin adapter between PipelineState and pipeline.run_report_pipeline: the
existing pipeline (context fetch, LLM narrative generation with strict-mode
section assertions, Pillow static map, ReportLab PDF, R2 upload, final_reports
DB write) runs unchanged, driven directly from state instead of a Band
handoff message.
"""

import logging

from pipeline import run_report_pipeline

from shared.pipeline_state import PipelineState

logger = logging.getLogger(__name__)


async def report_node(state: PipelineState) -> dict:
    """Run report generation for the event and return the state update.

    Reads event_id from state (the pipeline fetches satellite/hazard/impact
    context straight from the DB, the durable record for those stages) and
    writes report_result + status/progress. A total pipeline failure (e.g.
    every LLM section falling back to templated text under strict mode)
    surfaces as status: failed, never a fake success.
    """
    event_id = state["event_id"]

    # H#6: thread PipelineState's confidence_scores through as incoming_payload
    # so satellite's confidence becomes a genuinely live input to report's
    # confidence aggregation (agents/report/pipeline.py's
    # _merge_incoming_payload_into_context), not just a dead read key. This is
    # additive on top of the existing DB-fetch context — fetch_from_db and
    # incoming_payload are already designed to compose (see pipeline.py).
    result = await run_report_pipeline(
        event_id=event_id,
        fetch_from_db=True,
        upload_r2=True,
        write_db=True,
        use_llm=True,
        incoming_payload={"confidence_scores": state.get("confidence_scores") or {}},
    )

    if result.get("status") == "failed":
        logger.error("Report stage failed for %s: %s", event_id, result)
        return {
            "report_result": result,
            "status": "failed",
            "current_step": "report",
            "errors": (state.get("errors") or [])
            + [{"stage": "report", "error": result.get("error") or result.get("status")}],
        }

    confidence_scores = dict(state.get("confidence_scores") or {})
    confidence_level = result.get("confidence_level")
    if confidence_level:
        confidence_scores["report"] = confidence_level

    return {
        "report_result": result,
        "status": "complete",
        "current_step": "report",
        "progress": 100,
        "confidence_scores": confidence_scores,
    }
