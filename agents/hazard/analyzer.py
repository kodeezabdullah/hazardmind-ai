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
    """Fetch recent GDACS flood, tsunami, and earthquake alerts for a bbox.

    ``query`` on the returned dict is the raw-evidence-trace record (query
    URL/params, HTTP status, latency) so a hazard_zones row can show whether
    GDACS was actually reached and what it returned, for any hazard type's
    diagnostics that wants to record "was GDACS used or deliberately
    ignored" (the landslide path deliberately ignores the count — see
    analyze_landslide's docstring for why — but that exclusion should be
    visible in the trace, not only in a code comment).
    """
    url = f"{GDACS_API}/events/geteventlist/SEARCH"
    params = {
        "eventtype": "FL,TS,EQ",
        "bbox": ",".join(str(value) for value in bbox),
        "limit": 50,
    }
    started = datetime.now(timezone.utc)
    try:
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                latency_ms = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1)
                status = response.status
                response.raise_for_status()
                data = await _read_json(response)

        events = _extract_events(data)
        return {
            "events": events,
            "count": len(events),
            "source": "gdacs",
            "query": {
                "url": url,
                "params": params,
                "http_status": status,
                "latency_ms": latency_ms,
            },
        }
    except Exception as e:
        latency_ms = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1)
        return {
            "events": [],
            "count": 0,
            "source": "gdacs",
            "error": str(e),
            "query": {
                "url": url,
                "params": params,
                "http_status": None,
                "latency_ms": latency_ms,
            },
        }


async def fetch_usgs(bbox: list, days: int = 7) -> dict:
    """Fetch USGS earthquake GeoJSON features for a bbox.

    ``query`` on the returned dict is the raw-evidence-trace record (full
    query URL with parameters, HTTP status, latency) so the earthquake
    hazard_zones row can record exactly what was asked and what came back,
    independent of re-running the pipeline.
    """
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
    started = datetime.now(timezone.utc)
    try:
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                latency_ms = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1)
                status = response.status
                response.raise_for_status()
                data = await _read_json(response)

        earthquakes = data.get("features", []) if isinstance(data, dict) else []
        return {
            "earthquakes": earthquakes,
            "count": len(earthquakes),
            "source": "usgs",
            "query": {
                "url": url,
                "params": params,
                "http_status": status,
                "latency_ms": latency_ms,
            },
        }
    except Exception as e:
        latency_ms = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1)
        return {
            "earthquakes": [],
            "count": 0,
            "source": "usgs",
            "error": str(e),
            "query": {
                "url": url,
                "params": params,
                "http_status": None,
                "latency_ms": latency_ms,
            },
        }


# OpenTopoData public API — free, no-auth, global. SRTM 30m is the best global
# coverage dataset it serves. Queries return elevation (m) for a list of points.
_OPENTOPODATA_API = "https://api.opentopodata.org/v1/srtm30m"
# Grid resolution per axis for the elevation sample (NxN points over the bbox).
# Phase 4b (science/full-pass): raised 5 -> 10. 5x5 = 25 points across a whole
# district is far too coarse to see a single steep valley — the exact local
# feature landslide risk is driven by — and pairing it with a mean made the
# statistic doubly insensitive. 10x10 = 100 points is the maximum
# OpenTopoData's public API accepts in ONE request (its documented
# 100-locations/request limit), so this quadruples spatial detail at no extra
# request cost and stays inside the ~1 req/s public rate.
_DEM_GRID = 10


def _slope_from_grid(elevations: list, lats: list, lngs: list) -> float | None:
    """Compute mean terrain slope (degrees) from a grid of elevation samples.

    Uses numpy's gradient over the elevation grid, converting degree spacing to
    metres (≈111,320 m/deg lat; lng scaled by cos(lat)). Returns the mean slope
    in degrees, or None if the grid is unusable.
    """
    result = _slope_from_grid_traced(elevations, lats, lngs)
    return result[0] if result else None


