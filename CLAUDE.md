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

- **CDSE access-token expiry mid-run — production defect, not test-only
  (`agents/satellite/sentinel.py` / `processor.py` / `agent.py`).** A live
  Rawalpindi/flood e2e run (2026-07-26, ~51 min wall time) authenticated to
  Copernicus **exactly once** and every download issued after ~10 minutes
  in came back `401 Unauthorized` — CDSE Keycloak access tokens live ~10 min,
  and `authenticate_copernicus()` was called once at pipeline start with the
  resulting bare string threaded unchanged through every downstream call
  (`select_satellite` → `process_satellite_imagery` → `download_imagery` →
  `_download_bands_via_nodes`/`_download_product_zip` →
  `_stream_to_file_with_retry`). Any run longer than ~10 minutes — which a
  multi-tile, 100%-coverage mosaic search routinely is — hits this in
  production, not just in the e2e harness.
  Fixed with `sentinel.TokenManager`: captures `access_token` +
  `refresh_token` + `expires_in` from the Keycloak response, and `.get()`
  proactively refreshes (via the `refresh_token` grant, falling back to a
  full password re-auth if the refresh itself fails) whenever the cached
  token is within 90s of expiring — never reactively on a 401. Thread-safe
  (`threading.Lock`) so concurrent in-flight band downloads collapse onto one
  refresh instead of racing. `agent.py`'s `_authenticate_with_recovery` now
  returns the `TokenManager` itself (not a token string); `processor.py`'s
  `_resolve_token()` accepts either a manager (`.get()`) or a legacy plain
  string (existing tests / the module's own `__main__` smoke test), so
  `download_imagery`/`_download_bands_via_nodes` pull a **fresh** token
  per-file/per-band rather than reusing one snapshot across a run that can
  span many minutes. Logs every refresh and every fallback-to-password-grant.
  Unit-verified offline (mocked Keycloak): cache hit, proactive refresh on
  simulated expiry, fallback to password grant when refresh itself fails,
  and 10 concurrent `.get()` calls collapsing to exactly one HTTP request.
  **Not yet verified against live CDSE across a real multi-minute run** — the
  one available live e2e attempt failed on this exact bug before the fix
  existed; a follow-up live run is needed to confirm the fix holds under
  real Keycloak round-trip latency, not just mocks.
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
- **Graph loader now survives LAZY sibling imports (`backend/graph.py`).** The
  first loader fix (purging each agent's bare `sys.modules` entries after loading
  `node.py`) stopped cross-agent collision but broke intra-agent **call-time**
  imports: `agents/satellite/processor.py` does `from sentinel import ...` INSIDE
  a function, and by call time the purge had removed `sentinel` + popped the
  agent dir from `sys.path` → `ModuleNotFoundError` on the first real satellite
  run (in production too, not just the test). `_load_node` now **stashes** each
  agent's bare modules per-agent (out of shared `sys.modules`, so the next
  agent's load is still clean) and returns a **wrapper** that re-installs that
  agent's bare modules + dir on `sys.path` around every node call, then restores.
  Eager and lazy sibling imports both resolve to the right agent; isolation holds.
  Verified by a full 4-node e2e (9/9).
- **E2E harness (`tests/e2e/`).** Single-process run of the compiled graph
  (`satellite→hazard→impact→report`) for Rawalpindi/flood against live Neon +
  R2 + CDSE + Gemini + geoBoundaries. `docker-compose.yml` + `schema-test.sql`
  are the portable off-Neon path (Neon quota was extended, so the run targets
  Neon directly). `schema-test.sql` is the schema derived from live-Neon
  introspection (the real spec; `shared/db/schema.sql` stays stale). See
  `tests/e2e/README.md` for the full schema-mismatch table.
- **First fully-green live e2e (2026-07-26, event `88ad6095-51c5-4c66-95c0-7baf59dd1cab`,
  Rawalpindi/flood, S1/SAR path, cleaned up post-verification): 9/9 assertions
  passed, total 3244.1s.** This is the run that verified BUG 1 (GCP→UTM warp)
  and the CDSE token-refresh fix (`TokenManager`) both hold under a real
  multi-tier, multi-scene S1 search — not just unit/mock coverage. Three
  findings from this run to carry forward:
  - **The 142s baseline is retired for S1 — it predates a working S1 path
    entirely** (BUG 1 made every prior S1 run collapse to ~0% coverage almost
    instantly, which is fast but not a real success). **3244s is the new S1
    baseline.** A Sentinel-2/optical run (clear sky, <30% cloud) will be much
    faster — S2 has the per-band Nodes download shortcut (below) and a single
    100 km tile usually covers a city-scale AOI in tier 1, so don't use the S1
    number to judge S2 runtime or vice versa.
  - **Sentinel-1 has no per-band Nodes download path — every candidate scene is
    a full 1.2–1.7 GB `.SAFE` archive fetch.** `_download_bands_via_nodes`
    (`agents/satellite/processor.py`) explicitly returns `None` for any
    `satellite_type != "sentinel-2"` (its Nodes-tree mapping only knows the S2
    L1C IMG_DATA layout), so every S1 candidate falls through to
    `_download_product_zip` (the whole-archive path). The 2026-07-26 run
    downloaded 4 full archives (5,490.7 MB total) across its 4 coverage tiers.
    **Optimisation candidate**, not yet built: an S1-GRD-aware Nodes mapping
    (the `measurement/*.tiff` VV/VH paths) would let S1 reuse the same
    per-band/per-file cache and outage-grace machinery S2 already has, instead
    of re-downloading a full archive per candidate scene.
  - **Peak RSS 9,611.3 MB (~9.6 GB) at the clip stage with only 2 tiles
    mosaicked** (`agent.py`'s per-stage `[MEM]` log). RSS climbed roughly
    linearly with tile count across the run's tiers (single-tile clip stages
    peaked ~5.1–5.6 GB; 2-tile mosaic-and-clip stages peaked 7.3–9.6 GB) — a
    full-resolution S1 GRD scene is large (28,000×21,000+ px) and every
    `_open_georeferenced`/`WarpedVRT` + `rasterio.merge` pass holds full arrays
    in memory before the windowed clip trims them down. **Use this as the K8s
    satellite-pod memory sizing input**: provision for peak RSS scaling with
    the number of tiles a coverage search may need to mosaic (worst case,
    tier 4's cap on candidates × ~2.5 GB/tile), not just a flat per-pod number.
    A 3+ tile Mindanao-style scattered-city mosaic (see `agents/satellite/CLAUDE.md`
    Step 8/10) would need proportionally more headroom.

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

**`impact_data`** — schema.sql says `population_affected, hospitals_at_risk, roads_blocked_km, schools_affected, vulnerability_score`. **⚠️ MISMATCH:** the impact agent's own `services/db.py` DDL uses different columns (`event_id TEXT UNIQUE`, `total_affected`, `high_risk_people`, `medium_risk_people`, `hospitals_at_risk`, `schools_at_risk`, `roads_blocked` INTEGER, `roads_blocked_km` DOUBLE PRECISION (added 2026-07-28, see below), `bridges_at_risk`, `vulnerability_score TEXT`, `evacuation_routes JSONB`, `estimated_evacuation_time TEXT`, `overall_confidence DOUBLE PRECISION`). **Use `agents/impact/services/db.py` as the real spec.**

**GATE A RESOLVED (2026-07-28, live Neon `information_schema.columns` query):**
`impact_data.overall_confidence` **DOES exist on live Neon** — it was added
out-of-band at some point, with no migration file in this repo recording it.
The report stage's SELECT (`agents/report/db_client.py:_fetch_impact_data`)
was never actually hard-failing every report generation as
`SYSTEM_ANALYSIS.md` Section B.6/E.2 flagged as the worst-case possibility —
**that worst case did not hold**, so the "Pipeline running live... end to
end" framing above is accurate on this specific point. What WAS true: nothing
in `agents/impact/services/db.py` ever wrote that column, so every row's
`overall_confidence` silently persisted as `NULL` — a real, silent loss of
impact's confidence contribution to the report's final `min()`-aggregation
(distinct from, and less severe than, a hard failure). Fixed: `write_impact_data`
now writes it, and `_fetch_impact_data` additionally degrades gracefully
(catches the specific `UndefinedColumnError` case and returns
`overall_confidence: None` with a logged warning) instead of hard-failing, so
this class of schema drift can never again take down the whole report stage
on ANY Neon instance, present or future. See `agents/impact/ANALYSIS.md` and
`agents/report/ANALYSIS.md` for the original per-agent analysis this
resolves.

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
- Stale `CLAUDE.md` files in `agents/hazard/` and `agents/report/` describe the wrong Band adapter and (for report) the wrong map tech (claims MapLibre; it's Pillow) — **delete or rewrite these once Band is gone**, they'll be doubly wrong. (`agents/satellite/CLAUDE.md` is equally stale — a 2026-07-26 dated section was added at its top flagging this; the rest of that file is history, not current instructions.)
- **Satellite Sentinel-2 NDWI/NDVI thresholds need science-phase revalidation.** The 2026-07-26 coverage-correctness pass switched S2 from L1C (top-of-atmosphere) to L2A (surface reflectance) so real SCL cloud masking is available for the 100%-coverage requirement. `NDWI_WATER_THRESHOLD`/`NDVI_DAMAGE_THRESHOLD`/the 0.5 "severe" cutoff were tuned against L1C and were **not** retuned (out of scope for a correctness-only pass) — index values shift under L2A. See `agents/satellite/CLAUDE.md`'s 2026-07-26 section and `agents/satellite/processor.py`'s `_S2_BANDS` comment.
- **`affected_area_km2 = 0` on the S1/SAR flood path does NOT mean "no flood" — it means "flood cannot be determined from this index."** The live e2e (2026-07-26, event `88ad6095…`) produced `mean_index = 23.6485` on the SAR path and it is tempting to read that as "consistent with baseline dry-ground backscatter" — **that reading is not defensible.** Per BUG 5 (`agents/satellite/CLAUDE.md`), the SAR index is `10*log10(raw GRD DN)`: no radiometric calibration LUT, no speckle filter, no terrain correction. A positive dB-scale number merely confirms the value is uncalibrated raw-DN-space (real calibrated sigma0 backscatter is virtually always negative dB) — it supports **no** physical interpretation of ground conditions at all, wet or dry. Any consumer (human, report LLM, cross-validator) reading a S1 `affected_area_km2 = 0` or a "low"/"dry" verdict off the raw `mean_index` must be corrected: on this path the pipeline currently cannot produce a trustworthy flood answer in either direction. This is a real gap, not a benign result — flag it in the report/PDF as "flood status indeterminate (uncalibrated SAR)", not as "no flood detected".
- **Confidence silently drops at the satellite→hazard boundary, then compounds at hazard→report (found 2026-07-26; the flood-path masking bug FIXED 2026-07-28, see "Cross-Agent Honesty Fix Pass" below).** Satellite's `ConfidenceTracker.overall_confidence()` (a real, weighted assessment — SAR uncalibrated + cloud + temporal-dispersion concerns) is correctly written to `structured["confidence"]` and `PipelineState["confidence_scores"]["satellite"]`, and `agents/hazard/agent.py`'s `_normalise_satellite_payload` (line ~89) does carry it into the normalized payload's `confidence` key. `agents/hazard/analyzer.py`'s `run_parallel_analysis` DOES read it for flood (caps flood's confidence at satellite's, pre-existing before this fix pass) but earthquake/landslide are correctly uncapped (they don't consume satellite output at all). The masking this entry originally described — satellite 0.0 confidence not reaching the final report's `min()` — is now fixed via two channels: the pre-existing `hazard_scores["flood"]` read, AND (new, 2026-07-28) `agents/report/node.py` now wires `PipelineState["confidence_scores"]` through to `run_report_pipeline` as `incoming_payload`, making the `confidence.satellite_confidence`/`report.satellite.confidence` read keys `fa0d9bd` originally added actually live on the production call path (previously dead). See `agents/report/ANALYSIS.md` Section on confidence and `SYSTEM_ANALYSIS.md` B.7-B.9/H#6 for the full trace. **Still not fixed / separate, smaller issue:** `PipelineState["confidence_scores"]["hazard"]` itself (a flat 3-way average of flood/earthquake/landslide, computed in `hazard/node.py`) and impact's re-average of the same are still diluted intermediate figures — but this doesn't affect the final report's confidence_level today because report reads the per-hazard-type/dedicated-channel figures directly, not these diluted intermediates.
- **Uncalibrated-SAR-as-NDWI unit confusion is systemic, not a single call site (found 2026-07-26). The hazard-agent instance FIXED 2026-07-28** (see "Cross-Agent Honesty Fix Pass" below); the satellite-agent instance and the missing-calibration-caveat-in-prompts issue remain open. Every prompt/deterministic-check surveyed that consumes `mean_index`/`mean_value`/`water_percent`:
  - `agents/satellite/agent.py`'s `validation_input` dict (line ~728, feeds `cross_validator.validate_all`) — **hardcodes `"mean_ndwi": result.get("mean_index")` unconditionally**, even when `result["index_type"] == "SAR"`, and includes no `index_type`/`index_calibrated`/`satellite_type` key at all. `cross_validator.py`'s index-physics check (line ~374) then unconditionally applies NDWI thresholds (`ndwi > 0.3`/`> 0.1`) to whatever it's handed whenever `disaster_type == "flood"` — on the 2026-07-26 run this fed the raw SAR value `23.6485` through `elif ndwi > 0.1: add_evidence(0.75, weight=0.3)`, adding false confidence-boosting "evidence" from a value that was never an NDWI ratio. This is the exact defect class as `analyze_flood`'s already-documented SAR-into-NDWI-threshold bug (`root_cause.md` §4.3), but in the **deterministic** cross-validator, not an LLM, and one call site earlier (the mislabeling happens at the `validation_input` construction, before the value even reaches `cross_validator.py`). **Still open** — out of scope for the 2026-07-28 fix pass (that pass fixed the downstream hazard-agent instance of this defect class, not this satellite-agent instance, per the task's explicit boundary).
  - `agents/satellite/intelligence.py`'s `interpret_results` prompt (line ~497) — **states `index_type`** ("Index type: {index_type} (NDWI/NDVI/SAR)") but never states `index_calibrated` or any calibration caveat. **Still open.**
  - `agents/report/intelligence.py`'s `_compact_context` (line ~445, feeds every report-stage LLM prompt) — **FIXED 2026-07-28**: now also carries `index_calibrated` alongside `index_type` (H#11).
  - `agents/hazard/analyzer.py`'s `analyze_flood` prompt (line ~181) — the one call site that's actually correct: branches `index_label`/`index_context` on `satellite_type` so the LLM sees "SAR backscatter ratio (VV-VH)... Negative values mean flooding" vs "NDWI flood index... Above 0.3 indicate flooding," not a mislabeled ratio. Its **deterministic fallback** (used only when the LLM call fails) — **FIXED 2026-07-28 (H#4)**: now reads `index_calibrated` (carried through `_normalise_satellite_payload`, also fixed 2026-07-28 — Gate B established it was NOT previously carried) and, for uncalibrated SAR, bases the flood decision on `affected_area_km2` alone instead of applying NDWI-scale thresholds to the raw index. **Direction correction**: this was a false-CRITICAL defect (SAR dB is positive/uncalibrated in this codebase), not the false-negative this entry originally implied by omission — see `agents/hazard/ANALYSIS.md`/`agents/satellite/ANALYSIS.md` for the full correction.
  - `agents/report/generator.py`'s `deterministic_detailed_report` template (line ~685) — states `index_type` in the rendered text but never calibration status; only reached on the report LLM-fallback path. **Still open** (narrow, LLM-failure-only path; out of scope for the contained 2026-07-28 pass per its own scoping).
  - `agents/report/db_client.py`'s `db_context_to_report_context` (the **live DB-fetch path**, not covered by the above survey originally) — **FIXED 2026-07-28 (H#5)**: previously hardcoded `index_type: "database_result"`/`mean_value: 0` for every DB-fetched report; now derives `index_type`/`index_calibrated` from `satellite_type` (the only column actually available — `satellite_results` has no `index_type` column, confirmed via live schema query).
  Net (updated 2026-07-28): the hazard-fallback and report-DB-fetch instances of this defect class are fixed and now carry `index_calibrated`. The satellite-agent `validation_input`/cross-validator instance and the two narrower prompt-only gaps (satellite's `interpret_results`, report's LLM-fallback template) remain open, out of scope for this pass.
- **S1 coverage tiers 1–3 exactly-0.000%, not a bug — confirmed via live CDSE attribute lookup (2026-07-26).** On the 88ad6095 run, tiers 1–3 (same-relative-orbit required) kept retrying two adjacent DESCENDING/orbit-107 frames 25s apart (same physical strip — stacking them can't add new extent, so 0% is real, not a masking artifact). Tier 4 (the only tier that relaxes same-orbit) found the ASCENDING/orbit-100 pair a full day earlier that actually reached the AOI. **This is `COVERAGE_TIERS`'s same-orbit-first, then any-orbit design working as documented in `sentinel.py`** (`BUG 3`), not a filter bug — no valid tier-1-eligible pair was ever excluded. Worth a design discussion (should orbit ever be relaxed before the date window widens that far?) but not a defect to fix.

## Cross-Agent Honesty Fix Pass (2026-07-28, branch `fix/cross-agent-honesty`)

Follow-up to `SYSTEM_ANALYSIS.md`'s cross-agent audit (2026-07-27). Fixed the
top-ranked defects from that document's Section H (places the pipeline
generated dishonest or false-premise outputs). Science-model gaps (SAR
calibration H#7, population exposure model H#8) are explicitly out of scope —
separate future sessions. One commit per fix, independently revertable.

**Gate A — `impact_data.overall_confidence` (H#2):** confirmed present on
live Neon via a direct `information_schema.columns` query — added
out-of-band, no migration file recorded it. The worst-case scenario
`SYSTEM_ANALYSIS.md` flagged (every report generation hard-failing) did NOT
hold; this repo's "pipeline running live" framing stands. What was real: the
column existed but nothing wrote it, so every row's `overall_confidence` was
silently `NULL`. Fixed: `agents/impact/services/db.py` writes it now,
`agents/report/db_client.py:_fetch_impact_data` also degrades gracefully
(rather than hard-failing) if this or another expected column is ever absent
on a different Neon instance in the future.

**Gate B — does `_normalise_satellite_payload` carry `index_calibrated`/
`index_units`?** Resolved by direct read: it did NOT (prior to this fix
pass). `agents/satellite/ANALYSIS.md`'s claim (did not carry) was correct;
`agents/hazard/ANALYSIS.md`'s claim (did carry) was wrong — both docs
corrected. Fixed as part of H#4: the adapter now carries
`index_calibrated`/`index_units`/`confidence_basis`/`evidence_count`.

**H-numbered gaps closed:**

| H# | Issue | Fix |
|---|---|---|
| H#1 | Impact's population/infrastructure prompts hardcode "flood" regardless of `disaster_type`; hazard hardcodes earthquake/landslide risk to LOW handing off to impact | `agents/hazard/agent.py` now passes real `disaster_type`/per-hazard risks; `agents/impact/node.py`/`agent.py` thread them through; `population.py`/`infrastructure.py` branch prompt text on real `disaster_type`; added `_assert_disaster_type_consistent` guard |
| H#2 | `impact_data.overall_confidence` schema question | Gate A above |
| H#3 | Invalid-bbox fallback hardcodes `overall_severity: "HIGH"` | Now `UNKNOWN` + `status: "insufficient_data"`; `quality_check` rejects all-UNKNOWN-risks-with-non-UNKNOWN-severity as internally inconsistent |
| H#4 | Hazard's deterministic flood fallback applies NDWI thresholds to SAR-dB values | Branches on `index_calibrated`; uncalibrated SAR now decided from `affected_area_km2` alone, confidence capped 0.4, anomaly recorded. Direction-corrected: false-CRITICAL, not false-negative |
| H#5 | Report's DB-fetch context hardcodes `index_type: "database_result"`/`mean_value: 0` | Derives `index_type`/`index_calibrated` from `satellite_type` (the real, available column) |
| H#6 | Confidence chain fragility — the satellite-0.0-cannot-yield-HIGH guarantee held via redundancy, not design | `report/node.py` now wires `PipelineState["confidence_scores"]` through as `incoming_payload`, making the dedicated `fa0d9bd` read keys genuinely live; added an explicit invariant assert; strengthened the regression test to isolate the dedicated channel |
| H#10 | `impact/node.py` prefers `flood_risk` over `overall_severity`, masking real non-flood risk | Hazard now exposes `primary_hazard_risk` (keyed by real `disaster_type`); impact reads it first |
| H#11 | No SAR-calibration caveat in report prompts | `report/intelligence.py`'s `_compact_context` now carries `index_calibrated` alongside `index_type` |
| H#12 | `evacuation_routes` field fed `priority_zones` data | Both the DB write and in-memory payload now persist `vuln.get("evacuation_routes")` |
| H#14 | `roads_blocked` stores km under a count-sounding name; DB/payload rounding mismatch | Added `roads_blocked_km` column (additive, old column kept); both now derive from the same `round(...,1)` value |
| Hazard #6 | Dead prompt-building code in `analyze_earthquake`/`analyze_landslide` | Converted to comments |

Not done (explicitly out of scope, science-model gaps): H#7 (SAR
calibration), H#8 (population exposure model), H#9/H#13/H#15/H#16 (lower
system-level severity per `SYSTEM_ANALYSIS.md`'s own ranking, not touched).

## Coverage Tolerance Fix Pass (2026-07-28, branch `fix/coverage-tolerance`)

`agents/satellite/processor.py`'s `process_satellite_imagery` previously
demanded **exactly 100.0% interior-AOI valid-pixel coverage** or hard-failed
with `status:"failed", reason:"insufficient_coverage"` — see
`agents/satellite/ANALYSIS.md` §2.3's original "100%-or-fail" description
(now annotated as superseded, not deleted). That rule existed to stop the
pipeline from silently reporting a partial AOI as a complete analysis — a
real goal — but enforced it by refusing to answer instead of answering
honestly with the limitation stated. It also turned ordinary cloud cover into
an unbounded search: a live run on a 2.4x2.7 km town took 6 hours across 4
scenes, because cloud gaps cannot be downloaded away — if the sky was covered
that week, no amount of additional scenes closes the gap, so the old rule
could never terminate successfully in exactly the weather conditions where
flood analysis matters most.

**Coverage is now a caller-controlled quality band, not a single cliff**
(`processor.py`'s `DEFAULT_MIN_COVERAGE_PERCENT`/`COVERAGE_FLOOR`/
`COVERAGE_CEILING` = 90/80/100). A caller-supplied `min_coverage_percent` is
clamped server-side into `[80, 100]` — non-negotiable in both directions: a
caller cannot ask for less than 80% (too poorly sampled to mean anything) or
more than 100% (meaningless). The achieved `interior_coverage_percent` bands
into one of three outcomes:
- **`>= min_coverage_percent`** → `status:"complete"`,
  `coverage_status:"target_met"`, a small proportional confidence penalty for
  any shortfall from 100 (`(100 - coverage) * COVERAGE_PENALTY_SCALE`, fed to
  the `ConfidenceTracker` as reduced evidence, not a hardcoded cliff).
- **`>= COVERAGE_FLOOR` and `< min_coverage_percent`** → still
  `status:"complete"`, but `coverage_status:"below_target_coverage"`, a
  doubled penalty, a HIGH-severity tracker concern, and an entry in
  `coverage_anomalies`.
- **`< COVERAGE_FLOOR`** → `status:"failed"`, `reason:"insufficient_coverage"`
  — the same hard-stop shape as before, just floor-driven (80.0) instead of
  100.0-driven.
The rule that matters — never report a partial analysis as complete without
saying so — is unchanged; only the enforcement mechanism moved from refusal
to explicit, always-present reporting. Every run now carries
`coverage_percent`, `coverage_status`, `gap_count`, `gap_area_km2`,
`gap_attribution` (nodata vs cloud pixel/area split) and `gaps` (geometry) on
**every** path, not just the failure path as before.

**Hard search budgets, independent of coverage** — the actual runaway-cost
fix. `max_scenes` (default 3), `max_download_gb` (default 4.0),
`max_search_seconds` (default 900.0) bound the WHOLE tiered search (across
all tiers, not per-tier; the pre-existing `DOOMED_DOWNLOAD_LIMIT` only ever
aborted a single tier on consecutive failures, never the total search cost).
Exhausting any budget stops immediately (no new download starts) and returns
the best coverage achieved so far, banded the same way as above —
`budget_exhausted` names which limit tripped (`"max_scenes"` /
`"max_download_gb"` / `"max_search_seconds"`). One `[BUDGET]` log line per
scene attempt shows the running total against each limit before it's spent.

**Un-closeable gaps stop being chased (CHANGE 3).** Before attempting a
candidate scene, its footprint must genuinely intersect the remaining gap
geometry (`_scene_intersects_gaps`, a real shapely intersection test against
the gap bboxes, not "try it and see"). When the remaining gap is
cloud-attributed (`gap_cause["cloud"] > gap_cause["nodata"]`) and no
remaining candidate has materially lower cloud cover than what was already
tried (a 5-point margin), the search stops and reports
`gap_limited_by:"cloud"` rather than continuing to burn budget on scenes that
can't help. This uses whichever cloud figure is available per-scene — the
AOI-restricted one from CHANGE 6 when present, else the scene-level catalogue
figure (`_scene_cloud_for_gap_check`).

**Marginal-return stopping (CHANGE 4).** The pre-existing near-zero (0.01)
doomed-streak check (raw duplicate-contribution detection) is unchanged. A
NEW, separate check: once an acquisition is accepted (gained > 0.01) but
gains less than `MIN_MARGINAL_COVERAGE_GAIN` (2.0 percentage points), the
search stops entirely (not just skips that scene) — `marginal_return_stop` in
`coverage_anomalies`, banded per the coverage rules above.

**Per-satellite tier windows (CHANGE 5, `sentinel.py`).**
`COVERAGE_TIERS_S2` (0/±3/±7/±14 days, unchanged) and `COVERAGE_TIERS_S1`
(0/±10/±14 — the old ±3/±7 intermediate steps collapsed into one ±10-day
same-orbit window). S1's same-relative-orbit revisit over Pakistan was
measured ~11 days (live CDSE query, 2026-07-27 — see
`agents/satellite/CLAUDE.md`'s "Tier-window revisit analysis" and the
confirming live-e2e finding in this file's "S1 coverage tiers 1-3
exactly-0.000%" entry above), so any tier narrower than one revisit cycle is
a structural near-no-op for S1 — there was nothing for ±3/±7 to find, ever,
regardless of window tuning. S2's combined-constellation ~5-day revisit
already matched its existing tiers; left untouched. `build_coverage_tiers`
now selects the right tuple via `coverage_tiers_for(satellite_type)`;
`COVERAGE_TIERS` is kept as a back-compat alias for the S2 tuple. Re-measure
the S1 figure once the post-June-2026 constellation configuration
(Sentinel-1A retired 2026-06-29) has a clean 90-day history — the 6-day
S1C/1D repeat cycle is Europe-concentrated per ESA/ASF planning and does not
yet apply globally.

**AOI-restricted cloud measurement (CHANGE 6) — COMPLETE (2026-07-28,
second pass).** `CLOUD_COVER_THRESHOLD` (30%) was applied to the scene's
whole-tile cloud percentage, not the AOI — a scene can be 45% cloudy across
its full footprint and completely clear over a small town (or vice versa); a
real run selected the uncalibrated SAR path on a 45.9% scene-level reading
without ever checking whether the AOI itself was obscured, when the optical
path may have been perfectly usable.

The first pass on this branch left the SCL pre-fetch itself undone (documented
as a partial implementation, with `select_satellite`/`_peek_cloud_cover`
staying a download-free metadata-only catalogue query). This pass closes that
gap without the restructure that first pass thought it needed: instead of
teaching `sentinel.select_satellite` to download (which would still cycle
back into `processor.py`), the orchestration moved up a level, into
`agents/satellite/agent.py`, which already imports from both modules:

1. `agent.py` queries the S2 catalogue for the best candidate
   (`sentinel.search_imagery(bbox, SENTINEL_2, aoi_geom=merged)`) FIRST, even
   when the disaster hint points at S1 — the query is free and a real scene
   object (with an `Id`) is required to peek at all. No S2 candidate in the
   window → S1 is selected immediately, `selection_reason="no_s2_candidates"`,
   nothing to measure.
2. The candidate's scene-level `cloudCover` attribute decides whether a peek
   is worth its cost, via `processor.peek_needed()`:
   - `< PEEK_CLEAR_BELOW = 15.0` → clearly clear, select S2, no peek.
   - `> PEEK_CLOUDY_ABOVE = 50.0` → clearly cloudy, select S1, no peek.
   - in between → genuinely ambiguous, peek.
   **Basis for the two cut points** (`processor.py`, next to the constants):
   below 15% the scene-level reading already has enough margin under the 30%
   threshold that a materially worse AOI-local reading is unlikely to flip
   the decision; above 50% an AOI clear enough to flip the decision back to
   S2 would be a large, unusual divergence, so the default (weather-independent
   SAR) is taken rather than paying for a peek on every heavily overcast
   scene. This mirrors the live incident this fix targets (a 45.9%
   scene-level reading, squarely inside the ambiguous band) while not
   spending a download on the two-thirds of scenes where the scene-level
   figure already settles it.
3. On a peek, `processor.peek_aoi_cloud_percent(scene, merged_polygon,
   event_id, token_manager, remaining_download_gb=...)` downloads ONLY the
   SCL band via the existing per-band Nodes path
   (`_download_bands_via_nodes(..., ["SCL"], "sentinel-2")`), reuses
   `stack_bands`/`clip_to_polygon` unchanged (both are generic over any band
   set, so a single-band SCL "cube" clips exactly like a full one), erodes
   the clip mask by one pixel (the same interior-AOI convention
   `compute_coverage` uses) and measures the invalid fraction with the SAME
   `_SCL_INVALID_CLASSES` set the coverage metric already uses. `select_satellite`
   itself stayed synchronous and download-free, exactly as the first pass's
   docstring said it should — it now just accepts an optional pre-computed
   `aoi_cloud_percent`/`aoi_cloud_reason` and applies `CLOUD_COVER_THRESHOLD`
   to the AOI figure when one is present, falling back to the scene-level
   figure otherwise.
4. If S2 is selected, the peeked SCL is never re-downloaded: the peek writes
   it to the SAME bands directory `download_imagery` uses for a single
   accepted scene (`<temp>/<event_id>/bands/SCL.jp2` — `download_imagery`
   only switches to a per-scene `scene_<Id>` subdir once a real multi-scene
   mosaic is assembled, `len(scenes) > 1`, which is not yet known at
   selection time). `_download_bands_via_nodes`'s existing on-disk fast path
   (checks every requested band is already present before any network call)
   then finds and reuses it. If the peeked candidate instead ends up folded
   into a multi-scene mosaic, the real download re-keys under `scene_<Id>`
   and re-fetches SCL — a correct cache MISS in that rarer case (the peek
   still paid for itself by deciding selection correctly), not a bug.
   **This reuse is now directly observable, not just structurally implied.**
   `_download_bands_via_nodes` logs an explicit `"SCL cache HIT — reusing
   peeked band, skipping download"` or `"SCL cache MISS — downloading SCL
   (peek reuse did not apply)"` at the exact point it checks for SCL on disk
   (both the fully-cached fast path and the per-band download loop), and
   records the outcome in a small module-level flag
   (`_set_scl_reused`/`_last_scl_reused`, mirroring the existing
   `_add_bytes_downloaded`/`_bytes_downloaded_total` pattern — safe because
   `process_satellite_imagery`'s tier/scene loop is sequential, no concurrent
   scene downloads within one event). `download_imagery` reads the flag right
   after each `_download_bands_via_nodes` call and returns it as
   `scl_reused: bool | None` (`None` when SCL was never requested at all,
   e.g. Sentinel-1); `_attempt_clip`/`_finish_success` carry it onto the
   accepted candidate's merged result, and `agent.py`'s `structured` result
   dict — the same dict that is persisted and becomes `PipelineState["satellite_result"]`
   — now carries `scl_reused` alongside `bytes_downloaded`, so it shows up in
   the pipeline log / `/pipeline-log` trail without inventing a separate
   logging channel.

**Reporting.** `scene_cloud_percent` is always present. `aoi_cloud_percent`
is the real measured value when a peek succeeded, `None` otherwise.
`selection_reason` names exactly which basis decided it: `"aoi_scl_measured"`
/ `"scene_metadata_clear"` / `"scene_metadata_cloudy"` / `"no_s2_candidates"`
/ `"scl_unavailable_fallback"`. When `aoi_cloud_percent` and
`scene_cloud_percent` diverge by 10 points or more, `select_satellite` logs
it at INFO — that divergence is the entire justification for this work.

**Fallbacks.** Sentinel-1 has no SCL at all and never reaches any of this
machinery — `agent.py` only ever peeks an S2 candidate
(`search_imagery(bbox, SENTINEL_2, ...)`), so S1 selection is unchanged in
every respect (verified by `test_s1_selection_path_unchanged`). A failed peek
(SCL absent, download/stack/clip failure) returns
`{"aoi_cloud_percent": None, "reason": "scl_..._failed"}` and selection falls
back to the scene-level figure with `selection_reason="scl_unavailable_fallback"`
— a peek is an optimisation, never a requirement, and never aborts the run.

**Budget interaction.** The peek's SCL download goes through the SAME
`_stream_to_file_with_retry` → `_add_bytes_downloaded` path every other
download uses, so it counts against `max_download_gb` by construction, not by
a separate accounting path. `agent.py` also checks the remaining budget
BEFORE attempting a peek (`remaining_download_gb`) and skips the peek
entirely — falling back to the scene-level figure — if the budget is already
exhausted, per `peek_aoi_cloud_percent`'s own `budget_exhausted` early return.

**Measured SCL download size:** not measured against live CDSE this session
(no live e2e was run, per the task's own scope — offline/mocked coverage
only). `agents/satellite/CLAUDE.md`'s 2026-07-27 log records a live 4-band S2
download (B03/B08/B11/TCI) totalling 407 MB; SCL is a single 20 m band in that
same set (comparable to B11, the other 20 m band in that run, ~34 MB) — so a
single peek is expected to cost roughly that order of magnitude, a small
fraction of the ~400 MB a full per-band scene download costs. This estimate
should be confirmed against a real run before relying on it for capacity
planning.

**What this did NOT need, contrary to the first pass's assessment:** no
import-cycle-breaking restructure and no async `select_satellite`. The
orchestration (when to query, when to peek, when to reuse the token) lives in
`agent.py`, which already sits above both `sentinel.py` and `processor.py` in
the import graph — `sentinel.py` stays synchronous and pure-metadata, and
`processor.py` stays the only module that touches the network for band data,
unchanged from before this pass.

`CHANGE 3`'s cloud-gap comparison (`_scene_cloud_for_gap_check`) already
preferred an AOI-restricted figure the moment one exists
(`scene["_aoi_cloud"]`) — nothing further was needed there; it was written
ahead of this gap closing.

**Tests:** `agents/satellite/tests/test_coverage_tolerance.py` gained 19 new
checks (58 total in that file, up from 28) covering: peek_needed's two cut
points and the ambiguous band between them, AOI-vs-scene-level conflict
resolution in both directions, a failed SCL download falling back cleanly, the
SCL-reuse path (asserted structurally — same `event_id`-keyed directory,
single call to `_download_bands_via_nodes`), peek bytes counting against the
global download-byte total, an exhausted byte budget skipping the peek
outright, the S1 path staying provably unchanged, and (added in a follow-up
within this same pass) the explicit SCL cache HIT/MISS log lines and the
`scl_reused` flag's propagation from `_download_bands_via_nodes` through
`download_imagery` into the merged result. Full offline suite re-run:
`test_coverage_tolerance.py` 58/58,
`test_correctness_fixes_20260727.py` 18/19 (the 1 failure — a `scene_id`
source-grep check unrelated to this change — confirmed pre-existing via
`git stash`, present identically before and after this session's edits),
`test_index_label_integrity.py` 6/6. `test_bug_fixes.py`/`test_clip_window.py`
fail in this dev environment on an unrelated `PROJ_LIB` conflict (system
PostgreSQL/PostGIS's `proj.db` shadowing rasterio's bundled proj data — a
pre-existing environment issue, not a code regression). No live e2e was run
this session, per the task's explicit scope — that is the natural next step
once this lands.

**Threading.** `min_coverage_percent`/`max_scenes`/`max_download_gb`/
`max_search_seconds` are optional fields on `backend/models.py`'s
`AnalyzeRequest`, threaded through `backend/router.py`'s `disaster_data` →
`backend/orchestrator.py`'s initial `PipelineState` →
`shared/pipeline_state.py`'s TypedDict → `agents/satellite/node.py` →
`agents/satellite/agent.py`'s `ProcessDisasterInput` →
`process_satellite_imagery`'s kwargs (via the new
`_coverage_budget_kwargs()` helper). None of the four defaults are
hardcoded anywhere along that chain except as `process_satellite_imagery`'s
own parameter defaults — every upstream layer either passes the caller's
value through or omits the kwarg, letting the innermost function's default
win.

**Not attempted this session (explicitly out of scope, per the task):** a
true mid-run interactive gate (pausing at, say, 87% coverage to ask a human
whether to proceed) needs LangGraph's `interrupt()` plus a persistent
checkpointer (`PostgresSaver`), neither of which is wired yet. What landed
here is caller-controlled *tolerance* (set once, up front, via the API
request), not interactive human-in-the-loop — a natural follow-on once
`PostgresSaver` lands.

**Tests:** `agents/satellite/tests/test_coverage_tolerance.py` (new, 28
checks, offline/deterministic) plus an update to
`test_bug_fixes.py::test_bug3_partial_coverage_fails_not_risk` (the old test
asserted 95% hard-fails, which is now the wrong expectation since 95% is
above the default 90% target — the test now exercises the real floor-driven
hard-fail case at 70%). Full existing offline suite re-run alongside these
changes: 54/54 pass, no regressions (see `agents/satellite/ANALYSIS.md` for
the coverage-of-what-ran detail). No live e2e was run this session — that is
the explicit next step, to be done separately against these changes with a
clean run.

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
