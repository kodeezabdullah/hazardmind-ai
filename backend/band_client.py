import json
import logging
import os
import threading
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("hazardmind.band_client")

BAND_REST_URL = os.getenv("THENVOI_REST_URL", "https://app.band.ai/").rstrip("/")
BAND_API_KEY = os.getenv("BAND_API_KEY")
BAND_ROOM_ID = os.getenv("BAND_ROOM_ID")
BAND_AGENT_ID = os.getenv("BAND_AGENT_ID")  # the orchestrator itself

# --- Featherless (natural-language message generation) -----------------------
FEATHERLESS_API_KEY = os.getenv("FEATHERLESS_API_KEY")
FEATHERLESS_BASE_URL = os.getenv(
    "FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1"
).rstrip("/")

# Fallback chain — each model is tried in order until one returns a message.
FEATHERLESS_MODELS = [
    "google/gemma-4-31B-it",
    "moonshotai/Kimi-K2.6",
    "Qwen/Qwen3.6-35B-A3B",
]

SATELLITE_AGENT_ID = os.getenv("SATELLITE_AGENT_ID")
HAZARD_AGENT_ID = os.getenv("HAZARD_AGENT_ID")
IMPACT_AGENT_ID = os.getenv("IMPACT_AGENT_ID")
REPORT_AGENT_ID = os.getenv("REPORT_AGENT_ID")

SATELLITE_HANDLE = "@abdullah.gis.services/hazardmind-satellite"
HAZARD_HANDLE = "@hazardmind-hazard"
IMPACT_HANDLE = "@hazardmind-impact"
REPORT_HANDLE = "@hazardmind-report"

# handle -> configured agent id (may be None until set in .env).
AGENT_IDS: dict[str, Optional[str]] = {
    SATELLITE_HANDLE: SATELLITE_AGENT_ID,
    HAZARD_HANDLE: HAZARD_AGENT_ID,
    IMPACT_HANDLE: IMPACT_AGENT_ID,
    REPORT_HANDLE: REPORT_AGENT_ID,
}

# Every pipeline agent, in order. Band has no working "@all", so a broadcast
# must explicitly @mention each agent by name.
ALL_HANDLES = [SATELLITE_HANDLE, HAZARD_HANDLE, IMPACT_HANDLE, REPORT_HANDLE]

# Agent personalities — fed to Featherless so each agent sounds like a distinct
# expert colleague in the Band room rather than a generic assistant.
AGENT_PERSONALITIES = {
    "hazardmind-orchestrator": (
        "Calm, authoritative coordinator. Clear and decisive. Keeps team focused."
    ),
    "hazardmind-satellite": (
        "Technical and precise. Speaks in data and coordinates. "
        "Flags sensor anomalies immediately."
    ),
    "hazardmind-hazard": (
        "Urgent and safety-focused. Never downplays risk. "
        "Always cross-references multiple sources."
    ),
    "hazardmind-impact": (
        "Data-driven and methodical. Focuses on human numbers. "
        "Precise about infrastructure status."
    ),
    "hazardmind-report": (
        "Professional and concise. Government-ready language. "
        "Summarizes without losing critical detail."
    ),
}

# Map a handle (with or without owner prefix) to its bare agent name.
HANDLE_TO_AGENT = {
    SATELLITE_HANDLE: "hazardmind-satellite",
    HAZARD_HANDLE: "hazardmind-hazard",
    IMPACT_HANDLE: "hazardmind-impact",
    REPORT_HANDLE: "hazardmind-report",
}

# Band's message API requires at least one mention and forbids self-mention,
# so orchestrator-origin messages (thoughts / task updates / events) anchor a
# mention on a real participant. Satellite is the always-present pipeline agent.
ANCHOR_MENTION_ID = SATELLITE_AGENT_ID
ANCHOR_HANDLE = SATELLITE_HANDLE

