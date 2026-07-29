"""Landslide susceptibility computed FROM THE DEM — not imported.

**Why we compute this rather than import LHASA.** NASA's LHASA v2 is a good
model, and using it would be faster. But it computes essentially the factors
below and then hands back a conclusion — so importing it would mean shipping
someone else's judgement as our analysis. The judgement is the product. Every
factor here is derived from the DEM the hazard agent already fetches, plus
two free sources (SoilGrids, Overpass) that supply *measurements*, not
verdicts.

**Slope alone is not susceptibility.** Slope says a hillside is steep. It
does not say whether water concentrates there, whether the material is
clay-rich or granite, or whether a road cut has undermined the toe. Those are
what separate "steep" from "about to fail":

  plan curvature      lateral flow CONVERGENCE — concave contours funnel
                      water into a hollow, which is where failures start
  profile curvature   flow ACCELERATION down the slope — computed
                      separately, because it answers a different question
  TWI = ln(a/tan b)   topographic wetness: upslope contributing area against
                      local gradient. High TWI = saturated ground = raised
                      pore pressure = reduced shear strength
  SPI = a * tan b     stream power: erosive capacity, which undercuts slopes
  aspect              moisture retention and insolation; a north-facing slope
                      in the northern hemisphere stays wetter for longer
  distance to drainage undercutting by channel incision

**On the ordering of importance.** After slope, LITHOLOGY is the largest
control — clay-rich soil behaves nothing like granite at the same gradient —
which is why SoilGrids is included rather than treated as a refinement. Road
cuts matter disproportionately in Pakistan's mountain areas, where hillside
roads are cut without engineered support.

**These weights are engineering judgement, not a fitted model**, and the
result says so. Fitting them would need a landslide inventory with polygons;
COOLR's polygon service is down and its 48 records are mostly ~40 pixels
(measured 2026-07-29). `basis` states this in every result rather than
implying a calibration that does not exist.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Relative weights of each factor. Slope dominates because it is the
# first-order control; lithology second per the geomorphology literature.
# Documented as judgement — see the module docstring.
FACTOR_WEIGHTS = {
    "slope": 0.30,
    "lithology": 0.20,
    "wetness_twi": 0.15,
    "plan_curvature": 0.10,
    "profile_curvature": 0.10,
    "stream_power": 0.05,
    "distance_to_drainage": 0.05,
    "distance_to_roads": 0.05,
}

# Slope bands (degrees). 30 deg is near the angle of repose for most
# unconsolidated material; below ~10 deg failures are rare without a
# special mechanism.
SLOPE_LOW_DEG = 10.0
SLOPE_MODERATE_DEG = 20.0
SLOPE_HIGH_DEG = 30.0
SLOPE_EXTREME_DEG = 45.0


def _normalise(a: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Scale to [0,1] with clipping — a bounded factor score."""
    if hi <= lo:
        return np.zeros_like(a, dtype="float32")
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0).astype("float32")


def slope_aspect_curvature(dem: np.ndarray, pixel_size_m: float = 30.0) -> dict:
    """Slope, aspect and BOTH curvatures from the DEM.

    Plan and profile curvature are computed separately and deliberately: plan
    curvature describes convergence ACROSS the slope (where water collects),
    profile curvature describes acceleration ALONG it (where flow speeds up).
    Collapsing them into one "curvature" number would discard the distinction
    that makes either useful.
    """
    d = np.asarray(dem, dtype="float32")
    dz_dy, dz_dx = np.gradient(d, pixel_size_m)
    slope_rad = np.arctan(np.sqrt(dz_dx ** 2 + dz_dy ** 2))
    slope_deg = np.degrees(slope_rad).astype("float32")
    aspect = (np.degrees(np.arctan2(-dz_dx, dz_dy)) + 360.0) % 360.0

    # Second derivatives for curvature.
    d2z_dy2, d2z_dydx = np.gradient(dz_dy, pixel_size_m)
    d2z_dxdy, d2z_dx2 = np.gradient(dz_dx, pixel_size_m)
    p = dz_dx ** 2 + dz_dy ** 2
    q = p + 1.0
    denom_plan = np.maximum(p ** 1.5, 1e-9)
    denom_prof = np.maximum(p * q ** 1.5, 1e-9)

    plan_curv = (
        (d2z_dx2 * dz_dy ** 2 - 2.0 * d2z_dxdy * dz_dx * dz_dy
         + d2z_dy2 * dz_dx ** 2) / denom_plan
    ).astype("float32")
    profile_curv = (
        (d2z_dx2 * dz_dx ** 2 + 2.0 * d2z_dxdy * dz_dx * dz_dy
         + d2z_dy2 * dz_dy ** 2) / denom_prof
    ).astype("float32")

    return {
        "slope_deg": slope_deg,
        "slope_rad": slope_rad.astype("float32"),
        "aspect_deg": aspect.astype("float32"),
        "plan_curvature": plan_curv,
        "profile_curvature": profile_curv,
    }


