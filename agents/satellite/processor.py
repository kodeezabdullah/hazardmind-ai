"""Remote-sensing pipeline for the satellite agent.

Turns a CDSE scene (chosen by `sentinel.search_imagery`) plus a risk-area
polygon (from `boundary.py`) into web-ready map layers and vector zones for a
disaster. The full pipeline lives in `process_satellite_imagery`:

    download_imagery        # fetch + extract the bands we need
        -> stack_bands      # align bands into one numpy cube (resample to 10 m)
        -> clip_to_polygon  # mask to the actual risk geometry (not a rectangle)
        -> calculate_indices# NDWI / NDVI / SAR ratio + a classification mask
        -> export_png       # true_color, index_map, classification overlays
        -> vectorize_classification  # GeoJSON polygons of the affected zones

Mission-specific behaviour:
- Sentinel-2 (optical): downloads disaster-specific bands. Flood -> NDWI water
  detection; earthquake/landslide -> NDVI damage detection.
- Sentinel-1 (SAR): downloads VV+VH polarizations; flood detection from the
  backscatter (low VV -> smooth water).

CDSE delivers products as zipped `.SAFE` directories; the `$value` endpoint only
serves the whole archive, so we download it once (resumably) and extract the
specific band rasters into `<temp>/<event_id>/bands/`.

Every function logs and returns None on failure rather than raising, so a single
bad scene does not abort an analysis.

Run this file directly for a small smoke test:
    python processor.py
"""

import logging
import os
import re
import tempfile
import time
import zipfile
from typing import Optional

import numpy as np
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.features import shapes
from rasterio.mask import mask as rio_mask
from rasterio.merge import merge as rio_merge
from rasterio.warp import transform_bounds, transform_geom
from shapely.geometry import mapping, shape
from shapely.ops import transform as shapely_transform

logger = logging.getLogger(__name__)

# CDSE OData download endpoint. The product id from search_imagery is
# interpolated and the `$value` resource streams the zipped .SAFE archive.
DOWNLOAD_URL = (
    "https://catalogue.dataspace.copernicus.eu/odata/v1/Products({product_id})/"
    "$value"
)

# Where downloaded/extracted/exported files live. A dedicated subdirectory under
# the system temp dir keeps intermediate artifacts out of the repo.
TEMP_ROOT = os.path.join(tempfile.gettempdir(), "hazardmind-satellite")

# A single scene covering less than this percentage of the AOI triggers a
# multi-tile mosaic of the top-ranked scenes (FIX 2). Raised 60 -> 85 so
# scattered multi-city AOIs (best single tile still misses cities) mosaic.
COVERAGE_MOSAIC_THRESHOLD = 85.0
# How many top-scored scenes to mosaic when one scene is not enough.
MOSAIC_MAX_SCENES = 3
# After clipping, a result with fewer than this percentage of valid (non-nodata)
# pixels inside the risk polygon is rejected and the next scene is tried
# (FIX 3).
MIN_VALID_PIXEL_PERCENT = 5.0

# BUG 4c: if this many consecutive downloads within a tier fail to clip or add
# no coverage, abort the tier rather than working through every candidate.
DOOMED_DOWNLOAD_LIMIT = 3

# Per-tier date window (days) for log/anomaly text; mirrors sentinel.COVERAGE_TIERS.
COVERAGE_TIERS_DAYS = {1: 0, 2: 3, 3: 7, 4: 14}

# BUG 4d: cumulative bytes downloaded this process (for per-run logging). A
# simple module-level counter incremented by the streaming download helpers.
_BYTES_DOWNLOADED = 0


def _add_bytes_downloaded(n: int) -> None:
    """Record `n` freshly-downloaded bytes (BUG 4d logging)."""
    global _BYTES_DOWNLOADED
    if n and n > 0:
        _BYTES_DOWNLOADED += int(n)


def _bytes_downloaded_total() -> int:
    """Cumulative bytes downloaded so far this process."""
    return _BYTES_DOWNLOADED


# BUG 7 — per-stage peak-RSS instrumentation. Peak memory is 8-16 GB and rises
# with tile count; this records the peak resident-set size observed after each
# pipeline stage so we can see WHERE the peak occurs and how it scales with the
# number of tiles per mosaic. No processing is restructured — this only measures.
_STAGE_PEAK_RSS: dict = {}


def _rss_mb() -> float:
    """Current process resident-set size in MB, or 0.0 if psutil is absent."""
    try:
        import psutil

        return psutil.Process().memory_info().rss / 1e6
    except Exception:
        return 0.0


def _mem_stage(stage: str, tiles: Optional[int] = None) -> None:
    """Record peak RSS for a named stage (keeps the max seen per stage)."""
    rss = _rss_mb()
    if rss <= 0.0:
        return
    prev = _STAGE_PEAK_RSS.get(stage, {"peak_mb": 0.0, "tiles": tiles})
    if rss > prev["peak_mb"]:
        prev["peak_mb"] = round(rss, 1)
    if tiles is not None:
        prev["tiles"] = tiles
    _STAGE_PEAK_RSS[stage] = prev
    logger.info(
        "[MEM] stage=%s rss=%.1f MB%s",
        stage, rss, f" tiles={tiles}" if tiles is not None else "",
    )


def memory_report() -> dict:
    """Peak RSS per stage seen so far this process (BUG 7)."""
    if not _STAGE_PEAK_RSS:
        return {}
    peak_stage = max(_STAGE_PEAK_RSS, key=lambda s: _STAGE_PEAK_RSS[s]["peak_mb"])
    return {
        "per_stage": dict(_STAGE_PEAK_RSS),
        "peak_stage": peak_stage,
        "peak_mb": _STAGE_PEAK_RSS[peak_stage]["peak_mb"],
    }

# Sentinel-2 bands to download per disaster type. TCI (true-colour image) is
# always included for the true_color export. Keys are the band tokens that
# appear in JP2 filenames inside the .SAFE archive (e.g. "..._B03_10m.jp2").
# Sentinel-2 is now sourced as **L2A** (surface reflectance) so the Scene
# Classification Layer (SCL) is available for real cloud/shadow/cirrus masking of
# the coverage metric (BUG 2). SCL is included in every disaster's band set. NOTE
# (science phase, do not action this session): NDWI/NDVI values shift between L1C
# TOA and L2A surface reflectance, so the 0.3/0.5 NDWI and 0.2 NDVI thresholds
# were observed against L1C and REQUIRE REVALIDATION against L2A.
_S2_BANDS = {
    "flood": ["B03", "B08", "B11", "TCI", "SCL"],
    "earthquake": ["B02", "B04", "B08", "TCI", "SCL"],
    "landslide": ["B03", "B04", "B08", "TCI", "SCL"],
}
_S2_DEFAULT_BANDS = ["B04", "B03", "B02", "TCI", "SCL"]

# Native resolution (m) of each Sentinel-2 band we touch. 20 m bands (B11, SCL)
# are resampled to 10 m during stacking.
_S2_BAND_RES = {
    "B02": 10, "B03": 10, "B04": 10, "B08": 10,
    "B11": 20, "TCI": 10, "SCL": 20,
}

# SCL (Scene Classification Layer) class values to treat as invalid for the
# valid-pixel coverage metric: 0 no-data, 1 saturated/defective, 3 cloud shadow,
# 8 cloud medium-prob, 9 cloud high-prob, 10 thin cirrus, 11 snow/ice. Kept as
# valid: 2 dark/topo shadow, 4 vegetation, 5 bare soil, 6 water, 7 unclassified.
_SCL_INVALID_CLASSES = frozenset({0, 1, 3, 8, 9, 10, 11})

# Sentinel-1 polarizations.
_S1_POLARIZATIONS = ["VV", "VH"]

# CDSE serves the product bytes from a different host
# (download.dataspace.copernicus.eu) than the catalogue, via a 301 redirect.
# requests strips the Authorization header on cross-host redirects for safety,
# which makes the download endpoint return 401. Hosts we trust to keep carrying
# the Bearer token across that redirect.
_CDSE_AUTH_HOSTS = frozenset(
    {
        "catalogue.dataspace.copernicus.eu",
        "download.dataspace.copernicus.eu",
        "zipper.dataspace.copernicus.eu",
    }
)


class _CDSESession(requests.Session):
    """A requests Session that keeps the Bearer token across CDSE redirects.

    The product `$value` endpoint 301-redirects from the catalogue host to a
    download host. requests' default `rebuild_auth` drops the Authorization
    header on any host change, so we re-allow it when both the source and
    destination are trusted CDSE hosts.
    """

    def rebuild_auth(self, prepared_request, response):
        from urllib.parse import urlparse

        original = urlparse(response.request.url).hostname
        redirect = urlparse(prepared_request.url).hostname
        if original in _CDSE_AUTH_HOSTS and redirect in _CDSE_AUTH_HOSTS:
            return  # keep the Authorization header as-is
        super().rebuild_auth(prepared_request, response)


# Cap exported PNG longest side (pixels) to keep file size reasonable for web.
_MAX_PNG_DIMENSION = 1024

# Index thresholds (see calculate_indices).
NDWI_WATER_THRESHOLD = 0.3      # NDWI > this -> open water
NDVI_DAMAGE_THRESHOLD = 0.2     # NDVI < this -> bare/damaged ground
SAR_WATER_THRESHOLD_DB = -15.0  # VV backscatter < this dB -> smooth water

# Drop vectorized polygons smaller than this (km^2) as noise.
MIN_ZONE_AREA_KM2 = 0.5

# --------------------------------------------------------------------------- #
# Classification scheme
# --------------------------------------------------------------------------- #
# Classification arrays use graded hazard classes so the output is a real risk
# map, not a binary mask:
#   0   = unaffected / safe land    -> NOT drawn on the overlay (transparent)
#   1.. = increasing hazard severity-> drawn, deeper colour = worse
#   255 = nodata / outside the polygon
NODATA_CLASS = 255

# Per-index class definitions, ordered low->high severity. Each entry is
# (class_value, label, RGB colour, alpha). Pixels not matching any band stay 0.
# Thresholds are applied as: NDWI/SAR ascending bands, NDVI descending bands
# (low NDVI = more damage). See _classify().
_CLASS_SCHEMES = {
    "NDWI": {  # flood: more water = worse
        "order": "asc",
        "bands": [
            # (lower_bound, class_value, label, rgb, alpha)
            (0.0, 1, "wet_soil", (147, 197, 253), 150),    # light blue
            (0.3, 2, "water", (37, 99, 235), 200),         # blue
            (0.5, 3, "deep_water", (30, 58, 138), 220),    # dark blue
        ],
    },
    "SAR": {  # flood (radar): lower backscatter = smoother = water
        "order": "desc",
        "bands": [
            (-13.0, 1, "possible_water", (147, 197, 253), 150),
            (-15.0, 2, "water", (37, 99, 235), 200),
            (-18.0, 3, "deep_water", (30, 58, 138), 220),
        ],
    },
    "NDVI_QUAKE": {  # earthquake: lower NDVI = more bare/damaged
        "order": "desc",
        "bands": [
            (0.2, 1, "sparse_veg", (250, 204, 21), 150),   # yellow
            (0.1, 2, "stressed", (249, 115, 22), 190),     # orange
            (0.0, 3, "damage", (220, 38, 38), 220),        # red
        ],
    },
    "NDVI_LANDSLIDE": {  # landslide: lower NDVI = exposed scar
        "order": "desc",
        "bands": [
            (0.2, 1, "sparse_veg", (253, 224, 71), 150),   # pale yellow
            (0.1, 2, "exposed", (251, 146, 60), 190),      # light orange
            (0.0, 3, "scar", (234, 88, 12), 220),          # orange-red
        ],
    },
}


