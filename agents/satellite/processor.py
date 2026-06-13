"""Image download and processing for the satellite agent.

Takes a scene chosen by `sentinel.search_imagery`, downloads the product from
the Copernicus Data Space Ecosystem (CDSE), clips it to the analysis bbox from
`boundary.get_analysis_bbox`, and exports an optimized PNG for display.

Pipeline (see `process_satellite_imagery`):
    download_imagery -> clip_to_bbox -> export_png

CDSE products are delivered as zipped `.SAFE` directories. Sentinel-2 holds the
optical bands as JP2 files; Sentinel-1 holds GeoTIFF measurements. We locate a
visualisable raster inside the archive, clip it to the bbox, and write a PNG.

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
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

logger = logging.getLogger(__name__)

# CDSE OData download endpoint. The product id from search_imagery is
# interpolated and the `$value` resource streams the zipped .SAFE archive.
DOWNLOAD_URL = (
    "https://catalogue.dataspace.copernicus.eu/odata/v1/Products({product_id})/"
    "$value"
)

# Where downloaded/clipped/exported files live. A dedicated subdirectory under
# the system temp dir keeps intermediate artifacts out of the repo.
TEMP_ROOT = os.path.join(tempfile.gettempdir(), "hazardmind-satellite")

# Raster file extensions we can open with rasterio, in preference order. JP2 is
# Sentinel-2 optical; TIFF is Sentinel-1 SAR.
_RASTER_EXTENSIONS = (".jp2", ".tif", ".tiff")

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

# Cap the exported PNG's longest side (pixels) to keep file size reasonable for
# web display. Larger rasters are downsampled on read.
_MAX_PNG_DIMENSION = 1024


def download_imagery(
    scene_metadata: dict,
    token: str,
    timeout: int = 600,
    max_retries: int = 4,
) -> Optional[str]:
    """Download a scene's product archive from CDSE to a local file.

    Args:
        scene_metadata: a scene dict from `sentinel.search_imagery` (must carry
            an `Id` and ideally a `Name`).
        token: a CDSE access token from `sentinel.authenticate_copernicus`.
        timeout: per-request timeout in seconds (products are large).
        max_retries: how many times to resume after a dropped connection.

    CDSE products are large (often hundreds of MB) and the stream can drop
    mid-transfer. The download is resumable: on a connection error we re-issue
    the request with an HTTP Range header and append from where we left off,
    rather than restarting from zero. Returns the path to the downloaded
    `.zip`, or None on failure.
    """
    if not scene_metadata:
        logger.error("No scene metadata provided to download_imagery")
        return None

    product_id = scene_metadata.get("Id")
    if not product_id:
        logger.error("Scene metadata has no 'Id'; cannot download")
        return None

    if not token:
        logger.error("No access token provided; cannot download imagery")
        return None

    name = scene_metadata.get("Name", product_id)
    os.makedirs(TEMP_ROOT, exist_ok=True)
    dest_path = os.path.join(TEMP_ROOT, f"{product_id}.zip")
    part_path = f"{dest_path}.part"

    url = DOWNLOAD_URL.format(product_id=product_id)
    auth_header = {"Authorization": f"Bearer {token}"}

    # Start fresh: a stale partial from a previous run could be from a different
    # scene or a server that doesn't honor Range, so don't trust it.
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

                        # Determine the expected total size once.
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

                        # If we asked for a Range but the server replied 200
                        # (full body), it doesn't support resume: rewrite.
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
                        "Downloaded scene to %s (%d bytes)",
                        dest_path,
                        final_size,
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


def _find_raster_in_archive(zip_path: str) -> Optional[str]:
    """Return a rasterio-openable URI for a band inside a downloaded zip.

    Picks the first member with a known raster extension. The returned URI uses
    rasterio's `zip://` scheme so the band is read without extracting the whole
    archive.
    """
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        logger.error("Could not open archive %s: %s", zip_path, exc)
        return None

    candidates = [
        m
        for m in members
        if m.lower().endswith(_RASTER_EXTENSIONS) and not m.endswith("/")
    ]
    if not candidates:
        logger.error("No raster bands found inside %s", zip_path)
        return None

    # Prefer true-colour previews when present, otherwise the first raster.
    preferred = next(
        (m for m in candidates if "TCI" in m.upper() or "PREVIEW" in m.upper()),
        candidates[0],
    )
    logger.info("Using band %s from archive", preferred)
    return f"zip://{zip_path}!/{preferred}"


def clip_to_bbox(image_path: str, bbox: tuple) -> Optional[str]:
    """Clip a downloaded scene to the analysis bbox.

    Args:
        image_path: path to the downloaded product `.zip` (from
            `download_imagery`) or a raster file directly.
        bbox: (minx, miny, maxx, maxy) in WGS84 lon/lat — the analysis bbox from
            `boundary.get_analysis_bbox`.

    Reads only the windowed region intersecting the bbox and writes a clipped
    GeoTIFF alongside the input. Returns the clipped raster path, or None on
    failure.
    """
    if not image_path or not os.path.exists(image_path):
        logger.error("Image path %r does not exist", image_path)
        return None

    try:
        minx, miny, maxx, maxy = bbox
    except (TypeError, ValueError) as exc:
        logger.error("Invalid bbox %r: %s", bbox, exc)
        return None

    if image_path.lower().endswith(".zip"):
        raster_uri = _find_raster_in_archive(image_path)
        if raster_uri is None:
            return None
        base = os.path.splitext(image_path)[0]
    else:
        raster_uri = image_path
        base = os.path.splitext(image_path)[0]

    clipped_path = f"{base}_clipped.tif"

    try:
        with rasterio.open(raster_uri) as src:
            # The bbox is in lon/lat; reproject it to the raster's CRS so the
            # read window lines up with the pixels.
            if src.crs is not None and src.crs.to_epsg() != 4326:
                dst_bounds = transform_bounds(
                    "EPSG:4326", src.crs, minx, miny, maxx, maxy
                )
            else:
                dst_bounds = (minx, miny, maxx, maxy)

            window = from_bounds(*dst_bounds, transform=src.transform)
            window = window.round_offsets().round_lengths()

            data = src.read(window=window)
            if data.size == 0:
                logger.error("Clip window does not overlap the scene")
                return None

            profile = src.profile.copy()
            profile.update(
                driver="GTiff",
                height=data.shape[1],
                width=data.shape[2],
                transform=src.window_transform(window),
            )

            with rasterio.open(clipped_path, "w", **profile) as dst:
                dst.write(data)
    except rasterio.errors.RasterioError as exc:
        logger.error("Failed to clip %s: %s", image_path, exc)
        return None

    logger.info("Clipped scene to %s", clipped_path)
    return clipped_path


def _to_uint8(band: np.ndarray) -> np.ndarray:
    """Scale a single band to 0-255 using a 2-98 percentile stretch."""
    valid = band[np.isfinite(band)]
    if valid.size == 0:
        return np.zeros_like(band, dtype=np.uint8)

    lo, hi = np.percentile(valid, (2, 98))
    if hi <= lo:
        hi = lo + 1.0

    stretched = np.clip((band.astype("float32") - lo) / (hi - lo), 0, 1)
    return (stretched * 255).astype(np.uint8)


def export_png(clipped_path: str, event_id: str) -> Optional[str]:
    """Convert a clipped raster to an optimized PNG for display.

    Writes to `<TEMP_ROOT>/<event_id>/satellite.png`. Multi-band rasters are
    rendered as RGB (first three bands); single-band rasters (e.g. SAR) are
    rendered as greyscale. The longest side is capped at `_MAX_PNG_DIMENSION`
    via decimated reads to keep the file small. Returns the PNG path, or None on
    failure.
    """
    if not clipped_path or not os.path.exists(clipped_path):
        logger.error("Clipped path %r does not exist", clipped_path)
        return None

    out_dir = os.path.join(TEMP_ROOT, str(event_id))
    os.makedirs(out_dir, exist_ok=True)
    png_path = os.path.join(out_dir, "satellite.png")

    try:
        with rasterio.open(clipped_path) as src:
            # Decimate on read so large scenes downsample to <= _MAX_PNG_DIMENSION.
            scale = max(src.height, src.width) / _MAX_PNG_DIMENSION
            if scale > 1:
                out_h = max(1, int(src.height / scale))
                out_w = max(1, int(src.width / scale))
            else:
                out_h, out_w = src.height, src.width

            band_count = min(src.count, 3)
            data = src.read(
                indexes=list(range(1, band_count + 1)),
                out_shape=(band_count, out_h, out_w),
            )

            channels = [_to_uint8(data[i]) for i in range(band_count)]
            if band_count >= 3:
                rgb = np.dstack(channels[:3])
            else:
                # Greyscale -> replicate to 3 channels for a standard PNG.
                rgb = np.dstack([channels[0]] * 3)

        from PIL import Image

        Image.fromarray(rgb, mode="RGB").save(
            png_path, format="PNG", optimize=True
        )
    except rasterio.errors.RasterioError as exc:
        logger.error("Failed to read clipped raster %s: %s", clipped_path, exc)
        return None
    except (OSError, ValueError) as exc:
        logger.error("Failed to export PNG to %s: %s", png_path, exc)
        return None

    logger.info("Exported PNG to %s", png_path)
    return png_path


def process_satellite_imagery(
    scene_metadata: dict,
    bbox: tuple,
    event_id: str,
    token: str,
) -> Optional[str]:
    """Run the full download -> clip -> export pipeline for a scene.

    Args:
        scene_metadata: scene dict from `sentinel.search_imagery`.
        bbox: analysis bbox (minx, miny, maxx, maxy) from
            `boundary.get_analysis_bbox`.
        event_id: identifier used to namespace the output PNG.
        token: CDSE access token from `sentinel.authenticate_copernicus`.

    Returns the final PNG path, or None if any stage fails.
    """
    image_path = download_imagery(scene_metadata, token)
    if image_path is None:
        logger.error("Aborting pipeline: download failed")
        return None

    clipped_path = clip_to_bbox(image_path, bbox)
    if clipped_path is None:
        logger.error("Aborting pipeline: clip failed")
        return None

    png_path = export_png(clipped_path, event_id)
    if png_path is None:
        logger.error("Aborting pipeline: PNG export failed")
        return None

    logger.info("Satellite imagery pipeline complete: %s", png_path)
    return png_path


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Live smoke test: authenticate, find a recent scene over a small Lahore
    # bbox, then run the full pipeline. Needs valid Copernicus credentials.
    from sentinel import (
        SENTINEL_2,
        authenticate_copernicus,
        search_imagery,
    )

    lahore_bbox = (74.2, 31.4, 74.5, 31.7)

    token = authenticate_copernicus()
    if not token:
        print("Authentication failed; skipping pipeline smoke test")
    else:
        scene = search_imagery(lahore_bbox, SENTINEL_2, date_range=30)
        if not scene:
            print("No scene found; skipping pipeline smoke test")
        else:
            print(f"Found scene: {scene.get('Name')}")
            png = process_satellite_imagery(
                scene, lahore_bbox, event_id="smoke-test", token=token
            )
            if png:
                print(f"Pipeline produced PNG: {png}")
            else:
                print("Pipeline failed")
