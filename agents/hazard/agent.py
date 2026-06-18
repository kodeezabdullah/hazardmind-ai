import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import httpx
from band import Agent
from band.adapters.langgraph import LangGraphAdapter
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from dotenv import load_dotenv

from analyzer import run_parallel_analysis
from intelligence import quality_check, write_band_message

load_dotenv()

# Next agent in the pipeline (impact). All four pipeline agents are now under
# the single owner @abdullah.gis.services, so we mention the impact agent by its
# real id; the handle is overridable via .env for safety.
IMPACT_HANDLE = os.getenv("IMPACT_HANDLE", "@abdullah.gis.services/hazardmind-impact")
IMPACT_AGENT_ID = os.getenv("IMPACT_AGENT_ID", "7ecaf7d7-d302-4afc-9136-219f94655421")

BAND_AGENT_ID = os.getenv("BAND_AGENT_ID")
AIML_API_KEY = os.getenv("AIML_API_KEY")
NEON_DATABASE_URL = os.getenv("NEON_DATABASE_URL")
BAND_API_KEY = os.getenv("BAND_API_KEY", "")
# Static fallback only. The orchestrator now dispatches each event into a fresh
# per-event room and adds us to it; we post our handoff back into THAT room
# (captured as the LangGraph thread_id — see _set_active_room), not a hardcoded
# room. BAND_ROOM_ID is used only if no dispatch room was captured.
BAND_ROOM_ID = os.getenv("BAND_ROOM_ID")
THENVOI_REST_URL = os.getenv("THENVOI_REST_URL", "https://app.band.ai/")
THENVOI_WS_URL = os.getenv(
    "THENVOI_WS_URL", "wss://app.band.ai/api/v1/socket/websocket"
)

# The room the current disaster was dispatched in (set from the tool's
# RunnableConfig thread_id). Posts go here; falls back to BAND_ROOM_ID.
_active_room = None


def _set_active_room(room_id) -> None:
    global _active_room
    if room_id:
        _active_room = str(room_id)


def _current_room():
    return _active_room or BAND_ROOM_ID


# Defense against the LLM truncating the UUID event_id (it sometimes passes only
# the leading 8-char segment, which the UUID-typed `hazard_zones.event_id` column
# rejects with `invalid UUID ... length must be 32..36`). The Band adapter
# delivers each inbound message to `on_message` BEFORE the LLM runs, so we
# snapshot the full `event_id: <uuid>` from the dispatch text and bind it to the
# room (== the LangGraph thread_id the tool receives). `_resolve_event_id` then
# prefers that authoritative id over whatever the LLM parsed into the payload.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_EVENT_ID_RE = re.compile(
    r"event_id\"?\s*[:=]\s*\"?\s*("
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)
# A Band @mention renders into content as `@[[<agent-uuid>]]`; that UUID is an
# AGENT id, never the event id. The dynamic room's intro/title message @mentions
# this agent but carries no `event_id:` label, so a naive first-bare-UUID scan
# would bind this agent's OWN id as the event id and poison the binding.
_MENTION_RE = re.compile(r"@\[\[[^\]]*\]\]")
_room_event_ids: dict = {}


def _strip_mentions(content):
    return _MENTION_RE.sub(" ", content or "")


def _extract_event_id_from_text(content):
    """Full event_id UUID from dispatch/handoff text: label-anchored, else first.

    Mentions and this agent's own id are excluded from the bare fallback so the
    room intro message can never bind an agent id as the event id.
    """
    if not content:
        return None
    labeled = _EVENT_ID_RE.search(content)
    if labeled:
        return labeled.group(1)
    own = (os.getenv("BAND_AGENT_ID") or "").lower()
    for cand in _UUID_RE.findall(_strip_mentions(content)):
        if own and cand.lower() == own:
            continue
        return cand
    return None


def _bind_room_event_id(room_id, event_id) -> None:
    if room_id and event_id:
        _room_event_ids[str(room_id)] = str(event_id)


