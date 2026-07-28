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
from pathlib import Path
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
from processor import (
    DEFAULT_MAX_DOWNLOAD_GB as _PEEK_DEFAULT_MAX_DOWNLOAD_GB,
    _bytes_downloaded_total as _processor_bytes_downloaded_total,
    cleanup_event_temp,
    memory_report as _processor_memory_report,
    peek_aoi_cloud_percent,
    peek_needed,
    process_satellite_imagery,
)
from r2_upload import check_demo_cache, upload_all_results
from sentinel import (
    SENTINEL_2,
    TokenManager,
    authenticate_copernicus,
    backfill_uncovered_cities,
    search_imagery,
    select_satellite,
)

# Load THIS agent's own .env explicitly (not cwd-relative), and never clobber a
# variable already set by a parent process. In production each agent runs in its
# own container/cwd so a bare load_dotenv() happened to work; when the whole
# graph runs in ONE process (e.g. the e2e harness) a cwd-relative load would pull
# the wrong agent's .env or none at all. override=False lets an already-exported
# parent value (e.g. the harness pointing NEON_DATABASE_URL at local Postgres) win.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

logger = logging.getLogger(__name__)

# event_ids the satellite has already fully analysed. Process-once guard: a
# duplicate call for the same event_id (e.g. a graph re-invoke) is a no-op
# rather than re-running the pipeline or re-persisting the result.
_completed_event_ids: set[str] = set()


# Persist retry policy: a transient Neon outage must not be reported as a
# successful "complete" run with no durable row (the pipeline's success claim
# has to match what actually landed in the DB, not what merely computed
# successfully in memory).
PERSIST_MAX_ATTEMPTS = 3
PERSIST_RETRY_BACKOFF_SECONDS = (1, 3)  # delay before attempt 2, before attempt 3


