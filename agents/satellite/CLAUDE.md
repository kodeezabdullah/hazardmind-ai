# Satellite Agent

Processes Copernicus/Sentinel satellite imagery for disaster zones.

## Responsibilities
- Fetch satellite imagery for given bbox
- Detect affected area and land cover
- Upload processed imagery to Cloudflare R2
- Publish results via Band SDK

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
  under the `satellite_agent` key. Runtime values are loaded from `.env`.

### Required env vars (see `.env.example`)
- `THENVOI_REST_URL` — Band REST URL (`https://app.band.ai/`)
- `THENVOI_WS_URL` — Band WebSocket URL
- `BAND_AGENT_ID` — agent UUID from the Band platform
- `BAND_API_KEY` — agent API key from the Band platform
- `ANTHROPIC_API_KEY` — provider key for the Anthropic adapter

### Setup progress
- [x] Install Band SDK (`band-sdk[anthropic]`) and pin in `requirements.txt`
- [x] Add Band env vars to `.env.example`
- [x] Create `agent_config.yaml` (gitignored)
- [x] Add `verify_setup.py` connectivity check
- [x] Register agent on Band platform and fill in real credentials
- [x] Run `verify_setup.py` against live Band to confirm connection (connects as "HazardMind Satellite")
- [x] Implement imagery processing pipeline (`processor.py`)
- [x] Wire agent into Band rooms and publish results (`agent.py`)

## Core Logic

### Step 14: Intelligent expert layer — confidence, cross-validation, stance — DONE

The agent is no longer a pipeline that emits whatever it computed — it is an
**expert that evaluates its own confidence, cross-validates every source, and
forms (and defends) evidence-based positions.** Three new self-contained
modules, wired into `run_pipeline`. Branch: **main**.

**`event_id`.** Still parsed from the orchestrator's message and threaded
through unchanged — the agent **never generates its own**. (The Band tool flow
receives `event_id` as a required tool arg; an absent one means the model should
ask the orchestrator to resend.)

**`confidence_tracker.py` — `ConfidenceTracker`.** A pure, no-I/O running ledger
for one event. `add_evidence(source, value∈[0,1], weight>0)` and
`add_concern(text, severity∈{LOW,MEDIUM,HIGH,CRITICAL})` accumulate;
`overall_confidence()` is the weighted average of evidence **minus** a per-
concern penalty (LOW .05 / MEDIUM .10 / HIGH .20 / CRITICAL .35), clamped to
[0,1] (0.0 when there is no evidence yet). Thresholds drive behaviour:
`needs_verification()` (< 0.70 → ask the orchestrator before handing off) and
`should_alert_team()` (any CRITICAL concern → alert the room). Inputs are coerced
/ clamped and an unknown severity degrades to MEDIUM, so a bad value never
crashes the ledger. `get_report()` is a compact JSON snapshot.

**`cross_validator.py` — `CrossValidator`.** `validate_all(satellite_result,
disaster_type, location, tracker)` checks the result against **every reachable
source** and feeds the tracker, returning human-readable findings
(`{source,status,detail}`):
1. **GDACS** (`check_gdacs`, public `geteventlist/EVENTS4APP` GeoJSON) — nearest
   event within 250 km of the AOI centroid; compares affected-area extent:
   ratio 0.7–1.3 → CONFIRMED (evidence .9); > 2.0 → HIGH concern "secondary
   flooding?"; < 0.5 → HIGH concern "cloud masking?"; in-between → PARTIAL; event
   present but no area → weak EVENT_PRESENT corroboration.
2. **USGS** (`check_usgs`, FDSN `event/1/query`) — earthquakes only; strongest
   quake within 250 km / 14 days; M > 6.5 → HIGH concern (expect wider damage).
3. **Cloud cover** — > 60% → CRITICAL (optical unreliable); > 30% → MEDIUM; else
   strong evidence.
4. **Index physics** (flood) — NDWI < 0 while GDACS is RED → CRITICAL
   CONTRADICTION (cloud interference); NDWI > 0.3 & water > 20% → strong; etc.
5. **Coverage** — valid-pixel share < 60% → HIGH concern (incomplete picture).
6. **Featherless expert opinion** (`get_featherless_opinion`) — routes a "senior
   remote-sensing expert" prompt through the existing `SatelliteIntelligence`
   chain (Qwen-pinned) for `{reliable,confidence,concerns,alert_team,
   recommendation}`; its confidence is added as weighted evidence.
   **Every external call is best-effort** — an unreachable feed or LLM is logged
   and skipped (no evidence, no crash), so a missing cross-check never blocks a
   life-critical handoff. The geographic feeds accept a `(lat,lon)` pair, a
   lat/lon dict, or a bbox (centroid used) — `run_pipeline` passes the analysis
   `bbox`. Public feed URLs are overridable via `GDACS_GEOJSON_URL` /
   `USGS_QUERY_URL` (blank = built-in default).

**`stance_engine.py` — `StanceEngine`.** `evaluate_orchestrator_instruction(
instruction, current_evidence, tracker)` asks the LLM (Qwen-pinned) whether an
orchestrator instruction makes scientific sense given the agent's evidence,
returning `{agree, confidence_in_own_position, reasoning, recommendation,
response_to_orchestrator, will_comply_if_insisted}`. **On total LLM failure it
defaults to a conservative *comply* stance** (the agent defers, never silently
ignores the orchestrator). `form_band_message(stance, handle)` renders it as a
natural room message: agreement → "Proceeding as suggested"; disagreement →
the reasoned push-back with the agent's confidence and either "will switch if you
insist" or "strongly recommend reconsidering".

**`agent.py` wiring.** A module-level `cross_validator = CrossValidator(
intelligence=intelligence)` is shared across tool calls. In `run_pipeline`: a
`ConfidenceTracker` is created per event; after the R2 upload, `validate_all`
runs over the result + analysis bbox, populating the tracker. The interpreter's
self-rated confidence is folded in as one more weighted source, and the
tracker's **overall score becomes the authoritative `confidence`** (not the LLM's
number alone). The quality gate now fires when `confidence < MIN_CONFIDENCE`
**or** `needs_verification()` **or** `should_alert_team()`. The natural Band
message is fed the cross-validation concerns alongside the interpreter anomalies
so the handoff flags them.

**Completion signal.** The structured payload gained `concerns` (the tracker's
concern list), `validations` (per-source findings), `needs_verification` and
`should_alert`, alongside the existing artifact URLs / areas / bounds /
`confidence`. The `band_message` (relayed verbatim by the model) carries the
natural-language version.

