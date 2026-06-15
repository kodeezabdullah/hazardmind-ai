import json
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent


LATEST_FINAL_REPORT_COLUMNS = (
    "event_id",
    "pdf_url",
    "map_url",
    "executive_summary",
    "agent_log",
    "total_time_seconds",
    "confidence_level",
)


def is_valid_uuid(value: str) -> bool:
    """
    Return True only if value is a valid UUID string.
    """
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


async def fetch_report_context_from_db(event_id: str) -> dict:
    """
    Read Abdullah's latest Neon schema and return structured upstream context.
    """
    if not is_valid_uuid(event_id):
        raise ValueError("event_id must be a valid UUID")

    conn = await _connect()
    try:
        event = await _fetch_disaster_event(conn, event_id)
        satellite_results = await _fetch_satellite_results(conn, event_id)
        hazard_zones = await _fetch_hazard_zones(conn, event_id)
        impact_data = await _fetch_impact_data(conn, event_id)
        return build_structured_report_context(event_id, event, satellite_results, hazard_zones, impact_data)
    except Exception as exc:
        if _schema_mismatch(exc):
            raise RuntimeError(f"Neon DB schema mismatch while reading latest Report Agent context: {_safe_db_error(exc)}") from None
        raise
    finally:
        await conn.close()


async def write_final_report_metadata(report: dict, total_time_seconds: int | None = None) -> None:
    """
    Write to final_reports using Abdullah's latest schema.
    """
    values = build_final_report_db_values(report, total_time_seconds=total_time_seconds)
    event_id = values["event_id"]
    if not is_valid_uuid(event_id):
        raise ValueError("event_id must be a valid UUID")

    conn = await _connect()
    try:
        existing_id = await conn.fetchval(
            """
            SELECT id
            FROM final_reports
            WHERE event_id = $1::uuid
            ORDER BY created_at DESC
            LIMIT 1;
            """,
            event_id,
        )
        if existing_id is not None:
            await conn.execute(
                """
                UPDATE final_reports SET
                    pdf_url = $2,
                    map_url = $3,
                    executive_summary = $4,
                    agent_log = $5::jsonb,
                    total_time_seconds = $6,
                    confidence_level = $7
                WHERE id = $1;
                """,
                existing_id,
                values["pdf_url"],
                values["map_url"],
                values["executive_summary"],
                json.dumps(values["agent_log"]),
                values["total_time_seconds"],
                values["confidence_level"],
            )
        else:
            await conn.execute(
                """
                INSERT INTO final_reports (
                    event_id,
                    pdf_url,
                    map_url,
                    executive_summary,
                    agent_log,
                    total_time_seconds,
                    confidence_level
                )
                VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6, $7);
                """,
                event_id,
                values["pdf_url"],
                values["map_url"],
                values["executive_summary"],
                json.dumps(values["agent_log"]),
                values["total_time_seconds"],
                values["confidence_level"],
            )
    except Exception as exc:
        if _schema_mismatch(exc):
            expected = ", ".join(LATEST_FINAL_REPORT_COLUMNS)
            raise RuntimeError(f"Neon final_reports schema mismatch: expected latest columns ({expected}).") from None
        raise RuntimeError(f"Neon final_reports write failed: {type(exc).__name__}") from None
    finally:
        await conn.close()


def build_final_report_db_values(report: dict, total_time_seconds: int | None = None) -> dict:
    report_section = report.get("report", {})
    elapsed = total_time_seconds
    if elapsed is None:
        elapsed = report.get("total_time_seconds") or report_section.get("total_time_seconds")
    try:
        elapsed = int(elapsed)
    except (TypeError, ValueError):
        elapsed = 0
    return {
        "event_id": str(report.get("event_id", "")),
        "pdf_url": report_section.get("pdf_url") or "",
        "map_url": report_section.get("map_url") or "",
        "executive_summary": report_section.get("summary") or "",
        "agent_log": _agent_log_payload(report, total_time_seconds=elapsed),
        "total_time_seconds": elapsed,
        "confidence_level": calculate_confidence_level(report),
    }


async def _fetch_disaster_event(conn, event_id: str) -> dict:
    row = await conn.fetchrow(
        """
        SELECT
            event_id,
            disaster_type,
            location,
            magnitude,
            bbox,
            status,
            step,
            progress,
            created_at,
            updated_at
        FROM disaster_events
        WHERE event_id = $1::uuid;
        """,
        event_id,
    )
    return _row_to_dict(row)


