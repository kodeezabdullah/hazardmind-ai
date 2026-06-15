import json
import os
from typing import Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("NEON_DATABASE_URL")

POOL_MIN_SIZE = 2
POOL_MAX_SIZE = 10

_pool: Optional[asyncpg.Pool] = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Decode jsonb columns (used by get_event_results) into dicts/lists.
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("NEON_DATABASE_URL is not configured")
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            ssl="require",
            init=_init_connection,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def ping() -> bool:
    """Return True if the database answers a trivial query."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 - health check must not raise
        return False


async def create_disaster_event(
    event_id: str,
    location: str,
    disaster_type: str,
    magnitude: Optional[float],
) -> None:
    """Insert a new event. Status starts as 'received'."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO disaster_events
                (event_id, location, disaster_type, magnitude,
                 status, step, progress)
            VALUES ($1, $2, $3, $4, 'received', 'received', 0)
            """,
            event_id,
            location,
            disaster_type,
            magnitude,
        )


async def update_event_status(event_id: str, status: str, step: str) -> None:
    """Update an event's status/step and bump updated_at."""
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


async def insert_satellite_result(event_id: str, data: dict) -> None:
    """Write the satellite agent's result row from its completion payload.

    The satellite agent reports its result over Band; the orchestrator parses
    the completion signal (a dict with satellite_type / cloud_cover / scene_id /
    the four artifact URLs / affected_area_km2 / damage_percent / total_zones /
    bounds / bbox / risk_cities) and persists it here so GET /results can join
    it. Columns mirror the satellite_results table; jsonb columns are passed as
    JSON strings. Idempotent per event: an existing row is replaced so a re-run
    does not accumulate duplicates. Missing fields are written as NULL.
    """
    data = data or {}

    def _f(key):
        value = data.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _i(key):
        value = data.get(key)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    # jsonb columns: pass the raw list/dict. The connection's jsonb codec
    # (encoder=json.dumps) serializes it — do NOT pre-dump or it double-encodes
    # and the value comes back as a JSON string instead of an object.
    def _j(key):
        return data.get(key)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM satellite_results WHERE event_id = $1",
                event_id,
            )
            await conn.execute(
                """
                INSERT INTO satellite_results
                    (event_id, satellite_type, cloud_cover, scene_id,
                     true_color_url, index_url, classification_url, geojson_url,
                     affected_area_km2, damage_percent, total_zones,
                     bounds, bbox, risk_cities)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12, $13, $14)
                """,
                event_id,
                data.get("satellite_type"),
                _f("cloud_cover"),
                data.get("scene_id"),
                data.get("true_color_url"),
                data.get("index_url"),
                data.get("classification_url"),
                data.get("geojson_url"),
                _f("affected_area_km2"),
                _f("damage_percent"),
                _i("total_zones"),
                _j("bounds"),
                _j("bbox"),
                _j("risk_cities"),
            )


async def get_event_status(event_id: str) -> Optional[dict]:
    """Return the event's status and step (plus progress/timestamps)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT event_id, status, step, progress,
                   created_at, updated_at
            FROM disaster_events
            WHERE event_id = $1
            """,
            event_id,
        )
        return dict(row) if row else None


async def get_event_results(event_id: str) -> Optional[dict]:
    """Return the complete results by joining all 5 pipeline tables."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                e.event_id,
                e.disaster_type,
                e.location,
                e.magnitude,
                e.status,
                e.step,
                e.progress,
                e.created_at,
                e.updated_at,
                to_jsonb(s) - 'event_id' AS satellite,
                to_jsonb(h) - 'event_id' AS hazard,
                to_jsonb(i) - 'event_id' AS impact,
                to_jsonb(r) - 'event_id' AS report
            FROM disaster_events e
            LEFT JOIN satellite_results s ON s.event_id = e.event_id
            LEFT JOIN hazard_zones h ON h.event_id = e.event_id
            LEFT JOIN impact_data i ON i.event_id = e.event_id
            LEFT JOIN final_reports r ON r.event_id = e.event_id
            WHERE e.event_id = $1
            """,
            event_id,
        )
        return dict(row) if row else None