# Structured Band event types we emit for the judges' pipeline view.
EVENT_TASK = "task"
EVENT_THOUGHT = "thought"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_ERROR = "error"


class InboundStore:
    """In-memory buffer of inbound Band messages.

    Band's REST GET /messages does not return room history for this agent —
    inbound messages are delivered to the connected SDK agent over its
    WebSocket execution loop. The orchestrator's adapter records each message
    here so monitor_progress() can observe agent completions without REST
    polling. The buffer is the source of truth for GET /band-log too.

    Messages are normalized via parse_incoming_message() before storage, so a
    stored entry matches the parse_incoming_message() return shape.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: list[dict] = []

    def add(self, raw_message: dict) -> dict:
        parsed = parse_incoming_message(raw_message)
        with self._lock:
            self._messages.append(parsed)
        return parsed

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._messages)

    def for_event(self, event_id: str) -> list[dict]:
        """Return stored messages whose content references this event_id."""
        with self._lock:
            snapshot = list(self._messages)
        return [
            m
            for m in snapshot
            if m.get("event_id") == event_id
            or event_id in str(m.get("content", ""))
            or event_id in json.dumps(m.get("data") or {})
        ]


# Process-wide buffer of inbound messages (populated by the orchestrator adapter).
inbound_store = InboundStore()


def _require_config() -> tuple[str, str]:
    """Return (room_id, api_key) or raise if Band is not configured."""
    if not BAND_ROOM_ID:
        raise RuntimeError("BAND_ROOM_ID is not configured")
    if not BAND_API_KEY:
        raise RuntimeError("BAND_API_KEY is not configured")
    return BAND_ROOM_ID, BAND_API_KEY


def _resolve_mentions(mentions: Optional[list[str]]) -> list[str]:
    """Ensure a non-empty mention list (Band requires minItems: 1).

    Falls back to the anchor agent (satellite) when no explicit mention is
    given, so orchestrator thoughts/events still post.
    """
    ids = [m for m in (mentions or []) if m]
    if ids:
        return ids
    if ANCHOR_MENTION_ID:
        return [ANCHOR_MENTION_ID]
    raise RuntimeError(
        "Band messages require at least one mention and no anchor agent "
        "(SATELLITE_AGENT_ID) is configured"
    )


async def _post_message(content: str, mention_ids: list[str], room_id: Optional[str]) -> dict:
    """POST a message to a Band room.

    Band's schema is strict: {"message": {"content": str, "mentions": [{"id"}]}}.
    A `type` field is rejected and mentions must be non-empty.
    """
    default_room, api_key = _require_config()
    room = room_id or default_room
    url = f"{BAND_REST_URL}/api/v1/agent/chats/{room}/messages"
    payload = {
        "message": {
            "content": content,
            "mentions": [{"id": agent_id} for agent_id in mention_ids],
        }
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            headers={"X-API-Key": api_key},
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()

    # Record our own outbound message so GET /band-log shows the full
    # conversation (Band's REST history does not echo it back).
    inbound_store.add(
        {
            "content": content,
            "type": "text",
            "sender": {"name": "hazardmind-orchestrator"},
        }
    )
    return result


async def send_band_message(
    content: str,
    mention_ids: list[str],
    room_id: Optional[str] = None,
) -> dict:
    """Low-level: post a message with @mentions to a Band room.

    Prefer send_text_message() / send_event() in new code. The mentioned
    handles should also appear literally in `content`.
    """
    return await _post_message(content, _resolve_mentions(mention_ids), room_id)


async def send_text_message(
    content: str,
    mentions: Optional[list[str]] = None,
    room_id: Optional[str] = None,
) -> dict:
    """Send a human-readable text message for agent-to-agent discussion.

    `mentions` is a list of agent UUIDs to @mention. When empty, the message
    is anchored on the satellite agent so it still posts (Band requires a
    mention); the @handle in `content` carries the intended addressing.
    """
    return await _post_message(content, _resolve_mentions(mentions), room_id)


# Band's event endpoint accepts only these message_type values; "data" (used by
# handoffs) is not one of them, so it maps onto "task" (handoffs carry task data).
EVENT_MESSAGE_TYPES = {EVENT_TASK, EVENT_THOUGHT, EVENT_TOOL_CALL, EVENT_TOOL_RESULT, EVENT_ERROR}


async def _post_event(
    message_type: str,
    content: str,
    metadata: dict,
    room_id: Optional[str],
) -> dict:
    """POST a structured event to a Band room's /events endpoint.

    Events are a distinct channel from text messages: they carry structured
    `metadata`, require NO mentions, and render as collapsed pipeline events
    rather than visible chat lines. See
    POST /api/v1/agent/chats/{chat_id}/events.
    """
    default_room, api_key = _require_config()
    room = room_id or default_room
    url = f"{BAND_REST_URL}/api/v1/agent/chats/{room}/events"
    payload = {
        "event": {
            "content": content,
            "message_type": message_type,
            "metadata": metadata,
        }
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            headers={"X-API-Key": api_key},
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()

    # Record the event into the inbound store so GET /band-log and
    # monitor_progress() see the full pipeline (Band does not echo it back).
    inbound_store.add(
        {
            "content": content,
            "type": message_type,
            "data": metadata,
            "sender": {"name": "hazardmind-orchestrator"},
        }
    )
    return result


async def send_event(
    event_type: str,
    title: str,
    data: dict,
    mentions: Optional[list[str]] = None,
    room_id: Optional[str] = None,
) -> dict:
    """Send a structured Band event carrying pipeline data.

    Posts to Band's dedicated /events channel, so the JSON payload lives in the
    event `metadata` (parsed by the pipeline) rather than being dumped as a
    visible chat message. Events require no mention, so they never pollute the
    transcript with a misdirected @handle.

    event_type is one of: task / thought / tool_call / tool_result / error.
    Any other value (e.g. the handoff's "data") is sent as a `task` event.
    The `mentions` argument is accepted for call-site symmetry with
    send_text_message() but is unused — events are not directed at anyone.
    """
    message_type = event_type if event_type in EVENT_MESSAGE_TYPES else EVENT_TASK
    metadata = {"event": event_type, "title": title, "data": data}
    return await _post_event(message_type, title, metadata, room_id)


async def send_thought(content: str, room_id: Optional[str] = None) -> dict:
    """Emit an agent-reasoning event visible to judges.

    Example: "Cloud cover 42% detected, switching to Sentinel-1 SAR".
    """
    return await send_event(
        EVENT_THOUGHT,
        title="Orchestrator thought",
        data={"content": content},
        room_id=room_id,
    )


async def send_task_update(
    task_name: str,
    status: str,
    result: Optional[Any] = None,
    room_id: Optional[str] = None,
) -> dict:
    """Emit a task-progress event.

    status is one of: started / processing / complete / failed.
    """
    data: dict[str, Any] = {"task": task_name, "status": status}
    if result is not None:
        data["result"] = result
    return await send_event(
        EVENT_TASK,
        title=f"{task_name}: {status}",
        data=data,
        room_id=room_id,
    )


def parse_incoming_message(raw_message: dict) -> dict:
    """Normalize any Band message (text or event) into a structured dict.

    Returns:
        {
            "type": str,             # text | task | thought | tool_call | ...
            "content": str,          # human-readable content
            "data": dict | None,     # structured payload for event messages
            "event_id": str | None,  # pipeline event_id if present in content
            "agent": str | None,     # sender handle/name/id
            "timestamp": str | None,
            "raw": dict,             # the original message
        }
    """
    if not isinstance(raw_message, dict):
        return {
            "type": "text",
            "content": str(raw_message),
            "data": None,
            "event_id": None,
            "agent": None,
            "timestamp": None,
            "raw": raw_message,
        }

    raw_content = raw_message.get("content", "")
    content = raw_content if isinstance(raw_content, str) else str(raw_content)
    msg_type = raw_message.get("type", "text") or "text"
    data: Optional[dict] = None

    # Events carry their structured payload in `metadata`/`data` (the dedicated
    # /events channel), not embedded in the text. Honor that first.
    structured = raw_message.get("metadata")
    if not isinstance(structured, dict):
        structured = raw_message.get("data")
    if isinstance(structured, dict):
        if structured.get("event"):
            msg_type = structured["event"]
        inner_data = structured.get("data")
        data = inner_data if isinstance(inner_data, dict) else structured

    # Legacy / text path: an event may instead embed a JSON body in `content`
    # (optionally after a "@handle [type]..." header line). Pull it out too.
    if data is None:
        parsed = _try_json(_json_tail(content))
        if isinstance(parsed, dict):
            if parsed.get("event"):
                msg_type = parsed["event"]
            if isinstance(parsed.get("data"), dict):
                data = parsed["data"]
                inner = data.get("content")
                if isinstance(inner, str) and inner:
                    content = inner

    return {
        "type": msg_type,
        "content": content,
        "data": data,
        "event_id": raw_message.get("event_id")
        or _extract_event_id(str(raw_content))
        or (_extract_event_id(json.dumps(data)) if data else None),
        "agent": _sender_name(raw_message),
        "timestamp": raw_message.get("created_at")
        or raw_message.get("inserted_at")
        or raw_message.get("timestamp"),
        "raw": raw_message,
    }


def _json_tail(text: str) -> str:
    """Return the substring starting at the first '{' (events prepend a header)."""
    idx = text.find("{")
    return text[idx:] if idx != -1 else text


def _try_json(text: str) -> Any:
    text = text.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _extract_event_id(text: str) -> Optional[str]:
    """Pull an event_id (a UUID) out of message content if present."""
    for line in text.splitlines():
        if "event_id" in line:
            _, _, tail = line.partition("event_id")
            token = tail.strip(" \t:\"',").split()
            if token:
                return token[0].strip("\"',")
    return None


def _sender_name(msg: dict) -> Optional[str]:
    sender = msg.get("sender") or msg.get("author") or msg.get("agent")
    if isinstance(sender, dict):
        return sender.get("handle") or sender.get("name") or sender.get("id")
    return sender


def _unwrap_messages(data: Any) -> list[dict]:
    """Pull the message list out of Band's response envelope.

    Band returns {"data": [...], "metadata": {...}} for the messages list, but
    has also used {"messages": [...]} and bare lists, so handle all three.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "messages"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


async def get_room_messages(room_id: str, event_id: str) -> list[dict]:
    """Fetch a Band room's messages and return only those mentioning event_id.

    GET /api/v1/agent/chats/{room_id}/messages

    Every pipeline message carries the event_id in its content, so filtering by
    substring keeps the transcript scoped to a single job.
    """
    room = room_id or BAND_ROOM_ID
    if not room:
        raise RuntimeError("BAND_ROOM_ID is not configured")
    if not BAND_API_KEY:
        raise RuntimeError("BAND_API_KEY is not configured")

    url = f"{BAND_REST_URL}/api/v1/agent/chats/{room}/messages"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers={"X-API-Key": BAND_API_KEY})
        resp.raise_for_status()
        data = resp.json()

    return [
        msg
        for msg in _unwrap_messages(data)
        if event_id in str(msg.get("content", ""))
    ]


