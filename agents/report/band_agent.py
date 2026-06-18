import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from band_contract import (  # noqa: E402
    build_report_completion_message,
    extract_trailing_json,
    parse_report_trigger_message,
)
from pipeline import run_report_pipeline  # noqa: E402


SYSTEM_PROMPT = """You are HazardMind's report agent (Agent 4 of 4), the final stage.

You have exactly ONE job: when you are @mentioned with the impact result, call the
`run_report_pipeline` tool with the full message, WAIT for it to finish, and let it
post the completion signal. The tool is the single source of truth.

STRICT RULES — follow exactly:
1. DO NOT post anything before the tool returns. No "starting", no acknowledgements,
   no status notes, no questions. Stay silent while it runs.
2. When @mentioned with an impact/handoff result, call `run_report_pipeline` ONCE,
   passing the FULL Band message (natural text + trailing JSON) as band_message.
   Never generate a new event_id — the tool reads it from the message.
3. The tool generates the report, uploads the PDF/map, writes the DB, and posts the
   natural completion + JSON to the orchestrator itself. You do NOT compose your own
   summary or numbers and you do NOT invent data. After the tool returns, you are
   done — say nothing further.
4. Call the tool ONCE per event_id. Acknowledgements, nudges, and summaries about an
   event you already handled are informational — do not respond. Do not expose secrets.
"""


class RunReportFromBandMessage(BaseModel):
    """Run the HazardMind Report Agent pipeline from a full Band message."""

    band_message: str = Field(
        ...,
        description="The full Band @mention message, including natural text and the trailing JSON payload.",
    )


@dataclass
class BandRuntimeConfig:
    agent_id: str
    api_key: str
    anthropic_api_key: str
    anthropic_base_url: str
    model: str
    rest_url: str
    ws_url: str


async def run_report_from_band_message(band_message: str) -> str:
    """
    Parse a Band orchestrator message, run the Report Agent pipeline,
    and return the exact natural text + JSON completion message.
    """
    return await _run_report_from_band_message(
        band_message,
        fetch_from_db=True,
        upload_r2=True,
        write_db=True,
        use_llm=True,
    )


async def _run_report_from_band_message(
    band_message: str,
    *,
    fetch_from_db: bool,
    upload_r2: bool,
    write_db: bool,
    use_llm: bool,
) -> str:
    event_id = ""
    try:
        parsed_payload = parse_report_trigger_message(band_message)
        # Prefer the full event_id captured from the inbound dispatch (room-bound)
        # over the LLM-parsed value, which may be a truncated prefix.
        event_id = _resolve_event_id(
            parsed_payload["event_id"], room_id=_current_room()
        )
        parsed_payload["event_id"] = event_id
        result = await run_report_pipeline(
            event_id=event_id,
            fetch_from_db=fetch_from_db,
            upload_r2=upload_r2,
            write_db=write_db,
            incoming_payload=parsed_payload,
            use_llm=use_llm,
        )
    except Exception as exc:  # noqa: BLE001 - return a Band-shaped failure signal.
        if not event_id:
            event_id = _best_effort_event_id(band_message)
        result = {
            "event_id": event_id,
            "status": "failed",
            "error": f"Report Agent failed before completion: {_safe_error_message(exc)}",
        }
    return build_report_completion_message(result)


async def _run_report_tool(params: RunReportFromBandMessage) -> str:
    return await run_report_from_band_message(params.band_message)


# event_ids we have already auto-dispatched the report for.
_autodispatched_event_ids: set = set()


def _is_impact_handoff(content: str):
    """Return the parsed impact handoff payload if `content` is one, else None.

    The impact agent posts a `---`-separated JSON tail with from=hazardmind-impact,
    to=hazardmind-report, and a `data` object. We detect that so report can run
    deterministically rather than relying on the Featherless adapter LLM to emit
    the tool-call (which it skips, leaving final_reports unwritten).
    """
    if not content or "{" not in content:
        return None
    try:
        payload = extract_trailing_json(content)
    except Exception:  # noqa: BLE001 - not parseable -> not a handoff
        return None
    if not isinstance(payload, dict):
        return None
    frm = str(payload.get("from") or payload.get("agent") or "").lower()
    to = str(payload.get("to") or "").lower()
    if "impact" in frm and (not to or "report" in to):
        return payload
    return None


