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

# Sentinel-2 bands to download per disaster type. TCI (true-colour image) is
# always included for the true_color export. Keys are the band tokens that
# appear in JP2 filenames inside the .SAFE archive (e.g. "..._B03_10m.jp2").
_S2_BANDS = {
    "flood": ["B03", "B08", "B11", "TCI"],
    "earthquake": ["B02", "B04", "B08", "TCI"],
    "landslide": ["B03", "B04", "B08", "TCI"],
}
_S2_DEFAULT_BANDS = ["B04", "B03", "B02", "TCI"]

# Native resolution (m) of each Sentinel-2 band we touch. 20 m bands (B11) are
# resampled to 10 m during stacking.
_S2_BAND_RES = {
    "B02": 10, "B03": 10, "B04": 10, "B08": 10,
    "B11": 20, "TCI": 10,
}

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
def _download_product_zip(
    scene_metadata: dict,
    token: str,
    timeout: int = 600,
    max_retries: int = 4,
) -> Optional[str]:
    """Download a scene's full product archive from CDSE (resumable).

    CDSE products are large (often hundreds of MB) and the stream can drop
    mid-transfer. The download is resumable: on a connection error we re-issue
    the request with an HTTP Range header and append from where we left off,
    rather than restarting. Returns the path to the downloaded `.zip`, or None.
    """
    product_id = scene_metadata.get("Id")
    if not product_id:
        logger.error("Scene metadata has no 'Id'; cannot download")
        return None

    name = scene_metadata.get("Name", product_id)
    os.makedirs(TEMP_ROOT, exist_ok=True)
    dest_path = os.path.join(TEMP_ROOT, f"{product_id}.zip")
    part_path = f"{dest_path}.part"

    # A previously completed download can be reused as-is.
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        logger.info("Reusing cached product archive %s", dest_path)
        return dest_path

    url = DOWNLOAD_URL.format(product_id=product_id)
    auth_header = {"Authorization": f"Bearer {token}"}

    # Start fresh: a stale partial could be from a different scene or a server
    # that doesn't honor Range, so don't trust it.
    if os.path.exists(part_path):
        try:
            os.remove(part_path)
        except OSError:
            pass

    logger.info("Downloading scene %s from CDSE", name)
    total_size: Optional[int] = None

    try:
        with _CDSESession() as session:
            for attempt in range(max_retries + 1):
                downloaded = (
                    os.path.getsize(part_path)
                    if os.path.exists(part_path)
                    else 0
                )
                headers = dict(auth_header)
                mode = "wb"
                if downloaded:
                    headers["Range"] = f"bytes={downloaded}-"
                    mode = "ab"

                try:
                    with session.get(
                        url, headers=headers, stream=True, timeout=timeout
                    ) as response:
                        response.raise_for_status()

                        if total_size is None:
                            length = response.headers.get("Content-Length")
                            content_range = response.headers.get("Content-Range")
                            if content_range and "/" in content_range:
                                try:
                                    total_size = int(
                                        content_range.rsplit("/", 1)[1]
                                    )
                                except ValueError:
                                    total_size = None
                            elif length is not None and not downloaded:
                                total_size = int(length)

                        # Server ignored our Range (replied 200): rewrite.
                        if downloaded and response.status_code == 200:
                            mode = "wb"
                            downloaded = 0

                        with open(part_path, mode) as out:
                            for chunk in response.iter_content(
                                chunk_size=1024 * 1024
                            ):
                                if chunk:
                                    out.write(chunk)

                    final_size = os.path.getsize(part_path)
                    if total_size is not None and final_size < total_size:
                        raise requests.exceptions.ChunkedEncodingError(
                            f"incomplete: {final_size}/{total_size} bytes"
                        )

                    os.replace(part_path, dest_path)
                    logger.info(
                        "Downloaded scene to %s (%d bytes)", dest_path, final_size
                    )
                    return dest_path

                except (
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                ) as exc:
                    if attempt >= max_retries:
                        logger.error(
                            "Giving up on scene %s after %d attempts: %s",
                            name,
                            attempt + 1,
                            exc,
                        )
                        raise
                    resumed = (
                        os.path.getsize(part_path)
                        if os.path.exists(part_path)
                        else 0
                    )
                    logger.warning(
                        "Download of %s interrupted (%s); resuming from "
                        "%d bytes (attempt %d/%d)",
                        name,
                        exc,
                        resumed,
                        attempt + 1,
                        max_retries,
                    )
    except requests.RequestException as exc:
        logger.error("Failed to download scene %s: %s", name, exc)
        return None
    except OSError as exc:
        logger.error("Failed to write downloaded scene to %s: %s", dest_path, exc)
        return None

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


