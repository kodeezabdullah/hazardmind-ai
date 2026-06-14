import os
from typing import Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("NEON_DATABASE_URL")

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("NEON_DATABASE_URL is not configured")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def create_disaster_event(
    event_id: str,
    location: str,
    disaster_type: str,
    magnitude: Optional[float],
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO disaster_events
                (event_id, location, disaster_type, magnitude, status, step, progress)
            VALUES ($1, $2, $3, $4, 'processing', 'received', 0)
            """,
            event_id,
            location,
            disaster_type,
            magnitude,
        )


async def update_event_status(event_id: str, status: str, step: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE disaster_events
            SET status = $2, step = $3, updated_at = NOW()
            WHERE event_id = $1
            """,
            event_id,
            status,
            step,
        )


async def get_event_status(event_id: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM disaster_events WHERE event_id = $1",
            event_id,
        )
        return dict(row) if row else None


async def get_event_results(event_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        event = await conn.fetchrow(
            "SELECT * FROM disaster_events WHERE event_id = $1", event_id
        )
        satellite = await conn.fetchrow(
            "SELECT * FROM satellite_results WHERE event_id = $1", event_id
        )
        hazard = await conn.fetchrow(
            "SELECT * FROM hazard_zones WHERE event_id = $1", event_id
        )
        impact = await conn.fetchrow(
            "SELECT * FROM impact_data WHERE event_id = $1", event_id
        )
        report = await conn.fetchrow(
            "SELECT * FROM final_reports WHERE event_id = $1", event_id
        )
        return {
            "event": dict(event) if event else None,
            "satellite": dict(satellite) if satellite else None,
            "hazard": dict(hazard) if hazard else None,
            "impact": dict(impact) if impact else None,
            "report": dict(report) if report else None,
        }
