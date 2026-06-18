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
    // Optional: the real scene/image footprint, distinct from the camera bbox.
    scene_bbox?: [number, number, number, number];
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
    detailed_body?: string;
    technical_analysis?: string;
    recommendations: string[];
    response_priorities?: string[];
    assumptions?: string[];
    limitations?: string[];
    pdf_url: string;
    map_url: string;
  };
  intelligence?: {
    criticality: {
      criticality: "low" | "normal" | "high" | "critical";
      overall_confidence: number;
      escalation_required: boolean;
      rationale: string;
      trigger_factors: string[];
    };
    anomalies: {
      anomalies_detected: boolean;
      anomalies: Array<{
        type: string;
        severity: "low" | "medium" | "high" | "critical";
        description: string;
        recommended_handling: string;
      }>;
    };
    map_narrative: {
      map_narrative: string;
      key_spatial_findings: string[];
      hotspots: string[];
      map_limitations: string[];
    };
    priority_timeline: {
      next_6_hours: string[];
      next_24_hours: string[];
      next_72_hours: string[];
      resource_priorities: string[];
      coordination_priorities: string[];
    };
    decision_brief: {
      decision_brief: string;
      official_summary: string;
      key_decisions_required: string[];
      human_review_required: boolean;
    };
    quality_check: {
      status: "ready" | "ready_with_warnings" | "not_ready";
      checks: Record<string, boolean>;
      warnings: string[];
      blocking_issues: string[];
    };
    band_ready_message: {
      target: string;
      message: string;
      status: "COMPLETE" | "COMPLETE_WITH_WARNINGS" | "NEEDS_REVIEW";
      confidence: number;
    };
  };
  model_sources?: {
    detailed_report: string;
    executive_summary: string;
    fallback_used: boolean;
    featherless_model?: string;
    intelligence?: Record<string, string>;
  };
  agent_log: AgentLogEntry[];
};
