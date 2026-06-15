CREATE EXTENSION IF NOT EXISTS postgis;

-- Backend writes. event_id is generated ONCE by the orchestrator (UUID4)
-- and is the key every agent references via Band messages.
CREATE TABLE IF NOT EXISTS disaster_events (
    event_id UUID PRIMARY KEY,
    disaster_type VARCHAR(50),
    location VARCHAR(200),
    magnitude FLOAT,
    bbox FLOAT[],
    status VARCHAR(20) NOT NULL DEFAULT 'received',
    step VARCHAR(20) NOT NULL DEFAULT 'received',
    progress INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS satellite_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES disaster_events(event_id),
    image_url TEXT,
    affected_area_km2 FLOAT,
    land_cover TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hazard_zones (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES disaster_events(event_id),
    flood_risk VARCHAR(20),
    earthquake_risk VARCHAR(20),
    landslide_risk VARCHAR(20),
    overall_severity VARCHAR(20),
    risk_geom GEOMETRY(MULTIPOLYGON, 4326),
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS
    hazard_zones_geom_idx
    ON hazard_zones USING GIST(risk_geom);

CREATE TABLE IF NOT EXISTS impact_data (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES disaster_events(event_id),
    population_affected INTEGER,
    hospitals_at_risk INTEGER,
    roads_blocked_km FLOAT,
    schools_affected INTEGER,
    vulnerability_score FLOAT,
    created_at TIMESTAMP DEFAULT NOW()
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
