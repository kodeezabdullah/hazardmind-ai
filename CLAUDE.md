# HazardMind AI — Claude Context

> Full detail lives in `CODEBASE.md` (per-file docs, exact algorithms, line numbers). This file is the fast-load summary + migration tracker. Read this first; open `CODEBASE.md` only when you need file-level depth.

## What This Project Is

HazardMind AI is an autonomous multi-agent disaster-intelligence platform: given a location and disaster type (flood/earthquake/landslide), it resolves the real administrative boundary, pulls live Sentinel-1/2 imagery, computes grounded hazard indices (NDWI/USGS/DEM slope), assesses population+infrastructure impact from real GeoNames/OSM data, and generates an executive PDF + risk map + GeoJSON, all shown on an interactive 3D-globe frontend. Built by Team GridForce for disaster-response use (NDMA-style dispatch). **Current status:** Band→LangChain migration complete. All 5 services now run as LangGraph nodes/graph, zero Band dependency. Pipeline running live (hazardmindai.online) on native LangGraph orchestration end to end.

## Current Architecture (AS-IS)

- **5 independent services**, each its own process/container: `backend/` (FastAPI orchestrator) + `agents/{satellite,hazard,impact,report}/` (Band SDK listeners).
- **Band SDK today**: each agent connects via `band.Agent` + a `LangGraphAdapter` (ChatOpenAI wrapping Featherless, with Gemini fallbacks). Agents talk in a shared/per-event Band "room" via `@mention`.
- **Deterministic override pattern (load-bearing, keep during migration):** the LLM tool-calling path is unreliable, so every agent overrides `on_message` (`_BoundEventIdAdapter`) to parse the dispatch text **before** the LLM runs, extract the full UUID `event_id` (LLMs truncate it to 8 chars otherwise), and call the pipeline function **directly** — bypassing the LLM tool-call. The LLM path still exists as a redundant, idempotent second caller.
- **Data flow:** `Frontend (Next.js) → POST /analyze → Backend generates event_id (ONCE) → Band @mention satellite → satellite writes DB + R2, @mentions hazard → hazard writes DB, @mentions impact → impact writes DB, @mentions report → report writes DB + R2 → Backend posts verdict → Frontend polls /status /results /band-log`.
- **DB-backed hand-off, not message-bus-only.** Each agent persists to Postgres (Neon) *before* posting to Band. Downstream agents read the DB directly whenever the Band transcript is empty/missed a message — **this, not Band, is the actual reliability backbone.** Migration should preserve and lean into this even more.
- **Why these decisions exist:**
  - Multi-provider LLM fallback (Featherless→Gemini→AIML Opus→AIML GPT) because Featherless has a shared 4-concurrency-unit cap across ALL agents and a 32k context ceiling, and the AIML account ran out of funds (Gemini is now effectively primary escalation).
  - Deterministic hazard math (earthquake/landslide) instead of LLM reasoning, because LLMs inflate risk from a region's "reputation" even with zero observed activity.
  - Impact agent's no-significant-disaster gate reports zero-affected instead of letting an LLM invent casualty numbers.
  - Report agent's strict-mode assertions (`_assert_required_report_sections`) refuse to ship a report if a required section silently fell back to template text.

## Migration In Progress: Band → LangChain

**Goal:** remove all Band SDK dependency. Replace inter-agent transport with native LangChain/LangGraph orchestration (a single `StateGraph` the backend drives directly, or direct async function calls — no external chat-room service).