**Tests (`tests/test_suite_7_intelligence_stance.py`).** Four spec scenarios,
**offline + deterministic** (feeds + LLM stubbed): (1) normal flow → confidence
≥ 0.70, no alert, GDACS CONFIRMED; (2) GDACS discrepancy (sat 500 vs GDACS 120
km²) → 4.2× HIGH concern + DISCREPANCY finding; (3) low confidence → concerns
drop it < 0.70, `needs_verification()` true, CRITICAL trips `should_alert_team()`;
(4) stance disagreement (use SAR at 15% cloud) → agent pushes back with reasoning
in the Band message, will comply if insisted, and the LLM-down fallback defaults
to comply. **All 15 checks pass.** Graceful degradation verified separately (no
network: GDACS skipped, no crash; 70% cloud still trips the CRITICAL alert).

### Step 13: Rigorous end-to-end test pass + fixes — DONE

A full six-suite end-to-end test (intelligence, boundary, sentinel, processor,
R2, full pipeline) was run live against Featherless + CDSE + R2. Harnesses live
under `agents/satellite/tests/`. **Result: all suites pass.** Five real defects
were found and fixed along the way.

**Tally (live):**
- Suite 1 (intelligence, T1.1–T1.9): PASS — structural checks on all six methods.
- Suite 2 (boundary, T2.1–T2.4): 9/0/0.
- Suite 3 (sentinel, T3.1–T3.3): 7/0/1 (the 1 WARN: a CDSE cloud-peek read
  timeout, which correctly degrades to the user hint).
- Suite 4 (processor, T4.1–T4.6): 14/0/1 (the 1 WARN: 0 flood zones for
  dry-season Peshawar — physically correct).
