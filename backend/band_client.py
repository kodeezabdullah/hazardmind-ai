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


async def get_room_messages(room_id: str, event_id: str) -> list[dict]:
    """Fetch a Band room's messages and return only those mentioning event_id.

    GET /api/v1/agent/chats/{room_id}/messages

    Every pipeline message carries the event_id in its content, so filtering by
    substring keeps the transcript scoped to a single job.
    """
    room = room_id or BAND_ROOM_ID
    if not room:
        raise RuntimeError("BAND_ROOM_ID is not configured")
    if not BAND_API_KEY:
        raise RuntimeError("BAND_API_KEY is not configured")

    url = f"{BAND_REST_URL}/api/v1/agent/chats/{room}/messages"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers={"X-API-Key": BAND_API_KEY})
        resp.raise_for_status()
        data = resp.json()

    # Band may wrap the list under "messages" (or return a bare list).
    messages = data.get("messages", data) if isinstance(data, dict) else data
    if not isinstance(messages, list):
        return []

    return [
        msg
        for msg in messages
        if event_id in str(msg.get("content", ""))
    ]


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
