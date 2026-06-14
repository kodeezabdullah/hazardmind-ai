"""Sentinel selection and Copernicus Data Space access for the satellite agent.

Given a disaster type (and optional cloud cover), this module picks the right
Sentinel mission, authenticates against the Copernicus Data Space Ecosystem
(CDSE), and searches the catalogue for the best available scene over a bbox.

Mission choice:
- Floods are imaged through cloud/rain, so we use Sentinel-1 (SAR).
- Earthquakes and landslides need optical detail, so we use Sentinel-2.
- If optical imagery would be obscured (cloud cover > 30%), we fall back to
  Sentinel-1 which is weather-independent.

Credentials come from the environment (loaded from `.env`):
    COPERNICUS_USERNAME, COPERNICUS_PASSWORD

Run this file directly for a small smoke test:
    python sentinel.py
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from shapely.geometry import box, shape

load_dotenv()

logger = logging.getLogger(__name__)

# Copernicus Data Space Ecosystem endpoints.
TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/"
    "openid-connect/token"
)
CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

# Optical imagery above this cloud percentage is treated as unusable; we then
# fall back to SAR (Sentinel-1).
CLOUD_COVER_THRESHOLD = 30.0

# A single scene whose footprint covers less than this percentage of the AOI is
# not enough on its own; the processor mosaics the top-ranked scenes instead.
# Raised 60 -> 85 so scattered multi-city AOIs (where the best single tile still
# leaves cities uncovered) reliably trigger the mosaic path.
COVERAGE_MOSAIC_THRESHOLD = 85.0

SENTINEL_1 = "sentinel-1"
SENTINEL_2 = "sentinel-2"

# Maps our mission ids to the collection names used in the CDSE catalogue.
_COLLECTION_NAMES = {
    SENTINEL_1: "SENTINEL-1",
    SENTINEL_2: "SENTINEL-2",
}

# Disaster types whose user hint points at optical imagery (Sentinel-2).
_OPTICAL_DISASTERS = {"earthquake", "landslide", "wildfire"}
# Disaster types whose user hint points at SAR (Sentinel-1).
_SAR_DISASTERS = {"flood", "cyclone", "tsunami"}


def _peek_cloud_cover(
    bbox: tuple, token: Optional[str], date_range: int = 14, timeout: int = 30
) -> Optional[float]:
    """Quickly look up the cloud cover of the best recent Sentinel-2 scene.

    A lightweight, metadata-only catalogue query (no cloud-cover filter) used by
    `select_satellite` to decide optical-vs-SAR from the actual sky conditions.
    Returns the lowest cloud-cover percentage among recent scenes, or None if no
    scene is found or the lookup fails.
    """
    if not token:
        logger.info("No CDSE token for cloud peek; skipping metadata check")
        return None

    try:
        minx, miny, maxx, maxy = bbox
    except (TypeError, ValueError):
        return None

    start = (datetime.now(timezone.utc) - timedelta(days=date_range)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    polygon = (
        f"POLYGON(({minx} {miny},{maxx} {miny},{maxx} {maxy},"
        f"{minx} {maxy},{minx} {miny}))"
    )
    params = {
        "$filter": " and ".join(
            [
                "Collection/Name eq 'SENTINEL-2'",
                f"OData.CSC.Intersects(area=geography'SRID=4326;{polygon}')",
                f"ContentDate/Start gt {start}",
            ]
        ),
        "$orderby": "ContentDate/Start desc",
        "$top": "10",
        "$expand": "Attributes",
    }

    try:
        response = requests.get(CATALOGUE_URL, params=params, timeout=timeout)
        response.raise_for_status()
        results = response.json().get("value", [])
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Cloud-cover peek failed: %s", exc)
        return None

    if not results:
        return None

    best = min(results, key=_scene_cloud_cover)
    cc = _scene_cloud_cover(best)
    if cc == float("inf"):
        return None
    logger.info("Cloud-cover peek: best recent S2 scene has %.1f%% cloud", cc)
    return cc


def select_satellite(
    disaster_type: str,
    bbox: Optional[tuple] = None,
    token: Optional[str] = None,
    cloud_cover: Optional[float] = None,
) -> dict:
    """Pick the Sentinel mission for a disaster, cloud cover deciding.

    Priority order:
    1. Quick metadata check: peek the cloud cover of the best recent Sentinel-2
       scene over `bbox`. > CLOUD_COVER_THRESHOLD -> Sentinel-1; otherwise
       Sentinel-2. (Skipped when no bbox/token is available, or when an explicit
       `cloud_cover` is supplied.)
    2. User hint as a fallback / confirmation: flood/cyclone/tsunami -> SAR;
       earthquake/landslide/wildfire -> optical.
    3. Conflict resolution: cloud cover ALWAYS wins over the user hint
       (physics over assumption) — e.g. heavy cloud + "earthquake" still SAR.

    Returns:
        {
            "satellite_type": "sentinel-1" | "sentinel-2",
            "reason": str,                # why this mission was chosen
            "cloud_cover": float | None,  # observed cloud %, if known
            "user_hint": str,             # the disaster type, lowercased
        }
    """
    disaster = (disaster_type or "").strip().lower()

    # Hint-based choice (used as a fallback and to disambiguate the threshold).
    if disaster in _SAR_DISASTERS:
        hint_satellite = SENTINEL_1
    elif disaster in _OPTICAL_DISASTERS:
        hint_satellite = SENTINEL_2
    else:
        logger.warning(
            "Unknown disaster type %r; hint defaults to optical (Sentinel-2)",
            disaster_type,
        )
        hint_satellite = SENTINEL_2

    # Step 1: cloud cover from real metadata (or an explicitly supplied value).
    observed = cloud_cover
    if observed is None and bbox is not None:
        observed = _peek_cloud_cover(bbox, token)

    # Step 3: cloud cover wins when we have it.
    if observed is not None:
        if observed > CLOUD_COVER_THRESHOLD:
            satellite = SENTINEL_1
            reason = f"cloud_cover_{round(observed)}_percent"
        else:
            satellite = SENTINEL_2
            reason = f"clear_sky_cloud_cover_{round(observed)}_percent"
    else:
        # No cloud info: trust the user hint.
        satellite = hint_satellite
        reason = f"user_hint_{disaster or 'unknown'}"

    result = {
        "satellite_type": satellite,
        "reason": reason,
        "cloud_cover": observed,
        "user_hint": disaster,
    }
    logger.info(
        "Selected %s (reason=%s, cloud_cover=%s, hint=%s)",
        satellite,
        reason,
        observed,
        disaster,
    )
    return result


def authenticate_copernicus(timeout: int = 30) -> Optional[str]:
    """Obtain an access token from the Copernicus Data Space Ecosystem.

    Uses the password grant against the CDSE Keycloak token endpoint with the
    `COPERNICUS_USERNAME` / `COPERNICUS_PASSWORD` environment variables. Returns
    the access token string, or None if credentials are missing or the request
    fails.
    """
    username = os.getenv("COPERNICUS_USERNAME")
    password = os.getenv("COPERNICUS_PASSWORD")

    if not username or not password:
        logger.error(
            "COPERNICUS_USERNAME / COPERNICUS_PASSWORD not set; "
            "cannot authenticate"
        )
        return None

    data = {
        "client_id": "cdse-public",
        "username": username,
        "password": password,
        "grant_type": "password",
    }

    logger.info("Requesting Copernicus access token for %s", username)
    try:
        response = requests.post(TOKEN_URL, data=data, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Copernicus authentication failed: %s", exc)
        return None

    try:
        token = response.json().get("access_token")
    except ValueError as exc:
        logger.error("Could not parse Copernicus token response: %s", exc)
        return None

    if not token:
        logger.error("Copernicus token response contained no access_token")
        return None

    logger.info("Obtained Copernicus access token")
    return token


def _aoi_geometry(bbox: tuple, aoi_geom: Optional[dict]):
    """Build the shapely geometry coverage is measured against.

    Prefers the actual risk polygon (`aoi_geom`, the merged risk-city geometry
    in WGS84) when supplied, falling back to the bbox rectangle. Using the real
    polygon matters: a wide bbox around scattered cities is mostly empty, so a
    tile can overlap the *bbox* heavily while covering *none* of the cities.
    Returns None if neither can be built.
    """
    if aoi_geom:
        try:
            return shape(aoi_geom)
        except (ValueError, AttributeError, TypeError):
            pass
    try:
        minx, miny, maxx, maxy = bbox
        return box(minx, miny, maxx, maxy)
    except (TypeError, ValueError):
        return None


def _scene_aoi_overlap(scene: dict, aoi) -> float:
    """Return the fraction (0..1) of the AOI covered by a scene footprint.

    `aoi` is a shapely geometry (the risk polygon, or the bbox as a fallback).
    Uses the scene's `GeoFootprint` (a WGS84 GeoJSON polygon). A single Sentinel
    tile only covers part of a wide AOI, so this is what tells coverage-aware
    selection how useful a scene actually is. Returns 0.0 if the footprint is
    missing or unparseable.
    """
    footprint = scene.get("GeoFootprint")
    if not footprint or aoi is None:
        return 0.0
    try:
        aoi_area = aoi.area
        if aoi_area <= 0:
            return 0.0
        geom = shape(footprint)
        return max(0.0, min(1.0, aoi.intersection(geom).area / aoi_area))
    except (ValueError, AttributeError, TypeError) as exc:
        logger.debug("Could not compute AOI overlap: %s", exc)
        return 0.0


def _scene_score(scene: dict, aoi) -> float:
    """Coverage-aware score for a scene: overlap% * (1 - cloud_cover/100).

    A scene that covers more of the AOI and is less cloudy scores higher. Cloud
    cover is treated as 0 when unknown (Sentinel-1 has none). The score is in
    0..1; higher is better.
    """
    overlap = _scene_aoi_overlap(scene, aoi)
    cc = _scene_cloud_cover(scene)
    if cc == float("inf"):
        cc = 0.0
    cc = max(0.0, min(100.0, cc))
    return overlap * (1.0 - cc / 100.0)


def _scene_covers_geom(scene: dict, geom, min_fraction: float = 0.10) -> bool:
    """True if a scene's footprint covers at least `min_fraction` of `geom`.

    Used by greedy mosaic selection to decide whether a candidate scene
    meaningfully covers a given city polygon (a tiny sliver doesn't count).
    """
    footprint = scene.get("GeoFootprint")
    if not footprint or geom is None:
        return False
    try:
        area = geom.area
        if area <= 0:
            return False
        covered = geom.intersection(shape(footprint)).area / area
        return covered >= min_fraction
    except (ValueError, AttributeError, TypeError):
        return False


_MGRS_TILE_RE = re.compile(r"_T(\d{2}[A-Z]{3})_")


def _scene_tile_id(scene: dict) -> Optional[str]:
    """Extract the MGRS tile id (e.g. '51PXK') from a scene Name, or None."""
    match = _MGRS_TILE_RE.search(scene.get("Name", "") or "")
    return match.group(1) if match else None


def backfill_uncovered_cities(
    ranked,
    city_entries,
    satellite_type: str,
    aoi_geom: Optional[dict] = None,
    initial_date_range: int = 7,
    widen_date_ranges=(14, 30),
    min_covering_scenes: int = 2,
    timeout: int = 60,
):
    """Widen the date window for any city too few candidate scenes cover.

    The default 7-day search can leave a city effectively uncovered when its
    only recent tile is a *partial* acquisition that doesn't actually reach it
    (e.g. Mindanao's Cagayan de Oro: the single 7-day 51PXK scene's real data
    stops ~8.14N, south of the city, even though its catalogue footprint claims
    to reach 9.05N). The footprint overstates the data, so a city can look
    "covered" while the pixels are missing.

    To be robust to that, a city is considered safely covered only when at least
    `min_covering_scenes` *distinct acquisitions* (by footprint) include it — a
    second scene of the same tile fills the first's nodata/partial-swath gaps.
    For any city below that bar we re-search *its own bbox* over progressively
    wider windows, scoring new covering scenes against the full AOI and appending
    them (de-duplicated by product Id). Older scenes are acceptable here: a
    slightly less current tile that actually has data beats no coverage.

    Args:
        ranked: the ranked scene list from `search_imagery(..., return_ranked=True)`.
        city_entries: list of `{"name", "geojson", ...}` from
            `boundary.get_risk_city_boundaries`.
        satellite_type: "sentinel-1" / "sentinel-2".
        aoi_geom: merged AOI GeoJSON, used to re-score appended scenes.
        initial_date_range: the window already searched (skipped when widening).
        widen_date_ranges: ascending windows to try for uncovered cities.
        timeout: per-request timeout.

    Returns the (possibly extended) ranked list, re-sorted best-first.
    """
    if not city_entries:
        return ranked

    ranked = list(ranked)
    seen_ids = {s.get("Id") for s in ranked}
    aoi = _aoi_geometry(None, aoi_geom)

    for entry in city_entries:
        geom = None
        try:
            geom = shape(entry["geojson"])
        except (KeyError, ValueError, AttributeError, TypeError):
            continue
        if geom is None or geom.area <= 0:
            continue
        covering = sum(1 for s in ranked if _scene_covers_geom(s, geom))
        if covering >= min_covering_scenes:
            continue  # enough distinct acquisitions already include this city

        name = entry.get("name", "?")
        found = False
        for window in widen_date_ranges:
            if window <= initial_date_range:
                continue
            logger.info(
                "City %r uncovered by %dd candidates; widening to %dd",
                name,
                initial_date_range,
                window,
            )
            extra = search_imagery(
                geom.bounds,
                satellite_type,
                date_range=window,
                timeout=timeout,
                return_ranked=True,
                aoi_geom=entry["geojson"],
            )
            if not extra:
                continue
            for scene in extra:
                if scene.get("Id") in seen_ids:
                    continue
                if not _scene_covers_geom(scene, geom):
                    continue
                # Re-score against the full AOI so it ranks consistently.
                scene["_overlap"] = _scene_aoi_overlap(scene, aoi)
                scene["_cloud"] = _scene_cloud_cover(scene)
                scene["_score"] = _scene_score(scene, aoi)
                ranked.append(scene)
                seen_ids.add(scene.get("Id"))
                covering += 1
                found = True
            if found:
                logger.info(
                    "Backfilled coverage for %r from %dd window "
                    "(now %d covering scene(s))",
                    name,
                    window,
                    covering,
                )
            if covering >= min_covering_scenes:
                break
        if not found:
            logger.warning(
                "No scene with real coverage of %r found even after widening "
                "(data-availability limit)",
                name,
            )

    ranked.sort(key=lambda s: s.get("_score", 0.0), reverse=True)
    return ranked


def select_mosaic_scenes(ranked, city_geoms, max_scenes: int):
    """Greedily pick scenes that maximise combined city coverage (set-cover).

    The naive top-`max_scenes`-by-score set tends to bunch around the single
    best-covered city, leaving other scattered cities uncovered (the Mindanao
    trap: 3 highest-overlap tiles all clustered on one city). This instead does
    a greedy weighted set-cover over the *individual* city polygons:

    1. Start with the cities that no chosen scene covers yet.
    2. Repeatedly pick the scene that newly covers the most still-uncovered
       cities (ties broken by the scene's coverage score, so less-cloudy /
       higher-overlap wins), until every city is covered or `max_scenes` is hit.
    3. If the cap is reached with cities still uncovered, or all cities are
       covered with budget to spare, top up from the remaining ranked scenes
       (best score first) so the mosaic still fills nodata gaps.

    Args:
        ranked: scenes sorted best-first, each annotated with `_score`
            (from `search_imagery(..., return_ranked=True)`).
        city_geoms: list of shapely geometries, one per risk city (WGS84).
        max_scenes: maximum number of scenes to return.

    Returns the chosen scene dicts (a subset of `ranked`), best-first within the
    final set. Falls back to `ranked[:max_scenes]` when no city geometries are
    given.
    """
    if max_scenes <= 0:
        return []
    if not city_geoms:
        return list(ranked[:max_scenes])

    remaining = [g for g in city_geoms if g is not None]
    chosen = []
    pool = list(ranked)

    # Greedy set-cover: each round, take the scene covering the most cities not
    # yet covered. Ties -> higher score (already the pool's sort order).
    while remaining and pool and len(chosen) < max_scenes:
        best_scene = None
        best_new = 0
        best_score = -1.0
        for scene in pool:
            newly = [g for g in remaining if _scene_covers_geom(scene, g)]
            score = scene.get("_score", 0.0) or 0.0
            if len(newly) > best_new or (
                len(newly) == best_new and len(newly) > 0 and score > best_score
            ):
                best_scene, best_new, best_score = scene, len(newly), score
        if best_scene is None or best_new == 0:
            break  # no remaining scene covers any still-uncovered city
        chosen.append(best_scene)
        pool.remove(best_scene)
        remaining = [
            g for g in remaining if not _scene_covers_geom(best_scene, g)
        ]

    # Top up with the best remaining scenes (fills nodata gaps / unreached
    # cities) until we hit the cap. Prefer scenes whose MGRS tile is not already
    # in the set so a spare slot adds new geography rather than a duplicate
    # tile; fall back to any remaining scene if every tile is already present.
    chosen_tiles = {_scene_tile_id(s) for s in chosen}
    for prefer_new_tile in (True, False):
        for scene in pool:
            if len(chosen) >= max_scenes:
                break
            if scene in chosen:
                continue
            tid = _scene_tile_id(scene)
            if prefer_new_tile and tid is not None and tid in chosen_tiles:
                continue
            chosen.append(scene)
            chosen_tiles.add(tid)
        if len(chosen) >= max_scenes:
            break

    uncovered = len(remaining)
    logger.info(
        "Mosaic set-cover: chose %d scene(s) for %d cities (%d city(ies) "
        "still uncovered after selection)",
        len(chosen),
        len([g for g in city_geoms if g is not None]),
        uncovered,
    )
    return chosen


def search_imagery(
    bbox: tuple,
    satellite_type: str,
    date_range: int = 7,
    timeout: int = 60,
    return_ranked: bool = False,
    aoi_geom: Optional[dict] = None,
):
    """Search the CDSE catalogue for the best scene(s) over a bbox.

    Args:
        bbox: (minx, miny, maxx, maxy) in WGS84 lon/lat.
        satellite_type: "sentinel-1" or "sentinel-2".
        date_range: how many days back from now to search.
        timeout: per-request timeout in seconds.
        return_ranked: when True, return the full candidate list sorted by score
            (best first) instead of just the single best scene.
        aoi_geom: the merged risk geometry (WGS84 GeoJSON). When provided,
            coverage is scored against this polygon instead of the bbox — which
            is what actually matters when the cities are scattered across a wide,
            mostly-empty bounding box.

    Scenes are ranked coverage-aware (FIX 1): each candidate is scored
    `aoi_overlap% * (1 - cloud_cover/100)`, so a scene that covers more of the
    risk area and is less cloudy wins. This avoids picking a low-cloud tile that
    overlaps only the empty part of the bbox. For Sentinel-2 the catalogue is
    still pre-filtered to cloud cover below CLOUD_COVER_THRESHOLD.

    Each returned scene is annotated with `_score`, `_overlap` (0..1) and
    `_cloud` (percent). Returns the best scene dict (or None) by default, or the
    ranked list when `return_ranked` is True.
    """
    collection = _COLLECTION_NAMES.get(satellite_type)
    if collection is None:
        logger.error("Unknown satellite type %r", satellite_type)
        return None

    try:
        minx, miny, maxx, maxy = bbox
    except (TypeError, ValueError) as exc:
        logger.error("Invalid bbox %r: %s", bbox, exc)
        return None

    start = (datetime.now(timezone.utc) - timedelta(days=date_range)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    # OData polygon: counter-clockwise ring closing on the first vertex.
    polygon = (
        f"POLYGON(({minx} {miny},{maxx} {miny},{maxx} {maxy},"
        f"{minx} {maxy},{minx} {miny}))"
    )

    filters = [
        f"Collection/Name eq '{collection}'",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{polygon}')",
        f"ContentDate/Start gt {start}",
    ]

    if satellite_type == SENTINEL_2:
        # Filter on the cloud-cover attribute and prefer the least cloudy scene.
        filters.append(
            "Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq "
            "'cloudCover' and att/OData.CSC.DoubleAttribute/Value lt "
            f"{CLOUD_COVER_THRESHOLD})"
        )
        # Restrict to a single processing level (L1C). The catalogue returns
        # both L1C and L2A for the same tile; mixing them in a mosaic is unsafe
        # (different band naming/scaling), and the extractor targets L1C.
        filters.append("contains(Name,'MSIL1C')")
    order_by = "ContentDate/Start desc"

    params = {
        "$filter": " and ".join(filters),
        "$orderby": order_by,
        # Large enough to capture every tile intersecting the AOI in the window
        # so coverage-aware ranking is not defeated by date-ordered truncation.
        "$top": "100",
        "$expand": "Attributes",
    }

    logger.info(
        "Searching CDSE %s catalogue over bbox %s (last %d days)",
        satellite_type,
        bbox,
        date_range,
    )
    try:
        response = requests.get(CATALOGUE_URL, params=params, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Copernicus catalogue search failed: %s", exc)
        return None

    try:
        results = response.json().get("value", [])
    except ValueError as exc:
        logger.error("Could not parse catalogue response: %s", exc)
        return None

    if not results:
        logger.warning(
            "No %s scenes found over bbox %s in the last %d days",
            satellite_type,
            bbox,
            date_range,
        )
        return None

    # Coverage-aware ranking: score every candidate by AOI overlap and cloud
    # cover, then sort best-first. Annotate each scene so downstream code (the
    # mosaic decision) can read coverage without recomputing it. Coverage is
    # measured against the risk polygon when available, else the bbox.
    aoi = _aoi_geometry(bbox, aoi_geom)
    for scene in results:
        scene["_overlap"] = _scene_aoi_overlap(scene, aoi)
        scene["_cloud"] = _scene_cloud_cover(scene)
        scene["_score"] = _scene_score(scene, aoi)

    ranked = sorted(results, key=lambda s: s["_score"], reverse=True)

    best = ranked[0]
    logger.info(
        "Best %s scene: %s (score=%.3f, overlap=%.0f%%, cloud=%.1f%%)",
        satellite_type,
        best.get("Name"),
        best["_score"],
        best["_overlap"] * 100,
        best["_cloud"] if best["_cloud"] != float("inf") else 0.0,
    )

    if return_ranked:
        return ranked
    return best


def _scene_cloud_cover(scene: dict) -> float:
    """Extract a scene's cloud-cover percentage, or +inf if unavailable."""
    for attr in scene.get("Attributes", []):
        if attr.get("Name") == "cloudCover":
            try:
                return float(attr.get("Value"))
            except (TypeError, ValueError):
                return float("inf")
    return float("inf")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Mission selection by user hint only (no bbox/token -> no cloud peek).
    print("flood ->", select_satellite("flood"))
    print("earthquake ->", select_satellite("earthquake"))
    print("landslide ->", select_satellite("landslide"))
    print(
        "earthquake (forced cloudy) ->",
        select_satellite("earthquake", cloud_cover=80),
    )

    # Live auth + catalogue search smoke test (needs valid credentials).
    token = authenticate_copernicus()
    if not token:
        print("Authentication failed; skipping catalogue search")
    else:
        print(f"Got token (len={len(token)})")
        # Small bbox around Lahore, Pakistan.
        lahore_bbox = (74.2, 31.4, 74.5, 31.7)
        # Cloud-aware selection using real metadata.
        print(
            "earthquake @Lahore ->",
            select_satellite("earthquake", bbox=lahore_bbox, token=token),
        )
        scene = search_imagery(lahore_bbox, SENTINEL_2, date_range=14)
        if scene:
            print(f"Found scene: {scene.get('Name')}")
        else:
            print("No scene found")
