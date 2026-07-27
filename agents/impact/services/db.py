"""Neon DB writer — impact_data table (single consolidated table).

Schema is created on first run if it doesn't exist.
All writes are done with ON CONFLICT (event_id) DO UPDATE so
re-runs for the same event_id are idempotent.
"""

import json
import logging
import os

import asyncpg

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS impact_data (
    id                       SERIAL PRIMARY KEY,
    event_id                 TEXT UNIQUE NOT NULL,
    total_affected           INTEGER,
    high_risk_people         INTEGER,
    medium_risk_people       INTEGER,
    hospitals_at_risk        INTEGER,
    schools_at_risk          INTEGER,
    roads_blocked            INTEGER,
    bridges_at_risk          INTEGER,
    vulnerability_score      TEXT,
    evacuation_routes        JSONB,
    estimated_evacuation_time TEXT,
    overall_confidence       DOUBLE PRECISION,
    created_at               TIMESTAMPTZ DEFAULT NOW(),
    updated_at               TIMESTAMPTZ DEFAULT NOW()
);
"""

# overall_confidence: confirmed present on live Neon (added out-of-band, no
# migration file in this repo recorded it) via a direct information_schema
# query on 2026-07-28 — this DDL now matches reality. Report's
# agents/report/db_client.py:_fetch_impact_data SELECTs this column; prior to
# this fix nothing in this file wrote it, so every row's overall_confidence
# was silently NULL forever (the SELECT succeeded — this was a silent data
# loss, not the schema-mismatch hard-failure the column's absence would have
# caused). See SYSTEM_ANALYSIS.md Section B.6/H#2.
ALTER_DDL = """
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS overall_confidence DOUBLE PRECISION;
"""


async def write_impact_data(
    event_id: str,
    pop: dict,
    infra: dict,
    vuln: dict,
    overall_confidence: float | None = None,
) -> None:
    """Write consolidated impact results to the impact_data table."""
    dsn = os.environ.get("NEON_DATABASE_URL", "")
    if not dsn:
        logger.warning("[db] NEON_DATABASE_URL not set — skipping write")
        return

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(DDL)
        await conn.execute(ALTER_DDL)

        pop_count = int(pop.get("population_affected", 0) or 0)
        evac_time = (
            infra.get("estimated_evacuation_time")
            or vuln.get("estimated_evacuation_time", "unknown")
        )

        await conn.execute(
            """
            INSERT INTO impact_data (
                event_id,
                total_affected,
                high_risk_people,
                medium_risk_people,
                hospitals_at_risk,
                schools_at_risk,
                roads_blocked,
                bridges_at_risk,
                vulnerability_score,
                evacuation_routes,
                estimated_evacuation_time,
                overall_confidence,
                updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,NOW())
            ON CONFLICT (event_id) DO UPDATE SET
                total_affected           = EXCLUDED.total_affected,
                high_risk_people         = EXCLUDED.high_risk_people,
                medium_risk_people       = EXCLUDED.medium_risk_people,
                hospitals_at_risk        = EXCLUDED.hospitals_at_risk,
                schools_at_risk          = EXCLUDED.schools_at_risk,
                roads_blocked            = EXCLUDED.roads_blocked,
                bridges_at_risk          = EXCLUDED.bridges_at_risk,
                vulnerability_score      = EXCLUDED.vulnerability_score,
                evacuation_routes        = EXCLUDED.evacuation_routes,
                estimated_evacuation_time = EXCLUDED.estimated_evacuation_time,
                overall_confidence       = EXCLUDED.overall_confidence,
                updated_at               = NOW()
            """,
            event_id,
            pop_count,
            int(pop.get("high_risk_people", int(pop_count * 0.2)) or int(pop_count * 0.2)),
            int(pop.get("medium_risk_people", int(pop_count * 0.5)) or int(pop_count * 0.5)),
            int(infra.get("hospitals_at_risk", 0) or 0),
            int(infra.get("schools_at_risk", 0) or 0),
            int(round(float(infra.get("roads_blocked_km", 0) or 0))),
            int(infra.get("bridges_at_risk", 0) or 0),
            str(vuln.get("vulnerability_score", 0)),
            json.dumps(vuln.get("priority_zones", [])),
            evac_time,
            float(overall_confidence) if overall_confidence is not None else None,
        )
        logger.info("[db] impact_data upserted for event_id=%s", event_id)

    finally:
        await conn.close()


# Legacy alias — called by main.py FastAPI path
async def write_results(
    event_id: str,
    hazard_data: dict,
    population_result: dict,
    infrastructure_result: dict,
    vulnerability_result: dict,
) -> None:
    await write_impact_data(
        event_id,
        population_result,
        infrastructure_result,
        vulnerability_result,
    )
