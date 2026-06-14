"""HazardMind satellite agent — Band entry point.

Connects to the Band platform with the Anthropic adapter and waits for the
orchestrator to @mention this agent with a disaster to analyse. When mentioned,
the model calls the `processdisaster` custom tool, which runs the full
deterministic imagery pipeline:

    demo cache check
        -> region boundary
        -> risk-city detection + boundaries
        -> merged risk bbox
        -> Copernicus auth
        -> Sentinel selection
        -> scene search
        -> download / clip / export PNG
        -> upload to Cloudflare R2

The tool returns a structured result that the model relays to
`@hazardmind-hazard` in the format the hazard agent expects. Every stage logs
and the tool returns a `status: error` payload rather than raising, so a single
failure surfaces to the room instead of killing the agent.

Run:
    python agent.py
"""

import asyncio
import json
import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from band import Agent
from band.adapters.anthropic import AnthropicAdapter

from boundary import (
    get_analysis_bbox,
    get_region_boundary,
    get_risk_city_boundaries,
    merge_risk_boundaries,
)
from intelligence import SatelliteIntelligence
from processor import process_satellite_imagery
from r2_upload import check_demo_cache, upload_all_results
from sentinel import (
    authenticate_copernicus,
    backfill_uncovered_cities,
    search_imagery,
    select_satellite,
)

logger = logging.getLogger(__name__)

# The agent we report results back to on the Band platform.
HAZARD_AGENT = "@hazardmind-hazard"

# LLM intelligence layer (Featherless chain + Opus last resort). Shared across
# tool calls. Every method returns None on total failure, so the pipeline keeps
# working on its deterministic defaults if the LLMs are unreachable.
intelligence = SatelliteIntelligence()

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
# Custom tool: the satellite pipeline
# --------------------------------------------------------------------------- #
class ProcessDisasterInput(BaseModel):
    """Run the satellite imagery pipeline for a disaster event and return the
    image URL, bbox, satellite type, region boundary and risk cities. Call this
    whenever the orchestrator asks for satellite analysis of a disaster."""

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
            "The original, unparsed disaster alert text as it arrived in the "
            "room (e.g. 'flood in Peshawar magnitude 6.2'). Pass it through "
            "verbatim when available so the agent can parse it for structure "
            "and detect ambiguity."
        ),
    )


def _error(event_id: str, message: str) -> str:
    """Build the error payload the model should relay to the hazard agent."""
    logger.error("Pipeline error for %s: %s", event_id, message)
    return json.dumps(
        {"event_id": event_id, "status": "error", "error": message}
    )


def _clarification(event_id: str, profile: dict) -> str:
    """Build a clarification-request payload for an ambiguous disaster message.

    Returned when the intelligence layer flags the parsed input as ambiguous;
    the model relays it to the room so the orchestrator can supply the missing
    details (integration point 1).
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


def run_pipeline(params: ProcessDisasterInput) -> str:
    """Execute the full satellite pipeline and return a JSON result string.

    Returns a JSON object with status "complete" (image_url, bbox,
    satellite_type, region_boundary, risk_cities), "error" (error message), or
    "clarification_needed" (ambiguous input). Never raises — failures are
    reported as a payload so the agent can relay them to the room.

    Six LLM integration points run alongside the deterministic pipeline:
      1. parse the raw message + detect ambiguity (ask for clarification)
      2. devise the satellite strategy (logged reasoning)
      3. anomaly recovery on auth / scene-search failures (max 3 attempts)
      4. expert interpretation of the raw GIS numbers
      5. a natural Band hand-off message (not raw JSON)
      6. a confidence quality gate before sending
    """
    event_id = params.event_id
    location = params.location
    disaster_type = params.disaster_type

    logger.info(
        "Processing event %s: %s / %s (magnitude=%s)",
        event_id,
        location,
        disaster_type,
        params.magnitude,
    )

    try:
        # INTEGRATION POINT 1 — parse the raw Band message into a structured
        # profile and detect ambiguity. Best-effort: if the LLM chain is down
        # we keep the orchestrator-supplied location/disaster_type as-is.
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
            missing = {m.strip().lower() for m in (profile.get("missing_info") or [])}
            _LOC_TOKENS = {"location", "city", "place"}
            _TYPE_TOKENS = {"disaster_type", "disaster type", "type"}
            loc_missing = (not profile.get("location")) or bool(missing & _LOC_TOKENS)
            type_missing = (not profile.get("disaster_type")) or bool(missing & _TYPE_TOKENS)
            if profile.get("ambiguous") and (loc_missing or type_missing):
                return _clarification(event_id, profile)
            # Enrich downstream inputs from the parsed profile where the tool
            # args were thin (keep explicit args authoritative).
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
        )
        if result is None:
            return _error(event_id, "Satellite imagery processing failed")
        if result.get("status") == "coverage_insufficient":
            # INTEGRATION POINT 3 — let the LLM weigh in (it may recommend
            # Landsat). We surface the anomaly + its advice in the error so the
            # room/human sees an actionable next step, not a bare failure.
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

        # (h) Upload all artifacts to Cloudflare R2.
        urls = upload_all_results(
            event_id,
            {
                "true_color": result["png_paths"].get("true_color"),
                "index_map": result["png_paths"].get("index_map"),
                "classification": result["png_paths"].get("classification"),
                "geojson": result["geojson"],
            },
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

        # INTEGRATION POINT 6 — confidence quality gate. If the interpretation
        # is low-confidence, ask the LLM how to improve / whether to alert a
        # human. We still send (people need the data), but the anomaly advice
        # is logged and surfaced.
        confidence = (interpretation or {}).get("confidence")
        anomalies = (interpretation or {}).get("anomalies") or []
        try:
            low_confidence = confidence is not None and float(confidence) < MIN_CONFIDENCE
        except (TypeError, ValueError):
            low_confidence = False
        if low_confidence:
            _recover(
                "low_confidence",
                {
                    "event_id": event_id,
                    "confidence": confidence,
                    "anomalies": anomalies,
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

        # (i) Structured result for the hazard agent (full machine-readable
        # payload).
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
            # Expert reasoning from the intelligence layer (point 4).
            "interpretation": interpretation,
            "confidence": confidence,
        }

        # INTEGRATION POINT 5 — a natural, expert-sounding hand-off message for
        # the room (not raw JSON). The structured payload above rides along as
        # `structured_data` for any consumer that needs the numbers.
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
            anomalies=anomalies,
            confidence=confidence,
            next_agent_handle=HAZARD_AGENT,
        )
        if band_message:
            structured["band_message"] = band_message
            logger.info("Generated natural Band message (%d chars)", len(band_message))

        return json.dumps(structured)
    except Exception as exc:  # noqa: BLE001 - report any failure to the room.
        return _error(event_id, f"Unexpected error: {exc}")


# Custom tool definition: (Pydantic input model, callable). The tool name is
# derived from the model class name -> "processdisaster".
PROCESS_DISASTER_TOOL = (ProcessDisasterInput, run_pipeline)


# --------------------------------------------------------------------------- #
# System prompt
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = f"""\
You are HazardMind's satellite agent. You process Copernicus/Sentinel satellite \
imagery for disaster zones and report the results to the hazard analysis agent.

