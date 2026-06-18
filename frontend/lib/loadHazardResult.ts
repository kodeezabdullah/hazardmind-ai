import { sampleResult } from "./sampleResult";
import type { Facility, HazardMindResult, Severity } from "./types";

export type HazardResultSource = "backend" | "demo-fallback";

export type HazardResultLoad = {
  result: HazardMindResult;
  source: HazardResultSource;
  warnings: string[];
};

export async function loadHazardResult(eventId: string): Promise<HazardResultLoad> {
  const warnings: string[] = [];
  const apiUrl = process.env.NEXT_PUBLIC_API_URL?.trim();

  if (apiUrl) {
    try {
      const response = await fetch(`${apiUrl.replace(/\/$/, "")}/results/${encodeURIComponent(eventId)}`, {
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(`Backend returned ${response.status}`);
      }

      const payload = await response.json();
      return {
        result: normalizeHazardResult(unwrapHazardResult(payload), eventId),
        source: "backend",
        warnings,
      };
    } catch (error) {
      warnings.push(`Backend result unavailable; using demo fallback. ${safeError(error)}`);
    }
  } else {
    warnings.push("NEXT_PUBLIC_API_URL is not set; using bundled Rawalpindi demo data.");
  }

  // No backend configured: use the bundled Rawalpindi sample (real R2 artifacts).
  return {
    result: normalizeHazardResult(sampleResult, eventId),
    source: "demo-fallback",
    warnings,
  };
}

function unwrapHazardResult(payload: unknown): unknown {
  const data = asRecord(payload);
  const result = asRecord(data.result);
  if (looksLikeHazardResult(result)) {
    return result;
  }

  const report = asRecord(data.report);
  if (looksLikeHazardResult(report)) {
    return report;
  }

  // Backend /results shape: { job_id, status, satellite, hazard, impact, report }
  // where satellite/hazard/impact/report are the raw DB rows. Flatten it into the
  // top-level fields normalizeHazardResult expects (backend stays untouched).
  if (data.satellite || data.hazard || data.impact || data.report) {
    return adaptBackendResults(data);
  }

  return payload;
}

// Map the backend's per-agent DB rows onto the flat HazardMindResult shape.
function adaptBackendResults(data: Record<string, unknown>): Record<string, unknown> {
  const satellite = asRecord(data.satellite);
  const hazard = asRecord(data.hazard);
  const impact = asRecord(data.impact);
  const report = asRecord(data.report);

  // satellite_results: urls + bounds/bbox + zone stats
  const bbox =
    normalizeBbox(data.bbox) ??
    normalizeBbox(satellite.bbox) ??
    normalizeBbox(satellite.bounds) ??
    undefined;

  // hazard_zones row carries the severity + per-hazard risk levels.
  const severity = hazard.severity ?? hazard.risk_level;
  const hazardType = String(hazard.hazard_type ?? data.disaster_type ?? "").toLowerCase();
  const perHazard = {
    flood_risk: hazardType.includes("flood") ? severity : undefined,
    earthquake_risk: hazardType.includes("earth") || hazard.earthquake_mmi != null ? severity : undefined,
    landslide_risk: hazardType.includes("land") || hazard.landslide_probability != null ? severity : undefined,
  };

  // /results has no top-level location; derive it from the satellite risk_cities.
  const riskCities = Array.isArray(satellite.risk_cities) ? (satellite.risk_cities as unknown[]) : [];
  const cityName = riskCities.length ? String(riskCities[0]) : undefined;

  return {
    event_id: data.event_id ?? data.job_id,
    location: data.location ?? satellite.location ?? cityName,
    hazard_type: hazard.hazard_type ?? data.disaster_type,
    overall_severity: severity,
    satellite: {
      type: satellite.satellite_type,
      cloud_cover: satellite.cloud_cover,
      scene_id: satellite.scene_id,
    },
    boundaries: {
      bbox,
      // PNG/zone footprint uses the satellite bounds when present.
      scene_bbox: normalizeBbox(satellite.bounds) ?? bbox,
      risk_cities: satellite.risk_cities,
    },
    artifacts: {
      true_color_url: satellite.true_color_url,
      index_url: satellite.index_url,
      classification_url: satellite.classification_url,
      geojson_url: satellite.geojson_url,
    },
    analysis: {
      affected_area_km2: satellite.affected_area_km2 ?? hazard.area_km2,
      damage_percent: satellite.damage_percent,
      total_zones: satellite.total_zones,
    },
    hazard: {
      ...perHazard,
      confidence_scores: {
        flood: hazard.overall_confidence,
        earthquake: hazard.overall_confidence,
        landslide: hazard.overall_confidence,
      },
    },
    impact: {
      total_affected: impact.total_affected,
      hospitals_at_risk: impact.hospitals_at_risk,
      schools_at_risk: impact.schools_at_risk,
      roads_blocked: impact.roads_blocked,
      vulnerability_score: impact.vulnerability_score,
    },
    routes: {
      evacuation_routes: impact.evacuation_routes,
    },
    report: {
      summary: report.executive_summary,
      pdf_url: report.pdf_url,
      map_url: report.map_url,
    },
    agent_log: report.agent_log,
  };
}

function normalizeHazardResult(payload: unknown, requestedEventId: string): HazardMindResult {
  const data = asRecord(payload);
  const fallback = cloneSampleResult();
  const event = asRecord(data.event);
  const satellite = asRecord(data.satellite);
  const boundaries = asRecord(data.boundaries);
  const artifacts = asRecord(data.artifacts);
  const analysis = asRecord(data.analysis);
  const hazard = asRecord(data.hazard);
  const impact = asRecord(data.impact);
  const routes = asRecord(data.routes);
  const report = asRecord(data.report);
  const spatial = asRecord(data.spatial);

  const hazardZones = asArray(data.hazard_zones);
  const hazardGeojson =
    asFeatureCollection(spatial.hazard_geojson) ??
    asFeatureCollection(analysis.zones) ??
    hazardZonesToFeatureCollection(hazardZones) ??
    fallback.analysis.zones;

  const routeGeojson =
    asFeatureCollection(routes.evacuation_routes) ??
    asFeatureCollection(data.evacuation_routes) ??
    routeGeojsonToFeatureCollection(spatial.route_geojson) ??
    fallback.routes.evacuation_routes;

  const bbox =
    normalizeBbox(boundaries.bbox) ??
    normalizeBbox(event.bbox) ??
    normalizeBbox(spatial.bbox) ??
    bboxFromFeatureCollection(hazardGeojson) ??
    fallback.boundaries.bbox;

  return {
    event_id: stringValue(data.event_id, event.event_id, requestedEventId || fallback.event_id),
    location: stringValue(data.location, event.location, fallback.location),
    hazard_type: stringValue(data.hazard_type, event.disaster_type, fallback.hazard_type),
    overall_severity: severityValue(data.overall_severity, hazard.severity, event.status, fallback.overall_severity),
    satellite: {
      type: stringValue(satellite.type, satellite.satellite_type, fallback.satellite.type),
      reason: stringValue(satellite.reason, fallback.satellite.reason),
      cloud_cover: numberValue(satellite.cloud_cover, fallback.satellite.cloud_cover),
      scene_id: stringValue(satellite.scene_id, fallback.satellite.scene_id),
    },
    boundaries: {
      region_boundary: asFeatureCollection(boundaries.region_boundary) ?? fallback.boundaries.region_boundary,
      risk_cities: stringArray(boundaries.risk_cities, fallback.boundaries.risk_cities),
      merged_polygon: asFeature(boundaries.merged_polygon) ?? bboxPolygon(bbox),
      bbox,
      scene_bbox: normalizeBbox(boundaries.scene_bbox) ?? undefined,
    },
    artifacts: {
      true_color_url: stringValue(artifacts.true_color_url, satellite.true_color_url, fallback.artifacts.true_color_url),
      index_url: stringValue(artifacts.index_url, satellite.index_url, fallback.artifacts.index_url),
      classification_url: stringValue(artifacts.classification_url, satellite.classification_url, fallback.artifacts.classification_url),
      geojson_url: stringValue(artifacts.geojson_url, satellite.geojson_url, spatial.satellite_geojson_url, fallback.artifacts.geojson_url),
    },
    analysis: {
      index_type: stringValue(analysis.index_type, fallback.analysis.index_type),
      mean_value: numberValue(analysis.mean_value, fallback.analysis.mean_value),
      affected_area_km2: numberValue(analysis.affected_area_km2, fallback.analysis.affected_area_km2),
      damage_percent: numberValue(analysis.damage_percent, fallback.analysis.damage_percent),
      total_zones: numberValue(analysis.total_zones, hazardGeojson.features.length || fallback.analysis.total_zones),
      zones: hazardGeojson,
    },
    hazard: {
      flood_risk: severityValue(hazard.flood_risk, fallback.hazard.flood_risk),
      earthquake_risk: severityValue(hazard.earthquake_risk, fallback.hazard.earthquake_risk),
      landslide_risk: severityValue(hazard.landslide_risk, fallback.hazard.landslide_risk),
      confidence_scores: {
        flood: confidenceValue(asRecord(hazard.confidence_scores).flood, fallback.hazard.confidence_scores.flood),
        earthquake: confidenceValue(asRecord(hazard.confidence_scores).earthquake, fallback.hazard.confidence_scores.earthquake),
        landslide: confidenceValue(asRecord(hazard.confidence_scores).landslide, fallback.hazard.confidence_scores.landslide),
      },
    },
    impact: {
      population_affected: numberValue(impact.population_affected, impact.total_affected, fallback.impact.population_affected),
      hospitals_at_risk: numberValue(impact.hospitals_at_risk, fallback.impact.hospitals_at_risk),
      roads_blocked_km: numberValue(impact.roads_blocked_km, impact.roads_blocked, fallback.impact.roads_blocked_km),
      schools_affected: numberValue(impact.schools_affected, impact.schools_at_risk, fallback.impact.schools_affected),
      vulnerability_score: numberValue(impact.vulnerability_score, fallback.impact.vulnerability_score),
      critical_facilities: facilityArray(impact.critical_facilities, fallback.impact.critical_facilities),
    },
    routes: {
      evacuation_routes: routeGeojson,
    },
    report: {
      summary: stringValue(report.summary, fallback.report.summary),
      detailed_body: optionalString(report.detailed_body),
      technical_analysis: optionalString(report.technical_analysis),
      recommendations: stringArray(report.recommendations, fallback.report.recommendations),
      response_priorities: optionalStringArray(report.response_priorities),
      assumptions: optionalStringArray(report.assumptions),
      limitations: optionalStringArray(report.limitations),
      pdf_url: stringValue(report.pdf_url, fallback.report.pdf_url),
      map_url: stringValue(report.map_url, fallback.report.map_url),
    },
    intelligence: looksLikeIntelligence(data.intelligence) ? data.intelligence : fallback.intelligence,
    model_sources: normalizeModelSources(data.model_sources),
    agent_log: Array.isArray(data.agent_log) ? (data.agent_log as HazardMindResult["agent_log"]) : fallback.agent_log,
  };
}

function looksLikeHazardResult(value: Record<string, unknown>): boolean {
  return Boolean(value.event_id && value.location && value.hazard_type);
}

function looksLikeIntelligence(value: unknown): value is HazardMindResult["intelligence"] {
  const record = asRecord(value);
  return Boolean(record.criticality && record.map_narrative && record.priority_timeline);
}

function normalizeModelSources(value: unknown): HazardMindResult["model_sources"] | undefined {
  const record = asRecord(value);
  if (!record.detailed_report || !record.executive_summary) {
    return undefined;
  }
  return {
    detailed_report: stringValue(record.detailed_report),
    executive_summary: stringValue(record.executive_summary),
    fallback_used: Boolean(record.fallback_used),
    featherless_model: optionalString(record.featherless_model),
    intelligence: asRecord(record.intelligence) as Record<string, string>,
  };
}

function cloneSampleResult(): HazardMindResult {
  return JSON.parse(JSON.stringify(sampleResult)) as HazardMindResult;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringValue(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) {
      return value;
    }
    if (typeof value === "number") {
      return String(value);
    }
  }
  return "";
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function numberValue(...values: unknown[]): number {
  for (const value of values) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return 0;
}

function confidenceValue(value: unknown, fallback: number): number {
  const parsed = numberValue(value, fallback);
  return Math.max(0, Math.min(1, parsed));
}

function severityValue(...values: unknown[]): Severity {
  for (const value of values) {
    const normalized = String(value ?? "").toUpperCase();
    if (normalized === "CRITICAL" || normalized === "HIGH" || normalized === "MEDIUM" || normalized === "LOW") {
      return normalized;
    }
  }
  return "MEDIUM";
}

function stringArray(value: unknown, fallback: string[] = []): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : fallback;
}

