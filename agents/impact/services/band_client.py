"""Band network client — send/receive helpers.

In production: uses Band SDK (band-sdk) via WebSocket agent (agent.py).
In mock mode (USE_MOCK_BAND=true): logs instead of sending.

send_to_band_room()   — send formatted message to Band room
send_anomaly_to_band() — send anomaly/critical alert to Band room
receive_hazard_data()  — load hazard data from mock file or Band
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

MOCK_FILE = Path(__file__).parent.parent / "mock_hazard_output.json"


def _use_mock() -> bool:
    return os.environ.get("USE_MOCK_BAND", "true").lower() == "true"


async def send_to_band_room(message: str) -> None:
    """Send a text message to the Band room."""
    if _use_mock():
        logger.info("[band] MOCK send_to_band_room:\n%s", message[:400])
        return

    try:
        import httpx
        rest_url = os.environ.get("THENVOI_REST_URL", "https://app.band.ai/").rstrip("/")
        api_key  = os.environ.get("BAND_API_KEY", "")
        agent_id = os.environ.get("BAND_AGENT_ID", "")

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{rest_url}/api/v1/messages",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"agent_id": agent_id, "content": message},
            )
            if resp.status_code not in (200, 201):
                logger.warning(
                    "[band] send_to_band_room HTTP %d: %s",
                    resp.status_code, resp.text[:200],
                )
            else:
                logger.info("[band] Message sent successfully")
    except Exception as exc:
        logger.error("[band] send_to_band_room failed: %s", exc)


async def send_anomaly_to_band(message: str) -> None:
    """Send a critical anomaly alert to the Band room."""
    if _use_mock():
        logger.warning("[band] MOCK anomaly:\n%s", message)
        return
    await send_to_band_room(message)


async def receive_hazard_data() -> dict:
    """Load hazard data from mock file or Band SDK."""
    if _use_mock():
        logger.info("USE_MOCK_BAND=true — loading hazard data from %s", MOCK_FILE)
        with open(MOCK_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)

    try:
        from band_sdk import BandClient  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "band-sdk not installed. Run: pip install band-sdk[anthropic]"
        ) from exc

    logger.info("Connecting to Band — waiting for hazard agent message (timeout=300s)")
    client = BandClient(api_key=os.environ.get("BAND_API_KEY", ""))
    message = await client.receive(from_agent="hazard", timeout=300)
    logger.info("Received hazard message from Band")
    return message


async def send_impact_result(result: dict) -> None:
    """Send impact assessment result to Band (legacy FastAPI path)."""
    pop       = result.get("population_affected", result.get("total_affected", 0))
    hospitals = result.get("hospitals_at_risk", 0)
    score     = result.get("vulnerability_score", 0)
    event_id  = result.get("event_id", "unknown")

    natural = (
        f"@hazardmind-orchestrator\n"
        f"Impact assessment complete for event {event_id}.\n"
        f"{int(pop):,} population in affected zones.\n"
        f"{hospitals} hospitals at risk — "
        + ("CRITICAL: Immediate NDMA notification recommended." if hospitals > 10 else "monitoring required.")
        + f"\nVulnerability score: {score}/10\nHanding off to report agent."
    )

    json_payload = {
        "event_id": event_id,
        "agent": "hazardmind-impact",
        "status": "complete",
        "step": "impact",
        "data": result,
    }

    message = f"{natural}\n\n{json.dumps(json_payload, indent=2)}"
    await send_to_band_room(message)
