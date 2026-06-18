# Executive Report Agent

Generates final executive output: map snapshot, PDF report, summary.

## Responsibilities
- Render map using MapLibre + risk polygons
- Generate PDF report with summary
- Upload artifacts to Cloudflare R2
- Publish final report via Band SDK

## event_id truncation hardening (`band_agent.py`)

The Band LangGraph adapter's LLM sometimes truncates the UUID `event_id` to its
leading 8-char segment. `final_reports.event_id` is **UUID-typed**, so a short id
would break the write and the join back to the other tables. Fix (mirrors
satellite/hazard/impact):
- `_BoundEventIdAdapter.on_message` snapshots the full `event_id: <uuid>` from the
  inbound dispatch **before the LLM runs** and binds it to the room
  (`_bind_room_event_id`, keyed by the LangGraph `thread_id`).
- `_resolve_event_id(event_id, room_id)` (called in
  `run_report_from_band_message`) prefers that room-bound full UUID over the
  LLM-parsed value. The pre-existing `_best_effort_event_id` text-extraction
  fallback is retained for the failure path.

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
  under the `report_agent` key. Runtime values are loaded from `.env`.

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
- [ ] Implement report generation pipeline
- [ ] Wire agent into Band rooms and publish the final report
