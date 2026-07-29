"""Kittler-Illingworth adaptive thresholding for the S2 water index (Phase 2).

Fixed 0.3/0.5 cut points do not hold across seasons, sun angles, regions or
index formulas (Phase 1b measured exactly this: MNDWI's distribution puts
22% of a flooded AOI in the 0.0-0.3 band the NDWI-era scheme calls
"wet_soil" and 0.01% above 0.3). Kittler & Illingworth (1986) minimum-error
thresholding fits a two-component model to the index histogram and picks
the cut that minimises classification error. KI is chosen over Otsu because
its minimum-error formulation tolerates unequal class variances and
non-Gaussian mixtures better — the water/land reflectance mixture is
exactly that (and the same machinery serves the SAR log-ratio in Phase 3,
where Otsu's equal-variance assumption is decisively wrong).

**The unimodality guard.** KI always returns *a* number; on an effectively
unimodal histogram (a barely-flooded AOI — the ~1% flooded-fraction regime)
that number is meaningless and can slice the land mode in half. Bimodality
is therefore tested BEFORE the derived threshold is trusted, using
**Ashman's D** on the two KI-split classes:

    D = sqrt(2) * |mu1 - mu2| / sqrt(sigma1^2 + sigma2^2)

D > 2 is the standard criterion for a clean two-population separation
(Ashman, Bird & Zepf 1994). Two additional degeneracy guards: each class
must hold at least MIN_CLASS_FRACTION of the pixels (a "mode" of a handful
of pixels is noise, not a population), and the derived cut must land
strictly inside the observed value range. Any guard failing -> the caller
falls back to the fixed threshold, and the result records WHICH path ran
(`threshold_method`) plus the derived value either way, so any run's
classification can be re-derived from its stored result.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Ashman's D criterion for "cleanly bimodal" (Ashman, Bird & Zepf 1994).
ASHMAN_D_MIN = 2.0
# Each KI class must hold at least this fraction of pixels to count as a
# real population rather than a noise tail.
MIN_CLASS_FRACTION = 0.005  # 0.5%
_HIST_BINS = 512


def kittler_illingworth_threshold(values: np.ndarray) -> Optional[dict]:
    """Minimum-error threshold over a 1-D sample.

    Returns ``{"threshold", "ashman_d", "class_fractions", "means",
    "stds", "bimodal"}`` or ``None`` when the sample is too small/degenerate
    to even attempt (fewer than 100 finite values, or zero spread).
    ``bimodal`` already folds in every guard — callers should trust
    ``threshold`` only when it is True.
    """
    v = np.asarray(values, dtype="float64")
    v = v[np.isfinite(v)]
    if v.size < 100:
        return None
    vmin, vmax = float(v.min()), float(v.max())
    if not (vmax > vmin):
        return None

    hist, edges = np.histogram(v, bins=_HIST_BINS, range=(vmin, vmax))
    centers = (edges[:-1] + edges[1:]) / 2.0
    p = hist.astype("float64") / hist.sum()

    # Cumulative moments for every candidate cut.
    w1 = np.cumsum(p)
    w2 = 1.0 - w1
    m1_num = np.cumsum(p * centers)
    total_mean = m1_num[-1]
    eps = 1e-12
    mu1 = m1_num / np.maximum(w1, eps)
    mu2 = (total_mean - m1_num) / np.maximum(w2, eps)
    s1_num = np.cumsum(p * centers**2)
    var1 = s1_num / np.maximum(w1, eps) - mu1**2
    var2 = (s1_num[-1] - s1_num) / np.maximum(w2, eps) - mu2**2
    var1 = np.maximum(var1, eps)
    var2 = np.maximum(var2, eps)

    # KI criterion J(T); valid only where both classes are non-empty.
    J = (
        1.0
        + 2.0 * (w1 * np.log(np.sqrt(var1)) + w2 * np.log(np.sqrt(var2)))
        - 2.0 * (w1 * np.log(np.maximum(w1, eps)) + w2 * np.log(np.maximum(w2, eps)))
    )
    valid = (w1 > eps) & (w2 > eps)
    if not valid.any():
        return None
    J_masked = np.where(valid, J, np.inf)
    idx = int(np.argmin(J_masked))
    threshold = float(edges[idx + 1])

    f1, f2 = float(w1[idx]), float(w2[idx])
    m1v, m2v = float(mu1[idx]), float(mu2[idx])
    s1v, s2v = float(math.sqrt(var1[idx])), float(math.sqrt(var2[idx]))
    ashman_d = math.sqrt(2.0) * abs(m1v - m2v) / math.sqrt(s1v**2 + s2v**2)

    interior = vmin < threshold < vmax
    bimodal = (
        interior
        and ashman_d >= ASHMAN_D_MIN
        and min(f1, f2) >= MIN_CLASS_FRACTION
    )
    return {
        "threshold": round(threshold, 4),
        "ashman_d": round(ashman_d, 3),
        "class_fractions": (round(f1, 4), round(f2, 4)),
        "means": (round(m1v, 4), round(m2v, 4)),
        "stds": (round(s1v, 4), round(s2v, 4)),
        "bimodal": bool(bimodal),
    }


def derive_water_threshold(
    index_values: np.ndarray, fixed_threshold: float
) -> dict:
    """Adaptive-or-fallback water threshold for a calibrated water index.

    Returns a dict the caller can splice into its result for auditability:
        threshold        — the value to classify with
        threshold_method — "kittler_illingworth" | "fixed_fallback"
        ki               — the raw KI diagnostics dict (or None)
        fallback_reason  — set when the fixed threshold was used

    The water class is the HIGH side of the cut for MNDWI/NDWI (water
    positive). When KI is trusted, the returned threshold is the KI cut;
    the caller keeps its severity grading relative to it.
    """
    ki = kittler_illingworth_threshold(index_values)
    if ki is None:
        return {
            "threshold": fixed_threshold,
            "threshold_method": "fixed_fallback",
            "ki": None,
            "fallback_reason": "sample_too_small_or_degenerate",
        }
    if not ki["bimodal"]:
        reason = (
            f"not_bimodal (ashman_d={ki['ashman_d']}, "
            f"class_fractions={ki['class_fractions']})"
        )
        return {
            "threshold": fixed_threshold,
            "threshold_method": "fixed_fallback",
            "ki": ki,
            "fallback_reason": reason,
        }
    return {
        "threshold": ki["threshold"],
        "threshold_method": "kittler_illingworth",
        "ki": ki,
        "fallback_reason": None,
    }


if __name__ == "__main__":
    rng = np.random.default_rng(7)
    land = rng.normal(-0.35, 0.08, 50_000)
    water = rng.normal(0.45, 0.12, 10_000)
    bimodal_sample = np.concatenate([land, water])
    print("bimodal 17% water:", derive_water_threshold(bimodal_sample, 0.3))
    tiny = np.concatenate([land, rng.normal(0.45, 0.12, 200)])  # ~0.4% water
    print("unimodal-ish 0.4% water:", derive_water_threshold(tiny, 0.3))
