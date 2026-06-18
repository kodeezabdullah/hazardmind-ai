import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os

import aiohttp
import numpy as np
from dotenv import load_dotenv

from intelligence import smart_llm_call


load_dotenv()

logger = logging.getLogger(__name__)

GDACS_API = os.getenv("GDACS_API", "https://www.gdacs.org/gdacsapi/api")
USGS_API = os.getenv("USGS_API", "https://earthquake.usgs.gov/fdsnws/event/1")


async def fetch_gdacs(bbox: list) -> dict:
    """Fetch recent GDACS flood, tsunami, and earthquake alerts for a bbox."""
    try:
        url = f"{GDACS_API}/events/geteventlist/SEARCH"
        params = {
            "eventtype": "FL,TS,EQ",
            "bbox": ",".join(str(value) for value in bbox),
            "limit": 50,
        }
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await _read_json(response)

        events = _extract_events(data)
        return {"events": events, "count": len(events), "source": "gdacs"}
    except Exception as e:
        return {"events": [], "count": 0, "source": "gdacs", "error": str(e)}


async def fetch_usgs(bbox: list, days: int = 7) -> dict:
    """Fetch USGS earthquake GeoJSON features for a bbox."""
    try:
        starttime = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        url = f"{USGS_API}/query"
        params = {
            "format": "geojson",
            "minmagnitude": 2.0,
            "starttime": starttime,
            "minlongitude": bbox[0],
            "minlatitude": bbox[1],
            "maxlongitude": bbox[2],
            "maxlatitude": bbox[3],
        }
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await _read_json(response)

        earthquakes = data.get("features", []) if isinstance(data, dict) else []
        return {"earthquakes": earthquakes, "count": len(earthquakes), "source": "usgs"}
    except Exception as e:
        return {"earthquakes": [], "count": 0, "source": "usgs", "error": str(e)}


# OpenTopoData public API — free, no-auth, global. SRTM 30m is the best global
# coverage dataset it serves. Queries return elevation (m) for a list of points.
_OPENTOPODATA_API = "https://api.opentopodata.org/v1/srtm30m"
# Grid resolution per axis for the elevation sample (NxN points over the bbox).
# 5x5 = 25 points keeps us within OpenTopoData's 100-locations/request limit and
# its ~1 req/s public rate, while giving enough samples to estimate real slope.
_DEM_GRID = 5


def _slope_from_grid(elevations: list, lats: list, lngs: list) -> float | None:
    """Compute mean terrain slope (degrees) from a grid of elevation samples.

    Uses numpy's gradient over the elevation grid, converting degree spacing to
    metres (≈111,320 m/deg lat; lng scaled by cos(lat)). Returns the mean slope
    in degrees, or None if the grid is unusable.
    """
    try:
        n = _DEM_GRID
        if len(elevations) < n * n:
            return None
        grid = np.array(elevations[: n * n], dtype=float).reshape(n, n)
        if not np.isfinite(grid).all():
            return None
        lat_span = abs(max(lats) - min(lats)) or 1e-6
        lng_span = abs(max(lngs) - min(lngs)) or 1e-6
        mean_lat = (max(lats) + min(lats)) / 2.0
        # Metres per grid step along each axis.
        dy = (lat_span / (n - 1)) * 111_320.0
        dx = (lng_span / (n - 1)) * 111_320.0 * max(0.05, math.cos(math.radians(mean_lat)))
        gy, gx = np.gradient(grid, dy, dx)
        slope_rad = np.arctan(np.sqrt(gx**2 + gy**2))
        return float(np.degrees(slope_rad).mean())
    except Exception:  # noqa: BLE001 - any math failure -> caller falls back
        return None