def _persist_satellite_result(event_id: str, structured: dict) -> Optional[str]:
    """Write the satellite result straight to the DB, retrying on failure.

    The DB is the durable record downstream nodes/agents read from GET
    /results. Columns mirror satellite_results; jsonb columns are passed as
    JSON strings. Idempotent per event: an existing row is replaced.

    Retries up to PERSIST_MAX_ATTEMPTS times with backoff. Returns None on
    success, or an error string describing the final failure — the caller
    must treat a non-None return as a hard pipeline failure (status:"failed"),
    never as "complete", since a "complete" status with no durable row is
    silently indistinguishable from a real success to any DB-reading consumer.
    """
    import time

    db_url = os.getenv("NEON_DATABASE_URL")
    if not db_url:
        return "NEON_DATABASE_URL is not configured"

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

    import asyncpg

    # Durable evidence trail (feat/durable-evidence-trail, 2026-07-28):
    # diagnostics carries everything that is read but not filtered/sorted/
    # aggregated on — concerns, validations, needs_verification,
    # should_alert, artifacts_incomplete, failed_artifacts, gap detail, gap
    # geometry, coverage_tier, temporal_spread_days, acquisition_count,
    # bytes_downloaded, processing_level. Real (queryable) columns are used
    # for anything the API/report layer would filter/sort/aggregate on
    # (confidence, coverage_percent, coverage_status, scene_age_days,
    # index_calibrated/index_units, selection_reason, scene_cloud_percent/
    # aoi_cloud_percent, scl_reused).
    diagnostics = {
        "concerns": structured.get("concerns"),
        "validations": structured.get("validations"),
        "needs_verification": structured.get("needs_verification"),
        "should_alert": structured.get("should_alert"),
        "artifacts_incomplete": structured.get("artifacts_incomplete"),
        "failed_artifacts": structured.get("failed_artifacts"),
        "gap_count": structured.get("gap_count"),
        "gap_area_km2": structured.get("gap_area_km2"),
        "gap_attribution": structured.get("gap_attribution"),
        "gap_limited_by": structured.get("gap_limited_by"),
        "gaps": structured.get("gaps"),
        "coverage_tier": structured.get("coverage_tier"),
        "temporal_spread_days": structured.get("temporal_spread_days"),
        "acquisition_count": structured.get("acquisition_count"),
        "bytes_downloaded": structured.get("bytes_downloaded"),
        "processing_level": structured.get("processing_level"),
    }

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
                         affected_area_km2, total_zones,
                         bounds, bbox, risk_cities, scene_age_days,
                         confidence, confidence_basis, evidence_count,
                         coverage_percent, coverage_status,
                         index_calibrated, index_units, selection_reason,
                         scene_cloud_percent, aoi_cloud_percent, scl_reused,
                         diagnostics)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                            $15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26)
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
                    _i("total_zones"),
                    json.dumps(structured.get("bounds")) if structured.get("bounds") is not None else None,
                    json.dumps(structured.get("bbox")) if structured.get("bbox") is not None else None,
                    json.dumps(structured.get("risk_cities")) if structured.get("risk_cities") is not None else None,
                    _f("scene_age_days"),
                    _f("confidence"),
                    structured.get("confidence_basis"),
                    _i("evidence_count"),
                    _f("coverage_percent"),
                    structured.get("coverage_status"),
                    structured.get("index_calibrated"),
                    structured.get("index_units"),
                    structured.get("selection_reason"),
                    _f("scene_cloud_percent"),
                    _f("aoi_cloud_percent"),
                    structured.get("scl_reused"),
                    json.dumps(diagnostics),
                )
        finally:
            await conn.close()

    last_exc: Optional[Exception] = None
    for attempt in range(1, PERSIST_MAX_ATTEMPTS + 1):
        try:
            asyncio.run(_write())
            logger.info(
                "Persisted satellite_results row for event %s (attempt %d/%d)",
                event_id, attempt, PERSIST_MAX_ATTEMPTS,
            )
            return None
        except Exception as exc:  # noqa: BLE001 - retried below; final failure surfaces to caller
            last_exc = exc
            logger.warning(
                "Persist attempt %d/%d failed for %s: %s",
                attempt, PERSIST_MAX_ATTEMPTS, event_id, exc,
            )
            if attempt < PERSIST_MAX_ATTEMPTS:
                time.sleep(PERSIST_RETRY_BACKOFF_SECONDS[attempt - 1])

    error_message = f"DB persist failed after {PERSIST_MAX_ATTEMPTS} attempts: {last_exc}"
    logger.error("Could not persist satellite_results for %s: %s", event_id, error_message)
    return error_message


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

# Per-city artifact rendering (individual PNG/GeoJSON layers per risk city,
# re-clipped from the already-accepted mosaic) is fully implemented
# (processor._render_per_city) but disabled by default: re-clipping a
# multi-tile mosaic once per city multiplies peak RSS on top of the 100%
# valid-pixel coverage requirement, which already peaked ~9.6 GB at just 2
# mosaicked tiles on a live run (see CLAUDE.md's Step 13 FIX 5 / 2026-07-26
# memory notes). Flip via ENABLE_PER_CITY_ARTIFACTS=true once satellite
# memory sizing/headroom work lands; until then this is a deliberate,
# documented tradeoff, not dead code.
ENABLE_PER_CITY_ARTIFACTS = os.getenv("ENABLE_PER_CITY_ARTIFACTS", "false").strip().lower() == "true"


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
    # Coverage-tolerance / search-budget overrides (2026-07-28,
    # fix/coverage-tolerance). Optional; None means "use processor.py's own
    # defaults" (DEFAULT_MIN_COVERAGE_PERCENT / max_scenes=3 /
    # max_download_gb=4.0 / max_search_seconds=900.0) — these are never
    # hardcoded a second time here, only threaded through.
    min_coverage_percent: Optional[float] = Field(
        None, description="Target interior-AOI coverage percent (80-100, clamped server-side)."
    )
    max_scenes: Optional[int] = Field(
        None, description="Max scenes to attempt across the whole coverage search."
    )
    max_download_gb: Optional[float] = Field(
        None, description="Max cumulative bytes (GB) to download across the whole coverage search."
    )
    max_search_seconds: Optional[float] = Field(
        None, description="Max wall-clock seconds for the whole coverage search."
    )