# Track which Band message IDs have already been seeded into inbound_store
# from the REST poll path so we never add duplicates.
_seeded_rest_ids: set[str] = set()
_seeded_rest_lock = threading.Lock()


async def poll_room_into_store(event_id: str, room_id: Optional[str] = None) -> None:
    """Fetch the Band room REST history and seed inbound_store with new messages.

    The orchestrator's WebSocket adapter (RecordingAnthropicAdapter) only fires
    on_event() when Band routes a message *to the orchestrator* agent. Messages
    posted by the satellite / hazard / impact / report agents that @mention a
    *different* agent never trigger that hook, so monitor_progress() misses them
    and times out waiting for a completion signal that was already posted.

    This function is the complement: it reads the full room transcript via the
    REST API (not gated on recipient) and adds any message that (a) references
    event_id and (b) has not already been seeded. monitor_progress() calls this
    on every poll cycle so completion signals always land in the store.
    """
    try:
        messages = await get_room_messages(room_id or BAND_ROOM_ID or "", event_id)
    except Exception:  # noqa: BLE001 – REST poll is best-effort
        logger.debug("REST room poll failed for event_id=%s; skipping", event_id)
        return

    for msg in messages:
        msg_id = str(msg.get("id") or msg.get("message_id") or "")
        with _seeded_rest_lock:
            if msg_id and msg_id in _seeded_rest_ids:
                continue
            if msg_id:
                _seeded_rest_ids.add(msg_id)

        inbound_store.add(msg)
        logger.debug(
            "REST-seeded inbound_store: msg id=%s for event_id=%s", msg_id, event_id
        )

