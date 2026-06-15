"""HazardMind satellite agent — intelligent cross-validation.

The satellite result never stands on its own. ``CrossValidator`` checks it
against every external source we can reach — GDACS (global disaster alerts),
USGS (earthquakes), the scene's own cloud cover, the spectral-index physics, the
AOI coverage, and finally a Featherless "senior remote-sensing expert" opinion —
and feeds each finding into a :class:`ConfidenceTracker` as evidence or a
concern. The return value is a list of human-readable validation findings; the
durable scoring lives on the tracker.

Design rules:

* Every external call is best-effort. GDACS/USGS are public, unauthenticated
  feeds; on any network/parse error the check is *skipped* (no evidence, no
  crash) — a missing cross-check must never block a life-critical handoff.
* The validator forms opinions: a satellite extent far from GDACS's estimate is
  a HIGH concern with a reasoned hypothesis ("secondary flooding?" /
  "cloud masking?"), not a silent pass.
* The Featherless opinion reuses the proven ``SatelliteIntelligence`` model
  chain rather than a second HTTP client.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import requests

from confidence_tracker import ConfidenceTracker

logger = logging.getLogger(__name__)

# Public disaster feeds (no auth). Overridable via env if a mirror is needed.
GDACS_GEOJSON_URL = (
    os.getenv("GDACS_GEOJSON_URL")
    or "https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS4APP"
)
USGS_QUERY_URL = (
    os.getenv("USGS_QUERY_URL")
    or "https://earthquake.usgs.gov/fdsnws/event/1/query"
)

# How far (km) a feed event may be from our AOI centroid and still be "the same"
# event. GDACS/USGS report a point; our AOI is a city cluster.
_MATCH_RADIUS_KM = 250.0

# Network timeout for the public feeds. They are not on the critical path, so a
# short ceiling keeps a slow feed from stalling the pipeline.
_FEED_TIMEOUT = 15

_EARTH_RADIUS_KM = 6371.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two WGS84 points."""
    from math import asin, cos, radians, sin, sqrt

    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))


def _location_latlon(location: Any) -> Optional[tuple[float, float]]:
    """Best-effort (lat, lon) for a location argument.

    Accepts a ``(lat, lon)`` pair, a dict with ``lat``/``lon`` (or
    ``latitude``/``longitude``), or a bbox dict/tuple ``(minx, miny, maxx,
    maxy)`` whose centroid is used. Returns ``None`` when nothing usable is
    present — callers then skip the geographic feeds.
    """
    if location is None:
        return None
    # (lat, lon) or bbox tuple/list
    if isinstance(location, (tuple, list)):
        if len(location) == 2:
            try:
                return float(location[0]), float(location[1])
            except (TypeError, ValueError):
                return None
        if len(location) == 4:  # bbox (minx, miny, maxx, maxy) -> centroid
            try:
                minx, miny, maxx, maxy = (float(v) for v in location)
                return (miny + maxy) / 2.0, (minx + maxx) / 2.0
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(location, dict):
        lat = location.get("lat", location.get("latitude"))
        lon = location.get("lon", location.get("lng", location.get("longitude")))
        if lat is not None and lon is not None:
            try:
                return float(lat), float(lon)
            except (TypeError, ValueError):
                return None
        # bbox dict
        if all(k in location for k in ("minx", "miny", "maxx", "maxy")):
            try:
                return (
                    (float(location["miny"]) + float(location["maxy"])) / 2.0,
                    (float(location["minx"]) + float(location["maxx"])) / 2.0,
                )
            except (TypeError, ValueError):
                return None
    return None