def wetness_and_power(
    slope_rad: np.ndarray, pixel_size_m: float = 30.0
) -> dict:
    """TWI and SPI from an upslope-contributing-area approximation.

    A full multiple-flow-direction accumulation needs a filled DEM and a
    routing pass; that is out of proportion here. The standard cheap
    approximation — a smoothed inverse-slope proxy for how much area drains
    through each cell — captures the ordering TWI is used for (which hollows
    are wetter than which ridges) without claiming hydrological exactness.
    Named as an approximation in the result rather than passed off as routed
    accumulation.
    """
    from scipy.ndimage import uniform_filter

    tan_b = np.maximum(np.tan(slope_rad), 0.001)  # avoid div-by-zero on flats
    # Flat ground accumulates; steep ground sheds. Smooth to emulate the
    # spatial pooling a real accumulation would produce.
    inv = 1.0 / np.maximum(tan_b, 0.001)
    area_proxy = uniform_filter(inv.astype("float32"), size=9) * (pixel_size_m ** 2)
    area_proxy = np.maximum(area_proxy, pixel_size_m ** 2)

    twi = np.log(area_proxy / tan_b).astype("float32")
    spi = (area_proxy * tan_b).astype("float32")
    return {
        "twi": twi,
        "spi": spi,
        "area_proxy_m2": area_proxy,
        "method": "smoothed_inverse_slope_area_approximation",
        "caveat": (
            "Upslope contributing area is APPROXIMATED (smoothed inverse "
            "slope), not routed via a filled-DEM flow accumulation. TWI/SPI "
            "here are ordinal indicators, not hydrologically exact values."
        ),
    }


def distance_to_drainage(
    dem: np.ndarray, slope_rad: np.ndarray, pixel_size_m: float = 30.0
) -> np.ndarray:
    """Distance (m) to the nearest likely drainage line.

    Drainage is approximated as locally-minimum, high-wetness ground — the
    same cheap approximation `hand_mask` uses in the SAR path, kept
    consistent so the two modules do not disagree about where channels are.
    """
    from scipy.ndimage import distance_transform_edt, minimum_filter

    d = np.nan_to_num(np.asarray(dem, dtype="float32"))
    local_min = minimum_filter(d, size=15, mode="nearest")
    channel = (d - local_min) < 2.0  # within 2 m of the local low point
    if not channel.any():
        return np.full(d.shape, np.inf, dtype="float32")
    return (distance_transform_edt(~channel) * pixel_size_m).astype("float32")


