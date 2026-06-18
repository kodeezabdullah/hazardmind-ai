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
import re
import sys
import time
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from band import Agent
from band.adapters.langgraph import LangGraphAdapter
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

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

logger = logging.getLogger(__name__)

# The agent we report results back to on the Band platform.
HAZARD_AGENT = "@hazardmind-hazard"

# event_ids the satellite has already fully analysed. Guards against the LLM
# re-invoking processdisaster when it sees the orchestrator's own messages echo
# back in the room (acks / nudges / pipeline summary). One analysis per event.
_completed_event_ids: set[str] = set()

# The Band room the current disaster was dispatched in. The orchestrator now
# creates a fresh per-event room and adds us to it, so we must post our ack /
# progress / completion back into THAT room — not a hardcoded static room. The
# room id arrives as the LangGraph `thread_id` (see _coroutine); run_pipeline
# stashes it here so the deterministic _post_* helpers can read it. Falls back
# to BAND_ROOM_ID only if, somehow, no room was captured.
_active_room: Optional[str] = None


def _set_active_room(room_id: Optional[str]) -> None:
    """Record the room the current dispatch arrived in (the post target)."""
    global _active_room
    if room_id:
        _active_room = str(room_id)


def _current_room() -> Optional[str]:
    """The room to post into: the dispatch room, else the static fallback."""
    return _active_room or os.getenv("BAND_ROOM_ID")


# Root-cause fix for LLM event_id truncation: the orchestrator's dispatch text
# carries the FULL `event_id: <uuid>` line, and the Band adapter delivers that
# message to `on_message` BEFORE the LLM ever runs (see _BoundEventIdAdapter).
# We snapshot the full UUID per room there, so the tool can use the authoritative
# id regardless of how the LLM later mangles its `event_id` argument. Keyed by
# room id (== the LangGraph `thread_id` the tool receives).
_room_event_ids: dict[str, str] = {}


def _bind_room_event_id(room_id: Optional[str], event_id: Optional[str]) -> None:
    """Record the full event_id seen in a room's inbound dispatch text."""
    if room_id and event_id:
        _room_event_ids[str(room_id)] = str(event_id)


def _event_id_for_room(room_id: Optional[str]) -> Optional[str]:
    """The full event_id captured from the inbound dispatch for this room."""
    if not room_id:
        return None
    return _room_event_ids.get(str(room_id))


# event_ids we have already auto-dispatched the tool for (see
# _maybe_autodispatch). Distinct from _completed_event_ids (set only AFTER the
# pipeline finishes): this guards against firing the tool twice while it is
# still running, e.g. if the orchestrator's nudge arrives mid-pipeline.
_autodispatched_event_ids: set[str] = set()


def _parse_dispatch_fields(content: str) -> Optional[dict]:
    """Extract the structured dispatch fields from an orchestrator message.

    The orchestrator's satellite dispatch carries a `---` tail with
    `event_id: <uuid>`, `location: <place>`, `disaster_type: <type>` (and an
    optional `magnitude:`). We parse those deterministically so we can drive the
    `processdisaster` tool ourselves — the Featherless adapter LLM stays the
    primary brain for every *conversational* turn, but the one critical
    imagery-processing tool-call no longer depends on the model reliably emitting
    it (gemma frequently replies in prose instead, stalling the whole pipeline).

    Returns a dict with location/disaster_type/event_id/magnitude when the
    message is a genuine NEW disaster dispatch (has a location AND a disaster
    type AND a full event_id), else None — so nudges, acks, handoffs and
    check-ins (which lack these fields) never trigger a run.
    """
    if not content:
        return None

    fields: dict[str, str] = {}
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key in ("event_id", "location", "disaster_type", "magnitude") and value:
            fields.setdefault(key, value)

    location = fields.get("location")
    disaster_type = (fields.get("disaster_type") or "").lower()
    event_id = fields.get("event_id")

    # A genuine dispatch needs all three. Nudges/check-ins carry only event_id.
    if not (location and disaster_type and event_id):
        return None
    if disaster_type not in ("flood", "earthquake", "landslide"):
        return None

    magnitude = None
    if fields.get("magnitude"):
        try:
            magnitude = float(fields["magnitude"])
        except (TypeError, ValueError):
            magnitude = None

    return {
        "event_id": event_id,
        "location": location,
        "disaster_type": disaster_type,
        "magnitude": magnitude,
        "raw_message": content,
    }