function optionalStringArray(value: unknown): string[] | undefined {
  const list = stringArray(value);
  return list.length ? list : undefined;
}

function facilityArray(value: unknown, fallback: Facility[]): Facility[] {
  if (!Array.isArray(value)) {
    return fallback;
  }
  return value
    .map((item) => {
      const record = asRecord(item);
      const lat = numberValue(record.lat);
      const lng = numberValue(record.lng);
      if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
        return null;
      }
      return {
        name: stringValue(record.name, "Facility"),
        type: stringValue(record.type, "facility"),
        lat,
        lng,
        risk: severityValue(record.risk, "LOW") === "CRITICAL" ? "HIGH" : (severityValue(record.risk, "LOW") as Facility["risk"]),
      };
    })
    .filter((item): item is Facility => Boolean(item));
}

function asFeatureCollection(value: unknown): GeoJSON.FeatureCollection | null {
  const record = asRecord(value);
  if (record.type === "FeatureCollection" && Array.isArray(record.features)) {
    return record as unknown as GeoJSON.FeatureCollection;
  }
  if (record.type === "Feature") {
    return { type: "FeatureCollection", features: [record as unknown as GeoJSON.Feature] };
  }
  return null;
}

function asFeature(value: unknown): GeoJSON.Feature | null {
  const record = asRecord(value);
  return record.type === "Feature" ? (record as unknown as GeoJSON.Feature) : null;
}

