-- HazardMind AI — corrected e2e test schema.
--
-- Derived from the LIVE Neon database (introspected 2026-07-26) cross-checked
-- against what the code actually INSERTs/SELECTs, NOT from shared/db/schema.sql
-- (which is stale — see the mismatch table in tests/e2e/README or the session
-- report). Apply this to a fresh Postgres+PostGIS instance to get a schema that
-- the full pipeline (satellite → hazard → impact → report) runs against without
-- an UndefinedColumn error.
--
-- Sources of truth per table:
--   disaster_events   — backend/db.py (writes status/step/progress/magnitude/
--                       updated_at + runtime ADD COLUMN pipeline_log)
--   satellite_results — agents/satellite/agent.py INSERT (14 cols)
--   hazard_zones      — agents/hazard/agent.py INSERT + report/db_client.py SELECT
--   impact_data       — LIVE Neon shape (impact writes a SUBSET; report SELECTs
--                       overall_confidence, which the agent's own DDL omits but
--                       live Neon HAS — included here so the report read works)
--   final_reports     — agents/report/db_client.py INSERT/SELECT

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------------------------------------------------------------------------
-- disaster_events — owned by the backend. Bare schema.sql lacks status/step/
-- progress/magnitude/updated_at (all read+written by backend/db.py) and
-- pipeline_log (added at runtime by update_pipeline_log via ADD COLUMN IF NOT
-- EXISTS). We create the full shape up front so nothing depends on the runtime
-- ALTER firing first.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS disaster_events (
    event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    disaster_type   VARCHAR(50),
    location        VARCHAR(200),
    magnitude       DOUBLE PRECISION,
    bbox            FLOAT[],
    status          VARCHAR(50),
    step            VARCHAR(50),
    progress        INTEGER,
    pipeline_log    JSONB,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- satellite_results — the satellite node writes exactly these 14 columns
-- (agents/satellite/agent.py). schema.sql's image_url/land_cover are GONE; only
-- affected_area_km2 survives from the old shape. id is SERIAL/int on live Neon
-- (schema.sql said UUID — wrong). bounds/bbox/risk_cities are jsonb.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS satellite_results (
    id                 SERIAL PRIMARY KEY,
    event_id           UUID REFERENCES disaster_events(event_id),
    satellite_type     TEXT,
    cloud_cover        DOUBLE PRECISION,
    scene_id           TEXT,
    true_color_url     TEXT,
    index_url          TEXT,
    classification_url TEXT,
    geojson_url        TEXT,
    affected_area_km2  DOUBLE PRECISION,
    damage_percent     DOUBLE PRECISION,
    total_zones        INTEGER,
    bounds             JSONB,
    bbox               JSONB,
    risk_cities        JSONB,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- hazard_zones — one row per hazard_type per event. geometry is the only real
-- PostGIS column. The (event_id, hazard_type) UNIQUE index is REQUIRED — the
-- hazard node's INSERT ... ON CONFLICT (event_id, hazard_type) depends on it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hazard_zones (
    id                    SERIAL PRIMARY KEY,
    event_id              UUID REFERENCES disaster_events(event_id),
    geometry              GEOMETRY(POLYGON, 4326),
    risk_level            TEXT,
    hazard_type           TEXT,
    area_km2              DOUBLE PRECISION,
    severity              TEXT,
    confirmed_by          JSONB,
    flood_depth_estimate  TEXT,
    earthquake_mmi        DOUBLE PRECISION,
    landslide_probability TEXT,
    overall_confidence    DOUBLE PRECISION,
    created_at            TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hazard_zones_event ON hazard_zones (event_id);
CREATE INDEX IF NOT EXISTS idx_hazard_zones_geometry ON hazard_zones USING GIST (geometry);
-- The ON CONFLICT target — must exist or the hazard INSERT raises.
CREATE UNIQUE INDEX IF NOT EXISTS hazard_zones_event_hazard_uniq
    ON hazard_zones (event_id, hazard_type);

-- ---------------------------------------------------------------------------
-- impact_data — the LIVE Neon shape. The impact agent (services/db.py) writes a
-- SUBSET of these (no overall_confidence) via ON CONFLICT (event_id); report's
-- db_client.py SELECTs overall_confidence. On live Neon the column exists (added
-- out of band), so the report read succeeds — we replicate that here. event_id
-- is UUID on live Neon (the agent DDL says TEXT; live wins). The UNIQUE on
-- event_id is the ON CONFLICT target the impact write depends on.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS impact_data (
    id                        SERIAL PRIMARY KEY,
    event_id                  UUID UNIQUE REFERENCES disaster_events(event_id),
    total_affected            INTEGER,
    high_risk_people          INTEGER,
    medium_risk_people        INTEGER,
    hospitals_at_risk         INTEGER,
    schools_at_risk           INTEGER,
    roads_blocked             INTEGER,
    bridges_at_risk           INTEGER,
    vulnerability_score       TEXT,
    evacuation_routes         JSONB,
    estimated_evacuation_time TEXT,
    overall_confidence        DOUBLE PRECISION,
    created_at                TIMESTAMPTZ DEFAULT NOW(),
    updated_at                TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- final_reports — matches report/db_client.py and live Neon. id SERIAL/int.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS final_reports (
    id                 SERIAL PRIMARY KEY,
    event_id           UUID REFERENCES disaster_events(event_id),
    pdf_url            TEXT,
    map_url            TEXT,
    executive_summary  TEXT,
    agent_log          JSONB,
    total_time_seconds INTEGER,
    confidence_level   TEXT,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_final_reports_event ON final_reports (event_id);