async def _maybe_autodispatch(content: str, room_id: str) -> None:
    """Deterministically run the satellite pipeline on a genuine NEW dispatch.

    This is the reliability backbone: rather than trusting the Band-adapter LLM
    (Featherless gemma) to emit the `processdisaster` tool-call — which it often
    skips, answering in prose and stalling the pipeline — we detect a real
    dispatch from its structured tail and invoke the tool directly. Fires at most
    once per event_id; everything else (acks, nudges, handoffs) is left to the
    LLM's conversational turn. Best-effort: never raises into message handling.
    """
    fields = _parse_dispatch_fields(content)
    if not fields:
        return

    event_id = fields["event_id"]
    if event_id in _autodispatched_event_ids or event_id in _completed_event_ids:
        return
    _autodispatched_event_ids.add(event_id)

    # Post target is the dispatch room (matches the LangGraph thread_id the tool
    # would otherwise capture).
    _set_active_room(room_id)
    logger.info(
        "[autodispatch] genuine dispatch for event %s (%s/%s) — driving "
        "processdisaster tool directly",
        event_id, fields["location"], fields["disaster_type"],
    )
    try:
        await run_pipeline(
            ProcessDisasterInput(
                event_id=event_id,
                location=fields["location"],
                disaster_type=fields["disaster_type"],
                magnitude=fields["magnitude"],
                raw_message=fields["raw_message"],
            )
        )
    except Exception:  # noqa: BLE001 - tool failures are reported in-room, never crash the listener
        logger.exception("[autodispatch] pipeline failed for event %s", event_id)
        # Allow a later genuine retry if this was a transient failure.
        _autodispatched_event_ids.discard(event_id)


def _post_satellite_message(content: str) -> bool:
    """Post a chat message to the Band room as the satellite agent.

    Posts into the room the current disaster was dispatched in (_current_room).
    Mentions the orchestrator (an agent cannot @mention itself on Band). Used
    for the initial ack and for in-progress heartbeats so the room shows the
    satellite is alive and working, not silent. Best-effort: returns True on a
    successful post, False otherwise (never raises).
    """
    api_key = os.getenv("BAND_API_KEY")
    room_id = _current_room()
    orchestrator_id = os.getenv("ORCHESTRATOR_AGENT_ID")  # mention orchestrator, not self
    if not api_key or not room_id or not orchestrator_id:
        return False

    mentions = [{"id": orchestrator_id}]
    try:
        import httpx as _httpx
        with _httpx.Client(timeout=10.0) as client:
            rest_url = os.getenv("THENVOI_REST_URL", "https://app.band.ai").rstrip("/")
            client.post(
                f"{rest_url}/api/v1/agent/chats/{room_id}/messages",
                headers={"X-API-Key": api_key},
                json={"message": {"content": content, "mentions": mentions}},
            )
        return True
    except Exception:  # noqa: BLE001 – chatter is best-effort
        logger.warning("Failed to post satellite message to Band room")
        return False


def _post_ack(event_id: str, location: str, disaster_type: str, magnitude) -> None:
    """Post a Band acknowledgment message as soon as the satellite starts work.

    Lets the orchestrator and judges see immediately that the satellite received
    the dispatch and is actively processing imagery, rather than showing silence
    until the full pipeline finishes (which can take several minutes).
    """
    mag_str = f" (magnitude {magnitude})" if magnitude else ""
    content = (
        f"@hazardmind-orchestrator 🛰️ HazardMind Satellite here — acknowledged. "
        f"Received dispatch for {disaster_type} in {location}{mag_str}. "
        f"Spinning up imagery pipeline now, pulling Sentinel data for the affected area. "
        f"Stand by for analysis results.\nevent_id: {event_id}"
    )
    if _post_satellite_message(content):
        logger.info("Posted ack to Band room for event %s", event_id)


def _post_progress(event_id: str, step: str, detail: str) -> None:
    """Post an in-progress heartbeat so the room sees the satellite working."""
    content = (
        f"@hazardmind-orchestrator 🛰️ [working] {step} — {detail}\n"
        f"event_id: {event_id}"
    )
    if _post_satellite_message(content):
        logger.info("Posted progress (%s) to Band room for event %s", step, event_id)


