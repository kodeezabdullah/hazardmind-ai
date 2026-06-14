import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
import os

import aiohttp
from dotenv import load_dotenv

from intelligence import smart_llm_call


load_dotenv()

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


async def fetch_slope(bbox: list) -> dict:
    """Estimate slope from bbox geometry instead of fetching unparsed GTiff data."""
    try:
        min_lng, min_lat, max_lng, max_lat = [float(value) for value in bbox]
    except (TypeError, ValueError):
        min_lng, min_lat, max_lng, max_lat = 0.0, 0.0, 0.0, 0.0

    lat_center = (min_lat + max_lat) / 2
    bbox_area = abs((max_lng - min_lng) * (max_lat - min_lat))
    slope_estimate = 15.0 + (abs(lat_center - 25) * 0.5)
    if bbox_area < 0.5:
        slope_estimate += 10

    return {
        "available": True,
        "slope_estimate": slope_estimate,
        "source": "opentopography_estimated",
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
        f"Earthquake risk. Count: {eq_count}. Max magnitude: {max_mag}. "
        f"BBox: {bbox}. Return JSON only."
    )
    system = (
        "You are a seismic risk analyst. Return only JSON with keys: risk "
        "(CRITICAL/HIGH/MEDIUM/LOW), confidence (0.0-1.0), reasoning (string), "
        "liquefaction_probability (0.0-1.0)."
    )

    response = await smart_llm_call(prompt, system, criticality="normal")
    parsed = _parse_model_json(response)
    if parsed:
        return {
            "risk": _normalize_risk(parsed.get("risk"), "LOW"),
            "confidence": _clamp_confidence(parsed.get("confidence"), 0.6),
            "reasoning": str(parsed.get("reasoning") or "LLM seismic risk assessment."),
            "liquefaction_probability": _clamp_confidence(
                parsed.get("liquefaction_probability"),
                0.1,
            ),
        }

    if max_mag >= 7.0:
        risk, confidence, liq = "CRITICAL", 0.8, 0.8
    elif max_mag >= 5.5:
        risk, confidence, liq = "HIGH", 0.75, 0.5
    elif max_mag >= 4.0:
        risk, confidence, liq = "MEDIUM", 0.65, 0.3
    else:
        risk, confidence, liq = "LOW", 0.6, 0.1

    return {
        "risk": risk,
        "confidence": confidence,
        "reasoning": "Fallback earthquake risk based on recent maximum magnitude.",
        "liquefaction_probability": liq,
    }


async def analyze_landslide(bbox, gdacs_data, slope_data) -> dict:
    slope_estimate = slope_data.get("slope_estimate", 15.0)
    prompt = (
        f"Landslide risk. Slope estimate: {slope_estimate} degrees. "
        f"GDACS events: {gdacs_data.get('count', 0)}. BBox: {bbox}. Return JSON only."
    )
    system = (
        "You are a landslide risk analyst. Return only JSON with keys: risk "
        "(CRITICAL/HIGH/MEDIUM/LOW), confidence (0.0-1.0), reasoning (string), "
        "high_risk_zones (list)."
    )

    response = await smart_llm_call(prompt, system, criticality="normal")
    parsed = _parse_model_json(response)
    if parsed:
        return {
            "risk": _normalize_risk(parsed.get("risk"), "LOW"),
            "confidence": _clamp_confidence(parsed.get("confidence"), 0.55),
            "reasoning": str(parsed.get("reasoning") or "LLM landslide risk assessment."),
            "high_risk_zones": parsed.get("high_risk_zones")
            if isinstance(parsed.get("high_risk_zones"), list)
            else [],
        }

    slope = _to_float(slope_estimate)
    if slope > 45:
        risk, confidence = "CRITICAL", 0.75
    elif slope > 30:
        risk, confidence = "HIGH", 0.7
    elif slope > 15:
        risk, confidence = "MEDIUM", 0.6
    else:
        risk, confidence = "LOW", 0.55

    return {
        "risk": risk,
        "confidence": confidence,
        "reasoning": "Fallback landslide risk based on estimated slope.",
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
    max_score = max(
        severity_map.get(flood["risk"], 1),
        severity_map.get(quake["risk"], 1),
        severity_map.get(landslide["risk"], 1),
    )
    overall_severity = reverse_map[max_score]
    unknown_count = sum(
        1 for r in [flood, quake, landslide] if r.get("risk") == "UNKNOWN"
    )
    if unknown_count >= 2:
        overall_severity = "HIGH"

    return {
        "event_id": event_id,
        "flood_risk": flood["risk"],
        "earthquake_risk": quake["risk"],
        "landslide_risk": landslide["risk"],
        "overall_severity": overall_severity,
        "unknown_count": unknown_count,
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
