# Satellite Agent — Deep Analysis (Post-Migration, 2026-07-27, Session 2)

**REVISION NOTE (this session):** The prior version of this document (below)
was itself a pre-fix snapshot — it audited the agent at commit `2daddf6`, then
six fix commits (`7eb23f0`/`35bdb40`/`e90fc68`/`8825fc5`/`c9e6a1d`/`48aaaf8`/
`6300623`/`b7fdad4`) landed closing most of its gaps, then this document was
merged to `main` (`fd7a08c`) alongside those fixes **without being rewritten**.
So the text below describes gaps #1, #2, #6, #10, #11, #12 as open when they
are now closed, and its gap #5 ("satellite confidence never read by hazard")
was **already stale when it was written** — `fa0d9bd` had added that read one
commit earlier. See `SYSTEM_ANALYSIS.md` §B for the full, current trace of the
confidence chain (short version: hazard's flood-confidence cap now works on
the live in-memory path; report's `satellite_confidence` field is dead code on
the live DB-fetch path because no DB column exists to populate it from). Gaps
#3, #4, #7, #8, #9 below remain open and accurate. New findings not below:
report's `db_context_to_report_context` discards the real `index_type`/
`mean_value` (replaces them with `"database_result"`/`0`) on every DB-fetched
report — see `SYSTEM_ANALYSIS.md` §C/§F.

---

# Satellite Agent — Deep Analysis (Post-Migration, 2026-07-27) [ORIGINAL, SEE REVISION NOTE ABOVE]

**Scope:** `agents/satellite/` only, as the code exists on `main` after the
Band→LangGraph migration and the 2026-07-26/27 coverage-correctness pass. Every
file was read in full: `node.py`, `agent.py`, `sentinel.py`, `processor.py`,
`confidence_tracker.py`, `cross_validator.py`, `intelligence.py`, `boundary.py`,
`geoboundaries.py`, `r2_upload.py`, plus the `tests/` directory and the
downstream consumers in `agents/hazard/`, `agents/impact/`, `agents/report/`.

**Method.** This document trusts the code over `CLAUDE.md`/`CODEBASE.md`/
`root_cause.md`/`fix.md` wherever they disagree, and says so explicitly.
`root_cause.md` and `fix.md` are pre-migration documents (they describe a Band
adapter this agent no longer has) but their **code-level claims** about the
satellite pipeline (CRS bugs, SAR calibration, confidence math) were
independently re-verified against the current source and are cited only where
confirmed still true.

**No code was changed to produce this document.**

---

## 1. Execution Flow

### 1.1 Entry point

`node.py:satellite_node(state)` is the only way the pipeline is invoked in
production. It:
1. Builds a `ProcessDisasterInput(event_id, location, disaster_type, magnitude)`
   from `PipelineState` — `raw_message` is **never populated** here (only
   declared on the Pydantic model), so `run_pipeline` always synthesizes
   `raw_message = f"{disaster_type} in {location}"` at `agent.py:450`.
2. Calls `await run_pipeline(params)` → `asyncio.to_thread(_run_pipeline_sync, params)`.
3. Parses the JSON string result. If `status != "complete"`, returns
   `{"status": "failed", "errors": [...]}"` (appended, not overwritten). On
   success, returns `{"satellite_result": result, "status": "hazard",
   "progress": 25, "confidence_scores": {..., "satellite": result["confidence"]}}`.

This is a thin, correct adapter — no logic lives here beyond the state→params
translation and the failure/success branch. It cannot itself produce a
false-success: if `_run_pipeline_sync` returns anything but `"complete"`,
`node.py` marks the graph `failed`.

### 1.2 `_run_pipeline_sync` — the real pipeline (agent.py:401-941)

Call graph, in order, with every branch point:

```
_run_pipeline_sync(params)
├─ [event_id in _completed_event_ids?] → return cached "complete" (process-once guard)
├─ ConfidenceTracker() created (per-event ledger)
├─ INTEGRATION POINT 1: intelligence.parse_disaster_input(raw)         [LLM, best-effort]
│    branch: profile.ambiguous AND (loc_missing OR type_missing)
│      → return _clarification(...)  [status: clarification_needed]
│    else: profile may enrich location/disaster_type if caller args were thin
├─ get_region_boundary(location)                                       [boundary.py]
│    branch: None → return _error("Could not resolve region boundary")
├─ detect_risk_cities(location, disaster_type)                         [curated map or headline fallback]
│    branch: empty → return _error("No risk cities detected")
├─ get_risk_city_boundaries(location, cities)                          [boundary.py]
│    branch: empty → return _error("Could not resolve any risk-city boundaries")
├─ merge_risk_boundaries(city_polys) → merged (shapely unary_union)
│    branch: None → return _error("Failed to merge risk-city boundaries")
├─ get_analysis_bbox(merged) → bbox
│    branch: None → return _error("Failed to compute analysis bbox")
├─ check_demo_cache(event_id)                                          [r2_upload.py — only 3 literal ids]
│    branch: hit → return "complete" with cached_url, boundaries resolved but NO real analysis
├─ _authenticate_with_recovery(event_id, location)                     [≤3 attempts, LLM-guided]
│    → sentinel.TokenManager per attempt; branch: None after 3 → return _error(...)
├─ select_satellite(disaster_type, bbox, token_manager.get())          [sentinel.py — cloud-aware]
│    branch: cloud_cover > 30% → sentinel-1, else sentinel-2 (physics overrides disaster-type hint)
├─ INTEGRATION POINT 2: intelligence.devise_satellite_strategy(...)     [LLM, LOGGED ONLY — no branch taken on it]
├─ _search_with_recovery(event_id, bbox, satellite_type, merged)        [7→14→30 day widening]
│    branch: no scenes ever found → return _error("No {satellite_type} imagery found")
├─ backfill_uncovered_cities(scenes, city_polys, satellite_type, aoi_geom=merged)
│    [re-queries per under-covered city, widens 14d/30d, appends+re-scores]
├─ process_satellite_imagery(selection, scenes, bbox, merged, event_id, token_manager,
│                             disaster_type, city_geoms=[...], city_boundaries=None, tracker=tracker)
│    [see §1.3 for the tiered coverage search inside this call]
│    branch: result is None → return _error("Satellite imagery processing failed")
│    branch: result.status == "failed" and reason == "insufficient_coverage"
│      → _recover("coverage_insufficient", ...) [LLM advisory, non-blocking]
│      → return _coverage_failure(...) [status: error, reason: insufficient_coverage, gap geometry attached]
├─ upload_all_results(event_id, {true_color, index_map, classification, geojson}) [r2_upload.py]
├─ per-city upload loop over result.get("cities", [])                   [ALWAYS EMPTY — see §9]
├─ asyncio.run(cleanup_event_temp(event_id))                            [deletes temp working tree]
├─ validation_input built (index_type/index_calibrated/index_units/mean_index/...)
│    assert validation_input["index_type"] == result["index_type"]     [hard assertion, see §5]
├─ cross_validator.validate_all(validation_input, disaster_type, bbox, tracker)
│    [GDACS / USGS / cloud / index-physics / coverage / Featherless-expert — see §2, §4]
├─ INTEGRATION POINT 4: intelligence.interpret_results(...)              [LLM, folded into tracker as evidence]
├─ confidence = round(tracker.overall_confidence(), 4)                  [AUTHORITATIVE — not the LLM's own number]
├─ INTEGRATION POINT 6: confidence gate
│    branch: confidence < 0.6 OR needs_verification() OR should_alert_team()
│      → _recover("low_confidence", ...) [LOGGED ONLY — result is returned regardless]
├─ structured{} built (mirrors satellite_results DB columns + extras — see §5)
├─ INTEGRATION POINT 5: intelligence.generate_band_message(...)         [LLM, natural-language summary only]
├─ _persist_satellite_result(event_id, structured)                      [DB write, best-effort — see §6]
├─ _completed_event_ids.add(event_id)
└─ return json.dumps(structured)                                        [status: "complete"]

except Exception → return _error(event_id, f"Unexpected error: {exc}")   [BLANKET CATCH — see §6]
finally:
  → asyncio.run(cleanup_event_temp(event_id))  [guaranteed cleanup, BUG 7]
  → log memory_report()
```

