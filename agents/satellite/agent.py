"""HazardMind satellite agent — LangGraph pipeline entry point.

Runs the full deterministic imagery pipeline for a disaster event:

    demo cache check
        -> region boundary
        -> risk-city detection + boundaries
        -> merged risk bbox
        -> Copernicus auth
        -> Sentinel selection
        -> scene search
        -> download / clip / export PNG
        -> upload to Cloudflare R2

Called directly as a LangGraph node (see node.py) with a full event_id, a
location and a disaster_type — no transport-layer indirection, no LLM
tool-call parsing. Every stage logs and the pipeline returns a
``status: error`` payload rather than raising, so a single failure surfaces
to the caller instead of crashing the graph.
"""

import asyncio
import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from boundary import (
    get_analysis_bbox,
    get_region_boundary,
    get_risk_city_boundaries,
    merge_risk_boundaries,
)
from confidence_tracker import ConfidenceTracker
from cross_validator import CrossValidator
from intelligence import SatelliteIntelligence
from processor import cleanup_event_temp, process_satellite_imagery
from r2_upload import check_demo_cache, upload_all_results
from sentinel import (
    authenticate_copernicus,
    backfill_uncovered_cities,
    search_imagery,
    select_satellite,
)

load_dotenv()

logger = logging.getLogger(__name__)

# event_ids the satellite has already fully analysed. Process-once guard: a
# duplicate call for the same event_id (e.g. a graph re-invoke) is a no-op
# rather than re-running the pipeline or re-persisting the result.
_completed_event_ids: set[str] = set()


def _persist_satellite_result(event_id: str, structured: dict) -> None:
    """Write the satellite result straight to the DB.

    The DB is the durable record downstream nodes/agents read from GET
    /results. Columns mirror satellite_results; jsonb columns are passed as
    JSON strings. Idempotent per event: an existing row is replaced.
    """
    db_url = os.getenv("NEON_DATABASE_URL")
    if not db_url:
        return
    try:
        import asyncpg

        def _f(k):
            v = structured.get(k)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _i(k):
            v = structured.get(k)
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        async def _write():
            conn = await asyncpg.connect(db_url)
            try:
                async with conn.transaction():
                    await conn.execute("DELETE FROM satellite_results WHERE event_id=$1", event_id)
                    await conn.execute(
                        """
                        INSERT INTO satellite_results
                            (event_id, satellite_type, cloud_cover, scene_id,
                             true_color_url, index_url, classification_url, geojson_url,
                             affected_area_km2, damage_percent, total_zones,
                             bounds, bbox, risk_cities)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                        """,
                        event_id,
                        structured.get("satellite_type"),
                        _f("cloud_cover"),
                        structured.get("scene_id"),
                        structured.get("true_color_url"),
                        structured.get("index_url"),
                        structured.get("classification_url"),
                        structured.get("geojson_url"),
                        _f("affected_area_km2"),
                        _f("damage_percent"),
                        _i("total_zones"),
                        json.dumps(structured.get("bounds")) if structured.get("bounds") is not None else None,
                        json.dumps(structured.get("bbox")) if structured.get("bbox") is not None else None,
                        json.dumps(structured.get("risk_cities")) if structured.get("risk_cities") is not None else None,
                    )
            finally:
                await conn.close()

        asyncio.run(_write())
        logger.info("Persisted satellite_results row for event %s", event_id)
    except Exception as exc:  # noqa: BLE001 - DB write is best-effort
        logger.warning("Could not persist satellite_results for %s: %s", event_id, exc)


# LLM intelligence layer (Featherless chain + Opus last resort). Shared across
# runs. Every method returns None on total failure, so the pipeline keeps
# working on its deterministic defaults if the LLMs are unreachable.
intelligence = SatelliteIntelligence()

# Cross-validation layer (GDACS / USGS / cloud / index / coverage / Featherless
# expert). Reuses the shared intelligence layer for its expert opinion. Each
# check is best-effort, so an unreachable feed never blocks a handoff.
cross_validator = CrossValidator(intelligence=intelligence)

# Max recovery attempts per failing step before we give up / alert a human.
MAX_STEP_ATTEMPTS = 3

# Below this overall confidence we treat the result as low-quality and ask the
# intelligence layer how to improve (integration point 6, quality gate).
MIN_CONFIDENCE = 0.6


