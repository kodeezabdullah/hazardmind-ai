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
import tempfile
import zipfile
from typing import Optional

import numpy as np
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.features import shapes
from rasterio.mask import mask as rio_mask
from rasterio.warp import transform_geom
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


def download_imagery(
    selection: dict,
    scene_metadata: dict,
    event_id: str,
    token: str,
    disaster_type: str,
) -> Optional[dict]:
    """Download the product and extract the bands needed for this disaster.

    Args:
        selection: dict from `sentinel.select_satellite` (carries
            "satellite_type").
        scene_metadata: scene dict from `sentinel.search_imagery`.
        event_id: namespaces extracted bands under <temp>/<event_id>/bands/.
        token: CDSE access token.
        disaster_type: drives which Sentinel-2 bands are pulled.

    Returns {"satellite_type": ..., "band_paths": {token: path, ...}} or None.
    """
    if not scene_metadata:
        logger.error("No scene metadata provided to download_imagery")
        return None
    if not token:
        logger.error("No access token provided; cannot download imagery")
        return None

    satellite_type = selection.get("satellite_type", "sentinel-2")
    disaster = (disaster_type or "").strip().lower()

    if satellite_type == "sentinel-1":
        band_tokens = _S1_POLARIZATIONS
    else:
        band_tokens = _S2_BANDS.get(disaster, _S2_DEFAULT_BANDS)

    zip_path = _download_product_zip(scene_metadata, token)
    if zip_path is None:
        return None

    band_paths = _extract_bands(zip_path, event_id, band_tokens, satellite_type)
    if not band_paths:
        logger.error("No bands extracted for %s", event_id)
        return None

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
        valid = np.isfinite(vv) & (vv > 0)
        index[valid] = 10.0 * np.log10(vv[valid])
        index_type = "SAR"
        threshold = SAR_WATER_THRESHOLD_DB
        affected = np.isfinite(index) & (index < threshold)
    elif disaster == "flood":
        b03, b08 = bands.get("B03"), bands.get("B08")
        if b03 is None or b08 is None:
            logger.error("NDWI needs B03 and B08; one is missing")
            return None
        index = _safe_ratio(b03 - b08, b03 + b08)
        index_type = "NDWI"
        threshold = NDWI_WATER_THRESHOLD
        affected = np.isfinite(index) & (index > threshold)
    else:
        b08, b04 = bands.get("B08"), bands.get("B04")
        if b08 is None or b04 is None:
            logger.error("NDVI needs B08 and B04; one is missing")
            return None
        index = _safe_ratio(b08 - b04, b08 + b04)
        index_type = "NDVI"
        threshold = NDVI_DAMAGE_THRESHOLD
        affected = np.isfinite(index) & (index < threshold)

    # Classification: 1 affected, 0 unaffected, 255 nodata/outside polygon.
    classification = np.full(index.shape, 255, dtype="uint8")
    valid = np.isfinite(index)
    if mask is not None:
        valid = valid & mask
    classification[valid] = 0
    classification[valid & affected] = 1

    valid_count = int(valid.sum())
    affected_count = int((classification == 1).sum())
    water_percent = (
        round(100.0 * affected_count / valid_count, 2) if valid_count else 0.0
    )
    mean_value = (
        round(float(np.nanmean(index[valid])), 4) if valid_count else 0.0
    )

    logger.info(
        "%s: %.2f%% affected, mean=%.4f, threshold=%s",
        index_type,
        water_percent,
        mean_value,
        threshold,
    )
    return {
        "index_type": index_type,
        "array": index,
        "classification_array": classification,
        "water_percent": water_percent,
        "mean_value": mean_value,
        "threshold_used": threshold,
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

    try:
        # --- true_color.png -------------------------------------------------
        tci = clipped.get("tci")
        if tci is not None and tci.shape[0] >= 3:
            rgb = np.dstack([_decimate(tci[i]) for i in range(3)]).astype(
                "uint8"
            )
        else:
            # Sentinel-1 (or no TCI): greyscale from the index source band.
            base = None
            for tok in ("B04", "VV", "B08", "B03"):
                if tok in clipped["bands"]:
                    base = clipped["bands"][tok]
                    break
            if base is None:
                base = next(iter(clipped["bands"].values()))
            g = _stretch_uint8(_decimate(base))
            rgb = np.dstack([g, g, g])
        tc_path = os.path.join(out_dir, "true_color.png")
        Image.fromarray(rgb, mode="RGB").save(tc_path, format="PNG", optimize=True)
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
        # Transparent where there was no data.
        alpha = np.where(np.isfinite(index), 255, 0).astype("uint8")
        index_rgba = np.dstack([index_rgb, alpha])
        idx_path = os.path.join(out_dir, "index_map.png")
        Image.fromarray(index_rgba, mode="RGBA").save(
            idx_path, format="PNG", optimize=True
        )
        paths["index_map"] = idx_path

        # --- classification.png (semi-transparent overlay) ------------------
        cls = _decimate(indices["classification_array"])
        # Colours per disaster: affected vs unaffected.
        if disaster == "flood" or index_type in ("NDWI", "SAR"):
            affected_rgb = (37, 99, 235)    # blue = water
            ok_rgb = (255, 255, 255)        # white = land
        elif disaster == "landslide":
            affected_rgb = (234, 88, 12)    # orange = scar
            ok_rgb = (34, 197, 94)          # green = ok
        else:  # earthquake / default
            affected_rgb = (220, 38, 38)    # red = damage
            ok_rgb = (34, 197, 94)          # green = ok

        h, w = cls.shape
        rgba = np.zeros((h, w, 4), dtype="uint8")
        ok = cls == 0
        aff = cls == 1
        rgba[ok] = (*ok_rgb, 110)        # ~43% opacity
        rgba[aff] = (*affected_rgb, 180)  # ~70% opacity
        # cls == 255 stays fully transparent.
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


def vectorize_classification(
    classification_array: np.ndarray,
    transform,
    crs,
    disaster_type: str,
) -> dict:
    """Turn the affected-pixel mask into a GeoJSON FeatureCollection.

    Polygonizes pixels classified as affected (value 1), reprojects to WGS84,
    simplifies (tolerance 0.001 deg), and drops polygons smaller than
    MIN_ZONE_AREA_KM2. Each feature carries risk_type, area_km2 and a severity
    derived from the polygon's size. Returns a FeatureCollection with an added
    `total_area` (km^2) member.
    """
    disaster = (disaster_type or "").strip().lower() or "unknown"
    affected = (classification_array == 1).astype("uint8")

    features = []
    total_area = 0.0
    try:
        for geom, value in shapes(affected, mask=affected.astype(bool),
                                  transform=transform):
            if value != 1:
                continue
            poly = shape(geom)
            # Reproject geometry to WGS84 for the output and area threshold.
            if crs is not None and crs.to_epsg() != 4326:
                poly = shape(transform_geom(crs, "EPSG:4326", mapping(poly)))
            poly = poly.simplify(0.001, preserve_topology=True)
            if poly.is_empty:
                continue

            area_km2 = round(_polygon_area_km2(poly, "EPSG:4326"), 3)
            if area_km2 < MIN_ZONE_AREA_KM2:
                continue

            if area_km2 >= 25:
                severity = "high"
            elif area_km2 >= 5:
                severity = "medium"
            else:
                severity = "low"

            total_area += area_km2
            features.append(
                {
                    "type": "Feature",
                    "geometry": mapping(poly),
                    "properties": {
                        "risk_type": disaster,
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
def process_satellite_imagery(
    selection: dict,
    scene_metadata: dict,
    bbox: tuple,
    merged_polygon: dict,
    event_id: str,
    token: str,
    disaster_type: str,
) -> Optional[dict]:
    """Run the full remote-sensing pipeline for a scene.

    download_imagery -> stack_bands -> clip_to_polygon -> calculate_indices
        -> export_png -> vectorize_classification

    Args:
        selection: dict from `sentinel.select_satellite`.
        scene_metadata: scene dict from `sentinel.search_imagery`.
        bbox: analysis bbox (unused for clipping; kept for the result payload).
        merged_polygon: merged risk geometry from `boundary.merge_risk_boundaries`.
        event_id: namespaces all artifacts.
        token: CDSE access token.
        disaster_type: drives band selection, indices and styling.

    Returns a dict of local artifact paths and analysis stats, or None if any
    stage fails:
        {
            "satellite_type", "index_type", "water_percent", "mean_index",
            "affected_area_km2", "png_paths": {...}, "geojson": {...},
        }
    """
    satellite_type = selection.get("satellite_type", "sentinel-2")

    imagery = download_imagery(
        selection, scene_metadata, event_id, token, disaster_type
    )
    if imagery is None:
        logger.error("Aborting pipeline: download/extract failed")
        return None

    stacked = stack_bands(imagery["band_paths"], satellite_type)
    if stacked is None:
        logger.error("Aborting pipeline: band stacking failed")
        return None

    clipped = clip_to_polygon(stacked, merged_polygon)
    if clipped is None:
        logger.error("Aborting pipeline: polygon clip failed")
        return None

    indices = calculate_indices(clipped, satellite_type, disaster_type)
    if indices is None:
        logger.error("Aborting pipeline: index calculation failed")
        return None

    pngs = export_png(indices, clipped, event_id, disaster_type)
    if pngs is None:
        logger.error("Aborting pipeline: PNG export failed")
        return None

    geojson = vectorize_classification(
        indices["classification_array"],
        clipped["transform"],
        clipped["crs"],
        disaster_type,
    )

    logger.info("Satellite imagery pipeline complete for %s", event_id)
    return {
        "satellite_type": satellite_type,
        "index_type": indices["index_type"],
        "water_percent": indices["water_percent"],
        "mean_index": indices["mean_value"],
        "affected_area_km2": geojson["total_area"],
        "png_paths": pngs,
        "geojson": geojson,
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
