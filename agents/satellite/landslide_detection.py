"""Bi-temporal NDVI landslide-scar detection with shape filtering (Phase 4a).

**The gap this closes.** Landslide/earthquake damage was detected from a
SINGLE post-event scene thresholded against an absolute NDVI value, which
conflates disaster damage with terrain that was ALWAYS bare — deserts, rock,
quarries, urban fabric, harvested fields. Nothing in the code or the output
disclosed that limitation to the operator (SYSTEM_ANALYSIS.md's science-gap
table: "materially weaker than standard practice, undisclosed").

**What replaces it.** Bi-temporal differencing (NDVI_post - NDVI_pre) so only
terrain that actually LOST vegetation is a candidate — ground that was bare
in both scenes differences to ~0 and is never flagged.

**Why a drop alone is still not enough, and what shape adds.** Harvesting,
seasonal senescence and logging all produce clean NDVI drops. What separates
a landslide is GEOMETRY: a scar is elongated, aligned downslope, sits on
steep ground, and tapers. A circular NDVI drop on flat farmland is a
harvested field, not a landslide, and only shape analysis can tell them
apart. Each connected component is therefore filtered on:

  - elongation (major/minor axis) >= MIN_ELONGATION
  - orientation within ORIENTATION_TOLERANCE_DEG of the local slope aspect
  - mean slope >= MIN_SLOPE_DEG
  - area >= MIN_SCAR_AREA_PX
  - downslope tapering (upper half wider than lower half, or vice versa —
    a scar narrows toward one end rather than being a uniform blob)

**Thresholds are ENGINEERING JUDGEMENT, not values from a validated
inventory.** They encode well-documented qualitative properties of landslide
scars (elongated, downslope-aligned, on steep ground) but no calibration
against a landslide catalogue was performed — NASA COOLR is the path if that
is wanted later. Every threshold rides in the result so a run's filtering is
re-derivable and a future recalibration is auditable against past runs.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# An NDVI DROP of at least this much is a vegetation-loss candidate. 0.15 is
# comfortably above typical inter-scene NDVI noise/phenological drift for
# healthy vegetation while well below the drop a real scar produces
# (vegetated slope ~0.6-0.8 -> exposed soil/rock ~0.1-0.2).
NDVI_DROP_THRESHOLD = 0.15

# Scar geometry. A landslide is elongated downslope; 2.0 (twice as long as
# wide) is a deliberately permissive floor that still excludes blobs.
MIN_ELONGATION = 2.0
# How closely the object's long axis must align with the local slope aspect.
# Generous because aspect varies across a real scar and DEM aspect is noisy.
ORIENTATION_TOLERANCE_DEG = 40.0
# Landslides need gravity. Below ~15 deg, slides are rare enough that an
# NDVI drop there is far more likely to be agricultural.
MIN_SLOPE_DEG = 15.0
# Noise floor: at 10 m pixels this is ~2000 m^2.
MIN_SCAR_AREA_PX = 20
# Tapering: the wider half must exceed the narrower by this ratio.
MIN_TAPER_RATIO = 1.2


def ndvi_difference(ndvi_post: np.ndarray, ndvi_pre: np.ndarray) -> np.ndarray:
    """NDVI_post - NDVI_pre. Negative = vegetation lost."""
    a = np.asarray(ndvi_post, dtype="float32")
    b = np.asarray(ndvi_pre, dtype="float32")
    out = np.full(a.shape, np.nan, dtype="float32")
    ok = np.isfinite(a) & np.isfinite(b)
    out[ok] = a[ok] - b[ok]
    return out


def slope_and_aspect(dem: np.ndarray, pixel_size_m: float = 10.0):
    """Slope (degrees) and aspect (degrees clockwise from north) from a DEM."""
    d = np.asarray(dem, dtype="float32")
    filled = np.nan_to_num(d, nan=float(np.nanmean(d)) if np.isfinite(d).any() else 0.0)
    gy, gx = np.gradient(filled, pixel_size_m, pixel_size_m)
    slope = np.degrees(np.arctan(np.sqrt(gx**2 + gy**2)))
    # Aspect: direction of steepest DESCENT, clockwise from north.
    aspect = (np.degrees(np.arctan2(-gx, gy)) + 360.0) % 360.0
    return slope, aspect


def _angular_difference(a: float, b: float) -> float:
    """Smallest angle between two orientations, treating them as AXES
    (180-degree periodic) — a scar's long axis has no up/down sense."""
    diff = abs((a - b) % 180.0)
    return min(diff, 180.0 - diff)