def _fetch_room_impact_handoff(room_id: Any):
    """Fetch recent room messages via REST and return the impact handoff text.

    Resilience: if the live websocket message isn't the impact handoff (or we
    missed it during a reconnect), pull the room's recent history over REST and
    find the impact completion handoff there.
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
            if text and _is_impact_handoff(text):
                candidates.append(text)
        return candidates[-1] if candidates else None
    except Exception as exc:  # noqa: BLE001 - REST fallback is best-effort
        print(f"[report] room history fetch failed: {exc}", flush=True)
        return None


async def _maybe_autodispatch_report(content: str, room_id: Any) -> None:
    """Deterministically run the report pipeline on a genuine impact handoff.

    Detects the impact completion handoff and invokes run_report_from_band_message
    directly (which generates the PDF/map, uploads to R2, and writes final_reports).
    Fires at most once per event. Best-effort: never raises into message handling.

    If the live message isn't the handoff, falls back to scanning the room's REST
    history (covers a missed live post during a websocket reconnect).
    """
    handoff = content if _is_impact_handoff(content) else None
    if handoff is None:
        handoff = await asyncio.to_thread(_fetch_room_impact_handoff, room_id)

    payload = _is_impact_handoff(handoff) if handoff else None
    # Resolve the event_id from the handoff OR the room binding, so the DB path
    # works even when the room carried no usable impact handoff.
    event_id = _resolve_event_id(
        str((payload or {}).get("event_id") or "") or _room_event_ids.get(str(room_id), ""),
        room_id=room_id,
    )

    # DB fallback: Band's REST history is empty for per-event rooms, so the
    # orchestrator forwards an empty impact handoff. If impact has written its row
    # to the DB, build the report straight from the DB (fetch_from_db=True) — the
    # reliable channel — instead of depending on the room transcript.
    use_db = False
    if not payload and event_id:
        use_db = await _impact_data_exists_in_db(event_id)

    if not payload and not use_db:
        return
    if not event_id or event_id in _autodispatched_event_ids:
        return
    _autodispatched_event_ids.add(event_id)
    _set_active_room(room_id)
    print(
        f"[report][autodispatch] event {event_id} — driving report pipeline "
        f"directly ({'from DB' if use_db else 'from handoff'})",
        flush=True,
    )
    try:
        if use_db:
            result = await run_report_pipeline(
                event_id=event_id,
                fetch_from_db=True,
                upload_r2=True,
                write_db=True,
                use_llm=True,
            )
            from band_contract import build_report_completion_message
            completion = build_report_completion_message(result)
        else:
            completion = await run_report_from_band_message(handoff)
        await _post_completion_to_room(completion)
    except Exception as exc:  # noqa: BLE001 - report in-room, never crash the listener
        print(f"[report][autodispatch] report failed for {event_id}: {exc}", flush=True)
        _autodispatched_event_ids.discard(event_id)


async def _impact_data_exists_in_db(event_id: str) -> bool:
    """True if impact has persisted its row for this event (the DB hand-off)."""
    import os as _os
    import re as _re
    db_url = _os.getenv("NEON_DATABASE_URL")
    if not db_url or not event_id or not _re.match(r"^[0-9a-fA-F-]{32,36}$", event_id):
        return False
    try:
        import asyncpg
        conn = await asyncpg.connect(db_url)
        try:
            n = await conn.fetchval(
                "SELECT count(*) FROM impact_data WHERE event_id=$1", event_id
            )
        finally:
            await conn.close()
        return bool(n)
    except Exception:  # noqa: BLE001 - best-effort
        return False


def _build_report_tool() -> Any:
    """Build the report pipeline tool for the LangGraph adapter.

    The Anthropic adapter accepts a ``(PydanticModel, callable)`` tuple, but the
    LangGraph adapter feeds ``additional_tools`` straight into LangChain's
    ``create_agent``/``create_tool``, which requires a real LangChain tool (a
    callable with a ``__name__``), not a tuple. Wrap the callable as a
    ``StructuredTool`` whose args schema is the existing Pydantic model.
    """
    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import StructuredTool

    async def _coroutine(band_message: str, config: RunnableConfig = None) -> str:
        # Capture the dispatch room (LangGraph thread_id) so we can post the
        # completion signal directly into the per-event room.
        try:
            thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
            _set_active_room(thread_id)
        except Exception:  # noqa: BLE001 - room capture must never break the tool
            pass
        completion_message = await run_report_from_band_message(band_message)
        # Post the completion directly so the orchestrator detects it regardless
        # of how the LLM paraphrases; the LLM also returns this string.
        await _post_completion_to_room(completion_message)
        return completion_message

    return StructuredTool.from_function(
        coroutine=_coroutine,
        name="run_report_pipeline",
        description=RunReportFromBandMessage.__doc__ or "Run the HazardMind Report Agent pipeline.",
        args_schema=RunReportFromBandMessage,
    )


REPORT_TOOL = _build_report_tool()


# The room the current event was dispatched in (LangGraph thread_id). The report
# agent posts its completion signal directly into this room so detection does not
# depend on the LLM relaying the tool output verbatim. Falls back to BAND_ROOM_ID.
_active_room: str | None = None


def _set_active_room(room_id: Any) -> None:
    global _active_room
    if room_id:
        _active_room = str(room_id)


def _current_room() -> str | None:
    return _active_room or os.getenv("BAND_ROOM_ID")


# Defense against the LLM truncating the UUID event_id (the UUID-typed
# final_reports.event_id column rejects a short id). The Band adapter delivers
# each inbound dispatch to on_message BEFORE the LLM runs; we snapshot the full
# `event_id: <uuid>` and bind it to the room (== LangGraph thread_id). The
# pipeline prefers that authoritative id over the LLM-parsed one.
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


def _strip_mentions(content: Any) -> str:
    return _MENTION_RE.sub(" ", content or "")


def _extract_event_id_from_text(content: Any):
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


def _bind_room_event_id(room_id: Any, event_id: Any) -> None:
    if room_id and event_id:
        _room_event_ids[str(room_id)] = str(event_id)


def _resolve_event_id(event_id: Any, room_id: Any = None) -> str:
    """Full-UUID event_id, preferring the room-bound one over the LLM-parsed."""
    passed = str(event_id or "").strip()
    bound = _room_event_ids.get(str(room_id)) if room_id else None
    if bound and _UUID_RE.fullmatch(bound):
        return bound
    if passed and _UUID_RE.fullmatch(passed):
        return passed
    return passed


async def _post_completion_to_room(completion_message: str) -> None:
    """Post the report completion message into the dispatch room.

    Mentions the orchestrator (Band requires a mention; an agent cannot mention
    itself). Best-effort — never raises, so a posting failure cannot mask the
    report result the LLM will also relay.
    """
    room_id = _current_room()
    orchestrator_id = os.getenv("ORCHESTRATOR_AGENT_ID", "")
    api_key = os.getenv("BAND_API_KEY", "")
    if not room_id or not orchestrator_id or not api_key:
        return
    try:
        import httpx

        rest_url = os.getenv("THENVOI_REST_URL", "https://app.band.ai").rstrip("/")
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{rest_url}/api/v1/agent/chats/{room_id}/messages",
                headers={"X-API-Key": api_key},
                json={
                    "message": {
                        "content": completion_message,
                        "mentions": [{"id": orchestrator_id}],
                    }
                },
            )
    except Exception:  # noqa: BLE001 - direct post is best-effort
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run HazardMind Report Agent on Band.")
    parser.add_argument(
        "--dry-run-message-file",
        help="Read a Band message from a local file and print the generated Band response without connecting.",
    )
    parser.add_argument(
        "--contract-test",
        action="store_true",
        help="Dry-run without live LLM, R2 upload, DB fetch, or DB write.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    load_dotenv(BASE_DIR / ".env")

    if args.dry_run_message_file:
        message = Path(args.dry_run_message_file).read_text(encoding="utf-8")
        if args.contract_test:
            response = await _run_report_from_band_message(
                message,
                fetch_from_db=False,
                upload_r2=False,
                write_db=False,
                use_llm=False,
            )
        else:
            response = await run_report_from_band_message(message)
        print(response)
        return

    if args.contract_test:
        raise SystemExit("--contract-test is only supported with --dry-run-message-file.")

    await run_live_agent()


def _build_adapter_llm():
    """Band-adapter LLM: Gemini PRIMARY, Featherless (gemma) fallback.

    The adapter replays the whole room transcript into one LLM turn. Featherless's
    gemma caps at 32k context on this plan (HTTP 400 context_length_exceeded) AND
    its 4-unit concurrency limit makes parallel pipeline agents collide (HTTP 429);
    the AIML escalation path is out of funds. Gemini (1M-token context, separate
    quota) is therefore the PRIMARY adapter model. Featherless stays as fallback
    for when GEMINI_API_KEY is unset/unavailable.
    """
    from langchain_openai import ChatOpenAI

    feather = ChatOpenAI(
        model=os.getenv("BAND_ADAPTER_MODEL", "google/gemma-4-31B-it"),
        api_key=os.getenv("FEATHERLESS_API_KEY", ""),
        base_url="https://api.featherless.ai/v1",
        max_tokens=4096,
        max_retries=1,  # fail FAST to Gemini: long 429 backoff starved the ws keepalive
    )
    # Multi-key Gemini fallback chain (Featherless stays PRIMARY). Chaining
    # several free Gemini keys makes the shared 4-unit concurrency 429 disappear.
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


async def run_live_agent() -> None:
    print("HazardMind Report Band agent starting...")
    config = load_runtime_config()
    print_runtime_status(config)
    missing = required_missing(config)
    if missing:
        raise SystemExit(f"Missing required Band agent configuration: {', '.join(missing)}")

    try:
        from band import Agent
        from band.adapters.langgraph import LangGraphAdapter
        from langchain_openai import ChatOpenAI
        from langgraph.checkpoint.memory import InMemorySaver
    except ImportError as exc:
        raise SystemExit(f"Band SDK import failed: {_safe_error_message(exc)}") from exc

    class _BoundEventIdAdapter(LangGraphAdapter):
        """Snapshot the full event_id from each inbound dispatch before the LLM
        runs, so _resolve_event_id can use it even if the LLM truncates the id."""

        async def on_message(self, msg, *args, room_id: str, **kwargs):  # type: ignore[override]
            try:
                content = getattr(msg, "content", "") or ""
                found = _extract_event_id_from_text(content)
                if found:
                    _bind_room_event_id(room_id, found)
                    print(f"bound event_id {found} to room {room_id}", flush=True)
                # Deterministic dispatch: generate the report ourselves on a real
                # impact handoff. AWAIT inline (not create_task) so it can't be
                # orphaned by a websocket drop. Trigger on the handoff OR any
                # impact-context message (the REST fallback inside then finds the
                # real handoff in room history if we missed the live post).
                low = content.lower()
                if _is_impact_handoff(content) or "impact" in low:
                    await _maybe_autodispatch_report(content, room_id)
            except Exception:  # noqa: BLE001 - capture must never break handling
                pass
            return await super().on_message(msg, *args, room_id=room_id, **kwargs)

    llm = _build_adapter_llm()
    adapter = _BoundEventIdAdapter(
        llm=llm,
        checkpointer=InMemorySaver(),
        custom_section=SYSTEM_PROMPT,
        additional_tools=[REPORT_TOOL],
    )

    # Drain every joined room's backlog so we never replay a prior event's
    # handoff and re-run report generation on a stale event_id.
    try:
        from room_drain import drain_all_rooms

        await drain_all_rooms(config.agent_id, config.api_key, config.rest_url)
    except Exception:  # noqa: BLE001 - drain is best-effort
        print("startup room drain failed (non-fatal)")

    agent = Agent.create(
        adapter=adapter,
        agent_id=config.agent_id,
        api_key=config.api_key,
        ws_url=config.ws_url,
        rest_url=config.rest_url,
    )

    # Band rate-limits rapid websocket reconnects (HTTP 429). Retry with backoff
    # so a restart waits the window out instead of crashing.
    try:
        for attempt in range(1, 9):
            try:
                await agent.run()
                break
            except Exception as exc:  # noqa: BLE001 - retry transient ws 429s
                msg = str(exc)
                if "429" in msg or "rate-limit" in msg.lower() or "supersede" in msg.lower():
                    wait = min(60, 5 * (2 ** (attempt - 1)))
                    print(
                        f"[report] Band websocket rate-limited (attempt {attempt}/8); "
                        f"retrying in {wait}s", flush=True,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise
        else:
            print("[report] could not connect after 8 attempts (Band 429).", flush=True)
    except KeyboardInterrupt:
        print("HazardMind Report Band agent stopped.")
    except Exception as exc:  # noqa: BLE001 - keep live startup failures secret-safe.
        raise SystemExit(f"Band agent failed: {_safe_error_message(exc)}") from exc


def load_runtime_config() -> BandRuntimeConfig:
    agent_config = load_agent_config(BASE_DIR / "agent_config.yaml")
    agent_id = os.getenv("BAND_AGENT_ID") or _agent_config_value(agent_config, "agent", "uuid")
    api_key_env = _agent_config_value(agent_config, "band", "api_key_env") or "BAND_API_KEY"
    return BandRuntimeConfig(
        agent_id=agent_id or "",
        api_key=os.getenv(api_key_env, ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_base_url=os.getenv("ANTHROPIC_BASE_URL", ""),
        model=os.getenv("REPORT_BAND_MODEL") or os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-4-5-20250929",
        rest_url=os.getenv("THENVOI_REST_URL", "https://app.band.ai"),
        ws_url=os.getenv("THENVOI_WS_URL", "wss://app.band.ai/api/v1/socket/websocket"),
    )


def load_agent_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def print_runtime_status(config: BandRuntimeConfig) -> None:
    statuses = {
        "BAND_AGENT_ID": bool(config.agent_id),
        "BAND_API_KEY": bool(config.api_key),
        "FEATHERLESS_API_KEY": bool(os.getenv("FEATHERLESS_API_KEY")),
        "THENVOI_REST_URL": bool(config.rest_url),
        "THENVOI_WS_URL": bool(config.ws_url),
    }
    for name, present in statuses.items():
        print(f"{name}: {'present' if present else 'missing'}")


def required_missing(config: BandRuntimeConfig) -> list[str]:
    missing = []
    if not config.agent_id:
        missing.append("BAND_AGENT_ID")
    if not config.api_key:
        missing.append("BAND_API_KEY")
    if not os.getenv("FEATHERLESS_API_KEY"):
        missing.append("FEATHERLESS_API_KEY")
    return missing


def _agent_config_value(config: dict[str, Any], section: str, key: str) -> str:
    section_data = config.get(section, {})
    if not isinstance(section_data, dict):
        return ""
    value = section_data.get(key)
    return str(value).strip() if value is not None else ""


def _best_effort_event_id(message: str) -> str:
    try:
        payload = extract_trailing_json(message)
    except ValueError:
        return ""
    value = payload.get("event_id") if isinstance(payload, dict) else ""
    return str(value or "").strip()


def _safe_error_message(exc: Exception) -> str:
    message = str(exc) or type(exc).__name__
    for name in (
        "AIML_API_KEY",
        "ANTHROPIC_API_KEY",
        "BAND_API_KEY",
        "FEATHERLESS_API_KEY",
        "NEON_DATABASE_URL",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_R2_KEY",
        "CLOUDFLARE_R2_SECRET",
    ):
        value = os.getenv(name)
        if value:
            message = message.replace(value, "[redacted]")
    return message[:500]


if __name__ == "__main__":
    asyncio.run(main())