def _slope_from_grid_traced(
    elevations: list, lats: list, lngs: list
) -> tuple[float, dict] | None:
    """Same computation as ``_slope_from_grid`` but also returns the raw
    evidence trace (per-cell slopes, the m/deg conversion factor actually
    used at this latitude, and which statistic was taken) so a landslide
    hazard_zones row can show the exact arithmetic behind the verdict
    without re-running anything. Returns None on the same failure
    conditions as the untraced version (unchanged math).
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
        lng_cos_factor = max(0.05, math.cos(math.radians(mean_lat)))
        dx = (lng_span / (n - 1)) * 111_320.0 * lng_cos_factor
        gy, gx = np.gradient(grid, dy, dx)
        slope_rad = np.arctan(np.sqrt(gx**2 + gy**2))
        slope_deg_grid = np.degrees(slope_rad)
        # Phase 4b (science/full-pass): the 90th PERCENTILE replaces the mean.
        # Landslides are local: a district holding one steep unstable valley
        # and otherwise flat terrain averages to LOW under a mean, which is
        # the wrong statistic for a hazard driven by the worst slope present,
        # not the typical one. The 90th percentile reports the steep tail
        # while still rejecting a single DEM-noise outlier (which a plain max
        # would not). Both statistics are recorded so any past run's verdict
        # remains re-derivable from its own stored per-cell values.
        mean_slope = float(slope_deg_grid.mean())
        p90_slope = float(np.percentile(slope_deg_grid, 90))
        trace = {
            "per_cell_slopes_deg": [round(float(v), 3) for v in slope_deg_grid.flatten()],
            "metres_per_degree_lat": 111_320.0,
            "lng_cos_factor_at_latitude": round(lng_cos_factor, 6),
            "dy_metres_per_grid_step": round(dy, 3),
            "dx_metres_per_grid_step": round(dx, 3),
            # The statistic actually applied to the per-cell slope grid.
            "statistic": "p90",
            "mean_slope_deg": round(mean_slope, 3),
            "p90_slope_deg": round(p90_slope, 3),
            "grid_n": n,
            "resulting_value_deg": round(p90_slope, 3),
        }
        return p90_slope, trace
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

    The returned dict also carries a full raw-evidence trace (dem_query,
    dem_samples, dem_response_raw, slope_computation, fallback_used) so the
    landslide hazard_zones row can show the exact locations requested, every
    grid point's elevation, and the per-cell slope arithmetic behind the
    final verdict, re-derivable from the DB alone.
    """
    try:
        min_lng, min_lat, max_lng, max_lat = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return {
            "available": False,
            "slope_estimate": 10.0,
            "source": "bad_bbox_default",
            "dem_query": {"bbox": bbox, "error": "unparseable bbox"},
            "dem_samples": [],
            "dem_response_raw": {"all_points_returned": False, "null_points": []},
            "slope_computation": None,
            "fallback_used": {"reason": "unparseable bbox", "default_slope_deg": 10.0},
        }

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

    dem_query = {
        "requested_points": [{"lat": lat, "lng": lng} for lat in lats for lng in lngs],
        "endpoint": _OPENTOPODATA_API,
        "http_status": None,
        "latency_ms": None,
    }
    started = datetime.now(timezone.utc)

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        params = {"locations": "|".join(locations)}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_OPENTOPODATA_API, params=params) as response:
                dem_query["latency_ms"] = round(
                    (datetime.now(timezone.utc) - started).total_seconds() * 1000, 1
                )
                dem_query["http_status"] = response.status
                response.raise_for_status()
                data = await _read_json(response)

        results = data.get("results", []) if isinstance(data, dict) else []
        elevations = [
            r.get("elevation") for r in results if r.get("elevation") is not None
        ]
        grid_lats = [r["location"]["lat"] for r in results if r.get("location")]
        grid_lngs = [r["location"]["lng"] for r in results if r.get("location")]

        dem_samples = [
            {
                "lat": r.get("location", {}).get("lat"),
                "lon": r.get("location", {}).get("lng"),
                "elevation_m": r.get("elevation"),
            }
            for r in results
        ]
        null_points = [i for i, r in enumerate(results) if r.get("elevation") is None]
        dem_response_raw = {
            "requested_count": n * n,
            "returned_count": len(results),
            "all_points_returned": len(results) == n * n and not null_points,
            "null_point_indices": null_points,
        }

        traced = _slope_from_grid_traced(elevations, grid_lats or lats, grid_lngs or lngs)
        if traced is not None:
            slope, slope_computation = traced
            return {
                "available": True,
                "slope_estimate": round(slope, 2),
                "elevation_min_m": round(min(elevations), 1) if elevations else None,
                "elevation_max_m": round(max(elevations), 1) if elevations else None,
                "samples": len(elevations),
                "source": "opentopodata_srtm30m",
                "dem_query": dem_query,
                "dem_samples": dem_samples,
                "dem_response_raw": dem_response_raw,
                "slope_computation": slope_computation,
                "fallback_used": False,
            }
        return {
            "available": False,
            "slope_estimate": 10.0,
            "source": "no_dem_conservative_default",
            "dem_query": dem_query,
            "dem_samples": dem_samples,
            "dem_response_raw": dem_response_raw,
            "slope_computation": None,
            "fallback_used": {
                "reason": "DEM grid unusable (too few/non-finite samples for slope computation)",
                "default_slope_deg": 10.0,
            },
        }
    except Exception as e:  # noqa: BLE001 - DEM is best-effort; never crash analysis
        logger.warning("fetch_slope DEM lookup failed: %s", e)
        dem_query["latency_ms"] = dem_query["latency_ms"] or round(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000, 1
        )

    # No real DEM -> conservative low default (do NOT fabricate steepness).
    return {
        "available": False,
        "slope_estimate": 10.0,
        "source": "no_dem_conservative_default",
        "dem_query": dem_query,
        "dem_samples": [],
        "dem_response_raw": {"requested_count": n * n, "returned_count": 0, "all_points_returned": False, "null_point_indices": []},
        "slope_computation": None,
        "fallback_used": {"reason": "DEM fetch raised an exception", "default_slope_deg": 10.0},
    }