function hazardZonesToFeatureCollection(rows: unknown[]): GeoJSON.FeatureCollection | null {
  const features = rows
    .map((row) => {
      const record = asRecord(row);
      const geometry = asRecord(record.geometry_geojson).type ? asRecord(record.geometry_geojson) : asRecord(record.geometry);
      if (!geometry.type) {
        return null;
      }
      return {
        type: "Feature",
        properties: {
          id: record.id,
          risk_level: record.risk_level,
          hazard_type: record.hazard_type,
          area_km2: record.area_km2,
          severity: record.severity ?? record.risk_level,
          overall_confidence: record.overall_confidence,
        },
        geometry: geometry as unknown as GeoJSON.Geometry,
      } as GeoJSON.Feature;
    })
    .filter((feature): feature is GeoJSON.Feature => Boolean(feature));
  return features.length ? { type: "FeatureCollection", features } : null;
}

function routeGeojsonToFeatureCollection(value: unknown): GeoJSON.FeatureCollection | null {
  const featureCollection = asFeatureCollection(value);
  if (featureCollection) {
    return featureCollection;
  }
  const routes = asArray(value);
  const features = routes
    .map((route) => {
      const record = asRecord(route);
      const geojson = asRecord(record.geojson);
      if (geojson.type === "LineString" || geojson.type === "MultiLineString") {
        return {
          type: "Feature",
          properties: {
            name: record.name,
            distance_km: record.distance_km,
            status: record.status,
          },
          geometry: geojson as unknown as GeoJSON.Geometry,
        } as GeoJSON.Feature;
      }
      return asFeature(route);
    })
    .filter((feature): feature is GeoJSON.Feature => Boolean(feature));
  return features.length ? { type: "FeatureCollection", features } : null;
}

