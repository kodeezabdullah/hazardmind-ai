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
from processor import process_satellite_imagery
from r2_upload import check_demo_cache, upload_all_results
from sentinel import authenticate_copernicus, search_imagery, select_satellite

logger = logging.getLogger(__name__)

# The agent we report results back to on the Band platform.
HAZARD_AGENT = "@hazardmind-hazard"


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


def _error(event_id: str, message: str) -> str:
    """Build the error payload the model should relay to the hazard agent."""
    logger.error("Pipeline error for %s: %s", event_id, message)
    return json.dumps(
        {"event_id": event_id, "status": "error", "error": message}
    )


def run_pipeline(params: ProcessDisasterInput) -> str:
    """Execute the full satellite pipeline and return a JSON result string.

    Returns a JSON object with status "complete" (image_url, bbox,
    satellite_type, region_boundary, risk_cities) or "error" (error message).
    Never raises — failures are reported as an error payload so the agent can
    relay them to the room.
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

        # (d) Copernicus authentication (needed by select_satellite's cloud peek).
        token = authenticate_copernicus()
        if token is None:
            return _error(event_id, "Copernicus authentication failed")

        # (e) Smart, cloud-aware Sentinel selection.
        selection = select_satellite(disaster_type, bbox=bbox, token=token)
        satellite_type = selection["satellite_type"]

        # (f) Find the best scene over the bbox.
        scene = search_imagery(bbox, satellite_type)
        if scene is None:
            return _error(
                event_id,
                f"No {satellite_type} imagery found over bbox {bbox}",
            )

        # (g) Full remote-sensing pipeline (download -> stack -> clip ->
        # indices -> PNGs -> vectorize) over the real risk polygon.
        result = process_satellite_imagery(
            selection, scene, bbox, merged, event_id, token, disaster_type
        )
        if result is None:
            return _error(event_id, "Satellite imagery processing failed")

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

        # (i) Structured result for the hazard agent.
        logger.info(
            "Pipeline complete for %s (%s, %.2f km^2 affected)",
            event_id,
            satellite_type,
            result["affected_area_km2"],
        )
        return json.dumps(
            {
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
                "region_boundary": region.get("geojson"),
                "risk_cities": [c["name"] for c in city_polys],
                "true_color_url": urls["true_color_url"],
                "index_url": urls["index_url"],
                "classification_url": urls["classification_url"],
                "geojson_url": urls["geojson_url"],
                "image_url": urls["classification_url"] or urls["true_color_url"],
                "cached": False,
            }
        )
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
then call the `processdisaster` tool exactly once with those values.

The tool returns a JSON object. When status is "complete", reply with EXACTLY \
this format (substituting the tool's values):

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
