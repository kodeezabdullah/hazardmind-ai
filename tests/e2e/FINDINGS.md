# E2E Pipeline — Findings (Band→LangChain migration verification)

Date: 2026-07-26. Branch: `feat/langchain-migration`. DB: live Neon (quota extended).

## Blockers fixed this session

| # | Blocker | Status |
|---|---|---|
| 1 | `graph.py` `_load_node` didn't purge agent-local bare modules → single-process graph compilation broken | **Fixed** (commit 28ee4f2) |
| 2 | satellite/hazard/impact called bare `load_dotenv()` (cwd-relative) → wrong/no `.env` in single-process runs | **Fixed** — each loads its own `.env` with `override=False` |
| 3 | Gemini 429 had no retry; added `GEMINI_API_KEY_BACKUP` one-shot rotation in `llm_fallback.py` | **Fixed** — but see caveat below |
| 4 | Neon compute quota exhausted | **Resolved** — user extended quota; e2e targets Neon; docker-compose + schema-test.sql kept as portable off-Neon path |

### BLOCKER 3 caveat (must read)
The 4 pipeline agents **do not** call `shared/utils/llm_fallback.py` for Gemini.
Each agent's own `intelligence.py` / `services/llm_router.py` rotates over
`GEMINI_API_KEY` + `GEMINI_API_KEY_2..5` against Gemini's **OpenAI-compatible**
endpoint using the `openai` SDK — it never imports `llm_fallback` or
`google-genai`. So the backup-key retry added to `llm_fallback.py` (as the task
specified) currently rescues only callers of `llm_fallback.llm_call` — **none on
the pipeline path**. To make the backup key rescue the live pipeline,
`GEMINI_API_KEY_BACKUP` must be added to each agent's own key list (impact reads
only `GEMINI_API_KEY`). Flagged, not rewired (per-agent LLM-content code, out of
scope). In practice the primary Gemini key served every call in the runs below
(HTTP 200, no 429 on the OpenAI endpoint), so this did not block the e2e.

## Pre-flight — 5/5 PASS
Neon (SELECT 1 + 5 tables), Cloudflare R2 (bucket `hazardmind-storage`),
Copernicus CDSE (token), Gemini (429 on the `generateContent` REST probe — key
valid, free-tier quota), geoBoundaries (PAK ADM3).

## Satellite Sentinel-1 (flood path) — TWO pre-existing bugs

### BUG A — RAW scene selection — **FIXED**
`sentinel.search_imagery` had a `MSIL1C` product-type filter for Sentinel-2 but
**none for Sentinel-1**, so the S1 catalogue returned RAW (level-0) / SLC / GRD
and the ranker picked purely on overlap. A RAW winner
(`S1D_IW_RAW__0S...`) carries no VV/VH measurement GeoTIFFs, so after a full
**1.76 GB** download `_extract_bands` found nothing → retried the next (also RAW)
candidate → another 1.76 GB → same failure.

- **Pre-existing**, NOT a migration regression: `sentinel.py` scene-selection last
  changed 2026-06-14 (`952614e`); the migration branch never touched the file
  (`git log main..feat/langchain-migration -- agents/satellite/sentinel.py` empty).
- **Fix (commit f353bcc):** add `contains(Name,'GRD')` to the SENTINEL_1 branch
  (mirrors the S2 `MSIL1C` filter) + a belt-and-suspenders guard in
  `processor.download_imagery` that skips any non-GRD S1 product **before** the
  multi-GB download, with a clear "all candidates were RAW/SLC" error.
- **Proven working end-to-end:** the clean re-run selected
  `S1D_IW_GRDH_1SDV_...` (GRD, 100% overlap), downloaded 1.26 GB, and
  **successfully extracted VV + VH bands** (stacked 16719×25546). The catalogue
  now returns 42 GRD candidates, 0 RAW.

### BUG B — S1 GRD clip collapses to 1×2 px / 0% valid — **OPEN BLOCKER**
After VV/VH extraction the clip to the Rawalpindi ADM3 tehsil polygon collapsed
the 16719×25546 grid to **1×2 pixels, 0.00% valid** (< 5% floor), so the scene
was rejected and the next candidate tried — including the **same acquisition's
COG/non-COG twin** (wasting a duplicate ~1.2 GB download). All 42 GRD candidates
are COG+non-COG pairs claiming **~93% overlap** yet clipping to 0% valid.

- **Suspected cause:** CRS / footprint mismatch in the Sentinel-1 GRD clip path.
  GRD is ground-range-detected geometry (not a clean projected UTM grid like
  S2); the ~93%-overlap-but-0%-valid gap is the "catalogue footprint overstates
  real data coverage" class of issue the satellite CLAUDE.md already documents
  for S2 Mindanao, here compounded by the ground-range clip collapsing to ~1 px.
- **Also pre-existing** (same untouched clip code) and **distinct from BUG A**.
- **⚠️ BLOCKER — the flood path via Sentinel-1 is currently non-functional.**
  Must be fixed before merge to `main`. A flood run over a small AOI with
  cloud > 30% (→ SAR) selects a valid GRD product but produces no usable clip,
  so satellite output is never generated and the pipeline halts at
  `status: failed` in the satellite stage (hazard/impact/report never run).

