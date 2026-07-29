"""Gridded population exposure (Phase 6a, science/full-pass).

**The gap this closes.** Population-affected was a single city-level GeoNames
administrative figure, multiplied by an LLM-asserted "2x to 5x" urbanisation
factor stated in PROMPT TEXT rather than computed, then split by fixed
20%/50% risk fractions also asserted by the LLM. No gridded population
product was used anywhere in the codebase, and the flood-extent polygon the
satellite agent vectorises never reached the population prompt at all
(SYSTEM_ANALYSIS.md H#8 — ranked the single most consequential science gap
in the system, because every NDMA response-level threshold is downstream of
this number).

**What replaces it.** A real geospatial exposure calculation: intersect a
gridded population raster with the ACTUAL hazard extent polygon, and sum the
people in the intersected cells.

**Product choice: WorldPop, not GHSL.** Both are free and both would work.
WorldPop is chosen because:
  - it publishes **per-country, per-year population COUNT rasters at 100 m**
    where each pixel value is already "people in this pixel" — summing an
    intersection is then exactly the exposure figure, with no areal
    reallocation step that could silently introduce error;
  - GHSL's GHS-POP is distributed in Mollweide 100 m/1 km tiles that would
    need reprojection to intersect a WGS84 hazard polygon, adding a
    resampling step to a quantity (a count) that does not survive
    interpolation cleanly;
  - WorldPop's constrained UN-adjusted products align to national census
    totals, which is the figure a national disaster authority (the NDMA
    consumer here) reconciles against.
The trade-off worth stating: WorldPop is a MODELLED redistribution of census
counts, not a measurement. Its per-pixel value carries real uncertainty,
especially in rapidly-growing informal settlements. It is nonetheless a
defensible geospatial estimate, which the previous LLM-asserted multiplier
was not.

Every fetch is best-effort: an unreachable raster returns a result with
`method="unavailable"` and the caller keeps its previous behaviour — this
module never fails a run and never fabricates a number.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# WorldPop 100 m constrained UN-adjusted population count, 2020 (the most
# recent globally-complete constrained release). {iso3} lower-case.
WORLDPOP_URL_TEMPLATE = (
    "/vsicurl/https://data.worldpop.org/GIS/Population/"
    "Global_2000_2020_Constrained/2020/BSGM/{ISO3}/"
    "{iso3}_ppp_2020_UNadj_constrained.tif"
)
WORLDPOP_SOURCE_LABEL = "WorldPop_ppp_2020_UNadj_constrained_100m"

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "hazardmind-population")


def _bounds_of(geom) -> tuple:
    return geom.bounds


def population_in_polygon(
    hazard_geom,
    iso3: str,
    source_url: Optional[str] = None,
) -> dict:
    """Sum WorldPop population-count pixels inside `hazard_geom` (WGS84).

    `hazard_geom` is a shapely geometry in EPSG:4326 — the satellite agent's
    vectorised hazard extent, NOT an administrative boundary. Returns:

        {"population": int|None, "method": str, "source": str|None,
         "pixels": int, "polygon_area_km2": float, "notes": str}

    `method` is one of "worldpop_polygon_intersection" (the real
    calculation) or "unavailable" (raster unreachable / no geometry) — a
    caller must branch on it rather than treating a None population as zero.
    """
    if hazard_geom is None or getattr(hazard_geom, "is_empty", True):
        return {
            "population": None,
            "method": "unavailable",
            "source": None,
            "pixels": 0,
            "polygon_area_km2": 0.0,
            "notes": "no hazard geometry supplied — exposure not computed",
        }
    if not iso3:
        return {
            "population": None,
            "method": "unavailable",
            "source": None,
            "pixels": 0,
            "polygon_area_km2": 0.0,
            "notes": "country ISO3 unknown — WorldPop is a per-country product",
        }

    url = source_url or WORLDPOP_URL_TEMPLATE.format(
        ISO3=iso3.upper(), iso3=iso3.lower()
    )
    try:
        import rasterio
        from rasterio.mask import mask as rio_mask

        with rasterio.open(url) as src:
            out, _transform = rio_mask(
                src, [hazard_geom], crop=True, filled=True, nodata=0
            )
        arr = out[0].astype("float64")
        # WorldPop uses a large negative nodata; clamp defensively so a
        # nodata sentinel can never subtract from the population total.
        arr = np.where(np.isfinite(arr) & (arr > 0), arr, 0.0)
        total = float(arr.sum())
        pixels = int((arr > 0).sum())
        return {
            "population": int(round(total)),
            "method": "worldpop_polygon_intersection",
            "source": WORLDPOP_SOURCE_LABEL,
            "pixels": pixels,
            "polygon_area_km2": round(_geodesic_area_km2(hazard_geom), 3),
            "notes": (
                "Sum of WorldPop 100 m population-count pixels whose centres "
                "fall inside the satellite-derived hazard extent. Modelled "
                "census redistribution, not a measurement."
            ),
        }
    except Exception as exc:  # noqa: BLE001 — exposure is best-effort
        logger.warning("WorldPop exposure unavailable (%s): %s", url, exc)
        return {
            "population": None,
            "method": "unavailable",
            "source": None,
            "pixels": 0,
            "polygon_area_km2": round(_geodesic_area_km2(hazard_geom), 3),
            "notes": f"WorldPop raster unreachable: {exc}",
        }


def _geodesic_area_km2(geom) -> float:
    """Equal-area (EPSG:6933) area in km^2 — same method the satellite agent
    uses, so areas are comparable across agents."""
    try:
        import geopandas as gpd

        gs = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs("EPSG:6933")
        return float(gs.iloc[0].area / 1_000_000.0)
    except Exception:  # noqa: BLE001
        try:
            # Degrees^2 -> km^2 approximation at the polygon's own latitude.
            minx, miny, maxx, maxy = geom.bounds
            lat = (miny + maxy) / 2.0
            km_per_deg_lat = 110.574
            km_per_deg_lng = 111.320 * math.cos(math.radians(lat))
            return float(geom.area * km_per_deg_lat * km_per_deg_lng)
        except Exception:  # noqa: BLE001
            return 0.0


def facilities_in_polygon(facilities: list, hazard_geom) -> dict:
    """Phase 6b: constrain "at risk" facilities to those geometrically inside
    the hazard extent.

    The pipeline fetches REAL OSM facility counts but the at-risk fraction was
    an unconstrained LLM guess with no code-side clamp against the real count
    — an LLM could (and per the audit, did) report more facilities at risk
    than exist. Each facility needs `lat`/`lon` (or `lat`/`lng`); those
    without usable coordinates are counted separately rather than silently
    dropped or silently included.

    Returns `{"inside": int, "total": int, "unlocatable": int, "method": str}`.
    """
    total = len(facilities or [])
    if hazard_geom is None or getattr(hazard_geom, "is_empty", True):
        return {
            "inside": None,
            "total": total,
            "unlocatable": 0,
            "method": "unavailable",
        }
    try:
        from shapely.geometry import Point
    except ImportError:
        return {"inside": None, "total": total, "unlocatable": 0,
                "method": "unavailable"}

    inside = 0
    unlocatable = 0
    for f in facilities or []:
        lat = f.get("lat") if isinstance(f, dict) else None
        lon = (
            (f.get("lon") or f.get("lng")) if isinstance(f, dict) else None
        )
        if lat is None or lon is None:
            unlocatable += 1
            continue
        try:
            if hazard_geom.contains(Point(float(lon), float(lat))):
                inside += 1
        except (TypeError, ValueError):
            unlocatable += 1
    return {
        "inside": inside,
        "total": total,
        "unlocatable": unlocatable,
        "method": "polygon_containment",
    }


def clamp_at_risk(llm_value, real_total: Optional[int], geometric: Optional[int]) -> dict:
    """Code-side clamp for any LLM-reported "at risk" count.

    Precedence, most to least trustworthy:
      1. the geometric intersection count (real coordinates, real polygon);
      2. the LLM figure, CLAMPED to [0, real_total] — it can never exceed the
         number of facilities that actually exist;
      3. None when neither is available.
    The applied rule is returned so the report can state which basis was used
    rather than presenting an LLM guess as a measurement.
    """
    if geometric is not None:
        return {"value": geometric, "basis": "geometric_intersection",
                "clamped": False}
    try:
        v = int(llm_value)
    except (TypeError, ValueError):
        return {"value": None, "basis": "unavailable", "clamped": False}
    if real_total is None:
        return {"value": max(0, v), "basis": "llm_unclamped_no_real_total",
                "clamped": v < 0}
    clamped = max(0, min(v, int(real_total)))
    return {
        "value": clamped,
        "basis": "llm_clamped_to_real_osm_total",
        "clamped": clamped != v,
    }
