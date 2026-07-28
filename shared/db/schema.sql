-- shared/db/schema.sql
--
-- CURRENT-STATE snapshot of the live Neon schema, for humans/tools to read.
-- This file is NOT the change log — shared/db/migrations/ (numbered, additive,
-- idempotent .sql files) is the ordered history of how the schema got here.
-- When you change a column, add a migration file AND update this file to
-- match (see root CLAUDE.md's rule: "any future column change goes through a
-- migration file in shared/db/migrations/, never an inline ALTER TABLE in
-- application code").
--
-- Rebuilt 2026-07-28 from a live information_schema.columns introspection
-- (root CLAUDE.md's "TRUE LIVE SCHEMA" section) plus the durable-evidence-
-- trail migration (0002_durable_evidence_trail.sql). Running this file
-- against a fresh DB, followed by every file in migrations/ in order,
-- reconstructs the real live schema — this file alone is intentionally NOT
-- sufficient for that (migrations/ carries the additive history); this file
-- is the readable target state.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS disaster_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    disaster_type VARCHAR(50),
    location VARCHAR(200),
    bbox FLOAT8[],
    magnitude FLOAT8,
    status VARCHAR(50) NOT NULL DEFAULT 'received',
    step VARCHAR(50) NOT NULL DEFAULT 'received',
    progress INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    band_room_id TEXT,           -- dead post Band->LangChain migration; kept, unused
    pipeline_log JSONB
);

CREATE TABLE IF NOT EXISTS satellite_results (
    id SERIAL PRIMARY KEY,
    event_id UUID REFERENCES disaster_events(event_id),
    satellite_type TEXT,
    cloud_cover FLOAT8,
    scene_id TEXT,
    true_color_url TEXT,
    index_url TEXT,
    classification_url TEXT,
    geojson_url TEXT,
    affected_area_km2 FLOAT8,
    damage_percent FLOAT8,        -- no producer anywhere in the codebase; always NULL (see satellite CLAUDE.md)
    total_zones INTEGER,
    bounds JSONB,
    bbox JSONB,
    risk_cities JSONB,
    scene_age_days DOUBLE PRECISION, -- confirmed absent pre-2026-07-28; see agents/satellite CLAUDE.md
    -- Durable evidence trail (0002_durable_evidence_trail.sql, feat/durable-evidence-trail):
    confidence REAL,
    confidence_basis TEXT,
    evidence_count INTEGER,
    coverage_percent REAL,
    coverage_status TEXT,
    index_calibrated BOOLEAN,
    index_units TEXT,
    selection_reason TEXT,
    scene_cloud_percent REAL,
    aoi_cloud_percent REAL,
    scl_reused BOOLEAN,
    diagnostics JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

DROP TABLE IF EXISTS hazard_zones CASCADE;
CREATE TABLE hazard_zones (
    id SERIAL PRIMARY KEY,
    event_id UUID REFERENCES disaster_events(event_id),
    geometry GEOMETRY(POLYGON, 4326),
    risk_level TEXT,
    hazard_type TEXT,
    area_km2 FLOAT8,
    severity TEXT,
    confirmed_by JSONB,
    flood_depth_estimate TEXT,
    earthquake_mmi FLOAT8,
    landslide_probability TEXT,
    overall_confidence FLOAT8,
    -- Durable evidence trail (0002_durable_evidence_trail.sql):
    diagnostics JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_hazard_zones_event
ON hazard_zones(event_id);
CREATE INDEX idx_hazard_zones_geometry
ON hazard_zones USING GIST(geometry);
CREATE UNIQUE INDEX IF NOT EXISTS hazard_zones_event_hazard_uniq
ON hazard_zones(event_id, hazard_type);

CREATE TABLE IF NOT EXISTS impact_data (
    id SERIAL PRIMARY KEY,
    event_id UUID UNIQUE NOT NULL,   -- agents/impact/services/db.py's own DDL declares TEXT, but live Neon's
                                      -- actual column type is UUID (confirmed via information_schema.columns,
                                      -- 2026-07-28) -- the app-level DDL string here is stale/aspirational and
                                      -- was never what actually ran; this file reflects the real column type
    total_affected INTEGER,
    high_risk_people INTEGER,
    medium_risk_people INTEGER,
    hospitals_at_risk INTEGER,
    schools_at_risk INTEGER,
    -- roads_blocked / roads_blocked_km (resolved 2026-07-28, coherence pass
    -- on feat/durable-evidence-trail):
    --   * roads_blocked_km is CANONICAL. Correct name, correct precision
    --     (DOUBLE PRECISION, 1 decimal). All new readers must use this one.
    --   * roads_blocked (INTEGER) is a write-time DERIVED LEGACY MIRROR
    --     (int(round(roads_blocked_km))), written in the SAME transaction
    --     from the SAME source value as roads_blocked_km — the two can
    --     never diverge within one run (agents/impact/services/db.py).
    --     Kept only so pre-H#14 rows (written before roads_blocked_km
    --     existed) remain readable by old callers; not a second source of
    --     truth.
    --   * Drop condition: roads_blocked may be dropped once (a) no
    --     pre-H#14 rows remain in scope, AND (b) frontend/lib/loadHazardResult.ts's
    --     roads_blocked fallback (its own read of this column for old rows)
    --     is removed — whichever comes first triggers revisiting this, but
    --     BOTH must be true before the column is actually dropped.
    --   * Do NOT swap which one is canonical. roads_blocked_km stays
    --     canonical even if a future maintainer is tempted to consolidate
    --     the other way — its name and precision are correct, unlike
    --     roads_blocked's.
    roads_blocked INTEGER,             -- DEPRECATED legacy mirror of roads_blocked_km, see comment above
    roads_blocked_km DOUBLE PRECISION, -- CANONICAL — all new readers must use this column
    bridges_at_risk INTEGER,
    vulnerability_score TEXT,
    evacuation_routes JSONB,
    estimated_evacuation_time TEXT,
    overall_confidence DOUBLE PRECISION,
    -- Durable evidence trail (0002_durable_evidence_trail.sql):
    diagnostics JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

DROP TABLE IF EXISTS final_reports CASCADE;
CREATE TABLE final_reports (
    id SERIAL PRIMARY KEY,
    event_id UUID REFERENCES disaster_events(event_id),
    pdf_url TEXT,
    map_url TEXT,
    executive_summary TEXT,
    agent_log JSONB,
    total_time_seconds INT,
    confidence_level TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_final_reports_event
ON final_reports(event_id);