**What stays the same (do not touch pipeline logic):**
- All deterministic analysis logic: boundary resolution, satellite scene selection/mosaic, NDWI/NDVI/SAR indices, hazard risk math, population/infrastructure/vulnerability tasks, report generation/PDF/map rendering.
- All DB writes/reads (`db.py`/`services/db.py`/`db_client.py` in each agent) — the schema and write contracts are the interface downstream agents depend on.
- All R2 uploads (`r2_upload.py`, `storage_client.py`).
- FastAPI endpoints — now `/analyze`, `/status`, `/results`, `/pipeline-log` (was `/band-log`, repurposed to read `disaster_events.pipeline_log` — the persisted errors/anomalies/confidence_scores trail — instead of a Band transcript).
- The frontend (`frontend/`) — it talks to the backend's REST API, not to Band directly; `lib/bandLog.ts` still targets the old `/band-log` path and needs retargeting to `/pipeline-log` (not done as part of this backend change — frontend is out of scope per the file map below).
- LLM provider routing (`llm_router.py`, `llm_clients.py`, `intelligence.py` files) — these are LLM *content* generation, unrelated to the transport-layer migration.

**What gets deleted:**
- `backend/band_client.py`, `shared/utils/band_client.py`
- Every agent's `LangGraphAdapter`/`AnthropicAdapter` usage, `_BoundEventIdAdapter`, `on_message` override
- Every agent's `agent_config.yaml`, `room_drain.py`, `hf_app.py`'s Band-runner wiring (keep the health-server part if still needed for the deploy target)
- All `@mention` parsing / natural-language handoff generation (`generate_natural_message`, `_HANDOFF_DROP_KEYS`, `send_handoff`, `parse_incoming_message`, `band_contract.py`'s message-envelope parsing)
- `backend/orchestrator.py`'s Band-specific parts: `get_best_adapter`, `_record_only`, `cross_validate_and_discuss`'s Band-posting side (keep the anomaly-detection *logic*, replace the delivery), `monitor_progress`'s room-polling (replace with LangGraph state inspection)
- `backend/cleanup.py`'s Band-backlog-drain half (keep the stuck-event DB cleanup half)
- Every `THENVOI_REST_URL`/`THENVOI_WS_URL`/`BAND_*` env var

**Do NOT delete:** the deterministic-dispatch *pattern itself* — reimplement it as "the graph node calls the pipeline function directly," which is simpler without Band's on_message indirection to work around.

## PipelineState Schema (Shared Across All Agents)

The LangGraph `StateGraph` state — this replaces the Band room + DB-fallback-read pattern as the single source of truth passed node-to-node in memory (DB writes become a side effect per node, not the primary hand-off channel):

```python
from typing import TypedDict, Optional, Literal
from typing_extensions import NotRequired

class PipelineState(TypedDict):
    # Identity — set once by backend, never regenerated
    event_id: str  # full UUID, generated once in backend/router.py
    location: str
    disaster_type: Literal["flood", "earthquake", "landslide"]
    magnitude: NotRequired[Optional[float]]

    # Per-stage results (populated as the graph advances; None until that node runs)
    satellite_result: NotRequired[Optional[dict]]   # see satellite_results DB columns
    hazard_result: NotRequired[Optional[dict]]       # see hazard_zones DB columns (3 rows: flood/eq/landslide)
    impact_result: NotRequired[Optional[dict]]       # see impact_data DB columns
    report_result: NotRequired[Optional[dict]]       # see final_reports DB columns

    # Pipeline control
    status: Literal["received", "satellite", "hazard", "impact", "report", "complete", "failed"]
    current_step: str
    progress: int  # 0-100, mirrors backend StatusResponse.progress

    # Cross-cutting
    errors: NotRequired[list[dict]]        # [{stage, error, timestamp}], accumulate, never overwrite
    anomalies: NotRequired[list[dict]]      # cross_validate_and_discuss findings, carried forward
    confidence_scores: NotRequired[dict]    # {satellite, hazard, impact, report} — each stage's self-reported confidence

    created_at: NotRequired[str]
    updated_at: NotRequired[str]
```

**Rules for this schema:**
- Each agent node reads its required upstream field (e.g. hazard reads `state["satellite_result"]`), writes its own result field, and returns only the keys it changed (standard LangGraph partial-update pattern).
- `errors`/`anomalies` are additive — a node appends, never replaces.
- The **DB row shapes stay canonical** — `satellite_result`/`hazard_result`/`impact_result`/`report_result` should mirror what the current `db.py`/`services/db.py` functions write, so DB-write code needs no reshaping. Keep `agents/hazard/agent.py`'s `_normalise_satellite_payload` flat→nested adapter logic somewhere in the satellite→hazard edge — that contract mismatch is real and orthogonal to the transport migration.

## Agent Migration Status

- [x] `agents/satellite` — converted: `node.py` wraps `run_pipeline`/`_run_pipeline_sync` as a LangGraph node; Band adapter/room/mention machinery stripped from `agent.py`, all raster/boundary/cross-validation logic untouched
- [x] `agents/hazard` — converted: `node.py` wraps `analyze_hazard` as a LangGraph node; Band adapter/room/mention machinery stripped from `agent.py`, `_normalise_satellite_payload` adapter and all deterministic hazard math untouched
- [x] `agents/impact` — converted: `node.py` wraps `run_impact_analysis` as a LangGraph node; Band adapter/room/mention/UUID-recovery machinery stripped from `agent.py`, the no-significant-disaster gate and parallel Task1+Task2 (`asyncio.gather`) untouched
- [x] `agents/report` — converted: `node.py` wraps `pipeline.run_report_pipeline` as a LangGraph node; `band_agent.py`/`band_contract.py`/`room_drain.py`/`agent_config.yaml`/`verify_setup.py` deleted, `agent.py` (the CLI entry point, not the Band listener) trimmed of its `band_contract`-dependent flags, `hf_app.py` now just serves the health check, strict-mode validation (`_assert_required_report_sections`) and all of `pipeline.py`/`generator.py`/`map_generator.py`/`pdf_generator.py`/`db_client.py` untouched
- [x] `backend/orchestrator` — replaced with `backend/graph.py`'s `StateGraph` build (`satellite → hazard → impact → report`) + `OrchestratorAgent.start_pipeline()`'s `.ainvoke()` driver. Conditional edges (`_route_after`) halt the graph the moment a node reports `status: "failed"`, replacing `handle_failure`'s early-return.
  **Known gap, not yet ported:** the old `cross_validate_and_discuss`'s specific anomaly *rules* (GDACS-extent-vs-satellite ratio, low-confidence warning, CRITICAL-risk broadcast, HIGH-risk-but-few-zones discrepancy, multi-disaster detection) were Band-room-discussion logic tied to natural-language messaging between agents — they were **not** reimplemented as a post-node hook. Each node still self-reports into `state["anomalies"]`/`state["confidence_scores"]`, but nothing currently cross-checks e.g. satellite extent against GDACS between stages. Flagged for a follow-up task, not silently dropped.
- [x] Delete Band code — `backend/band_client.py`, `backend/agent_config.yaml`, `shared/utils/band_client.py`, Band-era `backend/test_*.py` files, `entrypoint.sh`'s agent_config.yaml generation, and `band-sdk` from `backend/requirements.txt` all removed; `backend/db.py`'s dead `insert_satellite_result` (only caller was the Band orchestrator's `_persist_satellite`) also removed. Remaining `band`/`Band`/`THENVOI` hits are docs-only (`CLAUDE.md`/`CODEBASE.md` historical notes) or explanatory comments referencing the old transport by name.

## Single-Process / E2E Hardening (post-migration)

- **Per-agent `.env` loading fixed for single-process runs.** `satellite`,
  `hazard` and `impact` entry modules previously called bare `load_dotenv()`
  (cwd-relative — fine one-container-per-agent, wrong when the whole graph runs
  in one process). Now each loads `load_dotenv(Path(__file__).resolve().parent /
  ".env", override=False)` — its OWN `.env`, and `override=False` so a
  parent-process variable (e.g. the e2e harness pointing `NEON_DATABASE_URL` at
  local Postgres) still wins. `report`'s modules already loaded `BASE_DIR /
  ".env"` and were left as-is.
- **Gemini backup-key rotation (`shared/utils/llm_fallback.py`).** On a
  429/quota-exceeded from the primary `GEMINI_API_KEY`, the SAME request is
  retried ONCE against `GEMINI_API_KEY_BACKUP`; if the backup also 429s (or the
  key is unset) the chain falls through to Claude as before — never hard-fails.
  Non-429 primary failures skip the backup and fall through immediately (the
  pre-existing behaviour). Logs the key *slot* used ("primary"/"backup") and why
  it switched, never a key value. No cooldown/round-robin/shared-pool yet — full
  rotation is a later phase. Real backup keys live in each agent's gitignored
  `.env`; `.env.example` files carry a commented placeholder only.
  - **⚠️ Reach-only, not on the live pipeline path.** The 4 pipeline agents do
    NOT call `llm_fallback.py` for Gemini — each agent's own `intelligence.py` /
    `services/llm_router.py` rotates over `GEMINI_API_KEY` + `GEMINI_API_KEY_2..5`
    against Gemini's **OpenAI-compatible** endpoint
    (`generativelanguage.googleapis.com/v1beta/openai/`) with the `openai` SDK —
    it never imports `llm_fallback` or the `google-genai` SDK. So the backup-key
    retry added here currently rescues only callers of `llm_fallback.llm_call`
    (none on the pipeline path today). To make the backup key rescue the actual
    pipeline, `GEMINI_API_KEY_BACKUP` would need adding to each agent's own key
    list (satellite/hazard/report read `_..._5`; **impact reads only
    `GEMINI_API_KEY`**). Flagged, not silently rewired — that touches per-agent
    LLM-content code, out of scope for a transport/e2e task.
- **E2E harness (`tests/e2e/`).** Single-process run of the compiled graph
  (`satellite→hazard→impact→report`) for Rawalpindi/flood against live Neon +
  R2 + CDSE + Gemini + geoBoundaries. `docker-compose.yml` + `schema-test.sql`
  are the portable off-Neon path (Neon quota was extended, so the run targets
  Neon directly). `schema-test.sql` is the schema derived from live-Neon
  introspection (the real spec; `shared/db/schema.sql` stays stale). See
  `tests/e2e/README.md` for the full schema-mismatch table.

## File Map (Only What Matters)

**Backend**
- `backend/main.py` — FastAPI app, lifespan, CORS, `/health`
- `backend/router.py` — all HTTP routes, owns the orchestrator singleton
- `backend/orchestrator.py` — `OrchestratorAgent.start_pipeline()` builds the initial `PipelineState` and drives `backend/graph.py`'s compiled `StateGraph` via `.ainvoke()`
- `backend/graph.py` — `build_pipeline_graph()`; loads each agent's `node.py` via an isolated `importlib` loader (agent dirs share bare module names like `agent.py`/`intelligence.py`, so a normal import would collide across agents)
- `backend/db.py` — asyncpg pool + all DB read/write helpers (keep as-is)
- `backend/models.py` — Pydantic request/response schemas (keep as-is)
- `backend/cleanup.py` — stuck-event GC (keep the DB half, drop the Band-drain half)

**Satellite (`agents/satellite/`)**
- `agent.py` — pipeline entry (migration target: strip Band, keep `_run_pipeline_sync` body)
- `boundary.py` / `geoboundaries.py` — real admin-boundary resolution (untouched)
- `sentinel.py` — CDSE auth/scene search/mosaic/backfill (untouched)
- `processor.py` — download→stack→clip→indices→PNG→vectorize (untouched)
- `intelligence.py` / `cross_validator.py` / `confidence_tracker.py` — LLM reasoning + cross-checks (untouched)
- `r2_upload.py` — R2 upload (untouched)
- `stance_engine.py` — dormant, not wired in; leave as-is or delete, doesn't affect migration

**Hazard (`agents/hazard/`)**
- `agent.py` — pipeline entry (migration target: strip Band, keep `analyze_hazard` body + `_normalise_satellite_payload`)
- `analyzer.py` — flood(LLM)/earthquake(deterministic)/landslide(deterministic) (untouched)
- `intelligence.py` — LLM routing + quality_check (untouched)

**Impact (`agents/impact/`)**
- `agent.py` — pipeline entry (migration target: strip Band, keep `run_impact_analysis` body + no-disaster gate)
- `tasks/population.py`, `tasks/infrastructure.py`, `tasks/vulnerability.py` — untouched
- `services/llm_router.py` — live LLM router (untouched)
- `services/db.py` — DB writer (untouched)
- `services/featherless.py`, `services/criticality.py` — already-dead legacy code, ignore

**Report (`agents/report/`)**
- `band_agent.py` — pipeline entry (migration target: strip Band, keep `run_report_from_band_message`'s body logic, rename)
- `pipeline.py` — `run_report_pipeline`, the real end-to-end orchestrator (untouched)
- `generator.py` — report content + strict-mode validation (untouched)
- `intelligence.py` — 7-section intelligence layer (untouched)
- `llm_clients.py` — LLM call layer (untouched)
- `db_client.py` — DB read/write (untouched)
- `map_generator.py` — Pillow static map (untouched)
- `pdf_generator.py` — ReportLab PDF (untouched)
- `band_contract.py` — migration target: the message-envelope parsing dies; `build_report_completion_message`'s *shape* (what fields a completion carries) can inform the `report_result` schema

**Frontend (`frontend/`)** — no changes required for the migration itself.
- `lib/analyze.ts`, `lib/loadHazardResult.ts`, `lib/bandLog.ts` — polling/adapter layer; `bandLog.ts` may need retargeting if `/band-log` is renamed/reshaped
- `components/HazardMap.tsx` — live map rendering (untouched)
- Dead code (`IntelligencePanel.tsx`, `AgentTimeline.tsx`, `Map.tsx`, etc.) — out of scope for this migration, don't touch unless separately asked

**Shared (`shared/`)**
- `shared/db/schema.sql` — DRIFTED, don't trust as spec (see §Database below)
- `shared/utils/band_client.py` — dead after migration, delete
- `shared/utils/llm_fallback.py` — keep, LLM-routing only, unrelated to transport

## Database Schema (Current Live)

⚠️ `shared/db/schema.sql` is **stale** — the live Neon DB has more columns than this file declares. Treat this as a floor, not a ceiling.

**`disaster_events`** — `event_id UUID PK`, `disaster_type`, `location`, `bbox FLOAT[]`, `created_at`. **Live DB also has** (not in schema.sql, confirmed by `backend/db.py` usage): `status`, `step`, `progress`, `magnitude`, `updated_at`, `band_room_id` (self-migrated via `ADD COLUMN IF NOT EXISTS` — this one becomes dead post-migration).

**`satellite_results`** — schema.sql says `image_url, affected_area_km2, land_cover`. **⚠️ MISMATCH:** `backend/db.py`'s `insert_satellite_result` actually writes `satellite_type, cloud_cover, scene_id, true_color_url, index_url, classification_url, geojson_url, affected_area_km2, damage_percent, total_zones, bounds, bbox, risk_cities`. Only `affected_area_km2` overlaps. **Use `backend/db.py` as the real spec, not schema.sql.**

**`hazard_zones`** — one row per hazard_type per event, unique on `(event_id, hazard_type)`. Columns: `id SERIAL`, `event_id`, `geometry GEOMETRY(POLYGON,4326)` (GIST-indexed — the only real PostGIS spatial column in the DB), `risk_level`, `hazard_type`, `area_km2`, `severity`, `confirmed_by JSONB`, `flood_depth_estimate`, `earthquake_mmi`, `landslide_probability`, `overall_confidence`, `created_at`. This one matches live code.

**`impact_data`** — schema.sql says `population_affected, hospitals_at_risk, roads_blocked_km, schools_affected, vulnerability_score`. **⚠️ MISMATCH:** the impact agent's own `services/db.py` DDL uses different columns (`event_id TEXT UNIQUE`, `total_affected`, `high_risk_people`, `medium_risk_people`, `hospitals_at_risk`, `schools_at_risk`, `roads_blocked` INTEGER, `bridges_at_risk`, `vulnerability_score TEXT`, `evacuation_routes JSONB`, `estimated_evacuation_time TEXT`) and is **missing `overall_confidence`**, which `agents/report/db_client.py` expects to read. **Use `agents/impact/services/db.py` as the real spec.** Fix the `overall_confidence` gap during migration if touching this table.

**`final_reports`** — `id SERIAL`, `event_id`, `pdf_url`, `map_url`, `executive_summary`, `agent_log JSONB`, `total_time_seconds`, `confidence_level`, `created_at`. Matches live code.

## Environment Variables (Complete List)

| Variable | Service(s) | Required? |
|---|---|---|
| `NEON_DATABASE_URL` | all | **Required** |
| `CLOUDFLARE_R2_KEY`, `CLOUDFLARE_R2_SECRET`, `CLOUDFLARE_R2_BUCKET` | satellite, report | **Required** for those two |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_R2_ENDPOINT` | satellite, report | one of the two required |
| `CLOUDFLARE_R2_PUBLIC_URL` / `CLOUDFLARE_R2_PUBLIC` / `R2_PUBLIC_BASE_URL` | all (naming varies) | **Required** for artifact URLs |
| `FEATHERLESS_API_KEY`, `FEATHERLESS_BASE_URL` | all agents | **Required** (primary LLM) |
| `GEMINI_API_KEY` (+`_2`..`_5`) | all agents | Optional, but effectively required (primary escalation now) |
| `AIML_API_KEY` | all agents | Optional (last-resort escalation) |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` | backend, shared | Optional |
| `OPENAI_BASE_URL` / `AIML_BASE_URL` | backend, shared, report | Optional |
| `GPT_FALLBACK_MODEL` | all agents | Optional, default `gpt-5.5-2026-04-23` |
| `COPERNICUS_USERNAME`, `COPERNICUS_PASSWORD` | satellite | **Required** |
| `GDACS_GEOJSON_URL`, `USGS_QUERY_URL` / `GDACS_API`, `USGS_API` | satellite, hazard | Optional (built-in default) |
| `GEONAMES_USERNAME` | impact | **Required** (⚠️ `.env.example` default `ahanan.24` vs README `hazardmind` — inconsistent) |
| `ALLOWED_ORIGINS` | backend | Optional, default `*` |
| `MAX_CONCURRENT_EVENTS` | backend | Optional, default 2 |
| `PORT` | all | Optional, default 7860 (HF Spaces) |
| `NEXT_PUBLIC_API_URL` | frontend | **Required** (unset → demo mode) |
| `NEXT_PUBLIC_MAPBOX_TOKEN` | frontend | **Required** for the live map — ⚠️ missing from `.env.example` |
| **DELETE post-migration:** `BAND_AGENT_ID`, `BAND_API_KEY`, `BAND_ROOM_ID`, `THENVOI_REST_URL`, `THENVOI_WS_URL`, `DYNAMIC_BAND_ROOMS`, `SATELLITE_AGENT_ID`, `HAZARD_AGENT_ID`, `IMPACT_AGENT_ID`, `REPORT_AGENT_ID`, `ORCHESTRATOR_AGENT_ID`, `*_BAND_API_KEY`, `BAND_ADAPTER_MODEL`, `BAND_ADAPTER_FALLBACK_MODEL`, `USE_MOCK_BAND` | — | — |

## Known Issues To Fix During Migration

- **`satellite_results` / `impact_data` schema mismatches** (see Database section above) — fix while touching each agent's DB write code during migration, since you'll already be in those files.
- **`agents/hazard/agent.py`'s `_normalise_satellite_payload` flat→nested adapter** — real and necessary, port it into the satellite→hazard graph edge, don't drop it.
- **Hazard agent's `risk_polygons` is always `{}`** — unimplemented despite docs claiming PostGIS polygon generation. Not migration-blocking; flag if scope expands.
- **`GEONAMES_USERNAME` default inconsistency** between `.env.example` and README — fix when touching impact's env docs.
- Stale `CLAUDE.md` files in `agents/hazard/` and `agents/report/` describe the wrong Band adapter and (for report) the wrong map tech (claims MapLibre; it's Pillow) — **delete or rewrite these once Band is gone**, they'll be doubly wrong.

## Enterprise Gaps (Post-Migration Priority)

1. **No auth on `/analyze`** — fully open endpoint that triggers paid LLM/satellite work. Highest priority once transport is stable.
2. **No structured metrics/tracing** — only stdout logging. Add before scaling beyond single-instance.
3. **`agent_config.yaml` files with plaintext live keys** in the working tree — becomes moot once Band is deleted, but audit for any other committed secrets.
4. **No CI pipeline** (no `.github/workflows`) — most test suites are manual scripts hitting live services. Worth building a mocked CI suite once the transport layer stabilizes (mocking a LangGraph state is much easier than mocking Band).
5. **Featherless 4-concurrency-unit ceiling** — still applies post-migration since it's a provider limit, not a Band limit. `MAX_CONCURRENT_EVENTS` mitigation carries over.
6. **No data-retention policy** for R2/Neon — lower priority, address after core migration.
7. **Manual GCP VM deployment**, no IaC — lower priority.

## Commands

```bash
# Backend
cd backend && python -m venv venv && venv/Scripts/activate && pip install -r requirements.txt
venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 8000

# Each agent (repeat for satellite, hazard, impact, report)
cd agents/<name> && python -m venv venv && venv/Scripts/activate && pip install -r requirements.txt
venv/Scripts/python -u agent.py          # satellite, hazard, impact
venv/Scripts/python -u band_agent.py     # report (band_agent.py, not agent.py)

# Frontend
cd frontend && npm install && npm run dev

# Trigger a run (once backend + all 4 agents are running)
curl -X POST http://127.0.0.1:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"location":"Rawalpindi","disaster_type":"flood","magnitude":0}'
curl http://127.0.0.1:8000/status/<job_id>
curl http://127.0.0.1:8000/results/<job_id>

# Impact agent's local test server (no Band needed)
cd agents/impact && USE_MOCK_BAND=true venv/Scripts/python -m uvicorn main:app --reload --port 8001

# Report agent offline/CLI test (no Band, no LLM)
cd agents/report && python agent.py demo-peshawar-flood --contract-test --no-llm
```

## Rules For Claude

- **Always read this file first** before touching any code in this repo.
- **Never modify pipeline logic during migration** — only swap the transport layer (Band → LangGraph). Boundary resolution, hazard math, impact tasks, report generation, DB writes, R2 uploads are off-limits unless the task explicitly asks for them.
- **Never break DB write contracts** — downstream agents (and the frontend, via `/results`) depend on the exact column shapes documented above. If a column name must change, update every reader in the same change.
- **`PipelineState` is the source of truth** for inter-agent data once migrated — not a Band room, not a re-read from DB (DB writes become a side effect, not the hand-off mechanism, though DB remains the durable record).
- **If a file is marked dead code, delete it — don't refactor it.** (`stance_engine.py`, `services/featherless.py`, `services/criticality.py`, `shared/utils/band_client.py`, most of `frontend/components/`.)
- For anything not covered here, check `CODEBASE.md` before re-reading source files — it has file-by-file detail, exact algorithms, and line numbers already extracted.