def _resolve_event_id(event_id, room_id=None) -> str:
    """Return the full-UUID event_id, preferring the room-bound one over the LLM's.

    Order: (1) the full UUID captured from the room's inbound dispatch (the LLM
    never touches it); (2) the passed id if already a full UUID; else the passed
    id unchanged (logged) so the caller can still report a clean error.
    """
    passed = str(event_id or "").strip()
    bound = _room_event_ids.get(str(room_id)) if room_id else None
    if bound and _UUID_RE.fullmatch(bound):
        if passed and passed.lower() != bound.lower():
            print(
                f"[hazard] using room-bound event_id {bound} "
                f"(payload had {passed!r})",
                flush=True,
            )
        return bound
    if passed and _UUID_RE.fullmatch(passed):
        return passed
    if passed:
        print(
            f"[hazard] event_id {passed!r} is not a full UUID and no room "
            "binding was found; DB write may fail",
            flush=True,
        )
    return passed


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


async def send_to_band(message_text: str, agent_id: str = IMPACT_AGENT_ID) -> None:
    """Post a message into the dispatch Band room, @mentioning the target agent."""
    room_id = _current_room()
    url = f"{THENVOI_REST_URL.rstrip('/')}/api/v1/agent/chats/{room_id}/messages"
    headers = {"X-API-Key": BAND_API_KEY}
    body = {
        "message": {
            "content": message_text,
            "mentions": [{"id": agent_id}],
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()


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
    # (the LLM sometimes passes the whole handoff object through).
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


async def analyze_hazard(satellite_payload: dict, send_message) -> dict:
    # Prefer the full event_id captured from the inbound dispatch (room-bound)
    # over whatever the LLM parsed into the payload, which may be truncated.
    event_id = _resolve_event_id(
        satellite_payload.get("event_id", "unknown"), room_id=_current_room()
    )
    satellite_payload["event_id"] = event_id
    try:
        # CONTRACT ADAPTER (Solution 1): the satellite emits a FLAT payload —
        # bbox / affected_area_km2 / mean_index / water_percent / risk_cities /
        # satellite_type all live at the TOP LEVEL. The analyzer, however, reads
        # the NESTED shape (boundaries.bbox, analysis.affected_area_km2,
        # analysis.mean_value, satellite.type). Previously this mismatch made the
        # analyzer see an empty bbox -> "invalid bbox" -> every risk UNKNOWN and a
        # hardcoded HIGH severity (a non-disaster stamped as a disaster). Here we
        # normalise the flat payload into the nested shape, preferring an
        # already-nested value when present (backward compatible).
        satellite_data = _normalise_satellite_payload(satellite_payload, event_id)

        raw_result = await run_parallel_analysis(satellite_data)

        qc = await quality_check(raw_result)
        if not qc["passed"]:
            error_msg = json.dumps(
                {
                    "agent": "hazardmind-hazard",
                    "event_id": event_id,
                    "status": "error",
                    "error": "quality check failed",
                }
            )
            await send_message(IMPACT_HANDLE, error_msg)
            return {"status": "error"}

        # DB write BEFORE Band post — if it fails, report error and stop.
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

        message = await write_band_message(raw_result, IMPACT_HANDLE)
        # Standard handoff format across all agents: natural prose, a `---`
        # separator line, then the structured JSON payload on the tail.
        full_message = message + "\n\n---\n" + json.dumps(payload, indent=2)
        await send_message(IMPACT_HANDLE, full_message)

        return payload

    except Exception as e:
        error_msg = json.dumps(
            {
                "agent": "hazardmind-hazard",
                "event_id": event_id,
                "status": "error",
                "error": str(e),
            }
        )
        await send_message(IMPACT_HANDLE, error_msg)
        return {"status": "error", "reason": str(e)}


async def analyze_hazard_tool(satellite_payload: dict, config=None) -> dict:
    """Band tool entrypoint. Runs analysis and posts the handoff to impact via Band.

    `config` is the LangChain RunnableConfig; the band adapter puts the dispatch
    room id in `configurable.thread_id`, which we capture so the handoff posts
    into the per-event room rather than a hardcoded one.
    """
    try:
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
        _set_active_room(thread_id)
    except Exception:  # noqa: BLE001 - room capture must never break the tool
        pass

    async def send_message(handle: str, message: str) -> None:
        await send_to_band(message, agent_id=IMPACT_AGENT_ID)

    return await analyze_hazard(satellite_payload, send_message)


SYSTEM_PROMPT = """You are HazardMind's hazard detection agent (Agent 2 of 4).

You have exactly ONE job: when you are @mentioned with a satellite result (the
orchestrator or satellite agent hands it to you), call the `analyze_hazard` tool
with the parsed payload, WAIT for it to finish, and let it post the handoff to
the impact agent. The tool is the single source of truth.

STRICT RULES — follow exactly:
1. DO NOT post anything before the tool returns. No "starting", no acknowledgements,
   no status notes. Stay silent while it runs.
2. When @mentioned with a satellite/handoff result, extract the JSON payload from
   the message tail (after the `---` line) — parse event_id, boundaries, analysis,
   artifacts — and call `analyze_hazard` ONCE with that payload. If you cannot
   find a field, pass what you have; do NOT post a question instead.
3. The tool itself posts the natural handoff + JSON to the impact agent. You do
   NOT compose your own handoff, risk numbers, or "missing fields" message — use
   only what the tool produced. After the tool returns, you are done; say nothing
   further.
4. Call the tool ONCE per event_id. Acknowledgements, nudges, and summaries about
   an event you already handled are informational — do not respond.
"""


def _build_analyze_hazard_tool():
    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import StructuredTool

    async def _coroutine(satellite_payload: dict, config: RunnableConfig = None) -> str:
        result = await analyze_hazard_tool(satellite_payload, config=config)
        return json.dumps(result)

    return StructuredTool.from_function(
        coroutine=_coroutine,
        name="analyze_hazard",
        description=(
            "Run multi-hazard analysis on the satellite payload, write hazard_zones, "
            "and post the handoff to the impact agent. Pass the full parsed satellite "
            "JSON payload as satellite_payload."
        ),
    )


ANALYZE_HAZARD_TOOL = _build_analyze_hazard_tool()


# event_ids we have already auto-dispatched the analysis for. Guards against
# firing analyze_hazard twice (e.g. on an orchestrator nudge echoing the handoff).
_autodispatched_event_ids: set = set()


def _extract_satellite_payload(content: str):
    """Pull the satellite's structured JSON payload from a handoff message.

    The satellite posts: natural prose, a line `satellite complete`, a `---`
    separator, then the slimmed JSON result object (flat: event_id, bbox,
    affected_area_km2, mean_index, water_percent, risk_cities, satellite_type).
    We extract and parse that trailing JSON so we can drive analyze_hazard
    ourselves — rather than depending on the Featherless adapter LLM to emit the
    tool-call, which it frequently skips (replying "hazard complete" in prose and
    leaving the DB row unwritten — the bug that made the pipeline only LOOK done).
    """
    if not content or "{" not in content:
        return None
    # Take the JSON after the last `---` separator if present, else the whole text.
    tail = content.rsplit("---", 1)[-1]
    start = tail.find("{")
    if start == -1:
        return None
    # Balance braces to capture the full JSON object even with trailing prose.
    depth = 0
    for i in range(start, len(tail)):
        if tail[i] == "{":
            depth += 1
        elif tail[i] == "}":
            depth -= 1
            if depth == 0:
                blob = tail[start : i + 1]
                try:
                    obj = json.loads(blob)
                except (ValueError, TypeError):
                    return None
                # Unwrap a wrapper envelope if the satellite nested its data.
                if isinstance(obj, dict):
                    for key in ("data", "result", "satellite"):
                        inner = obj.get(key)
                        if isinstance(inner, dict) and (
                            "bbox" in inner or "affected_area_km2" in inner
                        ):
                            # Carry the event_id up if only the envelope had it.
                            inner.setdefault("event_id", obj.get("event_id"))
                            return inner
                return obj
    return None


def _is_satellite_handoff(content: str) -> bool:
    """True when a message carries the satellite result for hazard to analyse.

    The handoff reaches hazard in two shapes:
      1. directly from the satellite agent ("satellite complete\\n---\\n{json}")
      2. relayed by the orchestrator to hazard, with the satellite result nested
         under a `{"from": "satellite", "data": {...bbox...}}` envelope.
    Either way, the ONLY thing that matters is whether a parseable payload with a
    bbox / affected_area is present (a nudge or chit-chat has none). We key on the
    payload itself rather than a specific marker string, so the orchestrator's
    relayed handoff (which lacks the literal "satellite complete" marker) is
    detected too — that omission is exactly why hazard sat idle in the live run.
    """
    if not content or "{" not in content:
        return False
    payload = _extract_satellite_payload(content)
    return bool(payload) and ("bbox" in payload or "affected_area_km2" in payload)


def _fetch_room_satellite_handoff(room_id: str) -> Optional[str]:
    """Fetch recent room messages via REST and return the satellite handoff text.

    Resilience fix: the live websocket message that triggers on_message is
    sometimes NOT the satellite handoff (it can be the orchestrator's dispatch),
    or the handoff arrives while our websocket is reconnecting and we miss it.
    Rather than depend on catching the exact live message, we pull the room's
    recent history over REST and find the satellite's completion handoff there.
    Returns the message content, or None.
    """
    api_key = os.getenv("BAND_API_KEY")
    rest_url = os.getenv("THENVOI_REST_URL", "https://app.band.ai").rstrip("/")
    if not api_key or not room_id:
        return None
    try:
        import httpx

        url = f"{rest_url}/api/v1/agent/chats/{room_id}/messages?page=1&page_size=50"
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers={"X-API-Key": api_key})
            resp.raise_for_status()
            body = resp.json()
        items = body.get("data") if isinstance(body, dict) else body
        # Newest-last or newest-first — scan all, prefer the most recent handoff.
        candidates = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            text = item.get("content") or item.get("message") or ""
            if isinstance(text, dict):
                text = text.get("content", "")
            if text and _is_satellite_handoff(text):
                candidates.append(text)
        return candidates[-1] if candidates else None
    except Exception as exc:  # noqa: BLE001 - REST fallback is best-effort
        print(f"[hazard] room history fetch failed: {exc}", flush=True)
        return None


async def _fetch_satellite_result_from_db(event_id: str):
    """Read the satellite's result row from the DB (the reliable hand-off path).

    Band's REST history is empty for per-event rooms, so the orchestrator forwards
    an empty ``data: {}`` to hazard. The satellite now persists its result to
    ``satellite_results`` (see satellite/_persist_satellite_result); we read it
    back here and shape it like the room payload the analyzer expects. Returns a
    flat dict (bbox/affected_area_km2/mean_index/water_percent/risk_cities/
    satellite_type/event_id) or None.
    """
    db_url = os.getenv("NEON_DATABASE_URL")
    if not db_url or not event_id or not _UUID_RE.fullmatch(event_id):
        return None
    try:
        conn = await asyncpg.connect(db_url)
        try:
            row = await conn.fetchrow(
                "SELECT * FROM satellite_results WHERE event_id=$1", event_id
            )
        finally:
            await conn.close()
        if not row:
            return None
        d = dict(row)

        def _loads(v):
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except (ValueError, TypeError):
                    return v
            return v

        return {
            "event_id": event_id,
            "bbox": _loads(d.get("bbox")),
            "bounds": _loads(d.get("bounds")),
            "risk_cities": _loads(d.get("risk_cities")) or [],
            "affected_area_km2": d.get("affected_area_km2"),
            "satellite_type": d.get("satellite_type") or "sentinel-2",
            # mean_index/water_percent aren't columns; the analyzer derives risk
            # from affected_area + bbox, and a missing index defaults safely.
        }
    except Exception as exc:  # noqa: BLE001 - DB read is best-effort
        print(f"[hazard] DB satellite read failed: {exc}", flush=True)
        return None


async def _maybe_autodispatch_hazard(content: str, room_id: str) -> None:
    """Deterministically run hazard analysis on a genuine satellite handoff.

    Resolution order for the satellite payload (most reliable last):
      1. the live message itself, if it carries the handoff JSON;
      2. the room's REST history (often empty for per-event rooms);
      3. the DB ``satellite_results`` row the satellite persisted — the
         dependable channel, since Band's REST history is empty and the
         orchestrator forwards an empty ``data: {}``.
    Invokes analyze_hazard_tool directly (runs analysis, writes hazard_zones,
    hands off to impact). Fires at most once per event. Never raises.
    """
    # Resolve the event_id from the room binding up front so the DB fallback works
    # even when the message carries no payload.
    bound_event = _room_event_ids.get(str(room_id)) or _extract_event_id_from_text(content)

    handoff_text = content if _is_satellite_handoff(content) else None
    if handoff_text is None:
        handoff_text = await asyncio.to_thread(_fetch_room_satellite_handoff, room_id)

    payload = _extract_satellite_payload(handoff_text) if handoff_text else None

    # DB fallback — the reliable path when the room carried no usable payload.
    if not payload and bound_event:
        payload = await _fetch_satellite_result_from_db(bound_event)
        if payload:
            print(f"[hazard] loaded satellite payload from DB for {bound_event}", flush=True)

    if not payload:
        return

    event_id = _resolve_event_id(payload.get("event_id", bound_event or "unknown"), room_id=room_id)
    if event_id in _autodispatched_event_ids:
        return
    _autodispatched_event_ids.add(event_id)
    payload["event_id"] = event_id
    _set_active_room(room_id)
    print(
        f"[hazard][autodispatch] satellite handoff for event {event_id} — "
        f"driving analyze_hazard directly",
        flush=True,
    )
    try:
        await analyze_hazard_tool(
            payload, config={"configurable": {"thread_id": room_id}}
        )
    except Exception as exc:  # noqa: BLE001 - report in-room, never crash the listener
        print(f"[hazard][autodispatch] analysis failed for {event_id}: {exc}", flush=True)
        _autodispatched_event_ids.discard(event_id)


class _BoundEventIdAdapter(LangGraphAdapter):
    """Snapshot the full event_id from each inbound dispatch before the LLM runs.

    The orchestrator/satellite dispatch text carries the full ``event_id: <uuid>``
    line; binding it to the room here lets ``_resolve_event_id`` use the
    authoritative id even if the LLM later truncates it when parsing the payload.
    """

    async def on_message(self, msg, *args, room_id: str, **kwargs):  # type: ignore[override]
        try:
            content = getattr(msg, "content", "") or ""
            found = _extract_event_id_from_text(content)
            if found:
                _bind_room_event_id(room_id, found)
                print(
                    f"[hazard] bound event_id {found} to room {room_id}",
                    flush=True,
                )
            # Deterministic dispatch: drive analyze_hazard ourselves. AWAIT inline
            # (not create_task) so it runs to completion within the message
            # lifecycle — a detached task gets orphaned if the websocket drops
            # mid-analysis (the bug that left hazard_zones unwritten). We trigger
            # when THIS message is the satellite handoff OR when it mentions the
            # satellite/handoff context (then the REST fallback inside finds the
            # actual handoff in room history — covers the case where we missed the
            # live handoff post during a websocket reconnect).
            low = content.lower()
            trigger = (
                _is_satellite_handoff(content)
                or "satellite" in low
                or "affected area" in low
                or "1.148" in content
                or "km" in low
            )
            print(
                f"[hazard] on_message: trigger={trigger} "
                f"is_handoff={_is_satellite_handoff(content)} len={len(content)}",
                flush=True,
            )
            if trigger:
                await _maybe_autodispatch_hazard(content, room_id)
        except Exception as exc:  # noqa: BLE001 - capture must never break handling
            print(f"[hazard] on_message autodispatch error: {exc!r}", flush=True)
        return await super().on_message(msg, *args, room_id=room_id, **kwargs)


def _build_adapter_llm():
    """Band-adapter LLM: Featherless (gemma) PRIMARY, Gemini fallback.

    The handoff JSON is now slimmed (no region_boundary/geometry), so a turn fits
    in Featherless's 32k context. Featherless is therefore the workhorse: it has
    real capacity, vs Gemini's 20-request/day free tier which is too small to be
    primary. Featherless's 4-unit concurrency cap (shared across agents) is
    handled by langchain's own 429 backoff (max_retries=8). Gemini is the
    fallback for the rare oversized/throttled turn. Needs GEMINI_API_KEY.
    """
    feather = ChatOpenAI(
        model=os.getenv("BAND_ADAPTER_MODEL", "google/gemma-4-31B-it"),
        api_key=os.getenv("FEATHERLESS_API_KEY"),
        base_url="https://api.featherless.ai/v1",
        max_tokens=4096,
        max_retries=1,  # fail FAST to Gemini: long 429 backoff starved the ws keepalive
    )
    # Multi-key Gemini fallback chain. Featherless stays PRIMARY (the showcase
    # integration); when its shared 4-unit concurrency 429s, the adapter falls
    # through these Gemini keys in order. Each free key has its own quota, so
    # chaining several keys makes the 429 storm effectively disappear.
    model = os.getenv("BAND_ADAPTER_FALLBACK_MODEL", "gemini-3.1-flash-lite")
    base = "https://generativelanguage.googleapis.com/v1beta/openai/"
    fallbacks = []
    for key_var in (
        "GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
        "GEMINI_API_KEY_4", "GEMINI_API_KEY_5",
    ):
        k = os.getenv(key_var)
        if k:
            fallbacks.append(
                ChatOpenAI(model=model, api_key=k, base_url=base, max_tokens=4096, max_retries=2)
            )
    return feather.with_fallbacks(fallbacks) if fallbacks else feather


llm = _build_adapter_llm()
adapter = _BoundEventIdAdapter(
    llm=llm,
    checkpointer=InMemorySaver(),
    custom_section=SYSTEM_PROMPT,
    additional_tools=[ANALYZE_HAZARD_TOOL],
)

async def _main() -> None:
    # Emit INFO logs (Band runtime connection/room-join + our own) to stderr so
    # the agent's connection state is visible. Without this the process was a
    # black box on startup — silence looked identical to "connected" and to
    # "silently stuck".
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print("[hazard] starting up, connecting to Band...", flush=True)

    from room_drain import drain_all_rooms

    # Drain every joined room's backlog so we never replay a prior event's
    # handoff and re-run analysis on a stale event_id.
    await drain_all_rooms(
        os.getenv("BAND_AGENT_ID", ""), os.getenv("BAND_API_KEY", ""), THENVOI_REST_URL
    )
    agent = Agent.create(
        adapter=adapter,
        agent_id=os.getenv("BAND_AGENT_ID"),
        api_key=os.getenv("BAND_API_KEY"),
        ws_url=THENVOI_WS_URL,
        rest_url=THENVOI_REST_URL,
    )
    # Band rate-limits rapid websocket reconnects (HTTP 429 / "reconnect
    # rate-limited after recent supersede") when an agent restarts soon after a
    # prior connection. Retry with backoff so a restart waits the window out
    # instead of crashing the process.
    for attempt in range(1, 9):
        try:
            await agent.run()
            break
        except Exception as exc:  # noqa: BLE001 - retry transient ws 429s
            msg = str(exc)
            if "429" in msg or "rate-limit" in msg.lower() or "supersede" in msg.lower():
                wait = min(60, 5 * (2 ** (attempt - 1)))
                print(
                    f"[hazard] Band websocket rate-limited (attempt {attempt}/8); "
                    f"retrying in {wait}s", flush=True,
                )
                await asyncio.sleep(wait)
                continue
            raise
    else:
        print("[hazard] could not connect after 8 attempts (Band 429).", flush=True)


if __name__ == "__main__":
    asyncio.run(_main())