async def fetch_slope(bbox: list) -> dict:
    """Fetch a REAL DEM over the bbox and compute actual terrain slope.

    Samples a 5x5 grid of SRTM 30m elevations from OpenTopoData (free, no-auth,
    global) and computes the mean slope in degrees from the elevation gradient.
    This replaces the old physically-meaningless heuristic (slope from
    latitude-distance-from-25° + bbox size) that falsely flagged flat cities like
    Rawalpindi as HIGH landslide risk. Works worldwide. On any failure it returns
    `available: False` with a low conservative default rather than fabricating
    steepness — so a missing DEM never invents a landslide.
    """
    try:
        min_lng, min_lat, max_lng, max_lat = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return {"available": False, "slope_estimate": 10.0, "source": "bad_bbox_default"}

    n = _DEM_GRID
    lats, lngs, locations = [], [], []
    for i in range(n):
        lat = min_lat + (max_lat - min_lat) * (i / (n - 1))
        lats.append(lat)
    for j in range(n):
        lng = min_lng + (max_lng - min_lng) * (j / (n - 1))
        lngs.append(lng)
    # Row-major grid of "lat,lng" points.
    for lat in lats:
        for lng in lngs:
            locations.append(f"{lat:.5f},{lng:.5f}")

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        params = {"locations": "|".join(locations)}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_OPENTOPODATA_API, params=params) as response:
                response.raise_for_status()
                data = await _read_json(response)

        results = data.get("results", []) if isinstance(data, dict) else []
        elevations = [
            r.get("elevation") for r in results if r.get("elevation") is not None
        ]
        grid_lats = [r["location"]["lat"] for r in results if r.get("location")]
        grid_lngs = [r["location"]["lng"] for r in results if r.get("location")]

        slope = _slope_from_grid(elevations, grid_lats or lats, grid_lngs or lngs)
        if slope is not None:
            return {
                "available": True,
                "slope_estimate": round(slope, 2),
                "elevation_min_m": round(min(elevations), 1) if elevations else None,
                "elevation_max_m": round(max(elevations), 1) if elevations else None,
                "samples": len(elevations),
                "source": "opentopodata_srtm30m",
            }
    except Exception as e:  # noqa: BLE001 - DEM is best-effort; never crash analysis
        logger.warning("fetch_slope DEM lookup failed: %s", e)

    # No real DEM -> conservative low default (do NOT fabricate steepness).
    return {
        "available": False,
        "slope_estimate": 10.0,
        "source": "no_dem_conservative_default",
    }


async def analyze_flood(
    bbox,
    affected_area_km2,
    mean_value,
    gdacs_data,
    satellite_type="sentinel-2",
) -> dict:
    if satellite_type == "sentinel-1":
        index_label = "SAR backscatter ratio (VV-VH)"
        index_context = "Values near 0 indicate water. Negative values mean flooding."
    else:
        index_label = "NDWI flood index"
        index_context = "Values above 0.3 indicate flooding. Above 0.5 is severe."

    prompt = (
        f"Flood risk analysis. Area: {affected_area_km2}km2. "
        f"{index_label}: {mean_value}. {index_context} "
        f"GDACS events: {gdacs_data.get('count', 0)}. "
        f"BBox: {bbox}. Return JSON only: risk, confidence, reasoning, affected_zones"
    )
    system = (
        "You are a flood risk analyst. Return only JSON with keys: risk "
        "(CRITICAL/HIGH/MEDIUM/LOW), confidence (0.0-1.0), reasoning (string), "
        "affected_zones (list)."
    )

    response = await smart_llm_call(prompt, system, criticality="normal")
    parsed = _parse_model_json(response)
    if parsed:
        return {
            "risk": _normalize_risk(parsed.get("risk"), "LOW"),
            "confidence": _clamp_confidence(parsed.get("confidence"), 0.55),
            "reasoning": str(parsed.get("reasoning") or "LLM flood risk assessment."),
            "affected_zones": parsed.get("affected_zones")
            if isinstance(parsed.get("affected_zones"), list)
            else [],
        }

    area = _to_float(affected_area_km2)
    flood_index = _to_float(mean_value)
    if area > 200 or flood_index > 0.5:
        risk, confidence = "CRITICAL", 0.7
    elif area > 100 or flood_index > 0.3:
        risk, confidence = "HIGH", 0.65
    elif area > 25:
        risk, confidence = "MEDIUM", 0.6
    else:
        risk, confidence = "LOW", 0.55

    return {
        "risk": risk,
        "confidence": confidence,
        "reasoning": "Fallback flood risk based on affected area and flood index.",
        "affected_zones": [],
    }


