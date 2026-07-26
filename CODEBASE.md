# HazardMind AI — Codebase Documentation

> Generated from a full read-through of every tracked file in the repository (177 files) plus each subsystem's internal `CLAUDE.md` engineering logs. This document is the authoritative map of the system as the code actually is today — including drift from the docs, dormant/dead code, and known inconsistencies.

---

## 1. Project Overview

**HazardMind AI** is an autonomous multi-agent disaster-intelligence platform. Given a location and disaster type (flood / earthquake / landslide), it:

1. Resolves the location to a **real administrative boundary** (geoBoundaries ADM1–ADM3, not a fixed demo list — works for any of 249 ISO countries).
2. Pulls **live Sentinel-1/2 imagery** from the Copernicus Data Space, mosaicking multiple scenes when needed for full coverage.
3. Computes **grounded hazard indices** — NDWI (flood), USGS observed seismicity (earthquake), real SRTM 30m DEM slope (landslide) — never inferring risk from a region's reputation.
4. Assesses **population and infrastructure impact** from real GeoNames/OSM data, with an honest **zero-impact gate** for non-events.
5. Generates an **executive PDF report**, a static risk map, and a GeoJSON zone layer, uploaded to Cloudflare R2.
6. Displays everything on an interactive 3D globe frontend with a live agent-chat feed.

### Core architecture & design philosophy

