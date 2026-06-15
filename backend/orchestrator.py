"""Orchestrator agent.

Drives the 4-agent HazardMind pipeline end to end:

    satellite -> hazard -> impact -> report -> complete

It connects to the Band room with the SDK's Anthropic adapter (agent_id /
api_key come from agent_config.yaml), kicks the pipeline off by mentioning the
satellite agent, and then watches the room transcript to advance the DB status
as each agent reports completion.

Messaging uses Band natively:
  - send_thought()       -> agent reasoning, visible to judges
  - send_task_update()   -> task progress (started/processing/complete/failed)
  - send_text_message()  -> human-readable agent-to-agent handoffs
  - send_event("error")  -> structured failure payload

Pipeline messages all carry the event_id, and each agent announces it is done
with a line like "satellite complete" / "hazard complete" / ... — that marker
is what monitor_progress() keys off of to step the database forward.
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

from band_client import (
    HAZARD_HANDLE,
    IMPACT_HANDLE,
    REPORT_HANDLE,
    handoff_message,
    inbound_store,
    notify_satellite,
    send_event,
    send_task_update,
    send_text_message,
    send_thought,
)
from db import update_event_status

load_dotenv()

logger = logging.getLogger("hazardmind.orchestrator")

BAND_WS_URL = os.getenv("THENVOI_WS_URL", "wss://app.band.ai/api/v1/socket/websocket")
BAND_REST_URL = os.getenv("THENVOI_REST_URL", "https://app.band.ai/").rstrip("/")
ANTHROPIC_MODEL = os.getenv("ORCHESTRATOR_MODEL", "claude-sonnet-4-5-20250929")

ORCHESTRATOR_AGENT_KEY = "orchestrator_agent"

# How often monitor_progress() re-reads the Band room while waiting.
POLL_INTERVAL_SECONDS = 5
# Give up after this long with no further progress (safety net for the demo).
MONITOR_TIMEOUT_SECONDS = 600

# Pipeline handoff plan, keyed off each agent's "<agent> complete" marker.
#   marker_agent      -> the agent whose completion triggers this transition
#   status, step      -> the (status, step) the event moves INTO
#   next_handle       -> Band handle to hand the job to (None at the end)
#   thought           -> reasoning emitted when the transition fires
#   task_name         -> task whose completion we announce
PIPELINE_TRANSITIONS = [
    {
        "agent": "satellite",
        "status": "processing",
        "step": "hazard",
        "next_handle": HAZARD_HANDLE,
        "task_name": "Satellite",
        "thought": "Satellite analysis complete. Initiating hazard detection.",
    },
    {
        "agent": "hazard",
        "status": "processing",
        "step": "impact",
        "next_handle": IMPACT_HANDLE,
        "task_name": "Hazard",
        "thought": "Hazard zones confirmed. Calculating population impact.",
    },
    {
        "agent": "impact",
        "status": "processing",
        "step": "report",
        "next_handle": REPORT_HANDLE,
        "task_name": "Impact",
        "thought": "Impact assessment done. Generating executive report.",
    },
    {
        "agent": "report",
        "status": "complete",
        "step": "complete",
        "next_handle": None,
        "task_name": "Report",
        "thought": "All agents complete. Pipeline successful.",
    },
]


def _make_recording_adapter():
    """Build an AnthropicAdapter that records every inbound message.

    Band delivers room messages to the connected SDK agent over its WebSocket
    execution loop (the REST GET /messages history is empty for this agent), so
    we capture each inbound message into the shared inbound_store. That store is
    what monitor_progress() and GET /band-log read from.
    """
    from band.adapters import AnthropicAdapter

    class RecordingAnthropicAdapter(AnthropicAdapter):
        async def on_event(self, inp) -> None:  # type: ignore[override]
            try:
                msg = inp.msg
                inbound_store.add(
                    {
                        "id": getattr(msg, "id", None),
                        "content": getattr(msg, "content", ""),
                        "type": getattr(msg, "message_type", "text"),
                        "sender": {
                            "id": getattr(msg, "sender_id", None),
                            "name": getattr(msg, "sender_name", None),
                        },
                        "created_at": str(getattr(msg, "created_at", "") or ""),
                    }
                )
            except Exception:  # noqa: BLE001 - recording must not break delivery
                logger.exception("Failed to record inbound Band message")
            await super().on_event(inp)

    return RecordingAnthropicAdapter(model=ANTHROPIC_MODEL)


class OrchestratorAgent:
    """Coordinates the disaster-analysis pipeline over a Band room."""

    def __init__(self) -> None:
        self.agent = None
        self.connected = False

    async def connect(self) -> None:
        """Connect to the Band room using a recording Anthropic adapter.

        agent_id and api_key are loaded from agent_config.yaml under the
        ``orchestrator_agent`` key.
        """
        from band import Agent

        adapter = _make_recording_adapter()
        self.agent = Agent.from_config(
            ORCHESTRATOR_AGENT_KEY,
            adapter=adapter,
            ws_url=BAND_WS_URL,
            rest_url=BAND_REST_URL,
        )
        await self.agent.start()
        self.connected = True
        logger.info("Orchestrator connected")

    async def start_pipeline(self, event_id: str, disaster_data: dict) -> None:
        """Move the event into satellite processing and hand it to Agent 1."""
        disaster_type = disaster_data.get("disaster_type")
        location = disaster_data.get("location")
        magnitude = disaster_data.get("magnitude")

        await send_thought(
            f"New disaster event received: {disaster_type} in {location}. "
            "Initializing 4-agent response pipeline."
        )
        await send_task_update("Pipeline", "started")

        await update_event_status(event_id, status="processing", step="satellite")
        await send_task_update("Satellite", "processing")

        await notify_satellite(
            event_id=event_id,
            location=location,
            disaster_type=disaster_type,
            magnitude=magnitude,
        )

        logger.info("Pipeline started for event_id=%s", event_id)

    async def monitor_progress(self, event_id: str) -> str:
        """Watch the Band room and advance the DB as each agent completes.

        Steps the event through hazard -> impact -> report -> complete as the
        matching "<agent> complete" markers appear in the transcript. Each
        transition emits a thought + task update and hands off to the next
        agent via a text message. Returns the final step reached.
        """
        stage = 0
        elapsed = 0

        while stage < len(PIPELINE_TRANSITIONS):
            t = PIPELINE_TRANSITIONS[stage]
            agent = t["agent"]

            if await self._agent_completed(event_id, agent):
                await self._advance(event_id, t)
                stage += 1
                elapsed = 0
                continue

            if elapsed >= MONITOR_TIMEOUT_SECONDS:
                logger.warning(
                    "event_id=%s: timed out waiting for %s to complete",
                    event_id,
                    agent,
                )
                await self.handle_failure(
                    event_id,
                    agent,
                    f"timed out after {MONITOR_TIMEOUT_SECONDS}s waiting for {agent}",
                )
                return t["step"]

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

        logger.info("event_id=%s: pipeline complete", event_id)
        return "complete"

    async def _advance(self, event_id: str, transition: dict) -> None:
        """Apply one pipeline transition: thought, task update, DB, handoff."""
        await send_thought(transition["thought"])
        await send_task_update(transition["task_name"], "complete")

        await update_event_status(
            event_id, status=transition["status"], step=transition["step"]
        )
        logger.info(
            "event_id=%s: %s complete -> %s/%s",
            event_id,
            transition["agent"],
            transition["status"],
            transition["step"],
        )

        next_handle = transition["next_handle"]
        if next_handle is not None:
            await send_task_update(transition["step"].capitalize(), "processing")
            content, mentions = handoff_message(next_handle, event_id)
            await send_text_message(content, mentions=mentions)

    async def handle_failure(self, event_id: str, agent: str, error: str) -> None:
        """Mark the event failed, log it, and alert the Band room."""
        await update_event_status(event_id, status="failed", step="failed")
        logger.error("event_id=%s: %s failed: %s", event_id, agent, error)

        try:
            await send_event(
                "error",
                "Pipeline Failed",
                {"agent": agent, "error": error, "event_id": event_id},
            )
            await send_task_update("Pipeline", "failed", result=error)
            await send_text_message(
                "⚠️ PIPELINE FAILED\n"
                f"Agent: {agent}\n"
                f"Error: {error}\n"
                f"event_id: {event_id}"
            )
        except Exception:  # noqa: BLE001 - alerting must never mask the failure
            logger.exception(
                "event_id=%s: failed to post failure alert to Band", event_id
            )

    async def _agent_completed(self, event_id: str, agent: str) -> bool:
        """True if a recorded inbound message reports ``<agent>`` complete.

        Reads the inbound_store (populated by the recording adapter), since
        Band's REST history is empty for this agent. Detects both the
        structured completion signal ({"step": "<agent>", "status": "complete"})
        and the plain-text "<agent> complete" marker.
        """
        marker = f"{agent} complete".lower()
        for parsed in inbound_store.for_event(event_id):
            content = str(parsed.get("content", "")).lower()
            data = parsed.get("data") or {}
            if (
                str(data.get("step", "")).lower() == agent
                and str(data.get("status", "")).lower() == "complete"
            ):
                return True
            if marker in content:
                return True
        return False
