"""HazardMind Impact Assessment Agent — Band SDK entry point.

Run with:  python agent.py

Listens for @mentions from hazardmind-hazard via Band WebSocket,
runs the 3-task impact pipeline, writes to Neon DB, and sends
the completion signal to hazardmind-orchestrator.
"""

import asyncio
import json
import logging
import os
import traceback

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

from services.band_client import (
    send_anomaly_to_band,
    send_to_band_room,
    set_active_room,
)
from services.db import write_impact_data
from tasks.infrastructure import run_infrastructure_task
from tasks.population import run_population_task
from tasks.vulnerability import run_vulnerability_task

import re

# Defense against the LLM truncating the UUID event_id (the UUID-typed
# impact_data.event_id column rejects a short id). The Band adapter delivers
# each inbound dispatch to on_message BEFORE the LLM runs, so we snapshot the
# full `event_id: <uuid>` and bind it to the room (== LangGraph thread_id the
# tool receives). _resolve_event_id prefers that authoritative id.
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
    """Full-UUID event_id, preferring the room-bound one over the LLM-supplied."""
    passed = str(event_id or "").strip()
    bound = _room_event_ids.get(str(room_id)) if room_id else None
    if bound and _UUID_RE.fullmatch(bound):
        if passed and passed.lower() != bound.lower():
            logger.warning(
                "[agent] using room-bound event_id %s (tool arg was %r)",
                bound, event_id,
            )
        return bound
    if passed and _UUID_RE.fullmatch(passed):
        return passed
    if passed:
        logger.error(
            "[agent] event_id %r is not a full UUID and no room binding found; "
            "DB write may fail", event_id,
        )
    return passed

SYSTEM_PROMPT = """You are HazardMind's impact assessment agent (Agent 3 of 4).

You have exactly ONE job: when you are @mentioned with a hazard result, call the
`run_impact_analysis` tool with the parsed fields, WAIT for it to finish, and let
it post the completion signal. The tool is the single source of truth.

STRICT RULES — follow exactly:
1. DO NOT post anything before the tool returns. No "starting", no acknowledgements,
   no status notes, no questions. Stay silent while it runs.
2. When @mentioned with a hazard/handoff result, parse the JSON payload from the
   message tail (after the `---` line). Extract event_id (use it exactly — NEVER
   generate your own), bounds, risk_level, severity, hazard zones, flood depth,
   confidence, and risk_cities, then call `run_impact_analysis` ONCE with them.
   If a field is absent, pass a sensible empty/default — do NOT post a question.
3. The tool runs the analysis, writes the DB, and posts the natural completion +
   JSON to the orchestrator itself. You do NOT compose your own summary, numbers,
   or "missing fields" message. After the tool returns, you are done — say nothing.
4. Call the tool ONCE per event_id. Acknowledgements, nudges and summaries about an
   event you already handled are informational — do not respond."""


def _no_significant_disaster(risk_level: str, overall_confidence: float) -> bool:
    """True when the hazard verdict means there is no real disaster to assess.

    A NEUTRAL verification ("is there flooding?") can legitimately answer "no".
    We treat the event as no-significant-disaster when the hazard risk is
    LOW / NONE / UNKNOWN. (UNKNOWN means hazard could not confirm a hazard — the
    honest impact is zero, not an invented population.) Overridable via
    IMPACT_FORCE_ASSESS=true for debugging.
    """
    if os.getenv("IMPACT_FORCE_ASSESS", "").lower() == "true":
        return False
    rl = str(risk_level or "").strip().upper()
    return rl in ("LOW", "NONE", "UNKNOWN", "", "MINIMAL", "NEGLIGIBLE")