async def analyze_flood(
    bbox,
    affected_area_km2,
    mean_value,
    gdacs_data,
    satellite_type="sentinel-2",
    index_calibrated=None,
    index_type=None,
) -> dict:
    if satellite_type == "sentinel-1":
        index_label = "SAR backscatter ratio (VV-VH)"
        index_context = "Values near 0 indicate water. Negative values mean flooding."
    else:
        # Phase 1b: the satellite agent computes MNDWI (Xu 2006) since
        # 2026-07-29; older payloads/fallbacks may still say NDWI. Label from
        # the real index_type the payload carries — never a hardcoded name
        # that can drift from what was actually computed.
        label = index_type if index_type in ("NDWI", "MNDWI") else "MNDWI"
        index_label = f"{label} flood index"
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
            "diagnostics": {
                "branch": "llm",
                "index_value": _to_float(mean_value),
                "index_type": index_label,
                "index_calibrated": index_calibrated,
                "affected_area_km2": _to_float(affected_area_km2),
                "gdacs_count": gdacs_data.get("count", 0),
                "gdacs_used": True,
                "threshold_applied": None,
            },
        }

    area = _to_float(affected_area_km2)

    # DETERMINISTIC FALLBACK (LLM call failed). GATE B / H#4: satellite's SAR
    # index is 10*log10(raw uncalibrated GRD DN) -- always POSITIVE in this
    # codebase (no radiometric LUT), unlike real calibrated sigma0 backscatter
    # (virtually always negative dB). Applying the NDWI-scale threshold
    # (`> 0.5`/`> 0.3`) unconditionally to that value is trivially satisfied by
    # ANY positive SAR reading, mechanically producing a FALSE-CRITICAL verdict
    # on every S1 run that reaches this fallback, regardless of actual ground
    # conditions -- not a false-negative (see satellite/ANALYSIS.md, corrected
    # 2026-07-28 to match this).
    #
    # is_uncalibrated_sar: prefer the explicit index_calibrated flag carried
    # through by _normalise_satellite_payload (Gate B); if that field is
    # itself absent (e.g. an older/incomplete payload), fall back to inferring
    # from satellite_type, which is strictly worse but still correct for the
    # common S1-vs-S2 case and was the previous (bug-free-on-this-axis, just
    # unit-blind) behavior.
    is_uncalibrated_sar = (
        index_calibrated is False
        if index_calibrated is not None
        else satellite_type == "sentinel-1"
    )

    if is_uncalibrated_sar:
        # The raw index has no physical interpretation here -- do NOT invent
        # NDWI-style thresholds on it. Base the decision on affected_area_km2
        # alone, and cap confidence at 0.4 since we are deliberately excluding
        # a data source we cannot interpret.
        if area > 200:
            risk, confidence = "CRITICAL", 0.4
        elif area > 100:
            risk, confidence = "HIGH", 0.4
        elif area > 25:
            risk, confidence = "MEDIUM", 0.4
        else:
            risk, confidence = "LOW", 0.4
        return {
            "risk": risk,
            "confidence": confidence,
            "reasoning": (
                "Fallback flood risk based on affected_area_km2 only. The SAR "
                "index was excluded as uninterpretable (uncalibrated raw "
                "backscatter, no radiometric LUT/speckle filter/terrain "
                "correction) -- NDWI-scale thresholds cannot be applied to it."
            ),
            "affected_zones": [],
            "anomaly": "sar_index_excluded_uncalibrated",
            "diagnostics": {
                "branch": "deterministic_fallback_uncalibrated_sar",
                "index_value": _to_float(mean_value),
                "index_type": index_label,
                "index_calibrated": False,
                "affected_area_km2": area,
                "gdacs_count": gdacs_data.get("count", 0),
                "gdacs_used": False,
                "gdacs_ignored_reason": (
                    "SAR index is uncalibrated raw backscatter; the decision "
                    "is based on affected_area_km2 alone, so GDACS count is "
                    "recorded here for visibility but not used as evidence."
                ),
                "threshold_applied": (
                    f"affected_area_km2 > 200 -> CRITICAL / > 100 -> HIGH / "
                    f"> 25 -> MEDIUM / else LOW (area={area})"
                ),
            },
        }

    flood_index = _to_float(mean_value)
    if area > 200 or flood_index > 0.5:
        risk, confidence = "CRITICAL", 0.7
        threshold_applied = f"area>200 or index>0.5 (area={area}, index={flood_index})"
    elif area > 100 or flood_index > 0.3:
        risk, confidence = "HIGH", 0.65
        threshold_applied = f"area>100 or index>0.3 (area={area}, index={flood_index})"
    elif area > 25:
        risk, confidence = "MEDIUM", 0.6
        threshold_applied = f"area>25 (area={area})"
    else:
        risk, confidence = "LOW", 0.55
        threshold_applied = f"else LOW (area={area}, index={flood_index})"

    return {
        "risk": risk,
        "confidence": confidence,
        "reasoning": "Fallback flood risk based on affected area and flood index.",
        "affected_zones": [],
        "diagnostics": {
            "branch": "deterministic_fallback_calibrated",
            "index_value": flood_index,
            "index_type": index_label,
            "index_calibrated": index_calibrated if index_calibrated is not None else True,
            "affected_area_km2": area,
            "gdacs_count": gdacs_data.get("count", 0),
            "gdacs_used": False,
            "gdacs_ignored_reason": (
                "GDACS count is recorded for visibility but this branch's "
                "decision uses affected_area_km2/index thresholds only."
            ),
            "threshold_applied": threshold_applied,
        },
    }