async def _fetch_satellite_results(conn, event_id: str) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT
            id,
            event_id,
            satellite_type,
            cloud_cover,
            scene_id,
            true_color_url,
            index_url,
            classification_url,
            geojson_url,
            affected_area_km2,
            damage_percent,
            total_zones,
            bounds,
            bbox,
            risk_cities,
            created_at
        FROM satellite_results
        WHERE event_id = $1::uuid
        ORDER BY created_at DESC;
        """,
        event_id,
    )
    return [_row_to_dict(row) for row in rows]


async def _fetch_hazard_zones(conn, event_id: str) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT
            id,
            event_id,
            ST_AsGeoJSON(geometry)::jsonb AS geometry_geojson,
            risk_level,
            hazard_type,
            area_km2,
            severity,
            confirmed_by,
            flood_depth_estimate,
            earthquake_mmi,
            landslide_probability,
            overall_confidence,
            created_at
        FROM hazard_zones
        WHERE event_id = $1::uuid
        ORDER BY created_at DESC;
        """,
        event_id,
    )
    return [_row_to_dict(row) for row in rows]


async def _fetch_impact_data(conn, event_id: str) -> dict:
    row = await conn.fetchrow(
        """
        SELECT
            id,
            event_id,
            total_affected,
            high_risk_people,
            medium_risk_people,
            hospitals_at_risk,
            schools_at_risk,
            roads_blocked,
            bridges_at_risk,
            vulnerability_score,
            evacuation_routes,
            estimated_evacuation_time,
            overall_confidence,
            created_at
        FROM impact_data
        WHERE event_id = $1::uuid
        ORDER BY created_at DESC
        LIMIT 1;
        """,
        event_id,
    )
    return _row_to_dict(row)


def build_structured_report_context(
    event_id: str,
    event: dict | None,
    satellite_results: list[dict] | None,
    hazard_zones: list[dict] | None,
    impact_data: dict | None,
) -> dict:
    event_data = _normalize_event_row(event or {"event_id": event_id})
    satellites = [_normalize_satellite_row(row) for row in (satellite_results or [])]
    hazards = [_normalize_hazard_row(row) for row in (hazard_zones or [])]
    impact = _normalize_impact_row(impact_data or {})
    hazard_geojson = hazard_zones_to_feature_collection(hazards)
    route_geojson = _route_geojson_list(impact.get("evacuation_routes"))
    hazard_confidence = _average_confidence(row.get("overall_confidence") for row in hazards)
    impact_confidence = _numeric_confidence(impact.get("overall_confidence"))
    combined_confidence = _average_confidence([hazard_confidence, impact_confidence])
    latest_satellite = satellites[0] if satellites else {}

    return {
        "event": event_data,
        "satellite_results": satellites,
        "hazard_zones": hazards,
        "impact_data": impact,
        "spatial": {
            "satellite_geojson_url": latest_satellite.get("geojson_url") or None,
            "hazard_geojson": hazard_geojson,
            "route_geojson": route_geojson,
        },
        "confidence": {
            "hazard_overall_confidence": hazard_confidence,
            "impact_overall_confidence": impact_confidence,
            "combined_confidence": combined_confidence,
        },
    }


