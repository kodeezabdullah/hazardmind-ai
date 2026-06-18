import type { HazardMindResult } from "./types";

// Real artifacts from our live Rawalpindi pipeline run (event 7d28eeaa), served
// from the public Cloudflare R2 bucket. The zone polygons + PNG risk maps below
// are the actual outputs the satellite/hazard agents produced.
const R2 = "https://pub-720f47eaad2f4997a76a02f8bf14f58a.r2.dev/events/7d28eeaa-cc0b-447f-b0f1-fe9e6ff57842";

// Demo scene footprint (from the pipeline's `bounds`). It is only approximate
// here because the demo data is hand-assembled.
//
// IN PRODUCTION this is not needed: the backend returns the boundary AND the PNG
// `bounds` together from the same satellite clip, so the PNG maps line up inside
// the real boundary automatically (same georeferencing). HazardMap already drapes
// the PNGs on `result.boundaries.scene_bbox ?? bbox`, so wiring the backend is all
// that's required to make it pixel-accurate. PNG overlays stay OFF by default so
// the demo's approximate alignment isn't shown until real bounds are available.
export const SCENE_BBOX: [number, number, number, number] = [72.617206, 33.135627, 73.374205, 33.669574];
export const RAWALPINDI_BBOX: [number, number, number, number] = SCENE_BBOX;

export const sampleResult: HazardMindResult = {
  event_id: "7d28eeaa-cc0b-447f-b0f1-fe9e6ff57842",
  location: "Rawalpindi, Pakistan",
  hazard_type: "Flood",
  overall_severity: "LOW",

  satellite: {
    type: "sentinel-2",
    reason: "low_cloud_cover_optical_selected",
    cloud_cover: 8,
    scene_id: "S2A_RAWALPINDI_20260613",
  },

  boundaries: {
    region_boundary: {
      type: "FeatureCollection",
      features: [],
    },
    risk_cities: ["Rawalpindi"],
    merged_polygon: {
      type: "Feature",
      properties: { name: "Rawalpindi analysis area" },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [SCENE_BBOX[0], SCENE_BBOX[1]],
            [SCENE_BBOX[2], SCENE_BBOX[1]],
            [SCENE_BBOX[2], SCENE_BBOX[3]],
            [SCENE_BBOX[0], SCENE_BBOX[3]],
            [SCENE_BBOX[0], SCENE_BBOX[1]],
          ],
        ],
      },
    },
    // Camera focuses on the city; image/zone footprint stays on SCENE_BBOX.
    bbox: RAWALPINDI_BBOX,
    scene_bbox: SCENE_BBOX,
  },

  // Real PNG risk maps + zones GeoJSON from the pipeline (Cloudflare R2).
  artifacts: {
    true_color_url: `${R2}/true_color.png`,
    index_url: `${R2}/index_map.png`,
    classification_url: `${R2}/classification.png`,
    geojson_url: `${R2}/zones.geojson`,
  },

  analysis: {
    index_type: "NDWI (Sentinel-2)",
    mean_value: 0.12,
    affected_area_km2: 3.98,
    damage_percent: 1.2,
    total_zones: 2,
    // Lightweight placeholder; HazardMap fetches the real zones from
    // artifacts.geojson_url (R2) and renders those instead.
    zones: {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          properties: { zone_id: "WZ-01", severity: "low", class_name: "wet_soil", area_km2: 1.99 },
          geometry: {
            type: "Polygon",
            coordinates: [
              [
                [72.88, 33.46],
                [72.9, 33.46],
                [72.9, 33.48],
                [72.88, 33.48],
                [72.88, 33.46],
              ],
            ],
          },
        },
      ],
    },
  },

  hazard: {
    flood_risk: "LOW",
    earthquake_risk: "LOW",
    landslide_risk: "LOW",
    confidence_scores: {
      flood: 0.34,
      earthquake: 0.21,
      landslide: 0.18,
    },
  },

  impact: {
    population_affected: 4200,
    hospitals_at_risk: 0,
    roads_blocked_km: 2,
    schools_affected: 1,
    vulnerability_score: 2.4,
    // Facilities near the analysed scene footprint (not the distant city centre).
    critical_facilities: [
      {
        name: "Rural Health Centre",
        type: "hospital",
        lat: 33.42,
        lng: 72.87,
        risk: "LOW",
      },
      {
        name: "Community School",
        type: "school",
        lat: 33.3,
        lng: 72.88,
        risk: "LOW",
      },
    ],
  },

  routes: {
    // No evacuation routes for a LOW-severity event.
    evacuation_routes: {
      type: "FeatureCollection",
      features: [],
    },
  },

  report: {
    summary:
      "Satellite analysis of Rawalpindi shows no significant flooding. NDWI classification detects only minor wet-soil patches (~4 km2) at LOW severity, with negligible exposure to population and infrastructure. No evacuation is required; routine monitoring is recommended.",
    recommendations: [
      "No evacuation required — overall flood risk is LOW.",
      "Continue routine monitoring of low-lying wet-soil areas.",
      "Maintain drainage readiness ahead of the monsoon season.",
      "Re-run analysis if heavy rainfall is forecast.",
    ],
    pdf_url: `${R2}/report.pdf`,
    map_url: `${R2}/index_map.png`,
  },

  agent_log: [
    {
      agent: "hazardmind-satellite",
      status: "complete",
      message: "Sentinel-2 optical scene selected (cloud cover 8%). NDWI computed; zones vectorized and uploaded.",
      timestamp: "2026-06-13T18:00:00Z",
    },
    {
      agent: "hazardmind-hazard",
      status: "complete",
      message: "Flood risk classified as LOW. Only minor wet-soil patches detected.",
      timestamp: "2026-06-13T18:01:00Z",
    },
    {
      agent: "hazardmind-impact",
      status: "complete",
      message: "Population and infrastructure exposure calculated — negligible.",
      timestamp: "2026-06-13T18:02:00Z",
    },
    {
      agent: "hazardmind-report",
      status: "complete",
      message: "Executive report generated: no flood, routine monitoring advised.",
      timestamp: "2026-06-13T18:03:00Z",
    },
  ],
};

