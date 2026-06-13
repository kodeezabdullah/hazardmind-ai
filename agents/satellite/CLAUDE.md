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
- S2 flood → NDWI `(B03−B08)/(B03+B08)`, water where `> 0.3`.
- S2 earthquake/landslide → NDVI `(B08−B04)/(B08+B04)`, damage where `< 0.2`.
- S1 → VV backscatter in dB, smooth water where `< −15 dB`.
- Builds a `classification_array` (1 affected / 0 unaffected / 255 nodata) and
  returns `{index_type, array, classification_array, water_percent,
  mean_value, threshold_used}`.

**7F — `export_png(indices, clipped, event_id, disaster_type)`.** Writes three
PNGs to `<temp>/<event_id>/`:
- `true_color.png` — S2 TCI RGB (S1: VV greyscale).
- `index_map.png` — NDWI Blues / NDVI RdYlGn / SAR grey, transparent nodata.
- `classification.png` — semi-transparent RGBA overlay for the map (flood
  blue=water/white=land; earthquake red=damage/green=ok; landslide
  orange=scar/green=ok).

**7G — `vectorize_classification(classification_array, transform, crs,
disaster_type)`.** `rasterio.features.shapes` over the affected mask →
reproject to WGS84 → `shapely` simplify (0.001°) → drop polygons
`< 0.5 km²` (area measured via EPSG:6933). Each feature carries
`risk_type/area_km2/severity`; the FeatureCollection adds `total_area`.

**7H — `r2_upload.upload_all_results(event_id, files_dict)`.** Uploads
`true_color.png`, `index_map.png`, `classification.png` and `zones.geojson`
(serialised from the in-memory dict) under `events/<event_id>/`, returning
`{true_color_url, index_url, classification_url, geojson_url}`. The single-file
`upload_to_r2` is retained for the demo-cache path.

**7I — `processor.process_satellite_imagery(selection, scene, bbox,
merged_polygon, event_id, token, disaster_type)`.** Chains
download → stack → clip → indices → export → vectorize and returns
`{satellite_type, index_type, water_percent, mean_index, affected_area_km2,
png_paths, geojson}`.

**7J — `agent.py`.** `run_pipeline` resolves region + risk-city boundaries
first (so they're reported even on a demo-cache hit), authenticates to CDSE,
calls the cloud-aware `select_satellite(disaster_type, bbox, token)`, runs the
full pipeline over the merged polygon, then `upload_all_results`. The reply to
`@hazardmind-hazard` now carries `satellite_type, cloud_cover, index_type,
affected_area_km2, water_percent` and all four artifact URLs.

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