def _mosaic_bands(per_scene_paths: list, event_id: str) -> dict:
    """Mosaic per-band rasters from several scenes into single rasters.

    `per_scene_paths` is a list of {band_token: path} dicts (one per scene). For
    each band token present in any scene, the matching rasters are merged with
    `rasterio.merge` (which fills nodata gaps from later scenes) and written to
    `<temp>/<event_id>/bands/<token>.tif`. Returns {band_token: mosaic_path}.
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
        try:
            for src in sources:
                datasets.append(rasterio.open(src))
            arr, transform = rio_merge(datasets)
            profile = datasets[0].profile.copy()
            profile.update(
                driver="GTiff",
                height=arr.shape[1],
                width=arr.shape[2],
                count=arr.shape[0],
                transform=transform,
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

    return mosaicked


def download_imagery(
    selection: dict,
    scene_metadata,
    event_id: str,
    token: str,
    disaster_type: str,
) -> Optional[dict]:
    """Download the product(s) and extract the bands needed for this disaster.

    Args:
        selection: dict from `sentinel.select_satellite` (carries
            "satellite_type").
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
    for idx, scene in enumerate(scenes):
        zip_path = _download_product_zip(scene, token)
        if zip_path is None:
            logger.warning("Skipping scene %d: download failed", idx)
            continue
        # Each scene's bands go in their own subdir so same-named JP2s from
        # different tiles don't clobber each other before mosaicking. Key the
        # subdir on the scene's stable product Id (not a positional index):
        # _extract_bands reuses an already-present file, so a bare scene_<idx>
        # would serve a *previous* run's tile when the same event_id is
        # re-processed with a different scene selection.
        if len(scenes) > 1:
            scene_key = scene.get("Id") or f"scene_{idx}"
            scene_event = f"{event_id}/scene_{scene_key}"
        else:
            scene_event = event_id
        paths = _extract_bands(
            zip_path, scene_event, band_tokens, satellite_type
        )
        if paths:
            per_scene_paths.append(paths)

    if not per_scene_paths:
        logger.error("No bands extracted for %s", event_id)
        return None

    if len(per_scene_paths) == 1:
        band_paths = per_scene_paths[0]
    else:
        band_paths = _mosaic_bands(per_scene_paths, event_id)

    return {"satellite_type": satellite_type, "band_paths": band_paths}