def _coverage_budget_kwargs(params: "ProcessDisasterInput") -> dict:
    """Build the kwargs for process_satellite_imagery's budget params.

    min_coverage_percent is always passed (process_satellite_imagery's own
    clamp treats None as "use DEFAULT_MIN_COVERAGE_PERCENT"). max_scenes/
    max_download_gb/max_search_seconds are typed with hard numeric defaults
    there (not Optional), so an unset override is simply omitted rather than
    passed as None — this keeps the default living in exactly one place
    (processor.py's function signature), never re-hardcoded here.
    """
    kwargs: dict = {"min_coverage_percent": params.min_coverage_percent}
    for name in ("max_scenes", "max_download_gb", "max_search_seconds"):
        value = getattr(params, name, None)
        if value is not None:
            kwargs[name] = value
    return kwargs


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


def _coverage_failure(event_id: str, message: str, detail: dict) -> str:
    """Honest insufficient-coverage failure with geometric gap detail (BUG 3).

    Unlike a generic error, this carries WHERE coverage is missing (disjoint
    gap regions with area + bbox) and WHY (nodata vs cloud), so downstream can
    tell "more tiles would fix it" from "the sky was covered that week". The
    pipeline never analyses a partial AOI, so no risk level is emitted.
    """
    logger.error("Insufficient-coverage failure for %s: %s", event_id, message)
    payload = {
        "event_id": event_id,
        "status": "error",
        "reason": "insufficient_coverage",
        "error": message,
    }
    payload.update(detail or {})
    return json.dumps(payload)


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