def _persist_satellite_result(event_id: str, structured: dict) -> None:
    """Write the satellite result straight to the DB (the reliable channel).

    Band's REST /messages returns an EMPTY history for these per-event rooms, so
    the orchestrator can't read the satellite's posted JSON and ends up forwarding
    an empty ``data: {}`` to hazard — leaving the whole downstream chain with no
    data to analyse (observed live). The DB is the dependable hand-off medium:
    the satellite persists its own row here, and the orchestrator/hazard read the
    payload from the DB instead of trusting the room transcript.
    """
    db_url = os.getenv("NEON_DATABASE_URL")
    if not db_url:
        return
    try:
        import asyncpg

        # Columns mirror satellite_results; jsonb cols (bounds/bbox/risk_cities)
        # are passed as JSON strings.
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
        logger.info("Persisted satellite_results row for event %s (DB hand-off)", event_id)
    except Exception as exc:  # noqa: BLE001 - DB write is best-effort; room post still happens
        logger.warning("Could not persist satellite_results for %s: %s", event_id, exc)


def _post_completion(event_id: str, band_message: str, structured: dict) -> None:
    """Post the satellite's completion signal to the room AND persist to DB.

    The orchestrator advances the pipeline only on a genuine completion signal.
    The Band Anthropic adapter is LLM-driven, and the model tends to paraphrase
    its result instead of relaying the structured payload verbatim — so we post
    the authoritative signal ourselves: the natural hand-off message, an explicit
    "satellite complete" marker, and the full JSON object on the tail. We ALSO
    write the result to the DB, because Band's REST history is empty for these
    rooms and the room transcript can't be relied on to carry the payload
    downstream (see _persist_satellite_result).
    """
    # DB hand-off first — the reliable channel.
    _persist_satellite_result(event_id, structured)

    content = (
        f"{band_message}\n\n"
        f"satellite complete\n"
        f"---\n"
        f"{json.dumps(_slim_completion_payload(structured))}"
    )
    if _post_satellite_message(content):
        logger.info("Posted completion signal to Band room for event %s", event_id)


# Verbose nested keys dropped from the room completion JSON. The downstream agents
# (and the orchestrator's detector) read the headline scalars + artifact URLs; the
# full geometry/validation detail is heavy and, replayed into the next agent's
# Band-adapter LLM turn, overflowed the Featherless 32k context cap. The complete
# payload is still written to the DB via insert_satellite_result.
_COMPLETION_DROP_KEYS = frozenset(
    {
        "bounds_corners",
        "bounds_leaflet",
        "validations",
        "class_counts",
        "concerns",
        # region_boundary is a full MultiPolygon (hundreds of coord pairs, ~20k
        # chars) — by far the biggest field. Downstream agents use the bbox/bounds
        # scalars + artifact URLs, not the raw geometry (which is on R2 as
        # zones.geojson). Dropping it keeps the room transcript small enough that
        # the next agent's 32k-context Band adapter never overflows.
        "region_boundary",
        "geojson",
        "coordinates",
    }
)


def _slim_completion_payload(data):
    """Recursively drop verbose keys so the room completion JSON stays small."""
    if isinstance(data, dict):
        return {
            k: _slim_completion_payload(v)
            for k, v in data.items()
            if k not in _COMPLETION_DROP_KEYS
        }
    if isinstance(data, list):
        return [_slim_completion_payload(v) for v in data]
    return data


# LLM intelligence layer (Featherless chain + Opus last resort). Shared across
# tool calls. Every method returns None on total failure, so the pipeline keeps
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


def _coerce_float(value) -> Optional[float]:
    """Return ``value`` as a float, or ``None`` if it isn't numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# A UUID that directly follows an `event_id` label, in either the dispatch's
# `event_id: <uuid>` line form or a JSON `"event_id": "<uuid>"` form. Preferred
# over a bare first-UUID match so a stray UUID elsewhere in the text (should one
# ever appear) cannot be mistaken for the event id.
_EVENT_ID_RE = re.compile(
    r"event_id\"?\s*[:=]\s*\"?\s*("
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)
# A Band @mention is rendered into message content as `@[[<agent-uuid>]]`. That
# UUID is an AGENT id, never the event id, so it must be stripped before any
# bare-UUID scan — otherwise the room's intro/title message (which @mentions the
# satellite but carries no `event_id:` label) would bind the satellite's OWN
# agent id as the event id (the truncated-event bug observed live).
_MENTION_RE = re.compile(r"@\[\[[^\]]*\]\]")

# The agent's own Band id, populated at startup (see main()). A bare UUID equal
# to this is the agent's own mention and is never the event id.
_OWN_AGENT_ID: Optional[str] = None


def _strip_mentions(content: str) -> str:
    """Remove `@[[<agent-uuid>]]` mention tokens so their ids aren't scanned."""
    return _MENTION_RE.sub(" ", content or "")


