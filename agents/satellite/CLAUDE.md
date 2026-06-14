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