# --------------------------------------------------------------------------- #
# Risk-city detection
# --------------------------------------------------------------------------- #
# The orchestrator gives us a location and disaster type; we infer which nearby
# cities are most at risk so we only download/process imagery over those areas.
# A small curated map covers the demo regions; anything else falls back to the
# location itself so the pipeline still runs.
_RISK_CITY_MAP = {
    ("peshawar, pakistan", "flood"): ["Peshawar", "Nowshera", "Charsadda"],
    ("dhaka, bangladesh", "flood"): ["Dhaka", "Narayanganj", "Gazipur"],
    ("kathmandu, nepal", "earthquake"): ["Kathmandu", "Lalitpur", "Bhaktapur"],
    ("kathmandu, nepal", "landslide"): ["Kathmandu", "Sindhupalchok"],
    # Mindanao is a whole island (~520x470 km); analysing it as one polygon
    # would clip a ~2.5-billion-pixel window and exhaust memory. The at-risk
    # population centres for the M7.8 scenario are these three scattered cities.
    ("mindanao, philippines", "earthquake"): [
        "Davao", "Cotabato", "Cagayan de Oro",
    ],
    ("mindanao, philippines", "landslide"): [
        "Davao", "Cotabato", "Cagayan de Oro",
    ],
}


def detect_risk_cities(location: str, disaster_type: str) -> list:
    """Infer the at-risk cities for a disaster.

    Looks up a curated map keyed by (location, disaster type); if there is no
    entry, falls back to the headline location itself so a boundary can still
    be resolved. The leading place token (before the first comma) is used as a
    sensible single-city fallback.
    """
    key = (location.strip().lower(), (disaster_type or "").strip().lower())
    cities = _RISK_CITY_MAP.get(key)
    if cities:
        return cities

    headline = location.split(",")[0].strip()
    logger.info(
        "No curated risk cities for %s/%s; falling back to %r",
        location,
        disaster_type,
        headline,
    )
    return [headline] if headline else []


# --------------------------------------------------------------------------- #
# Pipeline entry point
# --------------------------------------------------------------------------- #
class ProcessDisasterInput(BaseModel):
    """Run the satellite imagery pipeline for a disaster event and return the
    image URL, bbox, satellite type, region boundary and risk cities."""

    event_id: str = Field(..., description="Unique event id (uuid) for this disaster.")
    location: str = Field(
        ..., description='Affected location, e.g. "Peshawar, Pakistan".'
    )
    disaster_type: str = Field(
        ..., description="Disaster type: flood, earthquake, or landslide."
    )
    magnitude: Optional[float] = Field(
        None, description="Optional magnitude/severity of the event."
    )
    raw_message: Optional[str] = Field(
        None,
        description=(
            "The original disaster alert text, e.g. 'flood in Peshawar "
            "magnitude 6.2'. Passed through verbatim when available so the "
            "agent can parse it for structure and detect ambiguity."
        ),
    )