def _extract_event_id_from_text(content: str) -> Optional[str]:
    """The full event_id UUID from a dispatch/handoff: label-anchored, else first.

    Mention tokens (`@[[<agent-uuid>]]`) and the agent's own id are excluded from
    the bare-UUID fallback so the room intro message — which mentions the agent
    but has no `event_id:` label — can never bind an agent id as the event id.
    """
    if not content:
        return None
    # Label-anchored is always authoritative (works even with mentions present).
    labeled = _EVENT_ID_RE.search(content)
    if labeled:
        return labeled.group(1)
    # Bare fallback: scan AFTER stripping @mentions, and skip our own agent id.
    cleaned = _strip_mentions(content)
    for cand in _UUID_RE.findall(cleaned):
        if _OWN_AGENT_ID and cand.lower() == _OWN_AGENT_ID.lower():
            continue
        return cand
    return None


def _recover_full_event_id(
    event_id: str,
    raw_message: Optional[str],
    room_id: Optional[str] = None,
) -> str:
    """Resolve the authoritative full-UUID event_id, defending against LLM mangling.

    The Band adapter's model populates the ``event_id`` tool argument from the
    dispatch text and sometimes truncates it to the leading 8-char segment
    (e.g. ``e9e83455`` instead of ``e9e83455-8ea6-44b7-...``). The downstream DB
    columns are UUID-typed, so a truncated id makes the hazard agent's insert
    fail (``invalid UUID ... length must be 32..36``).

    Resolution order (most authoritative first):
      1. The full event_id captured from the room's inbound dispatch in
         ``on_message`` BEFORE the LLM ran (``_event_id_for_room``) — the
         root-cause fix; the LLM cannot corrupt this. Preferred whenever the
         LLM-supplied id is absent, truncated, or merely a prefix of it.
      2. The LLM-supplied ``event_id`` if it is already a full UUID.
      3. A UUID recovered from ``raw_message`` (the dispatch text threaded
         through the tool), preferring one the truncated prefix starts.
    Returns the original value unchanged only if nothing better is found.
    """
    passed = (event_id or "").strip()
    passed_is_full = bool(passed) and bool(_UUID_RE.fullmatch(passed))

    # (1) Room-bound id — the source the LLM never touches.
    bound = _event_id_for_room(room_id)
    if bound and _UUID_RE.fullmatch(bound):
        if not passed_is_full or passed.lower() != bound.lower():
            # Only log a correction when the LLM gave us something different.
            if passed and passed.lower() != bound.lower():
                logger.warning(
                    "Using room-bound event_id %s (LLM tool arg was %r)",
                    bound, event_id,
                )
            return bound

    # (2) LLM arg is already a valid full UUID.
    if passed_is_full:
        return passed

    # (3) Recover from the dispatch text carried as raw_message. Strip @mentions
    # and drop our own agent id first — otherwise a `@[[<agent-uuid>]]` mention
    # (which is NOT the event id) would be recovered as the event id.
    if raw_message:
        own = (_OWN_AGENT_ID or "").lower()
        candidates = [
            c for c in _UUID_RE.findall(_strip_mentions(raw_message))
            if not (own and c.lower() == own)
        ]
        if candidates:
            prefix = passed.lower()
            for cand in candidates:
                if prefix and cand.lower().startswith(prefix):
                    logger.warning(
                        "Recovered full event_id %s from raw_message (LLM passed "
                        "truncated %r)", cand, event_id,
                    )
                    return cand
            logger.warning(
                "Recovered full event_id %s from raw_message (LLM passed %r)",
                candidates[0], event_id,
            )
            return candidates[0]

    if passed:
        logger.error(
            "event_id %r is not a full UUID and could not be recovered (no room "
            "binding, no raw_message match); downstream DB writes may fail",
            event_id,
        )
    return event_id


