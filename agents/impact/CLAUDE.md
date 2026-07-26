# Impact Assessment Agent (Agent 3) — HazardMind

Assesses population, infrastructure, and vulnerability impact for a disaster
event. Migrated off Band SDK — see root `CLAUDE.md` for the LangGraph migration.

## Responsibilities
- Consume the hazard result from `PipelineState`
- Apply the no-significant-disaster gate (honest zero-impact when hazard risk
  is LOW/NONE/UNKNOWN — never fabricate a population)
- Run population (GeoNames) + infrastructure (Overpass OSM) in parallel,
  then vulnerability/evacuation reasoning sequentially
- Write `impact_data`
- Return the partial `PipelineState` update (see `node.py`)

## Files

- `agent.py` — pipeline entry: `run_impact_analysis(...)`, the
  `_no_significant_disaster` gate + `_emit_no_impact` (honest zero-impact,
  real and critical — never fabricate casualty/population numbers when the
  hazard risk is LOW/NONE/UNKNOWN).
- `node.py` — `impact_node(state: PipelineState) -> dict`, the LangGraph node
  wrapper. Reads `state["hazard_result"]`, maps the hazard payload's nested
  `hazard` block onto `run_impact_analysis`'s args, calls it, returns
  `impact_result`/`status`/`progress`/`confidence_scores`/`anomalies`.
- `tasks/population.py`, `tasks/infrastructure.py`, `tasks/vulnerability.py` —
  untouched by the migration.
- `services/db.py` — `write_impact_data` (the `impact_data` INSERT, untouched).
- `services/llm_router.py` — live LLM router (untouched).
- `services/band_client.py`, `services/featherless.py`, `services/criticality.py`
  — dead/legacy. `band_client.py` is still imported by `main.py` (the standalone
  local FastAPI test server, out of scope for this migration) — not deleted yet,
  but `agent.py`/`node.py` no longer import it.
- `main.py` — Band-era local test server (`USE_MOCK_BAND`, `/assess-impact`).
  Not touched by this migration; still Band-shaped.

## Known gap (not fixed by this migration)

`impact_data`'s own DDL (`services/db.py`) has no `overall_confidence` column,
but `agents/report/db_client.py` expects to read one. Flagged in root
`CLAUDE.md` under Database Schema — confirmed still present post-migration,
intentionally left untouched (out of scope here).

## event_id

The full UUID `event_id` is generated once in `backend/router.py` and passed
straight through `PipelineState` — no truncation risk, no room-binding
workaround needed (the pre-migration Band adapter's UUID-recovery machinery
— `_BoundEventIdAdapter`, `_resolve_event_id`, `_room_event_ids` — is gone).