async def analyze_earthquake(bbox, usgs_data) -> dict:
    magnitudes = [
        feature.get("properties", {}).get("mag")
        for feature in usgs_data.get("earthquakes", [])
        if isinstance(feature, dict)
    ]
    max_mag = max((_to_float(mag) for mag in magnitudes), default=0.0)
    eq_count = usgs_data.get("count", 0)

    # DETERMINISTIC risk from observed seismicity. We intentionally do NOT ask an
    # LLM here: earthquake risk is a direct function of recent magnitude/count,
    # and LLMs repeatedly inflated it from a region's general reputation (e.g.
    # "Pakistan is seismically active" -> HIGH) even when USGS shows zero recent
    # events — fabricating a disaster on a no-event feed. The data decides.
    #
    # (Hazard #6, SYSTEM_ANALYSIS.md: a prompt/system pair was previously built
    # here on every call and never used, since this function is fully
    # deterministic. Removed as dead code — the LLM prompt an earlier design
    # would have used is preserved below for reference/future reactivation:
    #
    #   prompt: "Earthquake risk assessment from OBSERVED data only.
    #     Recent earthquakes in area (USGS, last 7 days): count={eq_count},
    #     max magnitude={max_mag}. BBox: {bbox}. Rules: base risk ONLY on the
    #     observed count/magnitude above. If count is 0 and max magnitude is 0,
    #     there is NO recent seismic activity, so risk is LOW. Do NOT raise the
    #     risk from general regional seismicity or geographic assumptions —
    #     only real recent events count. Return JSON only."
    #   system: "You are a seismic risk analyst who reports ONLY what the
    #     observed data supports. Absence of recent earthquakes means LOW
    #     risk — never invent elevated risk from a region's general
    #     reputation. Return only JSON with keys: risk
    #     (CRITICAL/HIGH/MEDIUM/LOW), confidence (0.0-1.0), reasoning
    #     (string), liquefaction_probability (0.0-1.0)."
    # )
    # A USGS fetch failure (network error, timeout) degrades to the same
    # {"count": 0} shape as a genuine "no recent earthquakes" result. Without
    # `usgs_data.get("error")` surfacing downstream, a LOW verdict here is
    # indistinguishable from "we never actually asked USGS" — the same
    # provenance gap this function shares with analyze_landslide.
    usgs_fetch_failed = bool(usgs_data.get("error"))

    # Phase 5b (science/full-pass): DISTANCE DECAY. Previously the maximum
    # magnitude anywhere in the query radius drove the verdict, so a M6.0 at
    # 240 km and a M6.0 at 10 km produced an identical answer — which is
    # wrong: ground shaking attenuates steeply with distance. Each event is
    # now scored by an effective magnitude that decays with epicentral
    # distance, and the verdict is driven by the event with the highest
    # EFFECTIVE magnitude, recorded by its USGS event id.
    #
    # Decay form: M_eff = M - 1.5 * log10(max(R, 10) / 10)
    #   - log10-of-distance is the standard functional form of every
    #     ground-motion attenuation relation (intensity falls with the log
    #     of distance, not linearly).
    #   - the 1.5 coefficient and the 10 km saturation radius are
    #     ENGINEERING JUDGEMENT calibrated to reproduce the widely-observed
    #     ~1 intensity-unit drop per distance doubling in the near field;
    #     they are NOT taken from a named GMPE (Boore-Atkinson, Campbell-
    #     Bozorgnia etc.), and no site-condition (Vs30) or depth term is
    #     applied. A real ShakeMap MMI raster would supersede this entirely
    #     — see the Phase 5a note in SCIENCE_LOG.md for why that is deferred.
    #   - saturating below 10 km prevents log10 blowing up at R -> 0.
    #
    # MAGNITUDE-TYPE CONFLATION, now surfaced rather than hidden: USGS mixes
    # mb / ml / mw / md, which are NOT interchangeable scales (mb saturates
    # above ~6.5; ml is regional). The driving event's magnitude_type is
    # recorded in the verdict so a reader can see which scale the number is
    # on. No conversion is applied — converting between scales without the
    # station metadata would be a fabricated precision.
    driving = None
    best_eff = None
    for feature in usgs_data.get("earthquakes", []):
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties", {}) or {}
        mag = _to_float(props.get("mag"))
        if mag is None:
            continue
        coords = (feature.get("geometry", {}) or {}).get("coordinates") or []
        dist_km = None
        try:
            if bbox and len(bbox) == 4 and len(coords) >= 2:
                centroid_lng = (bbox[0] + bbox[2]) / 2.0
                centroid_lat = (bbox[1] + bbox[3]) / 2.0
                dlat = (coords[1] - centroid_lat) * 111.32
                dlng = (coords[0] - centroid_lng) * 111.32 * max(
                    0.05, math.cos(math.radians(centroid_lat))
                )
                dist_km = math.sqrt(dlat**2 + dlng**2)
        except (TypeError, ValueError, IndexError):
            dist_km = None
        if dist_km is None:
            eff = mag  # no geometry -> cannot decay; treat at face value
        else:
            eff = mag - 1.5 * math.log10(max(dist_km, 10.0) / 10.0)
        if best_eff is None or eff > best_eff:
            best_eff = eff
            driving = {
                "usgs_event_id": feature.get("id"),
                "magnitude": mag,
                "magnitude_type": props.get("magType"),
                "distance_km": round(dist_km, 2) if dist_km is not None else None,
                "effective_magnitude": round(eff, 3),
            }

    effective_mag = round(best_eff, 3) if best_eff is not None else 0.0

    # THRESHOLD BASIS — stated honestly, not falsely cited. 7.0/5.5/4.0 are
    # ENGINEERING JUDGEMENT, not cut points from a named standard. They align
    # loosely with the common descriptive bands (M>=7 "major", M5.5-7
    # "moderate-strong damaging", M4-5.5 "light, felt but rarely damaging")
    # used in general seismological communication, and the ORDERING is
    # sound; the exact values are unvalidated and no intensity scale (MMI,
    # EMS-98) is actually computed. They are now applied to the
    # DISTANCE-DECAYED effective magnitude, not the raw maximum.
    if effective_mag >= 7.0:
        risk, confidence, liq = "CRITICAL", 0.85, 0.8
        band = "effective_magnitude>=7.0 -> CRITICAL"
    elif effective_mag >= 5.5:
        risk, confidence, liq = "HIGH", 0.8, 0.5
        band = "effective_magnitude>=5.5 -> HIGH"
    elif effective_mag >= 4.0:
        risk, confidence, liq = "MEDIUM", 0.7, 0.3
        band = "effective_magnitude>=4.0 -> MEDIUM"
    else:
        risk, confidence, liq = "LOW", 0.85, 0.1
        band = "effective_magnitude<4.0 -> LOW"
    threshold_applied = (
        f"{band} (effective_mag={effective_mag}, raw_max_mag={max_mag}"
        + (
            f", driven by {driving['usgs_event_id']} M{driving['magnitude']}"
            f" [{driving['magnitude_type']}] at {driving['distance_km']}km"
            if driving else ""
        )
        + "; distance decay M-1.5*log10(R/10), engineering judgement)"
    )

    # events_returned: capped at the 20 LARGEST-by-magnitude events, with the
    # true total count recorded separately, so a seismically busy region
    # can't bloat the hazard_zones row.
    events_with_props = [
        f for f in usgs_data.get("earthquakes", []) if isinstance(f, dict)
    ]

    def _event_record(feature: dict) -> dict:
        props = feature.get("properties", {}) or {}
        geom = feature.get("geometry", {}) or {}
        coords = geom.get("coordinates") or [None, None, None]
        distance_km = None
        try:
            if bbox and len(bbox) == 4 and coords[0] is not None and coords[1] is not None:
                centroid_lng = (bbox[0] + bbox[2]) / 2.0
                centroid_lat = (bbox[1] + bbox[3]) / 2.0
                dlat = (coords[1] - centroid_lat) * 111.32
                dlng = (coords[0] - centroid_lng) * 111.32 * max(
                    0.05, math.cos(math.radians(centroid_lat))
                )
                distance_km = round(math.sqrt(dlat**2 + dlng**2), 2)
        except (TypeError, ValueError, IndexError):
            distance_km = None
        return {
            "usgs_event_id": feature.get("id"),
            "magnitude": _to_float(props.get("mag")),
            "magnitude_type": props.get("magType"),
            "depth_km": coords[2] if len(coords) > 2 else None,
            "time": props.get("time"),
            "distance_from_aoi_centroid_km": distance_km,
        }

    events_sorted = sorted(
        events_with_props,
        key=lambda f: _to_float((f.get("properties") or {}).get("mag")),
        reverse=True,
    )
    top_20 = [_event_record(f) for f in events_sorted[:20]]
    driving_event = top_20[0] if top_20 else None

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
        # Provenance: distinguishes "USGS was queried and found nothing" from
        # "USGS could not be reached" — both currently produce the same LOW
        # risk/reasoning text without this.
        "evidence_basis": {
            "eq_count": eq_count,
            "max_magnitude": max_mag,
            # Phase 5b: the verdict is driven by distance-decayed effective
            # magnitude, not the raw maximum. The specific event that drove
            # it is named (USGS id) with its magnitude_type, so the
            # mb/ml/mw conflation is visible rather than hidden.
            "effective_magnitude": effective_mag,
            "verdict_driving_event": driving,
            "distance_decay_model": "M_eff = M - 1.5*log10(max(R_km,10)/10)",
            "distance_decay_basis": (
                "engineering judgement; log-distance form matches standard "
                "attenuation relations but the 1.5 coefficient / 10 km "
                "saturation are not from a named GMPE, and no site or depth "
                "term is applied"
            ),
            "usgs_source": usgs_data.get("source"),
            "usgs_fetch_failed": usgs_fetch_failed,
            "usgs_error": usgs_data.get("error"),
        },
        "diagnostics": {
            "usgs_query": usgs_data.get("query"),
            "events_returned": top_20,
            "events_returned_total_count": len(events_with_props),
            "max_magnitude": max_mag,
            "max_magnitude_driving_event_id": (
                driving_event.get("usgs_event_id") if driving_event else None
            ),
            "magnitude_type": (
                driving_event.get("magnitude_type") if driving_event else None
            ),
            "threshold_applied": threshold_applied,
            "usgs_fetch_failed": usgs_fetch_failed,
        },
    }