def detect_landslide_scars(
    ndvi_post: np.ndarray,
    ndvi_pre: Optional[np.ndarray],
    dem: Optional[np.ndarray] = None,
    pixel_size_m: float = 10.0,
    valid_mask: Optional[np.ndarray] = None,
) -> dict:
    """Bi-temporal, shape-filtered landslide scar detection.

    `ndvi_pre` is REQUIRED for the bi-temporal method. Without it the
    function returns `status="insufficient_reference"` and NO mask —
    deliberately not falling back to single-scene absolute thresholding,
    which is the defect this module removes (it cannot distinguish damage
    from terrain that was always bare).
    """
    if ndvi_pre is None:
        return {
            "status": "insufficient_reference",
            "reason": (
                "No pre-event NDVI available. Single-scene absolute "
                "thresholding is NOT substituted: it cannot distinguish "
                "disaster damage from terrain that was always bare (desert, "
                "rock, urban, harvested field)."
            ),
            "scar_mask": None,
            "scars": [],
        }

    diff = ndvi_difference(ndvi_post, ndvi_pre)
    candidate = np.isfinite(diff) & (diff <= -NDVI_DROP_THRESHOLD)
    if valid_mask is not None:
        candidate &= valid_mask

    slope = aspect = None
    if dem is not None:
        slope, aspect = slope_and_aspect(dem, pixel_size_m)

    try:
        from scipy.ndimage import label
    except ImportError:
        return {
            "status": "unavailable",
            "reason": "scipy required for connected-component labelling",
            "scar_mask": None,
            "scars": [],
        }

    labelled, n = label(candidate)
    scars, kept_mask = [], np.zeros(candidate.shape, dtype=bool)
    rejected = {"area": 0, "elongation": 0, "slope": 0, "orientation": 0, "taper": 0}

    for idx in range(1, n + 1):
        obj = labelled == idx
        area_px = int(obj.sum())
        if area_px < MIN_SCAR_AREA_PX:
            rejected["area"] += 1
            continue
        props = _region_properties(obj)
        if props["elongation"] < MIN_ELONGATION:
            rejected["elongation"] += 1
            continue
        mean_slope = float(np.nanmean(slope[obj])) if slope is not None else None
        if mean_slope is not None and mean_slope < MIN_SLOPE_DEG:
            rejected["slope"] += 1
            continue
        mean_aspect = None
        if aspect is not None:
            # Circular mean of aspect over the object.
            rad = np.radians(aspect[obj])
            mean_aspect = float(
                (math.degrees(math.atan2(np.sin(rad).mean(), np.cos(rad).mean())) + 360.0)
                % 360.0
            )
            if _angular_difference(props["orientation"], mean_aspect) > ORIENTATION_TOLERANCE_DEG:
                rejected["orientation"] += 1
                continue
        if props["taper_ratio"] < MIN_TAPER_RATIO:
            rejected["taper"] += 1
            continue

        kept_mask |= obj
        scars.append({
            "area_px": area_px,
            "area_m2": round(area_px * pixel_size_m**2, 1),
            "elongation": round(props["elongation"], 2),
            "orientation_deg": round(props["orientation"], 1),
            "mean_slope_deg": round(mean_slope, 1) if mean_slope is not None else None,
            "mean_aspect_deg": round(mean_aspect, 1) if mean_aspect is not None else None,
            "taper_ratio": round(props["taper_ratio"], 2),
            "mean_ndvi_drop": round(float(np.nanmean(diff[obj])), 3),
        })

    valid_count = int(np.isfinite(diff).sum())
    return {
        "status": "complete",
        "scar_mask": kept_mask,
        "ndvi_difference": diff,
        "scars": scars,
        "scar_count": len(scars),
        "affected_percent": (
            round(100.0 * int(kept_mask.sum()) / valid_count, 3) if valid_count else 0.0
        ),
        "candidates_before_shape_filter": n,
        "rejected_by": rejected,
        # Full threshold audit — a run's filtering is re-derivable, and a
        # future recalibration against a real inventory stays comparable.
        "method": "bitemporal_ndvi_shape_filtered",
        "ndvi_drop_threshold": NDVI_DROP_THRESHOLD,
        "min_elongation": MIN_ELONGATION,
        "orientation_tolerance_deg": ORIENTATION_TOLERANCE_DEG,
        "min_slope_deg": MIN_SLOPE_DEG,
        "min_scar_area_px": MIN_SCAR_AREA_PX,
        "min_taper_ratio": MIN_TAPER_RATIO,
        "thresholds_basis": (
            "engineering judgement encoding documented scar geometry "
            "(elongated, downslope-aligned, steep); NOT calibrated against a "
            "landslide inventory — NASA COOLR is the path if that is wanted"
        ),
    }


def _region_properties(obj: np.ndarray) -> dict:
    """Elongation, orientation and taper of a binary component.

    Uses second-order central moments (the standard image-moment approach
    skimage.regionprops implements) so no extra dependency is required
    beyond numpy — the axis lengths and orientation come from the
    eigen-decomposition of the covariance matrix of the pixel coordinates.
    """
    ys, xs = np.nonzero(obj)
    if ys.size < 2:
        return {"elongation": 0.0, "orientation": 0.0, "taper_ratio": 0.0}
    y0, x0 = ys.mean(), xs.mean()
    yy = ((ys - y0) ** 2).mean()
    xx = ((xs - x0) ** 2).mean()
    xy = ((xs - x0) * (ys - y0)).mean()
    cov = np.array([[xx, xy], [xy, yy]])
    evals, evecs = np.linalg.eigh(cov)
    evals = np.maximum(evals, 1e-9)
    major, minor = math.sqrt(evals[1]), math.sqrt(evals[0])
    elongation = major / minor if minor > 0 else 0.0
    # Orientation of the MAJOR axis, degrees clockwise from north, to be
    # comparable with DEM aspect.
    vec = evecs[:, 1]  # (x, y) of the major axis
    orientation = (math.degrees(math.atan2(vec[0], vec[1])) + 360.0) % 180.0

    # Taper: project pixels onto the major axis, split at the midpoint, and
    # compare the perpendicular spread of the two halves. A scar narrows
    # toward one end; a blob does not.
    proj = (xs - x0) * vec[0] + (ys - y0) * vec[1]
    perp = (xs - x0) * (-vec[1]) + (ys - y0) * vec[0]
    upper, lower = proj >= 0, proj < 0
    if upper.sum() < 2 or lower.sum() < 2:
        taper = 0.0
    else:
        wu, wl = perp[upper].std(), perp[lower].std()
        taper = max(wu, wl) / min(wu, wl) if min(wu, wl) > 1e-9 else 0.0
    return {"elongation": elongation, "orientation": orientation,
            "taper_ratio": taper}