def db_context_to_report_context(db_context: dict) -> dict:
    """
    Convert the structured DB contract into the frontend-ready generator context.
    """
    event = db_context.get("event", {})
    satellites = db_context.get("satellite_results", [])
    latest_satellite = satellites[0] if satellites else {}
    hazards = db_context.get("hazard_zones", [])
    impact = db_context.get("impact_data", {})
    spatial = db_context.get("spatial", {})
    confidence = db_context.get("confidence", {})
    hazard_geojson = spatial.get("hazard_geojson") or {"type": "FeatureCollection", "features": []}
    bbox = (
        normalize_bbox(event.get("bbox"))
        or normalize_bbox(latest_satellite.get("bbox"))
        or normalize_bbox(latest_satellite.get("bounds"))
        or _bbox_from_feature_collection(hazard_geojson)
        or [0, 0, 0, 0]
    )
    hazard_type = event.get("disaster_type") or _dominant_hazard_type(hazards) or "Unknown"
    combined_confidence = confidence.get("combined_confidence")

    return {
        "event_id": str(event.get("event_id") or ""),
        "location": event.get("location") or "",
        "hazard_type": hazard_type,
        "overall_severity": _overall_severity(event, hazards),
        "satellite": {
            "type": latest_satellite.get("satellite_type") or "",
            "reason": "loaded_from_latest_neon_schema",
            "cloud_cover": latest_satellite.get("cloud_cover") or 0,
            "scene_id": latest_satellite.get("scene_id") or "",
        },
        "boundaries": {
            "region_boundary": {"type": "FeatureCollection", "features": []},
            "risk_cities": _json_list(latest_satellite.get("risk_cities")),
            "merged_polygon": _bbox_polygon_feature(bbox),
            "bbox": bbox,
        },
        "artifacts": {
            "true_color_url": latest_satellite.get("true_color_url") or "",
            "index_url": latest_satellite.get("index_url") or "",
            "classification_url": latest_satellite.get("classification_url") or "",
            "geojson_url": latest_satellite.get("geojson_url") or "",
        },
        "analysis": {
            "index_type": "database_result",
            "mean_value": 0,
            "affected_area_km2": latest_satellite.get("affected_area_km2") or 0,
            "damage_percent": latest_satellite.get("damage_percent") or 0,
            "total_zones": latest_satellite.get("total_zones") or len(hazard_geojson.get("features", [])),
            "zones": hazard_geojson,
        },
        "hazard": {
            "flood_risk": _risk_for_type(hazards, "flood"),
            "earthquake_risk": _risk_for_type(hazards, "earthquake"),
            "landslide_risk": _risk_for_type(hazards, "landslide"),
            "confidence_scores": {
                "overall": combined_confidence,
                "flood": _confidence_for_type(hazards, "flood", combined_confidence),
                "earthquake": _confidence_for_type(hazards, "earthquake", combined_confidence),
                "landslide": _confidence_for_type(hazards, "landslide", combined_confidence),
            },
        },
        "impact": {
            "population_affected": impact.get("total_affected") or 0,
            "total_affected": impact.get("total_affected") or 0,
            "high_risk_people": impact.get("high_risk_people") or 0,
            "medium_risk_people": impact.get("medium_risk_people") or 0,
            "hospitals_at_risk": impact.get("hospitals_at_risk") or 0,
            "schools_affected": impact.get("schools_at_risk") or 0,
            "roads_blocked_km": impact.get("roads_blocked") or 0,
            "bridges_at_risk": impact.get("bridges_at_risk") or 0,
            "vulnerability_score": impact.get("vulnerability_score") or 0,
            "critical_facilities": [],
            "estimated_evacuation_time": impact.get("estimated_evacuation_time"),
            "overall_confidence": impact.get("overall_confidence"),
        },
        "routes": {
            "evacuation_routes": _evacuation_routes(impact.get("evacuation_routes")),
        },
        "db_context": db_context,
    }


def hazard_zones_to_feature_collection(hazard_zones: list[dict]) -> dict:
    features = []
    for row in hazard_zones:
        geometry = _json_object(row.get("geometry_geojson"))
        if not geometry:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "id": row.get("id"),
                    "risk_level": row.get("risk_level"),
                    "hazard_type": row.get("hazard_type"),
                    "area_km2": row.get("area_km2"),
                    "severity": row.get("severity"),
                    "confirmed_by": _json_list(row.get("confirmed_by")),
                    "overall_confidence": row.get("overall_confidence"),
                    "flood_depth_estimate": row.get("flood_depth_estimate"),
                    "earthquake_mmi": row.get("earthquake_mmi"),
                    "landslide_probability": row.get("landslide_probability"),
                },
                "geometry": geometry,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def calculate_confidence_level(report: dict) -> str:
    values = _collect_confidence_values(report)
    if not values:
        return "UNKNOWN"
    if any(value < 0.6 for value in values):
        return "LOW"
    combined = sum(values) / len(values)
    if combined >= 0.8:
        return "HIGH"
    if combined >= 0.6:
        return "MEDIUM"
    return "LOW"


def normalize_bbox(value) -> list[float] | None:
    parsed = _json_object(value)
    if isinstance(parsed, (list, tuple)) and len(parsed) == 4:
        return _numeric_bbox(parsed)
    if isinstance(parsed, dict):
        for key in ("bbox", "bounds", "extent"):
            nested = normalize_bbox(parsed.get(key))
            if nested:
                return nested
        keyed = _bbox_from_keyed_dict(parsed)
        if keyed:
            return keyed
        if "coordinates" in parsed:
            return _bbox_from_coordinates(parsed.get("coordinates"))
        if parsed.get("type") == "FeatureCollection":
            return _bbox_from_feature_collection(parsed)
        if parsed.get("type") == "Feature":
            return normalize_bbox(parsed.get("geometry"))
    return None