def mentions_for(handle: str) -> list[str]:
    """Return the mention id list for a handle (empty -> anchor fallback)."""
    agent_id = AGENT_IDS.get(handle)
    return [agent_id] if agent_id else []


def mentions_for_all(exclude: Optional[list[str]] = None) -> list[str]:
    """Return mention ids for every configured pipeline agent, by name.

    Band has no working "@all" — a broadcast has to @mention each agent
    individually. Pass `exclude` (a list of handles) to drop agents the sender
    cannot mention (e.g. itself). Any handle whose id is unset is skipped.
    """
    skip = set(exclude or [])
    ids: list[str] = []
    for handle in ALL_HANDLES:
        if handle in skip:
            continue
        agent_id = AGENT_IDS.get(handle)
        if agent_id and agent_id not in ids:
            ids.append(agent_id)
    return ids


def all_handles_text(exclude: Optional[list[str]] = None) -> str:
    """Space-joined @handles for every pipeline agent (for message bodies).

    Mirrors mentions_for_all(): use this in the visible content so the names the
    reader sees match the mention ids attached to the message.
    """
    skip = set(exclude or [])
    return " ".join(h for h in ALL_HANDLES if h not in skip)


# --- Natural-language message generation (Featherless) -----------------------


def _build_featherless_prompt(
    sender_role: str,
    receiver_agent: str,
    receiver_handle: str,
    context: Any,
    findings: Any,
    urgency: str,
    anomalies: list,
    questions: list,
    personality: str,
) -> str:
    """Build the Featherless prompt for one agent-to-agent message."""
    return f"""You are {sender_role} AI agent in a real disaster response pipeline.

Write ONE natural message to {receiver_agent}.

Disaster context: {context}
Your findings: {findings}
Anomalies detected: {anomalies}
Questions to ask: {questions}
Urgency level: {urgency}

Your personality: {personality}

Rules:
- Start with {receiver_handle}
- Sound like an expert colleague in an emergency
- Mention specific numbers from findings
- If anomalies -> flag them clearly
- If questions -> ask directly
- Max 3-4 sentences
- NO JSON, NO brackets, NO code
- Natural professional English
- If CRITICAL urgency -> show urgency"""


