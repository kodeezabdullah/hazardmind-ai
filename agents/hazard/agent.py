"""HazardMind hazard agent — pipeline entry point.

Runs multi-hazard analysis (flood NDWI, earthquake USGS, landslide DEM slope)
over the satellite result and writes hazard_zones (3 rows: flood/earthquake/
landslide). Called directly as a LangGraph node (see node.py) with a full
event_id and the satellite's result dict — no transport-layer indirection.
"""

import json
import os
from datetime import datetime, timezone

import asyncpg
from dotenv import load_dotenv

from analyzer import run_parallel_analysis
from intelligence import quality_check

load_dotenv()

NEON_DATABASE_URL = os.getenv("NEON_DATABASE_URL")


def _normalise_satellite_payload(payload: dict, event_id: str) -> dict:
    """Map the satellite's FLAT completion payload into the nested shape the
    analyzer reads. Accepts both the flat form (what the satellite actually
    sends) and the already-nested form (fallback), so it is backward compatible.

    Satellite (flat)            ->  analyzer (nested)
      bbox                          boundaries.bbox
      risk_cities                   boundaries.risk_cities
      affected_area_km2             analysis.affected_area_km2
      mean_index (NDWI)             analysis.mean_value
      water_percent                 analysis.water_percent
      satellite_type               satellite.type
    """
    p = payload or {}

    # If the payload nests the LLM-parsed result under "data", unwrap it first
    # (kept for backward compatibility with an already-nested caller).
    if isinstance(p.get("data"), dict) and (
        "bbox" in p["data"] or "affected_area_km2" in p["data"]
    ):
        p = {**p, **p["data"]}

    nested_boundaries = p.get("boundaries") if isinstance(p.get("boundaries"), dict) else {}
    nested_analysis = p.get("analysis") if isinstance(p.get("analysis"), dict) else {}
    nested_satellite = p.get("satellite") if isinstance(p.get("satellite"), dict) else {}

    bbox = nested_boundaries.get("bbox") or p.get("bbox") or []
    risk_cities = nested_boundaries.get("risk_cities") or p.get("risk_cities") or []

    affected_area = (
        nested_analysis.get("affected_area_km2")
        if nested_analysis.get("affected_area_km2") is not None
        else p.get("affected_area_km2", 0.0)
    )
    # The satellite calls the index "mean_index"; the analyzer reads "mean_value".
    mean_value = (
        nested_analysis.get("mean_value")
        if nested_analysis.get("mean_value") is not None
        else p.get("mean_index", p.get("mean_value", 0.0))
    )
    water_percent = (
        nested_analysis.get("water_percent")
        if nested_analysis.get("water_percent") is not None
        else p.get("water_percent")
    )

    sat_type = (
        nested_satellite.get("type")
        or p.get("satellite_type")
        or (p.get("satellite") if isinstance(p.get("satellite"), str) else None)
        or "sentinel-2"
    )

    return {
        "event_id": event_id,
        "boundaries": {"bbox": list(bbox) if bbox else [], "risk_cities": risk_cities},
        "analysis": {
            "affected_area_km2": affected_area,
            "mean_value": mean_value,
            "water_percent": water_percent,
            "index_type": p.get("index_type"),
            "confidence": p.get("confidence"),
            "needs_verification": p.get("needs_verification"),
        },
        "artifacts": {
            "true_color_url": p.get("true_color_url"),
            "index_url": p.get("index_url"),
            "classification_url": p.get("classification_url"),
            "geojson_url": p.get("geojson_url"),
        },
        "satellite": {"type": sat_type},
    }


