"""Startup backlog drain for all rooms an agent belongs to.

The orchestrator creates a fresh per-event Band room each run and adds the
pipeline agents to it. On (re)connect an agent auto-rejoins ALL of its old
rooms, and the SDK would otherwise replay each room's history — re-triggering
analysis on a previous, already-finished event. Draining every joined room's
/next backlog before the runtime starts ensures the agent only acts on messages
that arrive AFTER it connects. Everything here is best-effort: any failure is
logged and startup continues.
"""

import logging

logging.getLogger(__name__)
logger = logging.getLogger(__name__)


async def list_agent_rooms(api_key: str, rest_url: str) -> list[str]:
    import httpx

    rooms: list[str] = []
    try:
        url = f"{rest_url.rstrip('/')}/api/v1/agent/chats?page=1&page_size=100"
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers={"X-API-Key": api_key})
            resp.raise_for_status()
            body = resp.json()
        items = body.get("data") if isinstance(body, dict) else body
        for item in items or []:
            if isinstance(item, dict):
                rid = item.get("id") or item.get("chat_id")
                if rid:
                    rooms.append(str(rid))
    except Exception:  # noqa: BLE001 - listing is best-effort
        logger.warning("Could not list agent rooms for startup drain")
    return rooms


async def drain_room(agent_id: str, api_key: str, room_id: str) -> int:
    try:
        from band.platform.link import BandLink
    except Exception:  # noqa: BLE001
        logger.warning("Could not import BandLink; skipping drain for %s", room_id)
        return 0

    link = BandLink(agent_id=agent_id, api_key=api_key)
    drained = 0
    try:
        await link.connect()
        for msg in await link.get_stale_processing_messages(room_id):
            await link.mark_processed(room_id, msg.id)
            drained += 1
        while True:
            msg = await link.get_next_message(room_id)
            if msg is None:
                break
            await link.mark_processing(room_id, msg.id)
            await link.mark_processed(room_id, msg.id)
            drained += 1
    except Exception:  # noqa: BLE001 - drain is best-effort
        logger.warning("Drain error in room %s after %d messages", room_id, drained)
    finally:
        try:
            await link.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return drained


async def drain_all_rooms(agent_id: str, api_key: str, rest_url: str) -> None:
    rooms = await list_agent_rooms(api_key, rest_url)
    logger.info("Startup drain: %d joined room(s) to clear", len(rooms))
    total = 0
    for rid in rooms:
        total += await drain_room(agent_id, api_key, rid)
    logger.info("Startup drain complete: cleared %d message(s) across %d room(s)", total, len(rooms))