def _fallback_natural_message(
    receiver_handle: str,
    findings: Any,
    urgency: str,
    anomalies: list,
    questions: list,
) -> str:
    """Templated natural-English message used when Featherless is unavailable.

    Still reads like a colleague (no JSON/brackets), so the Band transcript stays
    human-readable even with no FEATHERLESS_API_KEY or when every model errors.
    """
    parts: list[str] = [receiver_handle]
    if urgency == "critical":
        parts.append("URGENT —")
    if findings:
        parts.append(f"{findings}.")
    for anomaly in anomalies or []:
        parts.append(f"Heads up: {anomaly}.")
    for question in questions or []:
        parts.append(f"{question}")
    if urgency == "critical" and not anomalies:
        parts.append("Please prioritize this.")
    return " ".join(str(p).strip() for p in parts if str(p).strip())


async def _featherless_complete(prompt: str) -> Optional[str]:
    """Call Featherless chat completions, walking the model fallback chain.

    Returns the generated text, or None if no key is set or every model fails.
    """
    if not FEATHERLESS_API_KEY:
        return None

    url = f"{FEATHERLESS_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {FEATHERLESS_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        for model in FEATHERLESS_MODELS:
            try:
                resp = await client.post(
                    url,
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7,
                        "max_tokens": 220,
                    },
                )
                resp.raise_for_status()
                body = resp.json()
                text = body["choices"][0]["message"]["content"].strip()
                if text:
                    return text
            except Exception:  # noqa: BLE001 - try the next model in the chain
                logger.warning("Featherless model %s failed, trying next", model)
                continue
    return None