- **Five independent services**: a FastAPI orchestrator (`backend/`) plus four Band-SDK-connected agents (`agents/satellite`, `agents/hazard`, `agents/impact`, `agents/report`), each its own deployable process/container with its own venv, `requirements.txt`, and `.env`.
- **Database-backed hand-off, not message-bus-only.** Each agent persists its result to Postgres (Neon) *before* posting to the Band chat room. Downstream agents read the DB directly as a reliable fallback whenever the Band room transcript is empty or a message was missed — this is the actual reliability backbone of the whole pipeline, more so than Band itself.
- **Deterministic dispatch over LLM tool-calling.** Every agent's Band adapter LLM (Featherless/Gemini via LangGraph) is unreliable at emitting tool calls, so each agent overrides `on_message` to **parse the dispatch text deterministically and call its own pipeline function directly**, bypassing the LLM tool-call path entirely. The LLM still runs and can call the tool too (idempotent, guarded by an `_completed_event_ids`/`_autodispatched_event_ids` set), but in practice the deterministic path wins the race almost every time.
- **Multi-provider LLM routing with criticality-based escalation.** Every agent routes LLM calls through a fallback chain: Featherless (cheap, primary) → Gemini (now effectively primary escalation, since the AIML account ran out of funds) → AIML/Claude Opus → AIML/GPT (last resort). See [§7 LLM Provider Routing](#7-llm-provider-routing-cross-cutting).
- **Honesty by design.** Risk levels are derived deterministically from data (NDWI thresholds, USGS magnitude, DEM slope), not left to an LLM to "vibe out" from a location's reputation. The impact agent has an explicit no-significant-disaster gate that reports **zero affected** rather than fabricating casualties. Report generation has strict-mode assertions that **refuse to ship a report if a required LLM section silently fell back to templated text** — a total LLM failure is a `status: failed`, not a fake success.
- **event_id is generated exactly once**, by the backend, and threaded through every agent unchanged. Because the Band LangGraph adapter's LLM sometimes truncates the UUID to its leading 8 characters when parsing a tool argument, all four pipeline agents implement the same defense-in-depth pattern: a `_BoundEventIdAdapter.on_message` override that snapshots the full UUID from the raw dispatch text **before the LLM ever sees it**, binds it to the room, and every downstream function resolves the room-bound value in preference to whatever the LLM parsed.

### How the system works end to end

```
User query ("flood in Rawalpindi")
  │
  ▼
Frontend (Next.js)  ──POST /analyze──▶  Backend (FastAPI orchestrator)
  │                                         │
  │                                         ├─ event_id = uuid4()  (generated ONCE)
  │                                         ├─ INSERT disaster_events
  │                                         ├─ create/reuse Band room
  │                                         └─ @mention hazardmind-satellite
  │                                              │
  │                                              ▼
  │                                    Satellite agent (agents/satellite)
  │                                    boundary → scene select → download →
  │                                    NDWI/NDVI/SAR → classify → vectorize →
  │                                    cross-validate → upload PNGs+GeoJSON to R2
  │                                    → write satellite_results → @hazard
  │                                              │
  │                                              ▼
  │                                    Hazard agent (agents/hazard)
  │                                    flood(NDWI) + earthquake(USGS) +
  │                                    landslide(DEM slope) → hazard_zones (3 rows)
  │                                    → @impact
  │                                              │
  │                                              ▼
  │                                    Impact agent (agents/impact)
  │                                    no-disaster gate, else GeoNames population +
  │                                    Overpass infrastructure + vulnerability/
  │                                    evacuation reasoning → impact_data → @report
  │                                              │
  │                                              ▼
  │                                    Report agent (agents/report)
  │                                    LLM narrative + intelligence layer →
  │                                    static PNG map (Pillow) → PDF (ReportLab)
  │                                    → upload to R2 → final_reports
  │                                              │
  │                                              ▼
  │                                    Orchestrator posts outcome-aware verdict
  │                                    (all-clear vs. dispatch-ready summary)
  │
  ▼ (frontend polls /status, /results, /band-log every 2.5s)
Interactive 3D globe + live agent chat + downloadable PDF/map/GeoJSON
```

---

## 2. Directory Structure

```
hazardmind-ai/
├── README.md, DEPLOY_GCP.md, LICENSE, .env.example, .gitignore, .gitattributes
│
├── backend/                     FastAPI orchestrator + REST API (no GDAL — lightweight image)
│   ├── main.py                    app factory, lifespan, CORS, /health
│   ├── router.py                  /analyze /status /results /band-log routes
│   ├── orchestrator.py            OrchestratorAgent: Band connection, pipeline state machine
│   ├── band_client.py             full Band messaging layer (TEXT+EVENT channels, natural msgs)
│   ├── db.py                      asyncpg pool + all DB read/write helpers
│   ├── models.py                  Pydantic request/response schemas
│   ├── cleanup.py                 background loop: stuck-event GC + Band backlog drain
│   ├── entrypoint.sh, Dockerfile  container entry (writes agent_config.yaml from secrets)
│   ├── test_*.py                  pytest/live test suite (5 files)
│   └── CLAUDE.md, README.md       engineering log / HF Space manifest
│
├── agents/
│   ├── satellite/                Agent 1 — imagery acquisition & analysis (heaviest: GDAL/rasterio)
│   │   ├── agent.py                Band entry point + full pipeline orchestration (1592 lines)
│   │   ├── boundary.py             admin-boundary + risk-city resolution, merge, bbox
│   │   ├── geoboundaries.py        geoBoundaries ADM-level resolver (global, per-country)
│   │   ├── sentinel.py             CDSE auth, scene search/scoring, mosaic set-cover, backfill
│   │   ├── processor.py            download → stack → clip → indices → PNG → vectorize (1826 lines)
│   │   ├── intelligence.py         SatelliteIntelligence: 6 LLM reasoning methods
│   │   ├── confidence_tracker.py   pure evidence/concern ledger, no I/O
│   │   ├── cross_validator.py      GDACS/USGS/cloud/physics/coverage/LLM cross-checks
│   │   ├── stance_engine.py        LLM "agree or push back" reasoning (implemented, NOT wired in)
│   │   ├── r2_upload.py            boto3 → Cloudflare R2 upload, demo-cache check
│   │   ├── room_drain.py           startup Band backlog drain
│   │   ├── hf_app.py               HF Space health server + agent.py runner
│   │   ├── verify_setup.py         standalone connectivity smoke test (stale adapter)
│   │   ├── tests/                  7 standalone (non-pytest) test suites, live + offline
│   │   └── CLAUDE.md (65KB)        exhaustive "Step 1..17" engineering log — the best source of "why"
│   │
│   ├── hazard/                   Agent 2 — multi-hazard risk classification
│   │   ├── agent.py                Band entry point, deterministic autodispatch, DB write
│   │   ├── analyzer.py             flood/earthquake/landslide analysis (GDACS/USGS/DEM slope)
│   │   ├── intelligence.py         LLM routing + parse/strategy/interpret/quality-check
│   │   ├── room_drain.py, hf_app.py, test_db.py
│   │   └── CLAUDE.md (stale checklist), README.md
│   │
│   ├── impact/                   Agent 3 — population & infrastructure impact
│   │   ├── agent.py                Band entry point (production)
│   │   ├── main.py                 FastAPI local test server (port 8001)
│   │   ├── services/               band_client, db, llm_router (live router), featherless
│   │   │                           (legacy/unused), cost_tracker, criticality (unused), r2_reader
│   │   ├── tasks/                  population.py, infrastructure.py, vulnerability.py
│   │   ├── mock_hazard_output.json local test fixture
│   │   └── CLAUDE.md, README.md
│   │
│   ├── report/                   Agent 4 — executive report, map, PDF
│   │   ├── band_agent.py           **live** Band entry point (agent.py is an offline CLI variant)
│   │   ├── band_contract.py        wire-format contract: parse/build handoff messages
│   │   ├── pipeline.py             run_report_pipeline — the end-to-end orchestrator
│   │   ├── generator.py            report content assembly + strict-mode validation
│   │   ├── intelligence.py         7-section intelligence layer (criticality, anomalies, etc.)
│   │   ├── llm_clients.py          low-level multi-provider LLM call layer + cascades
│   │   ├── db_client.py            Neon read (context) + write (final_reports)
│   │   ├── map_generator.py        pure-Pillow static risk-map PNG renderer
│   │   ├── pdf_generator.py        ReportLab PDF assembly
│   │   ├── geometry_utils.py       GeoJSON validation/normalization
│   │   ├── storage_client.py       R2 upload (boto3)
│   │   ├── hardcore_test.py        comprehensive non-pytest test harness
│   │   ├── test_fixtures/          5 GeoJSON fixtures (incl. one intentionally malformed) + 1 msg
│   │   ├── room_drain.py, hf_app.py, verify_setup.py
│   │   └── CLAUDE.md (stale — describes wrong adapter + wrong map tech), README.md
│   │
│   └── (each agent ships its own .env.example, requirements.txt, Dockerfile, agent_config.yaml)
│
├── frontend/                    Next.js 14 + React 18 + TypeScript + Mapbox GL (3D globe)
│   ├── app/
│   │   ├── page.tsx                → DashboardShell (main "Command Center")
│   │   ├── map/[eventId]/page.tsx  → MapSnapshotView (read-only per-event permalink)
│   │   ├── api/r2/route.ts         CORS-bypass proxy for the public R2 bucket
│   │   ├── layout.tsx, globals.css (3045-line hand-authored HUD design system)
│   ├── components/                24 components — roughly HALF are dead code (see §9)
│   ├── lib/                       analyze.ts, loadHazardResult.ts, bandLog.ts, types.ts, etc.
│   └── package.json, next.config.mjs, tailwind.config.ts, ...
│
└── shared/                      Cross-agent utilities (older/simpler than the agents' own copies)
    ├── db/schema.sql              canonical(ish) Postgres/PostGIS DDL — DRIFTED from live DB (see §5)
    ├── models/types.py            shared dataclasses (also drifted from live schema)
    └── utils/
        ├── band_client.py         thin .env loader + basic LLM fallback (superseded, legacy)
        └── llm_fallback.py        canonical-ish 4-link LLM fallback chain (Featherless→Gemini→Claude→GPT)
```

---

## 3. Backend (`backend/`)

FastAPI orchestrator service. Owns the public REST API, generates the canonical `event_id`, drives the Band pipeline state machine, and is the only service that talks directly to the frontend.

### `backend/main.py`
FastAPI app factory. `lifespan()` calls `orchestrator.connect()` on startup (best-effort — API still serves reads if Band is down) and starts `cleanup.cleanup_loop()` as a background task; closes the DB pool and cancels cleanup on shutdown. CORS via `ALLOWED_ORIGINS` (default `"*"`). `GET /health` reports `band`/`db` connectivity.
**Depends on:** `db.py`, `router.py`. **Env:** `ALLOWED_ORIGINS`.

### `backend/router.py`
All HTTP routes; owns the module-level `orchestrator = OrchestratorAgent()` singleton.
- `POST /analyze` — concurrency-caps via `MAX_CONCURRENT_EVENTS` (default 2, matches Featherless's 4-unit/2-units-per-request free tier), generates `event_id`, resolves a room (static `BAND_ROOM_ID` or, if `DYNAMIC_BAND_ROOMS` is truthy, a fresh per-event room), inserts `disaster_events`, calls `orchestrator.start_pipeline(...)`, and fires a background `_monitor` task. Never 500s to the caller — a dispatch failure still returns 200 with `status:"failed"`.
- `GET /status/{job_id}`, `GET /results/{job_id}` (202 while processing), `GET /band-log/{job_id}` (reads `inbound_store`, not Band REST history — see below).
**Depends on:** `band_client.py`, `db.py`, `orchestrator.py`, `models.py`.

### `backend/orchestrator.py`
The pipeline state machine. `get_best_adapter()` picks Gemini → Claude → Featherless/LangGraph, each wrapped **record-only** (`_record_only`) so the orchestrator's LLM never auto-replies to inbound messages — this was a deliberate fix for an infinite orchestrator↔satellite chatter loop. `OrchestratorAgent.start_pipeline` posts the satellite dispatch as one natural-prose message with a structured tail. `monitor_progress` polls indefinitely (5s interval): checks each stage's completion via a Band-transcript signal *or* a DB-row-exists fallback (`_agent_completed_in_db`), advances via `_advance`/`_handoff`, nudges a silent agent after 45s (max 2 nudges, never itself advances the pipeline), and detects failure via `_agent_failed`. `cross_validate_and_discuss` is the anomaly-detection engine — 5 documented triggers (satellite/GDACS extent mismatch, low confidence, CRITICAL risk broadcast, HIGH risk with too few zones, multiple disasters) that open a Band discussion and pause 30s for a response. `on_pipeline_complete` posts an **outcome-aware verdict**: all-clear vs. dispatch-ready summary, derived from real downstream data, never a canned success message.
**Depends on:** `band_client.py`, `db.py`. **Env:** `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `ORCHESTRATOR_MODEL`, `FEATHERLESS_API_KEY`/`FEATHERLESS_BASE_URL`/`FEATHERLESS_MODEL`.

### `backend/band_client.py`
The full-featured Band messaging layer (distinct from and NOT the same module as `shared/utils/band_client.py`, an older/thinner file — see §5.4). Implements both Band channels:
- **TEXT** (`POST .../messages`) — visible chat, requires ≥1 mention, an agent cannot mention itself.
- **EVENT** (`POST .../events`) — structured, no mention required, not rendered as chat; `message_type` restricted to `task|thought|tool_call|tool_result|error`.

`send_handoff` posts exactly ONE message per hand-off: LLM-generated natural prose (`generate_natural_message`, Featherless-chain with a templated non-LLM fallback so the pipeline never blocks on an LLM outage) followed by a compact JSON tail with `_HANDOFF_DROP_KEYS` stripped — most importantly `region_boundary`/`geojson`/`coordinates` (full MultiPolygon geometry can be ~20k chars and was observed to blow Featherless's 32k context cap, permanently failing the receiving agent's turn). `create_event_room` implements dynamic per-event rooms (gated off by default — see below). The `InboundStore` class is the actual source of truth for both `/band-log` and completion detection, because **Band's REST message history is empty for the orchestrator agent** — inbound delivery only happens over the WebSocket execution loop, tapped via the `_record_only` adapter, plus a REST-poll complement (`poll_room_into_store`).

**Why dynamic per-event rooms are OFF by default (`DYNAMIC_BAND_ROOMS=false`):** the orchestrator's Band API key can only auto-populate a room with same-owner agents. Satellite shares the orchestrator's owner and auto-joins; hazard/impact/report historically belonged to different Band owner accounts, and adding them explicitly 403s while @mentioning a non-member 422s. The static shared `BAND_ROOM_ID` works today only because all agents were manually invited once. `.env.example` documents `DYNAMIC_BAND_ROOMS=true` as intended once all agents share one owner — this is a **live discrepancy** between the documented default and the code's fallback default.

### `backend/db.py`
asyncpg pool (`NEON_DATABASE_URL`, `ssl="require"`) with a registered `jsonb` codec (encoder=`json.dumps`, decoder=`json.loads` — callers must pass raw dict/list, never pre-serialize, or values double-encode). `create_disaster_event` self-migrates `band_room_id` via `ADD COLUMN IF NOT EXISTS`. `insert_satellite_result` is a transaction (`DELETE` then `INSERT`, idempotent per event). `count_active_events` powers the `/analyze` concurrency gate (only counts events updated in the last 30 minutes, so a stuck event doesn't block forever). `get_event_results` does a 4-way LEFT JOIN across all child tables, returning each as `to_jsonb(...) - 'event_id'`.

> **⚠️ Schema drift (see §5 for full detail):** the columns `insert_satellite_result` writes (`satellite_type, cloud_cover, scene_id, true_color_url, index_url, classification_url, geojson_url, ...`) do **not match** `shared/db/schema.sql`'s `satellite_results` table (`image_url, affected_area_km2, land_cover`). Similarly `disaster_events` in `db.py` reads/writes `status/step/progress/magnitude/updated_at`, none of which exist in `schema.sql`. The live Neon database almost certainly has more columns than the checked-in schema file — **do not treat `shared/db/schema.sql` as authoritative without verifying against the live DB.**

### `backend/models.py`
Pydantic schemas: `AnalyzeRequest{location, disaster_type, magnitude?}`, `AnalyzeResponse{job_id, status, message, band_room_id?}`, `StatusResponse{job_id, status, step, progress, created_at, updated_at}`, `ResultsResponse{job_id, status, satellite?, hazard?, impact?, report?}`, `BandLogResponse{job_id, messages}`.

### `backend/cleanup.py` (untracked/new)
Background loop (`CLEANUP_INTERVAL_HOURS`, default 12): (1) `UPDATE disaster_events SET status='stopped' WHERE status IN ('processing','received') AND updated_at < now() - 30min` — frees concurrency slots from stuck events; (2) drains every pipeline agent's Band `/messages/next` backlog using each agent's own key (`SATELLITE_BAND_API_KEY` etc. — the backend holds all agents' keys as secrets specifically to run this fleet-wide cleanup) so a reconnecting agent never replays a finished event.

### `backend/entrypoint.sh` / `Dockerfile`
Writes `agent_config.yaml` from `BAND_AGENT_ID`/`BAND_API_KEY` env vars at container start (keeps the key out of the image), then `exec uvicorn main:app`. Base image `python:3.12-slim` + `curl` only — no GDAL (this is the lightweight service in the fleet).

### Backend test files
`test_band_log.py`, `test_db.py` (live, hits real Neon), `test_live_band.py` (live, posts real Band messages, injects an artificial GDACS-extent anomaly to prove discussion triggers fire), `test_orchestrator.py` (the most thorough — full stubbed pipeline advance, failure handling, recording-adapter verification), `test_results.py`.

---

## 4. Agents

### 4.1 Satellite Agent (`agents/satellite/`) — Agent 1

The most complex service (1592-line `agent.py`, 1826-line `processor.py`, 65KB CLAUDE.md documenting 17 iterative "Steps" of real production fixes). GDAL/rasterio/GEOS-heavy — the only agent whose Dockerfile needs the full geospatial system-library stack.

**Band connection & dispatch:** `main()` builds a LangGraph `ChatOpenAI` (Featherless `google/gemma-4-31B-it` primary, chained `.with_fallbacks()` through up to 5 Gemini keys), wraps it in `_BoundEventIdAdapter` (a `LangGraphAdapter` subclass), drains all joined rooms' backlogs on startup, connects with up to 8 retries on Band's 429 rate-limiting, and calls `agent.run_forever()`. `on_message` (called by Band *before* the LLM turn) snapshots the full UUID event_id and fires `_maybe_autodispatch` as a background task — deterministic dispatch parsing that calls `run_pipeline` directly, bypassing the LLM tool-call. `run_pipeline` offloads the blocking work to a thread (`asyncio.to_thread`) so imagery I/O never starves the WebSocket keepalive.

**The pipeline (`_run_pipeline_sync`), in order:**
1. **event_id resolution & idempotency** — room-bound UUID wins; `_completed_event_ids` guard.
2. **LLM input parsing & ambiguity gate** — `intelligence.parse_disaster_input`; a genuinely ambiguous core field (location/type) returns `clarification_needed` instead of proceeding.
3. **Boundary resolution** (`boundary.py` + `geoboundaries.py`) — resolves the REAL administrative polygon via a 3-tier chain: geoBoundaries (authoritative, correct ADM level per country) → Nominatim/OSM areal relation → a buffered disk as a **loud last resort only** (most cities in OSM/Nominatim are mapped as zero-area Points, which is why geoBoundaries was added — without it, nearly every city silently fell back to an arbitrary ~6km circle, not any real boundary). `get_analysis_bbox` on the *merged* risk-city polygon (not the whole region) is the actual clip/search extent.
4. **Demo cache short-circuit** for 3 hardcoded event names (peshawar/dhaka/kathmandu).
5. **CDSE OAuth2 auth** with LLM-guided retry on failure.
6. **Satellite selection** — cloud-cover-aware: real observed cloud cover (via a lightweight metadata peek) always overrides the disaster-type hint (flood→SAR, earthquake→optical) — "physics over assumption."
7. **Scene search & coverage-aware ranking** — score = AOI-polygon-overlap × (1−cloud%) × recency-decay, measured against the *actual risk polygon*, not the bbox rectangle (fixes a real bug where a wide, mostly-empty bbox around scattered cities picked an edge tile covering 0% of any city). **Backfill** widens the search window (7→14→30 days) per-city when a city isn't covered by ≥2 independent scene acquisitions (a single acquisition's catalogue footprint can overstate its real pixel coverage).
8. **Download** — walks CDSE's OData Nodes tree to fetch only the needed bands individually (~30–120MB each) rather than the whole ~868MB `.SAFE` zip, because CDSE never honors HTTP Range (always 200, never 206) — per-band download means a dropped connection only costs one band, with each *completed* band resetting a per-band outage-grace budget.
9. **Mosaic** (if best single scene covers <85% of the AOI) — greedy weighted set-cover over individual city polygons (not naive top-N-by-score, which was observed to bunch all mosaic slots on the single best-covered city and leave others uncovered).
10. **Stack → clip to real polygon** (not bbox) — pre-windows to the polygon's pixel bbox before rasterizing, a major performance fix on large mosaics.
11. **Indices & classification** — NDWI (flood), NDVI (earthquake/landslide), SAR VV dB; graded per-pixel classification (0=safe, 1–3=severity, 255=nodata).
12. **PNG export** — all 3 layers RGBA with alpha=0 outside the clip polygon, so overlays composite cleanly on a map instead of showing a black/white box.
13. **Vectorization** — per-class polygonization, WGS84 reprojection, simplify, drop sub-0.5km² slivers.
14. **Coverage guard** — rejects any candidate under 5% valid pixels, tries the next; returns `coverage_insufficient` (not a silent empty result) if every candidate fails.
15. **R2 upload** — boto3 S3-compatible client, all artifacts `ACL=public-read`.
16. **Cross-validation** (`cross_validator.py`) — checks GDACS, USGS, cloud cover, index physics, coverage, and a Featherless "expert opinion" call, each feeding a `ConfidenceTracker` (pure, no-I/O ledger: weighted-average evidence minus per-severity concern penalties). The tracker's score — not the LLM's raw self-rating — becomes the authoritative `confidence`.
17. **Interpretation, quality gate, natural Band message, DB persist, completion post** — `_post_completion` posts the agent's own authoritative signal (not relying on the LLM to relay the tool's JSON verbatim) and writes `satellite_results` directly — the reliable hand-off channel to hazard.

**Dormant/unused code:** `StanceEngine` (evidence-based agree/push-back reasoning) is fully implemented and unit-tested but **never called from `agent.py`**. `intelligence.decide_landsat_fallback` is implemented but never called. Per-city artifact rendering (`_render_per_city`) is implemented but disabled (`city_boundaries=None`) — deemed too slow/memory-heavy for the value once the merged whole-area result already covers every city. `verify_setup.py` still references the older `AnthropicAdapter`, inconsistent with the current `LangGraphAdapter` runtime.

**Key files:** `boundary.py`, `geoboundaries.py` (global ADM-level resolver via `pycountry` + geoBoundaries gbOpen), `sentinel.py` (auth/search/scoring/mosaic/backfill), `processor.py` (the full raster pipeline), `intelligence.py` (6 LLM methods: parse/strategy/anomaly/interpret/message/landsat-decision), `confidence_tracker.py`, `cross_validator.py`, `stance_engine.py`, `r2_upload.py`.

### 4.2 Hazard Agent (`agents/hazard/`) — Agent 2

Converts the satellite result into multi-hazard risk levels. Same Band connection pattern as satellite (LangGraph adapter, `_BoundEventIdAdapter`, deterministic autodispatch via `_maybe_autodispatch_hazard` with a 3-tier payload resolution: live message → REST room history → **DB read of `satellite_results`**).

**`_normalise_satellite_payload`** is a critical contract adapter: the satellite agent emits a FLAT payload (`bbox`, `affected_area_km2` at top level) but the analyzer expects NESTED (`boundaries.bbox`, `analysis.affected_area_km2`). This mismatch previously made every risk `UNKNOWN` with a hardcoded `HIGH` severity — now fixed by reshaping flat→nested on ingest.

**`analyzer.py`:**
- `analyze_flood` — LLM-reasoned (NDWI/SAR-aware prompt) with a deterministic area/index-threshold fallback.
- `analyze_earthquake` — **fully deterministic, no LLM**, by design: LLMs repeatedly inflated risk from "regional reputation" (e.g. "Pakistan is seismically active") even with zero recent USGS events. Risk purely from max observed magnitude.
- `analyze_landslide` — **fully deterministic**, using only a real DEM slope sampled from OpenTopoData SRTM 30m (5×5 grid, numpy-gradient slope in degrees) — GDACS event counts were found to be unfiltered by bbox (93 "landslide" events near Rawalpindi actually located in China/Mongolia).
- `overall_severity` = max of the three risk levels; `UNKNOWN` maps to the lowest severity so it never inflates the overall result (a previous bug force-set HIGH whenever ≥2 hazards were UNKNOWN).
- `risk_polygons` is **always an empty dict** — CLAUDE.md/README describe "risk polygons in PostGIS" as a responsibility, but this is unimplemented.

`intelligence.py` — same LLM-routing shape as satellite's (Featherless chain → Gemini → AIML Opus), plus `quality_check` (validates the result shape before DB write/handoff) and `write_band_message` (natural <150-word handoff, templated fallback).

`write_to_db` writes **one row per hazard type** (flood/earthquake/landslide) into `hazard_zones`, upserting via `ON CONFLICT (event_id, hazard_type)`.

**Note:** CLAUDE.md's "Setup progress" checklist is stale (mostly unchecked items that are actually done in code) — unlike satellite's actively-maintained CLAUDE.md.

### 4.3 Impact Agent (`agents/impact/`) — Agent 3

Two entry points: `agent.py` (production, Band SDK) and `main.py` (local FastAPI test server, port 8001, `POST /assess-impact`).

**The no-significant-disaster gate** (`_no_significant_disaster`): if `risk_level` is LOW/NONE/UNKNOWN/MINIMAL/NEGLIGIBLE, the agent short-circuits to `_emit_no_impact` — all zero counts, `no_significant_impact: True` — rather than running the tasks and having an LLM invent plausible-sounding casualty numbers. Overridable via `IMPACT_FORCE_ASSESS=true`.

**Task pipeline** (real data + LLM reasoning about the *subset* affected, never pure LLM invention):
- **Task 1 — Population** (`tasks/population.py`): real population from GeoNames `/searchJSON` for the primary risk city; LLM reasons about the flood-zone-affected fraction and true metro population (2–5× the administrative figure). Dynamic criticality escalation based on the returned population (>2M→critical, >500K→high). Zero-result retry once at higher criticality; conservative floor (`max(2% of real_pop, 500)`) rather than raising if still zero.
- **Task 2 — Infrastructure** (`tasks/infrastructure.py`): real hospital/school/bridge/road counts from Overpass OSM (3 endpoints with failover); LLM reasons about the affected subset. Escalates to `high` criticality if `hospitals_at_risk > 10`.
- Tasks 1+2 run in **parallel** via `asyncio.gather` (documented as mandatory).
- **Task 3 — Vulnerability** (`tasks/vulnerability.py`): sequential, no external API — combines Task 1/2 results; the LLM prompt bakes in hard minimum-score rules (population>1M & hospitals>10 → score≥8.0; all three hazards HIGH/CRITICAL → score≥9.0).

**Anomaly checks after assessment:** `hospitals_at_risk > 10` → NDMA Level-3 critical alert; `overall_confidence < 0.7` → low-confidence field-verification alert.

**`services/llm_router.py` — the live multi-provider router**, exact model roster:
- Featherless chain: `google/gemma-4-31B-it` → `moonshotai/Kimi-K2.6` → `Qwen/Qwen3.6-35B-A3B`.
- AIML: `claude-opus-4-8` (secondary escalation), `gpt-5.5-2026-04-23` (last resort, only if Opus throws).
- Gemini: `gemini-2.5-flash`.
- **`opus_call()` implements the Gemini-first-since-AIML-ran-dry behavior explicitly**: `PREFER_GEMINI_ESCALATION` (default true) makes Gemini the actual primary escalation path, falling through to AIML Opus only if Gemini fails. `_call_model` fail-fasts (no retry) on error text containing `RESOURCE_EXHAUSTED`/`free_tier`/`out of funds`.
- Routing by criticality: `low`→Featherless only; `normal`→Featherless, escalate to Opus(Gemini-first) if confidence<0.6 but **keep** the low-confidence result rather than returning null if escalation fails too; `high`→Opus-first, GPT fallback, Featherless safety net; `critical`→same as high, plus a Featherless verification pass merged via `combine_results`.

**Legacy/unused code:** `services/featherless.py` (a simpler, superseded `call_with_fallback` — not imported by any current `tasks/*.py`) and `services/criticality.py` (defines `determine_criticality` but is never actually called — each task computes its own inline criticality logic instead).

**Schema note:** `services/db.py`'s `impact_data` DDL has no `overall_confidence` column, but `agents/report/db_client.py` selects `overall_confidence` from `impact_data` — a cross-agent schema-expectation mismatch, acknowledged in impact's own CLAUDE.md as "the schema block below is stale."

### 4.4 Report Agent (`agents/report/`) — Agent 4

`band_agent.py` is the **live** production entry point (Gemini-primary Band adapter — reversed priority vs. the other three agents, because the adapter replays the whole room transcript into one turn and Featherless's 32k context + 4-unit concurrency cap collide under the report agent's especially LLM-heavy narrative generation). `agent.py` is a separate offline/CLI variant (`--contract-test`, `--from-db`, `--band-message-file`) used for testing and manual runs.

**`band_contract.py`** defines the wire format precisely: `parse_report_trigger_message` validates an inbound `{event_id, from, to, data, anomalies}` message addressed (handle-suffix-tolerant) to `hazardmind-report`; `build_report_completion_message`/`build_report_failure_message` build the outbound `<natural text>\n\n---\n<json>` envelope with `{event_id, agent: "hazardmind-report", status, step: "report", data: {pdf_url, map_url, executive_summary, confidence_level, recommended_response_level}}`.

**`pipeline.run_report_pipeline`** — the end-to-end orchestrator:
1. Guards against unsafe contract-test side effects (blocks R2/DB writes in `--no-llm` mode unless explicitly allowed).
2. Sources context: DB fetch (`fetch_report_context_from_db`, requires a valid UUID) → or an incoming Band payload merged onto the built-in mock event → or pure mock.
3. `generator.generate_report` — LLM/intelligence generation (see below).
4. Resolves output paths (explicit → frontend demo mode → a per-event temp dir under `$REPORT_OUTPUT_DIR`).
5. `map_generator.generate_static_map` (Pillow) → `pdf_generator.generate_pdf_report` (ReportLab, embeds the map).
6. Uploads PDF + map PNG to R2 (per-artifact non-fatal); `map_url` points at the **public R2 image directly**, not a frontend route.
7. Writes `final_reports` (skipped safely, not fatally, for a non-UUID event_id).
8. Cleans up the local temp dir once R2 upload succeeds.
9. Returns `status: complete | complete_with_warnings | failed` — any exception anywhere collapses to a redacted `failed` result (`_safe_error_message` strips all known secret env values from the error text before it's ever logged or returned).

**`generator.py` strict-mode validation** — the project's "no silent fake success" philosophy is most visible here: `_assert_required_report_sections` raises `LLMGenerationError` if `detailed_report`/`technical_analysis`/`recommendations`/`executive_summary` fell back to deterministic template text in production (non-contract-test) mode. `_assert_live_intelligence_sources` similarly blocks completion if the anomaly-check section had to fall back with real anomalies present.

**`intelligence.py`** — 7 sections: `assess_event_criticality`, `detect_anomalies` (rule-based gate checked **before** any LLM call — if no rule-based finding exists, returns a clean result with zero LLM cost), `generate_map_narrative` (falls back to a genuinely metadata-derived, non-fabricated `cartographic_data_summary` on total LLM failure, never fake prose), `generate_priority_recommendations`, `generate_decision_brief` (the one section that calls AIML/Opus directly rather than the Featherless/Gemini cascade), `run_quality_check`, `generate_band_ready_message`.

**`llm_clients.py` model roster:** AIML (`claude-opus-4-8`, `gpt-5.5-2026-04-23` last resort), Featherless (Kimi-K2.6, gemma-4-31B-it, Qwen3.6-35B-A3B, DeepSeek-V4-Pro), Gemini (`gemini-3.1-flash-lite`, 5 chained keys). For most narrative calls **Gemini is effectively primary** — `call_featherless` tries Gemini first — because Featherless is the most congested provider for this agent specifically (many narrative sub-calls per report). The executive-summary generator is the one exception that still tries AIML first.

**`map_generator.py`** — despite CLAUDE.md/README claiming "Render map using MapLibre," this is **pure Pillow rasterization**: a hand-built equirectangular-ish projection, hazard zones colored/alpha-blended by severity, evacuation routes with arrows, facility markers, legend, scale bar, north arrow.

**`pdf_generator.py`** — ReportLab, multi-section: metadata, intelligence assessment table, priority timeline, anomalies, quality-check, detailed/technical analysis, recommendations, and a **model-source transparency note** stating exactly which provider/model generated each section.

**Note:** CLAUDE.md's Band Integration section describes an `AnthropicAdapter`, but the real `band_agent.py` uses `LangGraphAdapter` with Gemini/Featherless — stale documentation, same pattern as hazard's CLAUDE.md.

---

## 5. Database Schema

### 5.1 `shared/db/schema.sql` (checked-in DDL — see drift warning below)

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE disaster_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    disaster_type VARCHAR(50),
    location VARCHAR(200),
    bbox FLOAT[],
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE satellite_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES disaster_events(event_id),
    image_url TEXT,
    affected_area_km2 FLOAT,
    land_cover TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE hazard_zones (                       -- one row per hazard_type per event
    id SERIAL PRIMARY KEY,
    event_id UUID REFERENCES disaster_events(event_id),
    geometry GEOMETRY(POLYGON, 4326),              -- PostGIS spatial column
    risk_level TEXT,
    hazard_type TEXT,                              -- 'flood' | 'earthquake' | 'landslide'
    area_km2 FLOAT,
    severity TEXT,
    confirmed_by JSONB,
    flood_depth_estimate TEXT,
    earthquake_mmi FLOAT,
    landslide_probability TEXT,
    overall_confidence FLOAT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- idx_hazard_zones_event (event_id)
-- idx_hazard_zones_geometry USING GIST(geometry)   -- spatial index
-- UNIQUE idx_hazard_zones_event_type (event_id, hazard_type)

CREATE TABLE impact_data (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES disaster_events(event_id),
    population_affected INTEGER,
    hospitals_at_risk INTEGER,
    roads_blocked_km FLOAT,
    schools_affected INTEGER,
    vulnerability_score FLOAT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE final_reports (
    id SERIAL PRIMARY KEY,
    event_id UUID REFERENCES disaster_events(event_id),
    pdf_url TEXT,
    map_url TEXT,
    executive_summary TEXT,
    agent_log JSONB,
    total_time_seconds INT,
    confidence_level TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- idx_final_reports_event (event_id)
```

### 5.2 Relationships
`disaster_events.event_id` (UUID PK) is referenced by all four child tables. `hazard_zones` is one-to-many per event (one row per hazard type, unique on `(event_id, hazard_type)`); every other child table is effectively one-to-one per event (upserted).

### 5.3 PostGIS spatial fields
- `hazard_zones.geometry GEOMETRY(POLYGON, 4326)` — the only genuinely spatial-typed column, GIST-indexed.
- All other "geometry" (satellite zones, boundaries, evacuation routes) is stored as **GeoJSON inside JSONB/TEXT columns**, not native PostGIS types — full boundary polygons live on R2 (`zones.geojson`) or are reconstructed via `ST_AsGeoJSON(geometry)` on read (`db_client.py`'s `_fetch_hazard_zones`), not stored redundantly in Postgres beyond the one `hazard_zones.geometry` column.

### 5.4 ⚠️ Schema drift — do not treat `schema.sql` as authoritative

Confirmed by direct comparison of `schema.sql` against what the live code actually reads/writes:

1. **`disaster_events`** in `schema.sql` has no `status`, `step`, `progress`, `magnitude`, or `updated_at` columns — yet `backend/db.py` reads and writes all of them on every request. Only `band_room_id` is self-migrated at runtime (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`); the rest must already exist on the live Neon database via an out-of-band migration never captured in this file.
2. **`satellite_results`** in `schema.sql` has `image_url, affected_area_km2, land_cover` — but `backend/db.py`'s `insert_satellite_result` writes `satellite_type, cloud_cover, scene_id, true_color_url, index_url, classification_url, geojson_url, affected_area_km2, damage_percent, total_zones, bounds, bbox, risk_cities`. Only `affected_area_km2` overlaps. This is the most significant drift in the codebase — `shared/models/types.py`'s `SatelliteData` dataclass matches the *old* schema shape, confirming `schema.sql`/`types.py` reflect an earlier design that `backend/db.py` has since evolved past.
3. **`impact_data`** — the impact agent's own `services/db.py` DDL (`event_id TEXT UNIQUE`, `total_affected`, `high_risk_people`, `medium_risk_people`, `hospitals_at_risk`, `schools_at_risk`, `roads_blocked` INTEGER, `bridges_at_risk`, `vulnerability_score TEXT`, `evacuation_routes JSONB`, `estimated_evacuation_time TEXT`) differs from both `schema.sql`'s version AND lacks the `overall_confidence` column that `agents/report/db_client.py` expects to read. Impact's own CLAUDE.md explicitly flags this as stale.

**Practical implication:** the live Neon schema is the actual source of truth; treat every DDL/dataclass file in this repo as a historical snapshot, not a current spec.

---

## 6. API Endpoints (Backend)

| Method | Path | Request | Response | Notes |
|---|---|---|---|---|
| `GET` | `/health` | — | `{status, service, band: connected\|disconnected, db: connected\|disconnected, version}` | |
| `POST` | `/analyze` | `{location: str, disaster_type: str, magnitude?: float}` | `{job_id, status, message, band_room_id?}` | Always 200, even on dispatch failure (`status:"failed"` in body). Concurrency-capped (`MAX_CONCURRENT_EVENTS`, default 2). |
| `GET` | `/status/{job_id}` | — | `{job_id, status, step, progress: 0-100, created_at, updated_at}` | 404 if unknown. |
| `GET` | `/results/{job_id}` | — | Complete: `{job_id, status:"complete", satellite?, hazard?, impact?, report?}` (200). Processing: `{status:"processing", step, message}` (**202**). | 404 if unknown. |
| `GET` | `/band-log/{job_id}` | — | `{job_id, messages: [{agent, content, timestamp, type}]}` | Reads the in-memory `inbound_store`, not Band REST history (which is empty for this agent). 404 if unknown. |

Frontend polling contract (from `frontend/lib/analyze.ts`): `POST /analyze` → poll `/status/{job_id}` + `/band-log/{job_id}` every 2.5s (up to 40 min) → on completion, `GET /results/{job_id}` (treating HTTP 202 as "still processing," retried every 4s).

---

## 7. LLM Provider Routing (cross-cutting)

Every agent (and the shared `llm_fallback.py`) implements a variant of the same pattern: **Featherless-primary, criticality-based escalation, with Gemini now effectively the primary escalation tier** because the AIML account ran out of funds (this is documented explicitly in `agents/impact/services/llm_router.py`'s module comment and mirrored by `PREFER_GEMINI_ESCALATION` defaulting true across services). See prior memory: `[[llm-provider-budgets]]` `[[aiml-band-gpt-routing]]`.

| Provider | Role | Models seen in code | Notes |
|---|---|---|---|
| **Featherless** | Primary, cheap, high-volume | `google/gemma-4-31B-it`, `moonshotai/Kimi-K2.6`, `Qwen/Qwen3.6-35B-A3B`, `deepseek-ai/DeepSeek-V4-Pro` | Shared 4-concurrency-unit cap across ALL pipeline agents (Kimi costs 4 units — a single Kimi call can exhaust the cap). 32k context ceiling — large payloads (e.g. full boundary geometry) must be stripped before an agent's Band-adapter LLM turn. |
| **Gemini** | Now effectively primary escalation (AIML is out of funds) | `gemini-3.1-flash-lite`, `gemini-2.5-flash`, `gemini-3.5-flash` (varies by agent/env) | Multi-key rotation (`GEMINI_API_KEY`, `_2`..`_5`) to raise effective free-tier quota. 1M token context — used specifically to dodge Featherless's 32k ceiling on large prompts. |
| **AIML (Claude Opus)** | Secondary/last-tier escalation | `claude-opus-4-8` | Via AIML's Anthropic-protocol endpoint; must omit `temperature` (AIML rejects it for this model, HTTP 400 otherwise). |
| **AIML (GPT)** | Absolute last resort | `gpt-5.5-2026-04-23` (`GPT_FALLBACK_MODEL` env) | Only reached if Opus throws/times out. |

Each agent's specific routing table differs slightly (see per-agent sections above) but the shape is consistent: `low`/routine → Featherless only; `normal` → Featherless with confidence-gated escalation; `high`/`critical` → escalate first, verify/combine with a Featherless pass for `critical`. **Every fallback function returns `None`/a deterministic default on total failure rather than raising** (with the narrow exception of the older/superseded `shared/utils/band_client.py` and `agents/impact/services/featherless.py`, both of which do raise — confirmed unused by current live code paths).

---

## 8. Frontend (`frontend/`)

Next.js 14 (App Router) + React 18 + TypeScript, Tailwind v4, Mapbox GL JS (globe projection). Two routes:

- **`/` → `DashboardShell`** — the live "Command Center": idle spinning 3D globe → user query → `POST /analyze` → polls `/status` + `/band-log` → `HazardMap` flies to the event bbox and reveals overlays only once `resultReady` is true (decoupling data-arrival from camera-focus prevents a premature fly-to on a partial mid-run update). `AgentPanel`'s 5-step pipeline UI is driven by which agents have actually spoken in the Band room, not a timer.
- **`/map/[eventId]` → `MapSnapshotView`** — a read-only, shareable permalink for one completed event; **bug**: never passes `focus={true}` to `HazardMap`, so this page likely never leaves the idle spinning-globe state despite its evident purpose.

**Key libs:** `lib/analyze.ts` (query parsing + orchestration polling), `lib/loadHazardResult.ts` (511-line defensive backend-response normalizer — adapts multiple possible response envelope shapes onto the strict `HazardMindResult` type, with an all-blank `emptyResult` fallback that deliberately never shows stale demo data), `lib/bandLog.ts` (normalizes/dedupes the live Band chat feed). `app/api/r2/route.ts` is a CORS-bypass proxy for the public R2 bucket, hardcoded to one allowlisted hostname.

**Map rendering (`HazardMap.tsx`):** Mapbox GL globe with idle auto-spin, 3 raster PNG overlays (true-color/index/classification, `updateImage` in place when the result changes), a heatmap layer (zone-centroid weighted by severity), zone fill/line layers colored by severity, evacuation-route lines, and custom DOM facility markers. Camera movement is isolated to a single `focus`-keyed effect.

### 8.1 Significant dead code (frontend)

Roughly **half of `components/`** belongs to a superseded "dial/module-focused" dashboard design and is not reachable from any live route: `Map.tsx` (legacy MapLibre stub — superseded by Mapbox in `HazardMap.tsx`), `AgentLog.tsx` (empty stub, no data binding), `AgentTimeline.tsx`, `AgentNetwork.tsx`, `SelectedModulePanel.tsx`, `DialFocusedPanel.tsx`, `ControlPanel.tsx`, `MapLibre3DStage.tsx`.

**Most notable:** `IntelligencePanel.tsx` — the only component that renders the backend's rich `intelligence`/`model_sources` "Super Brain" block (criticality, anomalies, map narrative, priority timeline, decision brief, quality check, Band-ready message) — is **fully implemented, fully typed, and completely unwired** from `DashboardShell`. The backend/report-agent intelligence layer documented in §4.4 has essentially no UI surface in the live app today.

Also unused: `lib/cityBoundary.ts` (a standalone geoBoundaries-fetching utility, superseded by the backend returning `boundaries.region_boundary` directly), `lib/sampleResult.ts`'s `sampleResult` export (the real captured Rawalpindi demo dataset — only `emptyResult` is actually imported anywhere).

---

## 9. Shared (`shared/`)

- **`shared/db/schema.sql`** — see §5, drifted from the live DB.
- **`shared/models/types.py`** — dataclasses (`DisasterEvent`, `BandMessage`, `SatelliteData`, `HazardData`, `ImpactData`, `ReportData`) mirroring the *original* schema design, similarly drifted from `backend/db.py`'s actual columns.
- **`shared/utils/band_client.py`** — a thin, older `.env`-loading + basic `call_with_fallback` LLM helper. **Superseded** by `backend/band_client.py` (Band messaging) and `shared/utils/llm_fallback.py` (LLM routing) — this module's `call_with_fallback` **raises** on total failure (unlike every other fallback function in the codebase, which returns `None`), a latent inconsistency if anything still depends on it.
- **`shared/utils/llm_fallback.py`** — the closest thing to a canonical shared LLM router: 4-link chain Featherless → Gemini → Claude(AIML) → GPT(AIML), criticality-routed (`critical`→GPT direct, `low`→Featherless only, else full chain), all links non-raising.

---

## 10. Environment Variables (complete reference)

### Database & Storage
| Variable | Used by | Purpose |
|---|---|---|
| `NEON_DATABASE_URL` | all services | Postgres/PostGIS connection string (asyncpg) |
| `CLOUDFLARE_R2_KEY` / `CLOUDFLARE_R2_SECRET` | satellite, report | R2 (S3-compatible) credentials |
| `CLOUDFLARE_R2_BUCKET` | satellite, report | bucket name (default `hazardmind-storage`) |
| `CLOUDFLARE_ACCOUNT_ID` | satellite, report | derives the R2 endpoint if `CLOUDFLARE_R2_ENDPOINT` unset |
| `CLOUDFLARE_R2_ENDPOINT` | report | explicit R2 endpoint override |
| `CLOUDFLARE_R2_PUBLIC_URL` / `CLOUDFLARE_R2_PUBLIC` / `R2_PUBLIC_BASE_URL` | all (naming varies per service) | public bucket base URL for artifact links |

### Band SDK (per-agent)
| Variable | Used by | Purpose |
|---|---|---|
| `BAND_AGENT_ID`, `BAND_API_KEY` | every service | this service's own Band identity |
| `THENVOI_REST_URL` (default `https://app.band.ai/`), `THENVOI_WS_URL` | every service | Band REST/WebSocket endpoints |
| `BAND_ROOM_ID` | backend, impact, report | static shared room fallback |
| `DYNAMIC_BAND_ROOMS` | backend | gates per-event room creation (see §3) |
| `SATELLITE_AGENT_ID`, `HAZARD_AGENT_ID`, `IMPACT_AGENT_ID`, `REPORT_AGENT_ID` | backend | mention-target agent ids |
| `ORCHESTRATOR_AGENT_ID` | satellite, impact, report | mention target for completion posts |
| `SATELLITE_BAND_API_KEY`, `HAZARD_BAND_API_KEY`, `IMPACT_BAND_API_KEY`, `REPORT_BAND_API_KEY` | backend (`cleanup.py`) | per-agent keys for fleet-wide backlog drain |
| `BAND_ADAPTER_MODEL`, `BAND_ADAPTER_FALLBACK_MODEL` | satellite, hazard, impact | LangGraph adapter's LLM model overrides |
| `ORCHESTRATOR_MODEL` | backend | Claude adapter model id |
| `IMPACT_FORCE_ASSESS` | impact | bypasses the no-significant-disaster gate |
| `USE_MOCK_BAND` | impact | `main.py` local test mode |

### LLM Providers
| Variable | Used by | Purpose |
|---|---|---|
| `FEATHERLESS_API_KEY`, `FEATHERLESS_BASE_URL` | all agents | Featherless (primary provider) |
| `GEMINI_API_KEY` (+ `_2` … `_5`) | all agents | multi-key Gemini rotation |
| `GEMINI_MODEL` / `SATELLITE_GEMINI_MODEL` / `HAZARD_GEMINI_MODEL` / `REPORT_GEMINI_MODEL` / `GEMINI_ESCALATION_MODEL` | per-agent | model id overrides |
| `PREFER_GEMINI_ESCALATION` | impact | Gemini-before-AIML-Opus toggle (default true) |
| `AIML_API_KEY` | all agents | AIML (Claude Opus + GPT) escalation tier |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` | backend, shared | Anthropic-protocol AIML endpoint |
| `OPENAI_BASE_URL` / `AIML_BASE_URL` | backend, shared, report | OpenAI-protocol AIML endpoint |
| `GPT_FALLBACK_MODEL` (default `gpt-5.5-2026-04-23`) | all agents | AIML GPT last-resort model |
| `CLAUDE_FALLBACK_MODEL` | shared | `llm_fallback.py` Claude model id |
| `REPORT_LLM_TIMEOUT_SECONDS` | report | LLM call timeout cap |
| `REPORT_OUTPUT_DIR` | report | local temp/output directory root |
| `REPORT_BAND_MODEL` / `ANTHROPIC_MODEL` | report | **vestigial** — not read by the current Gemini/Featherless-only adapter |

### External data sources
| Variable | Used by | Purpose |
|---|---|---|
| `COPERNICUS_USERNAME`, `COPERNICUS_PASSWORD` | satellite | CDSE OAuth2 credentials |
| `GDACS_GEOJSON_URL`, `USGS_QUERY_URL` (satellite) / `GDACS_API`, `USGS_API` (hazard) | satellite, hazard | overridable feed URLs (blank = built-in default) |
| `GEONAMES_USERNAME` | impact | GeoNames population lookups (default varies: `.env.example` says `ahanan.24`, README says `hazardmind` — inconsistent) |

### App / infra
| Variable | Used by | Purpose |
|---|---|---|
| `ALLOWED_ORIGINS` | backend | CORS allowlist |
| `MAX_CONCURRENT_EVENTS` (default 2) | backend | `/analyze` concurrency cap |
| `CLEANUP_INTERVAL_HOURS` (default 12) | backend | cleanup loop interval |
| `PORT` (7860 on HF Spaces) | all services | HTTP listen port |
| `SATELLITE_KEEP_SCENE_CACHE` | satellite | keep downloaded `.zip` cache across runs (dev only) |
| `FRONTEND_BASE_URL` / `NEXT_PUBLIC_FRONTEND_URL` | report | (declared; not confirmed read in reviewed report code) |
| `NEXT_PUBLIC_API_URL` | frontend | backend base URL; unset → frontend runs in demo mode |
| `NEXT_PUBLIC_MAP_STYLE_URL` | frontend | only affects the **dead** `Map.tsx` (MapLibre) component |
| `NEXT_PUBLIC_MAPBOX_TOKEN` | frontend | required by the **actually-used** `HazardMap.tsx` (Mapbox) — **missing from `.env.example`**, a real gap |

---

## 11. Known Issues / TODOs / Documentation Drift

### Schema & contract mismatches
- **`shared/db/schema.sql` is stale** relative to the live Neon database on `disaster_events` and `satellite_results` (see §5.4) — do not trust it as a spec.
- **`impact_data` schema mismatch**: impact's own DDL lacks `overall_confidence`, which `report/db_client.py` selects — acknowledged by impact's CLAUDE.md as known-stale.
- **`GEONAMES_USERNAME` default inconsistency** between impact's `.env.example` (`ahanan.24`) and README (`hazardmind`).
- **`DYNAMIC_BAND_ROOMS` default mismatch**: `.env.example` documents `true` as intended; `router.py`'s code fallback is `"false"`.

### Stale CLAUDE.md files
- `agents/hazard/CLAUDE.md` — "Setup progress" checklist mostly unchecked despite the pipeline being fully implemented; also says "Anthropic adapter" when the code uses `LangGraphAdapter`.
- `agents/report/CLAUDE.md` — describes an `AnthropicAdapter` Band integration (code uses `LangGraphAdapter`/Gemini) and "Render map using MapLibre" (actual implementation is pure Pillow rasterization); checklist also stale.
- By contrast, `agents/satellite/CLAUDE.md` is exemplary — actively maintained, documents real production incidents in detail.

### Unimplemented / dormant features
- **Hazard agent never generates `risk_polygons`** despite CLAUDE.md/README describing "risk polygons in PostGIS" as a responsibility — `analyzer.py` always returns `{}`.
- **`StanceEngine`** (satellite) — fully implemented, unit-tested, never called from `agent.py`.
- **`intelligence.decide_landsat_fallback`** (satellite) — implemented, never called.
- **Per-city artifact rendering** (satellite `_render_per_city`) — implemented, deliberately disabled by default.
- **`services/criticality.py`** (impact) and **`services/featherless.py`** (impact) — defined but not used by the live task modules, which reimplement the logic inline or use `llm_router.py` instead.
- **`shared/utils/band_client.py`** — superseded by `backend/band_client.py` + `shared/utils/llm_fallback.py`; its `call_with_fallback` raises on failure, inconsistent with every other fallback function in the codebase.

### Frontend
- `MapSnapshotView` never passes `focus={true}` to `HazardMap` — the dedicated per-event map page likely never flies to the event area, appearing to be an oversight.
- `IntelligencePanel.tsx` (and the backend's full `intelligence`/`model_sources` data) has **zero UI exposure** in the live dashboard — either needs to be wired back in or the dead component tree removed.
- `ReportActions.tsx`'s "Final Package Pending" button is a permanently-disabled stub.
- `AgentLog.tsx` is an empty shell with no data binding.
- Two mapping libraries (`mapbox-gl`, `maplibre-gl`) are both dependencies; only Mapbox is actually used (MapLibre only by the dead `Map.tsx`).
- No request cancellation between successive `DashboardShell` query submissions (each targets an independent `job_id`, so not functionally broken, just wasteful).

### Design decisions that look like bugs but are intentional (documented in CLAUDE.md)
- CDSE never honors HTTP Range — true byte-resume is impossible against that provider; mitigated via per-band (not per-archive) download granularity.
- Broad `except Exception` is pervasive by design across all agents ("a missing cross-check must never block a life-critical handoff") — failure modes degrade silently to conservative defaults rather than crashing, which is deliberate but means many partial failures are invisible without reading logs.

---

## 12. Enterprise / Production-Readiness Gaps

**Security**
- Multiple `agent_config.yaml` files contain **live Band API keys in plaintext** committed to (or at least present in) the working tree (`agents/hazard/agent_config.yaml` confirmed populated with real-looking values; satellite's is empty, others not fully verified) — these should be generated at deploy time only (as `backend/entrypoint.sh` already does) and never checked in.
- `ALLOWED_ORIGINS` defaults to `"*"` (open CORS) if unset.
- No authentication/authorization on any backend endpoint — `/analyze` is fully open, meaning anyone can trigger paid LLM/satellite-download work. There is a concurrency cap but no rate-limiting per caller/IP, and no API key requirement for the public API.
- Secrets are redacted from *report-agent* error messages (`_safe_error_message`) but this pattern is not applied consistently across all services (e.g. backend's own error paths).

**Scalability**
- The satellite agent's pipeline is single-threaded per event with real disk I/O and multi-GB memory use on large mosaics (documented: Mindanao run needed ~8GB); horizontal scaling would require careful per-instance resource sizing (DEPLOY_GCP.md recommends 16GB RAM for this service alone).
- Featherless's shared 4-concurrency-unit cap across *all* pipeline agents is a hard ceiling on total system throughput — two concurrent pipeline runs can already 429 each other if both need Kimi (4 units) simultaneously; `MAX_CONCURRENT_EVENTS=2` in the backend is a direct mitigation, not a fix.
- No queueing/backpressure system beyond the simple polling-based concurrency gate — a burst of requests beyond the cap just busy-waits up to 8 minutes before proceeding anyway.
- The static shared `BAND_ROOM_ID` (dynamic per-event rooms gated off) means all events currently share one Band room transcript — no cross-event isolation of the chat log at the Band layer (mitigated at the app layer by filtering on `event_id`).

**Monitoring / Observability**
- No structured metrics/tracing (no Prometheus, OpenTelemetry, or equivalent) — the only signal is Python `logging` output per container.
- `/health` only checks Band connectivity + a DB ping — no deeper liveness signal (e.g. whether the pipeline is actually processing, queue depth, last-successful-event timestamp).
- No alerting integration — anomalies are surfaced only as Band chat messages, not to any on-call system.
- No centralized log aggregation configured in-repo (each service logs to stdout independently on its own Hugging Face Space / GCP VM).

**Testing**
- Most "test suites" (satellite, report) are standalone scripts run manually, not integrated into CI — no evidence of a CI pipeline (no `.github/workflows` observed in the tracked file list).
- Several tests are explicitly **live** (hit real Neon, real Band, real CDSE/R2) rather than mocked, making them unsuitable for automated CI gating without dedicated test credentials/environments.

**Data & compliance**
- No documented data-retention policy for R2 artifacts (PDFs/PNGs/GeoJSON) or Neon rows — events appear to accumulate indefinitely aside from the 30-minute stuck-event cleanup (which only changes `status`, doesn't delete data).
- No evident PII-handling policy, though the system's population/impact data is aggregate (city-level), not individual-level, which reduces but doesn't eliminate this concern for a production disaster-response deployment.

**Deployment**
- `DEPLOY_GCP.md` documents a manual, per-VM Docker deployment (`gcloud compute instances create-with-container`) with `.env` passed via `--container-env` — acceptable for a demo/hackathon deployment but not production-grade (no auto-scaling, no rolling updates, no secret manager integration by default, though the doc does note Secret Manager as a "for production" recommendation it doesn't implement).
- No infrastructure-as-code (Terraform/Pulumi) — deployment is a sequence of manual `gcloud`/`docker` commands.