class _BoundEventIdAdapter(LangGraphAdapter):
    """LangGraph adapter that snapshots the full event_id before the LLM runs.

    The Band runtime delivers each inbound room message to ``on_message`` (with
    the raw text and ``room_id``) BEFORE the LangGraph/LLM turn that decides to
    call the ``processdisaster`` tool. The orchestrator's dispatch text carries
    the full ``event_id: <uuid>`` line, so here we extract that UUID and bind it
    to the room. The tool then resolves the authoritative id by room
    (``_recover_full_event_id`` step 1), which the LLM cannot corrupt — fixing
    the truncated-UUID class of bug at its source rather than recovering after.
    """

    async def on_message(self, msg, *args, room_id: str, **kwargs):  # type: ignore[override]
        try:
            content = getattr(msg, "content", "") or ""
            logger.info(
                "[on_message DEBUG] room=%s content=%r", room_id, content[:400]
            )
            found = _extract_event_id_from_text(content)
            if found:
                _bind_room_event_id(room_id, found)
                logger.info(
                    "Bound full event_id %s to room %s from inbound dispatch",
                    found, room_id,
                )
            # Deterministic dispatch: if this is a genuine NEW disaster dispatch,
            # drive the processdisaster tool ourselves (in the background so the
            # listener stays responsive) instead of relying on the Featherless
            # adapter LLM to emit the tool-call. The LLM still handles this turn
            # conversationally; the tool's process-once guard makes any duplicate
            # tool-call from the LLM a no-op.
            asyncio.create_task(_maybe_autodispatch(content, room_id))
        except Exception:  # noqa: BLE001 - capture must never break message handling
            logger.debug("Could not bind event_id from inbound message")
        return await super().on_message(msg, *args, room_id=room_id, **kwargs)


async def run_pipeline(params: ProcessDisasterInput) -> str:
    """Async tool entry point — runs the (blocking) pipeline off the event loop.

    The Band SDK awaits coroutine tools but calls sync tools directly on its
    WebSocket event loop. The pipeline does blocking work — real imagery I/O and
    the paced ``time.sleep`` anomaly-recovery backoff — which, run inline, would
    starve the WebSocket keepalive and make Band drop the agent mid-job. So this
    coroutine offloads the synchronous pipeline to a worker thread via
    ``asyncio.to_thread``; the heartbeat sleeps and HTTP posts then block that
    thread, not the agent's event loop, and the connection stays alive.
    """
    return await asyncio.to_thread(_run_pipeline_sync, params)


def _run_pipeline_sync(params: ProcessDisasterInput) -> str:
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
    event_id = _recover_full_event_id(
        params.event_id, params.raw_message, room_id=_current_room()
    )
    location = params.location
    disaster_type = params.disaster_type

    # Process-once guard: the LLM may re-trigger this tool when it sees the
    # orchestrator's acks/nudges/summary echo in the room. Each event_id is
    # analysed exactly once; a repeat call returns a short "already complete"
    # WITHOUT re-running the pipeline or re-posting to the room, so the satellite
    # never loops or invents duplicate work. (The orchestrator already has the
    # result from the first completion signal.)
    if event_id in _completed_event_ids:
        logger.info("event %s already processed — skipping duplicate call", event_id)
        return json.dumps(
            {
                "event_id": event_id,
                "status": "complete",
                "already_processed": True,
                "band_message": (
                    f"@hazardmind-orchestrator satellite complete — event {event_id} "
                    "was already analysed and handed off. No further action needed."
                ),
            }
        )

    # Acknowledge receipt immediately so the room shows activity right away.
    _post_ack(event_id, location, disaster_type, params.magnitude)

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
            # The EXPLICIT tool args (params.location / params.disaster_type),
            # extracted by the dispatcher LLM from the orchestrator message, are
            # authoritative. A core field is only genuinely missing when it is
            # absent from BOTH the explicit args AND the re-parsed profile — so a
            # confident dispatch ("flood in Rawalpindi") never triggers a false
            # clarification just because the secondary parse was unsure.
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
        # and surface a compact per-city summary + URLs for the hazard agent.
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
        # still send (people need the data), but the advice is logged + surfaced.
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
            # Per-city artifacts + summaries (multi-city AOIs). Each entry has
            # its own PNGs/GeoJSON URLs and bounds, so the hazard agent and the
            # frontend can show individual city layers, not just the merged one.
            "cities": cities_payload,
            # Expert reasoning from the intelligence layer (point 4).
            "interpretation": interpretation,
            # Confidence is the cross-validation tracker's overall score (a
            # weighted blend of external checks + the expert interpretation),
            # not the LLM's self-rating alone.
            "confidence": confidence,
            # Cross-validation: concerns raised, per-source findings, and the
            # two action flags the orchestrator/frontend care about.
            "concerns": tracker.concerns,
            "validations": validations,
            "needs_verification": needs_verification,
            "should_alert": should_alert,
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
            # Surface both the interpreter's anomalies and the cross-validation
            # concerns so the natural handoff message flags what we're unsure of.
            anomalies=anomalies
            + [f"{c['severity']}: {c['concern']}" for c in tracker.concerns],
            confidence=confidence,
            next_agent_handle=HAZARD_AGENT,
        )
        if band_message:
            structured["band_message"] = band_message
            logger.info("Generated natural Band message (%d chars)", len(band_message))

        # Post the authoritative completion signal to the room ourselves. The
        # orchestrator advances the pipeline only on a genuine completion signal
        # ("satellite complete" marker + structured JSON), and relying on the
        # LLM adapter to relay it verbatim is unreliable — so post it directly.
        _post_completion(
            event_id,
            band_message or f"{HAZARD_AGENT} satellite processing complete.",
            structured,
        )

        _completed_event_ids.add(event_id)
        return json.dumps(structured)
    except Exception as exc:  # noqa: BLE001 - report any failure to the room.
        return _error(event_id, f"Unexpected error: {exc}")


