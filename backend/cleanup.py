"""Periodic background cleanup for the HazardMind pipeline.

Runs every CLEANUP_INTERVAL_HOURS (default 12) and marks stuck events
('processing'/'received' for > 30 min) as 'stopped' so they stop holding a
concurrency slot and don't show as "running". The Band-backlog-drain half
(each agent's own /next queue) is gone with Band itself.
"""

import asyncio
import logging
import os

logger = logging.getLogger("hazardmind.cleanup")

CLEANUP_INTERVAL_HOURS = float(os.getenv("CLEANUP_INTERVAL_HOURS", "12"))


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
    """One cleanup pass — clear stuck events."""
    await _clear_stuck_events()


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
