-- 0001_baseline_drift.sql
--
-- Baseline-drift migration. Documents, in idempotent SQL form, every column
-- that already existed on live Neon before this migration file existed but
-- was never recorded in shared/db/schema.sql or any migration (they were
-- added by inline `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements
-- scattered across application code — backend/db.py, agents/satellite/agent.py,
-- agents/impact/services/db.py — with no migration file tracking them).
--
-- This file is a NO-OP against live Neon (every column here already exists
-- there) but makes replaying migrations 0001+ against a FRESH database
-- reconstruct the real live schema, not the stale shared/db/schema.sql.
--
-- Ground truth for this file: a live information_schema.columns introspection
-- of Neon performed 2026-07-28 (see root CLAUDE.md's schema drift table for
-- the human-readable diff). Every statement below is idempotent
-- (IF NOT EXISTS) since it may run against a DB that already has some of
-- these columns from prior inline ALTERs.

-- ============================================================================
-- disaster_events — undocumented columns confirmed live
-- ============================================================================
ALTER TABLE disaster_events ADD COLUMN IF NOT EXISTS magnitude FLOAT8;
ALTER TABLE disaster_events ADD COLUMN IF NOT EXISTS status VARCHAR(50) NOT NULL DEFAULT 'received';
ALTER TABLE disaster_events ADD COLUMN IF NOT EXISTS step VARCHAR(50) NOT NULL DEFAULT 'received';
ALTER TABLE disaster_events ADD COLUMN IF NOT EXISTS progress INTEGER NOT NULL DEFAULT 0;
ALTER TABLE disaster_events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
ALTER TABLE disaster_events ADD COLUMN IF NOT EXISTS band_room_id TEXT;
ALTER TABLE disaster_events ADD COLUMN IF NOT EXISTS pipeline_log JSONB;

-- ============================================================================
-- satellite_results — schema.sql described a completely different table
-- shape (UUID PK, image_url/land_cover). Live table is INTEGER-serial PK
-- with a different, larger column set. We cannot retroactively change the PK
-- type here (that would not be additive / could break existing rows and
-- FKs) — this migration only ensures the REAL live columns exist, matching
-- what backend/db.py's insert_satellite_result and
-- agents/satellite/agent.py's _persist_satellite_result actually write.
-- A brand-new DB created from schema.sql + these migrations will get the
-- INTEGER-serial id (see 0001's CREATE below being a no-op on live Neon,
-- additive-only on a fresh DB via schema.sql itself).
-- ============================================================================
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS satellite_type TEXT;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS cloud_cover FLOAT8;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS scene_id TEXT;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS true_color_url TEXT;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS index_url TEXT;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS classification_url TEXT;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS geojson_url TEXT;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS damage_percent FLOAT8;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS total_zones INTEGER;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS bounds JSONB;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS bbox JSONB;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS risk_cities JSONB;
-- scene_age_days was ALREADY being written by agents/satellite/agent.py's
-- _persist_satellite_result via its own inline ALTER — confirmed via live
-- introspection (2026-07-28) that it did NOT actually reach live Neon,
-- despite the application code assuming it would. Included here so this
-- migration is the single place that guarantees it, going forward; see
-- 0002_durable_evidence_trail.sql for the rest of the durable-evidence work.
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS scene_age_days DOUBLE PRECISION;
-- created_at exists in both schema.sql and live; not repeated here.

-- ============================================================================
-- impact_data — schema.sql described a UUID-PK table with
-- population_affected/schools_affected/vulnerability_score(FLOAT). Live
-- table (agents/impact/services/db.py's own DDL) is INTEGER-serial PK with a
-- different column set entirely. Ensure the real live columns exist.
-- ============================================================================
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS total_affected INTEGER;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS high_risk_people INTEGER;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS medium_risk_people INTEGER;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS hospitals_at_risk INTEGER;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS schools_at_risk INTEGER;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS roads_blocked INTEGER;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS bridges_at_risk INTEGER;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS vulnerability_score TEXT;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS evacuation_routes JSONB;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS estimated_evacuation_time TEXT;
-- overall_confidence: Gate A (root CLAUDE.md) — confirmed present on live
-- Neon, added out-of-band with no migration file recording it, before this
-- session's fix made agents/impact/services/db.py actually WRITE it.
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS overall_confidence DOUBLE PRECISION;
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
-- roads_blocked_km: H#14 fix pass, additive column alongside the legacy
-- roads_blocked INTEGER (kept for back-compat, see agents/impact CLAUDE.md).
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS roads_blocked_km DOUBLE PRECISION;

-- A UNIQUE constraint on event_id is required for the ON CONFLICT (event_id)
-- upsert agents/impact/services/db.py relies on. Guard for a fresh DB where
-- schema.sql's impact_data has no such constraint at all.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'impact_data_event_id_uniq'
    ) THEN
        ALTER TABLE impact_data ADD CONSTRAINT impact_data_event_id_uniq UNIQUE (event_id);
    END IF;
END $$;

-- hazard_zones and final_reports already match live schema exactly per the
-- 2026-07-28 introspection — no baseline-drift statements needed for them.