async def write_to_db(result: dict) -> None:
    """Write hazard results to the hazard_zones table (matches shared/db/schema.sql).

    The schema is one row per hazard type, so we write a flood, earthquake, and
    landslide row for the event. Columns: risk_level, hazard_type, severity,
    confirmed_by, flood_depth_estimate, earthquake_mmi, landslide_probability,
    overall_confidence.
    """
    confidence_scores = result.get("confidence_scores", {})
    severity = result["overall_severity"]
    confirmed_by = json.dumps(confidence_scores)

    rows = [
        {
            "hazard_type": "flood",
            "risk_level": result["flood_risk"],
            "overall_confidence": confidence_scores.get("flood", 0.0),
            "flood_depth_estimate": result.get("flood_depth_estimate"),
            "earthquake_mmi": None,
            "landslide_probability": None,
        },
        {
            "hazard_type": "earthquake",
            "risk_level": result["earthquake_risk"],
            "overall_confidence": confidence_scores.get("earthquake", 0.0),
            "flood_depth_estimate": None,
            "earthquake_mmi": result.get("earthquake_mmi"),
            "landslide_probability": None,
        },
        {
            "hazard_type": "landslide",
            "risk_level": result["landslide_risk"],
            "overall_confidence": confidence_scores.get("landslide", 0.0),
            "flood_depth_estimate": None,
            "earthquake_mmi": None,
            "landslide_probability": result.get("landslide_probability"),
        },
    ]

    created_at = datetime.now(timezone.utc)
    conn = await asyncpg.connect(NEON_DATABASE_URL)
    try:
        for row in rows:
            await conn.execute(
                """
                INSERT INTO hazard_zones (
                    event_id, risk_level, hazard_type, severity,
                    confirmed_by, flood_depth_estimate, earthquake_mmi,
                    landslide_probability, overall_confidence, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (event_id, hazard_type) DO UPDATE SET
                    risk_level = EXCLUDED.risk_level,
                    severity = EXCLUDED.severity,
                    confirmed_by = EXCLUDED.confirmed_by,
                    flood_depth_estimate = EXCLUDED.flood_depth_estimate,
                    earthquake_mmi = EXCLUDED.earthquake_mmi,
                    landslide_probability = EXCLUDED.landslide_probability,
                    overall_confidence = EXCLUDED.overall_confidence,
                    created_at = EXCLUDED.created_at
                """,
                result["event_id"],
                row["risk_level"],
                row["hazard_type"],
                severity,
                confirmed_by,
                row["flood_depth_estimate"],
                row["earthquake_mmi"],
                row["landslide_probability"],
                row["overall_confidence"],
                created_at,
            )
    finally:
        await conn.close()


async def analyze_hazard(satellite_payload: dict, event_id: str) -> dict:
    """Run multi-hazard analysis and write hazard_zones. Returns the result payload.

    Never raises — failures are reported as a ``status: error`` payload so the
    caller (the LangGraph node) can propagate them into PipelineState.
    """
    satellite_payload = dict(satellite_payload or {})
    satellite_payload["event_id"] = event_id
    try:
        # CONTRACT ADAPTER: the satellite emits a FLAT payload — bbox /
        # affected_area_km2 / mean_index / water_percent / risk_cities /
        # satellite_type all live at the TOP LEVEL. The analyzer, however, reads
        # the NESTED shape (boundaries.bbox, analysis.affected_area_km2,
        # analysis.mean_value, satellite.type). Without normalising, the
        # analyzer sees an empty bbox -> "invalid bbox" -> every risk UNKNOWN
        # and a hardcoded HIGH severity (a non-disaster stamped as a disaster).
        satellite_data = _normalise_satellite_payload(satellite_payload, event_id)

        raw_result = await run_parallel_analysis(satellite_data)

        qc = await quality_check(raw_result)
        if not qc["passed"]:
            return {
                "agent": "hazardmind-hazard",
                "event_id": event_id,
                "status": "error",
                "error": "quality check failed",
            }

        await write_to_db(raw_result)

        payload = {
            "agent": "hazardmind-hazard",
            "event_id": event_id,
            "status": "complete",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "hazard": {
                "flood_risk": raw_result["flood_risk"],
                "earthquake_risk": raw_result["earthquake_risk"],
                "landslide_risk": raw_result["landslide_risk"],
                "overall_severity": raw_result["overall_severity"],
                "confidence_scores": raw_result["confidence_scores"],
                "risk_polygons": {},
                "risk_polygons_url": "",
            },
            "error": None,
        }
        return payload

    except Exception as e:
        return {
            "agent": "hazardmind-hazard",
            "event_id": event_id,
            "status": "error",
            "error": str(e),
        }
