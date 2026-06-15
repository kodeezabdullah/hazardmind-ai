"""Orchestrator agent.

Drives the 4-agent HazardMind pipeline end to end:

    satellite -> hazard -> impact -> report -> complete

It connects to the Band room with the SDK's Anthropic adapter (agent_id /
api_key come from agent_config.yaml), kicks the pipeline off by mentioning the
satellite agent, and then watches the room transcript to advance the DB status
as each agent reports completion.

Pipeline messages all carry the event_id, and each agent announces it is done
with a line like "satellite complete" / "hazard complete" / ... — that marker
is what monitor_progress() keys off of to step the database forward.
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

from band_client import (
    BAND_ROOM_ID,
    SATELLITE_AGENT_ID,
    get_room_messages,
    notify_satellite,
    send_band_message,
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

# Per-agent completion -> the (status, step) the event moves INTO next.
# When "satellite complete" is seen we hand off to hazard, and so on.
PIPELINE_TRANSITIONS = [
    ("satellite", ("processing", "hazard")),
    ("hazard", ("processing", "impact")),
    ("impact", ("processing", "report")),
    ("report", ("complete", "complete")),
]


class OrchestratorAgent:
    """Coordinates the disaster-analysis pipeline over a Band room."""

    def __init__(self) -> None:
        self.agent = None
        self.connected = False

    async def connect(self) -> None:
        """Connect to the Band room using the Anthropic adapter.

        agent_id and api_key are loaded from agent_config.yaml under the
        ``orchestrator_agent`` key.
        """
        from band import Agent
        from band.adapters import AnthropicAdapter

        adapter = AnthropicAdapter(model=ANTHROPIC_MODEL)
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
        await update_event_status(event_id, status="processing", step="satellite")

        await notify_satellite(
            event_id=event_id,
            location=disaster_data.get("location"),
            disaster_type=disaster_data.get("disaster_type"),
            magnitude=disaster_data.get("magnitude"),
        )

        logger.info("Pipeline started for event_id=%s", event_id)

    async def monitor_progress(self, event_id: str) -> str:
        """Watch the Band room and advance the DB as each agent completes.

        Steps the event through hazard -> impact -> report -> complete as the
        matching "<agent> complete" markers appear in the transcript. Returns
        the final step reached ("complete", or the last step on timeout).
        """
        stage = 0
        elapsed = 0

        while stage < len(PIPELINE_TRANSITIONS):
            agent, (status, step) = PIPELINE_TRANSITIONS[stage]

            if await self._agent_completed(event_id, agent):
                await update_event_status(event_id, status=status, step=step)
                logger.info(
                    "event_id=%s: %s complete -> %s/%s",
                    event_id,
                    agent,
                    status,
                    step,
                )
                stage += 1
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
                return step

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

        logger.info("event_id=%s: pipeline complete", event_id)
        return "complete"

    async def handle_failure(self, event_id: str, agent: str, error: str) -> None:
        """Mark the event failed, log it, and alert the Band room."""
        await update_event_status(event_id, status="failed", step="failed")
        logger.error("event_id=%s: %s failed: %s", event_id, agent, error)

        try:
            from band_client import SATELLITE_HANDLE

            mentions = [SATELLITE_AGENT_ID] if SATELLITE_AGENT_ID else []
            await send_band_message(
                content=(
                    f"{SATELLITE_HANDLE}\n"
                    "Pipeline failure.\n"
                    f"event_id: {event_id}\n"
                    f"agent: {agent}\n"
                    f"error: {error}"
                ),
                mention_ids=mentions,
                room_id=BAND_ROOM_ID,
            )
        except Exception:  # noqa: BLE001 - alerting must never mask the failure
            logger.exception(
                "event_id=%s: failed to post failure alert to Band", event_id
            )

    async def _agent_completed(self, event_id: str, agent: str) -> bool:
        """True if the room transcript shows ``<agent> complete`` for this event."""
        messages = await get_room_messages(BAND_ROOM_ID, event_id)
        marker = f"{agent} complete".lower()
        return any(marker in str(msg.get("content", "")).lower() for msg in messages)
