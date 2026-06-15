CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS disaster_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    disaster_type VARCHAR(50),
    location VARCHAR(200),
    bbox FLOAT[],
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS satellite_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES disaster_events(id),
    image_url TEXT,
    affected_area_km2 FLOAT,
    land_cover TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

DROP TABLE IF EXISTS hazard_zones CASCADE;
CREATE TABLE hazard_zones (
    id SERIAL PRIMARY KEY,
    event_id UUID REFERENCES disaster_events(id),
    geometry GEOMETRY(POLYGON, 4326),
    risk_level TEXT,
    hazard_type TEXT,
    area_km2 FLOAT,
    severity TEXT,
    confirmed_by JSONB,
    flood_depth_estimate TEXT,
    earthquake_mmi FLOAT,
    landslide_probability TEXT,
    overall_confidence FLOAT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_hazard_zones_event
ON hazard_zones(event_id);
CREATE INDEX idx_hazard_zones_geometry
ON hazard_zones USING GIST(geometry);

CREATE TABLE IF NOT EXISTS impact_data (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES disaster_events(id),
    population_affected INTEGER,
    hospitals_at_risk INTEGER,
    roads_blocked_km FLOAT,
    schools_affected INTEGER,
    vulnerability_score FLOAT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS final_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES disaster_events(id),
    pdf_url TEXT,
    map_url TEXT,
    summary TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