### 1.3 `process_satellite_imagery` — the tiered coverage search (processor.py:2163-2448)

This is the most decision-dense function in the agent:

```
process_satellite_imagery(selection, scene_metadata, bbox, merged_polygon, event_id, token, disaster_type, ...)
├─ dedupe_by_acquisition(scenes)                     [collapses GRD/GRD-COG twins of one acquisition]
├─ filter scenes to those with _scene_aoi_overlap(scene, aoi_shape) > 0.0
│    branch: none left → return {"status": "failed", "reason": "insufficient_coverage", ...} immediately
├─ build_coverage_tiers(scenes, satellite_type)       [tier 1: same date+orbit, tier 2: ±3d, tier 3: ±7d, tier 4: ±14d any-orbit]
│    branch: no parseable dates → single fallback tier (4, None, scenes)
└─ for (tier, orbit_dir, group) in tiers:              [tries tier 1 first, stops at first tier reaching 100%]
     for scene in group:                               [best-first within the tier]
       ├─ _attempt_clip(selection, accepted+[scene], merged_polygon, ...)  [download→stack→clip]
       │    branch: clip fails → doomed_streak++; abort tier if streak >= DOOMED_DOWNLOAD_LIMIT (3)
       ├─ compute_coverage(trial_clip)                 [interior_coverage_percent — the REAL pass/fail metric]
       │    branch: gained coverage <= 0.01% and accepted non-empty → doomed_streak++ (this scene added nothing)
       ├─ accept if gained > 0.01%: accepted = trial; cov = trial_cov
       └─ branch: cov["covered"] (interior coverage == 100.0%) → break inner loop, tier succeeds
     branch: tier succeeded → render (_render_clip: indices→PNG→vectorize→bounds), attach coverage_tier/
             temporal_spread_days/acquisition_count, add tier≥3 confidence-lowering concern via tracker,
             add SAR-uncalibrated concern via tracker if applicable, return merged_result
  [no tier ever reached 100%] → return {"status": "failed", "reason": "insufficient_coverage",
                                          "coverage_percent": best interior %, "gaps": [...], "gap_cause": {...}}
```

**Key fact, confirmed in code:** there is **no partial-coverage risk output**.
Either some tier's cumulative mosaic reaches exactly 100% interior valid-pixel
coverage, or the whole call fails with `insufficient_coverage` and gap
geometry. This matches `CLAUDE.md`'s BUG 3 description precisely.

---

## 2. Decision Logic — Every Branch and Its Basis

| Decision | Driver | Deterministic or LLM? | Basis for the value |
|---|---|---|---|
| **S1 vs S2 selection** (`select_satellite`, sentinel.py:126) | Real observed cloud cover from a lightweight metadata peek (`_peek_cloud_cover`); `CLOUD_COVER_THRESHOLD = 30.0` | **Deterministic.** LLM's `devise_satellite_strategy` result is logged only, never overrides this. | **Arbitrary.** No citation, no calibration study in the code or docs. 30% is a round-number guess. It is at least *consistently applied* (same threshold gates the S2 catalogue filter, sentinel.py:944-948). |
| **Date window widening** (`_search_with_recovery`, agent.py:354) | Fixed sequence 7→14→30 days | **Deterministic** (the LLM's `handle_anomaly("no_sentinel_scenes")` call is invoked but its `expand_date_range` hint is never read — the widening sequence is hardcoded regardless of what the LLM recommends) | Arbitrary round numbers; no stated rationale. |
| **Coverage-mosaic trigger** (`COVERAGE_MOSAIC_THRESHOLD`) | 85.0% (`sentinel.py:51`, `processor.py:66`) | Deterministic | Was 60%, raised to 85% after the Mindanao incident (`CLAUDE.md` Step 10 FIX A) — an empirically-motivated correction, but the specific number 85 (not 80, not 90) is not derived from anything; it's "high enough that the Mindanao case, whose best tile covered ~34%, definitely mosaics." **Note: this constant is dead in the current tiered-coverage design** — `process_satellite_imagery` (§1.3) no longer branches on it at all; the real acceptance test is `compute_coverage()["covered"]` (interior == 100%). `COVERAGE_MOSAIC_THRESHOLD` and `MOSAIC_MAX_SCENES` are now unused by the tiered search (see §9). |
| **Candidate rejection within a tier** (`_attempt_clip`/doomed-streak) | `DOOMED_DOWNLOAD_LIMIT = 3` consecutive non-contributing downloads aborts the tier | Deterministic | Arbitrary round number, no stated basis. |
| **Valid-pixel candidate floor** (`MIN_VALID_PIXEL_PERCENT = 5.0`) | processor.py:72 | Deterministic | **Now effectively dead** for the merged/main path — the tiered search's real bar is 100% interior coverage (`compute_coverage`), not this 5% floor. It IS still live in `_render_per_city`'s per-city skip check (processor.py:2129) — an inconsistency: the merged AOI demands 100%, but a per-city sub-clip only demands 5% (though per-city rendering is itself disabled by default, §9). |
| **Tier escalation** (`build_coverage_tiers`, sentinel.py:729-734) | `COVERAGE_TIERS = ((1,0,True),(2,3,True),(3,7,True),(4,14,False))` | Deterministic | The day windows (0/±3/±7/±14) are round numbers with no cited source. The *design* (same-orbit-first, relax orbit only at the widest tier) is a reasoned engineering choice (documented, verified live per `CLAUDE.md`'s 2026-07-26 tier-1-3 exactly-0% analysis), but the specific day boundaries are not derived from any Sentinel revisit-cycle calculation (S1's revisit is 6-12 days depending on constellation; a ±3d tier-2 window is narrower than one revisit cycle, so tier 2 will very often be empty for S1 — the tiers may be effectively S2-tuned). |
| **Classification class boundaries** (`_CLASS_SCHEMES`, processor.py:233-267) | NDWI: 0.0/0.3/0.5; SAR: -13/-15/-18 dB; NDVI(quake): 0.2/0.1/0.0; NDVI(landslide): 0.2/0.1/0.0 | Deterministic | **Arbitrary / literature-adjacent but unvalidated for this data.** NDWI 0.3 is a commonly-cited water threshold in remote-sensing literature (McFeeters 1996 uses >0 for open water; 0.3 is a stricter common variant) — plausible but not cited in-code. The SAR dB thresholds (-13/-15/-18) have **no defensible basis at all** given the index is uncalibrated raw-DN log (see §3.2) — they were tuned, if at all, against a physically different quantity than what the pipeline now computes. NDVI 0.2 "damage" threshold is a round number with no citation. **CLAUDE.md's own 2026-07-26 note confirms none of these were revalidated after switching S2 from L1C to L2A**, which shifts reflectance-derived index values. |
| **SCL cloud-mask classes** (`_SCL_INVALID_CLASSES`, processor.py:170) | ESA's own SCL class definitions (0,1,3,8,9,10,11 invalid) | Deterministic | **Sound** — this one *is* grounded: it directly follows Sentinel-2 L2A's documented SCL class table, not an invented threshold. |
| **Confidence quality gate** (`MIN_CONFIDENCE = 0.6`, agent.py:151) | Deterministic | Arbitrary round number. |
| **needs_verification threshold** (`VERIFICATION_THRESHOLD = 0.70`, confidence_tracker.py:30) | Deterministic | Arbitrary round number. |
| **Concern severity penalties** (`_SEVERITY_PENALTY`: LOW .05/MED .10/HIGH .20/CRITICAL .35) | Deterministic | Arbitrary, flat, additive — see §4 for why this is the central problem with the confidence number. |
| **GDACS discrepancy ratio bands** (0.7-1.3 CONFIRMED, >2.0 HIGH, <0.5 HIGH, else PARTIAL) | Deterministic | Round numbers, no cited basis. |
| **Recency half-life** (`_RECENCY_HALFLIFE_DAYS = 20.0`, sentinel.py:1067) | Deterministic | Arbitrary; stated rationale is "gentle enough that a well-covered older scene beats a nearly-empty newer one" — a reasoned design intent, but the specific 20-day constant is not derived from anything. |
| **Zone area floor** (`MIN_ZONE_AREA_KM2 = 0.5`) | Deterministic | Arbitrary noise-filter round number. |

**Summary:** every *threshold* in this agent is a round number chosen by
feel; the only threshold with a real physical basis is the SCL invalid-class
set (because it's ESA's own class table, not a tuned cutoff). The
*architecture* around the thresholds (tiered temporal coherence, same-orbit
constraint, cloud-aware satellite selection, coverage-before-cloud priority)
is well-reasoned and documented; the numeric knobs inside that architecture
are not.

---

## 3. The Analysis Itself — The Science

### 3.1 NDWI (flood, Sentinel-2)

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

### 3.2 SAR (flood, Sentinel-1)

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

### 3.3 NDVI (earthquake / landslide, Sentinel-2)

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

### 3.4 CRS handling / clip / vectorization / area

- **CRS resolution (BUG 1, fixed and unit-tested):** `_open_georeferenced`
  (processor.py:996-1058) detects GCP-only georeferencing (S1 GRD: `crs=None`
  or identity transform with GCPs present) and wraps the dataset in a
  `WarpedVRT` targeting either the AOI's UTM zone (`_dst_crs_from_polygon`) or
  the GCPs' own CRS. `clip_to_polygon` additionally refuses to clip
  (`return None`) if it still sees `crs is None or transform is identity`
  (processor.py:1210-1217) — a hard guard against the exact BUG B failure mode
  `fix.md` documented. **Confirmed fixed** by direct code read and by
  `tests/test_bug_fixes.py::test_bug1_gcp_raster_resolved` /
  `test_bug1_4326_poly_vs_utm_raster`.
- **Clip method:** reprojects the WGS84 polygon into the raster CRS, computes
  a pixel window from the polygon bounds (pre-windowing optimization,
  processor.py:1238-1261), rasterizes only that window via `rasterio.mask`,
  crops every band. This is standard practice and the pre-windowing is a real
  and correct performance fix (verified against `tests/test_clip_window.py`).
- **Vectorization:** `rasterio.features.shapes` per hazard class → reproject
  to WGS84 → `shapely.simplify(0.001°, preserve_topology=True)` → drop
  polygons < `MIN_ZONE_AREA_KM2` (0.5 km²). Standard raster-to-vector
  approach; the fixed 0.001° simplification tolerance is not adaptive to the
  raster's native resolution (a UTM 10 m pixel is ~0.00009° at the equator,
  so 0.001° is roughly 10x a pixel — a reasonable, if unstated, simplification
  factor).