def _authenticate_with_recovery(event_id: str, location: str) -> Optional[TokenManager]:
    """Authenticate to CDSE, retrying up to MAX_STEP_ATTEMPTS with LLM recovery.

    On each failure, asks the intelligence layer for a recovery strategy
    (anomaly ``copernicus_auth_failed``) and respects its delay hint before the
    next attempt (integration point 3). Returns a `TokenManager` (not a bare
    token string) or ``None``: a satellite run can take tens of minutes
    (multi-tile mosaic + large downloads), well past CDSE's ~10 min
    access-token lifetime, so every downstream consumer must be able to pull a
    freshly-refreshed token rather than reuse one string captured here.
    """
    import time

    for attempt in range(1, MAX_STEP_ATTEMPTS + 1):
        manager = TokenManager()
        token = manager.get()
        if token is not None:
            return manager

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
        # failure (anomaly copernicus_auth_failed, max 3 attempts). Returns a
        # TokenManager, not a bare string, so later long-running downloads can
        # pull a refreshed token instead of reusing this moment's snapshot.
        token_manager = _authenticate_with_recovery(event_id, location)
        if token_manager is None:
            return _error(event_id, "Copernicus authentication failed (after recovery)")

        # (e) Smart, cloud-aware Sentinel selection (CHANGE 6: AOI-restricted
        # cloud measurement via an SCL peek, 2026-07-28).
        #
        # Query the S2 catalogue for candidates FIRST, even when the disaster
        # hint or a coarse cloud reading might point at S1 — catalogue
        # queries are free, and we need a real scene object (with an Id) to
        # peek. No S2 candidate in the window means nothing to measure, so S1
        # is selected immediately with no peek attempted.
        #
        # islamabad-findings #2 — this used to run its OWN fixed 7-day search
        # here, independent of _search_with_recovery's later 7->14->30-day
        # widening for whatever satellite_type ended up selected. When the
        # 7-day window was empty, the peek gave up (no_s2_candidates) even
        # though the widened search moments later found scenes — the peek
        # never got a chance on exactly the runs where the window mattered.
        # Now there is one S2 catalogue search, using the same widening
        # recovery logic, and its result is reused as `scenes` below when S2
        # is the satellite actually selected — no second search for the same
        # candidate set.
        s2_scenes = _search_with_recovery(event_id, bbox, SENTINEL_2, merged)
        s2_candidate = s2_scenes[0] if s2_scenes else None

        if s2_candidate is None:
            selection = select_satellite(disaster_type, bbox=bbox, token=token_manager.get())
            selection["selection_reason"] = "no_s2_candidates"
            satellite_type = selection["satellite_type"]
        else:
            scene_cloud = None
            for attr in s2_candidate.get("Attributes", []):
                if attr.get("Name") == "cloudCover":
                    try:
                        scene_cloud = float(attr.get("Value"))
                    except (TypeError, ValueError):
                        scene_cloud = None
                    break

            aoi_cloud_percent = None
            aoi_cloud_reason = None
            if scene_cloud is not None and peek_needed(scene_cloud):
                # Ambiguous scene-level reading (see processor.PEEK_CLEAR_BELOW/
                # PEEK_CLOUDY_ABOVE) — only download-and-measure when the
                # answer is genuinely in doubt. Skip the peek if the byte
                # budget is already exhausted (BUDGET INTERACTION): a peek is
                # an optimisation, not a requirement, and an "exempt" spend
                # path is how budgets stop meaning anything.
                budget_gb = params.max_download_gb or _PEEK_DEFAULT_MAX_DOWNLOAD_GB
                remaining_gb = budget_gb - (_processor_bytes_downloaded_total() / 1e9)
                peek = peek_aoi_cloud_percent(
                    s2_candidate, merged, event_id, token_manager,
                    remaining_download_gb=remaining_gb,
                )
                aoi_cloud_percent = peek.get("aoi_cloud_percent")
                aoi_cloud_reason = peek.get("reason") or None

            selection = select_satellite(
                disaster_type, bbox=bbox, token=token_manager.get(),
                cloud_cover=scene_cloud,
                aoi_cloud_percent=aoi_cloud_percent,
                aoi_cloud_reason=aoi_cloud_reason,
            )
            satellite_type = selection["satellite_type"]
            # If S2 ends up selected, the peek's SCL download (when one
            # happened) is already sitting under this event's bands dir —
            # process_satellite_imagery's own download_imagery reuses it via
            # _download_bands_via_nodes's on-disk cache check rather than
            # re-fetching, so the peek is never paid for twice.

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
        #
        # islamabad-findings #2 — when Sentinel-2 was selected, the catalogue
        # search above (`s2_scenes`) already found this exact candidate set
        # (same widening logic, same bbox/aoi_geom) — searching again here
        # would just repeat it. Only re-search when Sentinel-1 was chosen
        # instead (a distinct catalogue query the peek never ran).
        scenes = s2_scenes if satellite_type == SENTINEL_2 else _search_with_recovery(
            event_id, bbox, satellite_type, merged
        )
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
            # Pass the TokenManager itself (not .get()'s snapshot) so downloads
            # deep inside the pipeline can refresh it as the run proceeds.
            selection, scenes, bbox, merged, event_id, token_manager, disaster_type,
            city_geoms=city_geoms,
            # Per-city artifacts are disabled by default (ENABLE_PER_CITY_ARTIFACTS,
            # see its definition above) — re-clipping the full mosaic to each city
            # is expensive on a large multi-tile AOI. `city_geoms` is always passed
            # regardless, so the mosaic set-cover still spreads scenes across the
            # scattered cities either way.
            city_boundaries=city_polys if ENABLE_PER_CITY_ARTIFACTS else None,
            # Tiers 3/4 lower confidence + append an anomaly through this tracker.
            tracker=tracker,
            # Coverage-tolerance / search-budget overrides (fix/coverage-tolerance).
            # min_coverage_percent accepts None directly (process_satellite_imagery
            # clamps None to its own default). max_scenes/max_download_gb/
            # max_search_seconds are typed with hard numeric defaults there, so an
            # unset override is omitted from the call rather than passed as None.
            **_coverage_budget_kwargs(params),
        )
        if result is None:
            return _error(event_id, "Satellite imagery processing failed")
        if result.get("status") == "failed" and (
            result.get("reason") == "insufficient_coverage"
        ):
            # HARD FLOOR: coverage stayed below COVERAGE_FLOOR (80%, see
            # processor.py's coverage-tolerance banding, 2026-07-28) even
            # after the whole tiered/budgeted search. FAIL HONESTLY rather
            # than analyse a too-poorly-sampled AOI and report a risk level
            # for it (BUG 3). Surface the gap geometry so downstream can see
            # WHERE coverage is missing and whether more tiles would help
            # (nodata) or the sky was covered that week (cloud), and whether
            # a budget ran out before the floor was even approached.
            gaps = result.get("gaps") or []
            gap_cause = result.get("gap_cause") or result.get("gap_attribution") or {}
            budget_exhausted = result.get("budget_exhausted")
            # INTEGRATION POINT 3 — let the LLM weigh in (may recommend Landsat).
            recovery = _recover(
                "coverage_insufficient",
                {
                    "event_id": event_id,
                    "best_coverage_percent": result.get("coverage_percent"),
                    "uncovered_area_km2": result.get("uncovered_area_km2"),
                    "uncovered_regions": result.get("uncovered_regions"),
                    "gap_cause": gap_cause,
                    "budget_exhausted": budget_exhausted,
                    "disaster_type": disaster_type,
                    "location": location,
                },
                MAX_STEP_ATTEMPTS,
            )
            note = ""
            if recovery and recovery.get("alert_human"):
                note = f" | {recovery.get('alert_message', '')}"
            logger.error(
                "[%s] INSUFFICIENT COVERAGE: best %.3f%% interior (floor "
                "%.1f%%); %d gap(s), %.3f km^2 uncovered (nodata=%s px, "
                "cloud=%s px)%s",
                event_id,
                result.get("coverage_percent", 0.0),
                result.get("min_coverage_percent") or 90.0,
                result.get("uncovered_regions", 0),
                result.get("uncovered_area_km2", 0.0),
                gap_cause.get("nodata"),
                gap_cause.get("cloud"),
                f" [budget_exhausted={budget_exhausted}]" if budget_exhausted else "",
            )
            return _coverage_failure(
                event_id,
                "insufficient_coverage: could not reach the minimum viable "
                f"AOI coverage (best {result.get('coverage_percent')}%; "
                f"{result.get('uncovered_regions')} uncovered region(s), "
                f"{result.get('uncovered_area_km2')} km^2)"
                + (f" | search budget exhausted: {budget_exhausted}" if budget_exhausted else "")
                + note,
                {
                    "coverage_percent": result.get("coverage_percent"),
                    "full_aoi_coverage_percent": result.get(
                        "full_aoi_coverage_percent"
                    ),
                    "uncovered_regions": result.get("uncovered_regions"),
                    "uncovered_area_km2": result.get("uncovered_area_km2"),
                    "gap_count": result.get("gap_count"),
                    "gap_area_km2": result.get("gap_area_km2"),
                    "gap_attribution": gap_cause,
                    "gap_limited_by": result.get("gap_limited_by"),
                    "gaps": gaps,
                    "gap_cause": gap_cause,
                    "bytes_downloaded": result.get("bytes_downloaded"),
                    "budget_exhausted": budget_exhausted,
                },
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
        # A missing artifact is not fatal (the run may still be useful without
        # one PNG), but the degraded state must be explicit rather than
        # something every downstream consumer has to null-check for.
        failed_artifacts = list(urls.get("failed_artifacts") or [])

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
            for artifact in city_urls.get("failed_artifacts") or []:
                failed_artifacts.append(f"cities/{slug}/{artifact}")
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
        # BUG (2026-07-27) — this dict used to hardcode "mean_ndwi": mean_index
        # regardless of what index was actually computed, so a SAR run's raw
        # dB value got read as an NDWI ratio by the physics check below. The
        # label must come from the same field the pipeline just computed
        # (result["index_type"]), never set independently of it — see the
        # assertion right after this dict.
        validation_input = {
            "affected_area_km2": result.get("affected_area_km2"),
            "cloud_cover": selection.get("cloud_cover"),
            "index_type": result["index_type"],
            "index_calibrated": result.get("index_calibrated"),
            "index_units": result.get("index_units"),
            "mean_index": result.get("mean_index"),
            # Phase 0b — the physics check compares THIS (mean over the
            # classified water pixels), not the whole-AOI mean, against water
            # thresholds. Whole-AOI mean_index stays as context only.
            "affected_mean_index": result.get("affected_mean_index"),
            "water_percent": result.get("water_percent"),
            "coverage_percent": result.get("valid_percent"),
            "valid_percent": result.get("valid_percent"),
        }
        assert validation_input["index_type"] == result["index_type"], (
            f"validation_input index_type {validation_input['index_type']!r} "
            f"diverges from computed index_type {result['index_type']!r}"
        )
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
            "affected_mean_index": result.get("affected_mean_index"),
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
        confidence_report = tracker.get_report()
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
            "reason": selection.get("reason"),
            # CHANGE 6 — the AOI-restricted SCL peek's own fields. These are
            # what let a downstream consumer (or the pipeline log) actually
            # see that the peek ran and what it decided, instead of only the
            # legacy scene-level cloud_cover/reason pair. selection_reason
            # names the real basis (aoi_scl_measured / scene_metadata_clear /
            # scene_metadata_cloudy / no_s2_candidates / scl_unavailable_fallback);
            # scene_cloud_percent/aoi_cloud_percent are the raw figures behind
            # it.
            "selection_reason": selection.get("selection_reason"),
            "scene_cloud_percent": selection.get("scene_cloud_percent"),
            "aoi_cloud_percent": selection.get("aoi_cloud_percent"),
            "index_type": result["index_type"],
            "water_percent": result["water_percent"],
            "mean_index": result["mean_index"],
            # Phase 0b — mean index over classified-affected pixels only (the
            # within-water mean for flood). None when nothing was classified.
            # This is the value the index-physics check and every downstream
            # LLM prompt should compare against water thresholds; the
            # whole-AOI mean_index above is context, never counter-evidence.
            "affected_mean_index": result.get("affected_mean_index"),
            "class_counts": result.get("class_counts"),
            "affected_area_km2": result["affected_area_km2"],
            # The satellite_results INSERT names these columns; total_zones was
            # already computed above (used in the LLM calls) but previously
            # dropped before reaching structured, and scene_id is the accepted
            # scene(s)' product id(s) from process_satellite_imagery. Both
            # persisted every row as NULL before this fix.
            "total_zones": total_zones,
            "scene_id": result.get("scene_id"),
            # BUG 5 — explicit calibration contract so the hazard agent can
            # branch on a real field instead of inferring from satellite_type.
            # SAR is 10*log10(raw GRD DN): uncalibrated, no speckle filter, no
            # terrain correction — it must NOT be threshold-compared. NDWI is a
            # bounded, calibrated ratio.
            "index_calibrated": result.get("index_calibrated"),
            "index_units": result.get("index_units"),
            # BUG 3 — coverage provenance so hazard/report see how the 100% AOI
            # coverage was achieved (which tier, temporal spread, tile count).
            "coverage_percent": result.get("coverage_percent"),
            "full_aoi_coverage_percent": result.get("full_aoi_coverage_percent"),
            "coverage_tier": result.get("coverage_tier"),
            "temporal_spread_days": result.get("temporal_spread_days"),
            "acquisition_count": result.get("acquisition_count"),
            "processing_level": result.get("processing_level"),
            "bytes_downloaded": result.get("bytes_downloaded"),
            # islamabad-findings audit follow-up (field-survival pass): these
            # were computed by processor.py's _finish_success on EVERY
            # success path (target_met and below_target_coverage alike, see
            # coverage_status just below) but were only ever copied into a
            # payload on the insufficient_coverage FAILURE branch above —
            # dropped silently whenever the run completed (even a
            # below-target-coverage "complete" run). Found by writing the
            # field-survival test this comment sits next to, not by report
            # or user complaint.
            "coverage_status": result.get("coverage_status"),
            "gap_count": result.get("gap_count"),
            "gap_area_km2": result.get("gap_area_km2"),
            "gap_attribution": result.get("gap_attribution") or result.get("gap_cause"),
            "gap_limited_by": result.get("gap_limited_by"),
            # durable-evidence-trail (2026-07-28): the gap GEOMETRY itself
            # (not just the count/area/attribution scalars above) was
            # computed by processor.py's _finish_success on every success
            # path (merged_result["gaps"] = gaps) but never copied into
            # structured — _persist_satellite_result's diagnostics dict reads
            # structured.get("gaps"), which was always None before this fix,
            # so no run ever actually persisted gap geometry despite the
            # write path assuming it would. Same defect class as the
            # coverage_status/gap_count fix above (bd9cf11): computed on
            # every success path, silently dropped one hop earlier.
            "gaps": result.get("gaps"),
            # islamabad-findings #4 — days between the most recent accepted
            # acquisition and now; reduces confidence (processor._finish_success)
            # but is never a hard cutoff, so it must always be visible downstream.
            "scene_age_days": result.get("scene_age_days"),
            # CHANGE 6 — whether the selection-time SCL peek's download was
            # reused during real processing (True), a fresh SCL had to be
            # downloaded anyway (False), or SCL was never requested for this
            # satellite_type/disaster (None, e.g. Sentinel-1).
            "scl_reused": result.get("scl_reused"),
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
            # A degraded R2 upload (missing PNG/GeoJSON) is not fatal, but must
            # be explicit rather than something every downstream consumer has
            # to discover by null-checking each artifact URL individually.
            "artifacts_incomplete": bool(failed_artifacts),
            "failed_artifacts": failed_artifacts,
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
            # Legibility for the confidence number: how much evidence went
            # into it and whether the low end (if it's low) means "we could
            # not gather evidence" vs "the evidence itself is unfavourable" —
            # see ConfidenceTracker.confidence_basis(). Not a recalibration of
            # the arithmetic, just making the existing heuristic legible.
            "evidence_count": confidence_report["evidence_count"],
            "confidence_basis": confidence_report["confidence_basis"],
            # Cross-validation: concerns raised, per-source findings, and the
            # two action flags the caller cares about.
            "concerns": tracker.concerns,
            "validations": validations,
            "needs_verification": needs_verification,
            "should_alert": should_alert,
        }
        # A field that exists in `selection` but silently vanishes from
        # `structured` is the defect class this whole audit series keeps
        # finding (see the index_type assertion above for the first
        # instance). If selection produced a real selection_reason,
        # structured must carry it through unchanged.
        if selection.get("selection_reason") is not None:
            assert structured["selection_reason"] == selection["selection_reason"], (
                f"structured selection_reason {structured['selection_reason']!r} "
                f"diverges from selection's {selection['selection_reason']!r}"
            )

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
        # downstream nodes/agents and GET /results read from. The pipeline may
        # not report "complete" for work that was not durably recorded: a
        # transient Neon outage here must surface as a hard failure, not a
        # silently-swallowed warning behind an otherwise-successful result.
        persist_error = _persist_satellite_result(event_id, structured)
        if persist_error is not None:
            structured["pipeline_log_entry"] = {
                "stage": "satellite",
                "error": persist_error,
                "event_id": event_id,
            }
            return _error(
                event_id,
                f"Satellite analysis completed but could not be durably recorded: {persist_error}",
            )

        _completed_event_ids.add(event_id)
        return json.dumps(structured)
    except Exception as exc:  # noqa: BLE001 - report any failure to the caller.
        return _error(event_id, f"Unexpected error: {exc}")
    finally:
        # BUG 7 — guarantee temp cleanup on EVERY exit path (success already
        # cleaned above and kept the .zip cache; here we catch the failure and
        # exception paths, which previously left the multi-GB working tree on
        # disk). cleanup_event_temp is safe to call when the dir is already gone.
        try:
            asyncio.run(cleanup_event_temp(event_id))
        except Exception as _cleanup_exc:  # noqa: BLE001 - never mask the result
            logger.warning(
                "[Cleanup] finally-path cleanup failed for %s: %s",
                event_id, _cleanup_exc,
            )
        try:
            mr = _processor_memory_report()
            if mr:
                logger.info(
                    "[MEM] peak stage=%s peak=%.1f MB; per-stage=%s",
                    mr.get("peak_stage"), mr.get("peak_mb"),
                    mr.get("per_stage"),
                )
        except Exception:  # noqa: BLE001
            pass
