"""HazardMind hazard agent — pipeline entry point.

Runs multi-hazard analysis (flood NDWI, earthquake USGS, landslide DEM slope)
over the satellite result and writes hazard_zones (3 rows: flood/earthquake/
landslide). Called directly as a LangGraph node (see node.py) with a full
event_id and the satellite's result dict — no transport-layer indirection.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from analyzer import run_parallel_analysis
from intelligence import quality_check

# Load THIS agent's own .env explicitly (not cwd-relative), override=False so a
# parent-process variable (e.g. the e2e harness's local NEON_DATABASE_URL) wins.
# See the satellite agent for the full rationale.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

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
            # GATE B (2026-07-28): confirmed by direct read that this adapter
            # did NOT previously carry index_calibrated/index_units into the
            # normalized payload — satellite's ANALYSIS.md was right, hazard's
            # ANALYSIS.md's claim that it did was wrong. Added here so the
            # deterministic flood fallback (analyzer.py) can branch on real
            # calibration status instead of inferring it from satellite_type
            # (H#4 / SYSTEM_ANALYSIS.md C, H#4).
            "index_calibrated": p.get("index_calibrated"),
            "index_units": p.get("index_units"),
            "confidence": p.get("confidence"),
            # confidence_basis/evidence_count: satellite computes these (see
            # its ANALYSIS.md) but nothing downstream previously saw them —
            # small additive carry-through, not otherwise used yet.
            "confidence_basis": p.get("confidence_basis"),
            "evidence_count": p.get("evidence_count"),
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


async def write_to_db(result: dict, primary_hazard_risk: str | None = None) -> None:
    """Write hazard results to the hazard_zones table (matches shared/db/schema.sql).

    The schema is one row per hazard type, so we write a flood, earthquake, and
    landslide row for the event. Columns: risk_level, hazard_type, severity,
    confirmed_by, flood_depth_estimate, earthquake_mmi, landslide_probability,
    overall_confidence, diagnostics.

    ``primary_hazard_risk`` (H#10): the caller now computes this BEFORE
    calling write_to_db (agent.analyze_hazard, ordering fix per
    TESTING_GAP_AUDIT.md/test_field_survival.py's
    test_primary_hazard_risk_reaches_payload_but_not_confirmed_by finding —
    previously write_to_db(raw_result) ran before primary_hazard_risk was
    even computed, so it could never reach confirmed_by/diagnostics no
    matter what this function did). Optional so any other caller that still
    only wants the pre-existing confirmed_by shape keeps working unchanged.
    """
    confidence_scores = result.get("confidence_scores", {})
    evidence_basis = result.get("evidence_basis") or {}
    raw_diagnostics = result.get("raw_diagnostics") or {}
    severity = result["overall_severity"]
    satellite_confidence = result.get("satellite_confidence")
    confidence_cap_applied = result.get("confidence_cap_applied")

    # confirmed_by carries confidence_scores (unchanged) plus, additively, the
    # evidence provenance (real DEM slope / USGS fetch status) for the
    # per-row hazard_type this JSON blob belongs to — a LOW/UNKNOWN verdict is
    # otherwise indistinguishable from one produced by a fetch failure's
    # conservative default (see analyzer.analyze_landslide/analyze_earthquake).
    def _confirmed_by(hazard_type: str) -> str:
        return json.dumps(
            {
                "confidence_scores": confidence_scores,
                "evidence_basis": evidence_basis.get(hazard_type),
            }
        )

    # diagnostics (durable-evidence-trail, feat/durable-evidence-trail): the
    # cross-cutting fields that were previously computed but never reached
    # confirmed_by (satellite_confidence, confidence_cap_applied,
    # primary_hazard_risk — see TESTING_GAP_AUDIT.md), plus the full raw
    # third-party evidence trace for THIS row's hazard_type (dem_query/
    # dem_samples/... for landslide, usgs_query/events_returned/... for
    # earthquake, index/threshold detail for flood), so any deterministic
    # decision is re-derivable from the DB row alone.
    def _diagnostics(hazard_type: str) -> str:
        return json.dumps(
            {
                "satellite_confidence": satellite_confidence,
                "confidence_cap_applied": confidence_cap_applied,
                "primary_hazard_risk": primary_hazard_risk,
                "raw": raw_diagnostics.get(hazard_type),
            }
        )

    rows = [
        {
            "hazard_type": "flood",
            "risk_level": result["flood_risk"],
            "overall_confidence": confidence_scores.get("flood", 0.0),
            "flood_depth_estimate": result.get("flood_depth_estimate"),
            "earthquake_mmi": None,
            "landslide_probability": None,
            "confirmed_by": _confirmed_by("flood"),
            "diagnostics": _diagnostics("flood"),
        },
        {
            "hazard_type": "earthquake",
            "risk_level": result["earthquake_risk"],
            "overall_confidence": confidence_scores.get("earthquake", 0.0),
            "flood_depth_estimate": None,
            "earthquake_mmi": result.get("earthquake_mmi"),
            "landslide_probability": None,
            "confirmed_by": _confirmed_by("earthquake"),
            "diagnostics": _diagnostics("earthquake"),
        },
        {
            "hazard_type": "landslide",
            "risk_level": result["landslide_risk"],
            "overall_confidence": confidence_scores.get("landslide", 0.0),
            "flood_depth_estimate": None,
            "earthquake_mmi": None,
            "landslide_probability": result.get("landslide_probability"),
            "confirmed_by": _confirmed_by("landslide"),
            "diagnostics": _diagnostics("landslide"),
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
                    landslide_probability, overall_confidence, diagnostics,
                    created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (event_id, hazard_type) DO UPDATE SET
                    risk_level = EXCLUDED.risk_level,
                    severity = EXCLUDED.severity,
                    confirmed_by = EXCLUDED.confirmed_by,
                    flood_depth_estimate = EXCLUDED.flood_depth_estimate,
                    earthquake_mmi = EXCLUDED.earthquake_mmi,
                    landslide_probability = EXCLUDED.landslide_probability,
                    overall_confidence = EXCLUDED.overall_confidence,
                    diagnostics = EXCLUDED.diagnostics,
                    created_at = EXCLUDED.created_at
                """,
                result["event_id"],
                row["risk_level"],
                row["hazard_type"],
                severity,
                row["confirmed_by"],
                row["flood_depth_estimate"],
                row["earthquake_mmi"],
                row["landslide_probability"],
                row["overall_confidence"],
                row["diagnostics"],
                created_at,
            )
    finally:
        await conn.close()