def _normalize_event_row(row: dict) -> dict:
    event = dict(row)
    event["event_id"] = str(event.get("event_id") or "")
    event["bbox"] = normalize_bbox(event.get("bbox"))
    return _json_safe(event)


def _normalize_satellite_row(row: dict) -> dict:
    satellite = dict(row)
    satellite["event_id"] = str(satellite.get("event_id") or "")
    satellite["bbox"] = normalize_bbox(satellite.get("bbox"))
    bounds = _json_object(satellite.get("bounds"))
    satellite["bounds"] = bounds
    if satellite["bbox"] is None:
        satellite["bbox"] = normalize_bbox(bounds)
    return _json_safe(satellite)


def _normalize_hazard_row(row: dict) -> dict:
    hazard = dict(row)
    hazard["event_id"] = str(hazard.get("event_id") or "")
    hazard["geometry_geojson"] = _json_object(hazard.get("geometry_geojson"))
    hazard["confirmed_by"] = _json_safe(hazard.get("confirmed_by"))
    return _json_safe(hazard)


def _normalize_impact_row(row: dict) -> dict:
    impact = dict(row)
    if impact.get("event_id"):
        impact["event_id"] = str(impact.get("event_id"))
    impact["evacuation_routes"] = _json_safe(_json_object(impact.get("evacuation_routes")))
    return _json_safe(impact)


def _agent_log_payload(report: dict, total_time_seconds: int) -> list:
    log = _json_safe(report.get("agent_log", []))
    elapsed_entry = {
        "agent": "hazardmind-report",
        "status": "complete",
        "message": "Pipeline elapsed time recorded.",
        "timestamp": "",
        "total_time_seconds": total_time_seconds,
    }
    if isinstance(log, list):
        return [*log, elapsed_entry]
    return [elapsed_entry]


def _collect_confidence_values(report: dict) -> list[float]:
    values: list[float] = []
    confidence = report.get("confidence", {}) if isinstance(report.get("confidence"), dict) else {}
    for key in ("hazard_overall_confidence", "impact_overall_confidence", "combined_confidence"):
        _append_confidence(values, confidence.get(key))
    _append_confidence(values, report.get("impact", {}).get("overall_confidence"))
    hazard_scores = report.get("hazard", {}).get("confidence_scores", {})
    if isinstance(hazard_scores, dict):
        for key in ("overall", "flood", "earthquake", "landslide"):
            _append_confidence(values, hazard_scores.get(key))
    _append_confidence(values, report.get("intelligence", {}).get("criticality", {}).get("overall_confidence"))
    return values


def _append_confidence(values: list[float], value) -> None:
    parsed = _numeric_confidence(value)
    if parsed is not None:
        values.append(parsed)


def _numeric_confidence(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if 0 <= parsed <= 1:
        return parsed
    return None


def _average_confidence(values) -> float | None:
    cleaned = [value for value in (_numeric_confidence(item) for item in values) if value is not None]
    if not cleaned:
        return None
    return round(sum(cleaned) / len(cleaned), 3)


def _database_url() -> str:
    load_dotenv(BASE_DIR / ".env")
    database_url = os.getenv("NEON_DATABASE_URL")
    if not database_url:
        raise RuntimeError("Missing required Neon environment variable: NEON_DATABASE_URL")
    return database_url


async def _connect():
    try:
        return await asyncpg.connect(_database_url())
    except Exception as exc:
        raise RuntimeError(f"Neon connection failed: {type(exc).__name__}") from None


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    return _json_safe(dict(row))


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _json_object(value):
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list, tuple)):
        return _json_safe(value)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _json_list(value) -> list:
    parsed = _json_object(value)
    if parsed is None:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, tuple):
        return list(parsed)
    return [parsed] if parsed not in ("", None) else []


def _numeric_bbox(values) -> list[float] | None:
    try:
        bbox = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    min_lng, min_lat, max_lng, max_lat = bbox
    if min_lng > max_lng:
        min_lng, max_lng = max_lng, min_lng
    if min_lat > max_lat:
        min_lat, max_lat = max_lat, min_lat
    return [min_lng, min_lat, max_lng, max_lat]


def _bbox_from_keyed_dict(value: dict) -> list[float] | None:
    key_sets = (
        ("minLng", "minLat", "maxLng", "maxLat"),
        ("min_lng", "min_lat", "max_lng", "max_lat"),
        ("west", "south", "east", "north"),
        ("xmin", "ymin", "xmax", "ymax"),
    )
    for keys in key_sets:
        if all(key in value for key in keys):
            return _numeric_bbox([value[key] for key in keys])
    return None