When the orchestrator @mentions you with a disaster, extract the location, \
disaster_type (flood/earthquake/landslide), magnitude (optional) and event_id, \
then call the `processdisaster` tool exactly once with those values. ALSO pass \
the original alert text verbatim as `raw_message` so the agent can parse it and \
detect ambiguity.

The tool returns a JSON object. Relay it as follows.

When status is "complete": the tool returns a `band_message` field — a natural, \
expert hand-off message already addressed to {HAZARD_AGENT}. Reply with that \
`band_message` verbatim as your message to the room. If (and only if) \
`band_message` is missing/empty, fall back to this exact format:

{HAZARD_AGENT} satellite processing complete.
event_id: <event_id>
satellite_type: <satellite_type>
cloud_cover: <cloud_cover>
index_type: <index_type>
affected_area_km2: <affected_area_km2>
water_percent: <water_percent>
true_color_url: <true_color_url>
index_url: <index_url>
classification_url: <classification_url>
geojson_url: <geojson_url>
bbox: <bbox>
region_boundary: <region_boundary>
risk_cities: <risk_cities>
status: complete

When status is "clarification_needed", the input was ambiguous. Reply asking \
the orchestrator to clarify, listing the `missing_info` items:

{HAZARD_AGENT} I need clarification before I can run satellite analysis.
event_id: <event_id>
missing: <missing_info>
status: clarification_needed

When status is "error", reply with:

{HAZARD_AGENT} satellite processing failed.
event_id: <event_id>
error: <error>
status: error

Do not invent any values — use only what the tool returns. Do not call the tool \
unless a disaster analysis was requested.
"""


def _require(name: str) -> str:
    """Return env var `name` or exit with a clear message if missing."""
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing required environment variable: {name} (set it in .env)")
    return value


async def main() -> None:
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    agent_id = _require("BAND_AGENT_ID")
    api_key = _require("BAND_API_KEY")
    anthropic_api_key = _require("ANTHROPIC_API_KEY")
    rest_url = os.getenv("THENVOI_REST_URL", "https://app.band.ai")
    ws_url = os.getenv(
        "THENVOI_WS_URL", "wss://app.band.ai/api/v1/socket/websocket"
    )

    adapter = AnthropicAdapter(
        model="claude-sonnet-4-5-20250929",
        provider_key=anthropic_api_key,
        system_prompt=SYSTEM_PROMPT,
        additional_tools=[PROCESS_DISASTER_TOOL],
    )

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
        ws_url=ws_url,
        rest_url=rest_url,
    )

    logger.info("Connecting satellite agent to Band...")
    await agent.start()
    try:
        logger.info("Connected as: %s. Waiting for disaster mentions...", agent.agent_name)
        await agent.run_forever()
    finally:
        await agent.stop()
        logger.info("Satellite agent disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