- **Area calculation:** `_polygon_area_km2` (processor.py:1622-1638)
  reprojects to **EPSG:6933** (a real world equal-area projection —
  Cylindrical Equal-Area) before computing `.area`. **This is scientifically
  correct** — equal-area projections are exactly what area measurement
  requires. The `except Exception: return geom.area` fallback (degrees²,
  labeled "only used for relative size" in the code comment) is a silent
  failure mode: if pyproj ever throws, the function returns a **degrees²**
  number *as if it were km²* with no unit change and no error surfaced to the
  caller — the comment acknowledges this is wrong but the code does nothing
  to prevent the mislabeled value from flowing into `affected_area_km2`
  (confirmed still present, unchanged from `fix.md`'s original finding).

---

## 4. Confidence — Traced Completely

### 4.1 Every input `ConfidenceTracker` consumes

`ConfidenceTracker` (confidence_tracker.py) is fed from exactly these call
sites, all inside `agent.py`/`cross_validator.py`/`processor.py`:

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

### 4.2 The exact arithmetic (confidence_tracker.py:114-132)

```python
weighted_sum = sum(e["value"] * e["weight"] for e in self.evidence)
total_weight = sum(e["weight"] for e in self.evidence)
base = weighted_sum / total_weight          # weighted average, NOT additive
for concern in self.concerns:
    base -= _SEVERITY_PENALTY[concern["severity"]]   # LOW .05/MED .10/HIGH .20/CRITICAL .35
return max(0.0, min(1.0, base))              # clamped
```

This is: **weighted-average of evidence, then flat additive penalty
subtraction per concern, clamped to [0,1].** No source is itself penalty-scaled
by its own weight — every concern costs the same fixed amount regardless of
how much evidence exists or how reliable the concerned source normally is.

### 4.3 How a run reaches 0.0 — is it reachable by design?

Two independent ways, both reachable by design, not edge cases:

1. **Zero evidence.** `overall_confidence()` returns `0.0` unconditionally
   when `self.evidence` is empty (confidence_tracker.py:120-121). This is the
   literal 2026-07-26 e2e observation cited in `CLAUDE.md`: if GDACS/USGS are
   unreachable, cloud data is absent, the NDWI physics check doesn't fire (SAR
   path), and the Featherless expert call fails, **zero evidence sources ever
   fire**, and the number is exactly 0.0 — indistinguishable from "everything
   strongly contradicts this result," which is the opposite failure mode.
   **This is the actual documented root cause of the satellite-confidence-0.0
   /report-confidence-HIGH incident** — not a stacking-of-penalties collapse,
   but an *empty evidence set*.
2. **Penalty stacking.** With `total_weight > 0`, `base` can still be driven
   to (or below, then clamped at) 0 by enough concerns: e.g. 3 CRITICAL
   concerns alone (`-0.35 × 3 = -1.05`) will floor any base to 0.0 regardless
   of how strong the evidence was. This is architecturally possible but
   requires several severe concerns simultaneously (CRITICAL only fires for
   cloud >60% or the NDWI-negative/GDACS-RED contradiction) — less common in
   practice than case 1.

Both are "reachable by design" in the sense that the code does exactly what
it says; neither is a bug in the arithmetic. The problem is **what the number
means when it's 0.0** — see §4.4.

### 4.4 Is this a calibrated uncertainty estimate, or a heuristic?