# Custom tool definition. The LangGraph adapter feeds `additional_tools`
# straight into LangChain's `create_agent`/`create_tool`, which requires a real
# LangChain tool (a callable with a `__name__`), not the `(PydanticModel,
# callable)` tuple the old Anthropic adapter accepted. Wrap `run_pipeline` as a
# StructuredTool whose args schema is ProcessDisasterInput; keep the tool name
# `processdisaster` (referenced by SYSTEM_PROMPT).
def _build_process_disaster_tool():
    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import StructuredTool

    async def _coroutine(config: RunnableConfig = None, **kwargs) -> str:
        # LangChain injects the RunnableConfig; the band adapter sets the room
        # id as `thread_id` (see langgraph adapter). Capture it so our direct
        # _post_* signals land in the room the dispatch arrived in (the dynamic
        # per-event room), not a hardcoded static room.
        try:
            thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
            _set_active_room(thread_id)
        except Exception:  # noqa: BLE001 - room capture must never break the tool
            logger.debug("Could not capture dispatch room id from tool config")
        return await run_pipeline(ProcessDisasterInput(**kwargs))

    return StructuredTool.from_function(
        coroutine=_coroutine,
        name="processdisaster",
        description=run_pipeline.__doc__ or "Run the full satellite disaster pipeline.",
        args_schema=ProcessDisasterInput,
    )


PROCESS_DISASTER_TOOL = _build_process_disaster_tool()


# --------------------------------------------------------------------------- #
# System prompt
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = f"""\
You are HazardMind's satellite agent. You process Copernicus/Sentinel satellite \
imagery for disaster zones and report results to the hazard analysis agent.

You have exactly ONE job: when the orchestrator @mentions you with a disaster, \
call the `processdisaster` tool, WAIT for it to finish, and relay its result \
to the room VERBATIM. The tool is the single source of truth. You never decide \
on your own whether data is missing, ambiguous, or complete — the tool tells you.

STRICT RULES — follow exactly:

1. DO NOT post ANY message before the tool returns. No "starting", no "I need \
   clarification", no status notes, no acknowledgements. Your FIRST and ONLY \
   output for a disaster is the tool's result, after it returns. Stay silent \
   while the tool runs (it may take several minutes — that is normal).

2. When the orchestrator @mentions you with a disaster, extract location, \
   disaster_type (flood/earthquake/landslide), magnitude (optional) and \
   event_id, then call `processdisaster` ONCE with those values. ALSO pass the \
   original alert text verbatim as `raw_message`. If you cannot find an \
   event_id, call the tool anyway with the values you have — do NOT post a \
   question instead.

3. The tool returns a JSON object with a `status` field. Relay it like this — \
   and ONLY this. Never write your own version of these messages, never invent \
   a "missing fields" or "clarification" message of your own:

   • status == "complete": the tool returns a `band_message` field (natural, \
     expert prose already addressed to {HAZARD_AGENT}). Post the `band_message` \
     EXACTLY as given, then a line containing only `---`, then the tool's FULL \
     JSON result object exactly as received. Nothing else.

   • status == "clarification_needed": the tool decided the input was genuinely \
     ambiguous. Post the tool's `message` field verbatim, then `---`, then the \
     full JSON object. Do NOT compose your own missing-field list — use only \
     what the tool returned.

   • status == "error": post `{HAZARD_AGENT} satellite processing hit an \
     error.`, then `---`, then the full JSON object.

4. Use ONLY values the tool returned. Invent nothing. If a field is absent from \
   the tool result, it is absent from your message.

5. Call the tool ONCE per event_id, then STOP. After you relay a "complete" \
   result for an event_id, your work for it is DONE. Acknowledgements, progress \
   notes, "thanks", status check-ins, nudges, or the pipeline summary about an \
   event you already finished are informational — DO NOT respond. Silence is \
   correct. Reply again only if the orchestrator explicitly reports a problem \
   with your result or asks you to re-run/verify something specific.
"""