# --------------------------------------------------------------------------- #
# Step 7B: download + extract the bands we actually need
# --------------------------------------------------------------------------- #
# Network-resilience policy for the CDSE download (and, by extension, a
# mid-pipeline internet drop). A single int timeout in `requests` applies to
# BOTH connect and each read, so a dead mid-stream socket blocks for the whole
# value before raising — which froze the pipeline for minutes during an outage.
# We instead use a (connect, read) tuple so a stall is detected quickly, and we
# retry-with-backoff against a TIME BUDGET rather than a fixed attempt count:
# if the connection comes back within OUTAGE_GRACE_SECONDS we resume from the
# `.part` and continue; if it stays down past the budget we give up so the
# caller can abort and clean everything up.
DOWNLOAD_CONNECT_TIMEOUT = 15      # seconds to establish a TCP/TLS connection
DOWNLOAD_READ_TIMEOUT = 90         # seconds of silence mid-stream before failing
OUTAGE_GRACE_SECONDS = 7 * 60      # tolerate an internet outage up to ~7 minutes
RETRY_BACKOFF_START = 5            # first retry waits this many seconds
RETRY_BACKOFF_MAX = 30            # backoff is capped here

# CDSE OData base used to walk the per-product Nodes tree (the in-archive file
# listing) so we can download only the band rasters we need instead of the whole
# ~868 MB .SAFE zip. NOTE: CDSE does NOT honour HTTP Range anywhere (the zip, the
# node `$value`, and the final storage URL all reply 200 + the full body, never
# 206) — verified empirically. So true byte-resume is impossible against this
# provider. Per-band download is still a large win: an interruption restarts only
# the one band in flight (a 10 m JP2 is ~120 MB, the 20 m SWIR ~30 MB) rather
# than the entire 868 MB archive, and any band already fully on disk is reused
# across retries AND process restarts (resume at band granularity).
ODATA_BASE = "https://catalogue.dataspace.copernicus.eu/odata/v1"