**It is a heuristic, not a calibrated estimate**, for these concrete reasons
found in the code:

- **Weights are hand-picked**, not fit to any historical accuracy data (there
  is no training/calibration step anywhere in the codebase; `0.3`/`0.4`/`0.2`/
  `0.25` etc. are chosen by the same "feels about right" process as every
  other threshold in this agent).
- **It conflates two different failure classes**: "the disaster's true state
  is genuinely uncertain" (e.g., legitimately unclear/contested GDACS
  numbers) and "our own pipeline could not gather enough reliable evidence
  to say anything" (e.g., GDACS unreachable, LLMs all failed). Both currently
  compress to the same 0-1 number, and — critically — the *empty-evidence*
  case (§4.3.1) reads identically to *maximally-contradicted* in this scale's
  low end, even though they mean opposite things operationally ("we don't
  know" vs "we're pretty sure this is wrong").
- **No confidence interval, no distribution, no error bars** — it is a single
  point estimate with an ad hoc penalty subtraction, not a probability in any
  formal sense (e.g., not derived from a calibrated classifier, not
  cross-validated against ground truth outcomes).
- **A downstream consumer cannot recover data-quality signal from it.** As
  documented in `CLAUDE.md`'s "Confidence silently drops..." entry (confirmed
  still true by direct grep, §5 below): `agents/hazard/analyzer.py`'s
  `run_parallel_analysis` never reads satellite's `confidence` at all, so even
  the imperfect signal computed here is dropped, not merely misinterpreted,
  one hop downstream.

**What it would take to be meaningful:** (a) separate the "no evidence
gathered" state from "evidence gathered and it's unfavorable" explicitly in
the output (e.g., a distinct `data_completeness` or `evidence_count` field
gating whether `confidence` should be trusted at all — `get_report()` already
computes `evidence_count` internally but it never rides into the structured
result dict returned to callers, §5); (b) derive weights/penalties from an
actual historical accuracy study rather than hand-picked constants; (c) stop
using the same tracker to answer both "is the satellite processing pipeline
healthy" (BUG-B-style failures) and "is the disaster hazard genuinely
uncertain" (contested reports) — these are different questions currently
answered by the same number.

### 4.5 What is written into the result dict and the DB, and what downstream can see

**In the structured result dict** (agent.py:827-883): `confidence` (float,
rounded to 4dp), `concerns` (full list of `{concern, severity, timestamp}`),
`validations` (per-source finding list), `needs_verification` (bool),
`should_alert` (bool). **`evidence_count`/raw evidence list are NOT included**
— `structured` never calls `tracker.get_report()`, it only calls
`tracker.overall_confidence()`, `tracker.concerns` (the raw list, reused
directly), `tracker.needs_verification()`, `tracker.should_alert_team()`. So
the "how much evidence actually went into this number" signal
(`evidence_count`) exists inside the tracker object but is **discarded**
before the result ever leaves the pipeline.

**In the DB** (`_persist_satellite_result`, agent.py:71-134): only
`affected_area_km2`, `damage_percent` (always NULL, §5), `total_zones`
(always NULL, §5) and the artifact URLs/bounds/bbox/risk_cities are written.
**`confidence`, `concerns`, `validations`, `needs_verification`,
`should_alert` are NOT columns in the INSERT statement at all** — they exist
only in the JSON blob returned by the node/pipeline call, which is passed
in-memory via `PipelineState["confidence_scores"]["satellite"]`
(node.py:49-50, just the scalar number) to the next graph node. **Anything
reading the satellite result from the DB directly (a fresh `GET /results`
call, or a future consumer that reads `satellite_results` rather than the
live graph state) never sees `concerns`/`validations`/`needs_verification`/
`should_alert` at all** — only the in-memory `PipelineState` hand-off carries
them, and only the bare confidence float survives even that hand-off into
`confidence_scores`.

**Downstream can see:** the bare `confidence` float (via
`PipelineState["confidence_scores"]["satellite"]`) and the full
`satellite_result` dict in-memory for the one graph run. **Downstream cannot
see** (from the DB, or from the in-memory state one hop later): evidence
count, per-source evidence values, or the tracker's raw ledger — only the
already-collapsed single number and the concern-text list.

**CORRECTION (this session, see revision note at top of document): this
claim was already stale when originally written.** `agents/hazard/agent.py`'s
`_normalise_satellite_payload` carries `confidence` into the normalized
payload at `analysis.confidence` (line ~89), and — as of `fa0d9bd`, which
landed *before* this audit was performed — `analyzer.py`'s
`run_parallel_analysis` DOES read it (`satellite_confidence =
_to_float(analysis.get("confidence"))`, analyzer.py:342) and uses it to cap
flood confidence: `if satellite_confidence < flood_confidence: flood =
{**flood, "confidence": satellite_confidence}` (analyzer.py:409-414).
Earthquake/landslide are deliberately left uncapped (self-sourced from
USGS/DEM). This was verified against the code at the time this document was
originally drafted and the claim above was wrong even then — see
`SYSTEM_ANALYSIS.md` §B for why the fix is real but does not fully solve the
problem (it only affects the flood row, and never reaches the report agent's
live DB-fetch path at all, since `satellite_results` has no confidence
column).

---

