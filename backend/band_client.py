import json
import os
import threading
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

BAND_REST_URL = os.getenv("THENVOI_REST_URL", "https://app.band.ai/").rstrip("/")
BAND_API_KEY = os.getenv("BAND_API_KEY")
BAND_ROOM_ID = os.getenv("BAND_ROOM_ID")
BAND_AGENT_ID = os.getenv("BAND_AGENT_ID")  # the orchestrator itself

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


async def send_event(
    event_type: str,
    title: str,
    data: dict,
    mentions: Optional[list[str]] = None,
    room_id: Optional[str] = None,
) -> dict:
    """Send a structured Band event carrying pipeline data.

    event_type is one of: task / thought / tool_call / tool_result / error.
    Band's message schema forbids a top-level `type` field, so the structure
    is encoded as a JSON document in `content` (parse_incoming_message reads
    it back). A leading @handle keeps the event readable in the transcript.
    """
    body = {"event": event_type, "title": title, "data": data}
    content = f"{ANCHOR_HANDLE} [{event_type}] {title}\n{json.dumps(body)}"
    return await _post_message(content, _resolve_mentions(mentions), room_id)


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

    # Event messages embed a JSON body (optionally after a "@handle [type]..."
    # header line). Pull out the event type + structured data when present.
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
        "event_id": _extract_event_id(str(raw_content)),
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
