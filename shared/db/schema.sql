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

CREATE TABLE IF NOT EXISTS hazard_zones (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES disaster_events(id),
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