async def _emit_no_impact(
    event_id: str, risk_cities: list, risk_level: str, overall_confidence: float
) -> str:
    """Emit an honest zero-impact completion (no fabricated population)."""
    city = risk_cities[0] if risk_cities else event_id
    json_data = {
        "event_id": event_id,
        "agent": "hazardmind-impact",
        "from": "hazardmind-impact",
        "to": "hazardmind-report",
        "status": "complete",
        "step": "impact",
        "anomalies": [],
        "data": {
            "total_affected": 0,
            "high_risk_people": 0,
            "medium_risk_people": 0,
            "hospitals_at_risk": 0,
            "schools_at_risk": 0,
            "roads_blocked": 0.0,
            "bridges_at_risk": 0,
            "vulnerability_score": "0",
            "evacuation_routes": [],
            "estimated_evacuation_time": "N/A",
            "overall_confidence": overall_confidence,
            "no_significant_impact": True,
            "assessment_note": (
                f"Hazard risk assessed as {str(risk_level).upper()} for {city}; "
                "no significant disaster impact detected. No population or "
                "infrastructure reported at risk."
            ),
        },
    }
    natural_text = (
        f"@hazardmind-orchestrator\n"
        f"Impact assessment complete for {city}. No significant disaster impact "
        f"detected — hazard risk is {str(risk_level).upper()}. "
        f"0 population at risk, no infrastructure affected. "
        f"Handing off to report agent for an all-clear summary."
    )
    message = f"{natural_text}\n\n---\n{json.dumps(json_data, indent=2)}"
    try:
        await send_to_band_room(message)
    except Exception as exc:  # noqa: BLE001 - band send is non-fatal
        logger.error("[agent] no-impact band send failed (non-fatal): %s", exc)
    # Persist the honest zero row too (so the DB reflects "assessed, no impact").
    if os.environ.get("NEON_DATABASE_URL"):
        try:
            zero = json_data["data"]
            await write_impact_data(
                event_id,
                {"population_affected": 0, "high_risk_people": 0, "medium_risk_people": 0},
                {"hospitals_at_risk": 0, "schools_at_risk": 0, "roads_blocked_km": 0, "bridges_at_risk": 0},
                {"vulnerability_score": "0", "priority_zones": [], "estimated_evacuation_time": "N/A"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[agent] no-impact DB write failed (non-fatal): %s", exc)
    logger.info("[agent] No-impact completion sent — event_id=%s", event_id)
    return json.dumps(json_data)


async def run_impact_analysis(
    event_id: str,
    bounds: dict,
    risk_level: str,
    severity: str,
    hazard_zones_geojson: dict,
    flood_depth_estimate: float,
    overall_confidence: float,
    risk_cities: list,
    config=None,
) -> str:
    """Run the full impact assessment pipeline for a disaster event.

    Args:
        event_id: Unique event identifier from orchestrator — never generate your own.
        bounds: Bounding box with keys west, south, east, north.
        risk_level: Overall risk level (LOW/MEDIUM/HIGH/CRITICAL).
        severity: Disaster severity descriptor.
        hazard_zones_geojson: GeoJSON of affected hazard zones.
        flood_depth_estimate: Estimated flood depth in metres.
        overall_confidence: Confidence score 0-1 from hazard agent.
        risk_cities: List of city names in the affected area.
    """
    # Capture the dispatch room (LangGraph thread_id) so our completion signal
    # posts into the per-event room, not a hardcoded one.
    thread_id = None
    try:
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
        set_active_room(thread_id)
    except Exception:  # noqa: BLE001 - room capture must never break the tool
        pass

    # Prefer the full event_id captured from the inbound dispatch over the
    # (possibly truncated) value the LLM parsed into the tool argument.
    event_id = _resolve_event_id(event_id, room_id=thread_id)

    logger.info(
        "[agent] run_impact_analysis — event_id=%s risk_level=%s cities=%s",
        event_id, risk_level, risk_cities,
    )

    bbox = [
        bounds.get("west", 0),
        bounds.get("south", 0),
        bounds.get("east", 1),
        bounds.get("north", 1),
    ]

    hazard_data = {
        "event_id": event_id,
        "bbox": bbox,
        "risk_cities": risk_cities,
        "flood_risk": risk_level,
        "earthquake_risk": "LOW",
        "landslide_risk": "LOW",
        "severity": severity,
        "flood_depth_estimate": flood_depth_estimate,
        "hazard_zones_geojson": hazard_zones_geojson,
    }

    # ── DECISION GATE (Solution 4): no-significant-disaster verdict ───────────
    # This is a NEUTRAL verification pipeline. If the hazard stage found no real
    # hazard (risk LOW/UNKNOWN/NONE), the honest answer is "no significant impact
    # — 0 affected", NOT a fabricated population. Without this gate the LLM is
    # prompted to always invent affected people, turning a clean "no flood"
    # result into a phantom disaster.
    if _no_significant_disaster(risk_level, overall_confidence):
        logger.info(
            "[agent] No-significant-disaster verdict (risk=%s conf=%.2f) — "
            "reporting zero impact honestly for event_id=%s",
            risk_level, overall_confidence, event_id,
        )
        return await _emit_no_impact(
            event_id, risk_cities, risk_level, overall_confidence
        )

    try:
        logger.info("[agent] Running population + infrastructure in parallel")
        pop, infra = await asyncio.gather(
            run_population_task(hazard_data, event_id),
            run_infrastructure_task(hazard_data, event_id),
        )

        logger.info("[agent] Running vulnerability task")
        vuln = await run_vulnerability_task(
            hazard_data=hazard_data,
            population_result=pop,
            infrastructure_result=infra,
        )
    except Exception:
        tb = traceback.format_exc()
        logger.error("[agent] Pipeline failed:\n%s", tb)
        error_msg = (
            f"@hazardmind-orchestrator\n"
            f"ERROR: Impact assessment failed for event {event_id}.\n"
            f"```\n{tb[-800:]}\n```"
        )
        await send_to_band_room(error_msg)
        return json.dumps({"event_id": event_id, "status": "error", "error": str(tb[-400:])})

    # ── DB write (non-fatal) ─────────────────────────────────────────────────
    if os.environ.get("NEON_DATABASE_URL"):
        try:
            await write_impact_data(event_id, pop, infra, vuln)
            logger.info("[agent] DB write complete for event_id=%s", event_id)
        except Exception as exc:
            logger.error("[agent] DB write failed (non-fatal): %s", exc)
    else:
        logger.warning("[agent] NEON_DATABASE_URL not set — skipping DB write")

    # ── Anomaly flags ────────────────────────────────────────────────────────
    hospitals = int(infra.get("hospitals_at_risk", 0) or 0)
    if hospitals > 10:
        await send_anomaly_to_band(
            f"@hazardmind-orchestrator\n"
            f"CRITICAL: {hospitals} hospitals in disaster zone for event {event_id}.\n"
            f"Immediate NDMA Level-3 response recommended."
        )

    if overall_confidence < 0.7:
        await send_anomaly_to_band(
            f"@hazardmind-orchestrator\n"
            f"Low confidence ({overall_confidence:.2f}) on impact data for event {event_id}.\n"
            f"Proceeding with caution — recommend field verification."
        )

    # ── Derive city name for completion signal ───────────────────────────────
    city = (risk_cities[0] if risk_cities else event_id)
    pop_count = int(pop.get("population_affected", 0) or 0)
    vuln_score = vuln.get("vulnerability_score", 0)

    natural_text = (
        f"@hazardmind-orchestrator\n"
        f"Impact assessment complete for {city}.\n"
        f"{pop_count:,} population in affected zones.\n"
        f"{hospitals} hospitals at risk — "
        + ("CRITICAL: Immediate NDMA notification recommended." if hospitals > 10 else "monitoring required.")
        + f"\nVulnerability score: {vuln_score}/10\n"
        f"Handing off to report agent."
    )

    json_data = {
        "event_id": event_id,
        "agent": "hazardmind-impact",
        "from": "hazardmind-impact",
        "to": "hazardmind-report",
        "status": "complete",
        "step": "impact",
        "anomalies": [],
        "data": {
            "total_affected": pop_count,
            "high_risk_people": int(pop.get("high_risk_people", int(pop_count * 0.2)) or int(pop_count * 0.2)),
            "medium_risk_people": int(pop.get("medium_risk_people", int(pop_count * 0.5)) or int(pop_count * 0.5)),
            "hospitals_at_risk": hospitals,
            "schools_at_risk": int(infra.get("schools_at_risk", 0) or 0),
            "roads_blocked": round(float(infra.get("roads_blocked_km", 0) or 0), 1),
            "bridges_at_risk": int(infra.get("bridges_at_risk", 0) or 0),
            "vulnerability_score": str(vuln_score),
            "evacuation_routes": vuln.get("priority_zones", []),
            "estimated_evacuation_time": (
                infra.get("estimated_evacuation_time")
                or vuln.get("estimated_evacuation_time", "4-6 hours")
            ),
            "overall_confidence": overall_confidence,
        },
    }

    message = f"{natural_text}\n\n---\n{json.dumps(json_data, indent=2)}"
    await send_to_band_room(message)

    logger.info(
        "[agent] Completion signal sent — event_id=%s pop=%d hospitals=%d score=%s",
        event_id, pop_count, hospitals, vuln_score,
    )
    return json.dumps(json_data)


# event_ids we have already auto-dispatched impact analysis for.
_autodispatched_event_ids: set = set()


def _extract_hazard_payload(content: str):
    """Pull the hazard agent's structured JSON from a handoff message.

    Hazard posts: natural prose, a `---` separator, then a JSON object with
    `event_id` and a nested `hazard` block (flood_risk, overall_severity,
    confidence_scores, ...). We parse that so impact can run deterministically
    instead of relying on the Featherless adapter LLM to emit the tool-call
    (which it skips, leaving impact_data unwritten).
    """
    if not content or "{" not in content:
        return None
    tail = content.rsplit("---", 1)[-1]
    start = tail.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(tail)):
        if tail[i] == "{":
            depth += 1
        elif tail[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(tail[start : i + 1])
                except (ValueError, TypeError):
                    return None
    return None


def _is_hazard_handoff(payload) -> bool:
    """True when a parsed payload is the hazard agent's completion handoff.

    Detected in three shapes:
      1. directly from hazard: agent="hazardmind-hazard" + nested "hazard" block
      2. relayed by the orchestrator to impact: {"from":"hazard","data":{...}}
      3. a flat payload carrying the hazard risk fields (flood_risk/risk_level)
    Keying on `from`/`agent`/risk-fields (not just the literal "hazard" block)
    is what lets the orchestrator's RELAYED handoff trigger impact — its absence
    is exactly why impact sat idle / mis-fired in the live run.
    """
    if not isinstance(payload, dict):
        return False
    tag = str(payload.get("agent") or payload.get("from") or "").lower()
    if "hazard" in tag:
        return True
    if isinstance(payload.get("hazard"), dict):
        return True
    inner = payload.get("data")
    if isinstance(inner, dict) and (
        "flood_risk" in inner or "risk_level" in inner or "overall_severity" in inner
    ):
        return True
    return "flood_risk" in payload or "risk_level" in payload


def _impact_args_from_hazard(payload: dict, event_id: str) -> dict:
    """Map a hazard handoff payload to run_impact_analysis's structured args.

    Unwraps the result from whichever envelope it arrived in: a nested "hazard"
    block, the orchestrator's "data" relay block, or a flat payload.
    """
    if isinstance(payload.get("hazard"), dict):
        hazard = payload["hazard"]
    elif isinstance(payload.get("data"), dict) and (
        "flood_risk" in payload["data"]
        or "risk_level" in payload["data"]
        or "overall_severity" in payload["data"]
    ):
        hazard = payload["data"]
    else:
        hazard = payload
    risk_level = (
        hazard.get("flood_risk")
        or hazard.get("overall_severity")
        or hazard.get("risk_level")
        or "UNKNOWN"
    )
    severity = hazard.get("overall_severity") or hazard.get("severity") or risk_level
    conf = hazard.get("confidence_scores") or {}
    if isinstance(conf, dict) and conf:
        try:
            overall_conf = sum(float(v) for v in conf.values()) / len(conf)
        except (TypeError, ValueError, ZeroDivisionError):
            overall_conf = float(hazard.get("overall_confidence", 0.0) or 0.0)
    else:
        overall_conf = float(hazard.get("overall_confidence", 0.0) or 0.0)

    bounds = payload.get("bounds") or hazard.get("bounds") or {}
    risk_cities = (
        payload.get("risk_cities")
        or hazard.get("risk_cities")
        or []
    )
    return {
        "event_id": event_id,
        "bounds": bounds if isinstance(bounds, dict) else {},
        "risk_level": str(risk_level).upper(),
        "severity": str(severity),
        "hazard_zones_geojson": hazard.get("risk_polygons") or {},
        "flood_depth_estimate": float(hazard.get("flood_depth_estimate", 0.0) or 0.0),
        "overall_confidence": overall_conf,
        "risk_cities": risk_cities,
    }


def _fetch_room_hazard_handoff(room_id: str):
    """Fetch recent room messages via REST and return the hazard handoff payload.

    Resilience: if the live websocket message that triggered on_message isn't the
    hazard handoff (or we missed it during a reconnect), pull the room's recent
    history over REST and find the hazard completion handoff there.
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
        candidates = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            text = item.get("content") or item.get("message") or ""
            if isinstance(text, dict):
                text = text.get("content", "")
            payload = _extract_hazard_payload(text) if text else None
            if _is_hazard_handoff(payload):
                candidates.append(payload)
        return candidates[-1] if candidates else None
    except Exception as exc:  # noqa: BLE001 - REST fallback is best-effort
        logger.warning("[agent] room history fetch failed: %s", exc)
        return None


async def _fetch_hazard_result_from_db(event_id: str):
    """Read hazard's result from the DB (the reliable hand-off channel).

    Band's REST history is empty for per-event rooms, so the orchestrator forwards
    an empty ``data: {}`` to impact. Hazard persists its risk levels to
    ``hazard_zones``; we read them back here and shape a hazard payload the impact
    gate + args mapper understand. Returns a dict or None.
    """
    import os as _os
    import re as _re
    db_url = _os.getenv("NEON_DATABASE_URL")
    _uuid = _re.compile(r"^[0-9a-fA-F-]{32,36}$")
    if not db_url or not event_id or not _uuid.match(event_id):
        return None
    try:
        import asyncpg
        conn = await asyncpg.connect(db_url)
        try:
            rows = await conn.fetch(
                "SELECT hazard_type, risk_level, severity FROM hazard_zones WHERE event_id=$1",
                event_id,
            )
            sat = await conn.fetchrow(
                "SELECT risk_cities, bounds, bbox FROM satellite_results WHERE event_id=$1",
                event_id,
            )
        finally:
            await conn.close()
        if not rows:
            return None
        risk = {r["hazard_type"]: (r["risk_level"] or "LOW") for r in rows}
        sev = (rows[0]["severity"] or "LOW")

        def _loads(v):
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except (ValueError, TypeError):
                    return v
            return v

        cities, bounds = [], {}
        if sat:
            cities = _loads(sat.get("risk_cities")) or []
            bounds = _loads(sat.get("bounds")) or {}
            if isinstance(bounds, dict) and "bounds" in bounds:
                bounds = bounds["bounds"]
        return {
            "event_id": event_id,
            "from": "hazard",
            "hazard": {
                "flood_risk": risk.get("flood", "LOW"),
                "earthquake_risk": risk.get("earthquake", "LOW"),
                "landslide_risk": risk.get("landslide", "LOW"),
                "overall_severity": sev,
                "confidence_scores": {},
            },
            "risk_cities": cities,
            "bounds": bounds if isinstance(bounds, dict) else {},
        }
    except Exception as exc:  # noqa: BLE001 - DB read is best-effort
        logger.warning("[agent] DB hazard read failed: %s", exc)
        return None


async def _maybe_autodispatch_impact(content: str, room_id: str) -> None:
    """Deterministically run impact analysis on a genuine hazard handoff.

    Resolution order for the hazard payload (most reliable last):
      1. the live message, if it carries the handoff JSON;
      2. the room's REST history (often empty for per-event rooms);
      3. the DB ``hazard_zones`` row hazard persisted — the dependable channel.
    Invokes run_impact_analysis directly (gate + analysis, writes impact_data,
    hands off to report). Fires at most once per event. Never raises.
    """
    bound_event = _room_event_ids.get(str(room_id)) or _extract_event_id_from_text(content)

    payload = _extract_hazard_payload(content)
    if not _is_hazard_handoff(payload):
        payload = await asyncio.to_thread(_fetch_room_hazard_handoff, room_id)
    if not _is_hazard_handoff(payload) and bound_event:
        payload = await _fetch_hazard_result_from_db(bound_event)
        if _is_hazard_handoff(payload):
            logger.info("[agent] loaded hazard payload from DB for %s", bound_event)
    if not _is_hazard_handoff(payload):
        return

    event_id = _resolve_event_id(payload.get("event_id", bound_event or "unknown"), room_id=room_id)
    if event_id in _autodispatched_event_ids:
        return
    _autodispatched_event_ids.add(event_id)
    set_active_room(room_id)
    args = _impact_args_from_hazard(payload, event_id)
    logger.info(
        "[agent][autodispatch] hazard handoff for event %s (risk=%s) — driving "
        "run_impact_analysis directly", event_id, args["risk_level"],
    )
    try:
        await run_impact_analysis(config={"configurable": {"thread_id": room_id}}, **args)
    except Exception:  # noqa: BLE001 - report in-room, never crash the listener
        logger.exception("[agent][autodispatch] impact failed for %s", event_id)
        _autodispatched_event_ids.discard(event_id)


async def main() -> None:
    load_dotenv()

    try:
        from band import Agent
        from band.adapters.langgraph import LangGraphAdapter
        from langchain_openai import ChatOpenAI
        from langgraph.checkpoint.memory import InMemorySaver
    except ImportError:
        logger.error(
            "band-sdk not installed. Run: pip install band-sdk[langgraph] langchain-openai"
        )
        raise

    # Band per-turn LLM runs on the LangGraph adapter backed by Featherless
    # (OpenAI-compatible /v1/chat/completions). The intelligence layer
    # (shared/utils/llm_fallback.py) keeps its own AIML/GPT last-resort chain.
    class _BoundEventIdAdapter(LangGraphAdapter):
        """Snapshot the full event_id from each inbound dispatch before the LLM
        runs, so _resolve_event_id can use it even if the LLM truncates the id."""

        async def on_message(self, msg, *args, room_id: str, **kwargs):  # type: ignore[override]
            try:
                content = getattr(msg, "content", "") or ""
                found = _extract_event_id_from_text(content)
                if found:
                    _bind_room_event_id(room_id, found)
                    logger.info(
                        "[agent] bound event_id %s to room %s", found, room_id
                    )
                # Deterministic dispatch: run impact ourselves on a real hazard
                # handoff. AWAIT inline (not create_task) so it can't be orphaned
                # by a websocket drop. Trigger on the handoff OR any hazard-context
                # message (the REST fallback inside then finds the real handoff in
                # room history if we missed the live post during a reconnect).
                low = content.lower()
                if _extract_hazard_payload(content) is not None or "hazard" in low:
                    await _maybe_autodispatch_impact(content, room_id)
            except Exception:  # noqa: BLE001 - capture must never break handling
                pass
            return await super().on_message(msg, *args, room_id=room_id, **kwargs)

    # Band-adapter LLM: Featherless (gemma) PRIMARY + Gemini fallback. The handoff
    # JSON is now slimmed (no geometry), so a turn fits Featherless's 32k context.
    # Featherless is the workhorse (real capacity) vs Gemini's 20-req/day free
    # tier; its 4-unit concurrency 429 is absorbed by langchain backoff
    # (max_retries=8). Gemini is fallback for the rare oversized/throttled turn.
    feather = ChatOpenAI(
        model=os.getenv("BAND_ADAPTER_MODEL", "google/gemma-4-31B-it"),
        api_key=os.getenv("FEATHERLESS_API_KEY", ""),
        base_url="https://api.featherless.ai/v1",
        max_tokens=4096,
        max_retries=1,  # fail FAST to Gemini: long 429 backoff starved the ws keepalive
    )
    # Multi-key Gemini fallback chain (Featherless stays PRIMARY). Chaining
    # several free Gemini keys makes the shared 4-unit concurrency 429 disappear.
    _g_model = os.getenv("BAND_ADAPTER_FALLBACK_MODEL", "gemini-3.1-flash-lite")
    _g_base = "https://generativelanguage.googleapis.com/v1beta/openai/"
    _g_fallbacks = []
    for _kv in (
        "GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
        "GEMINI_API_KEY_4", "GEMINI_API_KEY_5",
    ):
        _k = os.getenv(_kv)
        if _k:
            _g_fallbacks.append(
                ChatOpenAI(model=_g_model, api_key=_k, base_url=_g_base, max_tokens=4096, max_retries=2)
            )
    llm = feather.with_fallbacks(_g_fallbacks) if _g_fallbacks else feather
    adapter = _BoundEventIdAdapter(
        llm=llm,
        checkpointer=InMemorySaver(),
        custom_section=SYSTEM_PROMPT,
        additional_tools=[run_impact_analysis],
    )

    # Drain every joined room's backlog so we never replay a prior event's
    # handoff and re-run analysis on a stale event_id.
    try:
        from room_drain import drain_all_rooms

        await drain_all_rooms(
            os.getenv("BAND_AGENT_ID", ""),
            os.getenv("BAND_API_KEY", ""),
            os.getenv("THENVOI_REST_URL", "https://app.band.ai/"),
        )
    except Exception:  # noqa: BLE001 - drain is best-effort
        logger.warning("[agent] startup room drain failed (non-fatal)")

    agent = Agent.create(
        adapter=adapter,
        agent_id=os.getenv("BAND_AGENT_ID", ""),
        api_key=os.getenv("BAND_API_KEY", ""),
    )

    logger.info(
        "[agent] HazardMind Impact Agent starting — handle=%s agent_id=%s",
        os.getenv("BAND_HANDLE", "unknown"),
        os.getenv("BAND_AGENT_ID", "unknown"),
    )
    # Band rate-limits rapid websocket reconnects (HTTP 429). Retry with backoff
    # so a restart waits the window out instead of crashing.
    for attempt in range(1, 9):
        try:
            await agent.run()
            break
        except Exception as exc:  # noqa: BLE001 - retry transient ws 429s
            msg = str(exc)
            if "429" in msg or "rate-limit" in msg.lower() or "supersede" in msg.lower():
                wait = min(60, 5 * (2 ** (attempt - 1)))
                logger.warning(
                    "[agent] Band websocket rate-limited (attempt %d/8); retrying in %ds",
                    attempt, wait,
                )
                await asyncio.sleep(wait)
                continue
            raise
    else:
        logger.error("[agent] could not connect after 8 attempts (Band 429).")


if __name__ == "__main__":
    asyncio.run(main())
