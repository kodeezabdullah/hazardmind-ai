"""Free terrain factors for landslide susceptibility: soil and road cuts.

Both supply MEASUREMENTS, not verdicts. That distinction is the point: we
compute susceptibility ourselves (see susceptibility.py) and these are two of
its inputs, not a hazard product we import and re-badge.

**SoilGrids (ISRIC) — lithology/texture.** After slope, soil texture is the
largest control on whether a slope fails: clay-rich material loses shear
strength dramatically when saturated, granite does not. SoilGrids publishes
free global clay/sand/silt fractions at 250 m. We read clay content and turn
it into a 0-1 weakness score.

**Overpass (OSM) — road proximity.** Hillside roads in mountain terrain are
frequently cut without engineered support, undermining the slope toe. This is
a major landslide driver in Pakistan's northern areas specifically, which is
why it earns a factor rather than a footnote.

Both are BEST-EFFORT. Susceptibility redistributes the weight of any absent
factor (see compute_susceptibility), so an unreachable service degrades the
score's completeness — reported in `factors_absent` — never the run.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
REQUEST_TIMEOUT = 30

# Clay fraction (%) at which material is treated as fully "weak" for this
# score. ~40% clay is a common threshold for problematic expansive/plastic
# soils in slope-stability work. Engineering judgement, stated as such.
CLAY_WEAK_PERCENT = 40.0


def fetch_soil_weakness(lat: float, lon: float, session=None) -> Optional[dict]:
    """Clay-derived weakness score in [0,1] at a point, or None.

    SoilGrids is queried at the AOI centroid rather than per-pixel: the
    product is 250 m and lithology varies slowly relative to a town-scale
    AOI, so a per-pixel query would cost hundreds of requests to encode
    almost the same value. The result says it is a POINT sample so no reader
    mistakes it for a spatial field.
    """
    try:
        import requests
    except ImportError:
        return None
    sess = session or requests
    try:
        resp = sess.get(
            SOILGRIDS_URL,
            params={
                "lat": lat, "lon": lon,
                "property": "clay",
                "depth": "0-5cm",
                "value": "mean",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        layers = (resp.json().get("properties") or {}).get("layers") or []
        for layer in layers:
            if layer.get("name") != "clay":
                continue
            depths = layer.get("depths") or []
            if not depths:
                continue
            val = (depths[0].get("values") or {}).get("mean")
            if val is None:
                continue
            # SoilGrids returns clay in g/kg scaled x10 -> percent = val/10.
            clay_pct = float(val) / 10.0
            score = min(1.0, max(0.0, clay_pct / CLAY_WEAK_PERCENT))
            logger.info(
                "SoilGrids: clay %.1f%% at (%.4f, %.4f) -> weakness %.3f",
                clay_pct, lat, lon, score,
            )
            return {
                "clay_percent": round(clay_pct, 2),
                "weakness_score": round(score, 4),
                "source": "SoilGrids (ISRIC) 250 m, clay 0-5cm mean",
                "sampling": "POINT sample at the AOI centroid, not a field",
                "basis": (
                    f"weakness = clay% / {CLAY_WEAK_PERCENT:.0f}, clipped to "
                    "[0,1]. Engineering judgement: ~40% clay is a common "
                    "threshold for problematic plastic soils in slope "
                    "stability. NOT a fitted relationship."
                ),
            }
    except Exception as exc:  # noqa: BLE001 — optional factor, never fatal
        logger.warning("SoilGrids unavailable (%s)", str(exc)[:120])
    return None


def _overpass_roads_query(bbox: list) -> str:
    w, s, e, n = bbox[0], bbox[1], bbox[2], bbox[3]
    bb = f"{s},{w},{n},{e}"
    # Only roads substantial enough to involve a real cut. Tracks and paths
    # are excluded: a footpath does not undermine a slope.
    return (
        "[out:json][timeout:25];\n(\n"
        f'  way["highway"="motorway"]({bb});\n'
        f'  way["highway"="trunk"]({bb});\n'
        f'  way["highway"="primary"]({bb});\n'
        f'  way["highway"="secondary"]({bb});\n'
        f'  way["highway"="tertiary"]({bb});\n'
        ");\nout geom;"
    )


def fetch_road_distance_grid(
    bbox: list,
    shape: tuple,
    session=None,
) -> Optional[np.ndarray]:
    """Distance (m) from every grid cell to the nearest substantial road.

    Returns a grid matching `shape`, or None if Overpass is unreachable or
    the AOI contains no such road (which is itself meaningful — no roads
    means no road-cut destabilisation, and susceptibility then redistributes
    that factor's weight rather than scoring a misleading zero).
    """
    try:
        import requests
        from scipy.ndimage import distance_transform_edt
    except ImportError:
        return None
    if not bbox or len(bbox) < 4:
        return None

    sess = session or requests
    elements = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            resp = sess.post(
                endpoint, data={"data": _overpass_roads_query(bbox)},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Overpass %s failed for roads (%s) — trying next",
                endpoint, str(exc)[:90],
            )
            continue
    if not elements:
        logger.info(
            "No substantial roads resolved in the AOI — the road-cut factor "
            "is omitted and its weight redistributed, rather than scored as 0"
        )
        return None

    h, w = shape
    minx, miny, maxx, maxy = bbox
    span_x = max(maxx - minx, 1e-9)
    span_y = max(maxy - miny, 1e-9)
    road = np.zeros((h, w), dtype=bool)
    n_pts = 0
    for el in elements:
        for pt in (el.get("geometry") or []):
            lon, lat = pt.get("lon"), pt.get("lat")
            if lon is None or lat is None:
                continue
            col = int((lon - minx) / span_x * (w - 1))
            row = int((maxy - lat) / span_y * (h - 1))
            if 0 <= row < h and 0 <= col < w:
                road[row, col] = True
                n_pts += 1
    if not road.any():
        return None

    # Metres per cell, from the AOI extent (latitude-corrected).
    mid_lat = (miny + maxy) / 2.0
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * float(np.cos(np.radians(mid_lat)))
    cell_m = float(np.mean([
        span_y * m_per_deg_lat / max(h, 1),
        span_x * m_per_deg_lon / max(w, 1),
    ]))
    dist = distance_transform_edt(~road) * cell_m
    logger.info(
        "Road proximity: %d road vertices rasterised, cell ~%.1f m, "
        "median distance %.0f m", n_pts, cell_m, float(np.median(dist)),
    )
    return dist.astype("float32")