def _require(name: str) -> str:
    """Return env var `name` or exit with a clear message if missing."""
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing required environment variable: {name} (set it in .env)")
    return value


async def _drain_room_backlog(agent_id: str, api_key: str, room_id: str) -> None:
    """Mark all pending/stale messages in the room as processed before startup.

    Uses a short-lived BandLink (separate from the agent runtime) to clear the
    /next queue so the agent starts with a clean slate and only processes
    messages that arrive after it connects. Best-effort: any failure is logged
    and startup continues.
    """
    try:
        from band.platform.link import BandLink
    except Exception:  # noqa: BLE001
        logger.warning("Could not import BandLink; skipping startup backlog drain")
        return

    link = BandLink(agent_id=agent_id, api_key=api_key)
    drained = 0
    try:
        await link.connect()
        for msg in await link.get_stale_processing_messages(room_id):
            await link.mark_processed(room_id, msg.id)
            drained += 1
        while True:
            msg = await link.get_next_message(room_id)
            if msg is None:
                break
            await link.mark_processing(room_id, msg.id)
            await link.mark_processed(room_id, msg.id)
            drained += 1
    except Exception:  # noqa: BLE001 - drain is best-effort
        logger.warning("Startup backlog drain hit an error after %d messages", drained)
    finally:
        try:
            await link.disconnect()
        except Exception:  # noqa: BLE001
            pass
    logger.info("Startup backlog drain: cleared %d message(s) in room %s", drained, room_id)


async def _list_agent_rooms(api_key: str, rest_url: str) -> list[str]:
    """List every Band room this agent is currently a member of.

    The orchestrator creates a fresh per-event room each run and adds us to it;
    we auto-rejoin all of them on (re)connect. To avoid replaying a PREVIOUS
    event's transcript, we must drain the backlog of EVERY joined room at
    startup, not just the static BAND_ROOM_ID. Best-effort: returns [] on error.
    """
    import httpx as _httpx

    rooms: list[str] = []
    try:
        url = f"{rest_url.rstrip('/')}/api/v1/agent/chats?page=1&page_size=100"
        with _httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers={"X-API-Key": api_key})
            resp.raise_for_status()
            body = resp.json()
        items = body.get("data") if isinstance(body, dict) else body
        for item in items or []:
            rid = item.get("id") or item.get("chat_id") if isinstance(item, dict) else None
            if rid:
                rooms.append(str(rid))
    except Exception:  # noqa: BLE001 - listing is best-effort
        logger.warning("Could not list agent rooms for startup drain")
    return rooms


async def _drain_all_rooms(agent_id: str, api_key: str, rest_url: str) -> None:
    """Drain the startup backlog of every room the agent belongs to.

    This is the fix for stale-event replay: on startup the agent rejoins all of
    its old per-event rooms, and without draining each one the SDK would replay
    a previous run's dispatch and re-trigger analysis on a stale event_id.
    """
    rooms = await _list_agent_rooms(api_key, rest_url)
    logger.info("Startup drain: %d joined room(s) to clear", len(rooms))
    for rid in rooms:
        await _drain_room_backlog(agent_id, api_key, rid)


