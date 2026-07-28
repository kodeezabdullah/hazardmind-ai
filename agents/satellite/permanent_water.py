"""Permanent-water masking from JRC Global Surface Water (Phase 1c).

Flood means water where water is NOT normally present. Rivers, lakes and
reservoirs are permanently there and were previously counted as flood on
every run. This module sources the JRC Global Surface Water *occurrence*
layer (Pekel et al. 2016, v1.4, 1984-2021, 30 m) and turns it into a
per-pixel "normally water" mask on the clip grid.

**Sourcing decision (measured, 2026-07-29):** the JRC tiles on the public
GCS bucket (`storage.googleapis.com/global-surface-water/downloads2021/
occurrence/`) are internally-tiled GeoTIFFs (256x256 blocks), so a
`/vsicurl/` windowed read fetches ONLY the AOI's blocks — measured ~10 s
cold / KBs-MBs per city-scale AOI — versus ~300 MB per 10-degree tile
(~1 TB global) for bulk download. Fetch-on-demand windowed reads with a
small on-disk cache win on both storage and latency; no Google Earth
Engine dependency (this project deliberately avoids GEE on the real-time
path).

**Occurrence threshold: 75 (percent of observed months with water,
1984-2021).** Recorded in every result as
`permanent_water_occurrence_threshold`. Why 75 and not 50: masking at 50
("water more often than not") would also mask strongly *seasonal* water —
and seasonally-flooded land is exactly where flood damage occurs and what
EMS reference products map as flood. 75 keeps the mask to water present
the large majority of the observed record (true rivers/lakes) and errs
toward NOT masking — for a life-safety system, under-masking (a river
counted as flood) is a precision cost; over-masking (a flooded seasonal
plain silently dropped) is a recall cost, and missing flood is worse.

Every fetch is best-effort: an unreachable bucket degrades to "no mask,
reason recorded" — it never fails or blocks a run.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import tempfile
from typing import Optional

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from rasterio.windows import from_bounds

logger = logging.getLogger(__name__)

JRC_OCCURRENCE_URL_TEMPLATE = (
    "/vsicurl/https://storage.googleapis.com/global-surface-water/"
    "downloads2021/occurrence/occurrence_{lon}_{lat}v1_4_2021.tif"
)
JRC_SOURCE_LABEL = "JRC_GSW_occurrence_v1_4_2021"

# Percent of observed months (1984-2021) a pixel held water for it to count
# as "normally water". See module docstring for the 75-vs-50 argument.
DEFAULT_OCCURRENCE_THRESHOLD = 75

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "hazardmind-permanent-water")

# JRC tiles are 10x10 degrees, named by their TOP-LEFT corner.
_TILE_DEG = 10


def _tile_name(lon_left: int, lat_top: int) -> tuple[str, str]:
    lon_txt = f"{abs(lon_left)}{'E' if lon_left >= 0 else 'W'}"
    lat_txt = f"{abs(lat_top)}{'N' if lat_top >= 0 else 'S'}"
    return lon_txt, lat_txt


def _tiles_for_bounds(west: float, south: float, east: float, north: float):
    """(lon_left, lat_top) of every 10-degree JRC tile touching the bounds."""
    lon0 = int(math.floor(west / _TILE_DEG) * _TILE_DEG)
    lon1 = int(math.floor((east - 1e-9) / _TILE_DEG) * _TILE_DEG)
    lat0 = int(math.ceil(north / _TILE_DEG) * _TILE_DEG)
    lat1 = int(math.ceil((south + 1e-9) / _TILE_DEG) * _TILE_DEG)
    tiles = []
    for lon in range(lon0, lon1 + 1, _TILE_DEG):
        for lat in range(lat1, lat0 + 1, _TILE_DEG):
            tiles.append((lon, lat))
    return tiles


def _cache_path(west: float, south: float, east: float, north: float) -> str:
    key = hashlib.sha256(
        f"{west:.5f},{south:.5f},{east:.5f},{north:.5f},{JRC_SOURCE_LABEL}".encode()
    ).hexdigest()[:20]
    return os.path.join(_CACHE_DIR, f"occurrence_{key}.tif")


def fetch_occurrence_window(
    west: float, south: float, east: float, north: float, timeout_attempts: int = 2
) -> Optional[dict]:
    """Fetch the JRC occurrence layer for a WGS84 bounds window.

    Returns ``{"array": uint8 2-D, "transform": Affine, "crs": CRS}`` in
    EPSG:4326, or ``None`` when the bucket is unreachable (best-effort —
    logged, never raised). Windows are cached on disk so repeat calls for
    the same AOI (tier retries, harness reruns) cost no network I/O.
    """
    cache = _cache_path(west, south, east, north)
    if os.path.exists(cache) and os.path.getsize(cache) > 0:
        try:
            with rasterio.open(cache) as src:
                return {
                    "array": src.read(1),
                    "transform": src.transform,
                    "crs": src.crs,
                }
        except rasterio.errors.RasterioError:
            logger.warning("Corrupt permanent-water cache %s; refetching", cache)
            try:
                os.remove(cache)
            except OSError:
                pass

    tiles = _tiles_for_bounds(west, south, east, north)
    parts = []
    for lon_left, lat_top in tiles:
        lon_txt, lat_txt = _tile_name(lon_left, lat_top)
        url = JRC_OCCURRENCE_URL_TEMPLATE.format(lon=lon_txt, lat=lat_txt)
        last_exc: Optional[Exception] = None
        for _attempt in range(timeout_attempts):
            try:
                with rasterio.open(url) as src:
                    win = from_bounds(
                        max(west, lon_left),
                        max(south, lat_top - _TILE_DEG),
                        min(east, lon_left + _TILE_DEG),
                        min(north, lat_top),
                        src.transform,
                    )
                    arr = src.read(1, window=win)
                    transform = src.window_transform(win)
                    parts.append({"array": arr, "transform": transform, "crs": src.crs})
                last_exc = None
                break
            except (rasterio.errors.RasterioError, OSError, ValueError) as exc:
                last_exc = exc
        if last_exc is not None:
            logger.warning(
                "JRC occurrence tile %s/%s unreachable (%s) — permanent-water "
                "mask unavailable for this run",
                lon_txt, lat_txt, last_exc,
            )
            return None
    if not parts:
        return None

    if len(parts) == 1:
        result = parts[0]
    else:
        # Merge the per-tile windows onto one grid (all EPSG:4326, same res).
        from rasterio.io import MemoryFile
        from rasterio.merge import merge as rio_merge

        datasets = []
        memfiles = []
        try:
            for p in parts:
                mem = MemoryFile()
                memfiles.append(mem)
                with mem.open(
                    driver="GTiff",
                    height=p["array"].shape[0],
                    width=p["array"].shape[1],
                    count=1,
                    dtype=str(p["array"].dtype),
                    crs=p["crs"],
                    transform=p["transform"],
                ) as ds:
                    ds.write(p["array"], 1)
                datasets.append(mem.open())
            merged, merged_transform = rio_merge(datasets)
            result = {
                "array": merged[0],
                "transform": merged_transform,
                "crs": datasets[0].crs,
            }
        finally:
            for ds in datasets:
                ds.close()
            for mem in memfiles:
                mem.close()

    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with rasterio.open(
            cache, "w",
            driver="GTiff",
            height=result["array"].shape[0],
            width=result["array"].shape[1],
            count=1,
            dtype=str(result["array"].dtype),
            crs=result["crs"],
            transform=result["transform"],
            compress="deflate",
        ) as dst:
            dst.write(result["array"], 1)
    except (rasterio.errors.RasterioError, OSError) as exc:
        logger.warning("Could not cache permanent-water window (%s)", exc)

    return result


def permanent_water_mask_for_clip(
    clip_shape: tuple[int, int],
    clip_transform,
    clip_crs,
    occurrence_threshold: int = DEFAULT_OCCURRENCE_THRESHOLD,
) -> Optional[np.ndarray]:
    """Boolean mask (True = normally water) on the clip grid, or None.

    Thresholds the occurrence layer in its native 30 m EPSG:4326 grid, then
    reprojects the binary mask to the clip grid with nearest resampling (a
    thresholded mask is categorical — bilinear would invent fractional
    membership at edges).
    """
    if clip_transform is None or clip_crs is None:
        return None
    try:
        from rasterio.warp import transform_bounds

        h, w = clip_shape
        left, bottom, right, top = rasterio.transform.array_bounds(h, w, clip_transform)
        west, south, east, north = transform_bounds(
            clip_crs, "EPSG:4326", left, bottom, right, top
        )
    except (rasterio.errors.RasterioError, ValueError) as exc:
        logger.warning("Permanent-water mask: could not derive bounds (%s)", exc)
        return None

    # Small margin so edge pixels reproject cleanly.
    pad = 0.005
    window = fetch_occurrence_window(west - pad, south - pad, east + pad, north + pad)
    if window is None:
        return None

    binary = (window["array"] >= occurrence_threshold).astype("uint8")
    dest = np.zeros(clip_shape, dtype="uint8")
    try:
        reproject(
            source=binary,
            destination=dest,
            src_transform=window["transform"],
            src_crs=window["crs"],
            dst_transform=clip_transform,
            dst_crs=clip_crs,
            resampling=Resampling.nearest,
        )
    except (rasterio.errors.RasterioError, ValueError) as exc:
        logger.warning("Permanent-water mask reprojection failed (%s)", exc)
        return None
    return dest.astype(bool)


def permanent_water_geojson(
    west: float, south: float, east: float, north: float,
    occurrence_threshold: int = DEFAULT_OCCURRENCE_THRESHOLD,
) -> Optional[dict]:
    """Vectorized permanent-water geometry (WGS84 GeoJSON) for a bounds box.

    Used by the validation harness to compute the excluding-permanent-water
    metric split from the SAME source/threshold the pipeline masks with —
    the two must never diverge, or the split stops meaning anything.
    """
    window = fetch_occurrence_window(west, south, east, north)
    if window is None:
        return None
    binary = (window["array"] >= occurrence_threshold).astype("uint8")
    if not binary.any():
        return {"type": "MultiPolygon", "coordinates": []}
    try:
        from rasterio import features as rio_features
        from shapely.geometry import shape as shp_shape, mapping
        from shapely.ops import unary_union

        shapes = [
            shp_shape(geom)
            for geom, val in rio_features.shapes(
                binary, mask=binary.astype(bool), transform=window["transform"]
            )
            if val == 1
        ]
        if not shapes:
            return {"type": "MultiPolygon", "coordinates": []}
        return mapping(unary_union(shapes))
    except (ValueError, ImportError) as exc:
        logger.warning("Permanent-water vectorization failed (%s)", exc)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Live smoke test over the Kanalia AOI (network required).
    win = fetch_occurrence_window(22.83, 39.44, 22.95, 39.56)
    if win:
        arr = win["array"]
        print(f"window {arr.shape}, >=75: {(arr >= 75).mean():.4f}, "
              f">=50: {(arr >= 50).mean():.4f}")
    else:
        print("JRC unreachable")