def _bbox_from_coordinates(coordinates) -> list[float] | None:
    points: list[tuple[float, float]] = []

    def walk(item) -> None:
        if isinstance(item, (list, tuple)) and len(item) >= 2 and all(isinstance(part, (int, float)) for part in item[:2]):
            points.append((float(item[0]), float(item[1])))
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(coordinates)
    if not points:
        return None
    lngs = [point[0] for point in points]
    lats = [point[1] for point in points]
    return [min(lngs), min(lats), max(lngs), max(lats)]


def _bbox_from_feature_collection(feature_collection: dict) -> list[float] | None:
    if not isinstance(feature_collection, dict):
        return None
    bboxes = []
    for feature in feature_collection.get("features", []):
        bbox = normalize_bbox(feature)
        if bbox:
            bboxes.append(bbox)
    if not bboxes:
        return normalize_bbox(feature_collection.get("geometry"))
    return [
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    ]


def _bbox_polygon_feature(bbox: list[float]) -> dict:
    min_lng, min_lat, max_lng, max_lat = bbox
    return {
        "type": "Feature",
        "properties": {"name": "database analysis bbox"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [min_lng, min_lat],
                    [max_lng, min_lat],
                    [max_lng, max_lat],
                    [min_lng, max_lat],
                    [min_lng, min_lat],
                ]
            ],
        },
    }


def _evacuation_routes(value) -> dict:
    route_list = _route_geojson_list(value)
    features = []
    for route in route_list:
        if isinstance(route, dict) and route.get("type") == "FeatureCollection":
            features.extend(route.get("features", []))
        elif isinstance(route, dict) and route.get("type") == "Feature":
            features.append(route)
        elif isinstance(route, dict) and route.get("type") in {"LineString", "MultiLineString"}:
            features.append({"type": "Feature", "properties": {}, "geometry": route})
    return {"type": "FeatureCollection", "features": features}


def _route_geojson_list(value) -> list:
    parsed = _json_object(value)
    if parsed is None:
        return []
    if isinstance(parsed, dict):
        if parsed.get("type") in {"FeatureCollection", "Feature", "LineString", "MultiLineString"}:
            return [parsed]
        if isinstance(parsed.get("geojson"), dict):
            return [parsed["geojson"]]
        return []
    if isinstance(parsed, list):
        routes = []
        for route in parsed:
            if isinstance(route, dict) and isinstance(route.get("geojson"), dict):
                routes.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "name": route.get("name"),
                            "distance_km": route.get("distance_km"),
                            "status": route.get("status"),
                        },
                        "geometry": route["geojson"],
                    }
                )
            elif isinstance(route, dict):
                routes.extend(_route_geojson_list(route))
        return routes
    return []


def _overall_severity(event: dict, hazard_zones: list[dict]) -> str:
    status = str(event.get("status") or "").upper()
    severities = [str(zone.get("severity") or zone.get("risk_level") or "").upper() for zone in hazard_zones]
    for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        if status == level or level in severities:
            return level
    return "MEDIUM"


def _dominant_hazard_type(hazard_zones: list[dict]) -> str:
    for zone in hazard_zones:
        hazard_type = str(zone.get("hazard_type") or "").strip()
        if hazard_type:
            return hazard_type
    return ""


def _risk_for_type(hazard_zones: list[dict], hazard_type: str) -> str:
    matching = [
        str(zone.get("severity") or zone.get("risk_level") or "").upper()
        for zone in hazard_zones
        if hazard_type in str(zone.get("hazard_type") or "").lower()
    ]
    for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        if level in matching:
            return level
    return "LOW"


def _confidence_for_type(hazard_zones: list[dict], hazard_type: str, default: float | None) -> float | None:
    matching = [
        zone.get("overall_confidence")
        for zone in hazard_zones
        if hazard_type in str(zone.get("hazard_type") or "").lower()
    ]
    return _average_confidence(matching) if matching else default


def _schema_mismatch(exc: Exception) -> bool:
    name = type(exc).__name__
    text = str(exc).lower()
    return name in {"UndefinedColumnError", "UndefinedTableError"} or "column" in text and "does not exist" in text


def _safe_db_error(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    for name in ("NEON_DATABASE_URL",):
        value = os.getenv(name)
        if value:
            text = text.replace(value, "[redacted]")
    return text[:300]
