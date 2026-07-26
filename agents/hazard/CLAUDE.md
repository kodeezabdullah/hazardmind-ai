# Hazard Detection Agent

Analyzes hazard risks (flood, earthquake, landslide) for a disaster zone.
Migrated off Band SDK — see root `CLAUDE.md` for the LangGraph migration.

## Responsibilities
- Consume the satellite result from `PipelineState`
- Compute multi-risk classifications (flood NDWI, earthquake USGS,
  landslide DEM slope)
- Write `hazard_zones` (3 rows: flood/earthquake/landslide)
- Return the partial `PipelineState` update (see `node.py`)

## Files

- `agent.py` — pipeline entry: `analyze_hazard(satellite_payload, event_id)`,
  `_normalise_satellite_payload` (flat->nested contract adapter, real and
  necessary — the satellite emits a flat payload, the analyzer reads nested),
  `write_to_db` (the `hazard_zones` INSERT).
- `node.py` — `hazard_node(state: PipelineState) -> dict`, the LangGraph node
  wrapper. Reads `state["satellite_result"]`, calls `analyze_hazard`, returns
  `hazard_result`/`status`/`progress`/`confidence_scores`.
- `analyzer.py` — the deterministic hazard math (untouched by the migration).
- `intelligence.py` — LLM routing + `quality_check` (untouched). Also has
  `write_band_message`, a leftover natural-language handoff generator — no
  longer called (there is no chat room to post into), kept as dead code since
  it's content-generation infra, not transport.

## event_id

The full UUID `event_id` is generated once in `backend/router.py` and passed
straight through `PipelineState` — no truncation risk, no room-binding
workaround needed. (The pre-migration Band adapter used to truncate the UUID
to 8 chars; that failure mode doesn't exist once the LLM never touches
`event_id`.)
