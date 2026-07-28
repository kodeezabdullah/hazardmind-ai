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
import threading
import time
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
    aoi_geom: Optional[dict] = None,
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

    **AOI-restricted cloud (2026-07-28, CHANGE 6, PARTIAL — see below).**
    `CLOUD_COVER_THRESHOLD` was previously applied to the scene's own
    metadata cloud percentage, which CDSE computes over the WHOLE TILE, not
    the AOI. A scene can be 45% cloudy across its full footprint and
    completely clear over a small town (or vice versa) — a real run selected
    the uncalibrated SAR path on a 45.9% scene-level reading without ever
    checking whether the AOI itself was obscured.

    A real AOI-restricted figure requires the scene's SCL band, which means a
    download — `select_satellite`/`_peek_cloud_cover` are, by design, a
    lightweight metadata-only catalogue query with NO download capability at
    all (that lives in `processor.py`, which imports FROM this module, not
    the other way around; wiring a real per-candidate SCL pre-fetch in here
    would mean either an import-cycle-breaking restructure or making this
    function async and threading a download-capable session through the
    selection path — a larger restructuring than this session's scope). **So
    this is intentionally documented as a partial implementation, not forced
    into a half-working shape:**
    - `scene_cloud_percent` is always reported (the pre-existing scene-level
      figure).
    - `aoi_cloud_percent` is reported ONLY when an AOI-restricted figure was
      actually computed elsewhere and passed in as `cloud_cover` alongside a
      caller-supplied provenance hint — today, nothing upstream of this
      function computes one (no caller does the SCL pre-fetch), so in
      practice `aoi_cloud_percent` is currently always `None` and
      `selection_reason` always reflects the scene-level fallback path.
    - `selection_reason` explicitly names which basis drove the decision
      (`"scene_cloud_45pct_scl_unavailable"` shape) so a reader can always
      tell which figure was used, rather than silently assuming AOI-restricted.
    - Sentinel-1 has no SCL at all, so the scene-level fallback is not a
      degraded case for S1 — it is S1's normal, permanent path.

    Returns:
        {
            "satellite_type": "sentinel-1" | "sentinel-2",
            "reason": str,                # why this mission was chosen (legacy field)
            "cloud_cover": float | None,  # observed cloud %, if known (legacy field)
            "user_hint": str,             # the disaster type, lowercased
            "scene_cloud_percent": float | None,  # CDSE whole-tile cloud %
            "aoi_cloud_percent": float | None,    # AOI-restricted cloud %, when available
            "selection_reason": str,      # which basis drove the decision
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
    # No caller anywhere in this codebase computes a real AOI-restricted
    # figure today (the documented CHANGE 6 gap — see docstring above), so
    # ANY non-None `observed` value reaching this function, whether supplied
    # directly or peeked via the metadata-only catalogue query, is a
    # scene-level figure. `scl_unavailable_reason` is therefore always set
    # once we have a value from either path.
    observed = cloud_cover
    scl_unavailable_reason = None
    if observed is None and bbox is not None:
        observed = _peek_cloud_cover(bbox, token)
        if observed is not None:
            scl_unavailable_reason = "scl_not_fetched_pre_selection"
    elif observed is not None:
        scl_unavailable_reason = "scl_not_computed_caller_supplied_scene_level"
    else:
        scl_unavailable_reason = "no_bbox_for_cloud_peek"

    aoi_cloud_percent = None  # never populated today; see docstring gap note
    scene_cloud_percent = observed

    # Step 3: cloud cover wins when we have it.
    if observed is not None:
        basis_pct = round(observed)
        if observed > CLOUD_COVER_THRESHOLD:
            satellite = SENTINEL_1
            reason = f"cloud_cover_{basis_pct}_percent"
        else:
            satellite = SENTINEL_2
            reason = f"clear_sky_cloud_cover_{basis_pct}_percent"
        selection_reason = (
            f"scene_cloud_{basis_pct}pct_scl_unavailable"
            if scl_unavailable_reason
            else f"aoi_cloud_{basis_pct}pct"
        )
    else:
        # No cloud info: trust the user hint.
        satellite = hint_satellite
        reason = f"user_hint_{disaster or 'unknown'}"
        selection_reason = f"user_hint_{disaster or 'unknown'}_no_cloud_data"

    result = {
        "satellite_type": satellite,
        "reason": reason,
        "cloud_cover": observed,
        "user_hint": disaster,
        "scene_cloud_percent": scene_cloud_percent,
        "aoi_cloud_percent": aoi_cloud_percent,
        "selection_reason": selection_reason,
    }
    logger.info(
        "Selected %s (reason=%s, selection_reason=%s, scene_cloud=%s, "
        "aoi_cloud=%s, hint=%s)",
        satellite,
        reason,
        selection_reason,
        scene_cloud_percent,
        aoi_cloud_percent,
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


def _authenticate_copernicus_full(timeout: int = 30) -> Optional[dict]:
    """Like `authenticate_copernicus`, but returns the full token response.

    Used by `TokenManager` so it can capture `refresh_token`/`expires_in`
    alongside the access token. `authenticate_copernicus` itself is left
    untouched since other call sites only need the bare token string for a
    single short-lived request.
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
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("Copernicus authentication failed: %s", exc)
        return None


class TokenManager:
    """Keeps a CDSE access token valid across a long-running pipeline.

    A single `authenticate_copernicus()` call is only good for ~10 minutes
    (CDSE Keycloak's access-token lifetime); a multi-tile S1 GRD download can
    run for tens of minutes, so a token fetched once at pipeline start expires
    mid-run and every subsequent download 401s (observed live: a ~51 min e2e
    run had exactly one "Obtained Copernicus access token" log line and every
    download after ~10 min failed with 401 Unauthorized).

    `get()` returns a token that is valid for at least `refresh_margin_seconds`
    longer, refreshing proactively (not reactively on a 401) using the
    Keycloak `refresh_token` grant when available, falling back to a full
    username/password re-auth if the refresh token itself has expired or a
    refresh attempt fails. Safe to call from multiple threads (band downloads
    can run concurrently); refresh happens under a lock so only one request
    hits Keycloak at a time and the rest wait for the new token.
    """

    # CDSE access tokens live ~10 min; refresh this long before expiry so a
    # download already in flight doesn't straddle the boundary.
    _REFRESH_MARGIN_SECONDS = 90

    def __init__(self, timeout: int = 30):
        self._timeout = timeout
        self._lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._expires_at: float = 0.0

    def _apply_response(self, payload: dict) -> Optional[str]:
        token = payload.get("access_token")
        if not token:
            logger.error("Copernicus token response contained no access_token")
            return None
        self._access_token = token
        self._refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        try:
            expires_in = float(expires_in)
        except (TypeError, ValueError):
            expires_in = 600.0  # CDSE default; conservative if missing
        self._expires_at = time.monotonic() + expires_in
        return token

    def _refresh(self) -> Optional[str]:
        if not self._refresh_token:
            return None
        data = {
            "client_id": "cdse-public",
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            response = requests.post(TOKEN_URL, data=data, timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning(
                "Copernicus token refresh failed (%s); falling back to full "
                "re-authentication",
                exc,
            )
            return None
        token = self._apply_response(payload)
        if token:
            logger.info("Refreshed Copernicus access token (refresh_token grant)")
        return token

    def get(self) -> Optional[str]:
        """Return a currently-valid access token, refreshing/re-authing as needed."""
        with self._lock:
            if self._access_token and time.monotonic() < (
                self._expires_at - self._REFRESH_MARGIN_SECONDS
            ):
                return self._access_token

            if self._access_token:  # had a token, it's just expiring soon
                token = self._refresh()
                if token:
                    return token

            payload = _authenticate_copernicus_full(timeout=self._timeout)
            if not payload:
                self._access_token = None
                return None
            token = self._apply_response(payload)
            if token:
                logger.info(
                    "Obtained Copernicus access token (full re-authentication)"
                )
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
    """Coverage- and recency-aware score for a scene.

    Base: overlap% * (1 - cloud_cover/100) — a scene that covers more of the AOI
    and is less cloudy scores higher. Cloud cover is treated as 0 when unknown
    (Sentinel-1 has none).

    Recency: the base is multiplied by an exponential-decay recency factor so
    that, all else comparable, the LATEST scene wins (this is what you want for a
    *current* disaster verification — the imagery must reflect conditions now).
    The decay is gentle (half-life ~20 days) so a far newer but nearly-empty or
    fully-clouded tile still loses to a well-covered, clear, slightly older one —
    we never trade a usable scene for a useless newer one. The score is in 0..1;
    higher is better.
    """
    overlap = _scene_aoi_overlap(scene, aoi)
    cc = _scene_cloud_cover(scene)
    if cc == float("inf"):
        cc = 0.0
    cc = max(0.0, min(100.0, cc))
    base = overlap * (1.0 - cc / 100.0)

    age = _scene_age_days(scene)
    if age is None:
        return base  # unknown date: fall back to pure coverage/cloud score
    recency = 0.5 ** (age / _RECENCY_HALFLIFE_DAYS)
    return base * recency


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


# --------------------------------------------------------------------------- #
# Acquisition-identity accessors (BUG 3 temporal coherence, BUG 4 dedup)
# --------------------------------------------------------------------------- #
def _scene_attr(scene: dict, name: str):
    """Read a CDSE OData attribute value by Name, or None."""
    for attr in scene.get("Attributes", []) or []:
        if attr.get("Name") == name:
            return attr.get("Value")
    return None


def scene_orbit_direction(scene: dict) -> Optional[str]:
    """'ASCENDING' | 'DESCENDING' (upper-cased) or None.

    For Sentinel-1, ascending and descending passes view terrain from opposite
    look directions, so their backscatter is NOT comparable and must never be
    mixed in one mosaic (BUG 3). Falls back to the S1 orbit letter in the
    product Name (e.g. ``..._A_...`` implies ascending) when the attribute is
    absent.
    """
    val = _scene_attr(scene, "orbitDirection")
    if val:
        return str(val).strip().upper()
    return None


def scene_relative_orbit(scene: dict) -> Optional[int]:
    """The relative orbit number (int) or None.

    Same relative orbit == same viewing geometry / acquisition track, so scenes
    sharing it mosaic coherently. Read from the ``relativeOrbitNumber``
    attribute.
    """
    val = _scene_attr(scene, "relativeOrbitNumber")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def scene_datetime(scene: dict) -> Optional[datetime]:
    """UTC acquisition datetime (ContentDate/Start or OriginDate), or None."""
    raw = (scene.get("ContentDate") or {}).get("Start") or scene.get("OriginDate")
    if not raw:
        return None
    txt = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        try:
            dt = datetime.fromisoformat(txt[:19] + "+00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def scene_acq_date(scene: dict):
    """UTC acquisition date (date object) or None — the calendar day of capture."""
    dt = scene_datetime(scene)
    return dt.date() if dt else None


def _base_acquisition_id(scene: dict) -> Optional[str]:
    """Stable identity of the physical acquisition, ignoring product FORMAT.

    A single Sentinel acquisition is published as multiple products — e.g. a
    standard GRD and its Cloud-Optimized (GRD-COG) twin — that share the same
    tile/date/orbit but have distinct product Ids. Downloading both is pure
    waste (BUG 4). This derives a key that COLLAPSES those twins into one:

      Sentinel-2: MGRS tile + acquisition datetime (to the second).
      Sentinel-1: mission + mode + polarisation + start/stop timestamps parsed
        from the product Name, which are identical across GRD/GRD-COG twins.

    Falls back to the product Name with the trailing format/CRC token stripped.
    """
    name = (scene.get("Name") or "").strip()
    # S2: tile + datetime uniquely identify the acquisition.
    tile = _scene_tile_id(scene)
    dt = scene_datetime(scene)
    if tile and dt:
        return f"S2:{tile}:{dt.strftime('%Y%m%dT%H%M%S')}"
    # S1: parse mission/mode/product-type/polarisation/start/stop/orbit/dataTake
    # from the product Name. The satellite letter spans A-D (S1A/B/C/D are all
    # real, in-orbit or planned Sentinel-1 platforms — matching only [AB] here
    # silently fell through to the weak fallback below for S1C/S1D scenes,
    # which is exactly why a real S1D GRD/GRD-COG twin pair was NOT deduped in
    # production). The trailing CRC/format hex + optional _COG suffix is
    # deliberately EXCLUDED from the key: it is CDSE's per-product uniqueness
    # stamp and legitimately differs between the standard and COG products of
    # the SAME physical acquisition — everything through orbit+dataTake is the
    # acquisition identity.
    m = re.search(
        r"(S1[A-D])_([A-Z]{2})_([A-Z0-9]{4})_[0-9A-Z]{4}_"
        r"(\d{8}T\d{6})_(\d{8}T\d{6})_(\d+)_([0-9A-Z]+)",
        name.upper(),
    )
    if m:
        return "S1:" + ":".join(m.groups())
    # Last resort: strip a trailing product-format hint from the Name.
    return re.sub(r"(?i)[-_](cog|dgs|core)\b.*$", "", name) or (name or None)


def dedupe_by_acquisition(scenes: list) -> list:
    """Collapse products that are the SAME physical acquisition into one (BUG 4).

    COG and non-COG (and any other format) variants of one acquisition share a
    base acquisition id; downloading more than one is wasted bandwidth on a
    guaranteed-identical clip. Keeps the first (best-ranked) product per
    acquisition id, preserving input order. Products with no derivable id are
    kept as-is (never silently dropped).
    """
    seen = set()
    out = []
    dropped = 0
    for scene in scenes:
        key = _base_acquisition_id(scene)
        if key is not None and key in seen:
            dropped += 1
            continue
        if key is not None:
            seen.add(key)
        out.append(scene)
    if dropped:
        logger.info(
            "Deduped %d duplicate acquisition product(s) (COG/non-COG twins) "
            "-> %d unique acquisition(s)",
            dropped,
            len(out),
        )
    return out


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


# Temporal-coherence tiers for reaching the caller's coverage target (BUG 3;
# per-satellite windows since 2026-07-28, CHANGE 5). Each entry is (tier
# number, +/- day window around the anchor date, require same relative
# orbit). The last tier relaxes the orbit constraint. Ascending/descending
# are NEVER mixed within a Sentinel-1 mosaic in ANY tier (enforced
# separately).
#
# Day windows are derived from MEASURED revisit cadence, not chosen as round
# numbers. S2 combined-constellation revisit over Pakistan is ~5d (existing
# tiers already match this, left as-is). S1 same-relative-orbit revisit over
# Pakistan was measured ~11 days (see ANALYSIS.md / CLAUDE.md "Tier-window
# revisit analysis", live CDSE query, 2026-07-27) — tiers narrower than one
# revisit cycle are structural near-no-ops for S1 (confirmed live: tiers 2/3
# came back exactly 0.000% coverage gain on the 2026-07-26 e2e run, per
# CLAUDE.md's "S1 coverage tiers 1-3 exactly-0.000%" entry), so S1 collapses
# the old +/-3/+/-7 intermediate steps into a single same-orbit window at
# +/-10 days instead. Re-measure the S1 figure once the post-June-2026
# constellation configuration (Sentinel-1A retired 2026-06-29) has a clean
# 90-day history — the 6-day S1C/1D repeat cycle is Europe-concentrated per
# ESA/ASF planning and does not yet apply globally.
COVERAGE_TIERS_S2 = (
    (1, 0, True),    # same acquisition date, same relative orbit
    (2, 3, True),    # within +/-3 days, same relative orbit
    (3, 7, True),    # within +/-7 days, same relative orbit
    (4, 14, False),  # within +/-14 days, any orbit (same pass direction only)
)
COVERAGE_TIERS_S1 = (
    (1, 0, True),    # same acquisition date, same relative orbit
    (2, 10, True),   # within +/-10 days (one measured revisit cycle), same orbit
    (4, 14, False),  # within +/-14 days, any orbit — tier number kept as 4
                      # for continuity with S2/DB/log-message tier numbering,
                      # even though S1 has only 3 tiers total.
)

# Back-compat alias: some call sites/tests may still import the old flat
# name. Points at the S2 tuple (the design this constant originally
# described) — new code should use `coverage_tiers_for(satellite_type)`.
COVERAGE_TIERS = COVERAGE_TIERS_S2


def coverage_tiers_for(satellite_type: str):
    """Return the per-satellite tier-window tuple for `build_coverage_tiers`."""
    return COVERAGE_TIERS_S1 if satellite_type == "sentinel-1" else COVERAGE_TIERS_S2


def build_coverage_tiers(ranked, satellite_type: str):
    """Yield ordered candidate scene groups per temporal-coherence tier (BUG 3).

    Coverage must reach the caller's target using a *temporally coherent*
    mosaic, not an arbitrary set-cover. This produces, for each tier in
    order, the list of candidate groups to try (the caller downloads+clips
    group members until real valid-pixel coverage reaches its target, then
    stops at the first tier that succeeds).

    Coherence rules:
      - The anchor is the most-recent acquisition (best for a *current*
        disaster). Tiers widen the date window around it, per-satellite (see
        `COVERAGE_TIERS_S2`/`COVERAGE_TIERS_S1` above).
      - Same-orbit tiers require the SAME relative orbit as the anchor; the
        last tier relaxes that.
      - For Sentinel-1, ascending and descending are never mixed: each tier
        yields groups split by orbit direction (the anchor's direction first).

    Returns a list of ``(tier_number, orbit_direction_or_None, [scenes])`` in
    the order they should be attempted. Scenes within a group stay best-first.
    """
    scenes = [s for s in ranked if scene_datetime(s) is not None]
    if not scenes:
        return []

    # Anchor = most recent acquisition.
    anchor = max(scenes, key=lambda s: scene_datetime(s))
    anchor_date = scene_datetime(anchor).date()
    anchor_orbit = scene_relative_orbit(anchor)
    anchor_dir = scene_orbit_direction(anchor)
    is_s1 = satellite_type == "sentinel-1"
    tiers_for_satellite = coverage_tiers_for(satellite_type)

    groups = []
    for tier, window_days, same_orbit in tiers_for_satellite:
        in_window = [
            s for s in scenes
            if abs((scene_datetime(s).date() - anchor_date).days) <= window_days
        ]
        if same_orbit and anchor_orbit is not None:
            in_window = [
                s for s in in_window
                if scene_relative_orbit(s) == anchor_orbit
            ]
        if not in_window:
            continue

        if is_s1:
            # Split by orbit direction; never mix asc/desc. Anchor's dir first.
            by_dir: dict = {}
            for s in in_window:
                d = scene_orbit_direction(s) or "UNKNOWN"
                by_dir.setdefault(d, []).append(s)
            ordered_dirs = sorted(
                by_dir, key=lambda d: (d != anchor_dir, d)
            )
            for d in ordered_dirs:
                groups.append((tier, d, by_dir[d]))
        else:
            groups.append((tier, None, in_window))
    return groups


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
        # Restrict to a single processing level: **L2A** (surface reflectance).
        # L2A carries the Scene Classification Layer (SCL) the coverage metric
        # needs for real cloud/shadow/cirrus masking (BUG 2); L1C has no SCL.
        # The catalogue returns both L1C and L2A for the same tile; mixing
        # processing levels in a mosaic is unsafe (different band naming/scaling),
        # so we pin one. NOTE: L2A is not universal for older archive dates — if
        # no L2A candidate reaches full coverage the pipeline reports that
        # explicitly rather than silently downgrading to L1C.
        filters.append("contains(Name,'MSIL2A')")
    elif satellite_type == SENTINEL_1:
        # Restrict to GRD (Ground Range Detected) products. The S1 catalogue also
        # returns RAW (level-0, `..._RAW__0S...`) and SLC products; RAW carries
        # unfocused echo data with NO VV/VH measurement GeoTIFFs, so
        # processor._extract_bands finds no bands and the (multi-GB) download is
        # wasted, then the next candidate — often also RAW — is fully downloaded
        # and fails identically. Only GRD carries the analysis-ready VV/VH TIFFs
        # the pipeline needs, so the catalogue query is filtered to it up front.
        filters.append("contains(Name,'GRD')")
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


def _scene_age_days(scene: dict) -> Optional[float]:
    """Age of a scene in days from now (UTC), or None if the date is unreadable."""
    raw = (scene.get("ContentDate") or {}).get("Start") or scene.get("OriginDate")
    if not raw:
        return None
    txt = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        # Trim sub-second precision variants the parser rejects.
        try:
            dt = datetime.fromisoformat(txt[:19] + "+00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)


# Recency half-life (days): a scene this many days older than the newest viable
# one is worth ~halved on the recency factor. Tuned so that, among scenes of
# comparable coverage/cloud, the LATEST wins — but a far newer scene that is
# nearly empty or fully clouded still loses to a well-covered slightly older one.
_RECENCY_HALFLIFE_DAYS = 20.0


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
