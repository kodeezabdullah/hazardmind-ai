import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

BAND_REST_URL = os.getenv("THENVOI_REST_URL", "https://app.band.ai/").rstrip("/")
BAND_API_KEY = os.getenv("BAND_API_KEY")
BAND_ROOM_ID = os.getenv("BAND_ROOM_ID")
SATELLITE_AGENT_ID = os.getenv("SATELLITE_AGENT_ID")

SATELLITE_HANDLE = "@abdullah.gis.services/hazardmind-satellite"


async def send_band_message(
    content: str,
    mention_ids: list[str],
    room_id: Optional[str] = None,
) -> dict:
    """Post a message (with @mentions) to a Band room via the REST API.

    Band expects: {"message": {"content": str, "mentions": [{"id": uuid}, ...]}}
    The mentioned handles must also appear literally in `content`.
    """
    room = room_id or BAND_ROOM_ID
    if not room:
        raise RuntimeError("BAND_ROOM_ID is not configured")
    if not BAND_API_KEY:
        raise RuntimeError("BAND_API_KEY is not configured")
    if not mention_ids:
        raise RuntimeError("Band messages require at least one mention")

    url = f"{BAND_REST_URL}/api/v1/agent/chats/{room}/messages"
    payload = {
        "message": {
            "content": content,
            "mentions": [{"id": agent_id} for agent_id in mention_ids],
        }
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            headers={"X-API-Key": BAND_API_KEY},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def notify_satellite(
    event_id: str,
    location: str,
    disaster_type: str,
    magnitude: Optional[float],
) -> dict:
    """Send the initial pipeline message to the satellite agent."""
    if not SATELLITE_AGENT_ID:
        raise RuntimeError("SATELLITE_AGENT_ID is not configured")

    content = (
        f"{SATELLITE_HANDLE}\n"
        "New disaster event received.\n"
        f"event_id: {event_id}\n"
        f"location: {location}\n"
        f"disaster_type: {disaster_type}\n"
        f"magnitude: {magnitude}\n"
        "Please start satellite analysis."
    )
    return await send_band_message(content, mention_ids=[SATELLITE_AGENT_ID])
