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
- [ ] Wire agent into Band rooms and publish results

## Core Logic

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

### Step 4: Image download + processing (`processor.py`) — DONE

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
  and the `python processor.py` end-to-end smoke test need live CDSE creds.

### Step 3: Sentinel selection + Copernicus auth (`sentinel.py`) — DONE

Picks the Sentinel mission for a disaster, authenticates to the Copernicus Data
Space Ecosystem (CDSE), and finds the best scene over a bbox.

- `select_satellite(disaster_type, cloud_cover=None)` — flood → Sentinel-1
  (SAR, weather-independent); earthquake/landslide → Sentinel-2 (optical).
  If `cloud_cover > 30%`, an optical choice is switched to Sentinel-1. Unknown
  disaster types default to optical. Pure logic, no network.
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