def _stream_to_file_with_retry(
    session: requests.Session,
    url: str,
    headers: dict,
    dest_path: str,
    label: str,
    timeout: tuple = (DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT),
    grace_seconds: int = OUTAGE_GRACE_SECONDS,
) -> Optional[str]:
    """Stream one CDSE object to `dest_path`, outage-tolerant, within a time budget.

    CDSE does NOT honour HTTP Range on any of its download endpoints (the product
    `$value`, the per-node `$value`, and the final storage URL all reply 200 with
    the full body, never 206 — verified empirically). So we cannot resume a
    partial transfer byte-for-byte; on a connection drop we restart this object's
    download from scratch. To keep that affordable, the caller downloads small
    objects (individual band rasters, ~30-120 MB) rather than the whole 868 MB
    archive, so a restart only re-fetches the one file in flight.

    We retry with exponential backoff against a TIME BUDGET, not a fixed attempt
    count: if the connection recovers within `grace_seconds` (~7 min) we keep
    going; if it stays down past the budget we give up cleanly (delete the
    `.part`, return None) so the caller can abort and clear everything.

    A fully-downloaded `dest_path` already on disk is reused as-is (resume at
    file granularity across retries AND process restarts). Returns `dest_path`,
    or None on give-up / failure.
    """
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        logger.info("Reusing cached %s (%d bytes)", label, os.path.getsize(dest_path))
        return dest_path

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    part_path = f"{dest_path}.part"

    deadline = time.monotonic() + grace_seconds
    backoff = RETRY_BACKOFF_START
    attempt = 0

    while True:
        attempt += 1
        total_size: Optional[int] = None
        try:
            # No Range: CDSE ignores it. Always (re)fetch the whole object.
            with session.get(
                url, headers=headers, stream=True, timeout=timeout
            ) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length is not None:
                    total_size = int(length)

                with open(part_path, "wb") as out:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            out.write(chunk)
                            _add_bytes_downloaded(len(chunk))  # BUG 4d

            final_size = os.path.getsize(part_path)
            if total_size is not None and final_size < total_size:
                raise requests.exceptions.ChunkedEncodingError(
                    f"incomplete: {final_size}/{total_size} bytes"
                )

            os.replace(part_path, dest_path)
            logger.info(
                "Downloaded %s (%d bytes; cumulative %.1f MB this run)",
                label, final_size, _bytes_downloaded_total() / 1e6,
            )
            return dest_path

        except (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            # A failed attempt leaves a junk `.part` (CDSE can't resume it);
            # drop it so the next attempt starts clean.
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except OSError:
                pass

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(
                    "Giving up on %s after %d attempts / outage exceeded %ds: %s",
                    label,
                    attempt,
                    grace_seconds,
                    exc,
                )
                return None
            wait = min(backoff, max(1, int(remaining)))
            logger.warning(
                "Download of %s interrupted (%s); restarting in %ds "
                "(attempt %d, %ds of outage budget left)",
                label,
                exc,
                wait,
                attempt,
                int(remaining),
            )
            time.sleep(wait)
            backoff = min(backoff * 2, RETRY_BACKOFF_MAX)
        except requests.RequestException as exc:
            logger.error("Failed to download %s: %s", label, exc)
            return None
        except OSError as exc:
            logger.error("Failed to write %s to %s: %s", label, dest_path, exc)
            return None


def _download_product_zip(
    scene_metadata: dict,
    token: str,
    timeout: tuple = (DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT),
    grace_seconds: int = OUTAGE_GRACE_SECONDS,
) -> Optional[str]:
    """Download a scene's full product archive (.zip) from CDSE, outage-tolerant.

    Fallback path used when per-band Nodes download is unavailable (e.g. an
    unexpected archive layout). Downloads the whole ~868 MB .SAFE archive via the
    `$value` endpoint with the shared retry/grace-budget logic. Returns the
    `.zip` path, or None.
    """
    product_id = scene_metadata.get("Id")
    if not product_id:
        logger.error("Scene metadata has no 'Id'; cannot download")
        return None

    name = scene_metadata.get("Name", product_id)
    os.makedirs(TEMP_ROOT, exist_ok=True)
    dest_path = os.path.join(TEMP_ROOT, f"{product_id}.zip")
    url = DOWNLOAD_URL.format(product_id=product_id)
    auth_header = {"Authorization": f"Bearer {token}"}

    logger.info("Downloading scene %s (full archive) from CDSE", name)
    with _CDSESession() as session:
        return _stream_to_file_with_retry(
            session,
            url,
            auth_header,
            dest_path,
            label=f"scene {name}",
            timeout=timeout,
            grace_seconds=grace_seconds,
        )


def _node_url(product_id: str, segments: list) -> str:
    """Build a CDSE OData Nodes(...) traversal URL for an in-product path.

    `segments` is the ordered list of node names from the product root down to
    the target (e.g. ["<SAFE>", "GRANULE", "<granule>", "IMG_DATA", "<jp2>"]).
    Node names are wrapped in Nodes(<name>); the name itself is single-quote
    free in CDSE products, so no extra escaping is needed beyond URL-encoding.
    """
    from urllib.parse import quote

    path = "".join(f"/Nodes({quote(s, safe='')})" for s in segments)
    return f"{ODATA_BASE}/Products({product_id}){path}/$value"


def _list_nodes(session: requests.Session, product_id: str, segments: list,
                headers: dict, timeout: tuple) -> list:
    """Return child node names under the given product path (one OData hop)."""
    from urllib.parse import quote

    path = "".join(f"/Nodes({quote(s, safe='')})" for s in segments)
    url = f"{ODATA_BASE}/Products({product_id}){path}/Nodes"
    resp = session.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    items = body.get("result", body.get("value", []))
    return [n.get("Name", "") for n in items if n.get("Name")]


def _resolve_s2_band_nodes(
    session: requests.Session,
    product_id: str,
    band_tokens: list,
    headers: dict,
    timeout: tuple,
) -> dict:
    """Map each Sentinel-2 band token to its JP2 node path segments (L1C or L2A).

    Walks SAFE -> GRANULE -> <granule> -> IMG_DATA and matches each requested
    token (B03, B08, B11, TCI, SCL, ...) to its JP2 node.

    - **L1C**: IMG_DATA is flat, filenames end `_<token>.jp2`.
    - **L2A**: IMG_DATA holds resolution subdirs `R10m/`, `R20m/`, `R60m/` and
      filenames carry the resolution suffix (`..._B03_10m.jp2`, `..._SCL_20m.jp2`).
      We prefer the finest resolution a token is published at (10 m for the
      spectral bands and TCI, 20 m for SCL — SCL has no 10 m variant).

    Returns {token: [seg, seg, ...]} for the bands located. Raises on a
    traversal/HTTP error so the caller can fall back to the whole-zip download.
    """
    safe_children = _list_nodes(session, product_id, [], headers, timeout)
    safe_dir = next((n for n in safe_children if n.endswith(".SAFE")), None)
    if not safe_dir:
        raise ValueError("no .SAFE root node in product")

    granules = _list_nodes(
        session, product_id, [safe_dir, "GRANULE"], headers, timeout
    )
    if not granules:
        raise ValueError("no GRANULE node in product")
    granule = granules[0]

    img_base = [safe_dir, "GRANULE", granule, "IMG_DATA"]
    img_children = _list_nodes(session, product_id, img_base, headers, timeout)

    # Detect L2A: IMG_DATA contains R10m/R20m/R60m resolution subdirectories.
    res_subdirs = [c for c in img_children if re.fullmatch(r"R\d+m", c)]
    is_l2a = bool(res_subdirs)

    if not is_l2a:
        # L1C: flat IMG_DATA, filenames end `_<token>.jp2`.
        jp2s = img_children
        resolved: dict = {}
        for token in band_tokens:
            upper = token.upper()
            match = next(
                (f for f in jp2s
                 if f.lower().endswith(".jp2")
                 and f.upper().endswith(f"_{upper}.JP2")),
                None,
            )
            if not match:
                logger.warning("Band %s not found in L1C IMG_DATA listing", token)
                continue
            resolved[token] = img_base + [match]
        return resolved

    # L2A: descend into resolution subdirs. List each once and cache.
    listings: dict = {}
    for sub in ("R10m", "R20m", "R60m"):
        if sub in res_subdirs:
            listings[sub] = _list_nodes(
                session, product_id, img_base + [sub], headers, timeout
            )

    # Finest-resolution preference per token. SCL is only 20 m / 60 m.
    pref = {"SCL": ("R20m", "R60m")}
    default_pref = ("R10m", "R20m", "R60m")

    resolved = {}
    for token in band_tokens:
        upper = token.upper()
        order = pref.get(upper, default_pref)
        found = None
        for sub in order:
            files = listings.get(sub)
            if not files:
                continue
            match = next(
                (f for f in files
                 if f.lower().endswith(".jp2")
                 and f"_{upper}_" in f.upper()),
                None,
            )
            if match:
                found = img_base + [sub, match]
                break
        if not found:
            logger.warning("Band %s not found in L2A IMG_DATA listings", token)
            continue
        resolved[token] = found
    return resolved


def _download_bands_via_nodes(
    scene_metadata: dict,
    token: str,
    event_id: str,
    band_tokens: list,
    satellite_type: str,
    grace_seconds: int = OUTAGE_GRACE_SECONDS,
) -> Optional[dict]:
    """Download only the needed band rasters via the CDSE Nodes tree.

    Primary download path for Sentinel-2: instead of the whole ~868 MB .SAFE
    zip, fetch each requested band JP2 individually (~30-120 MB each) straight
    into `<temp>/<event_id>/bands/`. CDSE doesn't honour Range, but per-band
    download means a connection drop only restarts the one band in flight, not
    the whole archive — and any band already fully on disk is reused. The ~7-min
    outage budget is SHARED across all bands of the scene (a sustained outage
    aborts the scene, not each band independently). Returns {token: path} for the
    bands fetched, or None on traversal failure (caller falls back to zip).
    """
    product_id = scene_metadata.get("Id")
    if not product_id:
        return None
    if satellite_type != "sentinel-2":
        # Per-band Nodes mapping here only knows the S2 L1C IMG_DATA layout.
        return None

    bands_dir = os.path.join(TEMP_ROOT, str(event_id), "bands")
    os.makedirs(bands_dir, exist_ok=True)
    auth_header = {"Authorization": f"Bearer {token}"}
    timeout = (DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)

    # Fast path: if every requested band is already fully on disk, return them
    # without ANY network I/O. This skips the Nodes tree-walk entirely (4 HTTP
    # listings that, on a flaky link, can each hang up to the read timeout
    # before raising), so a fully-cached scene resumes instantly across a
    # restart instead of stalling on the catalogue.
    cached = {}
    for tok in band_tokens:
        cand = os.path.join(bands_dir, f"{tok}.jp2")
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            cached[tok] = cand
    if len(cached) == len(band_tokens):
        for tok, path in cached.items():
            logger.info("Reusing cached band %s (%d bytes)", tok, os.path.getsize(path))
        return cached

    try:
        with _CDSESession() as session:
            node_map = _resolve_s2_band_nodes(
                session, product_id, band_tokens, auth_header, timeout
            )
            if not node_map:
                logger.warning(
                    "No band nodes resolved for %s; will fall back to zip",
                    scene_metadata.get("Name", product_id),
                )
                return None

            # The outage budget governs how long we tolerate a *stall* (no
            # progress), NOT total download time — on a slow-but-alive link a
            # ~300 MB scene legitimately takes several minutes, and that must
            # not be mistaken for an outage. Each band that finishes proves the
            # connection is alive, so it gets its OWN fresh `grace_seconds`
            # budget (enforced inside _stream_to_file_with_retry per file). A
            # sustained outage still aborts: the in-flight band's own budget
            # expires with no completion. This is the per-band analogue of the
            # whole-zip path's "progress resets the clock".
            band_paths: dict = {}
            for tok, segments in node_map.items():
                out_path = os.path.join(bands_dir, f"{tok}.jp2")
                url = _node_url(product_id, segments)
                result = _stream_to_file_with_retry(
                    session,
                    url,
                    auth_header,
                    out_path,
                    label=f"band {tok}",
                    timeout=timeout,
                    grace_seconds=grace_seconds,
                )
                if result is None:
                    logger.error("Band %s download failed; aborting scene", tok)
                    return None
                band_paths[tok] = result

            return band_paths
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning(
            "Per-band Nodes download failed (%s); falling back to whole-zip",
            exc,
        )
        return None


def _extract_bands(
    zip_path: str,
    event_id: str,
    band_tokens: list,
    satellite_type: str,
) -> dict:
    """Extract the requested band rasters from the product archive.

    Looks inside the .SAFE zip for members matching each band token and copies
    them to `<temp>/<event_id>/bands/`. For Sentinel-2, prefers the 10 m variant
    of a band when several resolutions exist. Returns {band_token: local_path}
    for the bands that were found (missing bands are logged and skipped).
    """
    bands_dir = os.path.join(TEMP_ROOT, str(event_id), "bands")
    os.makedirs(bands_dir, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = [m for m in archive.namelist() if not m.endswith("/")]

            band_paths: dict = {}
            for token in band_tokens:
                matches = _match_band_members(members, token, satellite_type)
                if not matches:
                    logger.warning("Band %s not found in %s", token, zip_path)
                    continue

                member = matches[0]
                ext = os.path.splitext(member)[1] or ".bin"
                out_path = os.path.join(bands_dir, f"{token}{ext}")
                if not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
                    with archive.open(member) as src, open(out_path, "wb") as dst:
                        dst.write(src.read())
                band_paths[token] = out_path
                logger.info("Extracted band %s -> %s", token, out_path)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.error("Could not extract bands from %s: %s", zip_path, exc)
        return {}

    return band_paths


def _match_band_members(
    members: list, token: str, satellite_type: str
) -> list:
    """Return archive members for a band token, best (highest-res) first."""
    upper = token.upper()
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

    # Sentinel-2: JP2 files like R10m/..._B03_10m.jp2 or .../TCI.jp2.
    cand = [
        m
        for m in members
        if m.lower().endswith(".jp2") and f"_{upper}_" in m.upper()
    ]
    if not cand:
        # TCI in some products is named ..._TCI_10m.jp2 or ..._TCI.jp2
        cand = [
            m for m in members
            if m.lower().endswith(".jp2") and upper in m.upper()
        ]

    # Prefer the 10 m variant when resolution suffixes are present.
    def res_rank(path: str) -> int:
        low = path.lower()
        if "10m" in low or "_10" in low:
            return 0
        if "20m" in low:
            return 1
        if "60m" in low:
            return 2
        return 3

    return sorted(cand, key=res_rank)


def _mosaic_bands(
    per_scene_paths: list, event_id: str, satellite_type: str = "sentinel-2",
    dst_crs=None,
) -> dict:
    """Mosaic per-band rasters from several scenes into single rasters.

    `per_scene_paths` is a list of {band_token: path} dicts (one per scene). For
    each band token present in any scene, the matching rasters are merged with
    `rasterio.merge` (which fills nodata gaps from later scenes) and written to
    `<temp>/<event_id>/bands/<token>.tif`. Returns {band_token: mosaic_path}.

    BUG 1 follow-up: S1 GRD source rasters carry GCPs, not an affine — merging
    them RAW (as this function did before) hands `rasterio.merge` two datasets
    that both report `crs=None`/identity, which it correctly refuses
    ("upside down rasters cannot be merged") because there is nothing
    meaningful to align. Each source is now resolved via `_open_georeferenced`
    (the same GCP->UTM warp `stack_bands` uses) into a COMMON `dst_crs` before
    merging, so every source shares one real projection/orientation and the
    merge can align them properly. S2 sources (already affine) pass through
    `_open_georeferenced` as a no-op.
    """
    bands_dir = os.path.join(TEMP_ROOT, str(event_id), "bands")
    os.makedirs(bands_dir, exist_ok=True)

    tokens: list = []
    for paths in per_scene_paths:
        for tok in paths:
            if tok not in tokens:
                tokens.append(tok)

    mosaicked: dict = {}
    for token in tokens:
        sources = [p[token] for p in per_scene_paths if token in p]
        if len(sources) == 1:
            mosaicked[token] = sources[0]
            continue

        datasets = []
        raw_handles = []
        try:
            for src in sources:
                ds, raw = _open_georeferenced(src, dst_crs)
                datasets.append(ds)
                if raw is not None:
                    raw_handles.append(raw)
            arr, transform = rio_merge(datasets)
            profile = datasets[0].profile.copy()
            profile.update(
                driver="GTiff",
                height=arr.shape[1],
                width=arr.shape[2],
                count=arr.shape[0],
                transform=transform,
                crs=datasets[0].crs,
            )
            out_path = os.path.join(bands_dir, f"{token}.tif")
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(arr)
            mosaicked[token] = out_path
            logger.info(
                "Mosaicked band %s from %d scenes -> %s",
                token,
                len(sources),
                out_path,
            )
        except (rasterio.errors.RasterioError, ValueError) as exc:
            logger.warning(
                "Mosaic of band %s failed (%s); using first scene only",
                token,
                exc,
            )
            mosaicked[token] = sources[0]
        finally:
            for ds in datasets:
                ds.close()
            for raw in raw_handles:
                raw.close()

    return mosaicked


def download_imagery(
    selection: dict,
    scene_metadata,
    event_id: str,
    token: str,
    disaster_type: str,
    dst_crs=None,
) -> Optional[dict]:
    """Download the product(s) and extract the bands needed for this disaster.

    Args:
        selection: dict from `sentinel.select_satellite` (carries
            "satellite_type").
        dst_crs: target CRS for mosaicking GCP-georeferenced (S1 GRD) sources
            (BUG 1 follow-up) — see `_mosaic_bands`. Ignored for S2.
        scene_metadata: a single scene dict from `sentinel.search_imagery`, or a
            list of scene dicts to mosaic into one coverage (FIX 2).
        event_id: namespaces extracted bands under <temp>/<event_id>/bands/.
        token: CDSE access token.
        disaster_type: drives which Sentinel-2 bands are pulled.

    Returns {"satellite_type": ..., "band_paths": {token: path, ...}} or None.
    When several scenes are supplied, the per-band rasters are mosaicked first.
    """
    if not scene_metadata:
        logger.error("No scene metadata provided to download_imagery")
        return None
    if not token:
        logger.error("No access token provided; cannot download imagery")
        return None

    scenes = scene_metadata if isinstance(scene_metadata, list) else [scene_metadata]

    satellite_type = selection.get("satellite_type", "sentinel-2")
    disaster = (disaster_type or "").strip().lower()

    if satellite_type == "sentinel-1":
        band_tokens = _S1_POLARIZATIONS
    else:
        band_tokens = _S2_BANDS.get(disaster, _S2_DEFAULT_BANDS)

    per_scene_paths = []
    rejected_non_grd = 0
    for idx, scene in enumerate(scenes):
        # GUARD (Sentinel-1 only): reject a non-GRD product BEFORE downloading.
        # RAW (level-0) / SLC products carry no VV/VH measurement GeoTIFFs, so
        # extraction is guaranteed to fail — but the archive is multi-GB, so a
        # failed download costs real time/bandwidth. sentinel.search_imagery now
        # filters the catalogue to GRD, but this is a cheap belt-and-suspenders
        # check in case a non-GRD scene reaches here via another path.
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

        # Each scene's bands go in their own subdir so same-named JP2s from
        # different tiles don't clobber each other before mosaicking. Key the
        # subdir on the scene's stable product Id (not a positional index):
        # band download/extract reuses an already-present file, so a bare
        # scene_<idx> would serve a *previous* run's tile when the same event_id
        # is re-processed with a different scene selection.
        if len(scenes) > 1:
            scene_key = scene.get("Id") or f"scene_{idx}"
            scene_event = f"{event_id}/scene_{scene_key}"
        else:
            scene_event = event_id

        # PRIMARY path: download only the bands we need via the Nodes tree
        # (~30-120 MB each) instead of the whole ~868 MB .SAFE zip. This shrinks
        # the per-interruption restart cost from the full archive to one band,
        # since CDSE does not honour HTTP Range for true byte-resume.
        paths = _download_bands_via_nodes(
            scene, token, scene_event, band_tokens, satellite_type
        )

        # FALLBACK: whole-archive download + in-zip extract (unusual layouts,
        # Sentinel-1, or a Nodes traversal failure).
        if not paths:
            zip_path = _download_product_zip(scene, token)
            if zip_path is None:
                logger.warning("Skipping scene %d: download failed", idx)
                continue
            paths = _extract_bands(
                zip_path, scene_event, band_tokens, satellite_type
            )

        if paths:
            per_scene_paths.append(paths)

    if not per_scene_paths:
        if rejected_non_grd and rejected_non_grd == len(scenes):
            logger.error(
                "No usable Sentinel-1 bands for %s: all %d candidate scene(s) "
                "were non-GRD (RAW/SLC) products, skipped without download",
                event_id,
                rejected_non_grd,
            )
        else:
            logger.error("No bands extracted for %s", event_id)
        return None

    if len(per_scene_paths) == 1:
        band_paths = per_scene_paths[0]
    else:
        band_paths = _mosaic_bands(
            per_scene_paths, event_id, satellite_type, dst_crs
        )

    return {"satellite_type": satellite_type, "band_paths": band_paths}


# --------------------------------------------------------------------------- #
# CRS / georeferencing resolution (BUG 1 — S1 GRD carries GCPs, not an affine)
# --------------------------------------------------------------------------- #
def _utm_epsg_for_lonlat(lon: float, lat: float) -> int:
    """Return the EPSG code of the UTM zone containing (lon, lat)."""
    zone = int(np.floor((lon + 180.0) / 6.0)) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


def _open_georeferenced(path: str, dst_crs=None):
    """Open a band, resolving GCP georeferencing (S1 GRD) into a real CRS/grid.

    S1 GRD measurement TIFFs carry **GCPs (ground control points), not an affine
    geotransform**: a plain ``rasterio.open()`` reports ``crs=None`` and an
    identity transform, which makes ``clip_to_polygon`` read WGS84 degree
    coordinates as pixel indices and collapse the clip to ~1x2 px (BUG 1 / the
    audit's BUG B). When GCPs are present we wrap the dataset in a ``WarpedVRT``
    that reprojects into a real map projection with a proper affine, so every
    downstream step (stack -> clip -> index -> vectorize -> bounds) gets valid
    georeferencing exactly like the S2 (already-affine) path.

    ``dst_crs`` picks the target projection: pass the AOI's UTM zone so pixels
    stay square-ish in metres (areas/thresholds are then metric); when None the
    GCPs' own CRS (usually EPSG:4326) is used. S2 rasters (already affine,
    ``get_gcps()`` empty) pass straight through unchanged.

    Returns ``(dataset, src_to_close)``: the dataset the caller should read from,
    and (for the warped path) the underlying source handle the caller must also
    close. For the pass-through path ``src_to_close`` is None.
    """
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import calculate_default_transform

    src = rasterio.open(path)
    try:
        gcps, gcp_crs = src.get_gcps()
    except (rasterio.errors.RasterioError, ValueError):
        gcps, gcp_crs = ([], None)

    identity = src.transform == rasterio.Affine.identity()
    if gcps and (src.crs is None or identity):
        src_crs = gcp_crs or rasterio.crs.CRS.from_epsg(4326)
        if dst_crs is None:
            dst_crs = src_crs
        try:
            transform, width, height = calculate_default_transform(
                src_crs, dst_crs, src.width, src.height, gcps=gcps,
            )
            vrt = WarpedVRT(
                src,
                src_crs=src_crs,
                crs=dst_crs,
                transform=transform,
                width=width,
                height=height,
                resampling=Resampling.bilinear,
            )
            logger.info(
                "Resolved GCP georeferencing for %s -> %s (%dx%d)",
                os.path.basename(path),
                dst_crs,
                width,
                height,
            )
            return vrt, src
        except (rasterio.errors.RasterioError, ValueError) as exc:
            logger.error(
                "Failed to warp GCP-georeferenced raster %s: %s", path, exc
            )
            src.close()
            raise
    return src, None


def _dst_crs_from_polygon(merged_polygon) -> Optional[object]:
    """Pick a metric target CRS (the AOI-centroid UTM zone) for GCP warping."""
    if not merged_polygon:
        return None
    try:
        centroid = shape(merged_polygon).centroid
        return rasterio.crs.CRS.from_epsg(
            _utm_epsg_for_lonlat(centroid.x, centroid.y)
        )
    except (ValueError, AttributeError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Step 7C: stack bands into one aligned cube
# --------------------------------------------------------------------------- #
def stack_bands(
    band_paths: dict, satellite_type: str, dst_crs=None
) -> Optional[dict]:
    """Stack per-band rasters into one aligned numpy cube.

    Uses the first 10 m band as the reference grid; coarser bands (e.g. the
    Sentinel-2 20 m SWIR B11) are resampled to that grid on read. TCI, which is
    a 3-band RGB JP2, is kept separately for the true-colour export.

    Returns:
        {
            "bands": {token: 2-D float32 array, ...},  # single-band data
            "tci": (3, H, W) uint8 array or None,       # RGB preview
            "transform": affine,                        # reference grid
            "crs": CRS,
            "shape": (H, W),
        }
    """
    if not band_paths:
        logger.error("No band paths to stack")
        return None

    # Pick the reference grid: the highest-resolution single band available.
    single_tokens = [t for t in band_paths if t.upper() != "TCI"]
    if not single_tokens:
        single_tokens = list(band_paths)

    try:
        ref_token = min(
            single_tokens,
            key=lambda t: _S2_BAND_RES.get(t.upper(), 10)
            if satellite_type == "sentinel-2"
            else 10,
        )
        # S1 GRD bands carry GCPs (crs=None, identity transform); resolve them
        # into a real projected grid via _open_georeferenced so the reference
        # grid — and every downstream clip — has valid georeferencing (BUG 1).
        # S2 (already affine) passes straight through. dst_crs, when supplied,
        # warps S1 into the AOI's UTM zone so pixels are metric.
        ref_ds, ref_src = _open_georeferenced(band_paths[ref_token], dst_crs)
        try:
            ref_h, ref_w = ref_ds.height, ref_ds.width
            ref_transform = ref_ds.transform
            ref_crs = ref_ds.crs
        finally:
            ref_ds.close()
            if ref_src is not None:
                ref_src.close()

        bands: dict = {}
        for token, path in band_paths.items():
            if token.upper() == "TCI":
                continue
            src_ds, src_raw = _open_georeferenced(path, dst_crs)
            try:
                arr = src_ds.read(
                    1,
                    out_shape=(ref_h, ref_w),
                    resampling=Resampling.bilinear,
                ).astype("float32")
                bands[token] = arr
            finally:
                src_ds.close()
                if src_raw is not None:
                    src_raw.close()

        tci = None
        if "TCI" in band_paths:
            tci_ds, tci_raw = _open_georeferenced(band_paths["TCI"], dst_crs)
            try:
                count = min(tci_ds.count, 3)
                tci = tci_ds.read(
                    indexes=list(range(1, count + 1)),
                    out_shape=(count, ref_h, ref_w),
                    resampling=Resampling.bilinear,
                ).astype("uint8")
            finally:
                tci_ds.close()
                if tci_raw is not None:
                    tci_raw.close()
    except rasterio.errors.RasterioError as exc:
        logger.error("Failed to stack bands: %s", exc)
        return None

    logger.info(
        "Stacked %d band(s) onto %dx%d grid (ref=%s)",
        len(bands),
        ref_h,
        ref_w,
        ref_token,
    )
    return {
        "bands": bands,
        "tci": tci,
        "transform": ref_transform,
        "crs": ref_crs,
        "shape": (ref_h, ref_w),
    }


# --------------------------------------------------------------------------- #
# Step 7D: clip to the actual risk polygon (not a rectangle)
# --------------------------------------------------------------------------- #
def clip_to_polygon(
    stacked: dict, merged_polygon: dict
) -> Optional[dict]:
    """Mask the stacked cube to the real risk geometry.

    `merged_polygon` is the GeoJSON geometry from
    `boundary.merge_risk_boundaries` (WGS84). It is reprojected into the
    raster CRS and applied with `rasterio.mask` so pixels outside the polygon
    become nodata. Returns a copy of `stacked` with masked arrays plus a
    boolean `mask` (True = inside polygon) and an updated transform/shape.
    """
    if not stacked:
        logger.error("No stacked data to clip")
        return None
    if not merged_polygon:
        logger.warning("No polygon provided; returning unclipped stack")
        stacked = dict(stacked)
        h, w = stacked["shape"]
        stacked["mask"] = np.ones((h, w), dtype=bool)
        return stacked

    crs = stacked["crs"]
    transform = stacked["transform"]
    h, w = stacked["shape"]

    # Degenerate-georeferencing guard (BUG 1). An unresolved GCP raster reports
    # crs=None and an identity transform; the window math below would then read
    # WGS84 degree coordinates (~73, ~34) as pixel indices and collapse the clip
    # to a 1x2 px sliver. _open_georeferenced should have resolved this upstream;
    # if it did not, refuse to clip rather than silently ship a near-empty result.
    if crs is None or transform == rasterio.Affine.identity():
        logger.error(
            "Raster has no usable georeferencing (crs=%s, identity transform=%s); "
            "GCP resolution failed — refusing to clip",
            crs,
            transform == rasterio.Affine.identity(),
        )
        return None

    # Reproject the WGS84 polygon to the raster CRS.
    try:
        if crs is not None and crs.to_epsg() != 4326:
            geom = transform_geom("EPSG:4326", crs, merged_polygon)
        else:
            geom = merged_polygon
    except (rasterio.errors.RasterioError, ValueError) as exc:
        logger.error("Failed to reproject clip polygon: %s", exc)
        return None

    # Pre-window to the polygon's bounding box BEFORE the expensive mask.
    # rasterio.mask rasterizes against a full-grid in-memory dataset; on a big
    # mosaic (e.g. Mindanao's 30978x20976 ≈ 650M px) that is hundreds of MB and
    # seconds of work *per call*. The per-city path re-clips the same mosaic to
    # each city polygon, where a single city covers ~1-2% of the grid — masking
    # the whole grid each time is pathologically slow. So we first compute the
    # geometry's pixel window, slice the cube down to it (a cheap view), and run
    # the mask/rasterize only on that window. The result is identical; we just
    # avoid touching the 98% of pixels the polygon can't possibly include.
    try:
        gminx, gminy, gmaxx, gmaxy = shape(geom).bounds
    except (ValueError, AttributeError, TypeError) as exc:
        logger.error("Could not compute clip geometry bounds: %s", exc)
        return None

    # Pixel offsets of the geometry bbox in the cube (transform.e is negative).
    px0 = int(np.floor((gminx - transform.c) / transform.a))
    px1 = int(np.ceil((gmaxx - transform.c) / transform.a))
    py0 = int(np.floor((gmaxy - transform.f) / transform.e))
    py1 = int(np.ceil((gminy - transform.f) / transform.e))
    win_c0 = max(0, min(px0, px1))
    win_c1 = min(w, max(px0, px1))
    win_r0 = max(0, min(py0, py1))
    win_r1 = min(h, max(py0, py1))

    if win_c1 <= win_c0 or win_r1 <= win_r0:
        logger.warning("Clip geometry does not overlap the raster grid")
        return None

    win_h = win_r1 - win_r0
    win_w = win_c1 - win_c0
    # Transform of the windowed sub-grid (origin shifted to the window corner).
    win_transform = transform * rasterio.Affine.translation(win_c0, win_r0)

    # Build the inside-polygon boolean mask using rasterio.features against an
    # in-memory single-band dataset describing only the WINDOWED grid.
    from rasterio.io import MemoryFile

    try:
        profile = {
            "driver": "GTiff",
            "height": win_h,
            "width": win_w,
            "count": 1,
            "dtype": "uint8",
            "crs": crs,
            "transform": win_transform,
        }
        with MemoryFile() as mem:
            with mem.open(**profile) as tmp:
                tmp.write(np.ones((1, win_h, win_w), dtype="uint8"))
            with mem.open() as tmp:
                clipped, clip_transform = rio_mask(
                    tmp, [geom], crop=True, nodata=0, filled=True
                )
        inside = clipped[0] > 0
    except (rasterio.errors.RasterioError, ValueError) as exc:
        logger.error("Failed to build clip mask: %s", exc)
        return None

    # Apply the same crop window to every band. Offsets are relative to the
    # original cube transform; the mask crop is relative to the window, so add
    # the window origin back in.
    new_h, new_w = inside.shape
    col_off = win_c0 + round((clip_transform.c - win_transform.c) / transform.a)
    row_off = win_r0 + round((clip_transform.f - win_transform.f) / transform.e)

    def crop(arr: np.ndarray) -> np.ndarray:
        sub = arr[row_off:row_off + new_h, col_off:col_off + new_w]
        # Guard against off-by-one from rounding.
        return sub[:new_h, :new_w]

    out_bands = {}
    for token, arr in stacked["bands"].items():
        sub = crop(arr).astype("float32").copy()
        sub[~inside] = np.nan
        out_bands[token] = sub

    out_tci = None
    if stacked.get("tci") is not None:
        tci = stacked["tci"]
        cropped = np.stack([crop(tci[i]) for i in range(tci.shape[0])])
        cropped[:, ~inside] = 0
        out_tci = cropped

    logger.info("Clipped to polygon: %dx%d (was %dx%d)", new_h, new_w, h, w)
    return {
        "bands": out_bands,
        "tci": out_tci,
        "transform": clip_transform,
        "crs": crs,
        "shape": (new_h, new_w),
        "mask": inside,
    }


# --------------------------------------------------------------------------- #
# Step 7E: spectral / backscatter indices + classification
# --------------------------------------------------------------------------- #
def _safe_ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """Element-wise num/den, with 0 where the denominator is ~0."""
    out = np.full_like(num, np.nan, dtype="float32")
    np.divide(num, den, out=out, where=np.abs(den) > 1e-9)
    return out


def _classify(index: np.ndarray, valid: np.ndarray, scheme: dict) -> np.ndarray:
    """Map a continuous index to graded hazard classes per `scheme`.

    Returns a uint8 array: 0 = safe land, 1..N = increasing severity,
    NODATA_CLASS where invalid/outside. Bands are applied from least to most
    severe so the highest matching class wins.
    """
    out = np.full(index.shape, NODATA_CLASS, dtype="uint8")
    out[valid] = 0  # default: safe land
    ascending = scheme["order"] == "asc"
    for bound, value, _label, _rgb, _alpha in scheme["bands"]:
        if ascending:
            hit = valid & (index >= bound)
        else:
            hit = valid & (index <= bound)
        out[hit] = value
    return out


def calculate_indices(
    clipped: dict, satellite_type: str, disaster_type: str
) -> Optional[dict]:
    """Compute the disaster-appropriate index and a classification mask.

    Sentinel-2:
        flood              -> NDWI = (B03-B08)/(B03+B08); water where > 0.3
        earthquake/landslide -> NDVI = (B08-B04)/(B08+B04); damage where < 0.2
    Sentinel-1:
        flood/any          -> VV backscatter in dB; smooth water where < -15 dB

    Returns:
        {
            "index_type": "NDWI" | "NDVI" | "SAR",
            "array": 2-D float32 index,
            "classification_array": uint8 (1 = affected, 0 = unaffected,
                255 = outside polygon / nodata),
            "water_percent": float,         # % of valid pixels classed affected
            "mean_value": float,            # mean index over valid pixels
            "threshold_used": float,
        }
    """
    if not clipped:
        logger.error("No clipped data for index calculation")
        return None

    bands = clipped["bands"]
    mask = clipped.get("mask")
    disaster = (disaster_type or "").strip().lower()

    if satellite_type == "sentinel-1":
        vv = bands.get("VV")
        if vv is None:
            logger.error("Sentinel-1 VV band missing; cannot compute SAR index")
            return None
        # GRD products are linear power; convert to dB. Guard non-positive.
        index = np.full_like(vv, np.nan, dtype="float32")
        finite = np.isfinite(vv) & (vv > 0)
        index[finite] = 10.0 * np.log10(vv[finite])
        index_type = "SAR"
        scheme_key = "SAR"
        threshold = SAR_WATER_THRESHOLD_DB
        # BUG 5 — the SAR index is 10*log10(raw GRD DN): NO radiometric
        # calibration LUT, NO speckle filter, NO terrain correction. It is a
        # relative DN-space number, NOT calibrated sigma0 dB, so it must not be
        # threshold-compared as if it were.
        index_calibrated = False
        index_units = "dB_uncalibrated"
    elif disaster == "flood":
        b03, b08 = bands.get("B03"), bands.get("B08")
        if b03 is None or b08 is None:
            logger.error("NDWI needs B03 and B08; one is missing")
            return None
        index = _safe_ratio(b03 - b08, b03 + b08)
        index_type = "NDWI"
        scheme_key = "NDWI"
        threshold = NDWI_WATER_THRESHOLD
        index_calibrated = True
        index_units = "NDWI_ratio"
    else:
        b08, b04 = bands.get("B08"), bands.get("B04")
        if b08 is None or b04 is None:
            logger.error("NDVI needs B08 and B04; one is missing")
            return None
        index = _safe_ratio(b08 - b04, b08 + b04)
        index_type = "NDVI"
        scheme_key = "NDVI_LANDSLIDE" if disaster == "landslide" else "NDVI_QUAKE"
        threshold = NDVI_DAMAGE_THRESHOLD
        index_calibrated = True
        index_units = "NDVI_ratio"

    # Graded classification: 0 safe, 1..N severity, 255 nodata/outside polygon.
    valid = np.isfinite(index)
    if mask is not None:
        valid = valid & mask
    scheme = _CLASS_SCHEMES[scheme_key]
    classification = _classify(index, valid, scheme)

    valid_count = int(valid.sum())
    affected_mask = (classification >= 1) & (classification != NODATA_CLASS)
    affected_count = int(affected_mask.sum())
    water_percent = (
        round(100.0 * affected_count / valid_count, 2) if valid_count else 0.0
    )
    mean_value = (
        round(float(np.nanmean(index[valid])), 4) if valid_count else 0.0
    )

    # Per-class pixel counts (skip class 0 / nodata) for reporting.
    class_counts = {}
    for _bound, value, label, _rgb, _alpha in scheme["bands"]:
        n = int((classification == value).sum())
        if n:
            class_counts[label] = round(100.0 * n / valid_count, 2) if valid_count else 0.0

    logger.info(
        "%s: %.2f%% affected, mean=%.4f, classes=%s",
        index_type,
        water_percent,
        mean_value,
        class_counts,
    )
    return {
        "index_type": index_type,
        "scheme_key": scheme_key,
        "array": index,
        "classification_array": classification,
        "water_percent": water_percent,
        "mean_value": mean_value,
        "threshold_used": threshold,
        "class_counts": class_counts,
        # BUG 5 calibration contract (see the branch above).
        "index_calibrated": index_calibrated,
        "index_units": index_units,
    }


# --------------------------------------------------------------------------- #
# Step 7F: PNG exports (true colour, index map, classification overlay)
# --------------------------------------------------------------------------- #
def _decimate(arr: np.ndarray) -> np.ndarray:
    """Downsample a 2-D array so its longest side is <= _MAX_PNG_DIMENSION."""
    h, w = arr.shape[-2:]
    scale = max(h, w) / _MAX_PNG_DIMENSION
    if scale <= 1:
        return arr
    step = int(np.ceil(scale))
    return arr[..., ::step, ::step]


def _stretch_uint8(band: np.ndarray) -> np.ndarray:
    """2-98 percentile stretch of a band to 0-255 uint8 (NaN -> 0)."""
    valid = band[np.isfinite(band)]
    if valid.size == 0:
        return np.zeros(band.shape, dtype=np.uint8)
    lo, hi = np.percentile(valid, (2, 98))
    if hi <= lo:
        hi = lo + 1.0
    out = np.clip((np.nan_to_num(band, nan=lo) - lo) / (hi - lo), 0, 1)
    return (out * 255).astype(np.uint8)


def export_png(
    indices: dict, clipped: dict, event_id: str, disaster_type: str
) -> Optional[dict]:
    """Export the three display PNGs for an event.

    Writes to `<temp>/<event_id>/`:
        true_color.png      - natural colour (S2 TCI/RGB; S1 VV greyscale)
        index_map.png       - NDWI blues / NDVI RdYlGn / SAR greyscale
        classification.png  - semi-transparent affected-zone overlay (RGBA)

    All three are RGBA with the outside-polygon area fully transparent (alpha
    0), so any layer can be dropped over the map without a black/white box
    around the risk-area silhouette.

    Returns {"true_color": path, "index_map": path, "classification": path}.
    """
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import colormaps
    from PIL import Image

    out_dir = os.path.join(TEMP_ROOT, str(event_id))
    os.makedirs(out_dir, exist_ok=True)
    paths = {}

    index_type = indices["index_type"]
    disaster = (disaster_type or "").strip().lower()

    # Outside-polygon alpha: transparent where the clip mask is False so the
    # true-colour layer carries the risk-area silhouette, not a black box.
    mask = clipped.get("mask")

    try:
        # --- true_color.png -------------------------------------------------
        tci = clipped.get("tci")
        if tci is not None and tci.shape[0] >= 3:
            rgb = np.dstack([_decimate(tci[i]) for i in range(3)]).astype(
                "uint8"
            )
            # TCI nodata is also encoded as 0,0,0 inside clip_to_polygon; treat
            # all-black pixels as outside so seams/fill are transparent too.
            inside = rgb.any(axis=2)
        else:
            # Sentinel-1 (or no TCI): greyscale from the index source band.
            base = None
            for tok in ("B04", "VV", "B08", "B03"):
                if tok in clipped["bands"]:
                    base = clipped["bands"][tok]
                    break
            if base is None:
                base = next(iter(clipped["bands"].values()))
            base_dec = _decimate(base)
            g = _stretch_uint8(base_dec)
            rgb = np.dstack([g, g, g])
            inside = np.isfinite(base_dec)

        if mask is not None:
            inside = inside & _decimate(mask)
        tc_alpha = np.where(inside, 255, 0).astype("uint8")
        tc_rgba = np.dstack([rgb, tc_alpha])
        tc_path = os.path.join(out_dir, "true_color.png")
        Image.fromarray(tc_rgba, mode="RGBA").save(
            tc_path, format="PNG", optimize=True
        )
        paths["true_color"] = tc_path

        # --- index_map.png --------------------------------------------------
        index = _decimate(indices["array"])
        finite = index[np.isfinite(index)]
        if finite.size:
            lo, hi = np.percentile(finite, (2, 98))
            if hi <= lo:
                hi = lo + 1.0
        else:
            lo, hi = 0.0, 1.0
        norm = np.clip((np.nan_to_num(index, nan=lo) - lo) / (hi - lo), 0, 1)

        if index_type == "NDWI":
            cmap = colormaps["Blues"]
        elif index_type == "NDVI":
            cmap = colormaps["RdYlGn"]
        else:
            cmap = colormaps["gray"]
        index_rgb = (cmap(norm)[..., :3] * 255).astype("uint8")
        # Transparent where there was no data, and outside the risk polygon, so
        # the index layer shares the same silhouette as the other layers.
        idx_inside = np.isfinite(index)
        if mask is not None:
            idx_inside = idx_inside & _decimate(mask)
        alpha = np.where(idx_inside, 255, 0).astype("uint8")
        index_rgba = np.dstack([index_rgb, alpha])
        idx_path = os.path.join(out_dir, "index_map.png")
        Image.fromarray(index_rgba, mode="RGBA").save(
            idx_path, format="PNG", optimize=True
        )
        paths["index_map"] = idx_path

        # --- classification.png (graded hazard overlay) --------------------
        # Only hazard classes (1..N) are painted; safe land (0) and nodata
        # (255) stay fully transparent so this drops cleanly over the map /
        # true_color image. Deeper colour = higher severity.
        cls = _decimate(indices["classification_array"])
        scheme = _CLASS_SCHEMES[indices["scheme_key"]]

        h, w = cls.shape
        rgba = np.zeros((h, w, 4), dtype="uint8")
        for _bound, value, _label, rgb, alpha in scheme["bands"]:
            sel = cls == value
            rgba[sel] = (*rgb, alpha)
        # class 0 (safe) and 255 (nodata) remain (0,0,0,0) -> transparent.
        cls_path = os.path.join(out_dir, "classification.png")
        Image.fromarray(rgba, mode="RGBA").save(
            cls_path, format="PNG", optimize=True
        )
        paths["classification"] = cls_path
    except (OSError, ValueError) as exc:
        logger.error("Failed to export PNGs for %s: %s", event_id, exc)
        return None

    logger.info("Exported PNGs for %s: %s", event_id, list(paths))
    return paths


# --------------------------------------------------------------------------- #
# Step 7G: vectorize the classification into GeoJSON zones
# --------------------------------------------------------------------------- #
def _polygon_area_km2(geom, crs) -> float:
    """Approximate a WGS84/geographic polygon's area in km^2.

    Reprojects to a world equal-area projection (EPSG:6933) for the measure.
    """
    try:
        from pyproj import Transformer

        transformer = Transformer.from_crs(
            crs if crs else "EPSG:4326", "EPSG:6933", always_xy=True
        )
        projected = shapely_transform(
            lambda x, y, z=None: transformer.transform(x, y), geom
        )
        return projected.area / 1e6
    except Exception:  # noqa: BLE001 - area is best-effort
        return geom.area  # degrees^2 fallback; only used for relative size


# Hazard class value -> severity label (class 1 lowest, 3 highest).
_SEVERITY_BY_CLASS = {1: "low", 2: "medium", 3: "high"}


def vectorize_classification(
    classification_array: np.ndarray,
    transform,
    crs,
    disaster_type: str,
    scheme_key: Optional[str] = None,
) -> dict:
    """Turn the graded hazard classes into a GeoJSON FeatureCollection.

    Polygonizes each hazard class (1..N) separately, reprojects to WGS84,
    simplifies (tolerance 0.001 deg), and drops polygons smaller than
    MIN_ZONE_AREA_KM2. Each feature carries risk_type, hazard_class (the class
    label, e.g. "water"/"damage"), area_km2 and a severity derived from the
    class level. Returns a FeatureCollection with an added `total_area` (km^2).
    """
    disaster = (disaster_type or "").strip().lower() or "unknown"
    scheme = _CLASS_SCHEMES.get(scheme_key) if scheme_key else None
    labels = (
        {value: label for _b, value, label, _rgb, _a in scheme["bands"]}
        if scheme
        else {}
    )
    arr = classification_array

    features = []
    total_area = 0.0
    try:
        # Vectorize each hazard class (skip 0 safe and 255 nodata).
        hazard_values = sorted(
            v for v in np.unique(arr)
            if v != 0 and v != NODATA_CLASS
        )
        for value in hazard_values:
            sel = (arr == value).astype("uint8")
            label = labels.get(int(value), f"class_{int(value)}")
            severity = _SEVERITY_BY_CLASS.get(int(value), "low")
            for geom, gval in shapes(sel, mask=sel.astype(bool),
                                     transform=transform):
                if gval != 1:
                    continue
                poly = shape(geom)
                if crs is not None and crs.to_epsg() != 4326:
                    poly = shape(transform_geom(crs, "EPSG:4326", mapping(poly)))
                poly = poly.simplify(0.001, preserve_topology=True)
                if poly.is_empty:
                    continue

                area_km2 = round(_polygon_area_km2(poly, "EPSG:4326"), 3)
                if area_km2 < MIN_ZONE_AREA_KM2:
                    continue

                total_area += area_km2
                features.append(
                    {
                        "type": "Feature",
                        "geometry": mapping(poly),
                        "properties": {
                            "risk_type": disaster,
                            "hazard_class": label,
                            "class_level": int(value),
                            "area_km2": area_km2,
                            "severity": severity,
                        },
                    }
                )
    except (ValueError, rasterio.errors.RasterioError) as exc:
        logger.error("Vectorization failed: %s", exc)

    logger.info(
        "Vectorized %d zone(s), total %.2f km^2", len(features), total_area
    )
    return {
        "type": "FeatureCollection",
        "features": features,
        "total_area": round(total_area, 3),
    }


# --------------------------------------------------------------------------- #
# Step 7I: master pipeline
# --------------------------------------------------------------------------- #
def _reference_band(clipped: dict):
    """Pick a spectral band to measure nodata from (never SCL/TCI).

    SCL is a class layer (values are class ids, not radiance) and TCI is a
    display product, so neither is a valid nodata reference. Prefer a real
    spectral/backscatter band; fall back to any non-SCL band.
    """
    bands = clipped.get("bands") or {}
    for tok, arr in bands.items():
        if tok.upper() not in ("SCL", "TCI"):
            return arr
    # Only SCL present (shouldn't happen): fall back to it so we don't crash.
    return next(iter(bands.values())) if bands else None


def _scl_cloud_mask(clipped: dict):
    """Boolean mask (True = cloud/shadow/cirrus/invalid) from the SCL band.

    Returns None when there is no SCL band (e.g. Sentinel-1, or an L1C
    fallback), meaning "no cloud information available — mask nothing".
    """
    bands = clipped.get("bands") or {}
    scl = bands.get("SCL")
    if scl is None:
        return None
    # SCL rides through stack/clip as float32 with NaN outside the polygon;
    # round to the nearest class id and test membership.
    scl_int = np.rint(np.nan_to_num(scl, nan=0.0)).astype("int16")
    cloud = np.isin(scl_int, list(_SCL_INVALID_CLASSES))
    return cloud


def _valid_pixel_mask(clipped: dict):
    """Boolean mask of pixels that carry real, usable data inside the AOI.

    A pixel is valid when it is: inside the clip mask (the rasterized AOI),
    finite and non-zero on a spectral band (nodata gate), AND — for Sentinel-2
    with an SCL band — not flagged cloud/shadow/cirrus (BUG 2 cloud masking).
    Returns (valid_mask, inside_mask, cloud_mask_or_None).
    """
    band = _reference_band(clipped)
    if band is None:
        return None, None, None
    mask = clipped.get("mask")
    inside = mask if mask is not None else np.ones(band.shape, dtype=bool)
    nodata_ok = np.isfinite(band) & (band != 0)
    cloud = _scl_cloud_mask(clipped)
    valid = inside & nodata_ok
    if cloud is not None:
        valid = valid & (~cloud)
    return valid, inside, cloud


def _valid_pixel_percent(clipped: dict) -> float:
    """Percentage of in-polygon pixels that carry real, usable (non-nodata,
    non-cloud) data. Used as the quick candidate-acceptance gate (FIX 3). The
    authoritative pass/fail coverage metric is `compute_coverage` (BUG 2/3).
    """
    valid, inside, _cloud = _valid_pixel_mask(clipped)
    if valid is None:
        return 0.0
    inside_count = int(np.count_nonzero(inside))
    if inside_count == 0:
        return 0.0
    return 100.0 * int(np.count_nonzero(valid)) / inside_count


def _erode_mask(mask, iterations: int = 1):
    """Erode a boolean mask inward by `iterations` pixels (4-connectivity).

    Used to build the "interior AOI" for the coverage pass/fail test: boundary
    pixels are excluded because whether a rasterized boundary pixel falls inside
    or outside the polygon is a convention artifact, not a real coverage gap.
    Uses scipy when available, else a pure-numpy 4-neighbour shrink.
    """
    if mask is None:
        return None
    try:
        from scipy.ndimage import binary_erosion

        return binary_erosion(mask, iterations=iterations)
    except Exception:  # scipy absent — cheap 4-neighbour fallback
        m = mask
        for _ in range(max(1, iterations)):
            up = np.zeros_like(m); up[:-1, :] = m[1:, :]
            dn = np.zeros_like(m); dn[1:, :] = m[:-1, :]
            lf = np.zeros_like(m); lf[:, :-1] = m[:, 1:]
            rt = np.zeros_like(m); rt[:, 1:] = m[:, :-1]
            m = m & up & dn & lf & rt
        return m


def _gap_geometry(gap_mask, transform, crs) -> list:
    """Describe uncovered regions as disjoint components with area + bbox (WGS84).

    `gap_mask` is a boolean array (True = uncovered pixel inside the interior
    AOI). Returns a list of {area_km2, bbox:{west,south,east,north}, pixels}
    dicts, one per connected component, so a caller can report WHERE coverage is
    missing (BUG 2/3), not just how much. Empty list when there are no gaps.
    """
    if gap_mask is None or not gap_mask.any():
        return []
    try:
        from scipy.ndimage import label as _label

        labelled, n = _label(gap_mask)
        components = range(1, n + 1)
        comp_of = lambda i: labelled == i  # noqa: E731
    except Exception:
        # No scipy: treat the whole gap as one component.
        components = [1]
        comp_of = lambda i: gap_mask  # noqa: E731

    px_area_km2 = abs(transform.a * transform.e) / 1e6  # metres^2 -> km^2
    out = []
    for i in components:
        comp = comp_of(i)
        rows = np.any(comp, axis=1)
        cols = np.any(comp, axis=0)
        r0, r1 = np.where(rows)[0][[0, -1]]
        c0, c1 = np.where(cols)[0][[0, -1]]
        # Pixel-corner extent of the component's bbox in the raster CRS.
        x0 = transform.c + c0 * transform.a
        x1 = transform.c + (c1 + 1) * transform.a
        y0 = transform.f + r0 * transform.e
        y1 = transform.f + (r1 + 1) * transform.e
        try:
            west, south, east, north = transform_bounds(
                crs, "EPSG:4326",
                min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1),
            )
        except (rasterio.errors.RasterioError, ValueError):
            west = south = east = north = None
        pixels = int(np.count_nonzero(comp))
        out.append({
            "pixels": pixels,
            "area_km2": round(pixels * px_area_km2, 4),
            "bbox": None if west is None else {
                "west": round(west, 6), "south": round(south, 6),
                "east": round(east, 6), "north": round(north, 6),
            },
        })
    out.sort(key=lambda g: g["area_km2"], reverse=True)
    return out


def compute_coverage(clipped: dict) -> dict:
    """Real valid-pixel coverage of the AOI, with interior-AOI pass/fail + gaps.

    Replaces footprint/geometric overlap as the authoritative coverage metric
    (BUG 2). Coverage is measured on VALID pixels — inside the rasterized AOI,
    carrying non-nodata data, and (Sentinel-2) not cloud/shadow/cirrus per SCL.

    Per the coverage contract:
      - `interior_coverage_percent` is the PASS/FAIL metric and must be 100.0.
        The interior AOI is the clip mask eroded inward by one pixel, so
        boundary-pixel rasterization artifacts never count as gaps.
      - `full_aoi_coverage_percent` is informational (slightly < 100 from the
        boundary pixels the erosion drops).
      - Any uncovered region inside the interior AOI is a genuine gap regardless
        of size; `gaps` lists them geometrically (area/bbox), and `gap_cause`
        splits the uncovered pixels into nodata-caused vs cloud-caused so the
        caller can tell "more tiles would fix it" from "the sky was covered".
    """
    valid, inside, cloud = _valid_pixel_mask(clipped)
    transform = clipped.get("transform")
    crs = clipped.get("crs")
    if valid is None or inside is None:
        return {
            "interior_coverage_percent": 0.0,
            "full_aoi_coverage_percent": 0.0,
            "covered": False,
            "gaps": [],
            "gap_cause": {"nodata": 0, "cloud": 0},
        }

    interior = _erode_mask(inside, 1)
    # If erosion removes everything (a very thin AOI a pixel wide), fall back to
    # the full mask so we don't vacuously "pass" on an empty interior.
    if interior is None or not interior.any():
        interior = inside

    full_count = int(np.count_nonzero(inside))
    full_valid = int(np.count_nonzero(valid & inside))
    int_count = int(np.count_nonzero(interior))
    int_valid = int(np.count_nonzero(valid & interior))

    full_pct = round(100.0 * full_valid / full_count, 4) if full_count else 0.0
    int_pct = round(100.0 * int_valid / int_count, 4) if int_count else 0.0

    gap_mask = interior & (~valid)
    gaps = _gap_geometry(gap_mask, transform, crs) if transform is not None else []

    # Attribute each interior gap pixel to nodata vs cloud.
    band = _reference_band(clipped)
    nodata_gap = interior & ~(np.isfinite(band) & (band != 0)) if band is not None else gap_mask
    cloud_gap = interior & cloud if cloud is not None else np.zeros_like(gap_mask)
    gap_cause = {
        "nodata": int(np.count_nonzero(gap_mask & nodata_gap)),
        "cloud": int(np.count_nonzero(gap_mask & cloud_gap & ~nodata_gap)),
    }

    return {
        "interior_coverage_percent": int_pct,
        "full_aoi_coverage_percent": full_pct,
        "covered": int_pct >= 100.0,
        "gaps": gaps,
        "gap_cause": gap_cause,
    }


def _compute_bounds(clipped: dict) -> Optional[dict]:
    """Geographic bounds of the exported PNGs, for map georeferencing.

    The clip is in the scene's native CRS (UTM); the PNGs span the clip's full
    extent. A web map needs that extent in WGS84 lng/lat. Derives the extent
    from the clip transform + shape, reprojects the corners to EPSG:4326, and
    returns the extent in several common shapes so the frontend can pick:

        {
            "crs": "EPSG:4326",
            "bounds": {"west","south","east","north"},
            # Leaflet: L.imageOverlay(url, bounds_leaflet)
            "bounds_leaflet": [[south, west], [north, east]],
            # MapLibre/Mapbox image source: clockwise from top-left, [lng,lat]
            "bounds_corners": [[w,n],[e,n],[e,s],[w,s]],
        }

    Returns None if the transform/shape/crs are unavailable.
    """
    transform = clipped.get("transform")
    shape_hw = clipped.get("shape")
    crs = clipped.get("crs")
    if transform is None or not shape_hw or crs is None:
        return None

    h, w = shape_hw
    left = transform.c
    top = transform.f
    right = transform.c + transform.a * w
    bottom = transform.f + transform.e * h

    try:
        west, south, east, north = transform_bounds(
            crs, "EPSG:4326", left, bottom, right, top
        )
    except (rasterio.errors.RasterioError, ValueError) as exc:
        logger.warning("Could not reproject bounds to WGS84: %s", exc)
        return None

    west, south = round(west, 6), round(south, 6)
    east, north = round(east, 6), round(north, 6)
    return {
        "crs": "EPSG:4326",
        "bounds": {"west": west, "south": south, "east": east, "north": north},
        "bounds_leaflet": [[south, west], [north, east]],
        "bounds_corners": [
            [west, north], [east, north], [east, south], [west, south]
        ],
    }


def _attempt_clip(
    selection: dict,
    scenes,
    merged_polygon: dict,
    event_id: str,
    token: str,
    disaster_type: str,
) -> Optional[dict]:
    """Download -> stack -> clip for one candidate (single scene or mosaic).

    Returns the clipped cube (with a `valid_percent` field) or None if any of
    the download/stack/clip stages fails.
    """
    satellite_type = selection.get("satellite_type", "sentinel-2")
    n_tiles = len(scenes) if isinstance(scenes, list) else 1

    imagery = download_imagery(
        selection, scenes, event_id, token, disaster_type
    )
    if imagery is None:
        return None
    # BUG 7 — the mosaic step lives inside download_imagery (_mosaic_bands), so
    # this peak covers download + mosaic; sample it against the tile count so we
    # can see how peak RSS scales with tiles per mosaic.
    _mem_stage("download+mosaic", tiles=n_tiles)

    # For S1 GRD (GCP-georeferenced), warp into the AOI's UTM zone so the grid
    # is metric and clip_to_polygon has a real affine to work with (BUG 1). S2
    # is already affine and ignores dst_crs.
    dst_crs = (
        _dst_crs_from_polygon(merged_polygon)
        if satellite_type == "sentinel-1"
        else None
    )
    stacked = stack_bands(imagery["band_paths"], satellite_type, dst_crs)
    if stacked is None:
        return None
    _mem_stage("stack", tiles=n_tiles)

    clipped = clip_to_polygon(stacked, merged_polygon)
    if clipped is None:
        return None

    _mem_stage("clip", tiles=n_tiles)
    # Stash the pre-clip stacked cube so the caller can re-clip the same
    # imagery to individual city polygons without downloading/stacking again.
    clipped["_stacked"] = stacked
    clipped["valid_percent"] = _valid_pixel_percent(clipped)
    return clipped


def _slugify(name: str) -> str:
    """Turn a city name into a filesystem/URL-safe slug for artifact paths."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "city"


def _render_clip(
    clipped: dict,
    satellite_type: str,
    disaster_type: str,
    out_id: str,
) -> Optional[dict]:
    """Render the cheap tail for one clipped cube.

    indices -> PNGs -> vectorize -> bounds. `out_id` namespaces the PNG output
    directory (`<temp>/<out_id>/`), so callers pass `<event_id>` for the merged
    result and `<event_id>/cities/<slug>` for a per-city one. Returns the
    per-clip result dict (without `valid_percent`, which the caller sets) or
    None if indices/PNG export fails.
    """
    indices = calculate_indices(clipped, satellite_type, disaster_type)
    if indices is None:
        logger.error("Index calculation failed for %s", out_id)
        return None
    _mem_stage("index")

    pngs = export_png(indices, clipped, out_id, disaster_type)
    if pngs is None:
        logger.error("PNG export failed for %s", out_id)
        return None

    geojson = vectorize_classification(
        indices["classification_array"],
        clipped["transform"],
        clipped["crs"],
        disaster_type,
        scheme_key=indices["scheme_key"],
    )
    _mem_stage("vectorize")

    return {
        "satellite_type": satellite_type,
        "index_type": indices["index_type"],
        "water_percent": indices["water_percent"],
        "mean_index": indices["mean_value"],
        "class_counts": indices["class_counts"],
        "affected_area_km2": geojson["total_area"],
        # BUG 5 — calibration contract rides through to the result dict.
        "index_calibrated": indices.get("index_calibrated"),
        "index_units": indices.get("index_units"),
        "png_paths": pngs,
        "geojson": geojson,
        # Geographic extent of the PNGs, for map georeferencing. All PNGs from
        # this clip share these bounds (same clip extent). See _compute_bounds.
        "bounds": _compute_bounds(clipped),
    }


def _render_per_city(
    stacked: Optional[dict],
    satellite_type: str,
    disaster_type: str,
    event_id: str,
    city_boundaries: list,
) -> list:
    """Re-clip the already-stacked mosaic to each city and render its artifacts.

    Reuses the expensive stacked cube (no re-download). For each city boundary
    (`{"name", "geojson"}`) it clips to that city's polygon, checks the polygon
    actually has data (skips a city the imagery doesn't reach), and renders a
    full artifact set namespaced under `<event_id>/cities/<slug>/`. Returns a
    list of per-city result dicts; cities with no usable data are omitted.
    """
    if not stacked or not city_boundaries:
        return []

    out: list = []
    for cb in city_boundaries:
        name = cb.get("name") if isinstance(cb, dict) else None
        geojson_geom = cb.get("geojson") if isinstance(cb, dict) else None
        if not geojson_geom:
            continue
        slug = _slugify(name)

        clipped = clip_to_polygon(stacked, geojson_geom)
        if clipped is None:
            logger.warning("Per-city clip failed for %s; skipping", name)
            continue

        valid = _valid_pixel_percent(clipped)
        if valid < MIN_VALID_PIXEL_PERCENT:
            logger.info(
                "City %s has only %.2f%% valid pixels (< %.1f%%); imagery does "
                "not reach it, skipping per-city render",
                name,
                valid,
                MIN_VALID_PIXEL_PERCENT,
            )
            continue

        city_result = _render_clip(
            clipped,
            satellite_type,
            disaster_type,
            f"{event_id}/cities/{slug}",
        )
        if city_result is None:
            logger.warning("Per-city render failed for %s; skipping", name)
            continue

        city_result["name"] = name
        city_result["slug"] = slug
        city_result["valid_percent"] = round(valid, 2)
        out.append(city_result)
        logger.info(
            "Per-city render for %s: %.2f km^2 affected, %.1f%% valid",
            name,
            city_result["affected_area_km2"],
            valid,
        )

    return out


def process_satellite_imagery(
    selection: dict,
    scene_metadata,
    bbox: tuple,
    merged_polygon: dict,
    event_id: str,
    token: str,
    disaster_type: str,
    city_geoms=None,
    city_boundaries=None,
    tracker=None,
) -> Optional[dict]:
    """Run the full remote-sensing pipeline to 100% valid-pixel AOI coverage.

    download_imagery -> stack_bands -> clip_to_polygon -> calculate_indices
        -> export_png -> vectorize_classification

    Coverage is measured on VALID pixels (non-nodata, and for Sentinel-2 non-cloud
    per SCL) of the AOI, NOT on footprint overlap (BUG 2). Scene selection is a
    tiered, temporally-coherent search (BUG 3): the anchor is the most recent
    acquisition; tiers widen the date window (0, +/-3, +/-7, +/-14 d) and, for
    tiers 1-3, require the same relative orbit; Sentinel-1 never mixes ascending
    and descending passes in one mosaic. Within a tier, acquisitions are added
    best-first and the cumulative mosaic is re-clipped until interior-AOI
    coverage reaches 100%. The first tier that reaches 100% wins.

    Tiers 3 and 4 lower the confidence score (via `tracker`) and append an
    anomaly naming the temporal spread. If NO tier reaches 100%, returns
    ``{"status": "failed", "reason": "insufficient_coverage", ...}`` with the
    best coverage achieved and the geometry of the uncovered gaps — the pipeline
    NEVER analyses a partial AOI and reports a risk level for it.

    Args:
        selection / scene_metadata / bbox / merged_polygon / event_id / token /
        disaster_type: as before.
        city_geoms: per-city shapely geometries (WGS84), used only as a hint for
            spreading a tier's scenes across scattered cities.
        city_boundaries: per-city `{"name","geojson"}`; when >1, per-city
            artifacts are rendered from the same accepted mosaic.
        tracker: the event's `ConfidenceTracker`; tiers 3/4 add a concern and
            lower confidence through it.

    On success returns the merged result dict, which now also carries
    `coverage_percent`, `full_aoi_coverage_percent`, `coverage_tier`,
    `temporal_spread_days`, `acquisition_count` and `bytes_downloaded`.
    """
    satellite_type = selection.get("satellite_type", "sentinel-2")

    scenes = (
        list(scene_metadata)
        if isinstance(scene_metadata, list)
        else [scene_metadata]
    )
    if not scenes:
        logger.error("No scenes provided to process_satellite_imagery")
        return None

    from sentinel import (
        build_coverage_tiers,
        dedupe_by_acquisition,
        scene_acq_date,
        scene_orbit_direction,
    )

    # BUG 4a: collapse COG/non-COG twins of one acquisition to a single candidate
    # BEFORE any download, so we never fetch the same acquisition twice.
    scenes = dedupe_by_acquisition(scenes)

    # BUG 4b: validate real geometric intersection in a common CRS before
    # downloading. A scene whose footprint doesn't intersect the AOI at all can
    # never contribute valid pixels, so drop it up front (no wasted download).
    from sentinel import _scene_aoi_overlap
    try:
        aoi_shape = shape(merged_polygon) if merged_polygon else None
    except (ValueError, AttributeError, TypeError):
        aoi_shape = None
    if aoi_shape is not None:
        pre = len(scenes)
        scenes = [s for s in scenes if _scene_aoi_overlap(s, aoi_shape) > 0.0]
        if len(scenes) < pre:
            logger.info(
                "Dropped %d scene(s) with zero AOI footprint intersection "
                "before download", pre - len(scenes),
            )
    if not scenes:
        logger.error("No candidate scene geometrically intersects the AOI")
        return {
            "status": "failed",
            "reason": "insufficient_coverage",
            "satellite_type": satellite_type,
            "coverage_percent": 0.0,
            "best_interior_coverage_percent": 0.0,
            "gaps": [],
            "detail": "no candidate scene intersects the AOI footprint",
        }

    tiers = build_coverage_tiers(scenes, satellite_type)
    if not tiers:
        # No parseable acquisition dates — fall back to treating all scenes as a
        # single tier-4 group so the coverage search still runs.
        tiers = [(4, None, scenes)]

    bytes_before = _bytes_downloaded_total()
    best_cov = None
    best_interior = -1.0

    for tier, orbit_dir, group in tiers:
        # BUG 3: within a tier, add acquisitions best-first and re-measure real
        # coverage until interior AOI hits 100%. Consecutive doomed downloads
        # (valid pixels don't grow) abort the tier early (BUG 4c).
        accepted: list = []
        doomed_streak = 0
        clipped = None
        cov = None
        for scene in group:
            trial = accepted + [scene]
            attempt_id = f"{event_id}/t{tier}"
            trial_clip = _attempt_clip(
                selection, trial, merged_polygon, attempt_id, token,
                disaster_type,
            )
            if trial_clip is None:
                doomed_streak += 1
                if doomed_streak >= DOOMED_DOWNLOAD_LIMIT:
                    logger.warning(
                        "Tier %d: %d consecutive failed/empty downloads; "
                        "aborting tier", tier, doomed_streak,
                    )
                    break
                continue
            trial_cov = compute_coverage(trial_clip)
            gained = trial_cov["interior_coverage_percent"] - (
                cov["interior_coverage_percent"] if cov else 0.0
            )
            if gained <= 0.01 and accepted:
                # This acquisition added no coverage — a doomed contribution.
                doomed_streak += 1
                logger.info(
                    "Tier %d: %s added +%.3f%% coverage (doomed streak %d)",
                    tier, scene.get("Name"), gained, doomed_streak,
                )
                if doomed_streak >= DOOMED_DOWNLOAD_LIMIT:
                    logger.warning(
                        "Tier %d: %d consecutive non-contributing downloads; "
                        "aborting tier", tier, doomed_streak,
                    )
                    break
                continue
            doomed_streak = 0
            accepted = trial
            clipped = trial_clip
            cov = trial_cov
            logger.info(
                "Tier %d: %d acq -> interior coverage %.3f%% (full %.3f%%)",
                tier, len(accepted), cov["interior_coverage_percent"],
                cov["full_aoi_coverage_percent"],
            )
            if cov["covered"]:
                break

        if cov and cov["interior_coverage_percent"] > best_interior:
            best_interior = cov["interior_coverage_percent"]
            best_cov = cov

        if cov and cov["covered"]:
            # 100% reached in this tier. Compute temporal spread + count.
            dates = [d for d in (scene_acq_date(s) for s in accepted) if d]
            spread = (max(dates) - min(dates)).days if len(dates) >= 2 else 0
            logger.info(
                "Coverage reached 100%% at tier %d (%d acquisition(s), "
                "%d-day spread, orbit=%s)",
                tier, len(accepted), spread, orbit_dir,
            )

            if not (city_boundaries and len(city_boundaries) > 1):
                clipped.pop("_stacked", None)
                import gc
                gc.collect()

            merged_result = _render_clip(
                clipped, satellite_type, disaster_type, event_id
            )
            if merged_result is None:
                logger.error("Aborting pipeline: merged render failed")
                return None

            bytes_after = _bytes_downloaded_total()
            merged_result.update({
                "valid_percent": round(cov["interior_coverage_percent"], 2),
                "coverage_percent": cov["interior_coverage_percent"],
                "full_aoi_coverage_percent": cov["full_aoi_coverage_percent"],
                "coverage_tier": tier,
                "temporal_spread_days": spread,
                "acquisition_count": len(accepted),
                "orbit_direction": orbit_dir,
                "coverage_gaps": [],
                "bytes_downloaded": bytes_after - bytes_before,
                "processing_level": (
                    "L2A" if satellite_type == "sentinel-2" else None
                ),
                # BUG 7 — per-stage peak RSS + which stage peaked, scaled by tiles.
                "memory_report": memory_report(),
            })

            # Tiers 3 and 4 are a real limitation: a 7-14 day spread on a flood
            # is stale imagery. Lower confidence + append an anomaly so it is
            # visible downstream.
            if tier >= 3 and tracker is not None:
                sev = "HIGH" if tier == 4 else "MEDIUM"
                tracker.add_concern(
                    f"Coverage reached 100% only at tier {tier} "
                    f"(±{COVERAGE_TIERS_DAYS.get(tier)}d window, "
                    f"{spread}-day temporal spread across {len(accepted)} "
                    f"acquisitions"
                    + (", mixed relative orbits" if tier == 4 else "")
                    + "); imagery is temporally dispersed for this disaster.",
                    sev,
                )
                merged_result.setdefault("coverage_anomalies", []).append({
                    "type": "temporal_spread",
                    "tier": tier,
                    "temporal_spread_days": spread,
                    "acquisition_count": len(accepted),
                    "severity": sev,
                })

            # BUG 5 — the SAR index is uncalibrated (10*log10 of raw GRD DN, no
            # speckle filter, no terrain correction). Append a concern stating it
            # must not be threshold-compared and lower confidence via the tracker
            # (not a hardcoded number) so the limitation is visible downstream.
            if merged_result.get("index_calibrated") is False and tracker is not None:
                tracker.add_concern(
                    "SAR index is uncalibrated (10*log10 of raw GRD DN; no "
                    "radiometric calibration LUT, speckle filter, or terrain "
                    "correction). It is a relative DN-space value in "
                    f"'{merged_result.get('index_units')}', NOT calibrated "
                    "sigma0 dB — it must not be threshold-compared as an "
                    "absolute water/flood cutoff.",
                    "MEDIUM",
                )

            if city_boundaries and len(city_boundaries) > 1:
                cities = _render_per_city(
                    stacked=clipped.get("_stacked"),
                    satellite_type=satellite_type,
                    disaster_type=disaster_type,
                    event_id=event_id,
                    city_boundaries=city_boundaries,
                )
                if cities:
                    merged_result["cities"] = cities

            logger.info("Satellite imagery pipeline complete for %s", event_id)
            return merged_result

    # No tier reached 100% coverage — fail honestly with gap geometry. NEVER
    # analyse a partial AOI (BUG 3).
    gaps = best_cov["gaps"] if best_cov else []
    gap_cause = best_cov["gap_cause"] if best_cov else {"nodata": 0, "cloud": 0}
    total_gap_km2 = round(sum(g["area_km2"] for g in gaps), 4)
    bytes_after = _bytes_downloaded_total()
    logger.error(
        "INSUFFICIENT COVERAGE for %s: best interior coverage %.3f%% across all "
        "tiers; %d uncovered region(s) totalling %.3f km^2 (nodata=%d px, "
        "cloud=%d px). Refusing to analyse a partial AOI.",
        event_id, max(best_interior, 0.0), len(gaps), total_gap_km2,
        gap_cause["nodata"], gap_cause["cloud"],
    )
    return {
        "status": "failed",
        "reason": "insufficient_coverage",
        "satellite_type": satellite_type,
        "coverage_percent": round(max(best_interior, 0.0), 3),
        "best_interior_coverage_percent": round(max(best_interior, 0.0), 3),
        "full_aoi_coverage_percent": (
            best_cov["full_aoi_coverage_percent"] if best_cov else 0.0
        ),
        "uncovered_regions": len(gaps),
        "uncovered_area_km2": total_gap_km2,
        "gaps": gaps,
        "gap_cause": gap_cause,
        "bytes_downloaded": bytes_after - bytes_before,
        "processing_level": (
            "L2A" if satellite_type == "sentinel-2" else None
        ),
    }


async def cleanup_event_temp(event_id: str) -> None:
    """Remove an event's working files after the results are safe on R2.

    Always deletes `<temp>/hazardmind-satellite/<event_id>/` — the extracted band
    rasters and the PNG/GeoJSON outputs.

    The downloaded `.zip` scene archives (~0.8-1.6 GB each, keyed by product Id)
    are a re-download cache. Keeping them speeds up re-processing the same scene
    locally, but on an ephemeral cloud VM with a bounded disk they accumulate and
    eventually fill the volume. So zip cleanup is controlled by an env flag:

        SATELLITE_KEEP_SCENE_CACHE=true   -> keep the .zip archives (local/dev)
        (unset / false)                   -> delete them too (production default)

    The final imagery products always live on Cloudflare R2, so deleting the
    local cache never loses any output.
    """
    import shutil

    temp_dir = os.path.join(TEMP_ROOT, str(event_id))
    if os.path.isdir(temp_dir):
        try:
            shutil.rmtree(temp_dir)
            logger.info("[Cleanup] Removed %s", temp_dir)
        except OSError as exc:
            logger.warning("[Cleanup] Could not remove %s: %s", temp_dir, exc)

    keep_cache = os.getenv("SATELLITE_KEEP_SCENE_CACHE", "").strip().lower() in (
        "1", "true", "yes",
    )
    if keep_cache:
        return

    # Production: also drop the cached scene .zip archives so the VM disk does not
    # fill up over many events.
    try:
        for name in os.listdir(TEMP_ROOT):
            if name.endswith(".zip") or name.endswith(".zip.part"):
                path = os.path.join(TEMP_ROOT, name)
                try:
                    os.remove(path)
                    logger.info("[Cleanup] Removed cached archive %s", path)
                except OSError as exc:
                    logger.warning("[Cleanup] Could not remove %s: %s", path, exc)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Live smoke test: Peshawar flood scenario end-to-end.
    from boundary import (
        get_analysis_bbox,
        get_risk_city_boundaries,
        merge_risk_boundaries,
    )
    from sentinel import authenticate_copernicus, search_imagery, select_satellite

    token = authenticate_copernicus()
    if not token:
        print("Authentication failed; skipping pipeline smoke test")
        raise SystemExit(0)

    cities = get_risk_city_boundaries(
        "Khyber Pakhtunkhwa, Pakistan", ["Peshawar", "Nowshera", "Charsadda"]
    )
    merged = merge_risk_boundaries(cities)
    bbox = get_analysis_bbox(merged)
    print("Analysis bbox:", bbox)

    selection = select_satellite("flood", bbox=bbox, token=token)
    print("Selection:", selection)

    scene = search_imagery(bbox, selection["satellite_type"], date_range=30)
    if not scene:
        print("No scene found; skipping pipeline smoke test")
        raise SystemExit(0)

    print("Scene:", scene.get("Name"))
    result = process_satellite_imagery(
        selection, scene, bbox, merged, "smoke-peshawar", token, "flood"
    )
    print("Result:", {k: v for k, v in result.items() if k != "geojson"} if result else None)