- Suite 5 (R2, T5.1–T5.3): 9/0/0 — all artifact URLs HTTP 200, bounds shapes ok.
- Suite 6 (full pipeline):
  - **T6.1 Peshawar flood** 17/0/0 — Sentinel-2/NDWI, all 4 R2 URLs 200,
    interpretation + Band message generated (gemma + Kimi served the calls).
  - **T6.2 Mindanao earthquake** 10/0/0 — 3-tile set-cover mosaic → merged clip
    20990×14283 @ 67.5% valid → NDVI 26.97% affected, **251 zones / 822.39 km²**
    (matches Step 10) → all 4 merged R2 URLs 200 (geojson 13 MB) → Band message.
  - **T6.3 anomaly recovery** 3/0/0 — first CDSE auth forced to fail →
    `handle_anomaly("copernicus_auth_failed")` fired → retry succeeded → full
    output produced. This run exercised all three Featherless models
    (gemma/Kimi/**Qwen**).

**FIX 1 — reasoning-model token starvation (`intelligence.py`).** Kimi-K2.6 and
Qwen3.6 are *reasoning* models: they spend tokens thinking before emitting the
answer. At the old `max_tokens` (1024, and 512 for the Band message) they
returned `finish_reason=length` with **empty or truncated** content, so
`devise_satellite_strategy` (pins Kimi), `handle_anomaly` (pins Qwen) and
`generate_band_message` (pins Kimi) intermittently failed to `None`. Raised the
defaults to 2048 (`_complete`/`_complete_json`), `handle_anomaly` and
`interpret_results` to 2560, and the Band message to 1536.

**FIX 2 — Opus last resort always 400'd (`intelligence.py`).** The AIML-hosted
`claude-opus-4-8` rejects `temperature` ("deprecated for this model" → HTTP
400), so the final safety-net model never worked. `_complete` now omits
`temperature` for the `aiml` provider; the Opus fallback is verified working.

**FIX 3 — truncated-JSON repair (`intelligence.py`).** Added
`_repair_truncated_json`, used by `_extract_json` as a last resort: it walks the
bracket/string state of a cut-off response, drops the dangling token, and closes
open brackets so a mostly-complete reasoning-model reply still yields a usable
dict instead of dropping to the deterministic default. Unit-tested on mid-array,
after-colon, mid-string and nested truncations.

**FIX 4 — Mindanao analysed the whole island (`agent.py`).** `_RISK_CITY_MAP`
had no entry for `("mindanao, philippines", …)`, so `detect_risk_cities` fell
back to the headline token "Mindanao" — the **entire island** (~520×470 km). Its
bounding box is a ~2.5-**billion**-pixel clip window, which hung the pipeline for
40+ min and exhausted memory. Added curated 3-city entries (Davao, Cotabato,
Cagayan de Oro) for earthquake/landslide, matching the Step 8/10 scenario.

**FIX 5 — clip + memory blow-ups on the large mosaic (`processor.py`).**
- `clip_to_polygon` now **pre-windows to the polygon's pixel bbox** before the
  `rasterio.mask` rasterize. Previously it rasterized a full-grid in-memory
  GTiff (650M px ≈ hundreds of MB) on *every* call; windowing makes it operate
  only on the geometry's window. Verified byte-identical output to the old path
  (Suite 4 unchanged) and ~0.1 s on a 36M-px synthetic cube
  (`tests/test_clip_window.py`).
- The pre-clip stacked cube (`_stacked`, several GB on a mosaic) is now **freed
  + `gc.collect()`'d before the render tail** when per-city is off. On the 16 GB
  test box the Mindanao run dropped from ~17 GB (paging/thrash) to ~8 GB
  (CPU-bound) private memory, letting the merged vectorize + upload finish.

**Per-city artifacts disabled (`agent.py`).** Step 12's per-city render re-clips
the full mosaic once per city — far too slow/memory-heavy on a large multi-tile
AOI for the value it adds (the merged whole-area result already covers every
city). `run_pipeline` now passes `city_boundaries=None`, so the per-city block
is skipped; `city_geoms` is still threaded so the set-cover mosaic spreads
scenes across the scattered cities. Step 12 code is retained but dormant.

**Environment note.** The Mindanao mosaic (3 float32 bands × ~650M px + TCI) is
genuinely large; the full per-event run needs ~8 GB free RAM and the 251-zone
vectorize of the 300M-px merged array is a multi-minute single-threaded step.
Also observed: the Featherless plan has a **concurrency limit of 4 units** and
Kimi costs 4, so two pipeline runs in parallel get 429'd — the deterministic
pipeline issues LLM calls serially, so this is not a problem in production.

### Step 12: Per-city artifacts for multi-city AOIs — DONE (now dormant)

For a multi-city AOI the merged whole-area PNG/GeoJSON is awkward to consume —
the frontend and the hazard agent want **individual layers per city**. The
expensive part of the pipeline is download + stack (hundreds of MB for the
mosaic); clip, indices, PNG export and vectorization are all cheap and operate
on the already-stacked cube. So we **stack once, then re-clip the same mosaic to
each city polygon** and render a full artifact set per city — far cheaper than a
fresh search/download per city.

**`processor.py` refactor.**
- The cheap tail (indices → PNGs → vectorize → bounds) is extracted into
  `_render_clip(clipped, satellite_type, disaster_type, out_id)`, where `out_id`
  namespaces the PNG output dir (`<temp>/<out_id>/`). The merged result and each
  city now go through the same renderer.
- `_attempt_clip` stashes the pre-clip stacked cube on the clipped dict
  (`clipped["_stacked"]`). `clip_to_polygon` does **not** mutate its input
  `stacked` (it builds a fresh dict with `.copy()`'d band arrays), so the same
  cube can be re-clipped to many city polygons safely.
- `_render_per_city(stacked, satellite_type, disaster_type, event_id,
  city_boundaries)` loops the city boundaries (`{"name","geojson"}`), clips the
  shared `stacked` to each city polygon, **skips a city the imagery doesn't reach**
  (valid pixels `< MIN_VALID_PIXEL_PERCENT`), and renders its artifacts under
  `<event_id>/cities/<slug>/` (slug via `_slugify`). Returns a list of per-city
  result dicts (each with `name/slug/affected_area_km2/water_percent/mean_index/
  class_counts/valid_percent/png_paths/geojson/bounds`).
- `process_satellite_imagery` gained a `city_boundaries` param. When there is
  **more than one** city, the accepted mosaic is fed to `_render_per_city` and
  the per-city sets are attached to the result under `cities`. The merged
  whole-AOI result is **kept** (backward compatible) — per-city is additive.
  A single-city AOI is unchanged (no redundant `cities`).

**`agent.py` wiring.** `run_pipeline` passes `city_boundaries=city_polys` (the
boundaries already carry `{name, geojson}`). After uploading the merged set it
loops `result["cities"]`, uploading each via `upload_all_results(
f"{event_id}/cities/{slug}", {...})` so the R2 keys mirror the temp layout:
`events/<event_id>/cities/<slug>/{true_color,index_map,classification}.png` +
`zones.geojson`. A compact per-city summary + URLs (+ each city's own `bounds`)
is surfaced in the result payload under `cities`, alongside the merged
artifacts. `r2_upload.upload_all_results` was reused as-is — it already
namespaces every key by the `event_id` it's given, so the slashed per-city id
nests the objects correctly.

**Verification.** Unit-tested `_render_per_city` on a synthetic WGS84 S2 cube
(west half wet, east half dry) with two city boxes: West City → 100% water with
hazard zones, East City → 0% water (correctly clean), each writing its own three
PNGs under `cities/<slug>/` with independent bounds — proving each city is
clipped to its own polygon, not sharing the merged result. Directory layout on
disk confirmed: `<event_id>/cities/<slug>/{true_color,index_map,
classification}.png`.

### Step 11: LLM intelligence layer (`intelligence.py`) — DONE

The agent is no longer a pure GIS tool — every decision point can now consult an
LLM. `intelligence.py` adds `SatelliteIntelligence`, a thin reasoning layer over
**Featherless** (an OpenAI-compatible inference host) with a model fallback
chain and a Claude-Opus last resort via the **AIML** API. Both providers are
reached through the `openai` SDK with a custom `base_url`.

**Providers & keys.**
- Featherless — `base_url=https://api.featherless.ai/v1`, `FEATHERLESS_API_KEY`.
- AIML (Opus last resort) — `base_url=https://api.aimlapi.com/v1`, `AIML_API_KEY`.

**Model fallback chain (`_complete` / `_build_chain`).** Each LLM call walks
this chain, returning the first model that answers (logged as
`LLM call served by <provider>/<model>`):
1. `google/gemma-4-31B-it` — primary
2. `moonshotai/Kimi-K2.6` — fallback 1
3. `Qwen/Qwen3.6-35B-A3B` — fallback 2
4. `deepseek-ai/DeepSeek-V4-Pro` — fallback 3
5. `claude-opus-4-8` via AIML — last resort

A method may pin a **preferred primary** (e.g. method 2 prefers Kimi); the pin is
moved to the front of the chain for that call only. Per-model timeout is **30 s**
(`MODEL_TIMEOUT_SECONDS`); `max_retries=0` on the clients so our own chain owns
retry. A model that times out, errors, **or returns empty content** is skipped to
the next link. If every model fails, the method returns `None` and the caller
falls back to its deterministic default — intelligence is always additive, never
a hard dependency. JSON responses are parsed by `_extract_json` (strict parse →
strip ```` ```json ```` fence → outermost `{...}` span).

**The six methods** (each returns a parsed dict, or `None` on total failure;
method 5 returns free text):
1. `parse_disaster_input(raw_message)` — raw Band text → structured profile
   (`location/region/disaster_type/magnitude/secondary_risks/urgency/ambiguous/
   missing_info/confidence`). Model: gemma.
2. `devise_satellite_strategy(profile, cloud_cover, available_scenes_count,
   attempt_number)` — optimal satellite + analysis approach with reasoning
   (`satellite/reason/date_range_days/bands_priority/analysis_type/triage_*/
   confidence/fallback_strategy`). Model: Kimi.
3. `handle_anomaly(anomaly_type, context, attempt_number)` — recovery strategy
   (`action/specific_steps/use_landsat/expand_date_range/alert_human/
   alert_message/confidence_in_recovery/estimated_delay_seconds/reasoning`) for
   `no_sentinel_scenes`, `high_cloud_cover`, `low_data_quality`,
   `download_failed`, `coverage_insufficient`, `extreme_index_values`,
   `r2_upload_failed`, `copernicus_auth_failed`, `mosaic_failed`,
   `landsat_fallback_needed` (and any other label). Model: Qwen.
4. `interpret_results(index_type, index_stats, disaster_type, location,
   total_zones, area_km2, satellite_used)` — expert assessment
   (`severity/summary/key_findings/anomalies/comparison/immediate_concerns/
   confidence/data_quality/recommendations`). Model: gemma.
5. `generate_band_message(results, interpretation, anomalies, confidence,
   next_agent_handle)` — natural, expert-sounding hand-off message for the room
   (free text, not JSON; starts `@<handle>`, flags anomalies, ends with the
   event_id). Model: Kimi.
6. `decide_landsat_fallback(sentinel_failure_reason, disaster_type, location,
   days_since_disaster)` — whether Landsat 8/9 is worth trying
   (`use_landsat/reason/expected_quality/bands_to_use/confidence`). Model: gemma.

**Integration into `agent.py`.** `run_pipeline` now threads six integration
points alongside the deterministic pipeline (a module-level `intelligence =
SatelliteIntelligence()` is shared across tool calls; `MAX_STEP_ATTEMPTS = 3`,
`MIN_CONFIDENCE = 0.6`):
- **IP1 — parse + ambiguity gate.** A new optional `raw_message` tool field
  carries the original alert text; `parse_disaster_input` structures it. If the
  profile is `ambiguous` **and** a *core* field (location or disaster type) is
  genuinely missing, `run_pipeline` returns `status: clarification_needed`
  (`_clarification`) so the model asks the room to clarify. The gate matches
  missing fields as **standalone tokens** (`disaster_type`, `city`, …) or empty
  parsed values, so low-stakes notes like "confirmation of disaster type" do
  **not** trigger a spurious clarification loop.
- **IP2 — strategy reasoning.** `devise_satellite_strategy` runs after the
  cloud-aware `select_satellite`; its reasoning + date-window are **logged**. The
  deterministic cloud-aware selection stays authoritative for the actual mission
  (physics over assumption).
- **IP3 — anomaly recovery (max 3 attempts).** `_authenticate_with_recovery`
  retries CDSE auth, calling `handle_anomaly("copernicus_auth_failed")` between
  tries and honouring a bounded `estimated_delay_seconds` hint.
  `_search_with_recovery` widens the date window 7→14→30 days on
  `handle_anomaly("no_sentinel_scenes")`. A `coverage_insufficient` result asks
  `handle_anomaly` (which may advise Landsat / alert a human) and folds the
  advice into the error. `_recover` is the shared logging wrapper.
- **IP4 — interpretation.** After upload, `interpret_results` turns the raw index
  stats into an expert assessment, stored under `interpretation` in the result.
- **IP5 — natural Band message.** `generate_band_message` writes the room
  message; the model relays it **verbatim** (the system prompt instructs it to
  prefer `band_message` over the legacy key/value format, falling back only if
  it's missing). The full machine-readable payload still rides along in the JSON.
- **IP6 — confidence quality gate.** If the interpretation confidence
  `< MIN_CONFIDENCE`, `handle_anomaly("low_confidence")` is consulted (logged);
  the result is still sent because responders need the data.

The Band system prompt was updated to (a) pass `raw_message` through, (b) relay
`band_message` verbatim on success, and (c) handle the new
`clarification_needed` status.

**Verification.** `python intelligence.py` runs a live three-method smoke test
(parse → strategy → anomaly) against Featherless — confirmed: gemma serves the
gemma-pinned methods, and when Kimi/Qwen return empty content the chain falls
through to gemma cleanly. The three spec test cases pass:
- *Normal flood* (`"flood in Peshawar Pakistan"`): parsed `ambiguous=false`,
  conf 0.9, `disaster_type=flood`; strategy → sentinel-1 at 55% cloud.
- *Ambiguous* (`"disaster in KPK"`): parsed `location=null`,
  `disaster_type=null`, `ambiguous=true`, missing `["city","disaster_type",…]`
  → clarification fires.
- *Anomaly* (forced `copernicus_auth_failed`): exactly 3 attempts then graceful
  `None`.

### Step 7: Full remote-sensing pipeline — DONE

Major restructure of `processor.py` and the modules around it: the agent now
produces real analysis layers (true-colour, spectral index, classification
overlay) and vector zones clipped to the **actual risk polygon**, not a bbox.

**7A — Smart satellite selection (`sentinel.py:select_satellite`).** Now
returns a dict and is cloud-aware:
- `_peek_cloud_cover(bbox, token)` runs a lightweight, metadata-only S2
  catalogue query and reads the lowest cloud cover of recent scenes.
- Priority: cloud cover decides (> `CLOUD_COVER_THRESHOLD` 30% → Sentinel-1,
  else Sentinel-2). The user hint (flood/cyclone/tsunami → SAR;
  earthquake/landslide/wildfire → optical) is only a fallback when no cloud
  metadata is available. **Cloud cover always wins** (physics over assumption).
- Returns `{satellite_type, reason, cloud_cover, user_hint}`.

**7B — `download_imagery(selection, scene, event_id, token, disaster_type)`.**
CDSE only serves the whole `.SAFE` zip, so we download it once (resumable; the
`_CDSESession` + Range logic from the old code is kept and a completed zip is
reused) and **extract only the bands we need** into
`<temp>/<event_id>/bands/`:
- Sentinel-1 → `VV` + `VH` measurement TIFFs.
- Sentinel-2 → disaster-specific: flood `B03,B08,B11,TCI`; earthquake
  `B02,B04,B08,TCI`; landslide `B03,B04,B08,TCI`. The 10 m variant of a band is
  preferred when several resolutions exist.
- Returns `{satellite_type, band_paths}`.

**7C — `stack_bands(band_paths, satellite_type)`.** Reads each band onto the
highest-resolution band's grid, bilinearly resampling coarser bands (S2 B11 is
20 m → 10 m). TCI (RGB) is kept separately for the true-colour export. Returns
`{bands, tci, transform, crs, shape}`.

**7D — `clip_to_polygon(stacked, merged_polygon)`.** Replaces the old
`clip_to_bbox`. Reprojects the WGS84 merged risk geometry into the raster CRS,
builds an inside-polygon mask with `rasterio.mask` (`crop=True`), crops every
band to that window, and sets outside-polygon pixels to NaN (PNG nodata →
transparent). Verified to produce the true Peshawar+Nowshera+Charsadda polygon
silhouette, not a rectangle.

**7E — `calculate_indices(clipped, satellite_type, disaster_type)`.**
- S2 flood → NDWI `(B03−B08)/(B03+B08)`.
- S2 earthquake/landslide → NDVI `(B08−B04)/(B08+B04)`.
- S1 → VV backscatter in dB.
- Produces a **graded** `classification_array` via `_CLASS_SCHEMES`/`_classify`:
  `0` = safe land, `1..3` = increasing severity, `255` = nodata/outside polygon.
  Schemes: NDWI `wet_soil → water → deep_water`; SAR `possible_water → water →
  deep_water`; NDVI(quake) `sparse_veg → stressed → damage`; NDVI(landslide)
  `sparse_veg → exposed → scar`. Returns `{index_type, scheme_key, array,
  classification_array, water_percent, mean_value, threshold_used,
  class_counts}` (`class_counts` = % of valid pixels per class label).

**7F — `export_png(indices, clipped, event_id, disaster_type)`.** Writes three
PNGs to `<temp>/<event_id>/`. **All three are RGBA with the outside-polygon area
fully transparent (alpha 0)** so any layer drops over the map as the risk-area
silhouette, never a black/white box (the clip sets outside pixels to 0, so a
plain RGB true_color would otherwise show a solid black background):
- `true_color.png` — S2 TCI RGB (S1: VV greyscale), alpha from the clip mask
  (and all-black TCI pixels treated as outside, so seams stay transparent).
- `index_map.png` — NDWI Blues / NDVI RdYlGn / SAR grey, alpha = finite-index
  AND inside-polygon (shares the true_color silhouette).
- `classification.png` — graded hazard overlay (RGBA). **Only hazard classes
  (1..3) are painted**, deeper colour = higher severity; safe land (class 0)
  and outside-polygon (255) are fully transparent, so the layer drops cleanly
  over the map / true_color without a white "background" rectangle. (This
  replaced the earlier binary overlay that filled all land translucent white
  and rendered as an invisible blob when nothing was affected.)

**7G — `vectorize_classification(classification_array, transform, crs,
disaster_type, scheme_key)`.** Polygonizes **each hazard class separately**
(`rasterio.features.shapes`) → reproject to WGS84 → `shapely` simplify
(0.001°) → drop polygons `< 0.5 km²` (area via EPSG:6933). Each feature
carries `risk_type/hazard_class/class_level/area_km2/severity`
(severity from the class level: 1=low, 2=medium, 3=high); the
FeatureCollection adds `total_area`.

**7H — `r2_upload.upload_all_results(event_id, files_dict)`.** Uploads
`true_color.png`, `index_map.png`, `classification.png` and `zones.geojson`
(serialised from the in-memory dict) under `events/<event_id>/`, returning
`{true_color_url, index_url, classification_url, geojson_url}`. The single-file
`upload_to_r2` is retained for the demo-cache path.

**7I — `processor.process_satellite_imagery(selection, scene, bbox,
merged_polygon, event_id, token, disaster_type)`.** Chains
download → stack → clip → indices → export → vectorize and returns
`{satellite_type, index_type, water_percent, mean_index, affected_area_km2,
valid_percent, png_paths, geojson, bounds}` (`bounds` = WGS84 georeferencing for
the PNGs; see Step 9).

**7J — `agent.py`.** `run_pipeline` resolves region + risk-city boundaries
first (so they're reported even on a demo-cache hit), authenticates to CDSE,
calls the cloud-aware `select_satellite(disaster_type, bbox, token)`, runs the
full pipeline over the merged polygon, then `upload_all_results`. The reply to
`@hazardmind-hazard` now carries `satellite_type, cloud_cover, index_type,
affected_area_km2, water_percent` and all four artifact URLs.

### Step 8: Coverage-aware scene selection + mosaic + nodata guard — DONE

Testing a real disaster (Mindanao M7.8 earthquake, 2026-06-08) exposed a
selection failure: the three risk cities (Davao, Cotabato, Cagayan de Oro) are
scattered across a wide, mostly-empty bbox spanning **6+ Sentinel-2 tiles**. The
old `search_imagery` picked the single least-cloudy *intersecting* scene, which
turned out to be an edge tile (`T51NYJ`) overlapping only the empty corner of
the bbox. After clipping to the real risk polygon the result was **99.6% nodata**
→ NDVI 0, 0 zones, empty 64-byte GeoJSON. The pipeline ran to completion on
garbage. Three fixes, spanning `sentinel.py`, `processor.py` and `agent.py`:

**FIX 1 — coverage-aware ranking (`sentinel.py`).** `search_imagery` now scores
every candidate `score = aoi_overlap * (1 - cloud_cover/100)` and sorts
best-first. Crucially, overlap is measured against the **merged risk polygon**
(`aoi_geom`), not the bbox — a wide bbox around scattered cities is mostly empty,
so a tile can cover 30% of the *bbox* while covering 0% of the *cities* (exactly
the `T51NYJ` trap). Helpers: `_aoi_geometry(bbox, aoi_geom)` (polygon if given,
else bbox rectangle), `_scene_aoi_overlap(scene, aoi)` (intersect the scene's
`GeoFootprint` with the AOI), `_scene_score(scene, aoi)`. Each returned scene is
annotated with `_score`, `_overlap` (0..1), `_cloud` (%). New params:
`return_ranked` (return the full sorted list, not just the best) and `aoi_geom`.
Two supporting catalogue-query changes were required for ranking to work:
- **`$top` raised 10 → 100.** The catalogue is date-ordered, so with `$top=10`
  the high-coverage tile was truncated away before scoring ever saw it. The
  Mindanao 77%-coverage `T51PXK` tile only appeared once all intersecting scenes
  in the window were returned.
- **L1C-only filter (`contains(Name,'MSIL1C')`).** The catalogue returns both
  L1C and L2A for each tile; mixing processing levels in a mosaic is unsafe
  (different band naming/scaling) and the extractor targets L1C names. Filtering
  to one level keeps the candidate set and any mosaic consistent.

**FIX 2 — multi-tile mosaic (`processor.py`).** When the best scene covers less
than `COVERAGE_MOSAIC_THRESHOLD` (60%) of the AOI, `process_satellite_imagery`
mosaics the top `MOSAIC_MAX_SCENES` (3) scenes before clipping.
`_mosaic_bands(per_scene_paths, event_id)` merges each band token across scenes
with `rasterio.merge` (later scenes fill nodata gaps) into
`<temp>/<event_id>/bands/<token>.tif`. `download_imagery` now accepts a single
scene **or a list**, extracting each scene's bands into its own
`scene_<n>/` subdir (so same-named JP2s from different tiles don't clobber) then
mosaicking. When ≥60% is covered by one scene the mosaic is skipped (Mindanao:
`T51PXK` alone covers 77%, so a single download was used).

**FIX 3 — nodata guard with fallback (`processor.py`).** After clipping,
`_valid_pixel_percent(clipped)` measures the share of in-polygon pixels that are
finite and non-zero. `process_satellite_imagery` is now candidate-driven: it
builds an ordered attempt list (a mosaic first if coverage < 60%, then each
scene individually for fallback) via `_attempt_clip(...)`, and a candidate whose
valid share is below `MIN_VALID_PIXEL_PERCENT` (5%) is **rejected and the next
best is tried**. If every candidate is too sparse it returns
`{"status": "coverage_insufficient", "best_valid_percent", "min_required_percent"}`
instead of silently producing an empty result. On success the result dict gains
a `valid_percent` field. `agent.py` passes the ranked list + `merged` polygon to
the pipeline and surfaces `coverage_insufficient` as an error to the room.

New constants: `sentinel.COVERAGE_MOSAIC_THRESHOLD`,
`processor.{COVERAGE_MOSAIC_THRESHOLD, MOSAIC_MAX_SCENES, MIN_VALID_PIXEL_PERCENT}`.
`process_satellite_imagery`'s `scene_metadata` arg now accepts a single scene
(legacy) or the ranked list.

#### End-to-end test (Mindanao M7.8 earthquake, 2026-06-08) — PASS

Ran the full pipeline (`mindanao-eq-20260608`, "Mindanao, Philippines",
earthquake) against live CDSE + R2 over the merged Davao/Cotabato/Cagayan de Oro
polygon:
- Boundary: 3/4 cities resolved ("General Santos" had no Nominatim polygon and
  was skipped, not fatal). Merged-polygon area is only ~2.6% of the bbox area —
  the reason bbox-overlap was a bad proxy.
- Selection: polygon-aware ranking put `T51PXK` first (**score 0.636, 77%
  overlap, 17.8% cloud**), correctly above the old date/cloud winner `T51NYJ`
  (30% *bbox* overlap but **0% polygon** overlap). 9 L1C candidates ranked.
- 77% > 60% → single-scene path (no mosaic). Clip came back **100% valid
  pixels** (vs 0% before the fix).
- NDVI mean 0.242, **41.6% affected**; classes sparse_veg 8.7% / stressed 8.6% /
  **damage 24.3%**; **22 hazard zones, 153.37 km²**.
- All four artifacts uploaded and publicly fetchable (HTTP 200): true_color
  148 KB, index_map 189 KB, classification 21 KB, zones.geojson **901 KB** with
  22 graded features (cf. the pre-fix run: 614/1053/792/64 bytes).

### Step 10: Full multi-city coverage — set-cover mosaic + areal cities + date backfill — DONE

Re-testing Mindanao after raising the mosaic trigger surfaced that the three
risk cities still weren't all making it into the final overlay. Investigation
found **three independent causes**, each fixed:

**FIX A — mosaic threshold raised 60 → 85 (`sentinel.py` + `processor.py`).**
`COVERAGE_MOSAIC_THRESHOLD` (both copies) is now `85.0` (a **percent**, not a
fraction). The decision that actually drives the mosaic is
`processor.COVERAGE_MOSAIC_THRESHOLD` (compared `best_overlap*100 < threshold`);
`sentinel`'s copy is kept in sync but is not read for the decision. At 85, a
scattered multi-city AOI whose best single tile covers <85% reliably mosaics
(Mindanao's best is ~34%).

**FIX B — areal city geometries (`boundary.py`).** The real reason **Davao**
was never covered: Nominatim returns a **`MultiLineString` (zero area)** for
"Davao", not an admin polygon. A zero-area geometry can never be "covered"
(`intersection.area / geom.area` → 0/0) and silently dropped Davao from both
coverage scoring and the merged AOI. `get_risk_city_boundaries` now passes each
resolved geometry through `_ensure_areal()`, which buffers any zero-area
Point/line into a ~6 km disk (`_CITY_POINT_BUFFER_DEG = 0.05`) and re-emits it
via `shapely.mapping`. Davao went from 0% → 100% covered.

**FIX C — greedy set-cover mosaic selection (`sentinel.py`,
`select_mosaic_scenes`).** The old `scenes[:MOSAIC_MAX_SCENES]` took the top-N by
score, which **bunched on the single best-covered city** (all 3 slots near
Cotabato). Selection is now a weighted greedy set-cover over the *individual*
city polygons: each round picks the scene newly covering the most
still-uncovered cities (ties → higher score), then tops up spare slots
preferring scenes whose **MGRS tile** (`_scene_tile_id`) isn't already in the set
(so a spare slot adds new geography, not a duplicate tile). `processor`'s
`process_satellite_imagery` gained a `city_geoms` param; `agent.py` builds the
per-city shapely geoms and threads them in. Falls back to top-N when no geoms
are given.

**FIX D — date-window backfill for partial tiles
(`sentinel.py`, `backfill_uncovered_cities`).** With A–C in place **Cagayan de
Oro** still failed: its only 7-day tile (`51PXK`, 2026-06-09) is a **partial
acquisition** whose real pixel data stops at ~8.14 N — south of the city
(8.25–8.63 N) — even though its **catalogue footprint overstates** coverage
(claims 9.05 N). Because the footprint lies, footprint-based coverage thinks CdO
is fine while the pixels are missing. The backfill therefore treats a city as
safely covered only when **≥ `min_covering_scenes` (2) distinct acquisitions**
include it; for any city below that bar it re-queries *that city's own bbox*
over widening windows (14d, then 30d), appends new covering scenes (re-scored
against the AOI, de-duped by product Id), and stops once the bar is met. For CdO
this pulls in the **2026-06-01 51PXK** acquisition (footprint 8.75 N, real data
reaching the city); set-cover then prefers that scene over the partial one, and
the mosaic merges both PXK dates to fill the gap. `agent.py` runs the backfill
right after `search_imagery`. When even 30d finds nothing, it logs a
data-availability limit rather than silently dropping the city.

**FIX E — Id-keyed per-scene band extraction (`processor.py`).** With A–D in
place CdO *still* came back 0%, but selection was now correct (it picked the
2026-06-01 51PXK reaching 8.63 N). The culprit was a stale-cache bug:
`_extract_bands` reuses an already-present output file, and `download_imagery`
keyed each scene's extraction subdir on a **positional `scene_<idx>`**. Re-running
the same `event_id` with a *different* scene selection (as happens whenever the
candidate set changes) made `scene_1` serve a **previous run's tile**, so the
mosaic silently merged the wrong tile's data and CdO's pixels never made it in.
The per-scene subdir is now keyed on the scene's **stable product `Id`**
(`scene_<Id>`), so a different tile can never collide with another's cached
bands. This is a real correctness bug, not just a test artifact — any
re-process of an event with a changed scene set was affected.

New/changed: `sentinel.{select_mosaic_scenes, backfill_uncovered_cities,
_scene_covers_geom, _scene_tile_id}`; `boundary.{_ensure_areal,
_CITY_POINT_BUFFER_DEG}`; `processor.process_satellite_imagery(..., city_geoms=)`;
`processor.download_imagery` (Id-keyed scene subdirs). The Step 9 caveat
(single-tile bounds miss the southern cities) is now resolved by the mosaic path
covering all cities.

#### End-to-end test (Mindanao M7.8 earthquake, 2026-06-08) — PASS

Ran the full pipeline (`mindanao-eq-20260608`, "Mindanao, Philippines",
earthquake) against live CDSE over the merged Davao/Cotabato/Cagayan de Oro
polygon, verifying each city's polygon lands inside the exported PNG bounds:
- Boundary: 3/4 cities resolved (General Santos has no Nominatim polygon, skipped;
  **Davao resolved to a zero-area MultiLineString → buffered to a ~6 km disk**).
- Selection: best 7-day tile `T51NYH` covers only **34%** of the AOI (< 85%) →
  mosaic. Set-cover over the three city polygons. **Cagayan de Oro uncovered by
  the 7-day candidates → backfill widened to 14 days and pulled in the
  `T51PXK` 2026-06-01 acquisition** (99% overlap, 15% cloud) whose real data
  reaches 8.63 N; set-cover then chose it over the partial 06-09 PXK.
- Mosaic of 3 tiles (`NYH` 06-14 + `PXK` 06-01 + `NYJ` 06-09) → stacked
  30978×20976, clipped to 20990×14283, **67.5% valid pixels**.
- **Final PNG bounds N=8.630 (was 8.140 before FIX E)**; **all three cities
  100% inside the bounds** (Davao 0%→100%, Cotabato 100%, Cagayan de Oro
  0%→100%).
- NDVI mean 0.326, **27.0% affected**; classes sparse_veg 9.1% / stressed 10.8%
  / **damage 7.1%**; **251 hazard zones, 822.39 km²**.
- The `bounds` payload carries all three georeferencing shapes (`bounds`
  {west/south/east/north}, `bounds_leaflet`, `bounds_corners`) — verified
  present and consistent for the frontend overlay.

### Step 9: Overlay-ready layers — transparency + georeferencing — DONE

For the frontend to drop the PNGs onto a web map, two things were missing.

**Transparent outside-polygon background (`export_png`).** `true_color.png` was
written as plain RGB, and the clip sets outside-polygon pixels to 0, so it
rendered with a **solid black box** around the risk-area silhouette — fouling any
overlay. All three PNGs are now **RGBA with alpha 0 outside the clip mask**, so
they share one identical silhouette and composite cleanly:
- `true_color` alpha = clip mask AND not-all-black (so TCI nodata/seams are
  transparent too).
- `index_map` alpha = finite-index AND inside-polygon.
- `classification` already only paints graded hazard pixels.
Verified on Mindanao: true_color/index 90.8% transparent (9.2% = the cities),
classification 96.2% transparent.

**Georeferencing bounds (`_compute_bounds`, in the result payload).** A PNG has
no spatial info; a web map places it by its geographic extent. The clip is in the
scene's native UTM CRS, so `_compute_bounds(clipped)` derives the extent from the
clip `transform` + `shape`, reprojects the corners to **WGS84 (EPSG:4326)** with
`rasterio.warp.transform_bounds`, and returns it in three shapes (all PNGs share
these bounds):
- `bounds` — `{west, south, east, north}`
- `bounds_leaflet` — `[[south, west], [north, east]]` for `L.imageOverlay`
- `bounds_corners` — 4 `[lng, lat]` corners CW from top-left for a MapLibre/
  Mapbox `image` source
`process_satellite_imagery` adds `bounds` to its result and `agent.py` forwards
it in the room payload alongside the artifact URLs. Caveat: the bounds describe
the **rendered clip extent**, which on a single-scene result is that one tile's
footprint — for Mindanao (PXK only) the southern cities (Davao, Cotabato) fall
outside it; full coverage there needs the mosaic path.

#### End-to-end test (Peshawar flood) — PASS

Ran `run_pipeline(event_id="e2e-peshawar", location="Peshawar, Pakistan",
disaster_type="flood")` against live CDSE + R2:
- Smart selection peeked a recent S2 scene at **0% cloud** → Sentinel-2
  (`reason=clear_sky_cloud_cover_0_percent`), correctly overriding the flood
  hint's SAR default because the sky was clear.
- Downloaded the scene, extracted `B03/B08/B11/TCI`, stacked to 10980², clipped
  to the merged risk polygon (2661×5668, true silhouette).
- NDWI mean −0.146, 0% water — physically correct for dry-season June (no
  flood), so the GeoJSON is an empty FeatureCollection.
- All three PNGs + `zones.geojson` uploaded to R2 and are publicly fetchable
  (HTTP 200) at `pub-<id>.r2.dev/events/e2e-peshawar/…`.
- Index/classification/vectorization proven on a synthetic NDWI water block:
  36% water → one 1.474 km² `high/medium/low`-tagged GeoJSON polygon.

R2 credentials (account/endpoint/key/secret/bucket/public-URL) are now in
`.env`; `public-read` uploads and public reads both confirmed working.

#### Notes
- `select_satellite` signature changed to `(disaster_type, bbox=None,
  token=None, cloud_cover=None)` and returns a **dict** (was a string).
- `matplotlib` (colormaps), `shapely` and `pyproj` are now imported directly
  and pinned in `requirements.txt`. matplotlib 3.9+ dropped `cm.get_cmap`; we
  use `matplotlib.colormaps[...]`.
- Earlier per-module status (boundary/sentinel auth/search) from prior steps
  still holds; see below.

#### Fix: CDSE download auth + resumable transfer (`processor.py`)

Testing surfaced two real download failures, both now fixed in
`download_imagery`:

1. **401 on cross-host redirect.** The product `$value` endpoint 301-redirects
   from `catalogue.dataspace.copernicus.eu` to
   `download.dataspace.copernicus.eu`. `requests` strips the `Authorization`
   header on host changes, so the download host returned 401. Added
   `_CDSESession(requests.Session)` whose `rebuild_auth` keeps the Bearer token
   when both source and destination are trusted CDSE hosts (`_CDSE_AUTH_HOSTS`).
2. **`IncompleteRead` mid-transfer.** Products are large (the Peshawar scene was
   834 MB) and the stream can drop. The download now streams to a `.part` file
   and, on a connection/timeout error, **resumes** via an HTTP `Range` header
   (up to `max_retries=4`) instead of restarting; it verifies the final size
   against `Content-Length`/`Content-Range` before `os.replace`-ing the
   `.part` into place.

### Step 6: Band message send (`agent.py`) — DONE

The agent entry point. Connects to Band with the Anthropic adapter and waits for
the orchestrator to @mention it with a disaster, then runs the full pipeline and
relays the result to `@hazardmind-hazard`.

The Band Anthropic adapter is LLM-driven (it runs a tool loop and sends the
model's reply to the room), so the deterministic pipeline is exposed as a
**custom tool** rather than a hand-written message handler:

- `ProcessDisasterInput` — Pydantic model (`event_id`, `location`,
  `disaster_type`, optional `magnitude`). Its class name yields the tool name
  `processdisaster`; its docstring is the tool description.
- `run_pipeline(params)` — the tool callable. Chains the pipeline in order:
  `check_demo_cache` → `get_region_boundary` → `detect_risk_cities` →
  `get_risk_city_boundaries` → `merge_risk_boundaries` → `get_analysis_bbox` →
  `authenticate_copernicus` → `select_satellite` → `search_imagery` →
  `process_satellite_imagery` → `upload_to_r2`. Returns a JSON string with
  `status: complete` (image_url, bbox, satellite_type, region_boundary,
  risk_cities) or `status: error` (error message). Never raises — any failure
  (including a caught `Exception`) becomes an error payload so it surfaces to the
  room instead of killing the agent.
- `PROCESS_DISASTER_TOOL = (ProcessDisasterInput, run_pipeline)` — the
  `CustomToolDef` passed to `AnthropicAdapter(additional_tools=[...])`.
- `detect_risk_cities(location, disaster_type)` — infers at-risk cities from a
  small curated map (the three demo regions); unknown inputs fall back to the
  headline location token so a boundary can still resolve.
- `SYSTEM_PROMPT` — instructs the model to call `processdisaster` once per
  disaster mention and reply to `@hazardmind-hazard` in the exact required
  format, using only tool-returned values.

Demo cache short-circuit: on a `check_demo_cache` hit the agent still resolves
boundaries for the map overlay but skips the download/clip/export + upload.

Connection mirrors the (verified) `verify_setup.py`: `BAND_AGENT_ID` /
`BAND_API_KEY` / `ANTHROPIC_API_KEY` from `.env`, `THENVOI_REST_URL` /
`THENVOI_WS_URL` with Band defaults, `Agent.create(...)` → `await agent.start()`
→ `await agent.run_forever()` → `await agent.stop()`.

Notes:
- `python agent.py` runs the long-lived agent (needs live Band creds). Imports,
  tool-name resolution (`processdisaster`), schema/description, and
  `detect_risk_cities` are verified offline.

### Step 5: Cloudflare R2 upload (`r2_upload.py`) — DONE

Pushes the `satellite.png` from `processor.export_png` to a Cloudflare R2
bucket (S3-compatible) and returns a public URL for the frontend.

- `get_r2_client()` — boto3 S3 client pointed at the R2 endpoint. The endpoint
  is `CLOUDFLARE_R2_ENDPOINT` if set, else built from `CLOUDFLARE_ACCOUNT_ID`
  (`https://<account_id>.r2.cloudflarestorage.com`). Access key from
  `CLOUDFLARE_R2_KEY` (or `CLOUDFLARE_R2_ACCESS_KEY`), secret from
  `CLOUDFLARE_R2_SECRET`. Uses `region_name="auto"` + SigV4.
- `upload_to_r2(png_path, event_id)` — uploads to key
  `events/<event_id>/satellite.png` in `CLOUDFLARE_R2_BUCKET` with
  `ContentType=image/png` and a `public-read` ACL. Returns the public URL.
- `check_demo_cache(event_id)` — for the three demo events (`peshawar`,
  `dhaka`, `kathmandu`), `head_object`s the cached PNG; on a hit returns its
  public URL so the caller can skip the live pipeline, otherwise `None`. Any
  non-demo event returns `None` immediately without touching R2.

Notes:
- Public URL base is `CLOUDFLARE_R2_PUBLIC_URL` if set (an r2.dev or custom
  domain bound to the bucket), else the account r2.dev domain
  (`https://pub-<account_id>.r2.dev`).
- The existing `.env.example` already pins the R2 vars; `boto3` was already in
  `requirements.txt` (installed in the venv).
- All functions log and return `None` on failure rather than raising.
- `python r2_upload.py` builds a client and probes the demo cache. Verified
  offline: the module imports, fails gracefully with no creds, non-demo events
  short-circuit, and the object key is `events/<event_id>/satellite.png`. The
  live upload/cache check needs real R2 credentials in `.env`.

### Step 4: Image download + processing (`processor.py`) — SUPERSEDED by Step 7

The original single-band download → bbox-clip → single PNG flow described below
was replaced by the multi-band remote-sensing pipeline in Step 7
(`clip_to_bbox` → `clip_to_polygon`, single PNG → three PNGs + GeoJSON). Kept
for history.

Downloads the scene chosen by `sentinel.search_imagery`, clips it to the
analysis bbox from `boundary.get_analysis_bbox`, and exports a web-ready PNG.

- `download_imagery(scene_metadata, token)` — streams the product archive from
  the CDSE OData download endpoint (`/Products(<Id>)/$value`) using the Bearer
  token from `authenticate_copernicus`. Saves `<temp>/<Id>.zip` and returns its
  path.
- `clip_to_bbox(image_path, bbox)` — opens a band inside the downloaded `.zip`
  via rasterio's `zip://` scheme (prefers a TCI/preview band), reprojects the
  WGS84 bbox into the raster CRS, reads only the intersecting window, and writes
  a clipped GeoTIFF. Returns the clipped path.
- `export_png(clipped_path, event_id)` — renders the clip to RGB (first 3 bands;
  greyscale replicated for single-band SAR), applies a 2–98 percentile stretch,
  decimates so the longest side is ≤ 1024 px, and writes an optimized
  `<temp>/<event_id>/satellite.png`.
- `process_satellite_imagery(scene_metadata, bbox, event_id, token)` — master
  function chaining the three stages; returns the final PNG path.

Notes:
- Intermediate files live under `<system-temp>/hazardmind-satellite/` to keep
  artifacts out of the repo.
- Added `numpy` and `pillow` to `requirements.txt` (imported directly; numpy was
  previously only transitive via rasterio). Pillow had to be installed into the
  venv.
- All functions log and return `None` on failure rather than raising.
- `clip_to_bbox` and `export_png` are verified against a synthetic GeoTIFF
  (clip reduces extent exactly; PNG downsamples to 1024 px). `download_imagery`
  and the full pipeline are verified live against CDSE in Step 7 (after the
  auth-redirect + resumable-download fix).

### Step 3: Sentinel selection + Copernicus auth (`sentinel.py`) — DONE

Picks the Sentinel mission for a disaster, authenticates to the Copernicus Data
Space Ecosystem (CDSE), and finds the best scene over a bbox.

- `select_satellite(...)` — **superseded by Step 7A** (now cloud-aware and
  returns a dict). Originally: flood → Sentinel-1; earthquake/landslide →
  Sentinel-2; optical switched to SAR if `cloud_cover > 30%`.
- `authenticate_copernicus()` — password-grant token from the CDSE Keycloak
  endpoint using `COPERNICUS_USERNAME`/`COPERNICUS_PASSWORD` (client_id
  `cdse-public`). Returns the access token, or `None` on missing creds/failure.
- `search_imagery(bbox, satellite_type, date_range=7)` — queries the CDSE OData
  catalogue (`/odata/v1/Products`) intersecting the bbox over the last
  `date_range` days. Sentinel-1: most recent scene. Sentinel-2: filtered to
  cloud cover < 30%, least-cloudy scene wins. Returns the scene metadata dict.

Notes:
- `CLOUD_COVER_THRESHOLD = 30.0` is shared by both selection and the S2 filter.
- Added `requests` to `requirements.txt` (sentinel.py imports it directly;
  boundary.py had relied on it being transitively present).
- All functions log and return `None`/skip on failure rather than raising.
- `python sentinel.py` runs an offline selection demo plus a live auth +
  catalogue smoke test (small Lahore bbox); confirmed working against CDSE.

### Step 2: Boundary fetching (`boundary.py`) — DONE

Fetches administrative boundaries from the Nominatim (OpenStreetMap) API to
drive both the map display and the satellite clip extent.

- `get_region_boundary(location_name)` — region's GeoJSON polygon + bbox
  (faded background on the map). E.g. `"Punjab, Pakistan"` → province boundary.
- `get_risk_city_boundaries(region_name, city_list)` — one boundary per risk
  city (highlighted overlay). Cities are disambiguated by appending the region
  name; unresolved cities are logged and skipped, not fatal.
- `merge_risk_boundaries(city_polygons)` — `shapely.unary_union` of the risk
  cities into a single GeoJSON geometry.
- `get_analysis_bbox(merged_polygon)` — `(minx, miny, maxx, maxy)` of the
  merged geometry; this is the bbox the imagery pipeline clips to.

Strategy: region = faded background, risk cities = highlighted overlay,
satellite clip = merged risk-city bbox only (avoids downloading the whole
region).

Notes:
- Deps (`requests`, `shapely`) were already pinned; no new requirements added.
- Honors Nominatim policy: descriptive User-Agent + ≤1 request/sec (enforced
  by a module-level throttle in `_nominatim_search`).
- All functions return `None` / skip on failure and log via the module logger
  rather than raising, so a single bad city does not abort an analysis.
- `python boundary.py` runs a live smoke test (Punjab + Lahore/Multan). Place
  names can be non-ASCII (Urdu), so the test reconfigures stdout to UTF-8.
