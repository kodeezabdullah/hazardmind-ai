# Satellite Agent — Deep-Dive Technical Audit

**Scope:** `agents/satellite/` only. Source-level audit (not docstrings) of the
LangGraph node, pipeline entry, Sentinel selection/search, and the
download→stack→clip→indices→PNG→vectorize processor. Focus per request: SAR/CRS
handling, the clip/footprint logic, **BUG B** (S1 GRD clip collapse), and
confidence/timing weak points.

Files read in full: `node.py`, `agent.py`, `sentinel.py`, `processor.py`,
`confidence_tracker.py`, `cross_validator.py` (partial), `r2_upload.py`.

---

## 1. INPUT / OUTPUT CONTRACT

### 1.1 Input (what the node receives)

Entry is [`satellite_node(state)`](agents/satellite/node.py#L19) which reads from
the shared `PipelineState` and constructs a `ProcessDisasterInput`
([agent.py:199](agents/satellite/agent.py#L199)):

| Field | Type | Source | Notes |
|---|---|---|---|
| `event_id` | `str` (full UUID) | `state["event_id"]` | Set once by backend; never regenerated here |
| `location` | `str` | `state["location"]` | e.g. `"Rawalpindi, Pakistan"` |
| `disaster_type` | `str` | `state["disaster_type"]` | `flood` \| `earthquake` \| `landslide` |
| `magnitude` | `Optional[float]` | `state.get("magnitude")` | Passed through; **not used** by any satellite logic |
| `raw_message` | `Optional[str]` | *never set by node* | Declared on the model but `node.py` doesn't pass it → always `None`, so the alert text falls back to `f"{disaster_type} in {location}"` at [agent.py:421](agents/satellite/agent.py#L421) |

There is **no upstream DB read** — the satellite stage is the first pipeline
node, so its only input is the three identity fields from state.

### 1.2 Output — state update

On success `satellite_node` returns
([node.py:52](agents/satellite/node.py#L52)):

```python
{
    "satellite_result": result,     # full structured dict (below)
    "status": "hazard",             # advance
    "current_step": "satellite",
    "progress": 25,
    "confidence_scores": {..., "satellite": result["confidence"]},
}
```

On failure it returns `status: "failed"` and appends to `state["errors"]`
(never raises).

### 1.3 Output — the `structured` result dict

Built at [agent.py:749](agents/satellite/agent.py#L749). Mirrors the
`satellite_results` DB columns plus extras:

`event_id, status, satellite_type, cloud_cover, selection_reason, index_type,
water_percent, mean_index, class_counts, affected_area_km2, bbox, bounds,
region_boundary, risk_cities, true_color_url, index_url, classification_url,
geojson_url, image_url, cached, cities[], interpretation, confidence, concerns,
validations, needs_verification, should_alert, summary_message`.

### 1.4 Persistence

Two side-effect sinks, both inside `_run_pipeline_sync`:

**Cloudflare R2** — [`upload_all_results`](agents/satellite/r2_upload.py#L219)
writes under `events/<event_id>/`:
- `true_color.png`, `index_map.png`, `classification.png` (RGBA, ACL `public-read`)
- `zones.geojson`
- Per-city sets under `events/<event_id>/cities/<slug>/` (currently never
  produced — see §5).

**Postgres (Neon)** — [`_persist_satellite_result`](agents/satellite/agent.py#L66)
`DELETE`s then `INSERT`s one `satellite_results` row. Columns written:
`event_id, satellite_type, cloud_cover, scene_id, true_color_url, index_url,
classification_url, geojson_url, affected_area_km2, damage_percent, total_zones,
bounds, bbox, risk_cities`.

> **Contract gap (I/O):** The INSERT writes `scene_id`, `damage_percent`,
> `total_zones` — but the `structured` dict **never sets those keys**
> (`structured.get("scene_id")` → `None`, etc.). So `scene_id`,
> `damage_percent`, `total_zones` are persisted as `NULL` on every run. Not
> fatal, but the DB row is thinner than the code implies.

---

## 2. STEP-BY-STEP LOGIC (`_run_pipeline_sync`, [agent.py:372](agents/satellite/agent.py#L372))

1. **Process-once guard** — if `event_id ∈ _completed_event_ids`, return
   `{"status":"complete","already_processed":true}` without re-running.
2. **IP1 — parse + ambiguity gate.** `intelligence.parse_disaster_input(raw)`
   (LLM). Since `raw_message` is never set, `raw = "flood in Rawalpindi"`. A
   clarification is returned **only** when the profile is `ambiguous` AND a core
   field is genuinely absent from both explicit args and the parse — so a normal
   dispatch never trips it.
3. **Region boundary** — `get_region_boundary(location)`; `_error` if `None`.
4. **Risk cities** — `detect_risk_cities(location, disaster_type)` from the
   curated `_RISK_CITY_MAP` ([agent.py:156](agents/satellite/agent.py#L156)),
   else the headline token. `get_risk_city_boundaries` → `merge_risk_boundaries`
   → `get_analysis_bbox`. Any `None` → `_error`.
5. **Demo cache** — `check_demo_cache(event_id)` only fires for literal ids
   `peshawar`/`dhaka`/`kathmandu` (a UUID never matches), so this is effectively
   dead for production runs.
6. **CDSE auth** — `_authenticate_with_recovery` (≤3 attempts, LLM recovery on
   `copernicus_auth_failed`, bounded ≤10 s delay).
7. **IP2 — satellite selection.** `select_satellite(disaster_type, bbox, token)`
   ([sentinel.py:124](agents/satellite/sentinel.py#L124)). Cloud-cover peek
   decides optical-vs-SAR; LLM strategy is logged only (deterministic wins).
8. **IP3 — scene search.** `_search_with_recovery` widens 7→14→30 days on empty
   results. Then `backfill_uncovered_cities` re-queries per uncovered city.
9. **Processing** — `process_satellite_imagery(...)`
   ([processor.py:1624](agents/satellite/processor.py#L1624)). Chains
   download→stack→clip→indices→PNG→vectorize, coverage-aware with mosaic +
   fallback. Returns the merged result, a `coverage_insufficient` marker, or
   `None`.
10. **R2 upload** (merged + per-city) → `cleanup_event_temp`.
11. **Cross-validation** — `cross_validator.validate_all` feeds the
    `ConfidenceTracker` (GDACS/USGS/cloud/index/coverage/LLM-expert).
12. **IP4 — interpretation** (LLM), folded into the tracker as weighted evidence.
13. **IP6 — confidence gate.** Below `MIN_CONFIDENCE=0.6` OR `needs_verification`
    OR `should_alert` → `_recover("low_confidence")` (logged), result still ships.
14. Build `structured`, generate IP5 hand-off summary, `_persist_satellite_result`,
    mark complete.

### Branch triggers worth flagging
- **Mosaic vs single scene** ([processor.py:1690](agents/satellite/processor.py#L1690)):
  `best_overlap*100 < 85` AND `len(scenes) > 1`.
- **Candidate rejection** ([processor.py:1731](agents/satellite/processor.py#L1731)):
  `valid_percent < MIN_VALID_PIXEL_PERCENT (5%)` → try next candidate; all fail →
  `coverage_insufficient`.
- **S2 Nodes vs whole-zip** ([processor.py:754](agents/satellite/processor.py#L754)):
  per-band Nodes download is primary; whole-zip is fallback (and the **only**
  path for S1, since Nodes mapping only knows the S2 L1C layout,
  [processor.py:462](agents/satellite/processor.py#L462)).

### External API calls (exact params)
- **CDSE token** ([sentinel.py:216](agents/satellite/sentinel.py#L216)): POST
  password grant, `client_id=cdse-public`.
- **Catalogue peek** ([sentinel.py:92](agents/satellite/sentinel.py#L92)):
  `$filter=Collection/Name eq 'SENTINEL-2' and OData.CSC.Intersects(...) and
  ContentDate/Start gt <start>`, `$orderby=ContentDate/Start desc`, `$top=10`,
  `$expand=Attributes`.
- **Catalogue search** ([sentinel.py:598](agents/satellite/sentinel.py#L598)):
  same intersect/date filter, `$top=100`, plus per-mission filters (see §3.1).
- **Download** ([processor.py:263](agents/satellite/processor.py#L263)): streamed
  GET, `(connect=15, read=90)s` timeout, `~7min` outage budget, **no Range**
  (CDSE ignores it).

---

## 3. SAR / SATELLITE ANALYSIS SPECIFICS

### 3.1 Product selection (GRD vs SLC vs RAW) — the BUG A fix

For Sentinel-1, the catalogue query is filtered to GRD up front
([sentinel.py:615-623](agents/satellite/sentinel.py#L615)):

```python
    elif satellite_type == SENTINEL_1:
        # Restrict to GRD (Ground Range Detected) products. The S1 catalogue also
        # returns RAW (level-0, `..._RAW__0S...`) and SLC products; RAW carries
        # unfocused echo data with NO VV/VH measurement GeoTIFFs, so
        # processor._extract_bands finds no bands and the (multi-GB) download is
        # wasted, then the next candidate — often also RAW — is fully downloaded
        # and fails identically. Only GRD carries the analysis-ready VV/VH TIFFs
        # the pipeline needs, so the catalogue query is filtered to it up front.
        filters.append("contains(Name,'GRD')")
```

Belt-and-suspenders re-check before download
([processor.py:727-736](agents/satellite/processor.py#L727)):

```python
        if satellite_type == "sentinel-1":
            name = (scene.get("Name") or "").upper()
            if "GRD" not in name:
                logger.warning(
                    "Skipping non-GRD Sentinel-1 product %s (no VV/VH bands) "
                    "without downloading",
                    scene.get("Name"),
                )
                rejected_non_grd += 1
                continue
```

> **BUG A is genuinely fixed** — `contains(Name,'GRD')` at the catalogue level
> plus a pre-download guard. Note `contains(Name,'GRD')` also admits `GRD-COG`
> products (Cloud-Optimized GeoTIFF variant), which is fine — they still carry
> GRD VV/VH — but the band matcher (§3.2) has never been tested against COG
> member naming.

### 3.2 Polarization bands (VV/VH)

`_S1_POLARIZATIONS = ["VV", "VH"]` ([processor.py:92](agents/satellite/processor.py#L92)).
Match logic ([processor.py:581-590](agents/satellite/processor.py#L581)):

```python
    if satellite_type == "sentinel-1":
        # SAR measurement tiffs carry the polarization in the filename, e.g.
        # s1a-iw-grd-vv-...tiff
        cand = [
            m
            for m in members
            if m.lower().endswith((".tiff", ".tif"))
            and f"-{token.lower()}-" in m.lower()
        ]
        return cand
```

Only **VV** is actually used to compute the index (VH is downloaded, stacked,
then ignored — [processor.py:1067](agents/satellite/processor.py#L1067)):

```python
    if satellite_type == "sentinel-1":
        vv = bands.get("VV")
        if vv is None:
            logger.error("Sentinel-1 VV band missing; cannot compute SAR index")
            return None
        # GRD products are linear power; convert to dB. Guard non-positive.
        index = np.full_like(vv, np.nan, dtype="float32")
        finite = np.isfinite(vv) & (vv > 0)
        index[finite] = 10.0 * np.log10(vv[finite])
```

**Why VV:** VV backscatter drops over smooth open water (specular reflection
away from the sensor), so low VV dB → water — the standard SAR flood signature.
VH would add a co-/cross-pol ratio for vegetation/urban discrimination but isn't
used, so it's pure wasted download/stack cost.

### 3.3 Preprocessing chain — **what's implemented vs missing**

| Step | Status | Evidence |
|---|---|---|
| Radiometric **calibration** (DN → σ⁰/β⁰) | **MISSING** | The code does `10*log10(VV)` on the **raw GRD DN values**, treating them as "linear power" ([processor.py:1074](agents/satellite/processor.py#L1074)). GRD pixels are amplitude/intensity DNs, not calibrated backscatter. The `-15 dB` water threshold ([processor.py:133](agents/satellite/processor.py#L133)) is only meaningful on **calibrated σ⁰** — applied to raw DNs it's physically arbitrary. |
| **Speckle filtering** (Lee/Refined-Lee) | **MISSING** | No filter anywhere; SAR speckle noise passes straight into the threshold classifier, inflating false water pixels. |
| **Thermal noise removal** | **MISSING** | Not implemented. |
| **Terrain correction / orthorectification (RTC)** | **MISSING** | No GCP/RPC warp, no DEM, no `WarpedVRT`. This is the direct cause of **BUG B** (§4). |
| S2 **atmospheric correction** | **N/A (by design)** | Pipeline uses **L1C** (TOA reflectance), filtered via `contains(Name,'MSIL1C')`. NDWI/NDVI on TOA is acceptable for relative change but not surface reflectance. |
| Band **resampling** (20 m → 10 m) | **IMPLEMENTED** | `Resampling.bilinear` in `stack_bands` ([processor.py:840](agents/satellite/processor.py#L840)). |

> **Bottom line:** the SAR path is essentially a raw-DN log-ratio with a
> hardcoded dB threshold on unprojected data. It is **not** a
> calibrate→speckle→terrain-correct chain. The S2 path is well-built; the S1
> path is a stub dressed as a pipeline.

### 3.4 CRS handling — where reprojection happens

There is **no raster reprojection anywhere**. The only CRS operations are on
**vector geometry**:

`stack_bands` reads whatever CRS/transform the band file reports
([processor.py:827-830](agents/satellite/processor.py#L827)):

```python
        with rasterio.open(band_paths[ref_token]) as ref:
            ref_h, ref_w = ref.height, ref.width
            ref_transform = ref.transform
            ref_crs = ref.crs
```

The clip **reprojects the WGS84 polygon into the raster CRS**
([processor.py:901-906](agents/satellite/processor.py#L901)):

```python
    # Reproject the WGS84 polygon to the raster CRS.
    try:
        if crs is not None and crs.to_epsg() != 4326:
            geom = transform_geom("EPSG:4326", crs, merged_polygon)
        else:
            geom = merged_polygon
```

`vectorize_classification` reprojects polygons **back to WGS84**
([processor.py:1355-1356](agents/satellite/processor.py#L1355)):

```python
                if crs is not None and crs.to_epsg() != 4326:
                    poly = shape(transform_geom(crs, "EPSG:4326", mapping(poly)))
```

`_compute_bounds` reprojects the clip extent corners to WGS84
([processor.py:1450](agents/satellite/processor.py#L1450)) via `transform_bounds`.

**For S2** this works: `ref.crs` is a real UTM zone (e.g. EPSG:326xx) and
`ref.transform` is a proper affine → the polygon reprojects correctly into UTM
pixel space. **For S1 GRD it breaks** — see §4.

### 3.5 Clipping / footprint logic (full trace)

`clip_to_polygon` ([processor.py:876](agents/satellite/processor.py#L876)):

1. Reproject WGS84 polygon → raster CRS (`geom`).
2. Compute the geometry's **pixel window** in the cube using the raster's
   affine ([processor.py:927-934](agents/satellite/processor.py#L927)):

```python
    # Pixel offsets of the geometry bbox in the cube (transform.e is negative).
    px0 = int(np.floor((gminx - transform.c) / transform.a))
    px1 = int(np.ceil((gmaxx - transform.c) / transform.a))
    py0 = int(np.floor((gmaxy - transform.f) / transform.e))
    py1 = int(np.ceil((gminy - transform.f) / transform.e))
    win_c0 = max(0, min(px0, px1))
    win_c1 = min(w, max(px0, px1))
    win_r0 = max(0, min(py0, py1))
    win_r1 = min(h, max(py0, py1))
```

3. Rasterize a mask over just that window (`rio_mask(..., crop=True, nodata=0)`),
   crop every band to it, set outside-polygon → NaN.
4. `_valid_pixel_percent` measures finite-non-zero pixels inside the mask
   ([processor.py:1395](agents/satellite/processor.py#L1395)).

**This step is exactly where BUG B lives.** The window math assumes
`transform.c/f/a/e` are geographic/UTM map coordinates. When the transform is a
**pixel-index identity** (S1 GRD case), `(gminx - transform.c)/transform.a`
computes `(UTM_metre_value - 0)/1` — mapping a ~300000 m easting onto pixel
index 300000, far outside the raster → the window clamps to a 1-2 px sliver.

### 3.6 Cloud masking & the Rawalpindi 45.9% issue

There is **no per-pixel cloud masking** (no QA60/SCL band). Cloud handling is
entirely at **scene selection**:

- S2 catalogue is pre-filtered to `cloudCover < 30%`
  ([sentinel.py:604-610](agents/satellite/sentinel.py#L604)).
- `select_satellite` peeks the least-cloudy recent S2 scene; `> 30%` →
  routes to **Sentinel-1** ([sentinel.py:170-176](agents/satellite/sentinel.py#L170)).

So the **Rawalpindi 45.9% cloud** reading is precisely what *should* force the
SAR route — cloud > 30% → S1 GRD. That routing decision is correct; the problem
is the S1 GRD path it routes **into** is broken (BUG B). The
45.9% → SAR → 1×2px collapse is a single causal chain: the correct cloud
decision hands off to a non-functional processor.

> Cross-validation also flags cloud: `>60%` → CRITICAL, `>30%` → MEDIUM concern
> (cross_validator), which is what drops the confidence to ~0.27 (§5).

---

## 4. BUG B ROOT-CAUSE ANALYSIS

**Symptom:** S1 GRD clip collapses to ~1×2 px / 0% valid despite the scene
footprint claiming ~93% overlap of the AOI.

### 4.1 The two-CRS mismatch

The "93% overlap" and the actual clip are computed in **different coordinate
systems, against different georeferencing**:

1. **Overlap (93%)** is computed in `sentinel._scene_aoi_overlap`
   ([sentinel.py:266](agents/satellite/sentinel.py#L266)) by intersecting the
   AOI polygon with the scene's **`GeoFootprint`** — a WGS84 GeoJSON polygon
   from the **catalogue metadata**. This is correct and in EPSG:4326. It says
   "this scene's footprint covers 93% of the cities." **True.**

2. **The clip** is computed in `clip_to_polygon` against the **opened raster's
   `transform`/`crs`** ([processor.py:897-899](agents/satellite/processor.py#L897)).
   For an S1 GRD `measurement/*.tiff`, the georeferencing is stored as **GCPs
   (ground control points), not an affine geotransform**. A plain
   `rasterio.open()` on such a file returns:
   - `crs = None` (or occasionally a bare `EPSG:4326` with no useful transform), and
   - `transform = Affine.identity()` = `(1, 0, 0, 0, 1, 0)` — pixel-index space.

   The GCP georeferencing (`ds.gcps`) is **never read** — the pipeline has no
   `gcps`/`rpcs`/`WarpedVRT`/`reproject` call anywhere (verified: grep for
   `gcp|rpc|reproject|WarpedVRT` in `processor.py` → nothing).

### 4.2 The exact collapse

In `clip_to_polygon`:

```python
    crs = stacked["crs"]          # None for S1 GRD
    ...
    if crs is not None and crs.to_epsg() != 4326:
        geom = transform_geom("EPSG:4326", crs, merged_polygon)
    else:
        geom = merged_polygon      # <-- taken: geom stays in WGS84 degrees
```

`crs is None` → the `else` branch keeps `geom` in **WGS84 degrees** (e.g.
lon ≈ 73.0, lat ≈ 33.6 for Rawalpindi). Then the window math runs with
`transform = Affine.identity()` so `transform.a = 1, transform.c = 0,
transform.e = 1, transform.f = 0`:

```python
    px0 = floor((73.0 - 0) / 1) = 73
    px1 = ceil ((73.1 - 0) / 1) = 74
    py0 = floor((33.7 - 0) / 1) = 33   # transform.e = +1 here, not negative!
    py1 = ceil ((33.6 - 0) / 1) = 34
```

The polygon's **degree** coordinates (~73, ~34) are interpreted as **pixel
indices** → a window of roughly **1 column × 1-2 rows** at pixel (73, 33) of a
~25000×16000 GRD raster. That tiny window has essentially no in-polygon valid
pixels → **`valid_percent ≈ 0`**, every candidate is rejected
([processor.py:1731](agents/satellite/processor.py#L1731)), and the run ends in
`coverage_insufficient` (or produces a degenerate 1×2 clip).

The comment at line 926 ("transform.e is negative") is itself an S2-only
assumption — for the identity transform `transform.e = +1`, which also flips the
row window, compounding the collapse.

### 4.3 Answering the audit's specific checks

- *Is the AOI polygon in the same CRS as the image bounds at intersection?*
  **No.** At the clip, the polygon is WGS84 degrees while the "image bounds" are
  an identity pixel grid — an unresolved GCP raster. They are not comparable.
- *Is the 93% overlap computed in a different CRS than the clip?*
  **Yes.** Overlap uses the WGS84 catalogue `GeoFootprint` (metadata, correct);
  the clip uses the raster's absent/identity transform (GCPs unresolved). The
  metadata knows where the scene is; the opened pixel grid does not.

### 4.4 Concrete fix

**Resolve the S1 GCP georeferencing into a real projected grid before stacking**,
so `clip_to_polygon` receives a valid UTM `crs`/`transform` exactly like S2.
Warp GCP-georeferenced bands with a `WarpedVRT` in `stack_bands`, and only then
build the reference grid.

Add a helper in `processor.py`:

```python
from rasterio.vrt import WarpedVRT
from rasterio.warp import calculate_default_transform

def _open_georeferenced(path: str):
    """Open a band, resolving GCP georeferencing (S1 GRD) into a real CRS/grid.

    S1 GRD measurement TIFFs carry GCPs, not an affine geotransform: a plain
    rasterio.open() reports crs=None and an identity transform, which breaks the
    downstream clip (BUG B). When GCPs are present we wrap the dataset in a
    WarpedVRT that reprojects into the GCPs' own CRS (WGS84) with a proper
    affine, so every downstream step gets valid georeferencing. S2 (already
    affine-georeferenced) passes through unchanged.
    """
    src = rasterio.open(path)
    gcps, gcp_crs = src.get_gcps()          # ([], None) for S2 → no-op
    if gcps and (src.crs is None):
        dst_crs = gcp_crs or "EPSG:4326"
        transform, width, height = calculate_default_transform(
            gcp_crs, dst_crs, src.width, src.height, gcps=gcps,
        )
        vrt = WarpedVRT(
            src, crs=dst_crs, transform=transform, width=width, height=height,
            resampling=Resampling.bilinear,
        )
        return vrt, src          # return src too so caller can close it
    return src, None
```

Then in `stack_bands`, replace every `rasterio.open(path)` with
`_open_georeferenced(path)`, read from the returned dataset, and close both
handles. After this, for an S1 GRD scene `ref.crs` is a real CRS and
`ref.transform` is a proper affine, so:
- `clip_to_polygon`'s `crs.to_epsg() != 4326` branch reprojects the polygon
  correctly (or, if warped to 4326, the degree-based window math is now valid
  because the transform is a real geographic affine, not identity), and
- the window math maps AOI coordinates to the correct pixels → ~93% of the AOI
  is retained, matching the footprint overlap.

**Preferred variant:** warp S1 to the AOI's **UTM zone** (not 4326) so pixels
stay square-ish in metres and NDWI-equivalent thresholds/areas are metric —
compute the UTM EPSG from the AOI centroid and pass it as `dst_crs`. This also
sidesteps the `transform.e` sign assumption at
[processor.py:929](agents/satellite/processor.py#L929).

**Belt-and-suspenders guard** (independent of the warp): in `clip_to_polygon`,
reject a degenerate transform early so a future regression can't silently ship a
1×2 px clip:

```python
    if crs is None or transform == rasterio.Affine.identity():
        logger.error(
            "Raster has no usable georeferencing (crs=%s, identity transform); "
            "GCP resolution likely failed — refusing to clip", crs)
        return None
```

---

## 5. OTHER RISKS / WEAK POINTS

### 5.1 The `confidence = 0.27` is not trustworthy as a data-quality signal

`confidence` in the output is the `ConfidenceTracker.overall_confidence()`
([confidence_tracker.py:114](agents/satellite/confidence_tracker.py#L114)): a
weighted average of evidence **minus flat per-concern penalties** (LOW .05 /
MED .10 / HIGH .20 / CRITICAL .35). Problems:

- It **conflates "the disaster is uncertain" with "our imagery is broken."** A
  45.9%-cloud Rawalpindi run adds a MEDIUM cloud concern **and** the SAR clip
  fails — but the number that surfaces (~0.27) reads as "low confidence in the
  hazard," not "the SAR processor produced garbage." A consumer can't tell a
  legitimately-uncertain result from a pipeline failure.
- Penalties are **flat and additive**, so 3 MEDIUM concerns (−0.30) can crater a
  0.9-evidence result to 0.6 regardless of how strong the evidence is —
  arbitrary weighting, not calibrated.
- With **no evidence yet** it returns `0.0` ([confidence_tracker.py:120](agents/satellite/confidence_tracker.py#L120)),
  so a scene where every LLM/feed call failed reports `0.0` — indistinguishable
  from "strongly contradicted."

**Verdict:** 0.27 is a *heuristic morale score*, not a measured reliability
figure. It should not gate downstream decisions on its own, and it can't
substitute for the missing valid-pixel / clip-success signal (which is exactly
what BUG B needs surfaced).

### 5.2 Per-city artifacts are permanently dead

`run_pipeline` calls `process_satellite_imagery(..., city_boundaries=None)`
([agent.py:569](agents/satellite/agent.py#L569)), and the per-city render only
runs when `city_boundaries and len(...) > 1`
([processor.py:1772](agents/satellite/processor.py#L1772)). So `result["cities"]`
is **always empty**, the per-city upload loop
([agent.py:613](agents/satellite/agent.py#L613)) never iterates, and
`structured["cities"] = []` on every run. All the `_render_per_city` machinery is
dead weight. Either wire it back on or delete it.

### 5.3 DB persist writes NULLs for `scene_id` / `damage_percent` / `total_zones`

As noted in §1.4 — the INSERT names these columns but `structured` never
populates them. `total_zones` in particular is computed locally (`len(features)`
at [agent.py:687](agents/satellite/agent.py#L687)) yet **not** copied into
`structured`, so it's lost to the DB. Cheap fix: add
`"total_zones": total_zones` and `"scene_id": <selected scene Name/Id>` to the
`structured` dict.

### 5.4 Silently-swallowed failures

- `_persist_satellite_result` catches **all** exceptions and only `warning`s
  ([agent.py:127](agents/satellite/agent.py#L127)) — a DB write failure produces
  a "complete" pipeline with no persisted row; `GET /results` then 404s a
  successful run.
- `_polygon_area_km2` `except Exception` → returns **degrees²** as if km²
  ([processor.py:1306](agents/satellite/processor.py#L1306)). If the pyproj
  transform ever fails, `affected_area_km2` becomes a nonsense tiny number with
  no error surfaced.
- The whole `_run_pipeline_sync` is wrapped in a blanket
  `except Exception` → `_error` ([agent.py:823](agents/satellite/agent.py#L823)),
  so any bug reads as a generic "Unexpected error" with no stack context.

### 5.5 Timing — 204.7 s even with cached bands

Cached bands eliminate download, so the 204.7 s is dominated by CPU/serial work.
Likely contributors, in rough order:

1. **`asyncio.run()` called mid-pipeline, twice** — `_persist_satellite_result`
   ([agent.py:125](agents/satellite/agent.py#L125)) and `cleanup_event_temp`
   ([agent.py:652](agents/satellite/agent.py#L652)) each spin up a fresh event
   loop inside the sync worker. `asyncpg.connect()` per write (no pool) adds
   connection + TLS latency on every run.
2. **Serial LLM calls** — IP1 parse, IP2 strategy, IP4 interpret, IP5 hand-off
   message, plus the cross-validator's "Featherless expert" call, each with a
   30 s per-model timeout and a fallback chain. On a slow/So degraded provider
   these alone can burn 60-120 s and are **not on the imagery critical path** —
   they're pure narrative.
3. **Vectorization** — `rasterio.features.shapes` + per-polygon pyproj reprojection
   + shapely simplify over the full classification array
   ([processor.py:1346](agents/satellite/processor.py#L1346)) is single-threaded
   and scales with zone count (the Mindanao 251-zone run is documented as
   multi-minute).
4. **`np.percentile` stretch** on full-res arrays before decimation in
   `export_png` (percentile is computed on the un-decimated index for
   `index_map`, [processor.py:1235](agents/satellite/processor.py#L1235)).

**Cheap wins:** make LLM narrative calls concurrent (or fire-and-forget /
skip when not needed), reuse a single asyncpg connection/pool instead of
`asyncio.run(connect())` per write, and compute the stretch percentiles on the
already-decimated array.

### 5.6 Recency decay can pick a stale scene for a *current* disaster

`_scene_score` multiplies by a 20-day-half-life recency factor
([sentinel.py:314](agents/satellite/sentinel.py#L314)). For flood verification
you want the acquisition **during/after** the event; a gentle half-life means a
well-covered clear scene from *before* the flood can outscore a newer one — the
pipeline has no notion of the disaster date vs acquisition date (there's no
event timestamp input at all).

---

## PRIORITIZED ISSUES + FIXES

1. **[BUG B — CRITICAL] S1 GRD clip collapses because GCP georeferencing is
   never resolved.** The clip runs against `crs=None` + identity transform, so
   WGS84 degree coords are read as pixel indices → 1-2 px window → 0% valid →
   `coverage_insufficient`. **Fix:** add `_open_georeferenced()` (WarpedVRT over
   `src.get_gcps()`, ideally warping to the AOI's UTM zone) and route every
   `rasterio.open()` in `stack_bands` through it; add a degenerate-transform
   guard in `clip_to_polygon` (§4.4). This is the difference between the SAR path
   working at all and never producing output.

2. **[HIGH] SAR is uncalibrated + unfiltered.** `10*log10(raw GRD DN)` with a
   `-15 dB` threshold on unprojected, unspeckle-filtered data is physically
   meaningless. **Fix:** apply CDSE/SNAP-style calibration to σ⁰ (or at minimum
   document the threshold as empirical DN-space), add a Lee/Refined-Lee speckle
   filter before classification, and note that terrain correction is handled by
   the WarpedVRT from fix #1. Until then, treat all SAR water numbers as
   indicative only.

3. **[HIGH] DB write can silently fail yet report "complete."**
   `_persist_satellite_result` swallows every exception as a warning, so a failed
   INSERT yields a successful pipeline with no row → `GET /results` 404s. **Fix:**
   on persist failure, set the node result to `failed` / append to
   `state["errors"]` rather than returning "complete" with no durable record.

4. **[MEDIUM] `confidence` (0.27) is a heuristic morale score, not reliability.**
   It conflates disaster-uncertainty with pipeline-failure and returns 0.0 when
   all evidence sources are simply unreachable. **Fix:** surface a separate,
   explicit `data_quality`/`valid_percent`/`clip_ok` signal from the processor so
   consumers can distinguish "uncertain hazard" from "broken imagery"; stop using
   `confidence` alone as a gate.

5. **[MEDIUM] Persisted `scene_id` / `damage_percent` / `total_zones` are always
   NULL.** The INSERT names them but `structured` never sets them (`total_zones`
   is even computed then dropped). **Fix:** add those keys to the `structured`
   dict before persist (§5.3).

6. **[MEDIUM] Dead per-city artifact path.** `city_boundaries=None` makes
   `result["cities"]` always empty; ~120 lines of `_render_per_city` + the
   per-city upload loop never execute. **Fix:** wire it back on (pass
   `city_polys`) or delete the machinery.

7. **[LOW] Timing ~205 s on cached bands.** Per-write `asyncio.run(asyncpg.connect())`,
   serial 30 s-timeout LLM narrative calls off the critical path, and full-res
   percentile stretch dominate. **Fix:** pool the DB connection, run/skip LLM
   narrative concurrently, compute stretches on decimated arrays (§5.5).

8. **[LOW] VH downloaded but unused; recency decay ignores event date.** VH costs
   a full band download/stack for nothing; recency can prefer a pre-event scene.
   **Fix:** either drop VH from `_S1_POLARIZATIONS` or use a VV/VH ratio; feed the
   disaster date into scene scoring so post-event acquisitions win.