## 5. Data Contract — Every Field in the Result Dict

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
| `index_calibrated` | bool\|None | — | agent.py:843 | hazard `_normalise_satellite_payload` line ~89 area (present); **`run_parallel_analysis` never reads it**, so the honest calibration flag does not actually reach the flood risk decision |
| `index_units` | str | "NDWI_ratio"/"NDVI_ratio"/"dB_uncalibrated" | agent.py:844 | same as above — carried to hazard's normalise step but not read by the analyzer |
| `coverage_percent` | float | 0-100 (should be 100.0 on any successful run) | agent.py:847 | not found consumed downstream |
| `full_aoi_coverage_percent` | float | 0-100 | agent.py:848 | not found consumed downstream |
| `coverage_tier` | int | 1-4 | agent.py:849 | not found consumed downstream |
| `temporal_spread_days` | int | days | agent.py:850 | not found consumed downstream |
| `acquisition_count` | int | count | agent.py:851 | not found consumed downstream |
| `processing_level` | str\|None | "L2A" or None | agent.py:852 | not found consumed downstream |
| `bytes_downloaded` | int | bytes | agent.py:853 | not found consumed downstream |
| `bbox` | list[float] | WGS84 degrees | agent.py:854 | hazard (bbox scoping for USGS/GDACS/DEM fetches), impact |
| `bounds`/`bounds_leaflet`/`bounds_corners` | dict/list | WGS84 degrees | processor.py `_compute_bounds` | frontend map overlay (not this agent's scope, confirmed by CODEBASE.md) |
| `region_boundary` | GeoJSON | — | agent.py:858 | frontend |
| `risk_cities` | list[str] | — | agent.py:859 | hazard, impact (city-scoped population/infra lookups) |
| `true_color_url`/`index_url`/`classification_url`/`geojson_url`/`image_url` | str (URL)\|None | — | agent.py:860-864 | frontend, report map_generator |
| `cached` | bool | — | agent.py:865 | not consumed downstream |
| `cities` | list | — | agent.py:870 | **ALWAYS EMPTY** in production (see §9) — the per-city upload loop and `cities_payload` construction run over an empty list every time, since `city_boundaries=None` is passed unconditionally at agent.py:602 |
| `interpretation` | dict\|None | — | agent.py:872 | not found consumed by name downstream (report has its own separate interpretation) |
| `confidence` | float | 0-1 | agent.py:876 | hazard normalise (present, unread by analyzer — see §4.5) |
| `concerns`/`validations`/`needs_verification`/`should_alert` | list/list/bool/bool | — | agent.py:879-882 | **not persisted to DB at all** (confirmed §4.5); in-memory only, one hop |
| `summary_message` | str\|None | free text | agent.py:908 | not found consumed downstream by name |

### 5.1 Fields produced but never consumed anywhere downstream (confirmed by grep)

`selection_reason`, `class_counts`, `coverage_percent`,
`full_aoi_coverage_percent`, `coverage_tier`, `temporal_spread_days`,
`acquisition_count`, `processing_level`, `bytes_downloaded`, `cached`,
`interpretation`, `summary_message` — all present in the structured result and
in `PipelineState`, but no grep hit in `agents/hazard/`, `agents/impact/`, or
`agents/report/` reads them by name. These are either intended for future
consumers, dashboard-only, or genuinely dead weight on the data contract.

### 5.2 Fields whose label can disagree with content (the class the SAR-as-NDWI bug belongs to)

- **`mean_index`** is the single most dangerous field in this contract: its
  *numeric meaning* (bounded ratio vs. unbounded uncalibrated dB) depends
  entirely on `satellite_type`/`index_type`, which the hazard agent's own
  deterministic fallback (`analyze_flood`'s `flood_index > 0.5`/`> 0.3`
  thresholds, analyzer.py:213-215) **still does not branch on** — confirmed by
  direct read of `analyzer.py`: the LLM-facing prompt at line 180-185 DOES
  branch correctly on `satellite_type` to pick the right label/context, but
  the **deterministic fallback used when the LLM call fails** applies flat
  NDWI-scale thresholds to whatever `mean_value` is, regardless of whether it
  came from SAR or NDWI. This is the exact defect class `root_cause.md`
  documented (§4.3 there) and it is **still present and unfixed** in the
  current hazard code — the satellite agent now correctly labels the field
  (`index_calibrated`/`index_units`), but the hazard agent's fallback path
  does not consume those labels to change its own threshold logic.
- The satellite agent's own `validation_input` construction (agent.py:731-745)
  has been **fixed** since `root_cause.md`/`fix.md` were written: it now
  builds `index_type`/`index_calibrated`/`index_units` from the actual
  computed result and asserts `validation_input["index_type"] ==
  result["index_type"]` before calling the cross-validator. This closes the
  specific mislabeling bug those docs originally found *inside this agent*.
  The remaining live instance of the same defect class is one hop downstream,
  in hazard's fallback threshold logic, not in this agent.

---

## 6. Failure Modes

| Failure | Trigger | Surfaced or swallowed? | What's returned | Can downstream tell it apart from success? |
|---|---|---|---|---|
| Region boundary unresolvable | `get_region_boundary` returns None | Surfaced | `{"status":"error", "error": "..."}` | Yes — `status != "complete"` |
| No risk cities detected | `detect_risk_cities` returns empty | Surfaced | error payload | Yes |
| Risk-city boundaries unresolvable | all cities fail Nominatim+geoBoundaries+buffer | Surfaced | error payload | Yes |
| CDSE auth fails after 3 attempts | `TokenManager().get()` returns None each time | Surfaced | error payload | Yes |
| No scenes found after widening to 30d | `search_imagery` empty at every window | Surfaced | error payload | Yes |
| Insufficient coverage (no tier reaches 100%) | `process_satellite_imagery` returns `status:"failed"` | Surfaced, with gap geometry | `_coverage_failure(...)` — `status:"error", reason:"insufficient_coverage"` | Yes, and unusually well-informed (gap area/bbox/cause) |
| **DB persist fails** | any exception in `_persist_satellite_result` | **Swallowed** — `except Exception: logger.warning(...)`, no re-raise, no status change | Pipeline still returns `status:"complete"` with the full structured payload | **NO.** `GET /results` (or any DB-reading consumer) will 404/miss this event even though the in-memory graph state and the caller both saw a successful "complete" result. This is a **dangerous silent-success failure mode**, unchanged from `fix.md`'s original finding. |
| `_polygon_area_km2` pyproj failure | any exception in the equal-area reprojection | **Swallowed** — `except Exception: return geom.area` | `affected_area_km2` becomes a **degrees² value silently mislabeled as km²** (off by roughly 4 orders of magnitude at mid-latitudes) | **NO.** The field name and type are unchanged; nothing marks the value as suspect. This is the single most dangerous *silent* correctness bug in the codebase — a downstream consumer has no way to detect it without independently sanity-checking the magnitude. |
| Whole-pipeline unexpected exception | any uncaught exception in `_run_pipeline_sync`'s try block | Surfaced, but with **no stack context** | `{"status":"error","error": f"Unexpected error: {exc}"}` | Yes it's an error, but the *cause* is opaque — only `str(exc)`, no traceback, no stage attribution. Debugging requires reading server logs. |
| Cleanup failure (temp dir removal) | `shutil.rmtree` raises OSError | Swallowed — logged as warning | Does not affect pipeline status | N/A — correctly non-fatal |
| R2 upload failure (any artifact) | `_put_file`/`_put_bytes` raise | Swallowed per-artifact — returns `None` for that URL | `structured["true_color_url"]` etc. can be `None` while `status` is still `"complete"` | **Partially.** A `None` URL is visible to a careful consumer, but the pipeline does not treat a failed critical-artifact upload as a stage failure — a "complete" satellite result can carry `image_url: None`. |
| GDACS/USGS/Featherless-expert unreachable | any network/parse error in `cross_validator.py` checks | Swallowed — logged, check skipped, no evidence added | Confidence is lower (fewer evidence sources) but no explicit "this check could not run" flag rides in the result | Downstream sees a lower number but cannot distinguish "check ran and found a problem" from "check never ran" (this is the §4.4 ambiguity). |

**Summary of dangerous silent-success paths:** (1) DB persist failure —
"complete" status with no durable row; (2) area-unit silent fallback to
degrees² mislabeled as km²; (3) per-artifact R2 upload failure leaving `None`
URLs inside an otherwise-"complete" result. All three were flagged in
`fix.md` and are confirmed **still present, unfixed**, in the current code.

---

## 7. External Dependencies

| Dependency | Timeout/retry behavior | On unavailable |
|---|---|---|
| **CDSE OAuth (Keycloak)** | `authenticate_copernicus`/`_authenticate_copernicus_full`: single request, 30s timeout, no retry inside the function itself; `TokenManager.get()` proactively refreshes 90s before the ~10min expiry via `refresh_token` grant, falling back to full re-auth if refresh fails. Thread-safe via `threading.Lock`. `_authenticate_with_recovery` (agent.py) retries the whole TokenManager construction up to 3 times with an LLM-suggested (capped ≤10s) delay between attempts. | Pipeline fails cleanly with `_error("Copernicus authentication failed (after recovery)")`. |
| **CDSE catalogue search** (`search_imagery`, `_peek_cloud_cover`) | 60s / 30s timeout respectively, single attempt each (no internal retry — the retry loop is at the `_search_with_recovery` level: 7/14/30-day re-attempts, which are new *searches*, not retries of a failed request) | On `requests.RequestException`: logged, returns `None`. On empty results (not an error, a valid empty catalogue): logged warning, returns `None`, triggering the day-window widening. |
| **CDSE download** (Nodes per-band, or whole-zip fallback) | `(connect=15s, read=90s)` timeout tuple; `_stream_to_file_with_retry` retries against a **time budget** (`OUTAGE_GRACE_SECONDS = 7min`), not a fixed attempt count — exponential backoff 5s→30s cap. CDSE never honors HTTP Range (confirmed by the in-code comment and the whole per-band download design rationale), so every retry re-fetches the object from scratch; per-band granularity (vs. whole-archive) is what limits the blast radius of a single dropped connection. | After the grace budget expires: `None` returned, scene download abandoned, caller (`download_imagery`) tries the next candidate scene or falls back from Nodes to whole-zip. |
| **geoBoundaries API** (`geoboundaries.py`) | Metadata: 20s timeout; GeoJSON download: 120s timeout. Transient failures (network exceptions) are **not cached** (retryable next call); a genuine 404 (level absent for the country) **is cached** as a negative to avoid re-probing. | Falls through the source chain: geoBoundaries miss → Nominatim/OSM areal relation → buffered-disk last resort (with a loud warning). Never blocks the pipeline. |
| **Nominatim** (`boundary.py`, `geoboundaries.py`'s country lookup) | 30s (place search) / 20s (country lookup) timeout, no retry; throttled to ≤1 req/sec via a module-level `time.sleep` gate (policy compliance, not resilience) | On failure: `None` returned, city/region skipped (non-fatal for a single city; fatal for the overall pipeline only if *every* city fails). |
| **GDACS GeoJSON feed** (`cross_validator.py`) | 15s timeout, no retry | Logged warning, check skipped entirely — no evidence, no concern, no crash. |
| **USGS FDSN query** (`cross_validator.py`) | 15s timeout, no retry | Same as GDACS — skipped cleanly. |
| **LLM providers** (Gemini → Featherless chain → AIML/Opus, `intelligence.py`) | 30s per-model timeout (`MODEL_TIMEOUT_SECONDS`), `max_retries=0` (the chain itself is the retry mechanism — up to 5 Gemini keys + 4 Featherless models + 1 Opus = up to 10 attempts per call) | If every model in the chain fails/times out/returns empty content: the calling method returns `None`, and every call site in `agent.py` treats `None` as "fall back to the deterministic default" — confirmed correct at every one of the six integration points (IP1-IP6); none of them treat an LLM failure as a pipeline failure. |
| **Cloudflare R2** (`r2_upload.py`, boto3) | boto3 default retry/timeout behavior (not overridden in this code) | Every upload function catches `BotoCoreError`/`ClientError` and returns `None` per-artifact — see §6 for why this is a silent-partial-success risk, not a crash risk. |
| **Postgres/Neon** (`_persist_satellite_result`) | `asyncpg.connect()` per call (no pool — a fresh TLS connection every persist), no explicit timeout override, no retry | Any exception (including a connection failure) is caught and logged as a warning only — see §6, this is the most dangerous swallowed failure in the agent. |

---

## 8. Resources

### 8.1 Memory

Directly instrumented in code (`_mem_stage`/`memory_report`, processor.py:
98-141) — this is **not an estimate**, it is what the pipeline itself logs
per run. Per `CLAUDE.md`'s cited live run (2026-07-26, Rawalpindi S1/SAR,
event `88ad6095…`): **peak RSS 9,611.3 MB (~9.6 GB) at the clip stage with
only 2 tiles mosaicked**; single-tile clip stages peaked ~5.1-5.6 GB; 2-tile
mosaic-and-clip stages peaked 7.3-9.6 GB. RSS scales roughly linearly with
tile count because every `_open_georeferenced`/`WarpedVRT` and
`rasterio.merge` pass holds full-resolution arrays (S1 GRD scenes are
28,000×21,000+ px) in memory before the windowed clip trims them down. The
pre-clip stacked cube is explicitly freed + `gc.collect()`'d before the
render tail *only* when per-city rendering is off (processor.py:2337-2340) —
since per-city is always off in production (§9), this freeing path is always
taken, which is the one memory optimization actually exercised live.

### 8.2 Disk

`TEMP_ROOT` under the system temp dir holds: downloaded band JP2/TIFF files
(`<event_id>/bands/`), exported PNGs, and cached full-archive `.zip` files
(kept at the `TEMP_ROOT` top level, keyed by product Id, independent of
`event_id`). `cleanup_event_temp` (processor.py:2451-2496) is called both on
the success path (agent.py:718) and, per BUG 7, **guaranteed via a `finally`
block** (agent.py:919-930) on every exit path including exceptions — this
closes the pre-fix gap where a failure left a multi-GB working tree on disk.
The `.zip` archive cache is separately gated by `SATELLITE_KEEP_SCENE_CACHE`
(default: delete in production) — this is correctly designed to avoid
unbounded disk growth on an ephemeral VM.

### 8.3 Network volume

Per-band Nodes download for S2 (~30-120 MB/band) vs. whole-archive fallback
for S1 (~1.2-1.7 GB per scene, confirmed by the 2026-07-26 e2e run: 4 full
archives, 5,490.7 MB total, because `_download_bands_via_nodes` explicitly
returns `None` for any `satellite_type != "sentinel-2"` — processor.py:615-617
— since its Nodes-tree mapping only understands the S2 L1C/L2A IMG_DATA
layout). **This means every Sentinel-1 candidate scene is a full-archive
download with no per-band resume benefit** — a real, confirmed, unaddressed
gap: S1 gets none of the outage-resilience granularity S2 has.

### 8.4 Wall-clock time

The 2026-07-26 live e2e (S1/SAR, Rawalpindi, 4 coverage tiers, 4 full-archive
downloads) took **3244.1s (~54 min)** total — cited in `CLAUDE.md` as the
current S1 baseline (the old 142s baseline is explicitly retired because it
predated BUG 1's fix and represents an instant-collapse-to-0%-coverage run,
not a real success). Contributors, in order of likely magnitude based on the
code: (1) sequential full-archive S1 downloads across up to 4 tiers × up to
`DOOMED_DOWNLOAD_LIMIT`-bounded candidates each; (2) per-file/per-band
`asyncio.run()`-wrapped DB writes with no connection pooling
(`_persist_satellite_result` opens a fresh `asyncpg.connect()` every call);
(3) serial LLM calls at each of the 6 integration points, each with up to a
30s-per-model timeout across as many as 10 chained attempts if earlier
providers fail — these are explicitly **not on the imagery-processing
critical path** (they're narrative/reasoning, not required for the
deterministic pipeline to produce a result) but they are called synchronously
and block the thread; (4) single-threaded `rasterio.features.shapes`
vectorization scaling with zone count (documented elsewhere as
multi-minute on a 251-zone Mindanao mosaic).

---

## 9. Dead and Unreachable Code

Beyond the two already-known items (`stance_engine.py` and
`intelligence.decide_landsat_fallback`, both **already deleted** per
`CLAUDE.md`'s BUG 6 — confirmed absent from the current `agents/satellite/`
directory listing and from `intelligence.py`'s method list), the following
were found by direct reading:

| Item | Status | Apparent purpose | Real gap or should-delete? |
|---|---|---|---|
| **Per-city artifact rendering** (`_render_per_city`, `_render_clip`'s `out_id` namespacing, the per-city upload loop in `agent.py`) | Unreachable in production — `agent.py:602` hardcodes `city_boundaries=None` on every call to `process_satellite_imagery`, unconditionally. The `city_boundaries and len(city_boundaries) > 1` guard inside `process_satellite_imagery` (processor.py:2404) can therefore never be true from the live entry point. | Per-city artifact sets for multi-city AOIs (frontend/hazard wanting individual city layers) | **Fills a real, documented gap** (deliberately disabled for performance per `CLAUDE.md` Step 13's FIX 5) — this is dormant-by-design, not truly dead, but every line of `_render_per_city`/the per-city upload loop is currently unexercised by any live path. Worth flagging: `MIN_VALID_PIXEL_PERCENT` (5%) is the only place this constant is still load-bearing (§2), and it only matters for a code path that never runs. |
| **`COVERAGE_MOSAIC_THRESHOLD` / `MOSAIC_MAX_SCENES`** (`sentinel.py:51`, `processor.py:66-68`) | Defined, but **not read by `process_satellite_imagery`'s actual tiered-coverage logic** (§1.3/§2) — the tiered search's acceptance criterion is `compute_coverage()["covered"]` (100% interior), not a comparison against this threshold. `select_mosaic_scenes` (sentinel.py:797) — the function that *would* consume these constants for a greedy set-cover selection — exists and is fully implemented, but is **never called from `agent.py` or `processor.py`** (confirmed by grep: `select_mosaic_scenes` has zero call sites outside its own definition and test files). | Was the FIX C/Step-10 greedy set-cover scene-selection strategy, superseded by the later tiered-temporal-coherence design (BUG 3) that supersedes greedy set-cover with tier-then-accept-best-first. | **Should be deleted or explicitly wired back in.** It is fully implemented, documented at length in `CLAUDE.md`, unit-testable, and currently orphaned — the tiered search does its own simpler best-first-within-group acceptance instead. Leaving it creates the false impression (reading `CLAUDE.md` alone) that greedy set-cover is still the active mosaic strategy. |
| **`_scene_covers_geom`** (sentinel.py:449) | Still called from `backfill_uncovered_cities` (§1.2) — **not dead**, just decoupled from `select_mosaic_scenes`. |  |
| **`verify_setup.py`** | Deleted per the migration file-diff (confirmed absent from the current directory listing) — `CODEBASE.md`'s reference to it describing "the older AnthropicAdapter" is now moot since the file itself is gone, not merely stale. |  |
| **`total_zones` computed then dropped** (agent.py:763-767) | `total_zones` is computed locally (`len(result["geojson"].get("features", []))`) and used in the `interpret_results` LLM call and the `generate_band_message` call, but is **never added to the `structured` result dict** that gets persisted — the `_persist_satellite_result` INSERT names a `total_zones` column (agent.py:108/122) that is `structured.get("total_zones")` → always `None`. | Was presumably meant to be a persisted field | **Bug, not dead code** — cheap one-line fix (add `"total_zones": total_zones` to `structured`), but currently every `satellite_results.total_zones` DB row is NULL. Confirmed unchanged from `fix.md`'s original finding. |
| **`damage_percent`** (persisted column, agent.py:107/121) | The INSERT names this column but `structured` never sets a `damage_percent` key anywhere in `agent.py`. | Unclear original intent — possibly meant to mirror `water_percent` for non-flood disasters | **Always NULL in the DB.** Same class of bug as `total_zones`. |
| **`scene_id`** (persisted column, agent.py:106/115) | Same pattern — INSERT names it, `structured` never sets it. The selected scene's `Name`/`Id` is available in-pipeline (used for logging) but never threaded into `structured`. | Intended to record which scene was used | **Always NULL in the DB.** Same class of bug. |

---

## 10. The Gap List — Prioritized

| # | Issue | Type | Severity | Effort | Evidence |
|---|---|---|---|---|---|
| 1 | ~~DB persist failure is swallowed as a warning~~ **CLOSED by `7eb23f0`** — persist now retries 3x with backoff, exhaustion returns an error string, `_run_pipeline_sync` treats non-None as `status:"error"`, never a fake "complete". | correctness/contract | (resolved) | (done) | agent.py `_persist_satellite_result` |
| 2 | ~~`_polygon_area_km2`'s exception fallback returns degrees² mislabeled as km²~~ **CLOSED by `35bdb40`** — the `except Exception: return geom.area` fallback was deleted; a reprojection failure now propagates instead of silently mislabeling. | correctness | (resolved) | (done) | processor.py `_polygon_area_km2` |
| 3 | **SAR backscatter is uncalibrated raw-DN log, with no calibration LUT, no speckle filter, no terrain correction — the class thresholds applied to it (-13/-15/-18 dB) have no physical basis given that input.** The pipeline now honestly *labels* this (`index_calibrated: False`), but still *computes and ships a classified hazard map and a `mean_index` number* as if the thresholds meant something, for every Sentinel-1 flood run. | science | **High** — every S1 flood analysis (the case CDSE/cloud-routing selects specifically *because* optical is unusable) ships a classification/area/mean-index that is not scientifically defensible, even though it is correctly flagged as uncalibrated in the metadata. | High (real calibration + speckle filter + RTC is a substantial remote-sensing engineering task) | processor.py:1384-1401, 233-267; CLAUDE.md's own SAR notes |
| 4 | **Hazard agent's deterministic flood fallback still applies NDWI-scale thresholds to a SAR-dB `mean_value` without checking `satellite_type`, even though the satellite agent now correctly labels `index_calibrated`/`index_units`.** The label exists but is not consumed by the one place that would misuse it. | contract | **High but conditional** — only fires when the hazard agent's LLM call fails and the deterministic fallback runs; when it does fire on an S1 path, `flood_index > 0.3`/`> 0.5` is almost never true for SAR dB values, so it silently biases toward LOW/MEDIUM risk on a real flood signal (false negative), not a false alarm. | Low (branch the fallback on `satellite_type`/`index_calibrated`, mirroring what the LLM prompt already does) | agents/hazard/analyzer.py:180-185 (correct) vs 211-215 (not) |
| 5 | **CORRECTED — this claim was already stale when written (see revision note at top).** `fa0d9bd` (landed before this audit) already made `run_parallel_analysis` read satellite confidence and cap flood's confidence at it. The REAL remaining gap: the cap only applies to the flood row (not earthquake/landslide, by design), and report's live DB-fetch path never receives a satellite confidence value at all — see `SYSTEM_ANALYSIS.md` §B. | contract | High — narrower than originally stated, but the report-layer half is real and unfixed. | Medium (add a `satellite_results.confidence` DB column + read it in `db_client.py`) | agents/hazard/analyzer.py:342,409-414 (fixed); agents/report/db_client.py `_fetch_satellite_results`/`db_context_to_report_context` (not fixed) |
| 6 | **`ConfidenceTracker.overall_confidence()` cannot distinguish "no evidence was gathered" from "evidence strongly contradicts the result".** **PARTIALLY CLOSED by `e90fc68`/`8825fc5`** — `confidence_basis()` (`"insufficient_evidence"`/`"evidence_contradicts"`/`"evidence_supports"`) and `evidence_count` now ride into `structured`, making the two states legible. The underlying weighted-average-minus-penalty arithmetic itself is unchanged — still a heuristic, not calibrated, and `confidence_basis`/`evidence_count` are not yet consumed by anything downstream. | science/contract | Medium (down from High — now diagnosable, not fixed) | Medium (a real calibration pass needs accuracy data this repo doesn't have yet) | confidence_tracker.py; agent.py (now calls `get_report()`) |
| 7 | **NDWI/NDVI thresholds (0.3/0.5 water, 0.2 damage) were tuned against L1C top-of-atmosphere reflectance and have not been revalidated since the pipeline switched Sentinel-2 to L2A surface reflectance.** Acknowledged in-code and in `CLAUDE.md`, still unresolved. | science | **Medium-high** — affects every S2-path flood/earthquake/landslide run; direction of the bias (more or less permissive) is plausible but unverified. | Medium (a dedicated science-validation pass against known-ground-truth L2A scenes) | processor.py:148-151, 212-213; CLAUDE.md 2026-07-26 section |
| 8 | **NDVI-based earthquake/landslide damage detection is a single-scene absolute-threshold classification, not a bi-temporal (before/after) difference.** This conflates disaster damage with naturally low-NDVI terrain (bare soil, urban, desert) and is materially weaker than the standard literature approach; nothing in the result or documentation flags this limitation to an operator reading the classification map. | science | **Medium** — every earthquake/landslide S2 run is subject to this, silently. | High (requires fetching + differencing a pre-event baseline scene — a real feature addition, not a tuning fix) | processor.py:1413-1423 |
| 9 | **Sentinel-1 has no per-band Nodes download path — every S1 candidate scene is a full 1.2-1.7 GB archive fetch**, unlike S2's per-band resilience/resume granularity. Confirmed: `_download_bands_via_nodes` unconditionally returns `None` for `satellite_type != "sentinel-2"`. | performance | **Medium** — makes every S1 (i.e. every cloud-obscured, often the most urgent) run slower and less outage-resilient than the equivalent S2 run. | Medium (extend the Nodes-tree mapping to the S1 GRD `measurement/*.tiff` layout) | processor.py:615-617 |
| 10 | ~~`total_zones`, `damage_percent`, `scene_id` never populated~~ **PARTIALLY CLOSED by `c9e6a1d`** — `total_zones`/`scene_id` now populate correctly; `damage_percent` has no producer anywhere and was removed from the INSERT (honest absence) rather than shipped as permanent NULL. Note: report's `.get("damage_percent") or 0` read still silently reads 0, see `SYSTEM_ANALYSIS.md` §F. | contract | (resolved on satellite side) | (done) | agent.py; processor.py |
| 11 | ~~Per-city artifact rendering permanently unreachable~~ **CLOSED by `b7fdad4`** — now gated by `ENABLE_PER_CITY_ARTIFACTS` (default false, reachable/testable). `select_mosaic_scenes`/`COVERAGE_MOSAIC_THRESHOLD`/`MOSAIC_MAX_SCENES` (orphaned dead code) were deleted outright, not left dormant. | dead code | (resolved) | (done) | agent.py; sentinel.py |
| 12 | ~~R2 per-artifact upload failures degrade silently to `None` URLs~~ **CLOSED by `48aaaf8`/`6300623`** — `structured` now carries `artifacts_incomplete: bool` + `failed_artifacts: list[str]`, so a degraded run self-reports rather than looking clean. | contract | (resolved) | (done) | r2_upload.py; agent.py |
| 13 | **Coverage tiers' day windows (0/±3/±7/±14) are not derived from either satellite's actual revisit cycle** — S1's 6-12 day typical revisit makes a ±3-day tier 2 plausibly almost-always empty in practice for S1, silently collapsing tier 2 to a no-op most of the time. | correctness (design) | **Low-medium** — not incorrect, but likely an unintended near-no-op tier that adds search latency for little benefit on the S1 path specifically. | Low (a design review, not a code change) | sentinel.py:729-734; CLAUDE.md's own tier-1-3-exactly-0% analysis |
| — | **RESOLVED (design review, 2026-07-27, live-measured — see CLAUDE.md's "Tier-window revisit analysis" section for the full writeup).** S2's combined-constellation revisit (~5d) is reasonably matched by the existing tiers — no change warranted. S1's same-relative-orbit revisit was **measured live against CDSE** (Rawalpindi + Karachi, last 90 days, `Attributes`-expanded OData query): overwhelmingly **11 days**, with occasional 6-day and rare 12-day gaps — i.e. still effectively single-satellite cadence over Pakistan, NOT the newer post-2026-06-24 Sentinel-1C/1D 6-day constellation repeat (that faster cadence is concentrated over Europe per ESA/ASF acquisition planning, not global). This confirms tiers 2 (±3d) and 3 (±7d) are structurally unable to find a second same-orbit S1 pass over this AOI class — 7 < 11 regardless of window tuning — matching CLAUDE.md's live tier-1-3-exactly-0% finding exactly. **Left unchanged**: the empty-tier cost is cheap (zero downloads attempted, a single skipped loop iteration — `build_coverage_tiers`'s `if not in_window: continue`), and any widening would touch validated, live-tested tier behavior for an unverified benefit. This measurement is also a direct input to the planned S1 change-detection work (§10 #8's bi-temporal NDVI gap and any future S1 pre/post differencing), since a valid pre-event reference scene must come from the same relative orbit as the post-event one. | correctness (design), resolved | — | — (analysis only, no code change) | this session's live CDSE probe; CLAUDE.md |

---

## Appendix — Where This Document Confirms vs. Corrects the Existing Docs

- **Confirms** (re-verified against current code, not merely re-stated):
  BUG 1 (GCP/CRS clip collapse) is fixed and unit-tested; BUG 2 (valid-pixel
  coverage + SCL cloud masking) is implemented as described; BUG 3 (tiered
  temporal-coherence search, 100%-or-fail) is implemented as described; BUG 4
  (dedup/pre-intersection/doomed-streak) is implemented as described; BUG 5
  (explicit `index_calibrated`/`index_units` contract) is implemented and the
  cross-validator correctly gates on it; BUG 6 (`stance_engine.py`,
  `decide_landsat_fallback`) is confirmed deleted, not merely dormant; BUG 7
  (guaranteed `finally`-block cleanup + per-stage RSS instrumentation) is
  implemented as described; the CDSE `TokenManager` proactive-refresh fix is
  implemented as described.
- **Corrects/extends** `root_cause.md`/`fix.md` (both pre-migration): the
  specific `validation_input` SAR-labeled-as-NDWI bug they found **inside this
  agent** is fixed (assertion + correct construction, agent.py:731-745); the
  **same defect class survives one hop downstream**, in
  `agents/hazard/analyzer.py`'s deterministic fallback, which those documents
  correctly anticipated as a likely pattern but had not traced into the
  current hazard code (confirmed here in §5.2/§10 #4). The satellite
  confidence→hazard-not-reading-it gap CLAUDE.md flagged as "diagnosed only,
  not fixed" is confirmed still true by direct grep of the current
  `agents/hazard/analyzer.py` (§4.5/§10 #5).
- **New findings not previously documented anywhere in this repo:**
  `COVERAGE_MOSAIC_THRESHOLD`/`select_mosaic_scenes` are orphaned by the
  tiered-coverage redesign (§9, §10 #11); the coverage-tier day windows are
  not derived from either satellite's revisit cycle and likely make S1's tier
  2 a near-no-op (§10 #13); the confidence tracker's inability to distinguish
  "no evidence gathered" from "evidence contradicts" is traced to its exact
  arithmetic root cause here (§4.3-4.4), not just asserted as a symptom.