async def generate_natural_message(
    sender_agent: str,
    sender_role: str,
    receiver_agent: str,
    receiver_handle: str,
    context: Any,
    findings: Any,
    urgency: str = "normal",
    anomalies: Optional[list] = None,
    questions: Optional[list] = None,
    personality: str = "",
) -> str:
    """Generate ONE natural agent-to-agent message via Featherless.

    Sounds like an expert colleague in an emergency — mentions concrete numbers,
    flags anomalies, asks questions. Falls back to a templated natural-English
    message (never JSON) when Featherless is unavailable so the pipeline never
    blocks on the LLM. The returned string always starts with {receiver_handle}.
    """
    anomalies = anomalies or []
    questions = questions or []
    if not personality:
        personality = AGENT_PERSONALITIES.get(sender_agent, "")

    prompt = _build_featherless_prompt(
        sender_role=sender_role,
        receiver_agent=receiver_agent,
        receiver_handle=receiver_handle,
        context=context,
        findings=findings,
        urgency=urgency,
        anomalies=anomalies,
        questions=questions,
        personality=personality,
    )

    text = await _featherless_complete(prompt)
    if not text:
        return _fallback_natural_message(
            receiver_handle, findings, urgency, anomalies, questions
        )

    # Guarantee the handle leads the message even if the model dropped it.
    if receiver_handle not in text:
        text = f"{receiver_handle} {text}"
    return text


async def send_handoff(
    natural_msg: str,
    data: dict,
    mentions: Optional[list[str]] = None,
    title: str = "agent_result",
) -> None:
    """Send a handoff as ONE Band message: natural text + JSON appended at end.

    The natural prose (what judges read) leads the message and @mentions the
    target agent; the structured JSON is appended at the end so receiving agents
    can still parse the payload off the tail of the same message. One clean chat
    line carries both — no separate /events post.
    """
    content = f"{natural_msg}\n\n{json.dumps(data, indent=2)}"
    await send_text_message(content, mentions=mentions)


def handoff_message(handle: str, event_id: str, **fields: Any) -> tuple[str, list[str]]:
    """Build a handoff text message + mention list for an agent handle.

    Returns (content, mention_ids). When the agent's id is configured we
    @mention it; otherwise we anchor on satellite (so the message still posts)
    and rely on the @handle in the content for addressing.
    """
    lines = [handle, "Disaster event received. Please begin analysis.", f"event_id: {event_id}"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    agent_id = AGENT_IDS.get(handle)
    mentions = [agent_id] if agent_id else []
    return "\n".join(lines), mentions


async def notify_satellite(
    event_id: str,
    location: str,
    disaster_type: str,
    magnitude: Optional[float],
) -> dict:
    """Send the initial pipeline message to the satellite agent."""
    content, mentions = handoff_message(
        SATELLITE_HANDLE,
        event_id,
        location=location,
        disaster_type=disaster_type,
        magnitude=magnitude,
    )
    return await send_text_message(content, mentions=mentions)
