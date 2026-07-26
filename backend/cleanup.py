"""Periodic background cleanup for the HazardMind pipeline.

Runs every CLEANUP_INTERVAL_HOURS (default 12) and:
  1. Marks stuck events ('processing'/'received' for > 30 min) as 'stopped' so
     they stop holding a concurrency slot and don't show as "running".
  2. Drains the Band /next backlog for every room each agent belongs to, using
     each agent's own BAND key, so a (re)connecting agent never replays an old,
     already-finished event's messages.

All keys are read from the environment; any agent whose key is unset is skipped.
Everything is best-effort — a failure is logged and the loop continues.
"""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger("hazardmind.cleanup")

REST_URL = os.getenv("THENVOI_REST_URL", "https://app.band.ai").rstrip("/")
CLEANUP_INTERVAL_HOURS = float(os.getenv("CLEANUP_INTERVAL_HOURS", "12"))

# Each agent drains its OWN rooms with its OWN key. Backend holds them all as
# secrets so a single scheduler can clean the whole fleet.
_AGENT_KEY_VARS = {
    "orchestrator": "BAND_API_KEY",
    "satellite": "SATELLITE_BAND_API_KEY",
    "hazard": "HAZARD_BAND_API_KEY",
    "impact": "IMPACT_BAND_API_KEY",
    "report": "REPORT_BAND_API_KEY",
}


async def _drain_agent_rooms(name: str, api_key: str) -> int:
    """Drain the /next backlog for every room this agent belongs to."""
    drained = 0
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{REST_URL}/api/v1/agent/chats?page=1&page_size=100",
                headers={"X-API-Key": api_key},
            )
            data = resp.json().get("data") if isinstance(resp.json(), dict) else resp.json()
            rooms = [str(it["id"]) for it in (data or []) if isinstance(it, dict) and it.get("id")]

            for rid in rooms:
                for _ in range(100):  # cap per room
                    nr = await client.get(
                        f"{REST_URL}/api/v1/agent/chats/{rid}/messages/next",
                        headers={"X-API-Key": api_key},
                    )
                    if nr.status_code != 200:
                        break
                    msg = nr.json()
                    mid = msg.get("id") or (msg.get("data") or {}).get("id")
                    if not mid:
                        break
                    await client.post(
                        f"{REST_URL}/api/v1/agent/chats/{rid}/messages/{mid}/processing",
                        headers={"X-API-Key": api_key},
                    )
                    drained += 1
        logger.info("cleanup: %s drained %d backlog messages", name, drained)
    except Exception:  # noqa: BLE001
        logger.exception("cleanup: drain failed for %s", name)
    return drained


async def _clear_stuck_events() -> int:
    """Mark events stuck in processing/received (> 30 min) as stopped."""
    try:
        from db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            n = await conn.execute(
                """
                UPDATE disaster_events
                SET status = 'stopped'
                WHERE status IN ('processing', 'received')
                  AND updated_at < now() - interval '30 minutes'
                """
            )
        logger.info("cleanup: cleared stuck events (%s)", n)
        return 0
    except Exception:  # noqa: BLE001
        logger.exception("cleanup: clearing stuck events failed")
        return 0


async def run_cleanup_once() -> None:
    """One cleanup pass — clear stuck events, then drain every agent's backlog."""
    await _clear_stuck_events()
    for name, key_var in _AGENT_KEY_VARS.items():
        key = os.getenv(key_var)
        if key:
            await _drain_agent_rooms(name, key)


async def cleanup_loop() -> None:
    """Background loop: run a cleanup pass every CLEANUP_INTERVAL_HOURS."""
    interval_seconds = CLEANUP_INTERVAL_HOURS * 3600
    while True:
        await asyncio.sleep(interval_seconds)
        logger.info("cleanup: running scheduled pass")
        try:
            await run_cleanup_once()
        except Exception:  # noqa: BLE001
            logger.exception("cleanup: scheduled pass failed")