class CrossValidator:
    """Validate a satellite result against all reachable external sources.

    One instance can validate many events. ``validate_all`` is the entry point;
    the per-source checks are public so they can be unit-tested (and monkey-
    patched in tests that must run offline).
    """

    def __init__(self, intelligence: Optional[Any] = None) -> None:
        # The LLM layer is optional; if not supplied we lazily build one so the
        # Featherless expert opinion still works in standalone use.
        self._intelligence = intelligence

    # ------------------------------------------------------------------ #
    # External feeds (best-effort, public)
    # ------------------------------------------------------------------ #
    def check_gdacs(self, location: Any) -> Optional[dict]:
        """Look up the nearest current GDACS event to ``location``.

        Returns ``{alert, area, magnitude, event_type, distance_km, ...}`` for
        the closest event within ``_MATCH_RADIUS_KM``, or ``None`` if the feed
        is unreachable or nothing matches. ``area`` (km^2) is GDACS's affected
        area when present — that is what we compare the satellite extent to.
        """
        latlon = _location_latlon(location)
        if latlon is None:
            logger.info("GDACS: no usable lat/lon from location=%r; skipping", location)
            return None
        lat, lon = latlon
        try:
            resp = requests.get(GDACS_GEOJSON_URL, timeout=_FEED_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("GDACS feed unavailable (%s); skipping GDACS check", exc)
            return None

        features = data.get("features") if isinstance(data, dict) else None
        if not features:
            return None

        best: Optional[dict] = None
        best_dist = _MATCH_RADIUS_KM
        for feat in features:
            geom = (feat or {}).get("geometry") or {}
            coords = geom.get("coordinates")
            if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
                continue
            try:
                ev_lon, ev_lat = float(coords[0]), float(coords[1])
            except (TypeError, ValueError):
                continue
            dist = _haversine_km(lat, lon, ev_lat, ev_lon)
            if dist <= best_dist:
                props = (feat or {}).get("properties") or {}
                best = self._gdacs_props(props, dist)
                best_dist = dist
        return best

    @staticmethod
    def _gdacs_props(props: dict, distance_km: float) -> dict:
        """Normalise the GDACS feature properties we care about."""
        # GDACS exposes a severity blob with the affected area/value; field
        # names vary, so probe the common ones and degrade to None.
        severity = props.get("severitydata") or props.get("severity") or {}
        area = None
        if isinstance(severity, dict):
            area = severity.get("severity") if severity.get("severityunit") == "km2" else None
        return {
            "alert": (props.get("alertlevel") or props.get("alertLevel") or "").upper() or None,
            "area": area,
            "magnitude": props.get("magnitude") or (severity.get("severity") if isinstance(severity, dict) else None),
            "event_type": props.get("eventtype") or props.get("eventType"),
            "name": props.get("name") or props.get("htmldescription"),
            "distance_km": round(distance_km, 1),
        }

    def check_usgs(self, location: Any, days: int = 14, min_magnitude: float = 4.5) -> Optional[dict]:
        """Look up the largest recent USGS earthquake near ``location``.

        Queries the FDSN event API within ``_MATCH_RADIUS_KM`` over the last
        ``days``. Returns ``{magnitude, place, time, distance_km}`` for the
        strongest match, or ``None`` if unreachable / nothing found.
        """
        latlon = _location_latlon(location)
        if latlon is None:
            logger.info("USGS: no usable lat/lon from location=%r; skipping", location)
            return None
        lat, lon = latlon
        params = {
            "format": "geojson",
            "latitude": lat,
            "longitude": lon,
            "maxradiuskm": _MATCH_RADIUS_KM,
            "minmagnitude": min_magnitude,
            "orderby": "magnitude",
            "limit": 5,
        }
        # Constrain the window so we match this disaster, not historical quakes.
        try:
            from datetime import datetime, timedelta, timezone

            params["starttime"] = (
                datetime.now(timezone.utc) - timedelta(days=days)
            ).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001 - time math should never block the check
            pass

        try:
            resp = requests.get(USGS_QUERY_URL, params=params, timeout=_FEED_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("USGS feed unavailable (%s); skipping USGS check", exc)
            return None

        features = data.get("features") if isinstance(data, dict) else None
        if not features:
            return None
        feat = features[0]  # ordered by magnitude desc
        props = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or [None, None]
        distance = None
        try:
            distance = round(_haversine_km(lat, lon, float(coords[1]), float(coords[0])), 1)
        except (TypeError, ValueError, IndexError):
            distance = None
        return {
            "magnitude": props.get("mag"),
            "place": props.get("place"),
            "time": props.get("time"),
            "distance_km": distance,
        }

    # ------------------------------------------------------------------ #
    # The orchestrating validator
    # ------------------------------------------------------------------ #
    def validate_all(
        self,
        satellite_result: dict,
        disaster_type: str,
        location: Any,
        tracker: ConfidenceTracker,
    ) -> list[dict]:
        """Validate ``satellite_result`` against every reachable source.

        Adds evidence/concerns to ``tracker`` as a side effect and returns a
        list of validation findings (``{source, status, detail}``) for the
        room/log. Never raises — a failing individual check is logged and
        skipped.
        """
        validations: list[dict] = []

        sat_area = _coerce_float(
            satellite_result.get("affected_area_km2", satellite_result.get("area"))
        )

        # 1. GDACS validation — compare affected-area extent.
        gdacs = None
        try:
            gdacs = self.check_gdacs(location)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GDACS check raised (%s); skipping", exc)
        if gdacs and gdacs.get("area") and sat_area:
            gdacs_area = _coerce_float(gdacs["area"])
            if gdacs_area:
                ratio = sat_area / gdacs_area
                if 0.7 <= ratio <= 1.3:
                    tracker.add_evidence("gdacs", 0.9, weight=0.3)
                    validations.append(
                        {
                            "source": "GDACS",
                            "status": "CONFIRMED",
                            "detail": "Satellite area matches GDACS within 30%",
                        }
                    )
                elif ratio > 2.0:
                    tracker.add_evidence("gdacs", 0.4, weight=0.3)
                    tracker.add_concern(
                        f"Satellite extent {ratio:.1f}x larger than GDACS", "HIGH"
                    )
                    validations.append(
                        {
                            "source": "GDACS",
                            "status": "DISCREPANCY",
                            "detail": f"Satellite {ratio:.1f}x larger — secondary flooding?",
                        }
                    )
                elif ratio < 0.5:
                    tracker.add_evidence("gdacs", 0.4, weight=0.3)
                    tracker.add_concern(
                        f"Satellite extent only {ratio:.1f}x of GDACS estimate", "HIGH"
                    )
                    validations.append(
                        {
                            "source": "GDACS",
                            "status": "DISCREPANCY",
                            "detail": "Satellite shows less than GDACS — cloud masking?",
                        }
                    )
                else:
                    # Between 0.5..0.7 or 1.3..2.0 — a soft mismatch, still useful.
                    tracker.add_evidence("gdacs", 0.65, weight=0.3)
                    validations.append(
                        {
                            "source": "GDACS",
                            "status": "PARTIAL",
                            "detail": f"Satellite/GDACS area ratio {ratio:.1f}",
                        }
                    )
        elif gdacs:
            # We matched a GDACS event but it carries no comparable area —
            # presence is still weak corroboration that *something* happened.
            tracker.add_evidence("gdacs", 0.7, weight=0.15)
            validations.append(
                {
                    "source": "GDACS",
                    "status": "EVENT_PRESENT",
                    "detail": f"GDACS {gdacs.get('alert') or '?'} alert "
                    f"{gdacs.get('distance_km')}km away (no area to compare)",
                }
            )

        # 2. USGS validation (earthquakes).
        if (disaster_type or "").lower() == "earthquake":
            usgs = None
            try:
                usgs = self.check_usgs(location)
            except Exception as exc:  # noqa: BLE001
                logger.warning("USGS check raised (%s); skipping", exc)
            if usgs and usgs.get("magnitude") is not None:
                mag = _coerce_float(usgs["magnitude"])
                if mag and mag > 6.5:
                    tracker.add_concern(
                        f"High magnitude M{mag:.1f} — expect wider damage", "HIGH"
                    )
                tracker.add_evidence("usgs", 0.9, weight=0.4)
                validations.append(
                    {
                        "source": "USGS",
                        "status": "CONFIRMED",
                        "detail": f"M{mag} quake {usgs.get('distance_km')}km away",
                    }
                )

        # 3. Cloud-cover validation.
        cloud = _coerce_float(satellite_result.get("cloud_cover"))
        if cloud is not None:
            if cloud > 60:
                tracker.add_concern(
                    f"High cloud cover {cloud:.0f}% — optical unreliable", "CRITICAL"
                )
                tracker.add_evidence("cloud_check", 0.2, weight=0.2)
            elif cloud > 30:
                tracker.add_concern(
                    f"Moderate cloud {cloud:.0f}% — partial obscuration", "MEDIUM"
                )
                tracker.add_evidence("cloud_check", 0.6, weight=0.2)
            else:
                tracker.add_evidence("cloud_check", 0.95, weight=0.2)

        # 4. Index-value validation (physics sanity check).
        if (disaster_type or "").lower() == "flood":
            ndwi = _coerce_float(satellite_result.get("mean_ndwi", satellite_result.get("mean_index")))
            water_pct = _coerce_float(satellite_result.get("water_percent")) or 0.0
            gdacs_red = bool(gdacs and gdacs.get("alert") == "RED")
            if ndwi is not None:
                if ndwi < 0 and gdacs_red:
                    tracker.add_concern(
                        "NDWI negative but GDACS RED alert — cloud interference likely",
                        "CRITICAL",
                    )
                    validations.append(
                        {
                            "source": "INDEX",
                            "status": "CONTRADICTION",
                            "detail": "NDWI shows no water yet GDACS is RED",
                        }
                    )
                elif ndwi > 0.3 and water_pct > 20:
                    tracker.add_evidence("index_validation", 0.95, weight=0.3)
                elif ndwi > 0.1:
                    tracker.add_evidence("index_validation", 0.75, weight=0.3)
                else:
                    tracker.add_evidence("index_validation", 0.4, weight=0.3)

        # 5. Coverage validation.
        coverage = _coerce_float(
            satellite_result.get("coverage_percent", satellite_result.get("valid_percent"))
        )
        if coverage is not None and coverage < 60:
            tracker.add_concern(
                f"Only {coverage:.0f}% AOI coverage — incomplete picture", "HIGH"
            )
            validations.append(
                {
                    "source": "COVERAGE",
                    "status": "PARTIAL",
                    "detail": f"{coverage:.0f}% of AOI has valid pixels",
                }
            )

        # 6. Featherless expert opinion over all the evidence so far.
        try:
            opinion = self.get_featherless_opinion(satellite_result, validations, tracker)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Featherless opinion raised (%s); skipping", exc)
            opinion = None
        if opinion:
            conf = _coerce_float(opinion.get("confidence"))
            if conf is not None:
                tracker.add_evidence("featherless_expert", conf, weight=0.25)
            for concern in opinion.get("concerns") or []:
                tracker.add_concern(str(concern), "MEDIUM")
            validations.append(
                {
                    "source": "FEATHERLESS",
                    "status": "RELIABLE" if opinion.get("reliable") else "UNRELIABLE",
                    "detail": opinion.get("recommendation") or "expert opinion recorded",
                }
            )

        return validations

    # ------------------------------------------------------------------ #
    # Featherless expert opinion
    # ------------------------------------------------------------------ #
    def _intel(self) -> Any:
        """Lazily build/return the shared intelligence layer."""
        if self._intelligence is None:
            from intelligence import SatelliteIntelligence

            self._intelligence = SatelliteIntelligence()
        return self._intelligence

    def get_featherless_opinion(
        self, result: dict, validations: list[dict], tracker: ConfidenceTracker
    ) -> Optional[dict]:
        """Ask Featherless to weigh the evidence and return an expert opinion.

        Returns ``{reliable, confidence, concerns, alert_team, recommendation}``
        or ``None`` if the LLM chain is unavailable. Routed through the existing
        ``SatelliteIntelligence`` model chain (Qwen primary — a reasoning model
        suits the judgement call).
        """
        intel = self._intel()
        prompt = f"""\
You are a senior remote sensing expert validating a disaster analysis.

Satellite analysis results:
{json.dumps(result, indent=2, default=str)}

Cross-validation findings so far:
{json.dumps(validations, indent=2, default=str)}

Current confidence: {tracker.overall_confidence():.2f}
Outstanding concerns: {json.dumps(tracker.concerns, default=str)}

Evaluate:
1. Are these results reliable?
2. Any additional concerns the automated checks missed?
3. Your confidence level (0-1)?
4. Should the team be alerted?

Return ONLY valid JSON:
{{
  "reliable": true,
  "confidence": 0.0,
  "concerns": [],
  "alert_team": false,
  "recommendation": "..."
}}"""
        return intel._complete_json(
            prompt, primary_model="Qwen/Qwen3.6-35B-A3B", max_tokens=2048
        )


def _coerce_float(value: Any) -> Optional[float]:
    """Float or ``None`` — keeps the validator robust to missing/odd fields."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    # Offline structural smoke test using a stubbed validator (no network).
    logging.basicConfig(level=logging.INFO)

    class _StubValidator(CrossValidator):
        def check_gdacs(self, location):
            return {"alert": "RED", "area": 120.0, "distance_km": 12.0}

        def check_usgs(self, location):
            return None

        def get_featherless_opinion(self, result, validations, tracker):
            return None

    v = _StubValidator()
    trk = ConfidenceTracker()
    res = {"affected_area_km2": 500.0, "cloud_cover": 10, "mean_ndwi": 0.4, "water_percent": 30, "valid_percent": 95}
    findings = v.validate_all(res, "flood", {"lat": 34.0, "lon": 71.5}, trk)
    print(json.dumps(findings, indent=2))
    print("confidence:", round(trk.overall_confidence(), 3))
    print("alert_team:", trk.should_alert_team())
