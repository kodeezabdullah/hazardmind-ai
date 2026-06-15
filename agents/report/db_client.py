import json
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent


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
    Read available upstream context for the Report Agent using a UUID event_id.
    """
    if not is_valid_uuid(event_id):
        raise ValueError("event_id must be a valid UUID")

    conn = await _connect()
    try:
        event = await _fetch_disaster_event(conn, event_id)
        satellite = await _fetch_satellite_result(conn, event_id)
        hazard_zones = await _fetch_hazard_zones(conn, event_id)
        impact = await _fetch_impact_data(conn, event_id)
        return _build_report_context(event_id, event, satellite, hazard_zones, impact)
    finally:
        await conn.close()


async def write_final_report_metadata(report: dict, total_time_secs: int | None = None) -> None:
    """
    Write to final_reports using Abdullah's final schema only.
    """
    event_id = str(report.get("event_id", ""))
    if not is_valid_uuid(event_id):
        raise ValueError("event_id must be a valid UUID")

    report_section = report.get("report", {})
    intelligence = report.get("intelligence", {})
    payload = _compact_agent_log_payload(report)
    confidence_level = _confidence_level(report)

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
                    total_time_secs = $6,
                    confidence_level = $7
                WHERE id = $1;
                """,
                existing_id,
                report_section.get("pdf_url"),
                report_section.get("map_url"),
                report_section.get("summary"),
                json.dumps(payload),
                total_time_secs,
                confidence_level,
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
                    total_time_secs,
                    confidence_level,
                    created_at
                )
                VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6, $7, NOW());
                """,
                event_id,
                report_section.get("pdf_url"),
                report_section.get("map_url"),
                report_section.get("summary"),
                json.dumps(payload),
                total_time_secs,
                confidence_level,
            )
    except Exception as exc:
        raise RuntimeError(f"Neon final_reports write failed: {type(exc).__name__}") from None
    finally:
        await conn.close()

    # Keep local variable referenced so linters do not flag context use when schemas evolve.
    _ = intelligence


async def _fetch_disaster_event(conn, event_id: str) -> dict:
    row = await conn.fetchrow(
        """
        SELECT event_id, location, disaster_type, magnitude, status, created_at, updated_at
        FROM disaster_events
        WHERE event_id = $1::uuid;
        """,
        event_id,
    )
    return _row_to_dict(row)


async def _fetch_satellite_result(conn, event_id: str) -> dict:
    row = await conn.fetchrow(
        """
        SELECT
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
            risk_cities
        FROM satellite_results
        WHERE event_id = $1::uuid
        ORDER BY created_at DESC NULLS LAST
        LIMIT 1;
        """,
        event_id,
    )
    return _row_to_dict(row)


async def _fetch_hazard_zones(conn, event_id: str) -> list[dict]:
    try:
        rows = await conn.fetch(
            """
            SELECT
                risk_level,
                hazard_type,
                area_km2,
                severity,
                confirmed_by,
                flood_depth_estimate,
                earthquake_mmi,
                landslide_probability,
                ST_AsGeoJSON(geometry)::json AS geometry
            FROM hazard_zones
            WHERE event_id = $1::uuid;
            """,
            event_id,
        )
    except Exception:
        rows = await conn.fetch(
            """
            SELECT
                risk_level,
                hazard_type,
                area_km2,
                severity,
                confirmed_by,
                flood_depth_estimate,
                earthquake_mmi,
                landslide_probability
            FROM hazard_zones
            WHERE event_id = $1::uuid;
            """,
            event_id,
        )
    return [_row_to_dict(row) for row in rows]


async def _fetch_impact_data(conn, event_id: str) -> dict:
    row = await conn.fetchrow(
        """
        SELECT
            total_affected,
            high_risk_people,
            medium_risk_people,
            hospitals_at_risk,
            schools_at_risk,
            roads_blocked,
            bridges_at_risk,
            vulnerability_score,
            evacuation_routes,
            estimated_evacuation_time
        FROM impact_data
        WHERE event_id = $1::uuid
        ORDER BY created_at DESC NULLS LAST
        LIMIT 1;
        """,
        event_id,
    )
    return _row_to_dict(row)


def _build_report_context(event_id: str, event: dict, satellite: dict, hazard_zones: list[dict], impact: dict) -> dict:
    hazard_features = [_hazard_feature(zone, index) for index, zone in enumerate(hazard_zones, start=1)]
    flood_confidence = _confidence_from_zones(hazard_zones, "flood")
    earthquake_confidence = _confidence_from_zones(hazard_zones, "earthquake")
    landslide_confidence = _confidence_from_zones(hazard_zones, "landslide")
    risk_cities = _json_list(satellite.get("risk_cities"))
    bbox = _bbox_from_satellite(satellite)
    return {
        "event_id": event_id,
        "location": event.get("location", ""),
        "hazard_type": event.get("disaster_type", "Unknown"),
        "overall_severity": _overall_severity(event, hazard_zones),
        "satellite": {
            "type": satellite.get("satellite_type", ""),
            "reason": "loaded_from_database",
            "cloud_cover": satellite.get("cloud_cover") or 0,
            "scene_id": satellite.get("scene_id", ""),
        },
        "boundaries": {
            "region_boundary": {"type": "FeatureCollection", "features": []},
            "risk_cities": risk_cities,
            "merged_polygon": {"type": "Feature", "properties": {}, "geometry": None},
            "bbox": bbox,
        },
        "artifacts": {
            "true_color_url": satellite.get("true_color_url", ""),
            "index_url": satellite.get("index_url", ""),
            "classification_url": satellite.get("classification_url", ""),
            "geojson_url": satellite.get("geojson_url", ""),
        },
        "analysis": {
            "index_type": "database_result",
            "mean_value": 0,
            "affected_area_km2": satellite.get("affected_area_km2") or 0,
            "damage_percent": satellite.get("damage_percent") or 0,
            "total_zones": satellite.get("total_zones") or len(hazard_features),
            "zones": {"type": "FeatureCollection", "features": hazard_features},
        },
        "hazard": {
            "flood_risk": _risk_for_type(hazard_zones, "flood"),
            "earthquake_risk": _risk_for_type(hazard_zones, "earthquake"),
            "landslide_risk": _risk_for_type(hazard_zones, "landslide"),
            "confidence_scores": {
                "flood": flood_confidence,
                "earthquake": earthquake_confidence,
                "landslide": landslide_confidence,
            },
        },
        "impact": {
            "population_affected": impact.get("total_affected") or 0,
            "high_risk_people": impact.get("high_risk_people") or 0,
            "medium_risk_people": impact.get("medium_risk_people") or 0,
            "hospitals_at_risk": impact.get("hospitals_at_risk") or 0,
            "schools_affected": impact.get("schools_at_risk") or 0,
            "roads_blocked_km": impact.get("roads_blocked") or 0,
            "bridges_at_risk": impact.get("bridges_at_risk") or 0,
            "vulnerability_score": impact.get("vulnerability_score") or 0,
            "critical_facilities": [],
            "estimated_evacuation_time": impact.get("estimated_evacuation_time"),
        },
        "routes": {
            "evacuation_routes": _evacuation_routes(impact.get("evacuation_routes")),
        },
    }


def _hazard_feature(zone: dict, index: int) -> dict:
    return {
        "type": "Feature",
        "properties": {
            "zone_id": f"DB-{index:02d}",
            "risk_level": zone.get("risk_level"),
            "hazard_type": zone.get("hazard_type"),
            "area_km2": zone.get("area_km2"),
            "severity": zone.get("severity") or zone.get("risk_level") or "unknown",
            "confirmed_by": zone.get("confirmed_by"),
            "flood_depth_estimate": zone.get("flood_depth_estimate"),
            "earthquake_mmi": zone.get("earthquake_mmi"),
            "landslide_probability": zone.get("landslide_probability"),
        },
        "geometry": zone.get("geometry"),
    }


def _compact_agent_log_payload(report: dict) -> dict:
    intelligence = report.get("intelligence", {})
    return {
        "agent_log": report.get("agent_log", []),
        "model_sources": report.get("model_sources", {}),
        "intelligence": intelligence,
        "quality_check": intelligence.get("quality_check", {}),
        "band_ready_message": intelligence.get("band_ready_message", {}),
    }


def _confidence_level(report: dict) -> str:
    criticality = report.get("intelligence", {}).get("criticality", {})
    confidence = criticality.get("overall_confidence")
    label = criticality.get("criticality") or report.get("overall_severity")
    if confidence is None:
        return str(label or "unknown")
    return f"{label}:{round(float(confidence) * 100)}%"


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
    return {key: _json_safe(value) for key, value in dict(row).items()}


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _json_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value else []
        return parsed if isinstance(parsed, list) else []
    return []


def _bbox_from_satellite(satellite: dict) -> list[float]:
    bbox = satellite.get("bbox") or satellite.get("bounds")
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox)
        except json.JSONDecodeError:
            bbox = None
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        return [float(value) for value in bbox]
    return [0, 0, 0, 0]


def _evacuation_routes(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"type": "FeatureCollection", "features": []}
        return parsed if isinstance(parsed, dict) else {"type": "FeatureCollection", "features": []}
    return {"type": "FeatureCollection", "features": []}


def _overall_severity(event: dict, hazard_zones: list[dict]) -> str:
    status = str(event.get("status") or "").upper()
    severities = [str(zone.get("severity") or zone.get("risk_level") or "").upper() for zone in hazard_zones]
    for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        if status == level or level in severities:
            return level
    return "MEDIUM"


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


def _confidence_from_zones(hazard_zones: list[dict], hazard_type: str) -> float:
    if not hazard_zones:
        return 0.0
    if any(hazard_type in str(zone.get("hazard_type") or "").lower() for zone in hazard_zones):
        return 0.75
    return 0.35
