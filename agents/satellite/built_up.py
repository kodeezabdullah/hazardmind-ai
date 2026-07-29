"""Phase 4 — built-up (impervious surface) layer from Sentinel-2.

**Why IBI and not NDBI.** NDBI = (SWIR-NIR)/(SWIR+NIR) is the classic
built-up index, but bare soil has almost the same SWIR/NIR relationship as
concrete, so NDBI systematically misclassifies dry bare ground as built-up.
In semi-arid Pakistan — the pipeline's primary operating region — that error
class is not marginal, it is most of the landscape for much of the year.

Xu (2008)'s Index-based Built-up Index (IBI) removes it by subtracting the
two things bare soil is NOT: vegetation (SAVI) and water (MNDWI).

    NDBI  = (B11 - B08) / (B11 + B08)
    SAVI  = ((B08 - B04) / (B08 + B04 + L)) * (1 + L),   L = 0.5
    MNDWI = (B03 - B11) / (B03 + B11)
    IBI   = (NDBI - (SAVI + MNDWI)/2) / (NDBI + (SAVI + MNDWI)/2)

**Two purposes, both currently unserved by the pipeline:**

1. *Detection caution.* Built-up surfaces are the classic water-index false
   positive (asphalt and shadowed roofs are dark in NIR/SWIR much like
   water). MNDWI already suppresses much of this — that is why Phase 1b
   adopted it — but the built-up layer identifies WHERE the water index
   deserves less confidence, rather than silently trusting it everywhere.
2. *Exposure.* Flood over built-up means something categorically different
   from flood over farmland. This is the exposure base the impact agent
   needs and does not have.

**BAND AVAILABILITY — a real constraint, handled explicitly.** The flood band
set is B03/B08/B11/TCI/SCL (`_S2_BANDS["flood"]`); it does NOT include B04
(red), which SAVI requires. Rather than silently substituting a different
index under the IBI name — the exact defect class this repo's own audits keep
finding — `compute_ibi` returns None with a named reason when B04 is absent,
and the caller reports `built_up_available: False`. NDBI alone is deliberately
NOT used as a fallback: on semi-arid terrain it is the wrong answer, and a
wrong answer under a correct-looking field name is worse than no answer.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# SAVI's canopy background adjustment. L=0.5 is Huete (1988)'s general-purpose
# value for intermediate vegetation density and is what Xu (2008) uses in the
# IBI formulation; L=0 degenerates SAVI to NDVI, L=1 suits very sparse cover.
SAVI_L = 0.5

# IBI is a normalised ratio in [-1, 1], positive over built-up. Xu (2008) does
# not prescribe a universal cut point — it is scene-dependent, which is why
# this is reported as a CONTINUOUS layer plus an explicitly-labelled
# threshold, not as a hard "this is a building" mask. 0.0 is the sign change
# (built-up dominates the vegetation/water mixture) and is stated in the
# result so a different choice is auditable.
IBI_BUILTUP_THRESHOLD = 0.0


def _safe_ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    out = np.full(num.shape, np.nan, dtype="float32")
    ok = np.isfinite(num) & np.isfinite(den) & (np.abs(den) > 1e-10)
    out[ok] = (num[ok] / den[ok]).astype("float32")
    return out


def compute_ibi(bands: dict, valid: Optional[np.ndarray] = None) -> Optional[dict]:
    """Xu (2008) IBI from an already-stacked Sentinel-2 band dict.

    Returns ``{"ibi", "built_up_mask", "built_up_percent", "threshold",
    "formula", "components"}`` or ``None`` when a required band is missing
    (with the reason logged) — never a substituted index under the IBI name.
    """
    b03, b04, b08, b11 = (
        bands.get("B03"), bands.get("B04"), bands.get("B08"), bands.get("B11")
    )
    missing = [n for n, b in
               (("B03", b03), ("B04", b04), ("B08", b08), ("B11", b11))
               if b is None]
    if missing:
        logger.info(
            "IBI unavailable — missing band(s) %s. NDBI is deliberately NOT "
            "substituted: on semi-arid terrain it misclassifies bare soil as "
            "built-up, and a wrong answer under a correct-looking field name "
            "is worse than no answer.", ", ".join(missing),
        )
        return None

    b03 = b03.astype("float32")
    b04 = b04.astype("float32")
    b08 = b08.astype("float32")
    b11 = b11.astype("float32")

    ndbi = _safe_ratio(b11 - b08, b11 + b08)
    savi = _safe_ratio(b08 - b04, b08 + b04 + SAVI_L) * (1.0 + SAVI_L)
    mndwi = _safe_ratio(b03 - b11, b03 + b11)

    mix = (savi + mndwi) / 2.0
    ibi = _safe_ratio(ndbi - mix, ndbi + mix)

    finite = np.isfinite(ibi)
    if valid is not None:
        finite = finite & valid

    # NUMERICAL GUARD — found by measurement, not anticipated. IBI is a
    # normalised difference, so when NDBI and the (SAVI+MNDWI)/2 mixture are
    # BOTH negative the ratio of two negatives comes out strongly POSITIVE
    # and dense vegetation is scored as built-up. Measured on a vegetation
    # fixture: NDBI -0.3433, mix +0.0249 -> denominator -0.3184, numerator
    # -0.3682, IBI = +1.1564 — comfortably above the 0.0 threshold and
    # completely wrong.
    #
    # The physics says what the guard should be, so this is not a clamp on a
    # symptom: built-up REQUIRES SWIR > NIR, i.e. NDBI > 0. Where NDBI <= 0
    # the surface is not built-up whatever the ratio algebra produces, and a
    # true IBI in (-1, 1) is only defined when the denominator is not
    # vanishing. Both conditions are applied to the MASK; the continuous `ibi`
    # array is returned unmodified so the raw values stay auditable.
    ndbi_positive = np.isfinite(ndbi) & (ndbi > 0.0)
    denom_stable = np.abs(ndbi + mix) > 1e-3
    built = finite & ndbi_positive & denom_stable & (ibi > IBI_BUILTUP_THRESHOLD)
    n_valid = int(finite.sum())

    return {
        "ibi": ibi,
        "built_up_mask": built,
        "built_up_percent": (
            round(100.0 * int(built.sum()) / n_valid, 2) if n_valid else 0.0
        ),
        "threshold": IBI_BUILTUP_THRESHOLD,
        "formula": "IBI (Xu 2008) = (NDBI - (SAVI+MNDWI)/2) / (NDBI + (SAVI+MNDWI)/2)",
        "savi_l": SAVI_L,
        "components": {
            "ndbi_mean": round(float(np.nanmean(ndbi[finite])), 4) if n_valid else None,
            "savi_mean": round(float(np.nanmean(savi[finite])), 4) if n_valid else None,
            "mndwi_mean": round(float(np.nanmean(mndwi[finite])), 4) if n_valid else None,
        },
    }


def flood_builtup_overlap(
    flood_mask: np.ndarray,
    built_up_mask: np.ndarray,
    pixel_area_km2: float,
) -> dict:
    """Where does the detected flood intersect built-up surface?

    This is the number that distinguishes "3 km2 of flooded farmland" from
    "3 km2 of flooded streets" — categorically different for response, and a
    distinction the pipeline could not previously express.
    """
    if flood_mask is None or built_up_mask is None:
        return {"available": False}
    overlap = flood_mask & built_up_mask
    n_flood = int(flood_mask.sum())
    n_over = int(overlap.sum())
    return {
        "available": True,
        "flood_over_built_up_km2": round(n_over * pixel_area_km2, 4),
        "flood_over_built_up_percent": (
            round(100.0 * n_over / n_flood, 2) if n_flood else 0.0
        ),
        "built_up_area_km2": round(int(built_up_mask.sum()) * pixel_area_km2, 4),
    }
