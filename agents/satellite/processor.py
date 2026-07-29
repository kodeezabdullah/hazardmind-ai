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
from datetime import datetime, timezone
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

# After clipping, a result with fewer than this percentage of valid (non-nodata)
# pixels inside the risk polygon is rejected and the next scene is tried
# (FIX 3).
MIN_VALID_PIXEL_PERCENT = 5.0

# BUG 4c: if this many consecutive downloads within a tier fail to clip or add
# no coverage, abort the tier rather than working through every candidate.
DOOMED_DOWNLOAD_LIMIT = 3

# Per-tier date window (days) for log/anomaly text; mirrors
# sentinel.COVERAGE_TIERS_S2/COVERAGE_TIERS_S1 (2026-07-28, CHANGE 5 —
# per-satellite tier windows). Union of both satellites' windows so a log line
# always has a value regardless of which one produced the tier number.
COVERAGE_TIERS_DAYS = {1: 0, 2: 3, 3: 7, 4: 14}

# --------------------------------------------------------------------------- #
# 2026-07-28 — coverage tolerance (fix/coverage-tolerance)
#
# The prior rule demanded EXACTLY 100.0% interior-AOI valid-pixel coverage or
# the whole call failed with `insufficient_coverage`. That rule was written to
# stop the pipeline from silently reporting a partial AOI as a complete
# analysis — a real goal. But it enforced that goal by refusing to answer
# instead of answering honestly with the limitation stated, and cloud gaps
# cannot be downloaded away: if the sky was covered that week, no amount of
# additional scenes closes the gap, so the search could never terminate
# successfully in exactly the weather conditions where flood analysis matters
# most (a real 2.4x2.7 km AOI turned into a 6-hour, 4-scene search on this
# rule). Coverage is now a caller-controlled quality band instead of a single
# cliff — see `process_satellite_imagery`'s docstring and CLAUDE.md's
# "Coverage tolerance" section for the full writeup.
# --------------------------------------------------------------------------- #

# Caller's default coverage target when none is supplied. Coverage at or above
# this is "complete" with only a proportional confidence penalty.
DEFAULT_MIN_COVERAGE_PERCENT = 90.0

# Hard floor — never caller-adjustable below this. Below the floor the AOI is
# too poorly sampled to mean anything, so the call still hard-fails with
# status "insufficient_coverage" (unchanged shape from the old 100%-only rule,
# just now driven by this constant instead of 100.0).
COVERAGE_FLOOR = 80.0

# Clamp ceiling — a caller-supplied target above this is just 100 (asking for
# more than 100% coverage is meaningless).
COVERAGE_CEILING = 100.0

# Confidence-penalty scale for the shortfall between 100% and the achieved
# coverage, in the >= min_coverage_percent band (proportional, not a cliff):
# a linear penalty of `(100 - coverage) * COVERAGE_PENALTY_SCALE`, added to
# the tracker as a concern whose severity scales with the shortfall. E.g. at
# scale 0.01, a 97% run gets a 0.03 confidence knock; a 100% run gets none.
COVERAGE_PENALTY_SCALE = 0.01

# Default whole-search download budget (CHANGE 2), shared by
# `process_satellite_imagery`'s own default and by the CHANGE 6 selection
# peek's budget check in agent.py, so both read one number rather than two
# independently-hardcoded 4.0 literals.
DEFAULT_MAX_DOWNLOAD_GB = 4.0

# Minimum percentage-point gain a newly accepted scene must add over the
# marginal-return threshold to be worth continuing the search (CHANGE 4). This
# is DISTINCT from the near-zero (0.01) doomed-streak duplicate-detection
# check below — that one detects a raw non-contributing download; this one
# detects a technically-contributing but not-worth-its-cost download and stops
# the search entirely (not just this candidate).
MIN_MARGINAL_COVERAGE_GAIN = 2.0

# Scene-age confidence penalty (islamabad-findings #4). The tiered temporal
# search bounds coherence WITHIN a mosaic (BUG 3) but nothing previously
# bounded how old the accepted imagery is relative to the event itself — a
# live run accepted a 14-day-old scene with no signal downstream that the
# imagery predates the event by two weeks. For flood analysis specifically,
# imagery this old describes history, not current conditions. This is
# deliberately NOT a hard cutoff (old imagery is still better than none, and
# historical analysis is a planned feature) — age only reduces confidence and
# is always reported, via SCENE_AGE_ANOMALY_DAYS below.
#
# Basis for the 7-day cutoff: it matches the widest "same tier" S2 window
# (COVERAGE_TIERS_S2's ±7d tier) and is well inside a flood's typical
# multi-day-to-multi-week active/recession window, so imagery older than this
# is crossing from "describes the event" into "describes what came before or
# after it" territory for the specific disaster type this pipeline targets.
SCENE_AGE_ANOMALY_DAYS = 7
# Confidence penalty per day beyond SCENE_AGE_ANOMALY_DAYS (linear, not a
# cliff) — a 14-day-old scene (7 days past the threshold) knocks off 7 * 0.02
# = 0.14 evidence-equivalent, comparable in magnitude to the coverage-shortfall
# penalty scale above.
SCENE_AGE_PENALTY_PER_DAY = 0.02


def _clamp_min_coverage_percent(value: Optional[float]) -> float:
    """Clamp a caller-supplied coverage target into [COVERAGE_FLOOR, COVERAGE_CEILING].

    A caller cannot set the target below 80 (non-negotiable — below that the
    AOI is too poorly sampled to mean anything) or above 100 (meaningless).
    ``None``/unparseable falls back to `DEFAULT_MIN_COVERAGE_PERCENT`.
    """
    if value is None:
        value = DEFAULT_MIN_COVERAGE_PERCENT
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = DEFAULT_MIN_COVERAGE_PERCENT
    return max(COVERAGE_FLOOR, min(COVERAGE_CEILING, value))


def _coverage_band(interior_pct: float, min_coverage_percent: float) -> str:
    """Classify achieved coverage into one of three bands (CHANGE 1).

    - "complete": >= min_coverage_percent — full pass, small proportional
      confidence penalty for any shortfall from 100.
    - "below_target": >= COVERAGE_FLOOR and < min_coverage_percent — still
      "complete" status, but flagged and penalised harder.
    - "insufficient": < COVERAGE_FLOOR — hard stop, unchanged shape from the
      old 100%-only rule.
    """
    if interior_pct >= min_coverage_percent:
        return "complete"
    if interior_pct >= COVERAGE_FLOOR:
        return "below_target"
    return "insufficient"

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


# CHANGE 6: whether the most recent _download_bands_via_nodes call reused an
# SCL band already on disk (a prior selection peek) rather than downloading
# it. Set at the exact HIT/MISS log point, read by the caller (download_imagery)
# immediately after the call — process_satellite_imagery's tier/scene loop is
# sequential (no concurrent scene downloads within one event), so there is no
# cross-scene race on this flag, same as `_BYTES_DOWNLOADED` above. `None`
# means the most recent call never checked SCL at all (SCL wasn't in
# band_tokens, or the call short-circuited before the check).
_LAST_SCL_REUSED: Optional[bool] = None


def _set_scl_reused(value: Optional[bool]) -> None:
    global _LAST_SCL_REUSED
    _LAST_SCL_REUSED = value


def _last_scl_reused() -> Optional[bool]:
    """Whether the most recent _download_bands_via_nodes call reused a
    peeked SCL band (True), downloaded it fresh (False), or never checked
    SCL at all (None — SCL wasn't requested for this scene/satellite)."""
    return _LAST_SCL_REUSED


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

# Sentinel-1 polarizations actually downloaded. calculate_indices' SAR path
# (below) only ever reads VV — VH was downloaded but never consumed by any
# index/threshold/classification code. Restricted to VV-only so the
# per-band Nodes path (once wired below) fetches one ~100-300 MB GeoTIFF
# per scene instead of two, and the whole-archive fallback path extracts
# only the VV member instead of both.
_S1_POLARIZATIONS = ["VV"]

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