# --------------------------------------------------------------------------- #
# Step 7C: stack bands into one aligned cube
# --------------------------------------------------------------------------- #
def stack_bands(band_paths: dict, satellite_type: str) -> Optional[dict]:
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
        with rasterio.open(band_paths[ref_token]) as ref:
            ref_h, ref_w = ref.height, ref.width
            ref_transform = ref.transform
            ref_crs = ref.crs

        bands: dict = {}
        for token, path in band_paths.items():
            if token.upper() == "TCI":
                continue
            with rasterio.open(path) as src:
                arr = src.read(
                    1,
                    out_shape=(ref_h, ref_w),
                    resampling=Resampling.bilinear,
                ).astype("float32")
                bands[token] = arr

        tci = None
        if "TCI" in band_paths:
            with rasterio.open(band_paths["TCI"]) as src:
                count = min(src.count, 3)
                tci = src.read(
                    indexes=list(range(1, count + 1)),
                    out_shape=(count, ref_h, ref_w),
                    resampling=Resampling.bilinear,
                ).astype("uint8")
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

    # Reproject the WGS84 polygon to the raster CRS.
    try:
        if crs is not None and crs.to_epsg() != 4326:
            geom = transform_geom("EPSG:4326", crs, merged_polygon)
        else:
            geom = merged_polygon
    except (rasterio.errors.RasterioError, ValueError) as exc:
        logger.error("Failed to reproject clip polygon: %s", exc)
        return None

    # Build the inside-polygon boolean mask using rasterio.features against an
    # in-memory single-band dataset describing the reference grid.
    from rasterio.io import MemoryFile

    try:
        profile = {
            "driver": "GTiff",
            "height": h,
            "width": w,
            "count": 1,
            "dtype": "uint8",
            "crs": crs,
            "transform": transform,
        }
        with MemoryFile() as mem:
            with mem.open(**profile) as tmp:
                tmp.write(np.ones((1, h, w), dtype="uint8"))
            with mem.open() as tmp:
                clipped, clip_transform = rio_mask(
                    tmp, [geom], crop=True, nodata=0, filled=True
                )
        inside = clipped[0] > 0
    except (rasterio.errors.RasterioError, ValueError) as exc:
        logger.error("Failed to build clip mask: %s", exc)
        return None

    # Apply the same crop window to every band by reprojecting the crop bounds
    # back into pixel offsets relative to the original transform.
    new_h, new_w = inside.shape
    col_off = round((clip_transform.c - transform.c) / transform.a)
    row_off = round((clip_transform.f - transform.f) / transform.e)

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
    elif disaster == "flood":
        b03, b08 = bands.get("B03"), bands.get("B08")
        if b03 is None or b08 is None:
            logger.error("NDWI needs B03 and B08; one is missing")
            return None
        index = _safe_ratio(b03 - b08, b03 + b08)
        index_type = "NDWI"
        scheme_key = "NDWI"
        threshold = NDWI_WATER_THRESHOLD
    else:
        b08, b04 = bands.get("B08"), bands.get("B04")
        if b08 is None or b04 is None:
            logger.error("NDVI needs B08 and B04; one is missing")
            return None
        index = _safe_ratio(b08 - b04, b08 + b04)
        index_type = "NDVI"
        scheme_key = "NDVI_LANDSLIDE" if disaster == "landslide" else "NDVI_QUAKE"
        threshold = NDVI_DAMAGE_THRESHOLD

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
def _valid_pixel_percent(clipped: dict) -> float:
    """Percentage of in-polygon pixels that carry real (non-nodata) data.

    Looks at one source band inside the clip mask: pixels that are finite and
    non-zero count as valid. A scene that barely overlaps the AOI produces a
    clip that is almost entirely nodata, which this catches (FIX 3).
    """
    bands = clipped.get("bands") or {}
    if not bands:
        return 0.0
    mask = clipped.get("mask")
    band = next(iter(bands.values()))
    if mask is None:
        inside = np.ones(band.shape, dtype=bool)
    else:
        inside = mask
    inside_count = int(np.count_nonzero(inside))
    if inside_count == 0:
        return 0.0
    valid = np.isfinite(band) & (band != 0) & inside
    return 100.0 * int(np.count_nonzero(valid)) / inside_count


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

    imagery = download_imagery(
        selection, scenes, event_id, token, disaster_type
    )
    if imagery is None:
        return None

    stacked = stack_bands(imagery["band_paths"], satellite_type)
    if stacked is None:
        return None

    clipped = clip_to_polygon(stacked, merged_polygon)
    if clipped is None:
        return None

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

    return {
        "satellite_type": satellite_type,
        "index_type": indices["index_type"],
        "water_percent": indices["water_percent"],
        "mean_index": indices["mean_value"],
        "class_counts": indices["class_counts"],
        "affected_area_km2": geojson["total_area"],
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
) -> Optional[dict]:
    """Run the full remote-sensing pipeline, coverage-aware with fallback.

    download_imagery -> stack_bands -> clip_to_polygon -> calculate_indices
        -> export_png -> vectorize_classification

    `scene_metadata` may be a single scene dict (legacy) or the ranked list from
    `sentinel.search_imagery(..., return_ranked=True)`. With a ranked list:

    - FIX 2: if the best scene covers < COVERAGE_MOSAIC_THRESHOLD of the AOI, the
      top MOSAIC_MAX_SCENES scenes are mosaicked before clipping.
    - FIX 3: after clipping, if fewer than MIN_VALID_PIXEL_PERCENT of in-polygon
      pixels carry data, the result is rejected and the next-best scene is
      tried. If every candidate is too sparse, returns a
      `{"status": "coverage_insufficient", ...}` marker instead of None.

    Args:
        selection: dict from `sentinel.select_satellite`.
        scene_metadata: scene dict or ranked list of scenes.
        bbox: analysis bbox (kept for the result payload).
        merged_polygon: merged risk geometry from `boundary.merge_risk_boundaries`.
        event_id: namespaces all artifacts.
        token: CDSE access token.
        disaster_type: drives band selection, indices and styling.
        city_geoms: optional list of per-city shapely geometries (WGS84). When
            given, the mosaic uses greedy set-cover to spread scenes across all
            cities instead of taking the top-N by score (which can bunch on one
            city and leave scattered cities uncovered).
        city_boundaries: optional list of per-city `{"name", "geojson"}` dicts.
            When there is more than one city, the accepted mosaic is re-clipped
            to each city polygon and a per-city artifact set (PNGs + GeoJSON +
            bounds, namespaced under `<event_id>/cities/<slug>/`) is rendered
            and returned under the result's `cities` key. The expensive
            download+stack is reused, so this is cheap.

    Returns the result dict on success, a `coverage_insufficient` marker if no
    candidate has enough valid data, or None if a stage hard-fails. On success
    the result carries the merged-AOI artifacts plus, for a multi-city AOI, a
    `cities` list of per-city artifact sets.
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

    # Build the ordered list of candidate attempts. The first candidate is a
    # mosaic of the top scenes when the single best does not cover enough of the
    # AOI (FIX 2); the remaining candidates are individual scenes for fallback.
    best_overlap = scenes[0].get("_overlap")
    candidates = []
    if (
        best_overlap is not None
        and best_overlap * 100 < COVERAGE_MOSAIC_THRESHOLD
        and len(scenes) > 1
    ):
        # Greedy set-cover over the individual city polygons so the mosaic
        # spreads across scattered cities instead of bunching on the single
        # best-covered one. Falls back to top-N by score when no city geometries
        # are supplied.
        from sentinel import select_mosaic_scenes

        mosaic_set = select_mosaic_scenes(scenes, city_geoms, MOSAIC_MAX_SCENES)
        logger.info(
            "Best scene covers only %.0f%% of AOI (< %.0f%%); mosaicking %d "
            "scene(s) (set-cover over %d cities)",
            best_overlap * 100,
            COVERAGE_MOSAIC_THRESHOLD,
            len(mosaic_set),
            len([g for g in (city_geoms or []) if g is not None]),
        )
        candidates.append(("mosaic", mosaic_set))
    candidates.extend(("single", [s]) for s in scenes)

    best_seen = -1.0
    for kind, scene_set in candidates:
        attempt_id = (
            f"{event_id}/mosaic" if kind == "mosaic" else event_id
        )
        clipped = _attempt_clip(
            selection,
            scene_set,
            merged_polygon,
            attempt_id,
            token,
            disaster_type,
        )
        if clipped is None:
            continue

        valid = clipped.get("valid_percent", 0.0)
        best_seen = max(best_seen, valid)
        if valid < MIN_VALID_PIXEL_PERCENT:
            names = [s.get("Name") for s in scene_set]
            logger.warning(
                "Candidate (%s) has only %.2f%% valid pixels (< %.1f%%); "
                "trying next best. Scenes: %s",
                kind,
                valid,
                MIN_VALID_PIXEL_PERCENT,
                names,
            )
            continue

        logger.info(
            "Candidate (%s) accepted with %.2f%% valid pixels", kind, valid
        )

        # Render the cheap tail (indices -> PNGs -> vectorize -> bounds) for the
        # whole merged AOI.
        merged_result = _render_clip(
            clipped, satellite_type, disaster_type, event_id
        )
        if merged_result is None:
            logger.error("Aborting pipeline: merged render failed")
            return None
        merged_result["valid_percent"] = round(valid, 2)

        # Per-city artifacts. The expensive download+stack is already done; for
        # a multi-city AOI we re-clip the *same* stacked mosaic to each city
        # polygon and render its own PNGs + GeoJSON. This is far cheaper than a
        # fresh search per city and gives the hazard agent individual,
        # easy-to-consume layers per city (in addition to the merged result).
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
                logger.info(
                    "Rendered %d per-city artifact set(s) for %s",
                    len(cities),
                    event_id,
                )

        logger.info("Satellite imagery pipeline complete for %s", event_id)
        return merged_result

    # Every candidate was too sparse to be usable.
    logger.error(
        "Coverage insufficient for %s: best candidate had only %.2f%% valid "
        "pixels (need >= %.1f%%)",
        event_id,
        max(best_seen, 0.0),
        MIN_VALID_PIXEL_PERCENT,
    )
    return {
        "status": "coverage_insufficient",
        "satellite_type": satellite_type,
        "best_valid_percent": round(max(best_seen, 0.0), 2),
        "min_required_percent": MIN_VALID_PIXEL_PERCENT,
    }


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
