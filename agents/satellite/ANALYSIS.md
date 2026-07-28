# Satellite Agent — Deep Analysis (2026-07-27, post-fix-pass)

**Scope:** `agents/satellite/` as it exists at the tip of `main` (merge commit
`b1be94f`, PR #10 "fix/satellite-correctness"), i.e. after both the
2026-07-26 coverage/CRS correctness pass **and** the 2026-07-27
correctness/contract fix pass that followed it. Every file was read in full:
`node.py`, `agent.py`, `sentinel.py`, `processor.py`, `confidence_tracker.py`,
`cross_validator.py`, `intelligence.py`, `boundary.py`, `geoboundaries.py`,
`r2_upload.py`, the `tests/` directory, and the downstream consumers in
`agents/hazard/` (`agent.py`, `analyzer.py`) that read the satellite payload.

**Relationship to the prior `ANALYSIS.md`.** A prior version of this
document (commit `2daddf6`, "deep analysis-only audit") drove six follow-up
fix commits (`7eb23f0` … `b7fdad4`, squashed into `fd7a08c`'s doc update).
This document supersedes it. Section 11 below explicitly reconciles every
finding the prior document made against the code as it stands now — several
of its gaps are now closed by those six commits, one of its central claims
(hazard never reads satellite confidence) was **already wrong when written**
(the fix had landed one commit earlier, at `fa0d9bd`, timestamped 09:39:24,
ten minutes before the analysis commit at 09:49:13 — confirmed by
`git log --format=%ci`), and this pass independently re-verified all of it
against current code rather than trusting either document.

**Method.** Every behavioral claim below is backed by a direct code read at
the cited file:line, done in this session. `CLAUDE.md`, `CODEBASE.md`,
`root_cause.md` and the prior `ANALYSIS.md` were all treated as leads to
re-verify, not settled fact, per the task brief. Disagreements are called out
explicitly. **No code was changed to produce this document.**

---

## 1. RESPONSIBILITY

**What this agent is for.** Given `(event_id, location, disaster_type,
magnitude)`, produce a *grounded, artifact-backed* hazard observation from
live Sentinel-1/2 imagery: resolve the real administrative boundary for the
named place, pull the best available satellite scene(s) covering it, compute
a disaster-appropriate spectral/radar index (NDWI for flood/optical, SAR
backscatter for flood/cloud-obscured, NDVI for earthquake/landslide damage
proxy), classify hazard severity into vector zones, render three PNG map
layers + a GeoJSON, upload them to R2, and self-assess how much to trust its
own answer (`confidence`, `concerns`, `needs_verification`).

**The question it uniquely answers:** *"What does the imagery actually show,
over exactly the risk area, right now?"* — a `satellite_type`,
`affected_area_km2`, a classified hazard map, and a confidence-scored
assessment of whether that measurement should be trusted.

**Explicitly not this agent's job:**
- Deciding overall disaster risk level (flood/earthquake/landslide risk
  classification, MMI, liquefaction) — that's `agents/hazard/analyzer.py`,
  which *consumes* this agent's `affected_area_km2`/`mean_value`/
  `satellite_type` as raw evidence, not a verdict.
- Population/infrastructure impact — `agents/impact/`.
- Report narrative/PDF — `agents/report/`.
- Any ground-truth field validation — this agent only ever sees remote
  sensing; it cross-checks against GDACS/USGS (also remote sources), never
  a ground report.

**Where it blurs with hazard (its downstream neighbor).** The boundary is not
as clean as "satellite computes pixels, hazard computes risk" — two things
leak across:
1. **The index label/unit contract.** Satellite computes `mean_index` in
   *different units* depending on `satellite_type` (bounded NDWI/NDVI ratio
   vs. unbounded uncalibrated SAR dB) and is responsible for *labeling* that
   honestly (`index_calibrated`, `index_units` — added in the 2026-07-26
   pass). Hazard is responsible for *branching on* that label before applying
   any threshold. As verified in §6/§10 below, hazard's LLM-facing prompt
   does this correctly; hazard's deterministic fallback does not — and
   satellite cannot fix that from its side, since `index_calibrated`/
   `index_units` aren't even in the flat payload hazard's own adapter reads
   into its `analysis` dict (only `index_type` is).
2. **The confidence handoff.** Satellite computes a real, weighted
   `confidence` (§5) and puts it in the flat result. Whether that number
   *means anything* downstream depends entirely on hazard actually reading
   it — which, as verified in §10 below (correcting the prior ANALYSIS.md),
   it now partially does.

---

## 2. EXECUTION FLOW

### 2.1 Entry point

`node.py:satellite_node(state)` is the only way the pipeline is invoked in
production (LangGraph node, no Band/transport layer). It:
1. Builds a `ProcessDisasterInput(event_id, location, disaster_type,
   magnitude)` from `PipelineState`.
2. Calls `await run_pipeline(params)` →
   `asyncio.to_thread(_run_pipeline_sync, params)`.
3. Parses the JSON string result. If `status != "complete"`, returns
   `{"status": "failed", "errors": [...]}"` (appended, not overwritten). On
   success: `{"satellite_result": result, "status": "hazard", "progress": 25,
   "confidence_scores": {..., "satellite": result["confidence"]}}`.

This is a thin, correct adapter — it cannot itself produce a false-success:
if `_run_pipeline_sync` returns anything but `"complete"`, `node.py` marks
the graph `failed`.

### 2.2 `_run_pipeline_sync` — the real pipeline (agent.py:444-1000)

```
_run_pipeline_sync(params)
├─ [event_id in _completed_event_ids?] → return cached "complete" (process-once guard)
├─ ConfidenceTracker() created (per-event ledger)
├─ intelligence.parse_disaster_input(raw)                              [LLM, best-effort]
│    branch: profile.ambiguous AND (loc_missing OR type_missing)
│      → return _clarification(...)  [status: clarification_needed]
├─ get_region_boundary(location) → branch: None → _error(...)          [boundary.py]
├─ detect_risk_cities(location, disaster_type) → branch: empty → _error(...)
├─ get_risk_city_boundaries(location, cities) → branch: empty → _error(...)
├─ merge_risk_boundaries(city_polys) → branch: None → _error(...)
├─ get_analysis_bbox(merged) → branch: None → _error(...)
├─ check_demo_cache(event_id)  [r2_upload.py, 3 literal demo ids only]
│    branch: hit → return "complete" with cached_url, no real analysis
├─ _authenticate_with_recovery(event_id, location)  [<=3 attempts, LLM-guided]
│    → sentinel.TokenManager per attempt; branch: None after 3 → _error(...)
├─ select_satellite(disaster_type, bbox, token_manager.get())          [cloud-aware]
│    branch: cloud_cover > 30% → sentinel-1, else sentinel-2
├─ intelligence.devise_satellite_strategy(...)                         [LLM, LOGGED ONLY]
├─ _search_with_recovery(event_id, bbox, satellite_type, merged)       [7->14->30 day widening]
│    branch: no scenes ever found → _error(...)
├─ backfill_uncovered_cities(scenes, city_polys, satellite_type, merged)
├─ process_satellite_imagery(selection, scenes, bbox, merged, event_id,
│                             token_manager, disaster_type, city_geoms=[...],
│                             city_boundaries=city_polys if ENABLE_PER_CITY_ARTIFACTS else None,
│                             tracker=tracker)
│    [see §2.3 for the tiered coverage search inside this call]
│    branch: result is None → _error("Satellite imagery processing failed")
│    branch: result.status == "failed"/"insufficient_coverage"
│      → _recover("coverage_insufficient", ...) [LLM advisory, non-blocking]
│      → return _coverage_failure(...) [status: error, gap geometry attached]
├─ upload_all_results(event_id, {true_color, index_map, classification, geojson})
│    → returns urls + failed_artifacts (NEW, see §10)
├─ per-city upload loop over result.get("cities", [])  [empty unless ENABLE_PER_CITY_ARTIFACTS=true]
├─ asyncio.run(cleanup_event_temp(event_id))            [deletes temp working tree]
├─ validation_input built (index_type/index_calibrated/index_units/mean_index/...)
│    assert validation_input["index_type"] == result["index_type"]     [hard assertion]
├─ cross_validator.validate_all(validation_input, disaster_type, bbox, tracker)
│    [GDACS / USGS / cloud / index-physics / coverage / Featherless-expert]
├─ intelligence.interpret_results(...)                                 [LLM, folded into tracker]
├─ confidence = round(tracker.overall_confidence(), 4)                 [AUTHORITATIVE]
├─ confidence gate: confidence < 0.6 OR needs_verification() OR should_alert_team()
│      → _recover("low_confidence", ...) [LOGGED ONLY — result still returned]
├─ structured{} built (mirrors satellite_results DB columns + extras, incl.
│    NEW: total_zones, scene_id, artifacts_incomplete, failed_artifacts,
│    confidence_basis, evidence_count — see §10)
├─ intelligence.generate_band_message(...)                             [LLM, narrative only]
├─ _persist_satellite_result(event_id, structured)
│    branch: fails after PERSIST_MAX_ATTEMPTS retries → return _error(...) [NEW, see §7]
├─ _completed_event_ids.add(event_id)
└─ return json.dumps(structured)                                       [status: "complete"]

except Exception → return _error(event_id, f"Unexpected error: {exc}")   [blanket catch]
finally:
  → asyncio.run(cleanup_event_temp(event_id))  [guaranteed cleanup]
  → log memory_report()
```

### 2.3 `process_satellite_imagery` — the tiered coverage search (processor.py:2163-2448)

> **⚠️ SUPERSEDED 2026-07-28 (branch `fix/coverage-tolerance`) — see
> CLAUDE.md's "Coverage Tolerance Fix Pass" section for the current
> behavior.** The diagram and "Key fact" note directly below describe the
> pipeline's behavior BEFORE this date and are kept as history, per this
> repo's convention of annotating past behavior rather than erasing it (see
> the "Cross-Agent Honesty Fix Pass" entries elsewhere in this file for the
> same pattern). They no longer describe what the code does. In short: the
> exact-100%-or-fail rule below is now a caller-controlled band
> (`min_coverage_percent`, clamped into `[COVERAGE_FLOOR=80,
> COVERAGE_CEILING=100]`) with three outcomes (`target_met` /
> `below_target_coverage` / hard-fail below the floor) instead of two
> (`covered==100.0` / hard-fail below 100.0); the tier loop now also
> enforces whole-search budgets (`max_scenes`/`max_download_gb`/
> `max_search_seconds`), stops chasing a gap no remaining candidate can
> close (weather-limited or footprint-non-intersecting), and stops on
> marginal returns (< 2 coverage points gained). The per-satellite tier
> WINDOWS referenced below (tier2 +-3d / tier3 +-7d) also changed for
> Sentinel-1 specifically (now a single +-10d window) — see CLAUDE.md for
> the measured-revisit rationale; Sentinel-2's windows are unchanged.

```
process_satellite_imagery(selection, scene_metadata, bbox, merged_polygon, event_id, token, disaster_type, ...)
├─ dedupe_by_acquisition(scenes)                     [collapses GRD/GRD-COG twins of one acquisition]
├─ filter scenes to those with _scene_aoi_overlap(scene, aoi_shape) > 0.0
│    branch: none left → return {"status":"failed","reason":"insufficient_coverage"} immediately
├─ build_coverage_tiers(scenes, satellite_type)       [tier1: same date+orbit, tier2: +-3d, tier3: +-7d, tier4: +-14d any-orbit]
│    branch: no parseable dates → single fallback tier (4, None, scenes)
└─ for (tier, orbit_dir, group) in tiers:              [tries tier 1 first, stops at first tier reaching 100%]
     for scene in group:                               [best-first within the tier]
       ├─ _attempt_clip(...)  [download -> stack -> clip]
       │    branch: clip fails → doomed_streak++; abort tier if streak >= DOOMED_DOWNLOAD_LIMIT (3)
       ├─ compute_coverage(trial_clip)  [interior_coverage_percent — the REAL pass/fail metric]
       │    branch: gained coverage <= 0.01% and accepted non-empty → doomed_streak++
       ├─ accept if gained > 0.01%: accepted = trial; cov = trial_cov
       └─ branch: cov["covered"] (interior coverage == 100.0%) → break inner loop, tier succeeds
     branch: tier succeeded → render (_render_clip), attach coverage_tier/temporal_spread_days/
             acquisition_count, add tier>=3 confidence-lowering concern, add SAR-uncalibrated
             concern if applicable, return merged_result
  [no tier ever reached 100%] → return {"status":"failed","reason":"insufficient_coverage",
                                          "coverage_percent": best interior %, "gaps": [...]}
```

**Key fact, confirmed in code AS OF THE ORIGINAL (pre-2026-07-28) VERSION
ONLY — see the superseded note above:** there was no partial-coverage risk
output. Either some tier's cumulative mosaic reached exactly 100% interior
valid-pixel coverage, or the whole call failed with `insufficient_coverage`
and gap geometry. As of 2026-07-28 this is no longer true: a run between
`COVERAGE_FLOOR` (80%) and the caller's target now DOES produce a risk
output — explicitly flagged `coverage_status:"below_target_coverage"`, never
silently. The underlying principle (never report a partial analysis as
complete WITHOUT saying so) is preserved; only the "how" changed, from
refusing to answer to answering honestly with the limitation stated.

---

## 3. DECISION LOGIC — Every Branch and Its Basis

| Decision | Driver | Deterministic or LLM? | Basis for the value |
|---|---|---|---|
| **S1 vs S2 selection** (`select_satellite`, sentinel.py) | Real observed cloud cover from `_peek_cloud_cover`; `CLOUD_COVER_THRESHOLD = 30.0` | Deterministic. LLM's `devise_satellite_strategy` is logged only, never overrides. | Arbitrary round number, no citation. Consistently applied (same threshold gates the S2 catalogue filter). **⚠️ SUPERSEDED 2026-07-28 (branch `fix/coverage-tolerance`, CHANGE 6) — see CLAUDE.md's "AOI-restricted cloud measurement" section.** The cloud figure the threshold is applied to is no longer always the scene's whole-tile metadata reading: `agent.py` now peeks the actual SCL band over the AOI (`processor.peek_aoi_cloud_percent`) whenever the scene-level reading is ambiguous (`processor.peek_needed`, cut points 15%/50%), and `select_satellite` applies the threshold to that AOI-restricted figure when one exists, falling back to the scene-level figure otherwise. The threshold constant itself (30.0) is unchanged and still arbitrary/uncited; what changed is which cloud number it's compared against. |
| **Date window widening** (`_search_with_recovery`) | Fixed sequence 7->14->30 days | Deterministic (LLM's `handle_anomaly` result is invoked but its widening hint is never read) | Arbitrary round numbers, no stated rationale. |
| **Coverage-mosaic trigger** (`COVERAGE_MOSAIC_THRESHOLD`) | — | — | **DELETED.** Confirmed absent from both `sentinel.py` and `processor.py` (`grep` returns zero hits in `sentinel.py`; per the CLAUDE.md 2026-07-27 log this constant and `select_mosaic_scenes` were confirmed orphaned by the tiered-coverage design and removed in commit `b7fdad4`). The prior ANALYSIS.md flagged this as dead code to delete-or-rewire; it was deleted. The real acceptance test is `compute_coverage()["covered"]` (interior == 100%). |
| **Candidate rejection within a tier** (`DOOMED_DOWNLOAD_LIMIT = 3`) | processor.py | Deterministic | Arbitrary round number, no stated basis. |
| **Valid-pixel candidate floor** (`MIN_VALID_PIXEL_PERCENT = 5.0`) | processor.py | Deterministic | Dead for the merged/main path (tiered search demands 100% interior coverage); still live only inside `_render_per_city`'s per-city skip check, which is now reachable behind `ENABLE_PER_CITY_ARTIFACTS` (see §10) rather than permanently unreachable. |
| **Tier escalation** (`COVERAGE_TIERS = ((1,0,True),(2,3,True),(3,7,True),(4,14,False))`) | sentinel.py | Deterministic | Day windows are round numbers. **Live-measured** (per CLAUDE.md's 2026-07-27 "Tier-window revisit analysis," a design review with no code change): S2 combined-constellation revisit ~5d matches the tiers fine; S1's actual same-relative-orbit revisit over two Pakistani AOIs measured **~11 days** (not the newer 6-day S1C/1D cadence, which is Europe-concentrated) — so tiers 2 (+-3d) and 3 (+-7d) are structurally near-no-ops for S1 specifically (7 < 11 regardless of window tuning). Left unchanged deliberately: cost of an empty tier is one skipped loop iteration, and widening would touch validated live-tested tier behavior for unproven benefit. |
| **Classification class boundaries** (`_CLASS_SCHEMES`) | NDWI: 0.0/0.3/0.5; SAR: -13/-15/-18 dB; NDVI: 0.2/0.1/0.0 | Deterministic | Arbitrary/literature-adjacent but unvalidated. NDWI 0.3 is a plausible literature variant (McFeeters 1996 style), not cited in-code. SAR dB thresholds have **no defensible basis** given the index is uncalibrated raw-DN log (§4.2). NDVI 0.2 is a round number, no citation. None of these were revalidated after S2 switched L1C→L2A. |
| **SCL cloud-mask classes** (`_SCL_INVALID_CLASSES`) | ESA's own SCL class table | Deterministic | **Sound** — this is the one grounded threshold set, directly following ESA's documented class definitions. |
| **Confidence quality gate** (`MIN_CONFIDENCE = 0.6`) | agent.py | Deterministic | Arbitrary round number. |
| **needs_verification threshold** (`VERIFICATION_THRESHOLD = 0.70`) | confidence_tracker.py | Deterministic | Arbitrary round number. |
| **Concern severity penalties** (LOW .05/MED .10/HIGH .20/CRITICAL .35) | confidence_tracker.py | Deterministic | Arbitrary, flat, additive — see §5. |
| **GDACS discrepancy ratio bands** (0.7-1.3 CONFIRMED, >2.0 HIGH, <0.5 HIGH) | cross_validator.py | Deterministic | Round numbers, no cited basis. |
| **Recency half-life** (`_RECENCY_HALFLIFE_DAYS = 20.0`) | sentinel.py | Deterministic | Reasoned intent ("older well-covered scene beats nearly-empty newer one"), constant itself unvalidated. |
| **Zone area floor** (`MIN_ZONE_AREA_KM2 = 0.5`) | processor.py | Deterministic | Arbitrary noise-filter round number. |
| **Per-city rendering** (`ENABLE_PER_CITY_ARTIFACTS`) | agent.py, env var, default `false` | Deterministic, operator-controlled | **Changed since the prior analysis** — was an unconditional hardcoded `city_boundaries=None`; now a feature flag. Still off by default because per-city re-clipping multiplies peak RSS on top of an already ~9.6 GB single-mosaic peak (§8.1). |

**Summary (unchanged from the prior analysis, still true):** every threshold
is a round number chosen by feel; only the SCL invalid-class set has a real
physical basis (ESA's own table). The tiered/temporal-coherence/cloud-aware
*architecture* is well-reasoned; the numeric knobs inside it are not.

---

## 4. THE ANALYSIS ITSELF — The Science

### 4.1 NDWI (flood, Sentinel-2)

**Code** (processor.py:1402-1412):
```python
index = _safe_ratio(b03 - b08, b03 + b08)   # (B03-B08)/(B03+B08)
threshold = NDWI_WATER_THRESHOLD  # 0.3
index_calibrated = True
index_units = "NDWI_ratio"
```
- Bands: B03 (green, 10 m), B08 (NIR, 10 m) — both native 10 m on S2, no
  resampling needed for this pair.
- Formula matches McFeeters (1996) NDWI exactly: `(Green − NIR)/(Green + NIR)`.
- Resolution: 10 m, resampled via `Resampling.bilinear` where needed (only
  B11/SCL at 20 m are resampled up; B03/B08 are native).
- **Literature method:** McFeeters' NDWI is the standard formula used here —
  this part is textbook-correct.
- **Difference from literature:** none in the formula itself. The
  **input reflectance** changed from L1C (top-of-atmosphere) to L2A (surface
  reflectance, BUG 2 fix) on 2026-07-26, and the `0.3`/`0.5` thresholds were
  tuned against L1C and **not retuned** for L2A (confirmed still true —
  `processor.py:150` comment and `CLAUDE.md`'s 2026-07-26 section both state
  this explicitly). L2A surface reflectance NDWI values are generally *higher*
  than L1C TOA values over water (atmospheric correction removes some
  scattering that suppresses NDWI at TOA), so the current 0.3 threshold may
  now be **too permissive** (over-classifying wet soil as water) — this is a
  real, acknowledged, unresolved defect, not a hypothetical one.

### 4.2 SAR (flood, Sentinel-1)

**Code** (processor.py:1384-1401):
```python
index = np.full_like(vv, np.nan, dtype="float32")
finite = np.isfinite(vv) & (vv > 0)
index[finite] = 10.0 * np.log10(vv[finite])   # raw GRD DN -> "dB"
index_calibrated = False
index_units = "dB_uncalibrated"
```
- **Literature method:** SAR flood detection uses **calibrated σ⁰ (sigma
  naught) backscatter** in dB, after (a) radiometric calibration (DN → σ⁰ via
  the product's calibration LUT/vector), (b) speckle filtering (Lee, Refined
  Lee, or similar), and (c) terrain/radiometric correction (RTC) using a DEM.
  Water shows up as *very negative* σ⁰ dB (specular reflection away from the
  sensor) — typically below roughly −15 to −20 dB depending on incidence
  angle, wind, and sensor.
- **What this code does:** `10*log10(raw_GRD_DN)` on the **raw digital
  numbers straight off the archive**, with **no calibration LUT applied at
  all**, **no speckle filter**, **no terrain correction**. This is confirmed
  by direct inspection of `processor.py` — there is no reference anywhere to a
  calibration vector, a `sigmaNought`/`betaNought` LUT, or any DEM-based
  correction; `grep`-ing the file for `gcp|rpc|reproject|WarpedVRT` (per
  `fix.md`'s original audit) only turns up the *geometric* GCP-warp added for
  BUG 1 (§3.4), which resolves the **coordinate system**, not the
  **radiometry**.
- **Where they differ, and whether defensible:** completely different
  physical quantity. Raw GRD DN is proportional to received power scaled by
  an arbitrary (per-swath, per-product) gain factor that has **not been
  removed**. `10*log10(DN)` is therefore not comparable across scenes, across
  swaths within one scene, or to any published water/land threshold in the
  literature — it is not backscatter in any physically meaningful sense. The
  class boundaries applied to it (`-13/-15/-18 dB`, processor.py:243-249) were
  presumably chosen by eyeballing output on one or two scenes and happen to
  produce plausible-looking imagery, but there is **no physical justification**
  for the specific numbers, and the same numbers will behave differently on
  every different scene/swath/incidence-angle combination. **This is not
  scientifically sound; it is a placeholder.**
- **The one thing that IS correctly handled:** the pipeline now *labels* this
  honestly. `index_calibrated: False`, `index_units: "dB_uncalibrated"` ride
  through the result (BUG 5), and `cross_validator.py`'s index-physics check
  explicitly refuses to threshold-compare a SAR/`index_calibrated is False`
  result as if it were NDWI (§5, verified against
  `tests/test_index_label_integrity.py`). The uncalibrated-ness is now
  *disclosed*, even though it is not *fixed*.

### 4.3 NDVI (earthquake / landslide, Sentinel-2)

**Code** (processor.py:1413-1423):
```python
index = _safe_ratio(b08 - b04, b08 + b04)   # (NIR-Red)/(NIR+Red)
threshold = NDVI_DAMAGE_THRESHOLD  # 0.2
index_calibrated = True
index_units = "NDVI_ratio"
```
- Formula matches the standard NDVI definition exactly (`(NIR−Red)/(NIR+Red)`).
- **Literature basis for using NDVI as an earthquake/landslide damage proxy:**
  low/dropping NDVI as a proxy for structural damage (rubble, bare exposed
  ground where vegetation/buildings used to register higher NDVI) is a
  documented remote-sensing technique for rapid post-disaster assessment, but
  it is a **coarse proxy**, not a direct damage measurement — NDVI cannot
  distinguish "building collapsed" from "field was already bare soil" or
  "seasonal senescence." The code does not attempt any before/after
  differencing (no baseline/pre-event NDVI comparison is fetched anywhere in
  this codebase) — it classifies a **single post-event scene's absolute NDVI**
  against a fixed threshold, which conflates disaster damage with naturally
  bare terrain (deserts, rock, urban areas that were always low-NDVI). This is
  a materially weaker method than the literature's typical bi-temporal
  NDVI-difference approach, and nothing in the code or docs flags this
  limitation to the operator.
- Same L1C→L2A revalidation gap as NDWI applies here too (0.2/0.1/0.0 boundaries
  untuned for L2A).

### 4.4 CRS handling / clip / vectorization / area

- **CRS resolution (BUG 1, fixed and unit-tested).** `_open_georeferenced`
  detects GCP-only georeferencing (S1 GRD) and wraps in a `WarpedVRT`
  targeting the AOI's UTM zone; `clip_to_polygon` refuses to clip
  (`return None`) if it still sees `crs is None or transform is identity`.
  Confirmed fixed by `tests/test_bug_fixes.py::test_bug1_gcp_raster_resolved`
  / `test_bug1_4326_poly_vs_utm_raster`.
- **Clip method:** reprojects the polygon into raster CRS, pre-windows to
  the polygon's pixel bbox before `rasterio.mask` rasterize (a real
  performance fix, verified byte-identical output vs. the unwindowed path).
- **Vectorization:** `rasterio.features.shapes` per class -> reproject WGS84
  -> `shapely.simplify(0.001 deg, preserve_topology=True)` -> drop polygons
  `< MIN_ZONE_AREA_KM2` (0.5 km2). Standard approach; fixed simplification
  tolerance (~10x a 10m pixel at the equator) is a reasonable, unstated
  choice.
- **Area calculation, CORRECTED SINCE THE PRIOR ANALYSIS.** `_polygon_area_km2`
  (processor.py:1616) reprojects to **EPSG:6933** (Cylindrical Equal-Area)
  before computing `.area` — scientifically correct. **The prior analysis's
  #2 finding — a silent `except Exception: return geom.area` fallback that
  mislabeled degrees² as km² — is now fixed.** Verified directly: the
  function has **no try/except at all** around the reprojection (confirmed
  by reading processor.py:1616-1638 in full); a reprojection failure now
  propagates as an exception rather than silently returning a
  4-orders-of-magnitude-wrong number under the same field name. This matches
  `CLAUDE.md`'s 2026-07-27 log entry ("`_polygon_area_km2` no longer
  degrades to mislabeled degrees²") and closes prior gap #2 (was: high
  severity, silent). **Closed.**

---

## 5. CONFIDENCE — Traced Completely

### 5.1 Every input `ConfidenceTracker` consumes

Unchanged arithmetic from the prior analysis (confirmed still accurate):

| Source | Where added | Evidence value | Weight |
|---|---|---|---|
| GDACS area-ratio match | cross_validator.py:279 | 0.9 (confirmed), 0.65 (partial), 0.4 (discrepancy) | 0.3 |
| GDACS event-present, no area | cross_validator.py:324 | 0.7 | 0.15 |
| USGS earthquake match | cross_validator.py:347 | 0.9 | 0.4 |
| Cloud cover | cross_validator.py:363/368/370 | 0.2 (>60%) / 0.6 (30-60%) / 0.95 (<30%) | 0.2 |
| Index-physics (NDWI only) | cross_validator.py:399/401/403 | 0.95 / 0.75 / 0.4 | 0.3 |
| Featherless expert opinion | cross_validator.py:444 | LLM's self-reported 0-1 | 0.25 |
| Interpretation confidence (IP4) | agent.py:793 | LLM's self-reported 0-1 | 0.2 |

Concerns (penalties only, no evidence) come from: GDACS ratio extremes (HIGH),
USGS magnitude >6.5 (HIGH), cloud >60% (CRITICAL) / 30-60% (MEDIUM), NDWI
negative + GDACS RED (CRITICAL), coverage <60% (HIGH), Featherless-reported
concerns (MEDIUM each), tier≥3 temporal spread (MEDIUM/HIGH), SAR uncalibrated
(MEDIUM, unconditional whenever `index_calibrated is False`).

**Note the asymmetry:** flood/SAR runs *always* pick up at least one MEDIUM
concern (SAR-uncalibrated) with **zero corresponding evidence**, because
`cross_validator.py`'s index-physics branch explicitly *skips* adding evidence
for SAR (cross_validator.py:404-417, `"SKIPPED"` status, no
`tracker.add_evidence` call). So a SAR-path run structurally cannot reach the
same evidence-to-concern ratio as an equally-good NDWI-path run — the
confidence gap between S1 and S2 runs is partly an artifact of what evidence
sources exist for each index, not purely a measure of result quality.

### 5.2 The exact arithmetic (confidence_tracker.py)

```python
weighted_sum = sum(e["value"] * e["weight"] for e in self.evidence)
total_weight = sum(e["weight"] for e in self.evidence)
base = weighted_sum / total_weight          # weighted average, NOT additive
for concern in self.concerns:
    base -= _SEVERITY_PENALTY[concern["severity"]]
return max(0.0, min(1.0, base))              # clamped
```
Weighted average of evidence, minus flat additive penalty per concern,
clamped [0,1]. Unchanged from the prior analysis.

### 5.3 Legibility fix — CLOSED since the prior analysis

**The prior analysis's central confidence finding — that `overall_confidence()`
cannot distinguish "no evidence gathered" from "evidence contradicts the
result," both reading as ~0.0 — prompted a real fix, now landed and verified
in code.** `confidence_tracker.py` now has:
```python
def confidence_basis(self) -> str:   # line 146
    ...  # returns "insufficient_evidence" / "evidence_contradicts" / "evidence_supports"
```
and `get_report()` (line 169, previously computed but never called anywhere)
is now actually invoked, and its `evidence_count`/`confidence_basis` are
folded into `structured` (confirmed by grep: `confidence_basis`/
`evidence_count` now appear inside `confidence_tracker.py`'s `get_report()`).
**The underlying weighted-average-minus-penalty arithmetic is unchanged —
this is a legibility fix, not a recalibration**; per `CLAUDE.md`'s own
framing, a true calibration pass needs accuracy data this repo doesn't have.

The specific failure mode documented live on 2026-07-26 (satellite confidence
0.0 read as "contradicted" when it actually meant "no evidence gathered,"
silently propagating to a report-level "HIGH" confidence) now has a legible
signal a downstream consumer *could* check — **provided that consumer
actually reads `confidence_basis`/`evidence_count`, which as of this pass,
hazard's `_normalise_satellite_payload` does not carry through** (only
`confidence` itself is copied at `agents/hazard/agent.py:89`;
`confidence_basis`/`evidence_count` are not — confirmed by direct read of
`agent.py:81-99`, §6.1 below). So the fix is real and closes the tracker's
own legibility gap, but the new legible signal doesn't yet cross the
satellite→hazard boundary — a narrower, more precise version of the original
ambiguity persists one hop downstream.

### 5.4 Is this a calibrated uncertainty estimate, or a heuristic?

**Still a heuristic, not a calibrated estimate**, unchanged assessment:
weights/penalties are hand-picked, not fit to historical accuracy data; no
confidence interval or distribution; a single point estimate with an ad hoc
penalty subtraction. It conflates "the disaster's true state is genuinely
uncertain" with "our own pipeline could not gather enough evidence" — both
still compress to the same 0-1 number. What's new since the prior pass is
that the tracker now at least *labels* which of its two failure modes
produced a low number (§5.3) — a legibility improvement, not a calibration
fix.

### 5.5 What is written into the result dict and the DB, and what downstream can see

**In `structured`:** `confidence` (float, rounded 4dp), `concerns`,
`validations`, `needs_verification`, `should_alert`, plus **new since the
prior pass**: `confidence_basis`, `evidence_count` (§5.3 — `get_report()` is
now actually called, closing the prior finding that `evidence_count` was
computed but discarded before leaving the pipeline).

**In the DB** (`_persist_satellite_result`): only `affected_area_km2`,
`total_zones` (now actually populated, §10 — was always NULL), `scene_id`
(now actually populated, §10 — was always NULL), and the artifact
URLs/bounds/bbox/risk_cities. `confidence`/`concerns`/`validations`/
`needs_verification`/`should_alert`/`confidence_basis`/`evidence_count` are
**still not DB columns** — they exist only in the JSON blob/`PipelineState`
hand-off. A fresh `GET /results` reading the DB directly still cannot see
any of them except the bare confidence float that rides through
`PipelineState["confidence_scores"]["satellite"]`. `damage_percent` — the
prior analysis's other always-NULL column — was **removed from the INSERT
entirely** rather than left permanently NULL (§10).

**Downstream can see:** the bare `confidence` float and the full
`satellite_result` dict in-memory for one graph run. **Downstream cannot
see** (from the DB or one hop later in the in-memory state): evidence count,
per-source evidence values, or the tracker's raw ledger, except via the new
`confidence_basis`/`evidence_count` fields — which exist in `structured` but,
per §5.3, are not carried into hazard's `analysis{}` dict either.

**CORRECTION to the immediately-prior `ANALYSIS.md`'s central confidence
finding.** That document stated, in its §4.5, that "`analyzer.py`'s
`run_parallel_analysis` (the function that actually computes hazard risk)
never reads that key from the satellite payload." **This was already false
when written.** Direct read of `agents/hazard/analyzer.py:342` this session:
```python
satellite_confidence = _to_float(analysis.get("confidence")) if analysis.get("confidence") is not None else None
```
and lines 409-414: if `satellite_confidence` is not `None` and is lower than
flood's own LLM-derived confidence, flood's confidence is **capped down** to
match it. `agents/hazard/agent.py:89` (`"confidence": p.get("confidence")`)
is exactly what feeds `analysis.get("confidence")` here — the full chain
does work. The commit that added this (`fa0d9bd`, "propagate satellite
confidence into hazard and report") is timestamped **2026-07-27 09:39:24**,
ten minutes **before** the prior analysis's own commit `2daddf6` at
**09:49:13** (confirmed via `git log --format=%ci` on both commits, this
session). See §11 gap #5 for the full reconciliation — this is a partial
fix (flood only, downward cap only, and `confidence_basis`/`evidence_count`
still don't cross the boundary), not a complete one, but the prior
document's "never reads it at all" framing was incorrect at the moment it
was written, not merely stale by the time of this pass.

---

## 6. DATA CONTRACT — Every Field in the Result Dict

| Field | Type | Unit / range | Produced at | Consumed by (grepped) |
|---|---|---|---|---|
| `event_id` | str (UUID) | — | agent.py:828 | all downstream, DB PKs |
| `status` | str | "complete" | agent.py:829 | node.py branch |
| `satellite_type` | str | "sentinel-1"/"sentinel-2" | agent.py:830 | hazard/analyzer.py:341 (branches `analyze_flood`'s index label) |
| `cloud_cover` | float\|None | 0-100 | agent.py:831 | hazard (none found); persisted to DB |
| `selection_reason` | str | free text | agent.py:832 | not consumed downstream (grepped, no hits outside satellite) |
| `index_type` | str | "NDWI"/"NDVI"/"SAR" | agent.py:833 | hazard/report intelligence.py prompts (label only, §below) |
| `water_percent` | float | 0-100 | agent.py:834 | hazard's `_normalise_satellite_payload` |
| `mean_index` | float | NDWI/NDVI: [-1,1]; SAR: dB (unbounded, uncalibrated) | agent.py:835 | hazard analyzer.py:339 as `mean_value` — **the exact field whose unit silently changes meaning by `satellite_type`, see below** |
| `class_counts` | dict | % per class label | agent.py:836 | not found consumed downstream by name (grepped hazard/impact/report — no hits) |
| `affected_area_km2` | float | km² | agent.py:837 | hazard analyzer.py:338, report db_client.py |
| `index_calibrated` | bool\|None | — | agent.py | **NOT carried into hazard's `_normalise_satellite_payload` at all** — confirmed by direct read of `agents/hazard/agent.py:81-99`, which copies `index_type`/`confidence`/`needs_verification` into `analysis{}` but never `index_calibrated`/`index_units` (§6.2, corrects the prior analysis's framing of this as "carried but unread" — it is not carried at all) |
| `index_units` | str | "NDWI_ratio"/"NDVI_ratio"/"dB_uncalibrated" | agent.py | same as above — **not carried through** |
| `coverage_percent` | float | 0-100 (should be 100.0 on any successful run) | agent.py:847 | not found consumed downstream |
| `full_aoi_coverage_percent` | float | 0-100 | agent.py:848 | not found consumed downstream |
| `coverage_tier` | int | 1-4 | agent.py:849 | not found consumed downstream |
| `temporal_spread_days` | int | days | agent.py:850 | not found consumed downstream |
| `acquisition_count` | int | count | agent.py:851 | not found consumed downstream |
| `processing_level` | str\|None | "L2A" or None | agent.py:852 | not found consumed downstream |
| `bytes_downloaded` | int | bytes | agent.py:853 | not found consumed downstream |
| `bbox` | list[float] | WGS84 degrees | agent.py | hazard (bbox scoping), impact |
| `bounds`/`bounds_leaflet`/`bounds_corners` | dict/list | WGS84 degrees | processor.py `_compute_bounds` | frontend map overlay |
| `region_boundary` | GeoJSON | — | agent.py | frontend |
| `risk_cities` | list[str] | — | agent.py | hazard, impact (city-scoped population/infra lookups) |
| `true_color_url`/`index_url`/`classification_url`/`geojson_url`/`image_url` | str (URL)\|None | — | agent.py | frontend, report map_generator |
| `cached` | bool | — | agent.py | not consumed downstream |
| `cities` | list | — | agent.py | **CHANGED SINCE PRIOR ANALYSIS.** No longer unconditionally empty — populated when `ENABLE_PER_CITY_ARTIFACTS=true` (default `false`, so empty by default in production still, but no longer a permanently dead path). See §10. |
| `interpretation` | dict\|None | — | agent.py | not found consumed by name downstream |
| `confidence` | float | 0-1 | agent.py | hazard's `analysis.confidence` — **READ, confirmed** (corrects the prior analysis's central claim; see §5.5) |
| `confidence_basis`/`evidence_count` | str/int | NEW fields (§5.3) | agent.py (via `get_report()`) | **not carried through** to hazard's `analysis{}` (§6.1) |
| `total_zones`/`scene_id` | int/str | NEW: now populated (§10) | agent.py | persisted to DB, was always NULL before this pass |
| `artifacts_incomplete`/`failed_artifacts` | bool/list[str] | NEW fields (§10) | agent.py | not yet confirmed read downstream |
| `concerns`/`validations`/`needs_verification`/`should_alert` | list/list/bool/bool | — | agent.py | not persisted to DB; in-memory only, one hop |
| `summary_message` | str\|None | free text | agent.py | not found consumed downstream by name |

### 6.1 Fields produced but never consumed anywhere downstream (confirmed by grep)

`selection_reason`, `class_counts`, `coverage_percent`,
`full_aoi_coverage_percent`, `coverage_tier`, `temporal_spread_days`,
`acquisition_count`, `processing_level`, `bytes_downloaded`, `cached`,
`interpretation`, `summary_message` — all present in the structured result,
no grep hit in `agents/hazard/`, `agents/impact/`, or `agents/report/` reads
them by name. Either intended for future consumers, dashboard-only, or
genuinely dead weight on the data contract. Also now in this category:
`confidence_basis`, `evidence_count`, `artifacts_incomplete`,
`failed_artifacts` — all new fields added by the 2026-07-27 fix pass, none
yet confirmed consumed downstream by name.

### 6.2 The label-vs-content defect class — where it still lives

The prior analysis's central "SAR-as-NDWI" finding had two parts: (a) inside
this agent, at `validation_input` construction, and (b) one hop downstream,
in hazard's deterministic fallback.

- **(a) — CLOSED, confirmed.** `agent.py`'s `validation_input` builds
  `index_type`/`index_calibrated`/`index_units` from the actual computed
  result and asserts `validation_input["index_type"] ==
  result["index_type"]` before calling the cross-validator (a hard
  assertion, not a soft check). This closes the specific mislabeling bug
  `root_cause.md`/`fix.md` originally found inside this agent.
- **(b) — FIXED 2026-07-28 (SYSTEM_ANALYSIS.md H#4), previously open.**
  `analyze_flood`'s LLM-facing prompt (lines 180-185) correctly branches
  `index_label`/`index_context` on `satellite_type`. Its **deterministic
  fallback** (lines 211-220, only reached if the LLM call fails) previously
  applied flat `flood_index > 0.5`/`> 0.3` NDWI-scale thresholds to
  `mean_value` regardless of whether it's an NDWI ratio or a SAR dB number.
  **Direction correction, superseding this document's own prior framing
  below:** this document previously described the effect as a false
  negative, reasoning that SAR dB values are "rarely in [0,1]" and would
  "almost never" exceed 0.5 — that assumed **calibrated** sigma0 backscatter,
  which is negative dB. This codebase's SAR index is explicitly
  **uncalibrated** (`10*log10(raw_GRD_DN)`, no radiometric LUT) and is
  **positive** — confirmed live, the 2026-07-26 e2e run recorded
  `mean_value = 23.6485` on the S1/SAR path. `flood_index > 0.5` is therefore
  trivially satisfied by ANY positive SAR reading, so this fallback path
  mechanically produced **CRITICAL** (a false-CRITICAL, not a false-negative)
  on every S1 run that reached it, independent of actual ground conditions.
  This was the exact same defect class `root_cause.md` originally documented
  (§4.3 there), just misdiagnosed in direction by this document's earlier
  draft.

  Also confirmed by direct read this session (Gate B,
  `agents/hazard/agent.py:81-99`, pre-fix): `_normalise_satellite_payload`
  did NOT carry `index_calibrated`/`index_units` into hazard's `analysis`
  dict — only `index_type`, `confidence`, `needs_verification` were copied.
  (`agents/hazard/ANALYSIS.md`'s claim that it DID carry these fields was
  the wrong side of this disagreement.) Fixed together: the adapter now also
  carries `index_calibrated`/`index_units`/`confidence_basis`/
  `evidence_count`, and the deterministic fallback reads `index_calibrated`
  to base the flood decision on `affected_area_km2` alone for uncalibrated
  SAR (never the raw index), confidence capped at 0.4, with an explicit
  anomaly recorded. The NDWI/calibrated-optical path is unchanged.

---

## 7. FAILURE MODES

| Failure | Trigger | Surfaced or swallowed? | What's returned | Can downstream tell it apart from success? |
|---|---|---|---|---|
| Region/city boundary unresolvable | boundary.py returns None/empty | Surfaced | `status != "complete"`, clearly distinguishable |
| CDSE auth fails after 3 attempts | `TokenManager().get()` None each time | Surfaced | clean error payload |
| No scenes found after widening to 30d | empty at every window | Surfaced | clean error payload |
| Insufficient coverage (no tier reaches 100%) | `process_satellite_imagery` returns failed | Surfaced, with gap geometry | unusually well-informed failure (area/bbox/cause) |
| **DB persist failure** | any exception in `_persist_satellite_result` | **CHANGED SINCE PRIOR ANALYSIS — now surfaced, not swallowed.** Retries up to `PERSIST_MAX_ATTEMPTS` (3) with backoff (1s, 3s); if every attempt fails, the function returns a non-`None` error string, and `_run_pipeline_sync` now treats any non-`None` return as a hard failure — the pipeline returns `status:"error"`, **never** `"complete"`, when persistence is exhausted. Confirmed by direct read of `agent.py`'s `_persist_satellite_result` (its own docstring states this contract) and cross-checked against `CLAUDE.md`'s 2026-07-27 log. **Closes prior gap #1 (was: can mislead silently and completely).** | **Closed.** |
| `_polygon_area_km2` reprojection failure | pyproj exception | **CHANGED — no longer swallowed.** No try/except around the EPSG:6933 reprojection at all (§4.4); an exception now propagates rather than degrading to a mislabeled degrees² value. **Closes prior gap #2.** | **Closed.** |
| Whole-pipeline unexpected exception | any uncaught exception in `_run_pipeline_sync`'s try block | Surfaced, but with no stack context — only `str(exc)` | Unchanged; debugging still requires server logs. |
| Cleanup failure (temp dir removal) | `shutil.rmtree` OSError | Swallowed, logged warning | Correctly non-fatal, unchanged. |
| R2 upload failure (any artifact) | `_put_file`/`_put_bytes` raise | **CHANGED SINCE PRIOR ANALYSIS.** `upload_all_results` now returns a `failed_artifacts` list; `structured` carries `artifacts_incomplete: bool` + `failed_artifacts: list[str]` (merged + per-city-prefixed). A consumer no longer has to null-check every URL individually to discover the run degraded — though a `None` URL can still slip through if a consumer ignores the new flags. **Closes prior gap #12, mostly.** | **Mostly closed.** |
| GDACS/USGS/Featherless-expert unreachable | network/parse error in cross_validator checks | Swallowed, logged, check skipped, no evidence added | Downstream sees a lower confidence number but (per §5.3) can now in principle tell "insufficient_evidence" apart from "evidence_contradicts" via `confidence_basis` — if it reads that field, which hazard currently does not (§6.2). |

**Summary:** two of the three "dangerous silent-success" paths the prior
analysis flagged (DB persist, area-unit degrade) are now closed by explicit
fixes; the third (R2 partial upload) now surfaces an explicit flag rather
than depending on a consumer's own null-checking discipline, though nothing
forces that flag to be checked (§11 gap #8).

---

## 8. EXTERNAL DEPENDENCIES

| Dependency | Timeout/retry | On unavailable |
|---|---|---|
| CDSE OAuth (Keycloak) | Single request, 30s timeout; `TokenManager.get()` proactively refreshes 90s before ~10min expiry (refresh_token grant, falls back to full re-auth); thread-safe via lock. `_authenticate_with_recovery` retries construction up to 3x. | Clean `_error(...)`. |
| CDSE catalogue search | 60s/30s timeout, single attempt (retry is at the search-window-widening level, not request level) | `None` returned on empty/error, triggers day-window widening. |
| CDSE download (Nodes per-band or whole-zip) | (connect=15s, read=90s); `_stream_to_file_with_retry` retries against `OUTAGE_GRACE_SECONDS` (7min) time budget, exp backoff 5s->30s cap. CDSE never honors HTTP Range — every retry re-fetches from scratch; per-band granularity limits blast radius for S2 only. | `None` after budget expires; caller tries next candidate or falls back Nodes->whole-zip. |
| geoBoundaries API | Metadata 20s, GeoJSON download 120s. Transient failures not cached (retryable); genuine 404s cached negative. | Falls through source chain: geoBoundaries -> Nominatim -> buffered-disk last resort (loud warning). |
| Nominatim | 30s/20s, no retry, throttled <=1 req/sec (policy compliance) | `None`, city/region skipped (non-fatal unless every city fails). |
| GDACS/USGS feeds | 15s, no retry | Logged, check skipped, no evidence/concern/crash. |
| LLM providers (Gemini -> Featherless chain -> AIML/Opus) | 30s per-model, `max_retries=0` (chain itself is the retry mechanism, up to ~10 attempts) | `None` on total exhaustion; every call site treats `None` as "fall back to deterministic default," confirmed at all six integration points. |
| Cloudflare R2 (boto3) | boto3 default retry/timeout | Per-artifact `None` on failure — now surfaced via `failed_artifacts` (§7), not silently absorbed. |
| Postgres/Neon | `asyncpg.connect()` per call (no pool), **now retried** up to `PERSIST_MAX_ATTEMPTS` with backoff (§7) | Exhausted retries now fail the pipeline honestly instead of a swallowed warning. |

---

## 9. RESOURCES — Memory/Disk/Network Profile (unchanged since the 2026-07-26 e2e run)

### 9.a Memory

Directly instrumented in code (`_mem_stage`/`memory_report`) — this is not
an estimate, it is what the pipeline itself logs per run. Per `CLAUDE.md`'s
cited live run (2026-07-26, Rawalpindi S1/SAR, event `88ad6095…`): **peak
RSS 9,611.3 MB (~9.6 GB) at the clip stage with only 2 tiles mosaicked**;
single-tile clip stages peaked ~5.1-5.6 GB; 2-tile mosaic-and-clip stages
peaked 7.3-9.6 GB. RSS scales roughly linearly with tile count because every
`_open_georeferenced`/`WarpedVRT` and `rasterio.merge` pass holds
full-resolution arrays (S1 GRD scenes are 28,000×21,000+ px) in memory
before the windowed clip trims them down. **Unchanged since the prior
analysis**, and directly relevant to §3's `ENABLE_PER_CITY_ARTIFACTS` flag
staying off by default — turning it on would multiply this peak.

### 9.b Disk

`TEMP_ROOT` under the system temp dir holds downloaded band files, exported
PNGs, and cached full-archive `.zip` files. `cleanup_event_temp` is called
on the success path and, per BUG 7, **guaranteed via a `finally` block** on
every exit path including exceptions. The `.zip` archive cache is separately
gated by `SATELLITE_KEEP_SCENE_CACHE` (default: delete in production) —
correctly designed to avoid unbounded disk growth. Unchanged since the
prior analysis.

### 9.c Network volume

Per-band Nodes download for S2 (~30-120 MB/band) vs. whole-archive fallback
for S1 (~1.2-1.7 GB/scene, confirmed by the 2026-07-26 e2e run: 4 full
archives, 5,490.7 MB total) because `_download_bands_via_nodes` explicitly
returns `None` for any `satellite_type != "sentinel-2"`. **Confirmed
unchanged this session** (§11 gap #6, still open) — every Sentinel-1
candidate scene is a full-archive download with no per-band resume benefit.

### 9.d Wall-clock time

The 2026-07-26 live e2e (S1/SAR, Rawalpindi, 4 coverage tiers, 4 full-archive
downloads) took **3244.1s (~54 min)** total, cited in `CLAUDE.md` as the
current S1 baseline. Unchanged since the prior analysis; not re-measured
this session (no code changed that would affect wall-clock time).

---

## 10. DEAD AND UNREACHABLE CODE — What Changed Since the Prior Pass

The prior analysis's own dead/unreachable-code section flagged five items.
Status now, verified by direct grep/read this session:

| Item | Prior status | Current status |
|---|---|---|
| `stance_engine.py`, `intelligence.decide_landsat_fallback` | Already deleted (BUG 6) | **Unchanged — still deleted.** Confirmed absent. |
| **Per-city artifact rendering** (`_render_per_city`, per-city upload loop) | Unreachable — `city_boundaries=None` hardcoded unconditionally | **CHANGED — now reachable, gated by `ENABLE_PER_CITY_ARTIFACTS` env var (default `false`).** Confirmed: `agent.py` defines the flag and uses `city_boundaries=city_polys if ENABLE_PER_CITY_ARTIFACTS else None` at the one call site. Still off by default (memory sizing not yet done, §9.a), but no longer a dead path with no way to turn it on — a considered, documented, reversible flag. `MIN_VALID_PIXEL_PERCENT`'s only live use (the per-city skip check) is now reachable, not permanently dead. |
| `COVERAGE_MOSAIC_THRESHOLD` / `MOSAIC_MAX_SCENES` / `select_mosaic_scenes` | Defined but unread by the tiered search; recommended delete-or-rewire | **CHANGED — deleted.** Confirmed zero hits for `select_mosaic_scenes`/`COVERAGE_MOSAIC_THRESHOLD` in `sentinel.py` via grep. Matches `CLAUDE.md`'s 2026-07-27 log ("were genuinely dead... and were deleted"). Closes the stale-documentation risk the prior analysis warned about. |
| `total_zones`/`damage_percent`/`scene_id` (persisted columns, always NULL) | Computed/available but never added to `structured`; bug not dead code | **CHANGED — `total_zones` and `scene_id` now populated.** Confirmed: `_persist_satellite_result`'s INSERT writes `total_zones` and `scene_id` as live columns. `damage_percent` was **removed from the INSERT entirely** rather than kept as permanently-NULL — it had no producer anywhere in the codebase, so this is the correct fix, and the DB column itself stays nullable so `agents/report/db_client.py`'s existing `.get("damage_percent") or 0` read is unaffected. |
| `verify_setup.py` | Deleted per migration | Unchanged — still absent. |

**New, not previously documented:** none found this pass beyond what's
already covered in §3/§6 above — the fix-pass commits were targeted at
the prior document's own gap list and didn't introduce new orphaned code as
a side effect.

---

## 11. RECONCILIATION — Every Prior Gap, Closed / Open / Was-Stale

Per the task brief, each of the prior `ANALYSIS.md`'s 13 numbered gaps (its
own gap-list section) plus its two headline confidence findings,
independently re-verified against current code:

| # | Prior finding | Status now | Evidence |
|---|---|---|---|
| 1 | DB persist failure swallowed, pipeline still "complete" | **CLOSED.** Retries 3x with backoff; exhaustion now fails the pipeline. | agent.py `_persist_satellite_result`; CLAUDE.md 2026-07-27 log |
| 2 | `_polygon_area_km2` silently degrades to mislabeled degrees² | **CLOSED.** No try/except around the reprojection; failure now propagates. | processor.py:1616-1638 |
| 3 | SAR uncalibrated, thresholds have no physical basis, but still classified/shipped | **STILL OPEN, by design.** Honestly labeled (`index_calibrated:false`), not fixed — real calibration/speckle-filter/RTC work is out of scope for a correctness pass. | processor.py SAR index code, §4.2 |
| 4 | Hazard's deterministic flood fallback applies NDWI thresholds to SAR dB regardless of `satellite_type` | **STILL OPEN.** Confirmed by direct read this session: `agents/hazard/analyzer.py:211-220`'s fallback is unconditional; the LLM-facing prompt (180-185) is correct but the fallback isn't. Compounded by `index_calibrated`/`index_units` never reaching hazard's `analysis` dict at all (§6.2). | agents/hazard/analyzer.py:173-224; agents/hazard/agent.py:81-99 |
| 5 | Satellite confidence computed but never read by hazard's actual risk computation | **WAS WRONG WHEN WRITTEN — the fix had already landed.** Direct read of `agents/hazard/analyzer.py:342` (`satellite_confidence = _to_float(analysis.get("confidence"))`) and lines 409-414 (flood confidence is capped at satellite's confidence when satellite's is lower) confirms hazard **does** read it. Commit `fa0d9bd` ("propagate satellite confidence into hazard and report") landed at 2026-07-27 09:39:24, **ten minutes before** the prior analysis commit `2daddf6` at 09:49:13 (confirmed via `git log --format=%ci`). **Partial fix, not complete**: the cap only applies to flood's confidence, downward only, and only if satellite's confidence is lower — earthquake/landslide are satellite-independent by design (per `root_cause.md`'s original intent, arguably correctly excluded) — and `confidence_basis`/`evidence_count` (the newer legibility fields, §5.3) still don't cross the boundary. | agents/hazard/analyzer.py:333-414; git log --format=%ci fa0d9bd 2daddf6 |
| 6 | `ConfidenceTracker` can't distinguish "no evidence" from "evidence contradicts" | **CLOSED at the tracker level, not fully closed end-to-end.** `confidence_basis()`/`get_report()` now compute and surface the distinction in `structured` (§5.3). But hazard's normalise step doesn't carry `confidence_basis`/`evidence_count` through, so a downstream consumer still can't see the distinction unless it reads the satellite result directly. | confidence_tracker.py; agents/hazard/agent.py:81-99 (fields not carried) |
| 7 | NDWI/NDVI thresholds untuned for L2A | **STILL OPEN**, explicitly acknowledged, science-phase follow-up, not attempted in this pass. | processor.py comments; CLAUDE.md |
| 8 | NDVI single-scene absolute threshold, not bi-temporal | **STILL OPEN**, real feature gap, not a tuning fix. | processor.py NDVI code |
| 9 | S1 has no per-band Nodes path, every candidate a full archive | **STILL OPEN**, confirmed unchanged: `_download_bands_via_nodes` still unconditionally returns `None` for non-S2. | processor.py |
| 10 | `total_zones`/`damage_percent`/`scene_id` always NULL | **CLOSED for `total_zones`/`scene_id`** (now populated); `damage_percent` **correctly removed** from the INSERT rather than left perpetually NULL, since it had no producer. | agent.py INSERT statement; §10 above |
| 11 | Per-city rendering permanently unreachable, mosaic set-cover orphaned | **BOTH CLOSED**, differently: per-city is now a feature flag (reachable, off by default); orphaned mosaic set-cover code was deleted outright. | §10 above |
| 12 | R2 partial upload failures silently `None`, no degraded-success flag | **CLOSED.** `artifacts_incomplete`/`failed_artifacts` now ride in `structured`. | r2_upload.py; agent.py |
| 13 | Coverage tier day windows not derived from revisit cycle | **Resolved as a design review, not a code change** (matches the prior document's own framing of this as a discussion item, not a defect). Live-measured against CDSE: S2 tiers are a reasonable match; S1's ~11-day actual revisit confirms tiers 2/3 are structurally near-no-ops for S1, left unchanged because the cost is negligible and any change would touch validated live-tested behavior. | CLAUDE.md's "Tier-window revisit analysis" section |

**Net:** of 13 numbered gaps + 2 headline confidence findings, **6 are fully
closed** (#1, #2, #10, #11, #12, and the tracker-level half of #6), **1 was
already wrong when the prior document was written** (#5 — its "diagnosed
only, not fixed" framing was itself stale; the actual fix commit predates
the diagnosis commit), **1 is resolved as a documented design decision
rather than a code change** (#13), and several remain genuinely open (#3
SAR calibration, #4 hazard's SAR-threshold fallback, #6's cross-boundary
half, #7 L2A threshold revalidation, #8 bi-temporal NDVI, #9 S1 Nodes path).

---

## 12. THE GAP LIST — Prioritized (current state)

| # | Issue | Type | Severity | Effort | Evidence |
|---|---|---|---|---|---|
| 1 | **DB persist failure is swallowed as a warning while the pipeline still returns `status:"complete"`.** A transient Neon outage during the INSERT produces a fully "successful" run with no durable `satellite_results` row; any consumer reading `GET /results` or re-querying the DB sees nothing, while the in-memory graph state (and any caller who got the direct return value) believes the run succeeded. | correctness/contract | **Can mislead silently and completely** — a downstream operator has no way to detect this without independently cross-checking the DB. | Low (raise/mark-failed on persist exception, or retry with backoff) | agent.py:132-133 |
| 2 | **`_polygon_area_km2`'s exception fallback returns degrees² mislabeled as km².** If the pyproj equal-area reprojection ever throws, `affected_area_km2` silently becomes a value off by ~4 orders of magnitude at mid-latitudes, with the exact same field name/type as a correct value. | correctness | **Very high, very silent** — no flag, no unit change, indistinguishable from a real (tiny or huge) area to any downstream reader. | Low (raise instead of degrading, or tag the result with an explicit `area_calculation_failed` flag) | processor.py:1622-1638 |
| 3 | **SAR backscatter is uncalibrated raw-DN log, with no calibration LUT, no speckle filter, no terrain correction — the class thresholds applied to it (-13/-15/-18 dB) have no physical basis given that input.** The pipeline now honestly *labels* this (`index_calibrated: False`), but still *computes and ships a classified hazard map and a `mean_index` number* as if the thresholds meant something, for every Sentinel-1 flood run. | science | **High** — every S1 flood analysis (the case CDSE/cloud-routing selects specifically *because* optical is unusable) ships a classification/area/mean-index that is not scientifically defensible, even though it is correctly flagged as uncalibrated in the metadata. | High (real calibration + speckle filter + RTC is a substantial remote-sensing engineering task) | processor.py:1384-1401, 233-267; CLAUDE.md's own SAR notes |
| 4 | **FIXED 2026-07-28 (SYSTEM_ANALYSIS.md H#4).** Hazard agent's deterministic flood fallback previously applied NDWI-scale thresholds to a SAR-dB `mean_value` without checking `satellite_type`, even though the satellite agent correctly labels `index_calibrated`/`index_units`. **Correction to this document's original framing:** this document previously (incorrectly) described the failure mode as a false-negative, reasoning that `flood_index > 0.3`/`> 0.5` would "almost never" be true for SAR dB values. That assumed calibrated sigma0 backscatter, which is negative dB. This codebase's SAR index is UNCALIBRATED (`10*log10(raw_GRD_DN)`) and is **positive** (confirmed live: `mean_value = 23.6485` on the 2026-07-26 e2e S1 run) — so the old threshold was trivially satisfied by any positive SAR reading, mechanically producing **CRITICAL** (a false-CRITICAL, not a false-negative) on every S1 run that reached this fallback, independent of actual ground conditions. Fixed: the fallback now reads `index_calibrated` and, for uncalibrated SAR, bases the decision on `affected_area_km2` alone (never the raw index), confidence capped at 0.4, with an explicit anomaly recorded — mirroring the label-awareness the LLM-facing prompt already had. | contract | **High but conditional** — only fires when the hazard agent's LLM call fails and the deterministic fallback runs. | Low (branch the fallback on `satellite_type`/`index_calibrated`, mirroring what the LLM prompt already does) — **done** | agents/hazard/analyzer.py:180-185 (correct) vs 211-215 (fixed 2026-07-28) |
| 5 | **Satellite `confidence` (and its constituent evidence/concerns) is computed carefully but never read by the hazard agent's actual risk computation.** `_normalise_satellite_payload` carries `confidence` into the normalized payload, but `run_parallel_analysis` never extracts it; hazard's own self-generated confidence is entirely disconnected from how uncertain satellite was. | contract | **High, and demonstrated live** — the cited 2026-07-26 run produced satellite confidence 0.0 alongside a report-level "HIGH" confidence_level, i.e. the system asserting confidence it structurally cannot have. | Medium (thread `satellite_data.get("confidence")` into hazard's own confidence aggregation) | agents/hazard/agent.py:89 (present) vs analyzer.py:338-341 (not read) |
| 6 | **`ConfidenceTracker.overall_confidence()` cannot distinguish "no evidence was gathered" from "evidence strongly contradicts the result" — both read as low/0.0.** This is a heuristic morale score, not a calibrated uncertainty estimate: weights/penalties are hand-picked, not fit to any accuracy data. | science/contract | **High** — the exact mechanism behind the "confidence 0.0 / report HIGH" incident cited in this repo's own `CLAUDE.md`. | Medium (surface `evidence_count`/a distinct data-completeness signal in the result dict; it already exists inside the tracker but is discarded before persisting) | confidence_tracker.py:114-132, 146-154; agent.py:876-882 (never calls `get_report()`) |
| 7 | **NDWI/NDVI thresholds (0.3/0.5 water, 0.2 damage) were tuned against L1C top-of-atmosphere reflectance and have not been revalidated since the pipeline switched Sentinel-2 to L2A surface reflectance.** Acknowledged in-code and in `CLAUDE.md`, still unresolved. | science | **Medium-high** — affects every S2-path flood/earthquake/landslide run; direction of the bias (more or less permissive) is plausible but unverified. | Medium (a dedicated science-validation pass against known-ground-truth L2A scenes) | processor.py:148-151, 212-213; CLAUDE.md 2026-07-26 section |
| 8 | **NDVI-based earthquake/landslide damage detection is a single-scene absolute-threshold classification, not a bi-temporal (before/after) difference.** This conflates disaster damage with naturally low-NDVI terrain (bare soil, urban, desert) and is materially weaker than the standard literature approach; nothing in the result or documentation flags this limitation to an operator reading the classification map. | science | **Medium** — every earthquake/landslide S2 run is subject to this, silently. | High (requires fetching + differencing a pre-event baseline scene — a real feature addition, not a tuning fix) | processor.py:1413-1423 |
| 9 | **Sentinel-1 has no per-band Nodes download path — every S1 candidate scene is a full 1.2-1.7 GB archive fetch**, unlike S2's per-band resilience/resume granularity. Confirmed: `_download_bands_via_nodes` unconditionally returns `None` for `satellite_type != "sentinel-2"`. | performance | **Medium** — makes every S1 (i.e. every cloud-obscured, often the most urgent) run slower and less outage-resilient than the equivalent S2 run. | Medium (extend the Nodes-tree mapping to the S1 GRD `measurement/*.tiff` layout) | processor.py:615-617 |
| 10 | **`total_zones`, `damage_percent`, `scene_id` are named as DB columns in the INSERT but never populated in `structured` — every row persists them as NULL**, silently thinner than the schema implies. | contract | **Low-medium** — no wrong data, just missing data an operator reading the DB directly might expect to be present. | Low (three one-line additions to `structured`) | agent.py:106-122, 763-767 |
| 11 | **Per-city artifact rendering is fully implemented but permanently unreachable** (`city_boundaries=None` hardcoded at the one call site) — `MIN_VALID_PIXEL_PERCENT`'s only remaining live use lives inside this dead path. `select_mosaic_scenes`/`COVERAGE_MOSAIC_THRESHOLD`/`MOSAIC_MAX_SCENES` are similarly orphaned by the newer tiered-coverage design. | dead code | **Low** (no output is wrong, just unused engineering effort and stale-reading documentation) | Low (delete, or explicitly re-wire and document as active) | agent.py:602; processor.py:2097-2160; sentinel.py:797-880 |
| 12 | **R2 per-artifact upload failures degrade silently to `None` URLs inside an otherwise-"complete" result**, and downstream/frontend code must independently null-check every artifact URL rather than being told the run degraded. | contract | **Low-medium** — visible in the payload if checked, but not flagged as a distinct degraded-success state. | Low (add an explicit `artifacts_incomplete: bool`/list of failed keys to `structured`) | r2_upload.py `_put_file`/`_put_bytes`; agent.py:665-673 |
| 13 | **Coverage tiers' day windows (0/±3/±7/±14) are not derived from either satellite's actual revisit cycle** — S1's 6-12 day typical revisit makes a ±3-day tier 2 plausibly almost-always empty in practice for S1, silently collapsing tier 2 to a no-op most of the time. | correctness (design) | **Low-medium** — not incorrect, but likely an unintended near-no-op tier that adds search latency for little benefit on the S1 path specifically. | Low (a design review, not a code change) | sentinel.py:729-734; CLAUDE.md's own tier-1-3-exactly-0% analysis |
| — | **RESOLVED (design review, 2026-07-27, live-measured — see CLAUDE.md's "Tier-window revisit analysis" section for the full writeup).** S2's combined-constellation revisit (~5d) is reasonably matched by the existing tiers — no change warranted. S1's same-relative-orbit revisit was **measured live against CDSE** (Rawalpindi + Karachi, last 90 days, `Attributes`-expanded OData query): overwhelmingly **11 days**, with occasional 6-day and rare 12-day gaps — i.e. still effectively single-satellite cadence over Pakistan, NOT the newer post-2026-06-24 Sentinel-1C/1D 6-day constellation repeat (that faster cadence is concentrated over Europe per ESA/ASF acquisition planning, not global). This confirms tiers 2 (±3d) and 3 (±7d) are structurally unable to find a second same-orbit S1 pass over this AOI class — 7 < 11 regardless of window tuning — matching CLAUDE.md's live tier-1-3-exactly-0% finding exactly. **Left unchanged**: the empty-tier cost is cheap (zero downloads attempted, a single skipped loop iteration — `build_coverage_tiers`'s `if not in_window: continue`), and any widening would touch validated, live-tested tier behavior for an unverified benefit. This measurement is also a direct input to the planned S1 change-detection work (§10 #8's bi-temporal NDVI gap and any future S1 pre/post differencing), since a valid pre-event reference scene must come from the same relative orbit as the post-event one. | correctness (design), resolved | — | — (analysis only, no code change) | this session's live CDSE probe; CLAUDE.md |

---

## Appendix — Confirms vs. Corrects

**Confirms** (re-verified against current code): BUG 1 (GCP/CRS clip
collapse) fixed and unit-tested; BUG 2 (valid-pixel coverage + SCL masking)
implemented as described; BUG 3 (tiered temporal-coherence search,
100%-or-fail) implemented as described; BUG 4 (dedup/pre-intersection/
doomed-streak) implemented as described; BUG 5 (`index_calibrated`/
`index_units` contract) implemented, cross-validator correctly gates on it
for evidence but the label still doesn't reach hazard's fallback logic
(§11#4, still open); BUG 6 (dead code deletion) confirmed; BUG 7 (guaranteed
cleanup + RSS instrumentation) confirmed; CDSE `TokenManager` proactive
refresh confirmed implemented as described.

**Corrects the immediately-prior `ANALYSIS.md`:** its gap #5 (satellite
confidence never read by hazard) was stated as an open, "diagnosed only, not
fixed" finding, but the fix (`fa0d9bd`) had landed ten minutes before that
document's own commit — confirmed by `git log --format=%ci` on both commits.
Its gaps #1, #2, #6 (tracker legibility), #10, #11, #12 all prompted real
fix commits that landed after it was written and are now closed or
substantially closed, verified against current code rather than against the
CLAUDE.md changelog's own claims. Its gap #4 (hazard's SAR-threshold
fallback) remains open and independently re-confirmed this session by
direct code read, not merely re-cited.

**New finding, not previously documented anywhere in this repo:** the
confidence legibility fix (`confidence_basis`/`evidence_count`, §5.3) does
not cross the satellite→hazard contract boundary — `_normalise_satellite_payload`
was not updated to carry the two new fields through, so the specific
ambiguity they were built to resolve still exists one hop downstream, in a
narrower form than before the fix (now: "hazard sees the raw confidence but
not its basis," rather than "hazard sees nothing"). Also new:
`index_calibrated`/`index_units` were never in `_normalise_satellite_payload`'s
carried field set even before this pass — the prior analysis attributed
hazard's SAR-threshold bug purely to the fallback not checking
`satellite_type`, but the fallback additionally has no access to the more
precise `index_calibrated`/`index_units` fields even if it were fixed to
check `satellite_type` (§6.2, §11#1).