async def analyze_landslide(bbox, gdacs_data, slope_data) -> dict:
    slope_estimate = slope_data.get("slope_estimate", 15.0)

    # DETERMINISTIC risk from the real DEM slope. No LLM: LLMs inflated landslide
    # risk from a region's reputation even on flat terrain. We also do NOT use the
    # GDACS `count` here — the GDACS feed returns GLOBAL events (its bbox filter
    # is unreliable; e.g. it returned 93 events for Rawalpindi, all at coordinates
    # in China/Mongolia), so a raw count would falsely raise the risk. The real
    # measured slope is the trustworthy signal.
    #
    # (Hazard #6, SYSTEM_ANALYSIS.md: a prompt/system pair was previously built
    # here on every call and never used, since this function is fully
    # deterministic. Removed as dead code — preserved below for reference:
    #
    #   prompt: "Landslide risk assessment from OBSERVED data only. Mean
    #     terrain slope (real DEM): {slope_estimate} degrees. GDACS landslide
    #     events in area: {gdacs_data.get('count', 0)}. BBox: {bbox}. Rules:
    #     base risk ONLY on the slope and events above. Flat terrain (slope <
    #     10 degrees) with no events is LOW risk. Do NOT raise risk from
    #     general regional assumptions — only the measured slope/events
    #     count. Return JSON only."
    #   system: "You are a landslide risk analyst who reports ONLY what the
    #     observed slope/events support. Flat terrain with no events means
    #     LOW risk — never invent elevated risk from a region's reputation.
    #     Return only JSON with keys: risk (CRITICAL/HIGH/MEDIUM/LOW),
    #     confidence (0.0-1.0), reasoning (string), high_risk_zones (list)."
    # )
    # Phase 4b/5c (science/full-pass): `slope` is now the 90th-PERCENTILE
    # slope over a 10x10 DEM grid (was the mean over 5x5) — see
    # _slope_from_grid_traced. Landslides are driven by the steepest terrain
    # present, not the district average.
    #
    # THRESHOLD BASIS — stated honestly rather than cited falsely. These cut
    # points are ENGINEERING JUDGEMENT, not values taken from a named
    # geotechnical standard. What can be said for them:
    #   - 30 deg is the approximate angle of repose for loose granular
    #     material (typically ~30-37 deg for dry sand/gravel), above which
    #     unconsolidated slope material is at or beyond its natural
    #     stability limit. That makes >30 a defensible HIGH boundary in
    #     ORDER-OF-MAGNITUDE terms.
    #   - 45 deg is well beyond the repose angle of essentially any
    #     unconsolidated material, so slopes above it hold only where
    #     bedrock or cohesion carries them — failure there is
    #     high-consequence. Defensible as CRITICAL in the same qualitative
    #     sense.
    #   - 15 deg is NOT tied to any physical limit. It is a screening floor
    #     chosen so that near-flat terrain reports LOW.
    # No slope-stability analysis (factor-of-safety, soil strength,
    # pore-pressure) is performed anywhere in this codebase, and none of
    # these numbers are calibrated against a landslide inventory. Treat the
    # ORDERING as meaningful and the exact numbers as unvalidated.
    slope = _to_float(slope_estimate)
    if slope > 45:
        risk, confidence = "CRITICAL", 0.8
        threshold_applied = (
            f"p90_slope>45 -> CRITICAL (slope={slope:.2f}; engineering "
            "judgement, far above any unconsolidated repose angle)"
        )
    elif slope > 30:
        risk, confidence = "HIGH", 0.75
        threshold_applied = (
            f"p90_slope>30 -> HIGH (slope={slope:.2f}; ~angle of repose for "
            "loose granular material)"
        )
    elif slope > 15:
        risk, confidence = "MEDIUM", 0.65
        threshold_applied = (
            f"p90_slope>15 -> MEDIUM (slope={slope:.2f}; screening floor, "
            "no physical basis claimed)"
        )
    else:
        risk, confidence = "LOW", 0.8
        threshold_applied = (
            f"p90_slope<=15 -> LOW (slope={slope:.2f}; screening floor, "
            "no physical basis claimed)"
        )

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
        # Provenance: a LOW verdict from genuinely flat terrain (real DEM
        # sample) must be distinguishable from a LOW verdict produced by the
        # conservative 10.0-degree default when the DEM call failed —
        # currently both look identical downstream.
        "evidence_basis": {
            "slope_estimate": slope,
            "dem_available": bool(slope_data.get("available")),
            "dem_source": slope_data.get("source"),
            "elevation_min_m": slope_data.get("elevation_min_m"),
            "elevation_max_m": slope_data.get("elevation_max_m"),
            "sample_count": slope_data.get("samples"),
        },
        "diagnostics": {
            "dem_query": slope_data.get("dem_query"),
            "dem_samples": slope_data.get("dem_samples"),
            "dem_response_raw": slope_data.get("dem_response_raw"),
            "slope_computation": slope_data.get("slope_computation"),
            "threshold_applied": threshold_applied,
            "fallback_used": slope_data.get("fallback_used", False),
            # GDACS is deliberately EXCLUDED from the landslide decision (its
            # bbox filter is unreliable — see the comment above, e.g. it
            # returned 93 "Rawalpindi" events all located in China/Mongolia).
            # This makes that exclusion visible in the trace itself, not
            # only in a code comment.
            "gdacs_count": gdacs_data.get("count", 0),
            "gdacs_query": gdacs_data.get("query"),
            "gdacs_used": False,
            "gdacs_ignored_reason": (
                "GDACS's bbox filter is unreliable for landslide events "
                "(observed returning events located in a different country "
                "entirely) -- the real measured DEM slope is the only "
                "signal used for this hazard type."
            ),
        },
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
    satellite_confidence = _to_float(analysis.get("confidence")) if analysis.get("confidence") is not None else None
    index_calibrated = analysis.get("index_calibrated")

    if not bbox or len(bbox) < 4:
        # A plumbing failure (no valid bbox handed off from satellite) is NOT
        # a disaster signal. Previously this returned overall_severity="HIGH"
        # in the same dict as an honest "error" string -- a fake HIGH that
        # passed quality_check unflagged (a valid enum value) and could flow
        # straight into an NDMA response-level escalation with zero real
        # disaster behind it (SYSTEM_ANALYSIS.md H#3 / Section E.1). Use an
        # explicit UNKNOWN severity + insufficient_data status instead, so a
        # plumbing failure can never masquerade as a real HIGH-severity
        # verdict downstream.
        return {
            "event_id": event_id,
            "flood_risk": "UNKNOWN",
            "earthquake_risk": "UNKNOWN",
            "landslide_risk": "UNKNOWN",
            "overall_severity": "UNKNOWN",
            "status": "insufficient_data",
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
        analyze_flood(bbox, affected_area_km2, mean_value, gdacs_data, satellite_type, index_calibrated, analysis.get("index_type")),
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

    # FLOOD ONLY: flood risk is derived directly from the satellite index, so a
    # flood conclusion cannot be more confident than the satellite data it rests
    # on — cap it at the satellite's own confidence. Earthquake and landslide
    # self-source from USGS/DEM and are intentionally NOT capped (they are
    # independent of satellite confidence per root_cause.md).
    confidence_cap_applied = False
    if satellite_confidence is not None and flood.get("confidence") is not None:
        flood_confidence = _to_float(flood.get("confidence"))
        if satellite_confidence < flood_confidence:
            flood = {**flood, "confidence": satellite_confidence}
            confidence_cap_applied = True

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
        "satellite_confidence": satellite_confidence,
        "confidence_cap_applied": confidence_cap_applied,
        "risk_polygons": {},
        # Evidence provenance for earthquake/landslide (see analyze_earthquake/
        # analyze_landslide) — lets a downstream reader tell a genuine
        # no-seismicity/flat-terrain verdict apart from a fetch failure that
        # degraded to the same conservative default.
        "evidence_basis": {
            "earthquake": quake.get("evidence_basis"),
            "landslide": landslide.get("evidence_basis"),
        },
        "raw": {"gdacs": gdacs_data, "usgs": usgs_data, "slope": slope_data},
        # H#4: surfaced only when the deterministic flood fallback excluded an
        # uncalibrated SAR index rather than misapplying NDWI thresholds to it.
        "anomalies": [flood["anomaly"]] if flood.get("anomaly") else [],
        # Raw evidence trace (durable-evidence-trail, feat/durable-evidence-trail):
        # per-hazard-type diagnostics so any deterministic decision here is
        # re-derivable from what's persisted in hazard_zones.diagnostics
        # without re-running the pipeline. See analyze_flood/analyze_earthquake/
        # analyze_landslide's own "diagnostics" keys for what each carries.
        "raw_diagnostics": {
            "flood": flood.get("diagnostics"),
            "earthquake": quake.get("diagnostics"),
            "landslide": landslide.get("diagnostics"),
        },
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
