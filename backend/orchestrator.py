"""Orchestrator: drives the 4-agent HazardMind pipeline as a LangGraph.

    satellite -> hazard -> impact -> report -> complete

Replaces the Band room/@mention/cross-validation-discussion transport with a
single in-process StateGraph (backend/graph.py). Each node calls its agent's
pipeline function directly and writes its own DB row as a side effect (see each
agent's agent.py/pipeline.py) — PipelineState is the in-memory hand-off, the DB
is the durable record, matching CLAUDE.md's "PipelineState Schema" section.

event_id is generated ONCE in router.py and threaded through state unchanged;
no agent regenerates it and no truncation-recovery machinery is needed anymore
(that existed only to work around the Band LLM adapter mangling the UUID).
"""
import logging
from datetime import datetime, timezone

from graph import build_pipeline_graph
from db import update_event_status, update_pipeline_log

from shared.pipeline_state import PipelineState

logger = logging.getLogger("hazardmind.orchestrator")

STAGE_PROGRESS = {
    "satellite": 25,
    "hazard": 50,
    "impact": 75,
    "report": 100,
}


class OrchestratorAgent:
    """Drives the LangGraph pipeline for each disaster event."""

    def __init__(self) -> None:
        self._graph = None

    async def connect(self) -> None:
        """Compile the graph once. Kept async + named connect() so main.py's
        existing lifespan hook needs no change beyond dropping the Band health
        flag (no external service to connect to anymore)."""
        self._graph = build_pipeline_graph()
        logger.info("Pipeline graph compiled")

    @property
    def graph(self):
        if self._graph is None:
            self._graph = build_pipeline_graph()
        return self._graph

    async def start_pipeline(self, event_id: str, disaster_data: dict) -> None:
        """Move the event into satellite processing and run the graph.

        Runs the graph to completion (or failure) and writes the final
        disaster_events status. router.py invokes this as a background task,
        same shape as the pre-migration Band dispatch.
        """
        await update_event_status(event_id, status="processing", step="satellite")
        logger.info("Pipeline started for event_id=%s", event_id)

        initial_state: PipelineState = {
            "event_id": event_id,
            "location": disaster_data.get("location"),
            "disaster_type": disaster_data.get("disaster_type"),
            "magnitude": disaster_data.get("magnitude"),
            # Coverage-tolerance / search-budget overrides (fix/coverage-tolerance).
            "min_coverage_percent": disaster_data.get("min_coverage_percent"),
            "max_scenes": disaster_data.get("max_scenes"),
            "max_download_gb": disaster_data.get("max_download_gb"),
            "max_search_seconds": disaster_data.get("max_search_seconds"),
            "status": "satellite",
            "current_step": "satellite",
            "progress": 0,
            "errors": [],
            "anomalies": [],
            "confidence_scores": {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            final_state = await self.graph.ainvoke(initial_state)
        except Exception:  # noqa: BLE001 - a raised node error must still fail cleanly
            logger.exception("event_id=%s: pipeline crashed", event_id)
            await update_event_status(event_id, status="failed", step="failed")
            return

        log = {
            "errors": final_state.get("errors") or [],
            "anomalies": final_state.get("anomalies") or [],
            "confidence_scores": final_state.get("confidence_scores") or {},
        }
        try:
            await update_pipeline_log(event_id, log)
        except Exception:  # noqa: BLE001 - log persistence must not mask the result
            logger.exception("event_id=%s: failed to persist pipeline_log", event_id)

        if final_state.get("status") == "failed":
            errors = log["errors"]
            last_error = errors[-1] if errors else {}
            logger.error(
                "event_id=%s: pipeline failed at %s: %s",
                event_id,
                last_error.get("stage"),
                last_error.get("error"),
            )
            await update_event_status(event_id, status="failed", step="failed")
            return

        await update_event_status(event_id, status="complete", step="complete")
        logger.info("event_id=%s: pipeline complete", event_id)
