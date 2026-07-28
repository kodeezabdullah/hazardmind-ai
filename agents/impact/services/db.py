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
    roads_blocked_km         DOUBLE PRECISION,
    bridges_at_risk          INTEGER,
    vulnerability_score      TEXT,
    evacuation_routes        JSONB,
    estimated_evacuation_time TEXT,
    overall_confidence       DOUBLE PRECISION,
    created_at               TIMESTAMPTZ DEFAULT NOW(),
    updated_at               TIMESTAMPTZ DEFAULT NOW(),
    diagnostics              JSONB
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
#
# roads_blocked_km: H#14 — roads_blocked stores kilometres under a
# count-sounding name with no unit suffix, AND the DB previously rounded to
# nearest integer while the in-memory payload rounded to 1 decimal (agent.py),
# so the same run could show two different values depending on which surface
# you read. Per root CLAUDE.md's DB-contract rule (never break write
# contracts without updating every reader in the same change), this ADDS a
# correctly-named, correctly-typed column rather than renaming roads_blocked
# in place — roads_blocked is kept, still populated (deprecated, not
# removed), so no existing reader breaks. New readers should prefer
# roads_blocked_km.
#
# diagnostics (durable-evidence-trail, feat/durable-evidence-trail,
# 2026-07-28): the three task-level confidences (population/infrastructure/
# vulnerability) that each task already computes in its own LLM response but
# were previously discarded before reaching write_impact_data — only the
# hazard-derived overall_confidence (passed in as a parameter) was ever
# persisted. See CLAUDE.md's migration-discipline rule: this column was
# added via shared/db/migrations/0002_durable_evidence_trail.sql, NOT via
# this inline ALTER_DDL — the ALTER_DDL below is retained only for the two
# pre-existing columns it already guarded (this file predates the migration
# tooling); do not add new columns here going forward.
ALTER_DDL = """
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS overall_confidence DOUBLE PRECISION;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS roads_blocked_km DOUBLE PRECISION;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS diagnostics JSONB;
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

        roads_blocked_km = round(float(infra.get("roads_blocked_km", 0) or 0), 1)

        # diagnostics (durable-evidence-trail, feat/durable-evidence-trail):
        # the three task-level confidences (population/infrastructure/
        # vulnerability) that impact currently computes and discards, plus
        # the real evacuation_routes output (also in its own column —
        # carried here too so a single diagnostics read shows the full
        # provenance alongside the task confidences).
        diagnostics = {
            "population_confidence": pop.get("confidence"),
            "infrastructure_confidence": infra.get("confidence"),
            "vulnerability_confidence": vuln.get("confidence"),
            "evacuation_routes": vuln.get("evacuation_routes", []),
        }

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
                roads_blocked_km,
                bridges_at_risk,
                vulnerability_score,
                evacuation_routes,
                estimated_evacuation_time,
                overall_confidence,
                diagnostics,
                updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,NOW())
            ON CONFLICT (event_id) DO UPDATE SET
                total_affected           = EXCLUDED.total_affected,
                high_risk_people         = EXCLUDED.high_risk_people,
                medium_risk_people       = EXCLUDED.medium_risk_people,
                hospitals_at_risk        = EXCLUDED.hospitals_at_risk,
                schools_at_risk          = EXCLUDED.schools_at_risk,
                roads_blocked            = EXCLUDED.roads_blocked,
                roads_blocked_km         = EXCLUDED.roads_blocked_km,
                bridges_at_risk          = EXCLUDED.bridges_at_risk,
                vulnerability_score      = EXCLUDED.vulnerability_score,
                evacuation_routes        = EXCLUDED.evacuation_routes,
                estimated_evacuation_time = EXCLUDED.estimated_evacuation_time,
                overall_confidence       = EXCLUDED.overall_confidence,
                diagnostics              = EXCLUDED.diagnostics,
                updated_at               = NOW()
            """,
            event_id,
            pop_count,
            int(pop.get("high_risk_people", int(pop_count * 0.2)) or int(pop_count * 0.2)),
            int(pop.get("medium_risk_people", int(pop_count * 0.5)) or int(pop_count * 0.5)),
            int(infra.get("hospitals_at_risk", 0) or 0),
            int(infra.get("schools_at_risk", 0) or 0),
            # roads_blocked (legacy, INTEGER, deprecated but still populated
            # for back-compat with existing readers) now rounds the SAME
            # underlying value as roads_blocked_km/the in-memory payload
            # (round-then-int, not a separate round(0-decimals) call) so the
            # two no longer silently disagree within one run (H#14).
            int(roads_blocked_km),
            roads_blocked_km,
            int(infra.get("bridges_at_risk", 0) or 0),
            str(vuln.get("vulnerability_score", 0)),
            # H#14/H#12: this column is named evacuation_routes and report's
            # reader (_evacuation_routes/db_client.py) expects real route data
            # (name/distance_km/status/geojson) -- it was previously fed
            # priority_zones (named place data: name/lat/lon/priority/reason),
            # a genuine content/label mismatch. vulnerability.py's LLM output
            # already computes both fields; this now persists the one that
            # actually matches the column name and the reader's expectations.
            json.dumps(vuln.get("evacuation_routes", [])),
            evac_time,
            float(overall_confidence) if overall_confidence is not None else None,
            json.dumps(diagnostics),
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
