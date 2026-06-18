"""Real administrative-boundary resolution via geoBoundaries (with OSM fallback).

The previous boundary logic buffered a city's centre Point into an arbitrary
~6 km disk whenever Nominatim had no polygon for it — which is what actually
happens for most cities (OSM maps a city as a node, not a boundary relation).
Clipping the satellite imagery to that arbitrary circle is wrong: it is neither
the city nor any real administrative unit.

This module resolves the **real administrative polygon at the correct level**:

  * a CITY name      -> the finest available admin unit (e.g. tehsil / ADM3 in
                        Pakistan, county / ADM2 in the USA which has no ADM3)
  * a DISTRICT name  -> the district unit (ADM2)
  * a PROVINCE/state -> the province unit (ADM1)

The admin hierarchy and the number of levels VARY BY COUNTRY (PAK has ADM1-3,
USA has only ADM1-2, BGD/IND go to ADM4), so the resolver discovers which levels
exist for the place's country and maps the intent onto the finest sensible one,
climbing UP a level if the target level has no matching shape. An arbitrary
buffer-circle is kept only as a loud last resort (flagged), never the default.

Source: geoBoundaries (https://www.geoboundaries.org) gbOpen release — open data,
per-country ADM1..ADMn GeoJSON. Downloaded GeoJSON is cached in-memory per
process (the ADM3 files are multi-MB).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_USER_AGENT = "HazardMind-SatelliteAgent/1.0 (disaster-response)"
_GB_META_URL = "https://www.geoboundaries.org/api/current/gbOpen/{iso}/{adm}/"

# Admin-intent levels. ADM1 = top-level (province/state), increasing = finer.
_LEVELS = ("ADM1", "ADM2", "ADM3", "ADM4", "ADM5")

# Keyword -> intended admin level. Matched against the place token (lowercased).
# Province/state words pin ADM1; district/division words pin ADM2; anything else
# (a bare city/place name) targets the finest level available for the country.
_PROVINCE_WORDS = (
    "province", "provincial", "state", "region", "territory", "prefecture",
    "صوبہ", "صوبه",
)
_DISTRICT_WORDS = (
    "district", "division", "zila", "zilla", "ضلع", "county", "governorate",
)

# Minimal country-name -> ISO3 map for the demo/likely regions. Anything not
# here falls back to a Nominatim country lookup.
_COUNTRY_ISO3 = {
    "pakistan": "PAK",
    "bangladesh": "BGD",
    "nepal": "NPL",
    "india": "IND",
    "philippines": "PHL",
    "united states": "USA",
    "usa": "USA",
    "united states of america": "USA",
}

# In-memory caches (per process). geoBoundaries metadata + downloaded GeoJSON.
_meta_cache: dict = {}        # (iso, adm) -> metadata dict | None
_geojson_cache: dict = {}     # (iso, adm) -> FeatureCollection dict | None
_iso_cache: dict = {}         # country-name(lower) -> ISO3 | None


def _norm(text: str) -> str:
    """Lowercase, strip accents/punctuation/extra space for name matching."""
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def country_to_iso3(country: str) -> Optional[str]:
    """Resolve a country name OR ISO code to its ISO3 code — for the whole world.

    Resolution order:
      1. The small curated map (fast path for the common demo regions).
      2. pycountry — the complete ISO 3166 database (249 countries). Handles
         English names, official names, common aliases, alpha-2 ("pk") and
         alpha-3 ("PAK") inputs, plus a fuzzy search for minor spelling/format
         variations. This is what makes resolution work GLOBALLY, not just for a
         hardcoded set.
      3. Nominatim — last-resort network lookup if pycountry can't match.
    Cached per process.
    """
    if not country:
        return None
    key = _norm(country)
    if key in _iso_cache:
        return _iso_cache[key]

    # (1) Curated fast path.
    iso = _COUNTRY_ISO3.get(key)

    # (2) pycountry — global ISO 3166 coverage.
    if iso is None:
        iso = _iso3_via_pycountry(country)

    # (3) Nominatim network fallback.
    if iso is None:
        iso = _country_iso3_via_nominatim(country)

    _iso_cache[key] = iso
    return iso


def _iso3_via_pycountry(country: str) -> Optional[str]:
    """Resolve any country name / alpha-2 / alpha-3 to ISO3 via pycountry."""
    try:
        import pycountry
    except ImportError:
        return None

    raw = (country or "").strip()
    if not raw:
        return None

    # Direct lookup handles names, official names, alpha-2 and alpha-3.
    try:
        match = pycountry.countries.lookup(raw)
        if match is not None:
            return match.alpha_3
    except LookupError:
        pass

    # Fuzzy search for minor spelling/format variants (e.g. "Republic of Korea").
    try:
        results = pycountry.countries.search_fuzzy(raw)
        if results:
            return results[0].alpha_3
    except (LookupError, AttributeError):
        pass

    return None


def _country_iso3_via_nominatim(country: str) -> Optional[str]:
    """Look up a country's ISO3 via Nominatim (best-effort, may return None)."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "country": country,
                "format": "jsonv2",
                "addressdetails": 1,
                "extratags": 1,
                "limit": 1,
            },
            headers={"User-Agent": _USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        # ISO3166-1:alpha3 lives under extratags when present.
        tags = data[0].get("extratags") or {}
        iso3 = tags.get("ISO3166-1:alpha3")
        return iso3.upper() if iso3 else None
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None


def _available_levels(iso: str) -> list:
    """Which ADM levels geoBoundaries actually publishes for this country."""
    levels = []
    for adm in _LEVELS:
        meta = _metadata(iso, adm)
        if meta and meta.get("gjDownloadURL"):
            levels.append(adm)
    return levels


def _metadata(iso: str, adm: str) -> Optional[dict]:
    key = (iso, adm)
    if key in _meta_cache:
        return _meta_cache[key]
    meta = None
    try:
        resp = requests.get(
            _GB_META_URL.format(iso=iso, adm=adm),
            headers={"User-Agent": _USER_AGENT},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                data = data[0] if data else None
            meta = data or None
        elif resp.status_code == 404:
            # 404 = this level genuinely does not exist for the country; cache
            # the negative so we don't re-probe it every call.
            _meta_cache[key] = None
            return None
    except (requests.RequestException, ValueError, IndexError) as exc:
        # Transient failure — don't poison the cache; allow a retry.
        logger.warning(
            "geoBoundaries: metadata fetch failed for %s %s (%s); will retry",
            iso, adm, exc,
        )
        return None
    _meta_cache[key] = meta
    return meta


def _geojson(iso: str, adm: str) -> Optional[dict]:
    """Download (and cache in-memory) the FeatureCollection for an ADM level."""
    key = (iso, adm)
    if key in _geojson_cache:
        return _geojson_cache[key]
    fc = None
    meta = _metadata(iso, adm)
    url = meta.get("gjDownloadURL") if meta else None
    if url:
        try:
            resp = requests.get(
                url, headers={"User-Agent": _USER_AGENT}, timeout=120
            )
            resp.raise_for_status()
            fc = resp.json()
        except (requests.RequestException, ValueError) as exc:
            # Do NOT cache a transient failure — a flaky download must be
            # retryable on the next call, not permanently disable this level.
            logger.warning(
                "geoBoundaries: download failed for %s %s (%s); will retry",
                iso, adm, exc,
            )
            return None
    if fc is not None:
        _geojson_cache[key] = fc
    return fc


def _target_level(place_token: str, available: list) -> Optional[str]:
    """Map the place token's admin intent onto an available ADM level.

    province/state words -> ADM1; district/division words -> ADM2; otherwise a
    bare city/place name -> the FINEST level the country publishes.
    """
    if not available:
        return None
    norm = _norm(place_token)
    words = set(norm.split())
    if words & set(_PROVINCE_WORDS) or any(w in norm for w in _PROVINCE_WORDS):
        return "ADM1" if "ADM1" in available else available[0]
    if words & set(_DISTRICT_WORDS) or any(w in norm for w in _DISTRICT_WORDS):
        return "ADM2" if "ADM2" in available else available[-1]
    # Bare city name -> finest available unit (tehsil/ADM3 in PAK, county/ADM2 in USA).
    return available[-1]


def _strip_admin_words(place_token: str) -> str:
    """Drop trailing 'District'/'Province'/etc so 'Rawalpindi District' matches 'RAWALPINDI'."""
    norm = _norm(place_token)
    for w in (*_DISTRICT_WORDS, *_PROVINCE_WORDS):
        norm = re.sub(rf"\b{re.escape(w)}\b", " ", norm)
    return re.sub(r"\s+", " ", norm).strip()


def _match_feature(fc: dict, place_token: str) -> Optional[dict]:
    """Find the feature whose shapeName matches the place token (accent-tolerant)."""
    if not fc:
        return None
    want = _strip_admin_words(place_token)
    if not want:
        return None
    feats = fc.get("features") or []
    # Exact normalized match first, then containment either way.
    for f in feats:
        name = _norm((f.get("properties") or {}).get("shapeName", ""))
        if name and name == want:
            return f
    for f in feats:
        name = _norm((f.get("properties") or {}).get("shapeName", ""))
        if name and (want in name or name in want):
            return f
    return None


def resolve_admin_polygon(place: str, country: str) -> Optional[dict]:
    """Resolve a place to its real administrative GeoJSON geometry.

    Returns a dict ``{"geometry": <GeoJSON geometry>, "level": "ADM3",
    "shape_name": str, "source": "geoboundaries"}`` or ``None`` if no real
    boundary could be found (the caller decides on the last-resort buffer).

    The target admin level is derived from the place token (city -> finest,
    district -> ADM2, province -> ADM1) and the country's available levels;
    if the target level has no matching shape we climb UP to the next coarser
    level so we still return a REAL boundary rather than nothing.
    """
    iso = country_to_iso3(country)
    if not iso:
        logger.warning("geoBoundaries: could not resolve ISO3 for country %r", country)
        return None

    available = _available_levels(iso)
    if not available:
        logger.warning("geoBoundaries: no ADM levels published for %s", iso)
        return None

    target = _target_level(place, available)
    if target is None:
        return None

    # Try the target level, then climb up (finer -> coarser) until we match.
    start = available.index(target)
    for adm in reversed(available[: start + 1]):
        fc = _geojson(iso, adm)
        feat = _match_feature(fc, place)
        if feat is not None:
            shape_name = (feat.get("properties") or {}).get("shapeName", place)
            logger.info(
                "geoBoundaries: %r -> %s %r (%s)",
                place, adm, shape_name, iso,
            )
            return {
                "geometry": feat.get("geometry"),
                "level": adm,
                "shape_name": shape_name,
                "source": "geoboundaries",
            }

    logger.info(
        "geoBoundaries: no %s match for %r in %s (levels tried: %s)",
        target, place, iso, available[: start + 1],
    )
    return None