async def main() -> None:
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    agent_id = _require("BAND_AGENT_ID")
    global _OWN_AGENT_ID
    _OWN_AGENT_ID = agent_id
    api_key = _require("BAND_API_KEY")
    featherless_api_key = _require("FEATHERLESS_API_KEY")
    rest_url = os.getenv("THENVOI_REST_URL", "https://app.band.ai")
    ws_url = os.getenv(
        "THENVOI_WS_URL", "wss://app.band.ai/api/v1/socket/websocket"
    )

    # Band-adapter LLM: Featherless (gemma) PRIMARY + Gemini fallback. The handoff
    # JSON is slimmed (no geometry), so a turn fits Featherless's 32k context.
    # Featherless is the workhorse (real capacity) vs Gemini's 20-req/day free
    # tier; its 4-unit concurrency 429 is absorbed by langchain backoff
    # (max_retries=8). Gemini is fallback for the rare oversized/throttled turn.
    feather = ChatOpenAI(
        model=os.getenv("BAND_ADAPTER_MODEL", "google/gemma-4-31B-it"),
        api_key=featherless_api_key,
        base_url="https://api.featherless.ai/v1",
        max_tokens=4096,
        max_retries=8,
    )
    # Gemini fallback chain. Each free-tier key has its own ~20-req/day quota, so
    # we chain BOTH keys after Featherless: gemma (primary) -> Gemini key 1 ->
    # Gemini key 2. When key 1 429s on quota, the adapter falls through to key 2,
    # roughly doubling the daily Gemini budget. gemini-3.1-flash-lite is the
    # default model (confirmed to reliably emit tool-calls via the OpenAI-compat
    # endpoint, unlike gemma).
    _gemini_model = os.getenv("BAND_ADAPTER_FALLBACK_MODEL", "gemini-3.1-flash-lite")
    _gemini_base = "https://generativelanguage.googleapis.com/v1beta/openai/"
    _gemini_fallbacks = []
    for _key_var in (
        "GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
        "GEMINI_API_KEY_4", "GEMINI_API_KEY_5",
    ):
        _k = os.getenv(_key_var)
        if _k:
            _gemini_fallbacks.append(
                ChatOpenAI(
                    model=_gemini_model,
                    api_key=_k,
                    base_url=_gemini_base,
                    max_tokens=4096,
                    max_retries=2,
                )
            )
    llm = feather.with_fallbacks(_gemini_fallbacks) if _gemini_fallbacks else feather
    adapter = _BoundEventIdAdapter(
        llm=llm,
        checkpointer=InMemorySaver(),
        custom_section=SYSTEM_PROMPT,
        additional_tools=[PROCESS_DISASTER_TOOL],
    )

    # Skip history on startup: mark every message already in the room as
    # processed BEFORE the agent's runtime begins, so the SDK's /next backlog
    # sync starts empty. Without this, on every (re)connect the agent replays
    # the whole room history — old dispatches, handoffs and nudges — and its LLM
    # tries to answer each one (slow, and it invents phantom event_ids),
    # jamming it before it reaches the live dispatch. We only ever act on
    # messages that arrive AFTER we're connected.
    # Drain the backlog of EVERY room we're a member of (the static room plus
    # any per-event dynamic rooms we were added to in prior runs), so we never
    # replay a stale event's dispatch on startup.
    await _drain_all_rooms(agent_id, api_key, rest_url)

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
        ws_url=ws_url,
        rest_url=rest_url,
    )

    logger.info("Connecting satellite agent to Band...")
    # Band rate-limits rapid websocket reconnects (HTTP 429 / "reconnect
    # rate-limited after recent supersede") when an agent restarts soon after a
    # previous connection. Retry with exponential backoff so a restart during
    # that window waits the limit out instead of crashing the process.
    for attempt in range(1, 9):
        try:
            await agent.start()
            break
        except Exception as exc:  # noqa: BLE001 - retry transient ws 429s
            msg = str(exc)
            if "429" in msg or "rate-limit" in msg.lower() or "supersede" in msg.lower():
                wait = min(60, 5 * (2 ** (attempt - 1)))
                logger.warning(
                    "Band websocket rate-limited (attempt %d/8); retrying in %ds",
                    attempt, wait,
                )
                await asyncio.sleep(wait)
                continue
            raise
    else:
        logger.error("Satellite could not connect after 8 attempts (Band 429).")
        return
    try:
        logger.info("Connected as: %s. Waiting for disaster mentions...", agent.agent_name)
        await agent.run_forever()
    finally:
        await agent.stop()
        logger.info("Satellite agent disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