_PRIMARY_RISK_KEY = {
    "flood": "flood_risk",
    "earthquake": "earthquake_risk",
    "landslide": "landslide_risk",
}


async def analyze_hazard(satellite_payload: dict, event_id: str, disaster_type: str = "flood") -> dict:
    """Run multi-hazard analysis and write hazard_zones. Returns the result payload.

    Never raises — failures are reported as a ``status: error`` payload so the
    caller (the LangGraph node) can propagate them into PipelineState.

    ``disaster_type`` is the dispatch's ACTUAL disaster type (from
    PipelineState, set once by the backend). All three hazard types are still
    analyzed unconditionally (existing behavior, unchanged) but the result now
    also surfaces ``primary_hazard_risk`` — an unambiguous field naming the
    risk level for the disaster actually being assessed, so downstream
    consumers (impact) don't have to guess via a flood_risk-first fallback
    chain that silently reads LOW/UNKNOWN on a real non-flood event (H#10).
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
                "error": f"quality check failed: {qc.get('checks')}",
            }

        # insufficient_data (H#3): a plumbing failure (e.g. invalid bbox from
        # satellite), not a completed analysis. Surface it distinctly so the
        # graph node can propagate the real error into PipelineState["errors"]
        # and record an anomaly, instead of silently presenting a fabricated
        # HIGH severity as if it were a real verdict. Still write the honest
        # UNKNOWN/0.0-confidence rows to hazard_zones so the DB reflects
        # "assessed, could not determine" rather than having no row at all.
        if raw_result.get("status") == "insufficient_data":
            await write_to_db(raw_result, primary_hazard_risk="UNKNOWN")
            return {
                "agent": "hazardmind-hazard",
                "event_id": event_id,
                "status": "insufficient_data",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "hazard": {
                    "disaster_type": disaster_type,
                    "flood_risk": raw_result["flood_risk"],
                    "earthquake_risk": raw_result["earthquake_risk"],
                    "landslide_risk": raw_result["landslide_risk"],
                    "primary_hazard_risk": "UNKNOWN",
                    "overall_severity": raw_result["overall_severity"],
                    "confidence_scores": raw_result["confidence_scores"],
                    "risk_polygons": {},
                    "risk_polygons_url": "",
                },
                "error": raw_result.get("error") or "insufficient data for hazard analysis",
            }

        primary_key = _PRIMARY_RISK_KEY.get(str(disaster_type or "").lower(), "flood_risk")
        primary_hazard_risk = raw_result.get(primary_key, "UNKNOWN")

        # ORDERING FIX (durable-evidence-trail, 2026-07-28): primary_hazard_risk
        # must be computed BEFORE write_to_db so it can actually reach
        # confirmed_by/diagnostics. Previously write_to_db(raw_result) ran
        # first (see the insufficient_data branch above, which still has this
        # ordering since it hardcodes "UNKNOWN" as its own primary_hazard_risk
        # and needs no computed value) — on the success path this meant
        # primary_hazard_risk could never reach the DB row no matter what
        # write_to_db did with it, per TESTING_GAP_AUDIT.md's finding.
        await write_to_db(raw_result, primary_hazard_risk=primary_hazard_risk)

        payload = {
            "agent": "hazardmind-hazard",
            "event_id": event_id,
            "status": "complete",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "hazard": {
                "disaster_type": disaster_type,
                "flood_risk": raw_result["flood_risk"],
                "earthquake_risk": raw_result["earthquake_risk"],
                "landslide_risk": raw_result["landslide_risk"],
                # The risk level for the ACTUAL disaster this event was
                # dispatched to assess — additive, does not replace the
                # per-hazard-type fields above (H#10 fix).
                "primary_hazard_risk": primary_hazard_risk,
                "overall_severity": raw_result["overall_severity"],
                "confidence_scores": raw_result["confidence_scores"],
                "risk_polygons": {},
                "risk_polygons_url": "",
                "anomalies": raw_result.get("anomalies") or [],
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