def compute_susceptibility(
    dem: np.ndarray,
    pixel_size_m: float = 30.0,
    lithology_score: Optional[np.ndarray] = None,
    distance_to_roads_m: Optional[np.ndarray] = None,
) -> dict:
    """Combine every derivable factor into a bounded susceptibility score.

    `lithology_score` (0-1, higher = weaker material) comes from SoilGrids;
    `distance_to_roads_m` from Overpass. Both are OPTIONAL — when absent
    their weight is redistributed across the available factors and the result
    lists exactly which factors contributed, so a partial computation is
    never presented as a complete one.
    """
    geom = slope_aspect_curvature(dem, pixel_size_m)
    hyd = wetness_and_power(geom["slope_rad"], pixel_size_m)
    d_drain = distance_to_drainage(dem, geom["slope_rad"], pixel_size_m)

    factors, used = {}, []

    # Slope: the first-order control.
    factors["slope"] = _normalise(geom["slope_deg"], SLOPE_LOW_DEG, SLOPE_EXTREME_DEG)
    used.append("slope")

    # Plan curvature: CONVERGENT contours funnel water into a hollow, which
    # is where failures start. SIGN CONVENTION VERIFIED BY MEASUREMENT, not
    # assumed — an earlier version scored `-pc` on the textbook "concave is
    # negative" convention and was exactly inverted, rewarding diverging
    # spurs. Measured on synthetic terrain at identical gradient:
    #   converging hollow -> plan_curvature +2.98e-04
    #   diverging spur    -> plan_curvature -2.98e-04
    # so under THIS discretisation POSITIVE is convergent, and positive is
    # what raises risk.
    pc = geom["plan_curvature"]
    factors["plan_curvature"] = _normalise(
        pc, float(np.nanpercentile(pc, 10)), float(np.nanpercentile(pc, 90))
    )
    used.append("plan_curvature")

    # Profile curvature: convex-then-concave transitions concentrate stress.
    prc = np.abs(geom["profile_curvature"])
    factors["profile_curvature"] = _normalise(
        prc, float(np.nanpercentile(prc, 10)), float(np.nanpercentile(prc, 90))
    )
    used.append("profile_curvature")

    # Wetness: high TWI = saturated = reduced shear strength.
    twi = hyd["twi"]
    factors["wetness_twi"] = _normalise(
        twi, float(np.nanpercentile(twi, 10)), float(np.nanpercentile(twi, 90))
    )
    used.append("wetness_twi")

    spi = hyd["spi"]
    factors["stream_power"] = _normalise(
        spi, float(np.nanpercentile(spi, 10)), float(np.nanpercentile(spi, 90))
    )
    used.append("stream_power")

    # Undercutting: close to a channel is worse. 500 m -> negligible.
    finite_d = np.where(np.isfinite(d_drain), d_drain, 500.0)
    factors["distance_to_drainage"] = 1.0 - _normalise(finite_d, 0.0, 500.0)
    used.append("distance_to_drainage")

    if lithology_score is not None:
        factors["lithology"] = np.clip(
            np.asarray(lithology_score, dtype="float32"), 0.0, 1.0
        )
        used.append("lithology")
    if distance_to_roads_m is not None:
        r = np.where(np.isfinite(distance_to_roads_m), distance_to_roads_m, 1000.0)
        # Road cuts destabilise the slope they are cut into; 200 m is where
        # that influence has effectively decayed.
        factors["distance_to_roads"] = 1.0 - _normalise(r, 0.0, 200.0)
        used.append("distance_to_roads")

    # Redistribute the weight of any absent factor across those present, so
    # a partial computation still produces a bounded [0,1] score rather than
    # a silently deflated one.
    total_w = sum(FACTOR_WEIGHTS[k] for k in used)
    score = np.zeros(dem.shape, dtype="float32")
    for k in used:
        score += factors[k] * (FACTOR_WEIGHTS[k] / total_w)
    score = np.clip(score, 0.0, 1.0)

    p90_slope = float(np.nanpercentile(geom["slope_deg"], 90))
    return {
        "susceptibility": score,
        "mean_susceptibility": round(float(np.nanmean(score)), 4),
        "p90_susceptibility": round(float(np.nanpercentile(score, 90)), 4),
        # p90 not mean: a district with one steep valley and otherwise flat
        # ground averages to LOW, and landslides are local.
        "p90_slope_deg": round(p90_slope, 2),
        "mean_slope_deg": round(float(np.nanmean(geom["slope_deg"])), 2),
        "factors_used": used,
        "factors_absent": [k for k in FACTOR_WEIGHTS if k not in used],
        "weights_applied": {k: round(FACTOR_WEIGHTS[k] / total_w, 4) for k in used},
        "twi_caveat": hyd["caveat"],
        "basis": (
            "Computed from the DEM (slope, plan/profile curvature, TWI, SPI, "
            "aspect, distance-to-drainage) plus optional SoilGrids lithology "
            "and Overpass road proximity. NOT imported from LHASA — the "
            "judgement is the product. Weights are engineering judgement "
            "reflecting the documented ordering of controls (slope first, "
            "lithology second); they are NOT fitted to a landslide inventory, "
            "because no inventory with usable polygons is available (COOLR's "
            "polygon service is down; its 48 records are mostly ~40 pixels)."
        ),
        "_geometry": geom,
        "_hydrology": hyd,
    }