function normalizeBbox(value: unknown): [number, number, number, number] | null {
  if (Array.isArray(value) && value.length === 4) {
    const bbox = value.map(Number);
    if (bbox.every(Number.isFinite)) {
      return [bbox[0], bbox[1], bbox[2], bbox[3]];
    }
  }
  const record = asRecord(value);
  const nested = record.bbox ?? record.bounds ?? record.extent;
  if (nested) {
    return normalizeBbox(nested);
  }
  const keys = [
    ["minLng", "minLat", "maxLng", "maxLat"],
    ["west", "south", "east", "north"],
    ["min_lng", "min_lat", "max_lng", "max_lat"],
  ];
  for (const keySet of keys) {
    if (keySet.every((key) => key in record)) {
      return normalizeBbox(keySet.map((key) => record[key]));
    }
  }
  return null;
}

function bboxFromFeatureCollection(featureCollection: GeoJSON.FeatureCollection): [number, number, number, number] | null {
  const points: Array<[number, number]> = [];
  const visit = (value: unknown) => {
    if (Array.isArray(value) && value.length >= 2 && typeof value[0] === "number" && typeof value[1] === "number") {
      points.push([value[0], value[1]]);
      return;
    }
    if (Array.isArray(value)) {
      value.forEach(visit);
    }
  };
  featureCollection.features.forEach((feature) => {
    const geometry = feature.geometry as GeoJSON.Geometry | null;
    if (geometry && "coordinates" in geometry) {
      visit(geometry.coordinates);
    }
  });
  if (!points.length) {
    return null;
  }
  const lngs = points.map((point) => point[0]);
  const lats = points.map((point) => point[1]);
  return [Math.min(...lngs), Math.min(...lats), Math.max(...lngs), Math.max(...lats)];
}

function bboxPolygon(bbox: [number, number, number, number]): GeoJSON.Feature {
  const [west, south, east, north] = bbox;
  return {
    type: "Feature",
    properties: { name: "Analysis area" },
    geometry: {
      type: "Polygon",
      coordinates: [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
    },
  };
}

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : "Unknown error";
}