// Blank result for the IDLE state — no location, no data, empty download URLs.
// The dashboard starts on this (a clean spinning globe) and only shows real
// numbers/links once a query's backend result arrives. This guarantees no
// stale/demo (Rawalpindi) data ever leaks into the result, downloads, or panel.
export const emptyResult: HazardMindResult = {
  event_id: "",
  location: "",
  hazard_type: "",
  overall_severity: "LOW",
  satellite: { type: "", reason: "", cloud_cover: 0, scene_id: "" },
  boundaries: {
    region_boundary: { type: "FeatureCollection", features: [] },
    risk_cities: [],
    merged_polygon: {
      type: "Feature",
      properties: {},
      geometry: { type: "Polygon", coordinates: [[[0, 0], [0, 0], [0, 0], [0, 0]]] },
    },
    bbox: [0, 0, 0, 0],
  },
  artifacts: { true_color_url: "", index_url: "", classification_url: "", geojson_url: "" },
  analysis: {
    index_type: "",
    mean_value: 0,
    affected_area_km2: 0,
    damage_percent: 0,
    total_zones: 0,
    zones: { type: "FeatureCollection", features: [] },
  },
  hazard: {
    flood_risk: "LOW",
    earthquake_risk: "LOW",
    landslide_risk: "LOW",
    confidence_scores: { flood: 0, earthquake: 0, landslide: 0 },
  },
  impact: {
    population_affected: 0,
    hospitals_at_risk: 0,
    roads_blocked_km: 0,
    schools_affected: 0,
    vulnerability_score: 0,
    critical_facilities: [],
  },
  routes: { evacuation_routes: { type: "FeatureCollection", features: [] } },
  report: { summary: "", recommendations: [], pdf_url: "", map_url: "" },
  agent_log: [],
};