async def analyze_earthquake(bbox, usgs_data) -> dict:
    magnitudes = [
        feature.get("properties", {}).get("mag")
        for feature in usgs_data.get("earthquakes", [])
        if isinstance(feature, dict)
    ]
    max_mag = max((_to_float(mag) for mag in magnitudes), default=0.0)
    eq_count = usgs_data.get("count", 0)
    prompt = (
        f"Earthquake risk assessment from OBSERVED data only.\n"
        f"Recent earthquakes in area (USGS, last 7 days): count={eq_count}, "
        f"max magnitude={max_mag}.\n"
        f"BBox: {bbox}.\n"
        f"Rules: base risk ONLY on the observed count/magnitude above. "
        f"If count is 0 and max magnitude is 0, there is NO recent seismic "
        f"activity, so risk is LOW. Do NOT raise the risk from general regional "
        f"seismicity or geographic assumptions — only real recent events count.\n"
        f"Return JSON only."
    )
    system = (
        "You are a seismic risk analyst who reports ONLY what the observed data "
        "supports. Absence of recent earthquakes means LOW risk — never invent "
        "elevated risk from a region's general reputation. Return only JSON with "
        "keys: risk (CRITICAL/HIGH/MEDIUM/LOW), confidence (0.0-1.0), reasoning "
        "(string), liquefaction_probability (0.0-1.0)."
    )

    # DETERMINISTIC risk from observed seismicity. We intentionally do NOT ask an
    # LLM here: earthquake risk is a direct function of recent magnitude/count,
    # and LLMs repeatedly inflated it from a region's general reputation (e.g.
    # "Pakistan is seismically active" -> HIGH) even when USGS shows zero recent
    # events — fabricating a disaster on a no-event feed. The data decides.
    if max_mag >= 7.0:
        risk, confidence, liq = "CRITICAL", 0.85, 0.8
    elif max_mag >= 5.5:
        risk, confidence, liq = "HIGH", 0.8, 0.5
    elif max_mag >= 4.0:
        risk, confidence, liq = "MEDIUM", 0.7, 0.3
    else:
        risk, confidence, liq = "LOW", 0.85, 0.1

    return {
        "risk": risk,
        "confidence": confidence,
        "reasoning": (
            f"Seismic risk from observed USGS data: {eq_count} recent event(s), "
            f"max magnitude {max_mag}. No recent significant seismicity -> LOW."
            if risk == "LOW"
            else f"Seismic risk from observed USGS data: max magnitude {max_mag}."
        ),
        "liquefaction_probability": liq,
    }


async def analyze_landslide(bbox, gdacs_data, slope_data) -> dict:
    slope_estimate = slope_data.get("slope_estimate", 15.0)
    prompt = (
        f"Landslide risk assessment from OBSERVED data only.\n"
        f"Mean terrain slope (real DEM): {slope_estimate} degrees.\n"
        f"GDACS landslide events in area: {gdacs_data.get('count', 0)}.\n"
        f"BBox: {bbox}.\n"
        f"Rules: base risk ONLY on the slope and events above. Flat terrain "
        f"(slope < 10 degrees) with no events is LOW risk. Do NOT raise risk "
        f"from general regional assumptions — only the measured slope/events "
        f"count.\nReturn JSON only."
    )
    system = (
        "You are a landslide risk analyst who reports ONLY what the observed "
        "slope/events support. Flat terrain with no events means LOW risk — "
        "never invent elevated risk from a region's reputation. Return only JSON "
        "with keys: risk (CRITICAL/HIGH/MEDIUM/LOW), confidence (0.0-1.0), "
        "reasoning (string), high_risk_zones (list)."
    )

    # DETERMINISTIC risk from the real DEM slope. No LLM: LLMs inflated landslide
    # risk from a region's reputation even on flat terrain. We also do NOT use the
    # GDACS `count` here — the GDACS feed returns GLOBAL events (its bbox filter
    # is unreliable; e.g. it returned 93 events for Rawalpindi, all at coordinates
    # in China/Mongolia), so a raw count would falsely raise the risk. The real
    # measured slope is the trustworthy signal.
    slope = _to_float(slope_estimate)
    if slope > 45:
        risk, confidence = "CRITICAL", 0.8
    elif slope > 30:
        risk, confidence = "HIGH", 0.75
    elif slope > 15:
        risk, confidence = "MEDIUM", 0.65
    else:
        risk, confidence = "LOW", 0.8

    return {
        "risk": risk,
        "confidence": confidence,
        "reasoning": (
            f"Landslide risk from real DEM mean slope {slope:.1f}°. "
            f"Flat terrain -> LOW."
            if risk == "LOW"
            else f"Landslide risk from real DEM mean slope {slope:.1f}°."
        ),
        "high_risk_zones": [],
    }


