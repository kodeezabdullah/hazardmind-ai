export type Severity = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";

export type LayerKey =
  | "hazardZones"
  | "boundary"
  | "facilities"
  | "evacuationRoutes"
  | "satellite"
  | "index"
  | "classification";

export type LayerState = Record<LayerKey, boolean>;

export type Artifacts = {
  true_color_url: string;
  index_url: string;
  classification_url: string;
  geojson_url: string;
};

export type Facility = {
  name: string;
  type: string;
  lat: number;
  lng: number;
  risk: "HIGH" | "MEDIUM" | "LOW";
};

export type AgentLogEntry = {
  agent: string;
  status: "complete" | "running" | "pending" | "failed";
  message: string;
  timestamp: string;
};

export type HazardMindResult = {
  event_id: string;
  location: string;
  hazard_type: string;
  overall_severity: Severity;
  satellite: {
    type: string;
    reason: string;
    cloud_cover: number;
    scene_id: string;
  };
  boundaries: {
    region_boundary: GeoJSON.FeatureCollection;
    risk_cities: string[];
    merged_polygon: GeoJSON.Feature;
    bbox: [number, number, number, number];
  };
  artifacts: Artifacts;
  analysis: {
    index_type: string;
    mean_value: number;
    affected_area_km2: number;
    damage_percent: number;
    total_zones: number;
    zones: GeoJSON.FeatureCollection;
  };
  hazard: {
    flood_risk: Severity;
    earthquake_risk: Severity;
    landslide_risk: Severity;
    confidence_scores: {
      flood: number;
      earthquake: number;
      landslide: number;
    };
  };
  impact: {
    population_affected: number;
    hospitals_at_risk: number;
    roads_blocked_km: number;
    schools_affected: number;
    vulnerability_score: number;
    critical_facilities: Facility[];
  };
  routes: {
    evacuation_routes: GeoJSON.FeatureCollection;
  };
  report: {
    summary: string;
    recommendations: string[];
    pdf_url: string;
    map_url: string;
  };
  agent_log: AgentLogEntry[];
};