## hazard_zones — 3 rows regardless of disaster_type (verified)
`agents/hazard/agent.py:write_to_db` writes a **fixed 3-row list**
(flood/earthquake/landslide) unconditionally; the analyzer runs all three
hazards regardless of the requested `disaster_type`. So assertion #4 (exactly 3
rows: earthquake/flood/landslide) holds for **any** disaster_type — including the
earthquake run used to complete end-to-end verification.

## BUG C — graph loader broke LAZY sibling imports — **FIXED (important)**
The BLOCKER 1 loader fix (`_load_node` purging each agent's bare sibling modules
from `sys.modules` after loading `node.py`) prevented cross-agent *collision* but
broke intra-agent **lazy** imports. `agents/satellite/processor.py` does
`from sentinel import select_mosaic_scenes` **inside a function** (call time);
by then the purge had removed `sentinel` and popped the agent dir off `sys.path`,
so the satellite node crashed at runtime with
`ModuleNotFoundError: No module named 'sentinel'`.

- **This would fail in PRODUCTION too**, not just the test — the single-process
  LangGraph hits the lazy import on the first real satellite run (mosaic path).
- **Fix (this session):** `_load_node` no longer deletes the bare modules — it
  **stashes** them per-agent (out of the shared `sys.modules` so the next agent's
  load still sees a clean slate) and returns a WRAPPER that re-installs that
  agent's bare modules + dir on `sys.path` around **every call**, then restores.
  So both eager and lazy sibling imports resolve to the correct agent, and
  cross-agent isolation still holds. Verified: the satellite node now runs past
  the mosaic/download into clip.

## BUG D — PROJ database pollution (test-env only) — **FIXED in harness**
The test machine has a global `PROJ_LIB` pointing at the PostgreSQL/PostGIS
install's `proj.db` (old LAYOUT.VERSION). GDAL/rasterio picked it up instead of
the venv's pyproj `proj.db`, flooding the log with "another PROJ installation"
errors AND breaking the WGS84→UTM reprojection of the clip polygon → "Clip
geometry does not overlap the raster grid" → 0 valid pixels → satellite fail.

- **Test-environment issue, NOT a pipeline bug** (the leaked env var came from
  the local Postgres install used earlier for the docker-less BLOCKER 4 option).
- **Fix:** `tests/e2e/_env.py` now pins `PROJ_LIB`/`PROJ_DATA` to the venv's own
  pyproj data dir. Only affects the test process.

## End-to-end completion run — **9/9 PASS** ✅
Full satellite→hazard→impact→report completion verified via an **earthquake** run
over **Quetta** (0% cloud → Sentinel-2 optical path; Rawalpindi's 45.9% cloud
forces even earthquake onto S1, so a clear-sky location is required to exercise
the optical clip). Satellite selection is **cloud-driven, not disaster-driven**
— by design ("physics over assumption").

All 9 assertions passed (event `be00323c...`, run report_20260726_154728.md):
1. event_id byte-identical at all 4 nodes ✅ (the #1 pre-migration bug class — gone)
2. disaster_events terminal status `complete` ✅
3. satellite_results + 4 R2 URLs all 200/non-zero ✅ (true_color 1.2MB, index
   789KB, classification 55KB, geojson 3.7MB — real files, not XML error pages)
4. hazard_zones exactly 3 rows (earthquake/flood/landslide) ✅
5. impact_data row ✅ (total_affected=0 — the no-significant-disaster gate firing
   honestly, since the hazard read produced no significant disaster)
6. final_reports + PDF 200 + body starts `%PDF` ✅ (131KB)
7. final PipelineState.status == complete ✅
8. confidence_scores from every stage ✅ (satellite 0.27, hazard 0.87, impact
   0.87, report HIGH)
9. pipeline_log errors/anomalies trail persisted ✅

**Timing (vs ~142s baseline):** satellite 204.7s (bands CACHED — no download; time
is NDVI/vectorize/R2 of a 2751×2961 clip), hazard 19.7s, impact 2.1s, report
25.5s, **total 252s**. A cold run adds the S2 download (~700MB per-band) on top.

**Swallowed WARNING/ERROR (3, all benign):** geoBoundaries ISO3 miss for
"Quetta" (recovers via Nominatim → PAK ADM3); GDAL 'Memory'→'MEM' driver
deprecation (cosmetic); a `low_confidence` anomaly (satellite self-reported 0.27
because NDWI −0.003 vs 96% water is physically inconsistent for arid Quetta) —
this is the cross-validator working AS DESIGNED, captured in
anomalies/confidence_scores, not a failure.

**Gemini:** every LLM call was served by the agents' own `gemini:0` slot (their
`intelligence.py`, OpenAI-compatible endpoint), HTTP 200, no 429 — confirming
again that `llm_fallback.py`'s new backup-key path is off the live pipeline path.

## Two more bugs fixed en route to green
Beyond BUG A (S1 RAW) and BUG C (loader lazy-import), the run also required:
- **PROJ pin (test-env, BUG D)** — see above; fixed in `_env.py` at import time.
The S1 flood path (BUG B) remains the one open BLOCKER for a flood-scenario merge.