def _resolve_token(token) -> Optional[str]:
    """Resolve `token` to a fresh access-token string.

    `token` is either a plain string (legacy call sites / tests: used as-is,
    no refresh possible) or a `sentinel.TokenManager` (has `.get()`, which
    proactively refreshes before the ~10 min CDSE access-token expiry). A
    multi-tile mosaic download can run tens of minutes, so pulling a fresh
    token per file here — rather than reusing one string captured once at
    pipeline start — is what actually prevents the mid-run 401s.
    """
    if hasattr(token, "get") and callable(getattr(token, "get")):
        return token.get()
    return token


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
    fresh_token = _resolve_token(token)
    if not fresh_token:
        logger.error("No valid CDSE access token available; cannot download %s", name)
        return None
    auth_header = {"Authorization": f"Bearer {fresh_token}"}

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


def _resolve_s1_band_nodes(
    session: requests.Session,
    product_id: str,
    band_tokens: list,
    headers: dict,
    timeout: tuple,
) -> dict:
    """Map each Sentinel-1 GRD polarization token to its measurement GeoTIFF
    node path segments.

    Walks SAFE -> measurement and matches each requested token (VV, VH) to
    the file whose name embeds the polarization (e.g.
    `s1a-iw-grd-vv-...tiff`) — same matching convention `_match_band_members`
    already uses for the whole-zip fallback extractor, just applied to a
    Nodes listing instead of a zip member list.

    Returns {token: [seg, seg, ...]} for the polarizations located. Raises on
    a traversal/HTTP error so the caller falls back to the whole-archive
    download, same contract as `_resolve_s2_band_nodes`.
    """
    safe_children = _list_nodes(session, product_id, [], headers, timeout)
    safe_dir = next((n for n in safe_children if n.endswith(".SAFE")), None)
    if not safe_dir:
        raise ValueError("no .SAFE root node in product")

    measurement_base = [safe_dir, "measurement"]
    files = _list_nodes(session, product_id, measurement_base, headers, timeout)
    if not files:
        raise ValueError("no measurement node in product")

    resolved: dict = {}
    for token in band_tokens:
        lower = token.lower()
        match = next(
            (f for f in files
             if f.lower().endswith((".tiff", ".tif"))
             and f"-{lower}-" in f.lower()),
            None,
        )
        if not match:
            logger.warning("Polarization %s not found in measurement listing", token)
            continue
        resolved[token] = measurement_base + [match]
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

    Instead of the whole ~868 MB (S2) / ~1.1-1.7 GB (S1 GRD) .SAFE archive,
    fetch each requested band/polarization individually straight into
    `<temp>/<event_id>/bands/`. CDSE doesn't honour Range, but per-band
    download means a connection drop only restarts the one band in flight, not
    the whole archive — and any band already fully on disk is reused. The ~7-min
    outage budget is SHARED across all bands of the scene (a sustained outage
    aborts the scene, not each band independently). Returns {token: path} for the
    bands fetched, or None on traversal failure (caller falls back to the whole
    archive).

    Sentinel-2 bands are JP2 (IMG_DATA tree); Sentinel-1 GRD polarizations are
    GeoTIFF (measurement/ tree) — `_resolve_s2_band_nodes`/`_resolve_s1_band_nodes`
    both return {token: [node segments]}, differing only in which tree they
    walk and which raster format they name, so the rest of this function is
    satellite-agnostic.
    """
    _set_scl_reused(None)  # reset per call; only set when SCL is actually checked

    product_id = scene_metadata.get("Id")
    if not product_id:
        return None
    if satellite_type not in ("sentinel-1", "sentinel-2"):
        return None

    ext = ".tiff" if satellite_type == "sentinel-1" else ".jp2"
    bands_dir = os.path.join(TEMP_ROOT, str(event_id), "bands")
    os.makedirs(bands_dir, exist_ok=True)
    fresh_token = _resolve_token(token)
    if not fresh_token:
        logger.error("No valid CDSE access token available for %s", event_id)
        return None
    auth_header = {"Authorization": f"Bearer {fresh_token}"}
    timeout = (DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)

    # Fast path: if every requested band is already fully on disk, return them
    # without ANY network I/O. This skips the Nodes tree-walk entirely (4 HTTP
    # listings that, on a flaky link, can each hang up to the read timeout
    # before raising), so a fully-cached scene resumes instantly across a
    # restart instead of stalling on the catalogue.
    cached = {}
    for tok in band_tokens:
        cand = os.path.join(bands_dir, f"{tok}{ext}")
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            cached[tok] = cand
    if len(cached) == len(band_tokens):
        for tok, path in cached.items():
            logger.info("Reusing cached band %s (%d bytes)", tok, os.path.getsize(path))
            if tok.upper() == "SCL":
                logger.info(
                    "SCL cache HIT — reusing peeked band, skipping download"
                )
                _set_scl_reused(True)
        return cached

    try:
        with _CDSESession() as session:
            resolver = (
                _resolve_s1_band_nodes
                if satellite_type == "sentinel-1"
                else _resolve_s2_band_nodes
            )
            node_map = resolver(
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
            #
            # Re-resolve the token before EACH band (not just once at the top
            # of this function): several bands at ~30-120 MB each can add up to
            # many minutes, comfortably past the ~10 min CDSE access-token
            # lifetime, and a stale Bearer header here is exactly what produced
            # the mid-run 401s this fix addresses.
            band_paths: dict = {}
            for tok, segments in node_map.items():
                band_token = _resolve_token(token)
                if not band_token:
                    logger.error(
                        "No valid CDSE access token available for band %s", tok
                    )
                    return None
                band_auth_header = {"Authorization": f"Bearer {band_token}"}
                out_path = os.path.join(bands_dir, f"{tok}{ext}")

                # SCL-specific cache visibility (CHANGE 6 reuse path): this is
                # the exact same on-disk check _stream_to_file_with_retry is
                # about to make internally, done here first ONLY to log
                # whether a prior CHANGE 6 selection peek's SCL download is
                # about to be reused. Every other band token skips this log —
                # only SCL was ever pre-fetched ahead of the real download.
                if tok.upper() == "SCL":
                    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                        logger.info(
                            "SCL cache HIT — reusing peeked band, skipping download"
                        )
                        _set_scl_reused(True)
                    else:
                        logger.info(
                            "SCL cache MISS — downloading SCL (peek reuse did not apply)"
                        )
                        _set_scl_reused(False)

                url = _node_url(product_id, segments)
                result = _stream_to_file_with_retry(
                    session,
                    url,
                    band_auth_header,
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


# --------------------------------------------------------------------------- #
# CHANGE 6 (2026-07-28): AOI-restricted cloud peek for S2 selection.
#
# `sentinel.select_satellite` only ever sees the scene's whole-tile cloud
# percentage (from catalogue metadata) — a scene can be 45% cloudy across its
# full footprint and clear over a small AOI, or the reverse. A real
# AOI-restricted figure needs the scene's SCL band, i.e. a download, which
# `sentinel.py` cannot do (it has no download capability and processor.py
# already imports FROM sentinel.py, so the reverse import would cycle). This
# function lives here instead and is called from agent.py, between catalogue
# search and the main tiered download, only when the scene-level figure is
# genuinely ambiguous (see PEEK_CLEAR_BELOW / PEEK_CLOUDY_ABOVE below).
# --------------------------------------------------------------------------- #

# Cut points for when a peek is worth its cost (one small ~10-20 MB SCL
# download vs. staying with the free scene-level catalogue figure):
#   - Below this, the scene-level reading is already comfortably under
#     CLOUD_COVER_THRESHOLD (30%, sentinel.py) with enough margin that even a
#     meaningfully worse AOI-local reading is very unlikely to flip the
#     decision — spend nothing.
#   - Above this, the scene is cloudy enough tile-wide that an AOI clear
#     enough to flip the decision back to S2 would be a large, unusual
#     divergence — still possible (that's the whole premise of this fix), but
#     rare enough that we default to the safe (weather-independent) SAR path
#     rather than paying for a peek on every heavily overcast scene.
# Between the two, the scene-level number alone cannot be trusted either way
# and the AOI figure genuinely might change the outcome — that is exactly the
# 45.9%-scene / clear-AOI live incident this fix exists for, so it is peeked.
PEEK_CLEAR_BELOW = 15.0
PEEK_CLOUDY_ABOVE = 50.0


def peek_needed(scene_cloud_percent: Optional[float]) -> bool:
    """Whether `scene_cloud_percent` is ambiguous enough to warrant a peek.

    See PEEK_CLEAR_BELOW/PEEK_CLOUDY_ABOVE above for the basis of the two cut
    points. `None` (no scene-level figure at all) is never peek-worthy — there
    is nothing to disambiguate against, and the caller falls back to the user
    hint as before.
    """
    if scene_cloud_percent is None:
        return False
    return PEEK_CLEAR_BELOW <= scene_cloud_percent <= PEEK_CLOUDY_ABOVE


def peek_aoi_cloud_percent(
    scene_metadata: dict,
    merged_polygon: dict,
    event_id: str,
    token,
    remaining_download_gb: Optional[float] = None,
) -> dict:
    """Download only the SCL band for one S2 candidate and measure AOI cloud.

    SCL is a 20 m (L2A) class layer, a small fraction of a full scene (the
    other bands in a flood/earthquake/landslide request are 10 m spectral
    bands several times the pixel count) — this reuses the existing per-band
    Nodes download path (`_download_bands_via_nodes`), so a fully-cached SCL
    from a prior peek (or from processing itself) is reused, not re-fetched,
    and any peek download is counted by the SAME `_add_bytes_downloaded`
    global the rest of the pipeline's budget accounting reads.

    Downloads straight into the SAME per-scene bands directory processing
    would use (`download_imagery`'s `scene_event` layout), so if this
    candidate is later accepted for real processing, `_download_bands_via_nodes`'s
    already-on-disk fast path reuses this SCL file — it is never fetched
    twice.

    Returns:
        {
            "aoi_cloud_percent": float | None,   # None on any failure
            "reason": str,   # "" on success, else why the peek didn't produce a figure
        }
    Never raises — a failed peek is always recoverable by the caller falling
    back to the scene-level figure (this is an optimisation, not a
    requirement).
    """
    if remaining_download_gb is not None and remaining_download_gb <= 0:
        logger.info(
            "Skipping AOI cloud peek for %s: byte budget already exhausted",
            scene_metadata.get("Name"),
        )
        return {"aoi_cloud_percent": None, "reason": "budget_exhausted"}

    product_id = scene_metadata.get("Id")
    if not product_id:
        return {"aoi_cloud_percent": None, "reason": "no_product_id"}

    # Namespace the peek's SCL under the SAME bands dir `download_imagery`
    # uses when this candidate is later downloaded ALONE (its dominant case —
    # selection peeks the single best S2 candidate before any mosaic decision
    # exists): `download_imagery` only switches to a per-scene
    # `scene_<Id>` subdir once `len(scenes) > 1` (a real mosaic). Using the
    # plain `event_id` dir here means `_download_bands_via_nodes`'s
    # already-on-disk fast path finds and reuses this exact SCL file for the
    # common single-scene accept. If this candidate instead ends up folded
    # into a multi-scene mosaic, the real download re-keys under
    # `scene_<Id>` and re-fetches SCL — a correct cache MISS in that rarer
    # case, not a bug (the peek still saved the selection decision).
    scene_event = event_id

    try:
        band_paths = _download_bands_via_nodes(
            scene_metadata, token, scene_event, ["SCL"], "sentinel-2"
        )
    except Exception as exc:  # pragma: no cover - defensive, peek must never crash the run
        logger.warning("AOI cloud peek download failed for %s: %s",
                        scene_metadata.get("Name"), exc)
        return {"aoi_cloud_percent": None, "reason": "scl_download_failed"}

    if not band_paths or "SCL" not in band_paths:
        logger.info(
            "AOI cloud peek: SCL unavailable for %s (L1C-only date, or "
            "traversal failure)",
            scene_metadata.get("Name"),
        )
        return {"aoi_cloud_percent": None, "reason": "scl_download_failed"}

    try:
        stacked = stack_bands(band_paths, "sentinel-2")
        if stacked is None:
            return {"aoi_cloud_percent": None, "reason": "scl_stack_failed"}

        clipped = clip_to_polygon(stacked, merged_polygon)
        if clipped is None:
            return {"aoi_cloud_percent": None, "reason": "scl_clip_failed"}

        scl = clipped.get("bands", {}).get("SCL")
        mask = clipped.get("mask")
        if scl is None or mask is None:
            return {"aoi_cloud_percent": None, "reason": "scl_missing_after_clip"}

        # Interior AOI, same convention as compute_coverage: erode the clip
        # mask by one pixel so rasterized-boundary artifacts never count.
        interior = _erode_mask(mask, 1)
        if interior is None or not interior.any():
            interior = mask

        int_count = int(np.count_nonzero(interior))
        if int_count == 0:
            return {"aoi_cloud_percent": None, "reason": "empty_interior_aoi"}

        scl_int = np.rint(np.nan_to_num(scl, nan=0.0)).astype("int16")
        invalid = np.isin(scl_int, list(_SCL_INVALID_CLASSES)) & interior
        aoi_cloud_pct = round(100.0 * int(np.count_nonzero(invalid)) / int_count, 2)

        logger.info(
            "AOI cloud peek for %s: %.2f%% invalid over the interior AOI "
            "(%d px)",
            scene_metadata.get("Name"), aoi_cloud_pct, int_count,
        )
        return {"aoi_cloud_percent": aoi_cloud_pct, "reason": ""}
    except (rasterio.errors.RasterioError, ValueError, MemoryError) as exc:
        logger.warning("AOI cloud peek measurement failed for %s: %s",
                        scene_metadata.get("Name"), exc)
        return {"aoi_cloud_percent": None, "reason": "scl_measurement_failed"}


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
    scl_reused = None  # CHANGE 6: whether an S2 scene's SCL reused a peek
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
        reused = _last_scl_reused()
        if reused is not None:
            # Last write wins across scenes in a mosaic — fine for logging/
            # reporting purposes (this is an observability signal, not a
            # correctness input); a mosaic's first scene is also the one
            # selection would have peeked, so it's the meaningful one anyway.
            scl_reused = reused

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

    return {
        "satellite_type": satellite_type,
        "band_paths": band_paths,
        "scl_reused": scl_reused,
    }


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
    from rasterio.warp import calculate_default_transform, reproject

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

        # BUG 1 follow-up (2026-07-29, found by the S1 validation trace): the
        # previous implementation wrapped `src` in a WarpedVRT and relied on
        # GDAL picking up the source's GCPs implicitly. On REAL S1 GRD
        # measurement files (COG-structured TIFFs from CDSE) that implicit
        # pickup silently fails: the VRT reports a correct crs/transform/shape
        # but reads ALL-ZERO pixel data everywhere — no error, no warning.
        # (Synthetic plain-GTiff GCP fixtures warp fine, which is why
        # test_bug1_gcp_raster_resolved never caught it; the same scene's
        # raw pixel reads are fine, and `reproject(gcps=...)` on the same
        # file produces real data — isolated live on event trace-s1-islamabad,
        # scene S1D_IW_GRDH_1SDV_20260725...207C_COG.)
        # Fix: warp EXPLICITLY via rasterio.warp.reproject with `gcps=` into
        # an on-disk temp raster next to the source, and return that dataset.
        # The downstream contract is unchanged — callers get a dataset with a
        # real affine + CRS, same as the VRT path claimed to provide.
        warped_path = f"{path}.warped-{str(dst_crs).replace(':', '')}.tif"
        try:
            if not (os.path.exists(warped_path) and os.path.getsize(warped_path) > 0):
                transform, width, height = calculate_default_transform(
                    src_crs, dst_crs, src.width, src.height, gcps=gcps,
                )
                profile = {
                    "driver": "GTiff",
                    "height": height,
                    "width": width,
                    "count": src.count,
                    "dtype": src.dtypes[0],
                    "crs": dst_crs,
                    "transform": transform,
                    "nodata": src.nodata if src.nodata is not None else 0,
                    "tiled": True,
                    "compress": "deflate",
                    "BIGTIFF": "IF_SAFER",
                }
                with rasterio.open(warped_path, "w", **profile) as dst:
                    for b in range(1, src.count + 1):
                        reproject(
                            source=rasterio.band(src, b),
                            destination=rasterio.band(dst, b),
                            gcps=gcps,
                            src_crs=src_crs,
                            dst_transform=transform,
                            dst_crs=dst_crs,
                            resampling=Resampling.bilinear,
                        )
                logger.info(
                    "Resolved GCP georeferencing for %s -> %s (%dx%d) via "
                    "explicit-GCP reproject",
                    os.path.basename(path), dst_crs, width, height,
                )
            else:
                logger.info(
                    "Reusing cached explicit-GCP warp for %s",
                    os.path.basename(path),
                )
            src.close()
            warped = rasterio.open(warped_path)

            # Empty-warp guard: the defect this fix replaces FAILED SILENTLY
            # (valid metadata, all-zero pixels), so verify the warp actually
            # carries data before handing it downstream. A cheap decimated
            # read is enough — a genuinely all-zero warped scene means the
            # warp path is broken again, not that the ground is featureless
            # (raw GRD DNs over any real terrain are nonzero).
            probe = warped.read(1, out_shape=(min(256, warped.height),
                                              min(256, warped.width)))
            if not (probe != 0).any():
                warped.close()
                raise ValueError(
                    f"explicit-GCP warp of {os.path.basename(path)} produced "
                    "all-zero data — refusing to hand an empty raster downstream"
                )
            return warped, None
        except (rasterio.errors.RasterioError, ValueError) as exc:
            logger.error(
                "Failed to warp GCP-georeferenced raster %s: %s", path, exc
            )
            if not src.closed:
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
                # Phase 1a: SCL is a CATEGORICAL class layer — bilinear
                # interpolation of class ids invents non-existent classes at
                # every boundary (e.g. midway between cloud-shadow 3 and
                # vegetation 4 reads as 3.5). Nearest keeps every resampled
                # pixel a real ESA class id; continuous spectral bands stay
                # bilinear as before.
                resampling = (
                    Resampling.nearest
                    if token.upper() == "SCL"
                    else Resampling.bilinear
                )
                arr = src_ds.read(
                    1,
                    out_shape=(ref_h, ref_w),
                    resampling=resampling,
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
        # Phase 1b (science/full-pass): MNDWI (Xu 2006) replaces McFeeters
        # NDWI as the primary flood index. (B03-B11)/(B03+B11) — B11 (SWIR)
        # was chosen by Xu specifically because built-up surfaces, NDWI's
        # documented false-positive class, reflect strongly in SWIR and are
        # suppressed in the ratio, while water absorbs SWIR even more
        # strongly than NIR. B11 is already in the flood band set (no new
        # fetch) and is resampled 20m->10m bilinearly in stack_bands —
        # appropriate for a continuous radiometric band feeding a continuous
        # ratio (nearest would tile 2x2 blocks of identical reflectance and
        # put blocky artifacts on every water edge; SCL, the categorical
        # layer, is the one that gets nearest).
        # Threshold: HELD at the NDWI-era constant this phase so the
        # measured delta is the index formula alone (Phase 2 replaces the
        # fixed threshold adaptively) — the measured number is therefore a
        # lower bound on MNDWI's value, stated in SCIENCE_LOG.md.
        # Fallback: if B11 is genuinely absent (non-flood band set reuse,
        # legacy cache), compute NDWI and LABEL IT NDWI — the label always
        # follows the formula actually used, never the intended one.
        b03, b08, b11 = bands.get("B03"), bands.get("B08"), bands.get("B11")
        if b03 is None or (b11 is None and b08 is None):
            logger.error("Flood index needs B03 plus B11 (MNDWI) or B08 (NDWI fallback)")
            return None
        if b11 is not None:
            index = _safe_ratio(b03 - b11, b03 + b11)
            index_type = "MNDWI"
            index_units = "MNDWI_ratio"
        else:
            logger.warning(
                "B11 missing — falling back to NDWI (B03/B08); built-up "
                "false-positive suppression unavailable on this run"
            )
            index = _safe_ratio(b03 - b08, b03 + b08)
            index_type = "NDWI"
            index_units = "NDWI_ratio"
        scheme_key = "NDWI"
        threshold = NDWI_WATER_THRESHOLD
        index_calibrated = True
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
    # Phase 1a (science/full-pass): per-pixel SCL masking INSIDE the index.
    # SCL was used for the coverage metric but not for the index itself —
    # cloud shadow (class 3) is spectrally almost identical to water in any
    # water index and is the largest false-positive source after built-up
    # surfaces. Masked pixels (same _SCL_INVALID_CLASSES set coverage uses:
    # 0 nodata, 1 saturated, 3 cloud shadow, 8/9 cloud, 10 cirrus, 11 snow)
    # are excluded from the index, the classification, mean_value,
    # affected_mean_index, water_percent and affected_area_km2 — they become
    # NODATA_CLASS, never silently "safe land" or zero.
    scl_masked_percent = None
    cloud_invalid = _scl_cloud_mask(clipped) if satellite_type == "sentinel-2" else None
    if cloud_invalid is not None:
        in_aoi = mask if mask is not None else np.isfinite(index)
        aoi_count = int(np.count_nonzero(in_aoi))
        masked_count = int(np.count_nonzero(cloud_invalid & in_aoi & valid))
        scl_masked_percent = (
            round(100.0 * masked_count / aoi_count, 2) if aoi_count else 0.0
        )
        valid = valid & ~cloud_invalid
    scheme = _CLASS_SCHEMES[scheme_key]

    # Phase 2 (science/full-pass): Kittler-Illingworth adaptive thresholding
    # for the calibrated S2 water index. Fixed cut points don't hold across
    # seasons/regions/index formulas — Phase 1b measured MNDWI putting 22% of
    # a flooded AOI in the fixed scheme's 0.0-0.3 "wet_soil" band with 0.01%
    # above 0.3. KI fits the AOI's own histogram and derives the
    # minimum-error water/land cut; the graded severity boundaries keep the
    # fixed scheme's internal spacing (+0.3/+0.5) relative to the derived
    # cut. Guarded by a bimodality test (Ashman's D >= 2 + class-fraction
    # floor, see adaptive_threshold.py) — a barely-flooded AOI's unimodal
    # histogram falls back to the fixed scheme with the reason recorded.
    # Either way the derived value + method ride in the result so any run's
    # classification can be re-derived. S2 calibrated water index only —
    # the SAR path is Phase 3's change-detection work, not this.
    threshold_method = None
    derived_threshold = None
    affected_cut = None
    ki_diagnostics = None
    ki_fallback_reason = None
    if scheme_key == "NDWI" and index_calibrated:
        try:
            from adaptive_threshold import derive_water_threshold

            fixed_affected_cut = scheme["bands"][0][0]  # 0.0, the legacy cut
            decision = derive_water_threshold(index[valid], fixed_affected_cut)
            threshold_method = decision["threshold_method"]
            ki_fallback_reason = decision.get("fallback_reason")
            ki_diagnostics = decision.get("ki")
            affected_cut = decision["threshold"]
            if threshold_method == "kittler_illingworth":
                derived_threshold = decision["threshold"]
                t = derived_threshold
                scheme = {
                    "order": "asc",
                    "bands": [
                        (t, 1, "wet_soil", (147, 197, 253), 150),
                        (t + 0.3, 2, "water", (37, 99, 235), 200),
                        (t + 0.5, 3, "deep_water", (30, 58, 138), 220),
                    ],
                }
                logger.info(
                    "Adaptive threshold: KI cut %.4f (ashman_d=%.2f, "
                    "class_fractions=%s) replaces fixed %.2f",
                    t,
                    (ki_diagnostics or {}).get("ashman_d", float("nan")),
                    (ki_diagnostics or {}).get("class_fractions"),
                    fixed_affected_cut,
                )
            else:
                logger.info(
                    "Adaptive threshold fallback to fixed %.2f: %s",
                    fixed_affected_cut,
                    ki_fallback_reason,
                )
        except Exception as exc:  # noqa: BLE001 — adaptive is optional
            logger.warning("Adaptive thresholding unavailable (%s)", exc)
            threshold_method = "fixed_fallback"
            ki_fallback_reason = f"error: {exc}"
            affected_cut = scheme["bands"][0][0]

    classification = _classify(index, valid, scheme)

    # Phase 1c (science/full-pass): permanent-water masking. Flood means
    # water where water is NOT normally present — rivers/lakes/reservoirs
    # (JRC Global Surface Water occurrence >= 75% of observed months,
    # 1984-2021) were previously counted as flood on every run. Pixels
    # classified water that are NORMALLY water are reclassified to 0
    # ("not flood-affected" — the honest flood-purpose claim) and excluded
    # from water_percent / affected_mean_index / affected_area_km2, with
    # the share recorded as permanent_water_percent. Best-effort: an
    # unreachable JRC bucket degrades to no mask with the applied-flag
    # False — it never fails a run. See permanent_water.py for the
    # sourcing decision (windowed /vsicurl/ reads, disk cache) and the
    # 75-vs-50 occurrence-threshold argument (75 errs toward NOT masking
    # seasonal water: recall cost beats precision cost in life safety).
    permanent_water_percent = None
    permanent_water_threshold = None
    permanent_water_source = None
    permanent_water_mask_applied = False
    if disaster == "flood":
        try:
            from permanent_water import (
                DEFAULT_OCCURRENCE_THRESHOLD,
                JRC_SOURCE_LABEL,
                permanent_water_mask_for_clip,
            )

            pw_mask = permanent_water_mask_for_clip(
                index.shape, clipped.get("transform"), clipped.get("crs")
            )
        except Exception as exc:  # noqa: BLE001 — mask is optional, run is not
            logger.warning("Permanent-water masking unavailable (%s)", exc)
            pw_mask = None
        if pw_mask is not None:
            affected_now = (classification >= 1) & (classification != NODATA_CLASS)
            reclassified = affected_now & pw_mask
            classification[reclassified] = 0
            valid_now = int(valid.sum())
            permanent_water_percent = (
                round(100.0 * int(reclassified.sum()) / valid_now, 2)
                if valid_now
                else 0.0
            )
            permanent_water_threshold = DEFAULT_OCCURRENCE_THRESHOLD
            permanent_water_source = JRC_SOURCE_LABEL
            permanent_water_mask_applied = True
            logger.info(
                "Permanent-water mask applied: %.2f%% of valid pixels "
                "reclassified water->normally-water (occurrence >= %d, %s)",
                permanent_water_percent,
                permanent_water_threshold,
                permanent_water_source,
            )

    valid_count = int(valid.sum())
    affected_mask = (classification >= 1) & (classification != NODATA_CLASS)
    affected_count = int(affected_mask.sum())
    water_percent = (
        round(100.0 * affected_count / valid_count, 2) if valid_count else 0.0
    )
    mean_value = (
        round(float(np.nanmean(index[valid])), 4) if valid_count else 0.0
    )
    # Phase 0b (science/full-pass): mean index over the CLASSIFIED-AFFECTED
    # pixels only (for flood: the within-water mean). The whole-AOI
    # `mean_value` stays negative until ~43% of the AOI is water, so any
    # physics check comparing IT against a water threshold fires on every
    # realistic partial flood. The correct support for that comparison is the
    # affected-pixel population — if the classification is physically sound,
    # THIS mean sits above the water threshold regardless of flooded fraction.
    # None (not 0.0) when nothing was classified affected: "no affected
    # pixels" and "affected pixels averaging 0" are different claims.
    affected_mean_index = (
        round(float(np.nanmean(index[affected_mask])), 4)
        if affected_count
        else None
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
        "affected_mean_index": affected_mean_index,
        # Phase 1a: % of in-AOI otherwise-valid pixels excluded by the SCL
        # cloud/shadow/cirrus mask (None on S1 / no-SCL runs). Auditability:
        # any run's index support can be re-derived from this.
        "scl_masked_percent": scl_masked_percent,
        # Phase 1c: permanent-water audit trail — threshold + source make any
        # run's mask re-derivable; applied=False means the run proceeded
        # unmasked (JRC unreachable / non-flood), never silently.
        "permanent_water_mask_applied": permanent_water_mask_applied,
        "permanent_water_percent": permanent_water_percent,
        "permanent_water_occurrence_threshold": permanent_water_threshold,
        "permanent_water_source": permanent_water_source,
        # Phase 2: adaptive-threshold audit trail — the applied affected/not
        # cut, how it was derived, and the KI diagnostics, so any run's
        # classification is re-derivable. None on paths KI doesn't cover
        # (SAR, NDVI).
        "threshold_method": threshold_method,
        "derived_threshold": derived_threshold,
        "affected_cut": affected_cut,
        "ki_diagnostics": ki_diagnostics,
        "ki_fallback_reason": ki_fallback_reason,
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
    """Compute a WGS84/geographic polygon's area in km^2.

    Reprojects to a world equal-area projection (EPSG:6933) for the measure.
    This must never silently degrade to a degrees^2 value mislabeled as km^2
    (off by ~4 orders of magnitude at mid-latitudes, and indistinguishable
    from a real value downstream) — if the equal-area reprojection fails, that
    is a hard failure: it propagates so the pipeline returns status:"failed"
    instead of shipping a wrong-but-plausible-looking area.
    """
    from pyproj import Transformer

    transformer = Transformer.from_crs(
        crs if crs else "EPSG:4326", "EPSG:6933", always_xy=True
    )
    projected = shapely_transform(
        lambda x, y, z=None: transformer.transform(x, y), geom
    )
    return projected.area / 1e6


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
      - `interior_coverage_percent` is the real, continuous coverage figure.
        The interior AOI is the clip mask eroded inward by one pixel, so
        boundary-pixel rasterization artifacts never count as gaps.
      - `covered` here is a legacy "exactly 100%" convenience flag, kept for
        any caller/test still reading it directly. **As of 2026-07-28
        (fix/coverage-tolerance) this is NOT the pass/fail decision anymore**
        — the caller (`process_satellite_imagery`) now compares
        `interior_coverage_percent` against a caller-controlled
        `min_coverage_percent`/`COVERAGE_FLOOR` band instead of requiring
        exactly 100.0 (see that function's docstring and CLAUDE.md's
        "Coverage tolerance" section). This function itself is unchanged —
        it still just measures.
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
    # CHANGE 6: whether this candidate's SCL reused a selection-time peek
    # (True), was freshly downloaded (False), or SCL wasn't requested at all
    # for this satellite_type/disaster (None, e.g. Sentinel-1).
    clipped["scl_reused"] = imagery.get("scl_reused")
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
        # Phase 0b: mean index over classified-affected pixels only (the
        # within-water mean on the flood path); None when nothing classified.
        "affected_mean_index": indices.get("affected_mean_index"),
        # Phase 1a: % of in-AOI pixels the SCL cloud/shadow mask excluded
        # from the index support (None on S1 / no-SCL runs).
        "scl_masked_percent": indices.get("scl_masked_percent"),
        # Phase 1c: permanent-water audit trail.
        "permanent_water_mask_applied": indices.get("permanent_water_mask_applied"),
        "permanent_water_percent": indices.get("permanent_water_percent"),
        "permanent_water_occurrence_threshold": indices.get("permanent_water_occurrence_threshold"),
        "permanent_water_source": indices.get("permanent_water_source"),
        # Phase 2: adaptive-threshold audit trail.
        "threshold_method": indices.get("threshold_method"),
        "derived_threshold": indices.get("derived_threshold"),
        "affected_cut": indices.get("affected_cut"),
        "ki_diagnostics": indices.get("ki_diagnostics"),
        "ki_fallback_reason": indices.get("ki_fallback_reason"),
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
    min_coverage_percent: Optional[float] = DEFAULT_MIN_COVERAGE_PERCENT,
    max_scenes: int = 3,
    max_download_gb: float = DEFAULT_MAX_DOWNLOAD_GB,
    max_search_seconds: float = 900.0,
) -> Optional[dict]:
    """Run the full remote-sensing pipeline to a caller-controlled coverage band.

    download_imagery -> stack_bands -> clip_to_polygon -> calculate_indices
        -> export_png -> vectorize_classification

    Coverage is measured on VALID pixels (non-nodata, and for Sentinel-2 non-cloud
    per SCL) of the AOI, NOT on footprint overlap (BUG 2). Scene selection is a
    tiered, temporally-coherent search (BUG 3): the anchor is the most recent
    acquisition; tiers widen the date window per-satellite (see
    `sentinel.COVERAGE_TIERS_S2`/`COVERAGE_TIERS_S1`, CHANGE 5) and, for the
    same-orbit tiers, require the same relative orbit; Sentinel-1 never mixes
    ascending and descending passes in one mosaic. Within a tier, acquisitions
    are added best-first and the cumulative mosaic is re-clipped until interior
    coverage stops improving usefully.

    **Coverage tolerance (2026-07-28, replaces the old exact-100%-or-fail
    rule — see CLAUDE.md's "Coverage tolerance" section for the full
    rationale).** `min_coverage_percent` (caller-supplied, clamped into
    `[COVERAGE_FLOOR, COVERAGE_CEILING]` = [80, 100]) sets the target. A run's
    achieved `interior_coverage_percent` bands into one of three outcomes:
      - `>= min_coverage_percent` -> status "complete", `coverage_status`
        "target_met", a small proportional confidence penalty for any
        shortfall from 100 (`(100 - coverage) * COVERAGE_PENALTY_SCALE`).
      - `>= COVERAGE_FLOOR` and `< min_coverage_percent` -> still status
        "complete", but `coverage_status: "below_target_coverage"`, a larger
        confidence penalty, and an anomaly appended to
        `coverage_anomalies` naming the shortfall.
      - `< COVERAGE_FLOOR` -> status "failed", reason "insufficient_coverage"
        (the hard stop, unchanged shape from the old rule — just driven by
        `COVERAGE_FLOOR` instead of 100.0).
    The search stops trying more scenes within a tier as soon as
    `interior_coverage_percent >= min_coverage_percent` (not `== 100.0`); the
    outer tier loop only advances to the next tier if the current one never
    reached at least `COVERAGE_FLOOR`. `coverage_percent`, `gap_count`,
    `gap_area_km2`, `gaps` and `gap_attribution` (nodata vs cloud pixel/area
    breakdown) ride in the result on every path, not just the failure path.

    **Search budgets (CHANGE 2 — the actual runaway-cost fix).**
    `max_scenes`/`max_download_gb`/`max_search_seconds` bound the WHOLE
    tiered search (across all tiers, not per-tier). Exhausting any budget
    stops the search immediately and returns the best coverage achieved so
    far, banded the same way as above (`budget_exhausted` names which budget
    tripped) — never starts another download after a budget trips.

    **Un-closeable gaps (CHANGE 3) and marginal returns (CHANGE 4).** Before
    attempting another scene, its footprint must genuinely intersect the
    remaining gap geometry. A gap attributed to cloud (per `gap_cause`) stops
    being chased once no remaining candidate has materially lower AOI cloud
    cover over it (`gap_limited_by: "cloud"`/`"nodata"`/`None` on the
    result). An accepted scene that gains less than
    `MIN_MARGINAL_COVERAGE_GAIN` percentage points stops the search entirely
    (`marginal_return_stop` in `coverage_anomalies`) — distinct from the
    pre-existing near-zero (0.01) doomed-streak duplicate-detection check.

    Args:
        selection / scene_metadata / bbox / merged_polygon / event_id / token /
        disaster_type: as before.
        city_geoms: per-city shapely geometries (WGS84), used only as a hint for
            spreading a tier's scenes across scattered cities.
        city_boundaries: per-city `{"name","geojson"}`; when >1, per-city
            artifacts are rendered from the same accepted mosaic.
        tracker: the event's `ConfidenceTracker`; coverage shortfall and
            tiers 3/4 add concerns and lower confidence through it.
        min_coverage_percent: caller's coverage target (see above); defaults
            to `DEFAULT_MIN_COVERAGE_PERCENT` and is clamped server-side.
        max_scenes / max_download_gb / max_search_seconds: whole-search
            budgets (see above); defaults are the only place these are
            hardcoded — every upstream caller should thread its own values.

    On success returns the merged result dict, which now also carries
    `coverage_percent`, `full_aoi_coverage_percent`, `coverage_tier`,
    `coverage_status`, `temporal_spread_days`, `acquisition_count`,
    `gap_count`, `gap_area_km2`, `gap_attribution`, `gap_limited_by` and
    `bytes_downloaded`.
    """
    satellite_type = selection.get("satellite_type", "sentinel-2")
    min_cov = _clamp_min_coverage_percent(min_coverage_percent)

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
            "gap_count": 0,
            "gap_area_km2": 0.0,
            "gap_attribution": {"nodata": 0, "cloud": 0},
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
    best_clipped = None
    best_accepted: list = []
    best_tier = None
    best_orbit_dir = None

    # CHANGE 2 — whole-search budgets, tracked across ALL tiers.
    search_start = time.monotonic()
    scenes_attempted = 0
    budget_exhausted = None  # "max_scenes" | "max_download_gb" | "max_search_seconds" | None
    marginal_stop = False
    gap_limited_by = None

    for tier, orbit_dir, group in tiers:
        if budget_exhausted:
            break
        # BUG 3: within a tier, add acquisitions best-first and re-measure real
        # coverage until interior AOI reaches the caller's target. Consecutive
        # doomed downloads (valid pixels don't grow) abort the tier early
        # (BUG 4c).
        accepted: list = []
        doomed_streak = 0
        clipped = None
        cov = None
        for scene in group:
            # CHANGE 3 — before attempting a candidate to fill a remaining
            # gap, check it genuinely intersects the gap geometry (not just
            # the whole AOI, which was already checked above). Only
            # meaningful once we have at least one accepted scene with a
            # measured gap; the very first scene in a tier always gets tried.
            if cov and cov.get("gaps"):
                gap_polys = _gap_geoms_as_shapes(cov["gaps"])
                if gap_polys is not None and not _scene_intersects_gaps(
                    scene, gap_polys
                ):
                    logger.info(
                        "Tier %d: %s does not intersect the remaining gap "
                        "geometry; skipping", tier, scene.get("Name"),
                    )
                    continue
                # Cloud-attributed gap: stop chasing it once no remaining
                # candidate has materially lower AOI cloud cover than what
                # was already tried.
                cause = cov.get("gap_cause") or {}
                if cause.get("cloud", 0) > cause.get("nodata", 0):
                    prev_cloud = _scene_cloud_for_gap_check(accepted[-1] if accepted else None)
                    cand_cloud = _scene_cloud_for_gap_check(scene)
                    if (
                        prev_cloud is not None
                        and cand_cloud is not None
                        and cand_cloud >= prev_cloud - 5.0  # not "materially" lower
                    ):
                        gap_limited_by = "cloud"
                        logger.info(
                            "Tier %d: remaining gap is cloud-attributed and "
                            "%s has no materially lower AOI cloud cover "
                            "(%.1f%% vs %.1f%%); stopping this tier's search.",
                            tier, scene.get("Name"), cand_cloud, prev_cloud,
                        )
                        break

            # CHANGE 2 — budget checks BEFORE starting another download.
            elapsed = time.monotonic() - search_start
            bytes_so_far_gb = (_bytes_downloaded_total() - bytes_before) / 1e9
            logger.info(
                "[BUDGET] scenes=%d/%d bytes=%.2f/%.2f GB elapsed=%.0f/%.0f s",
                scenes_attempted, max_scenes, bytes_so_far_gb, max_download_gb,
                elapsed, max_search_seconds,
            )
            if scenes_attempted >= max_scenes:
                budget_exhausted = "max_scenes"
                break
            if bytes_so_far_gb >= max_download_gb:
                budget_exhausted = "max_download_gb"
                break
            if elapsed >= max_search_seconds:
                budget_exhausted = "max_search_seconds"
                break

            trial = accepted + [scene]
            attempt_id = f"{event_id}/t{tier}"
            trial_clip = _attempt_clip(
                selection, trial, merged_polygon, attempt_id, token,
                disaster_type,
            )
            scenes_attempted += 1
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
            prior_pct = cov["interior_coverage_percent"] if cov else 0.0
            gained = trial_cov["interior_coverage_percent"] - prior_pct
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
            # CHANGE 1 — stop as soon as the caller's target is met, not only
            # at exactly 100%.
            if cov["interior_coverage_percent"] >= min_cov:
                break
            # CHANGE 4 — marginal-return stopping: an accepted-but-small
            # contribution (below MIN_MARGINAL_COVERAGE_GAIN, but above the
            # 0.01 doomed-streak floor above) isn't worth its download cost.
            # Only applies after the FIRST acquisition in a tier (the first
            # scene establishes a baseline; there's nothing to compare it
            # against).
            if len(accepted) > 1 and gained < MIN_MARGINAL_COVERAGE_GAIN:
                marginal_stop = True
                logger.info(
                    "Tier %d: %s gained only +%.3f%% (< %.1f%% marginal "
                    "threshold); stopping search rather than trying more "
                    "scenes.", tier, scene.get("Name"), gained,
                    MIN_MARGINAL_COVERAGE_GAIN,
                )
                break

        if cov and cov["interior_coverage_percent"] > best_interior:
            best_interior = cov["interior_coverage_percent"]
            best_cov = cov
            best_clipped = clipped
            best_accepted = accepted
            best_tier = tier
            best_orbit_dir = orbit_dir

        if cov and cov["interior_coverage_percent"] >= min_cov:
            # Target reached in this tier. Compute temporal spread + count.
            dates = [d for d in (scene_acq_date(s) for s in accepted) if d]
            spread = (max(dates) - min(dates)).days if len(dates) >= 2 else 0
            logger.info(
                "Coverage target %.1f%% reached at tier %d (%.3f%% achieved, "
                "%d acquisition(s), %d-day spread, orbit=%s)",
                min_cov, tier, cov["interior_coverage_percent"],
                len(accepted), spread, orbit_dir,
            )
            return _finish_success(
                clipped, cov, accepted, tier, orbit_dir, spread,
                satellite_type, disaster_type, event_id, city_boundaries,
                tracker, bytes_before, min_cov, budget_exhausted=None,
                marginal_stop=marginal_stop, gap_limited_by=gap_limited_by,
            )

        if budget_exhausted or marginal_stop or gap_limited_by:
            # gap_limited_by: the remaining gap is weather-limited within
            # THIS tier's own candidates, but a wider/different tier may
            # still offer a genuinely different (non-cloud-affected)
            # acquisition — so this only stops the OUTER search, same as a
            # budget or marginal-return stop, rather than being treated as
            # "try the next tier and see" (which would just re-hit the same
            # cloud ceiling on every tier's candidates, burning budget for
            # nothing, as the search-budget log would show).
            break

    # No tier reached the target (or a budget/marginal-return/weather stop fired).
    # Band the best-effort result per CHANGE 1: if it's still >= min_cov here
    # (possible when a budget/marginal stop hit mid-tier after already
    # crossing the target — defensive, the success return above should have
    # already caught that) or >= COVERAGE_FLOOR, report "complete" with the
    # coverage caveat; only below the floor is a true hard stop.
    if best_cov and best_interior >= COVERAGE_FLOOR:
        dates = [d for d in (scene_acq_date(s) for s in best_accepted) if d]
        spread = (max(dates) - min(dates)).days if len(dates) >= 2 else 0
        logger.warning(
            "Coverage search stopped (%s) at %.3f%% (target %.1f%%, floor "
            "%.1f%%) — reporting best-effort as complete with caveats.",
            budget_exhausted or ("marginal_return" if marginal_stop else "exhausted_tiers"),
            best_interior, min_cov, COVERAGE_FLOOR,
        )
        return _finish_success(
            best_clipped, best_cov, best_accepted, best_tier, best_orbit_dir,
            spread, satellite_type, disaster_type, event_id, city_boundaries,
            tracker, bytes_before, min_cov, budget_exhausted=budget_exhausted,
            marginal_stop=marginal_stop, gap_limited_by=gap_limited_by,
        )

    # Below COVERAGE_FLOOR — fail honestly with gap geometry. NEVER analyse a
    # partial AOI below the floor (BUG 3, floor-driven per CHANGE 1).
    gaps = best_cov["gaps"] if best_cov else []
    gap_cause = best_cov["gap_cause"] if best_cov else {"nodata": 0, "cloud": 0}
    total_gap_km2 = round(sum(g["area_km2"] for g in gaps), 4)
    bytes_after = _bytes_downloaded_total()
    logger.error(
        "INSUFFICIENT COVERAGE for %s: best interior coverage %.3f%% (floor "
        "%.1f%%) across all tiers; %d uncovered region(s) totalling %.3f "
        "km^2 (nodata=%d px, cloud=%d px). Refusing to analyse a partial "
        "AOI below the floor.%s",
        event_id, max(best_interior, 0.0), COVERAGE_FLOOR, len(gaps),
        total_gap_km2, gap_cause["nodata"], gap_cause["cloud"],
        f" budget_exhausted={budget_exhausted}" if budget_exhausted else "",
    )
    result = {
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
        "gap_count": len(gaps),
        "gap_area_km2": total_gap_km2,
        "gap_cause": gap_cause,
        "gap_attribution": gap_cause,
        "gap_limited_by": gap_limited_by,
        "bytes_downloaded": bytes_after - bytes_before,
        "processing_level": (
            "L2A" if satellite_type == "sentinel-2" else None
        ),
        "min_coverage_percent": min_cov,
    }
    if budget_exhausted:
        result["budget_exhausted"] = budget_exhausted
    return result


def _gap_geoms_as_shapes(gaps: list):
    """Best-effort bbox polygons (WGS84) for a `compute_coverage` gap list.

    Used by CHANGE 3's pre-download gap-intersection check. Returns None when
    no gap has usable bbox geometry (falls back to "always try" upstream).
    """
    from shapely.geometry import box as _box

    polys = []
    for g in gaps or []:
        bbox = g.get("bbox")
        if not bbox:
            continue
        try:
            polys.append(
                _box(bbox["west"], bbox["south"], bbox["east"], bbox["north"])
            )
        except (KeyError, TypeError, ValueError):
            continue
    return polys or None


def _scene_intersects_gaps(scene: dict, gap_polys: list) -> bool:
    """True if `scene`'s WGS84 footprint intersects any gap bbox polygon.

    CHANGE 3: cheapest-correct implementation — a real geometric intersection
    test (not "try it and see"), restricted to the gap bboxes rather than the
    full AOI (which was already checked earlier in the search).
    """
    footprint = scene.get("GeoFootprint")
    if not footprint:
        return True  # unknown footprint: don't block the candidate
    try:
        geom = shape(footprint)
    except (ValueError, AttributeError, TypeError):
        return True
    for gap in gap_polys:
        try:
            if geom.intersects(gap):
                return True
        except (ValueError, AttributeError, TypeError):
            continue
    return False


def _scene_cloud_for_gap_check(scene: Optional[dict]) -> Optional[float]:
    """Cloud-cover percent for CHANGE 3's cloud-gap comparison, or None.

    Prefers the AOI-restricted figure (CHANGE 6) when the scene carries one
    (`_aoi_cloud`), then the scene-level catalogue figure already annotated
    by `sentinel.search_imagery`'s ranking pass (`_cloud`), then falls back to
    reading the raw `cloudCover` attribute directly (a scene passed straight
    from a catalogue query that skipped `search_imagery`'s ranking, e.g. a
    single-scene direct call, still has `Attributes` but no `_cloud`).
    """
    if not scene:
        return None
    aoi_cloud = scene.get("_aoi_cloud")
    if aoi_cloud is not None:
        try:
            return float(aoi_cloud)
        except (TypeError, ValueError):
            pass
    cloud = scene.get("_cloud")
    if cloud is not None and cloud != float("inf"):
        try:
            return float(cloud)
        except (TypeError, ValueError):
            pass
    try:
        from sentinel import _scene_cloud_cover
        raw = _scene_cloud_cover(scene)
        if raw != float("inf"):
            return float(raw)
    except Exception:
        pass
    return None


def _finish_success(
    clipped: dict,
    cov: dict,
    accepted: list,
    tier: int,
    orbit_dir,
    spread: int,
    satellite_type: str,
    disaster_type: str,
    event_id: str,
    city_boundaries,
    tracker,
    bytes_before: int,
    min_cov: float,
    budget_exhausted: Optional[str],
    marginal_stop: bool,
    gap_limited_by: Optional[str],
) -> Optional[dict]:
    """Render + finalize a successful (target-met or best-effort) coverage result.

    Shared tail for both the "target reached" return and the "budget/marginal
    stop but still >= COVERAGE_FLOOR" best-effort return (CHANGE 1/2/4), so
    both paths carry identical fields.
    """
    interior_pct = cov["interior_coverage_percent"]
    band = _coverage_band(interior_pct, min_cov)

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
    scene_ids = [
        s.get("Id") or s.get("Name") for s in accepted
        if s.get("Id") or s.get("Name")
    ]
    gaps = cov.get("gaps") or []
    gap_cause = cov.get("gap_cause") or {"nodata": 0, "cloud": 0}
    total_gap_km2 = round(sum(g["area_km2"] for g in gaps), 4)

    # Scene recency (islamabad-findings #4): the tiered search bounds temporal
    # COHERENCE within a mosaic (BUG 3) but nothing bounds staleness relative
    # to the event itself. Use the MOST RECENT accepted acquisition — the
    # freshest data point the result is based on — measured against "now"
    # (when the pipeline finished processing it, the closest available proxy
    # for "when the event was analysed").
    from sentinel import scene_datetime as _scene_datetime

    acq_datetimes = [
        d for d in (_scene_datetime(s) for s in accepted) if d is not None
    ]
    scene_age_days = None
    if acq_datetimes:
        newest = max(acq_datetimes)
        scene_age_days = (datetime.now(timezone.utc) - newest).total_seconds() / 86400.0
        scene_age_days = round(scene_age_days, 2)

    # Proportional confidence penalty for shortfall from 100% (CHANGE 1). Not
    # a hardcoded cliff: linear in the shortfall, scaled by
    # COVERAGE_PENALTY_SCALE, and doubled in the below-target band since that
    # band is a real, larger limitation (the caller's own quality bar wasn't
    # met).
    shortfall = max(0.0, 100.0 - interior_pct)
    penalty = shortfall * COVERAGE_PENALTY_SCALE
    if band == "below_target":
        penalty *= 2.0

    merged_result.update({
        "valid_percent": round(interior_pct, 2),
        "coverage_percent": interior_pct,
        "full_aoi_coverage_percent": cov["full_aoi_coverage_percent"],
        "coverage_tier": tier,
        "coverage_status": (
            "target_met" if band == "complete" else "below_target_coverage"
        ),
        "min_coverage_percent": min_cov,
        "temporal_spread_days": spread,
        "acquisition_count": len(accepted),
        "orbit_direction": orbit_dir,
        "coverage_gaps": gaps,
        "gaps": gaps,
        "gap_count": len(gaps),
        "gap_area_km2": total_gap_km2,
        "gap_cause": gap_cause,
        "gap_attribution": gap_cause,
        "gap_limited_by": gap_limited_by,
        "bytes_downloaded": bytes_after - bytes_before,
        "scene_id": ",".join(scene_ids) if scene_ids else None,
        "scl_reused": clipped.get("scl_reused"),
        "processing_level": (
            "L2A" if satellite_type == "sentinel-2" else None
        ),
        # BUG 7 — per-stage peak RSS + which stage peaked, scaled by tiles.
        "memory_report": memory_report(),
        # Scene recency (islamabad-findings #4) — how many days old the most
        # recent accepted acquisition is, relative to now. Always reported
        # (None only when no accepted scene had a parseable date), so a
        # responder can always tell whether they're looking at current
        # conditions or dated imagery.
        "scene_age_days": scene_age_days,
    })
    if budget_exhausted:
        merged_result["budget_exhausted"] = budget_exhausted

    # Confidence penalty + anomaly for stale imagery (islamabad-findings #4).
    # Linear in days past the threshold, not a hard cutoff — old imagery is
    # still reported and used, just with visibly reduced confidence.
    if scene_age_days is not None and scene_age_days > SCENE_AGE_ANOMALY_DAYS:
        age_over = scene_age_days - SCENE_AGE_ANOMALY_DAYS
        age_penalty = age_over * SCENE_AGE_PENALTY_PER_DAY
        if tracker is not None:
            tracker.add_evidence(
                "scene_age", max(0.0, 1.0 - age_penalty), weight=0.2
            )
            tracker.add_concern(
                f"Most recent accepted scene is {scene_age_days:.1f} days old "
                f"(> {SCENE_AGE_ANOMALY_DAYS}-day threshold) — imagery may "
                "describe conditions before or after the event, not current "
                "conditions.",
                "HIGH" if scene_age_days > 2 * SCENE_AGE_ANOMALY_DAYS else "MEDIUM",
            )
        merged_result.setdefault("coverage_anomalies", []).append({
            "type": "stale_scene_age",
            "scene_age_days": scene_age_days,
            "threshold_days": SCENE_AGE_ANOMALY_DAYS,
        })

    # Coverage shortfall confidence penalty + anomaly (CHANGE 1). Applied
    # whenever coverage is below 100%, regardless of band.
    if shortfall > 0.0:
        if tracker is not None:
            tracker.add_evidence(
                "coverage_shortfall", max(0.0, 1.0 - penalty), weight=0.3
            )
        if band == "below_target_coverage" or merged_result["coverage_status"] == "below_target_coverage":
            if tracker is not None:
                tracker.add_concern(
                    f"Coverage reached only {interior_pct:.2f}% "
                    f"(target {min_cov:.1f}%, floor {COVERAGE_FLOOR:.1f}%); "
                    f"{len(gaps)} uncovered region(s) totalling "
                    f"{total_gap_km2:.3f} km^2 (nodata={gap_cause.get('nodata')}px, "
                    f"cloud={gap_cause.get('cloud')}px).",
                    "HIGH",
                )
            merged_result.setdefault("coverage_anomalies", []).append({
                "type": "below_target_coverage",
                "coverage_percent": interior_pct,
                "min_coverage_percent": min_cov,
                "gap_count": len(gaps),
                "gap_area_km2": total_gap_km2,
                "gap_cause": gap_cause,
                "severity": "HIGH",
            })

    if marginal_stop:
        merged_result.setdefault("coverage_anomalies", []).append({
            "type": "marginal_return_stop",
            "coverage_percent": interior_pct,
            "threshold_percent": MIN_MARGINAL_COVERAGE_GAIN,
        })

    if gap_limited_by:
        merged_result.setdefault("coverage_anomalies", []).append({
            "type": "gap_limited_by_weather",
            "gap_limited_by": gap_limited_by,
            "gap_count": len(gaps),
            "gap_area_km2": total_gap_km2,
        })

    # Tiers 3 and 4 are a real limitation: a 7-14 day spread on a flood
    # is stale imagery. Lower confidence + append an anomaly so it is
    # visible downstream.
    if tier >= 3 and tracker is not None:
        sev = "HIGH" if tier == 4 else "MEDIUM"
        tracker.add_concern(
            f"Coverage reached target only at tier {tier} "
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

    logger.info(
        "Satellite imagery pipeline complete for %s (coverage=%.3f%%, "
        "status=%s)", event_id, interior_pct, merged_result["coverage_status"],
    )
    return merged_result


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