async def run_parallel_analysis(satellite_data: dict) -> dict:
    event_id = satellite_data.get("event_id", "unknown")
    boundaries = satellite_data.get("boundaries", {})
    bbox = boundaries.get("bbox", [])
    analysis = satellite_data.get("analysis", {})
    affected_area_km2 = analysis.get("affected_area_km2", 0.0)
    mean_value = analysis.get("mean_value", 0.0)
    risk_cities = boundaries.get("risk_cities", [])
    satellite_type = satellite_data.get("satellite", {}).get("type", "sentinel-2")

    if not bbox or len(bbox) < 4:
        return {
            "event_id": event_id,
            "flood_risk": "UNKNOWN",
            "earthquake_risk": "UNKNOWN",
            "landslide_risk": "UNKNOWN",
            "overall_severity": "HIGH",
            "confidence_scores": {
                "flood": 0.0,
                "earthquake": 0.0,
                "landslide": 0.0,
            },
            "risk_polygons": {},
            "error": "Invalid bbox received from satellite agent",
        }

    fetch_results = await asyncio.gather(
        fetch_gdacs(bbox),
        fetch_usgs(bbox),
        fetch_slope(bbox),
        return_exceptions=True,
    )
    gdacs_data = (
        fetch_results[0]
        if not isinstance(fetch_results[0], Exception)
        else {"events": [], "count": 0, "source": "gdacs"}
    )
    usgs_data = (
        fetch_results[1]
        if not isinstance(fetch_results[1], Exception)
        else {"earthquakes": [], "count": 0, "source": "usgs"}
    )
    slope_data = (
        fetch_results[2]
        if not isinstance(fetch_results[2], Exception)
        else {"available": False, "slope_estimate": 15.0, "source": "estimated"}
    )

    analysis_results = await asyncio.gather(
        analyze_flood(bbox, affected_area_km2, mean_value, gdacs_data, satellite_type),
        analyze_earthquake(bbox, usgs_data),
        analyze_landslide(bbox, gdacs_data, slope_data),
        return_exceptions=True,
    )
    flood = (
        analysis_results[0]
        if not isinstance(analysis_results[0], Exception)
        else {"risk": "UNKNOWN", "confidence": 0.0, "reasoning": "task failed"}
    )
    quake = (
        analysis_results[1]
        if not isinstance(analysis_results[1], Exception)
        else {"risk": "UNKNOWN", "confidence": 0.0, "reasoning": "task failed"}
    )
    landslide = (
        analysis_results[2]
        if not isinstance(analysis_results[2], Exception)
        else {"risk": "UNKNOWN", "confidence": 0.0, "reasoning": "task failed"}
    )

    severity_map = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 1}
    reverse_map = {4: "CRITICAL", 3: "HIGH", 2: "MEDIUM", 1: "LOW"}
    # Overall severity follows the highest KNOWN risk. UNKNOWN maps to 1 (it does
    # not raise severity) — for a flood event, the earthquake/landslide checks
    # legitimately return UNKNOWN (no quake/landslide data), and that absence must
    # NOT be treated as a hazard. (Previously `unknown_count >= 2` force-set HIGH,
    # which stamped every flood-only event — even a no-flood one — as HIGH
    # severity: a systematic false alarm. Removed.)
    max_score = max(
        severity_map.get(flood["risk"], 1),
        severity_map.get(quake["risk"], 1),
        severity_map.get(landslide["risk"], 1),
    )
    overall_severity = reverse_map[max_score]
    unknown_count = sum(
        1 for r in [flood, quake, landslide] if r.get("risk") == "UNKNOWN"
    )
    # Only flag genuine uncertainty when the PRIMARY hazard itself is unknown
    # (i.e. we could not assess the disaster we were dispatched for) — surface it
    # as a concern, never as an automatic severity escalation.
    primary_unknown = flood.get("risk") == "UNKNOWN"

    return {
        "event_id": event_id,
        "flood_risk": flood["risk"],
        "earthquake_risk": quake["risk"],
        "landslide_risk": landslide["risk"],
        "overall_severity": overall_severity,
        "unknown_count": unknown_count,
        "primary_unknown": primary_unknown,
        "confidence_scores": {
            "flood": flood.get("confidence", 0.0),
            "earthquake": quake.get("confidence", 0.0),
            "landslide": landslide.get("confidence", 0.0),
        },
        "risk_polygons": {},
        "raw": {"gdacs": gdacs_data, "usgs": usgs_data, "slope": slope_data},
    }


async def _read_json(response: aiohttp.ClientResponse) -> dict:
    try:
        return await response.json(content_type=None)
    except (aiohttp.ContentTypeError, json.JSONDecodeError):
        text = await response.text()
        return json.loads(text)


def _extract_events(data) -> list:
    if isinstance(data, dict):
        for key in ("events", "features", "Event", "event"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data] if data else []
    if isinstance(data, list):
        return data
    return []


def _parse_model_json(response: str | None) -> dict | None:
    if not response:
        return None

    cleaned = response.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    return parsed if isinstance(parsed, dict) else None


def _normalize_risk(value, default: str) -> str:
    risk = str(value or "").upper()
    return risk if risk in {"CRITICAL", "HIGH", "MEDIUM", "LOW"} else default


def _clamp_confidence(value, default: float) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, confidence))


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


if __name__ == "__main__":
    async def test():
        sample = {
            "event_id": "test-123",
            "boundaries": {
                "bbox": [71.5, 33.9, 72.1, 34.3],
                "risk_cities": ["Peshawar"],
            },
            "analysis": {"affected_area_km2": 153.37, "mean_value": 0.24},
            "artifacts": {},
        }
        result = await run_parallel_analysis(sample)
        print(json.dumps(result, indent=2))

    asyncio.run(test())
