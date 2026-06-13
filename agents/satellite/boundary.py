"""Boundary fetching and merging for the satellite agent.

Fetches administrative boundaries from the Nominatim (OpenStreetMap) API and
prepares the geometry the imagery pipeline needs:

- The overall *region* boundary is used as a faded background on the map.
- The *risk city* boundaries are highlighted as an overlay.
- The merged risk-city geometry yields the bbox used to clip satellite
  imagery, so we only download/process the areas that actually matter.

Nominatim usage policy requires a descriptive User-Agent and at most one
request per second; both are honored here.

Run this file directly for a small smoke test:
    python boundary.py
"""

import logging
import time
from typing import Optional

import requests
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "HazardMind-SatelliteAgent/1.0 (disaster-response)"

# Nominatim asks callers to stay under 1 request/second.
_MIN_REQUEST_INTERVAL = 1.0
_last_request_time = 0.0


def _nominatim_search(query: str, timeout: int = 30) -> Optional[dict]:
    """Query Nominatim for a place and return its first result, or None.

    Requests the result as GeoJSON geometry. Respects the 1 req/sec policy by
    spacing consecutive calls.
    """
    global _last_request_time

    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)

    params = {
        "q": query,
        "format": "jsonv2",
        "polygon_geojson": 1,
        "limit": 1,
    }
    headers = {"User-Agent": USER_AGENT}

    logger.info("Nominatim lookup: %s", query)
    try:
        response = requests.get(
            NOMINATIM_URL, params=params, headers=headers, timeout=timeout
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Nominatim request failed for %r: %s", query, exc)
        return None
    finally:
        _last_request_time = time.monotonic()

    try:
        results = response.json()
    except ValueError as exc:
        logger.error("Could not parse Nominatim response for %r: %s", query, exc)
        return None

    if not results:
        logger.warning("No boundary found for %r", query)
        return None

    return results[0]


def get_region_boundary(location_name: str) -> Optional[dict]:
    """Fetch the overall region boundary.

    Returns a dict with the region's GeoJSON polygon and its bbox, or None if
    the lookup fails:
        {
            "name": str,
            "geojson": <GeoJSON geometry>,
            "bbox": (minx, miny, maxx, maxy),
        }

    Example: get_region_boundary("Punjab, Pakistan") -> full province boundary.
    """
    result = _nominatim_search(location_name)
    if result is None:
        return None

    geojson = result.get("geojson")
    if geojson is None:
        logger.warning("Region %r has no polygon geometry", location_name)
        return None

    try:
        geometry = shape(geojson)
    except (ValueError, AttributeError) as exc:
        logger.error("Invalid geometry for region %r: %s", location_name, exc)
        return None

    return {
        "name": result.get("display_name", location_name),
        "geojson": geojson,
        "bbox": geometry.bounds,
    }


def get_risk_city_boundaries(region_name: str, city_list: list) -> list:
    """Fetch a boundary polygon for each risk city.

    Cities are disambiguated by appending the region name to the query. Returns
    a list of dicts (one per successfully resolved city); cities that cannot be
    resolved are logged and skipped.

        [{"name": str, "geojson": <GeoJSON geometry>, "bbox": (...)}, ...]

    Example: get_risk_city_boundaries("Punjab, Pakistan", ["Lahore", "Multan"]).
    """
    boundaries = []
    for city in city_list:
        query = f"{city}, {region_name}" if region_name else city
        result = _nominatim_search(query)
        if result is None:
            logger.warning("Skipping city with no boundary: %r", city)
            continue

        geojson = result.get("geojson")
        if geojson is None:
            logger.warning("City %r has no polygon geometry", city)
            continue

        try:
            geometry = shape(geojson)
        except (ValueError, AttributeError) as exc:
            logger.error("Invalid geometry for city %r: %s", city, exc)
            continue

        boundaries.append(
            {
                "name": city,
                "geojson": geojson,
                "bbox": geometry.bounds,
            }
        )

    logger.info(
        "Resolved %d/%d risk-city boundaries", len(boundaries), len(city_list)
    )
    return boundaries


def merge_risk_boundaries(city_polygons: list) -> Optional[dict]:
    """Merge per-city polygons into a single GeoJSON geometry.

    `city_polygons` is the list returned by `get_risk_city_boundaries`. Uses
    shapely's unary_union so overlapping/adjacent city areas dissolve into one
    geometry. Returns the merged GeoJSON, or None if there is nothing to merge.
    """
    if not city_polygons:
        logger.warning("No risk-city polygons to merge")
        return None

    geometries = []
    for entry in city_polygons:
        try:
            geometries.append(shape(entry["geojson"]))
        except (ValueError, AttributeError, KeyError, TypeError) as exc:
            logger.error(
                "Skipping unmergeable geometry for %r: %s",
                entry.get("name", "<unknown>"),
                exc,
            )

    if not geometries:
        logger.error("No valid geometries available to merge")
        return None

    merged = unary_union(geometries)
    logger.info("Merged %d risk-city geometries", len(geometries))
    return mapping(merged)


def get_analysis_bbox(merged_polygon: dict) -> Optional[tuple]:
    """Return the bounding box of the merged risk geometry.

    Returns (minx, miny, maxx, maxy) — the extent used to clip satellite
    imagery — or None if the geometry is invalid.
    """
    if not merged_polygon:
        logger.warning("No merged polygon provided for bbox computation")
        return None

    try:
        geometry = shape(merged_polygon)
    except (ValueError, AttributeError) as exc:
        logger.error("Invalid merged polygon: %s", exc)
        return None

    bbox = geometry.bounds
    logger.info("Analysis bbox: %s", bbox)
    return bbox


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Place names may contain non-ASCII characters; avoid crashing on consoles
    # with a limited encoding (e.g. cp1252 on Windows).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # Simple end-to-end smoke test against the live Nominatim API.
    region = get_region_boundary("Punjab, Pakistan")
    if region:
        print(f"Region: {region['name']}")
        print(f"Region bbox: {region['bbox']}")
    else:
        print("Failed to fetch region boundary")

    cities = get_risk_city_boundaries("Punjab, Pakistan", ["Lahore", "Multan"])
    print(f"Fetched {len(cities)} city boundaries: {[c['name'] for c in cities]}")

    merged = merge_risk_boundaries(cities)
    if merged:
        analysis_bbox = get_analysis_bbox(merged)
        print(f"Merged risk-area analysis bbox: {analysis_bbox}")
    else:
        print("No merged boundary produced")
