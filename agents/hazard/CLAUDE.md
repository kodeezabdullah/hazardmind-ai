# Hazard Detection Agent

Analyzes hazard risks (flood, earthquake, landslide) for a disaster zone.

## Responsibilities
- Consume satellite output
- Compute multi-risk classifications
- Generate risk polygons in PostGIS
- Publish results via Band SDK

## event_id truncation hardening (`agent.py`)

The Band LangGraph adapter's LLM sometimes truncates the UUID `event_id` to its
leading 8-char segment when parsing the inbound payload (e.g. `e9e83455` instead
of `e9e83455-8ea6-44b7-...`). `hazard_zones.event_id` is **UUID-typed**, so a
short id makes the INSERT fail with `invalid UUID ... length must be 32..36` and
crashes the analysis. Fix (defense-in-depth, mirrors the satellite agent):
- `_BoundEventIdAdapter` overrides `on_message` to extract the full
  `event_id: <uuid>` from the inbound dispatch text **before the LLM runs** and
  bind it to the room (`_bind_room_event_id`, keyed by the LangGraph
  `thread_id`). The LLM cannot corrupt this snapshot.
- `_resolve_event_id(event_id, room_id)` (called at the top of `analyze_hazard`)
  prefers the room-bound full UUID over the LLM-parsed value; falls back to the
  passed id if already a full UUID, else logs and returns it unchanged.
The real root-cause fix is upstream (satellite now sends the full UUID in its
handoff JSON); this is the local safety net so hazard never crashes on a bad id.

## Band Integration

Connects to the Band platform using the Anthropic adapter.

- **Package:** `band-sdk[anthropic]` (installed as `band-sdk`, imported as `band`).
  The public docs reference `thenvoi`; this project uses `band` v1.0.0.
- **Import:** `from band import Agent`,
  `from band.adapters.anthropic import AnthropicAdapter`
- **Connect:** `Agent.create(adapter=..., agent_id=..., api_key=..., ws_url=..., rest_url=...)`
  then `await agent.start()` (opens WebSocket, fetches metadata), and
  `await agent.stop()` to disconnect. Use `await agent.run()` for a long-lived agent.
- **Credentials:** `agent_config.yaml` (gitignored) holds `agent_id`/`api_key`
  under the `hazard_agent` key. Runtime values are loaded from `.env`.

### Required env vars (see `.env.example`)
- `THENVOI_REST_URL` — Band REST URL (`https://app.band.ai/`)
- `THENVOI_WS_URL` — Band WebSocket URL
- `BAND_AGENT_ID` — agent UUID from the Band platform
- `BAND_API_KEY` — agent API key from the Band platform
- `ANTHROPIC_API_KEY` — provider key for the Anthropic adapter

### Setup progress
- [x] Add Band SDK (`band-sdk[anthropic]`) to `requirements.txt`
- [x] Add Band env vars to `.env.example`
- [x] Create `agent_config.yaml` template (gitignored)
- [ ] Register agent on Band platform and fill in real credentials
- [ ] Add a connectivity check and confirm connection to live Band
- [ ] Implement hazard classification pipeline
- [ ] Wire agent into Band rooms and publish results
