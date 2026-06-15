import type { HazardMindResult } from "./types";

export const sampleResult: HazardMindResult = {
  event_id: "demo-peshawar-flood",
  location: "Peshawar, Pakistan",
  hazard_type: "Flood",
  overall_severity: "CRITICAL",

  satellite: {
    type: "sentinel-1",
    reason: "cloud_cover_above_30_percent_sar_selected",
    cloud_cover: 42,
    scene_id: "S1A_DEMO_PESHAWAR_20260613",
  },

  boundaries: {
    region_boundary: {
      type: "FeatureCollection",
      features: [],
    },
    risk_cities: ["Peshawar"],
    merged_polygon: {
      type: "Feature",
      properties: { name: "Peshawar analysis area" },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [71.4, 33.9],
            [71.65, 33.9],
            [71.65, 34.1],
            [71.4, 34.1],
            [71.4, 33.9],
          ],
        ],
      },
    },
    bbox: [71.4, 33.9, 71.65, 34.1],
  },

  artifacts: {
    true_color_url: "",
    index_url: "",
    classification_url: "",
    geojson_url: "",
  },

  analysis: {
    index_type: "SAR VV/VH ratio",
    mean_value: 0.24,
    affected_area_km2: 153.37,
    damage_percent: 24.3,
    total_zones: 22,
    zones: {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          properties: {
            zone_id: "FZ-01",
            severity: "critical",
            class_name: "deep_water",
            area_km2: 12.4,
          },
          geometry: {
            type: "Polygon",
            coordinates: [
              [
                [71.47, 33.96],
                [71.56, 33.96],
                [71.56, 34.03],
                [71.47, 34.03],
                [71.47, 33.96],
              ],
            ],
          },
        },
        {
          type: "Feature",
          properties: {
            zone_id: "FZ-02",
            severity: "high",
            class_name: "water",
            area_km2: 8.7,
          },
          geometry: {
            type: "Polygon",
            coordinates: [
              [
                [71.43, 33.93],
                [71.5, 33.93],
                [71.5, 33.98],
                [71.43, 33.98],
                [71.43, 33.93],
              ],
            ],
          },
        },
      ],
    },
  },

  hazard: {
    flood_risk: "CRITICAL",
    earthquake_risk: "MEDIUM",
    landslide_risk: "LOW",
    confidence_scores: {
      flood: 0.91,
      earthquake: 0.67,
      landslide: 0.54,
    },
  },

  impact: {
    population_affected: 540000,
    hospitals_at_risk: 14,
    roads_blocked_km: 89,
    schools_affected: 67,
    vulnerability_score: 8.2,
    critical_facilities: [
      {
        name: "Lady Reading Hospital",
        type: "hospital",
        lat: 34.015,
        lng: 71.57,
        risk: "HIGH",
      },
      {
        name: "Khyber Teaching Hospital",
        type: "hospital",
        lat: 33.998,
        lng: 71.487,
        risk: "MEDIUM",
      },
    ],
  },

  routes: {
    evacuation_routes: {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          properties: { name: "Evacuation Route 1" },
          geometry: {
            type: "LineString",
            coordinates: [
              [71.49, 33.97],
              [71.53, 34.0],
              [71.59, 34.06],
            ],
          },
        },
      ],
    },
  },

  report: {
    summary:
      "Critical flood risk has been detected across high-density areas of Peshawar. Satellite-derived classification identifies multiple deep-water and high-risk zones, with significant exposure to population, hospitals, roads, and schools. Immediate evacuation, road clearance, and medical support are recommended.",
    recommendations: [
      "Prioritize evacuation in critical flood zones.",
      "Deploy emergency medical support near hospitals at risk.",
      "Clear blocked road corridors for rescue access.",
      "Open temporary shelters for displaced families.",
      "Monitor flood expansion using updated satellite imagery.",
    ],
    pdf_url: "",
    map_url: "",
  },

  agent_log: [
    {
      agent: "hazardmind-satellite",
      status: "complete",
      message: "Sentinel-1 SAR selected due to cloud cover above 30%. Zones vectorized and uploaded.",
      timestamp: "2026-06-13T18:00:00Z",
    },
    {
      agent: "hazardmind-hazard",
      status: "complete",
      message: "Flood risk classified as CRITICAL. Earthquake and landslide risks assessed.",
      timestamp: "2026-06-13T18:01:00Z",
    },
    {
      agent: "hazardmind-impact",
      status: "complete",
      message: "Population and infrastructure exposure calculated.",
      timestamp: "2026-06-13T18:02:00Z",
    },
    {
      agent: "hazardmind-report",
      status: "complete",
      message: "Executive report and dashboard output generated.",
      timestamp: "2026-06-13T18:03:00Z",
    },
  ],
};