def _coerce_float(value) -> Optional[float]:
    """Return ``value`` as a float, or ``None`` if it isn't numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _error(event_id: str, message: str) -> str:
    """Build the error payload for the caller (LangGraph node)."""
    logger.error("Pipeline error for %s: %s", event_id, message)
    return json.dumps(
        {"event_id": event_id, "status": "error", "error": message}
    )


def _clarification(event_id: str, profile: dict) -> str:
    """Build a clarification-request payload for an ambiguous disaster message.

    Returned when the intelligence layer flags the parsed input as ambiguous;
    the caller can surface this so the orchestrator/backend can supply the
    missing details (integration point 1).
    """
    missing = profile.get("missing_info") or ["location", "disaster_type"]
    logger.info("Ambiguous input for %s; requesting clarification: %s", event_id, missing)
    return json.dumps(
        {
            "event_id": event_id,
            "status": "clarification_needed",
            "missing_info": missing,
            "parsed_profile": profile,
            "message": (
                "Ambiguous disaster alert — need clarification on: "
                + ", ".join(missing)
            ),
        }
    )


def _recover(anomaly_type: str, context: dict, attempt: int) -> Optional[dict]:
    """Ask the intelligence layer for a recovery strategy for an anomaly.

    Thin wrapper around ``intelligence.handle_anomaly`` that logs the anomaly
    and the recovered strategy (integration point 3). Returns the strategy dict
    or ``None`` if the LLM chain is unavailable.
    """
    logger.warning(
        "Anomaly '%s' (attempt %d/%d); context=%s",
        anomaly_type,
        attempt,
        MAX_STEP_ATTEMPTS,
        context,
    )
    strategy = intelligence.handle_anomaly(anomaly_type, context, attempt)
    if strategy is not None:
        logger.info(
            "Recovery for '%s': action=%s use_landsat=%s expand=%s reason=%s",
            anomaly_type,
            strategy.get("action"),
            strategy.get("use_landsat"),
            strategy.get("expand_date_range"),
            strategy.get("reasoning"),
        )
    return strategy


def _authenticate_with_recovery(event_id: str, location: str) -> Optional[str]:
    """Authenticate to CDSE, retrying up to MAX_STEP_ATTEMPTS with LLM recovery.

    On each failure, asks the intelligence layer for a recovery strategy
    (anomaly ``copernicus_auth_failed``) and respects its delay hint before the
    next attempt (integration point 3). Returns the token or ``None``.
    """
    import time

    for attempt in range(1, MAX_STEP_ATTEMPTS + 1):
        token = authenticate_copernicus()
        if token is not None:
            return token

        strategy = _recover(
            "copernicus_auth_failed",
            {"event_id": event_id, "location": location, "attempt": attempt},
            attempt,
        )
        if attempt == MAX_STEP_ATTEMPTS:
            break
        # Honour a (bounded) delay hint so we don't hammer the auth endpoint.
        delay = 0
        if strategy:
            try:
                delay = min(int(strategy.get("estimated_delay_seconds") or 0), 10)
            except (TypeError, ValueError):
                delay = 0
        if delay:
            logger.info("Waiting %ds before auth retry %d", delay, attempt + 1)
            time.sleep(delay)
    return None


def _search_with_recovery(
    event_id: str,
    bbox: tuple,
    satellite_type: str,
    merged: dict,
) -> Optional[list]:
    """Search for scenes, expanding the date window on the LLM's advice.

    If the initial 7-day search finds nothing, asks the intelligence layer to
    handle ``no_sentinel_scenes``; if it recommends widening the window we
    re-search over the larger range (integration point 3). Returns the ranked
    scene list, or ``None`` if nothing is ever found.
    """
    for attempt in range(1, MAX_STEP_ATTEMPTS + 1):
        date_range = 7 if attempt == 1 else (14 if attempt == 2 else 30)
        scenes = search_imagery(
            bbox,
            satellite_type,
            date_range=date_range,
            return_ranked=True,
            aoi_geom=merged,
        )
        if scenes:
            if attempt > 1:
                logger.info(
                    "Found %d scenes after widening to %d days", len(scenes), date_range
                )
            return scenes

        _recover(
            "no_sentinel_scenes",
            {
                "event_id": event_id,
                "satellite": satellite_type,
                "date_range_days": date_range,
                "bbox": list(bbox),
            },
            attempt,
        )
    return None


async def run_pipeline(params: ProcessDisasterInput) -> str:
    """Async entry point — runs the (blocking) pipeline off the event loop."""
    return await asyncio.to_thread(_run_pipeline_sync, params)


def _run_pipeline_sync(params: ProcessDisasterInput) -> str:
    """Execute the full satellite pipeline and return a JSON result string.

    Returns a JSON object with status "complete" (image_url, bbox,
    satellite_type, region_boundary, risk_cities), "error" (error message), or
    "clarification_needed" (ambiguous input). Never raises — failures are
    reported as a payload so the caller can propagate them into PipelineState.

    Six LLM integration points run alongside the deterministic pipeline:
      1. parse the raw message + detect ambiguity (ask for clarification)
      2. devise the satellite strategy (logged reasoning)
      3. anomaly recovery on auth / scene-search failures (max 3 attempts)
      4. expert interpretation of the raw GIS numbers
      5. a natural hand-off summary message (not raw JSON)
      6. a confidence quality gate before returning
    """
    event_id = params.event_id
    location = params.location
    disaster_type = params.disaster_type

    # Process-once guard: a repeat call for an already-analysed event_id
    # returns a short "already complete" WITHOUT re-running the pipeline.
    if event_id in _completed_event_ids:
        logger.info("event %s already processed — skipping duplicate call", event_id)
        return json.dumps(
            {
                "event_id": event_id,
                "status": "complete",
                "already_processed": True,
            }
        )

    # Running confidence ledger for this event. Cross-validation feeds it
    # evidence/concerns; the completion signal carries its overall score.
    tracker = ConfidenceTracker()

    logger.info(
        "Processing event %s: %s / %s (magnitude=%s)",
        event_id,
        location,
        disaster_type,
        params.magnitude,
    )

    try:
        # INTEGRATION POINT 1 — parse the raw message into a structured
        # profile and detect ambiguity. Best-effort: if the LLM chain is down
        # we keep the caller-supplied location/disaster_type as-is.
        profile = None
        raw = params.raw_message or f"{disaster_type} in {location}"
        profile = intelligence.parse_disaster_input(raw)
        if profile:
            logger.info("Parsed disaster profile: %s", json.dumps(profile, default=str))
            # Only ask for clarification when the model is unsure AND a core
            # field (location or disaster type) is genuinely absent — not just
            # mentioned in a low-stakes "missing_info" note. We treat a core
            # field as missing when the parsed value is empty OR the missing_info
            # list names it as a standalone token (e.g. "disaster_type", "city"),
            # avoiding spurious clarification loops on phrases like
            # "confirmation of disaster type".
            # The EXPLICIT caller args (params.location / params.disaster_type)
            # are authoritative. A core field is only genuinely missing when it
            # is absent from BOTH the explicit args AND the re-parsed profile —
            # so a confident dispatch ("flood in Rawalpindi") never triggers a
            # false clarification just because the secondary parse was unsure.
            missing = {m.strip().lower() for m in (profile.get("missing_info") or [])}
            _LOC_TOKENS = {"location", "city", "place"}
            _TYPE_TOKENS = {"disaster_type", "disaster type", "type"}
            have_location = bool(location) or bool(profile.get("location"))
            have_type = bool(disaster_type) or bool(profile.get("disaster_type"))
            loc_missing = (not have_location) or (
                not bool(location) and bool(missing & _LOC_TOKENS)
            )
            type_missing = (not have_type) or (
                not bool(disaster_type) and bool(missing & _TYPE_TOKENS)
            )
            if profile.get("ambiguous") and (loc_missing or type_missing):
                return _clarification(event_id, profile)
            # Enrich downstream inputs from the parsed profile where the
            # caller args were thin (keep explicit args authoritative).
            if not location and profile.get("location"):
                location = profile["location"]
            if not disaster_type and profile.get("disaster_type"):
                disaster_type = profile["disaster_type"]
        # (a) Region boundary (faded map background) — always resolved so the
        # frontend can draw the regional context, demo cache or not.
        region = get_region_boundary(location)
        if region is None:
            return _error(event_id, f"Could not resolve region boundary for {location!r}")

        # (b) Detect at-risk cities and resolve their boundaries.
        cities = detect_risk_cities(location, disaster_type)
        if not cities:
            return _error(event_id, f"No risk cities detected for {location!r}")

        city_polys = get_risk_city_boundaries(location, cities)
        if not city_polys:
            return _error(event_id, "Could not resolve any risk-city boundaries")

        merged = merge_risk_boundaries(city_polys)
        if merged is None:
            return _error(event_id, "Failed to merge risk-city boundaries")

        bbox = get_analysis_bbox(merged)
        if bbox is None:
            return _error(event_id, "Failed to compute analysis bbox")

        # (c) Demo cache short-circuit: reuse the pre-rendered classification PNG
        # but still report the boundaries resolved above for the map.
        cached_url = check_demo_cache(event_id)
        if cached_url:
            logger.info("Demo cache hit for %s", event_id)
            return json.dumps(
                {
                    "event_id": event_id,
                    "status": "complete",
                    "satellite_type": select_satellite(disaster_type)["satellite_type"],
                    "bbox": list(bbox),
                    "region_boundary": region.get("geojson"),
                    "risk_cities": [c["name"] for c in city_polys],
                    "classification_url": cached_url,
                    "image_url": cached_url,
                    "cached": True,
                }
            )

        # (d) Copernicus authentication (needed by select_satellite's cloud
        # peek). INTEGRATION POINT 3 — retry with LLM-guided recovery on
        # failure (anomaly copernicus_auth_failed, max 3 attempts).
        token = _authenticate_with_recovery(event_id, location)
        if token is None:
            return _error(event_id, "Copernicus authentication failed (after recovery)")

        # (e) Smart, cloud-aware Sentinel selection.
        selection = select_satellite(disaster_type, bbox=bbox, token=token)
        satellite_type = selection["satellite_type"]

        # INTEGRATION POINT 2 — devise the satellite strategy with full LLM
        # reasoning, logged. The deterministic cloud-aware selection stays
        # authoritative for the actual mission (physics over assumption), but
        # we honour the LLM's date-window when it asks for a wider search.
        strategy = intelligence.devise_satellite_strategy(
            profile or {"disaster_type": disaster_type, "location": location},
            cloud_cover=selection.get("cloud_cover"),
            available_scenes_count=0,  # pre-search; refined by recovery below
            attempt_number=1,
        )
        if strategy:
            logger.info(
                "Satellite strategy: satellite=%s date_range_days=%s reason=%s "
                "fallback=%s",
                strategy.get("satellite"),
                strategy.get("date_range_days"),
                strategy.get("reason"),
                strategy.get("fallback_strategy"),
            )

        # (f) Find candidate scenes over the bbox, ranked coverage-aware against
        # the actual risk polygon, so the pipeline can mosaic / fall back if the
        # best single tile is too sparse. INTEGRATION POINT 3 — widen the date
        # window on the LLM's advice when nothing is found.
        scenes = _search_with_recovery(event_id, bbox, satellite_type, merged)
        if not scenes:
            return _error(
                event_id,
                f"No {satellite_type} imagery found over bbox {bbox} "
                "(after widening the search window)",
            )

        # Backfill: a scattered city can be left uncovered when its only recent
        # tile is a partial acquisition that doesn't actually reach it. Re-query
        # a wider window per uncovered city so the mosaic can cover everyone.
        scenes = backfill_uncovered_cities(
            scenes, city_polys, satellite_type, aoi_geom=merged
        )

        # (g) Full remote-sensing pipeline (download -> stack -> clip ->
        # indices -> PNGs -> vectorize) over the real risk polygon. Pass the
        # per-city geometries so a mosaic spreads scenes across all scattered
        # cities (greedy set-cover) instead of bunching on the best-covered one.
        from shapely.geometry import shape as _shape

        city_geoms = []
        for cp in city_polys:
            try:
                city_geoms.append(_shape(cp["geojson"]))
            except (KeyError, ValueError, AttributeError, TypeError):
                continue

        result = process_satellite_imagery(
            selection, scenes, bbox, merged, event_id, token, disaster_type,
            city_geoms=city_geoms,
            # Per-city artifacts are intentionally disabled: re-clipping the full
            # mosaic to each city is very expensive on a large multi-tile AOI
            # (the merged whole-area clip already gives the frontend and hazard
            # agent everything they need). `city_geoms` is still passed so the
            # mosaic set-cover spreads scenes across the scattered cities.
            city_boundaries=None,
        )
        if result is None:
            return _error(event_id, "Satellite imagery processing failed")
        if result.get("status") == "coverage_insufficient":
            # INTEGRATION POINT 3 — let the LLM weigh in (it may recommend
            # Landsat). We surface the anomaly + its advice in the error so the
            # caller sees an actionable next step, not a bare failure.
            recovery = _recover(
                "coverage_insufficient",
                {
                    "event_id": event_id,
                    "best_valid_percent": result.get("best_valid_percent"),
                    "min_required_percent": result.get("min_required_percent"),
                    "disaster_type": disaster_type,
                    "location": location,
                },
                MAX_STEP_ATTEMPTS,
            )
            note = ""
            if recovery and recovery.get("alert_human"):
                note = f" | {recovery.get('alert_message', '')}"
            return _error(
                event_id,
                "coverage_insufficient: no scene covers enough of the risk "
                f"area (best {result.get('best_valid_percent')}% valid pixels, "
                f"need >= {result.get('min_required_percent')}%)" + note,
            )

        # (h) Upload all artifacts to Cloudflare R2 (merged AOI).
        urls = upload_all_results(
            event_id,
            {
                "true_color": result["png_paths"].get("true_color"),
                "index_map": result["png_paths"].get("index_map"),
                "classification": result["png_paths"].get("classification"),
                "geojson": result["geojson"],
            },
        )

        # (h.2) Per-city artifacts (multi-city AOIs). Each city's PNGs + GeoJSON
        # were rendered from the same mosaic and namespaced under
        # <event_id>/cities/<slug>/; upload each set under the matching R2 prefix
        # and surface a compact per-city summary + URLs for downstream consumers.
        cities_payload = []
        for city in result.get("cities", []) or []:
            slug = city.get("slug") or "city"
            city_urls = upload_all_results(
                f"{event_id}/cities/{slug}",
                {
                    "true_color": city["png_paths"].get("true_color"),
                    "index_map": city["png_paths"].get("index_map"),
                    "classification": city["png_paths"].get("classification"),
                    "geojson": city["geojson"],
                },
            )
            cities_payload.append(
                {
                    "name": city.get("name"),
                    "slug": slug,
                    "affected_area_km2": city.get("affected_area_km2"),
                    "water_percent": city.get("water_percent"),
                    "mean_index": city.get("mean_index"),
                    "class_counts": city.get("class_counts"),
                    "valid_percent": city.get("valid_percent"),
                    "bounds": city.get("bounds"),
                    "true_color_url": city_urls["true_color_url"],
                    "index_url": city_urls["index_url"],
                    "classification_url": city_urls["classification_url"],
                    "geojson_url": city_urls["geojson_url"],
                }
            )
        if cities_payload:
            logger.info(
                "Uploaded %d per-city artifact set(s) for %s",
                len(cities_payload),
                event_id,
            )

        # (h.3) R2 upload done — drop this event's extracted bands + PNGs from
        # the temp dir. The downloaded .zip product archives are kept (see
        # cleanup_event_temp) so a re-process reuses the cached download. Runs
        # from this sync pipeline via asyncio.run; failures are non-fatal.
        asyncio.run(cleanup_event_temp(event_id))

        # CROSS-VALIDATION — check the satellite result against every reachable
        # external source (GDACS / USGS / cloud / index physics / coverage /
        # Featherless expert), feeding evidence + concerns into the confidence
        # tracker. The bbox centroid drives the geographic feed lookups. Never
        # raises — a failing feed is skipped.
        validation_input = {
            "affected_area_km2": result.get("affected_area_km2"),
            "cloud_cover": selection.get("cloud_cover"),
            "mean_ndwi": result.get("mean_index"),
            "mean_index": result.get("mean_index"),
            "water_percent": result.get("water_percent"),
            "coverage_percent": result.get("valid_percent"),
            "valid_percent": result.get("valid_percent"),
        }
        validations = cross_validator.validate_all(
            validation_input, disaster_type, bbox, tracker
        )
        logger.info(
            "Cross-validation: %d findings, confidence=%.2f, alert=%s",
            len(validations),
            tracker.overall_confidence(),
            tracker.should_alert_team(),
        )

        # INTEGRATION POINT 4 — expert interpretation of the raw GIS numbers.
        index_stats = {
            "mean_index": result.get("mean_index"),
            "water_percent": result.get("water_percent"),
            "class_counts": result.get("class_counts"),
            "valid_percent": result.get("valid_percent"),
        }
        total_zones = 0
        try:
            total_zones = len(result["geojson"].get("features", []))
        except (KeyError, AttributeError, TypeError):
            total_zones = 0

        interpretation = intelligence.interpret_results(
            index_type=result["index_type"],
            index_stats=index_stats,
            disaster_type=disaster_type,
            location=location,
            total_zones=total_zones,
            area_km2=result["affected_area_km2"],
            satellite_used=satellite_type,
        )
        if interpretation:
            logger.info(
                "Interpretation: severity=%s data_quality=%s confidence=%s",
                interpretation.get("severity"),
                interpretation.get("data_quality"),
                interpretation.get("confidence"),
            )

        # Fold the interpreter's self-rated confidence into the cross-validation
        # ledger as one more weighted source, then use the tracker's overall
        # score as the authoritative confidence for the gate + handoff. This
        # blends the expert read with the hard external checks rather than
        # trusting the LLM's number alone.
        interp_conf = _coerce_float((interpretation or {}).get("confidence"))
        if interp_conf is not None:
            tracker.add_evidence("interpretation", interp_conf, weight=0.2)
        confidence = round(tracker.overall_confidence(), 4)
        anomalies = (interpretation or {}).get("anomalies") or []

        # INTEGRATION POINT 6 — confidence quality gate. Below MIN_CONFIDENCE the
        # result is low-quality: ask the LLM how to improve / whether to alert a
        # human, and flag that the team should verify before relying on it. We
        # still return the result (people need the data), but the advice is
        # logged + surfaced.
        needs_verification = tracker.needs_verification()
        should_alert = tracker.should_alert_team()
        if confidence < MIN_CONFIDENCE or needs_verification or should_alert:
            _recover(
                "low_confidence",
                {
                    "event_id": event_id,
                    "confidence": confidence,
                    "anomalies": anomalies,
                    "concerns": tracker.concerns,
                    "index_stats": index_stats,
                },
                MAX_STEP_ATTEMPTS,
            )

        logger.info(
            "Pipeline complete for %s (%s, %.2f km^2 affected, confidence=%s)",
            event_id,
            satellite_type,
            result["affected_area_km2"],
            confidence,
        )

        # (i) Structured result for downstream nodes (full machine-readable
        # payload — mirrors satellite_results DB columns plus extras).
        structured = {
            "event_id": event_id,
            "status": "complete",
            "satellite_type": satellite_type,
            "cloud_cover": selection.get("cloud_cover"),
            "selection_reason": selection.get("reason"),
            "index_type": result["index_type"],
            "water_percent": result["water_percent"],
            "mean_index": result["mean_index"],
            "class_counts": result.get("class_counts"),
            "affected_area_km2": result["affected_area_km2"],
            "bbox": list(bbox),
            # Geographic extent of the PNG layers, for map overlay. Shapes
            # for Leaflet (bounds_leaflet) and MapLibre (bounds_corners).
            "bounds": result.get("bounds"),
            "region_boundary": region.get("geojson"),
            "risk_cities": [c["name"] for c in city_polys],
            "true_color_url": urls["true_color_url"],
            "index_url": urls["index_url"],
            "classification_url": urls["classification_url"],
            "geojson_url": urls["geojson_url"],
            "image_url": urls["classification_url"] or urls["true_color_url"],
            "cached": False,
            # Per-city artifacts + summaries (multi-city AOIs). Each entry has
            # its own PNGs/GeoJSON URLs and bounds, so downstream consumers and
            # the frontend can show individual city layers, not just the merged
            # one.
            "cities": cities_payload,
            # Expert reasoning from the intelligence layer (point 4).
            "interpretation": interpretation,
            # Confidence is the cross-validation tracker's overall score (a
            # weighted blend of external checks + the expert interpretation),
            # not the LLM's self-rating alone.
            "confidence": confidence,
            # Cross-validation: concerns raised, per-source findings, and the
            # two action flags the caller cares about.
            "concerns": tracker.concerns,
            "validations": validations,
            "needs_verification": needs_verification,
            "should_alert": should_alert,
        }

        # INTEGRATION POINT 5 — a natural, expert-sounding hand-off summary
        # (not raw JSON). The structured payload above still carries the
        # numbers for any consumer that needs them.
        band_message = intelligence.generate_band_message(
            results={
                "event_id": event_id,
                "satellite_type": satellite_type,
                "index_type": result["index_type"],
                "affected_area_km2": result["affected_area_km2"],
                "water_percent": result["water_percent"],
                "class_counts": result.get("class_counts"),
                "total_zones": total_zones,
                "location": location,
            },
            interpretation=interpretation,
            # Surface both the interpreter's anomalies and the cross-validation
            # concerns so the natural summary flags what we're unsure of.
            anomalies=anomalies
            + [f"{c['severity']}: {c['concern']}" for c in tracker.concerns],
            confidence=confidence,
            next_agent_handle="hazard",
        )
        if band_message:
            structured["summary_message"] = band_message
            logger.info("Generated natural hand-off summary (%d chars)", len(band_message))

        # Persist the authoritative result to the DB — the durable record
        # downstream nodes/agents and GET /results read from.
        _persist_satellite_result(event_id, structured)

        _completed_event_ids.add(event_id)
        return json.dumps(structured)
    except Exception as exc:  # noqa: BLE001 - report any failure to the caller.
        return _error(event_id, f"Unexpected error: {exc}")
