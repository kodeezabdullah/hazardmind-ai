"""Earthquake BUILDING-DAMAGE detection from Sentinel-1 SAR.

**What this is, and what it deliberately is not.** Ground shaking is not
observable from satellite — it comes from seismometer networks we do not
have. USGS is therefore a TRIGGER ("a quake occurred here, go look"), never
the answer. ShakeMap and PAGER are modelled hazard products and are not used:
consuming them would mean shipping someone else's conclusion.

What IS observable, and is the product: **what broke**. This module detects
building damage from SAR change, with reasoning.

**Why NDVI is the wrong signal, and was removed.** The pipeline previously
ran NDVI for earthquakes (`NDVI_QUAKE`). Buildings collapse; vegetation does
not change. A collapsed apartment block and its intact neighbour have
essentially the same NDVI, while a harvested field between them has a large
NDVI drop and no damage at all. NDVI cannot see the thing we care about.

**The physical basis — a change in SCATTERING MECHANISM, not brightness.**

    standing building  -> the wall-ground corner acts as a dihedral
                          reflector: strong DOUBLE-BOUNCE return, VV-dominant
    collapsed building -> rubble is a randomly-oriented volume: VOLUME
                          scattering, which DEPOLARISES, so VH rises
                          RELATIVE to VV

So the **change in the VH/VV ratio** is a damage indicator that is more
specific than intensity change alone — intensity can move for many reasons
(soil moisture, a different look, a wet roof), whereas a shift from
double-bounce toward volume scattering is characteristic of structural
collapse. This is the basis of the operational SAR damage literature
(Matsuoka & Yamazaki's work on Kobe 1995 and Bam 2003 established the
intensity-correlation form used below).

Four complementary measures are computed and combined:

  1. intensity log-ratio      10*log10(post/pre)      — how much changed
  2. VH/VV ratio change       depolarisation          — HOW it changed
  3. local correlation        11x11 moving window     — did the local
                              texture pattern survive? (Matsuoka-Yamazaki)
  4. GLCM-style texture change contrast/homogeneity   — rubble is rougher

**Constrained by built-up (Phase 4's IBI).** Damage is only meaningful where
buildings exist. Farmland and bare land produce intensity change for
irrigation and ploughing reasons that have nothing to do with an earthquake,
so scoring them is pure noise. The built-up mask is a REQUIRED input, not an
optional refinement.

**THE RESOLUTION LIMIT, stated plainly.** Sentinel-1 at 10 m cannot resolve
individual buildings — a 10 m pixel spans a whole small structure and parts
of its neighbours. This detects LARGE-SCALE DESTRUCTION (collapsed blocks,
levelled districts), NOT per-structure damage, and any per-building claim
from this data would be false. `resolution_limit` says so in every result.

**Documented upgrade path (not built here):** InSAR COHERENCE change, as used
by NASA/JPL ARIA Damage Proxy Maps, is the stronger method — coherence loss
is more sensitive to structural change than intensity. It requires SLC
products and an interferometric processing chain (co-registration, phase),
neither of which this pipeline has. Recorded as the next step rather than
half-implemented.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# --- Thresholds -------------------------------------------------------------
# A rise in VH/VV of this many dB indicates a shift toward volume scattering
# (depolarisation) — the collapse signature. Engineering judgement encoding
# the documented mechanism, NOT calibrated against a damage inventory: xBD is
# sub-metre commercial optical over one earthquake and cannot score a 10 m SAR
# detector (measured 2026-07-29). `thresholds_basis` states this in-result.
VHVV_DEPOLARISATION_DB = 2.0

# Intensity change magnitude that counts as "something happened here".
INTENSITY_CHANGE_DB = 3.0

# Matsuoka-Yamazaki correlation window. 11x11 at 10 m ~ 110 m: large enough
# for a stable correlation estimate, small enough to localise a city block.
CORRELATION_WINDOW = 11
# Below this, the local texture pattern did not survive — structural change.
CORRELATION_LOSS_THRESHOLD = 0.3

# Minimum contiguous damaged pixels. At 10 m, 20 px ~ 0.002 km2 — around a
# city block. Below this is speckle, not a damaged district.
MIN_DAMAGE_PATCH_PIXELS = 20

# Approximate smallest area this method can honestly claim, at 10 m.
MIN_DETECTABLE_DAMAGE_M2 = MIN_DAMAGE_PATCH_PIXELS * 100


def _local_mean(a: np.ndarray, w: int) -> np.ndarray:
    from scipy.ndimage import uniform_filter
    return uniform_filter(np.nan_to_num(a, nan=0.0).astype("float32"), size=w)


def local_correlation(pre: np.ndarray, post: np.ndarray,
                      window: int = CORRELATION_WINDOW) -> np.ndarray:
    """Moving-window Pearson correlation between pre and post intensity.

    Matsuoka & Yamazaki's damage indicator: an undamaged area keeps its local
    texture pattern between acquisitions (high correlation) even if the mean
    brightness shifts; a collapsed area does not. Correlation is therefore
    sensitive to STRUCTURAL change specifically, not to overall brightness.
    """
    a = np.nan_to_num(pre, nan=0.0).astype("float32")
    b = np.nan_to_num(post, nan=0.0).astype("float32")
    ma, mb = _local_mean(a, window), _local_mean(b, window)
    va = np.maximum(_local_mean(a * a, window) - ma * ma, 0.0)
    vb = np.maximum(_local_mean(b * b, window) - mb * mb, 0.0)
    cov = _local_mean(a * b, window) - ma * mb
    denom = np.sqrt(va * vb)
    out = np.where(denom > 1e-9, cov / np.maximum(denom, 1e-9), 0.0)
    return np.clip(out, -1.0, 1.0).astype("float32")


def texture_contrast(a: np.ndarray, window: int = CORRELATION_WINDOW) -> np.ndarray:
    """Local variance as a GLCM-contrast proxy.

    A full grey-level co-occurrence matrix per pixel is prohibitively slow on
    a full scene; local variance captures the same property this detector
    needs — rubble is texturally rougher than intact roofs — at a fraction of
    the cost. Named honestly as a proxy rather than claimed as GLCM.
    """
    m = _local_mean(a, window)
    return np.maximum(_local_mean(a * a, window) - m * m, 0.0).astype("float32")


def _log_ratio(post: np.ndarray, pre: np.ndarray) -> np.ndarray:
    out = np.full(post.shape, np.nan, dtype="float32")
    ok = (np.isfinite(post) & np.isfinite(pre) & (post > 0) & (pre > 0))
    out[ok] = 10.0 * np.log10(post[ok] / pre[ok])
    return out


def detect_earthquake_damage(
    post_vv: np.ndarray,
    pre_vv: np.ndarray,
    post_vh: Optional[np.ndarray] = None,
    pre_vh: Optional[np.ndarray] = None,
    built_up_mask: Optional[np.ndarray] = None,
    valid_mask: Optional[np.ndarray] = None,
    pixel_size_m: float = 10.0,
) -> dict:
    """SAR building-damage detection on same-relative-orbit pre/post scenes.

    `built_up_mask` is REQUIRED. Without it the function returns
    `status="no_exposure_mask"` rather than scoring farmland, where intensity
    change means irrigation and ploughing, not earthquake damage.

    VH is optional but strongly preferred: without it the polarimetric
    (scattering-mechanism) evidence is unavailable and the result falls back
    to intensity + correlation + texture only, which is LESS SPECIFIC. The
    result says which evidence was actually available.
    """
    if built_up_mask is None:
        return {
            "status": "no_exposure_mask",
            "reason": (
                "No built-up mask supplied. Damage is only meaningful where "
                "buildings exist; intensity change over farmland reflects "
                "irrigation and ploughing, not earthquake damage. Scoring it "
                "would be noise reported as a finding."
            ),
            "damage_mask": None,
        }

    valid = np.isfinite(post_vv) & np.isfinite(pre_vv) & built_up_mask
    if valid_mask is not None:
        valid &= valid_mask
    if not valid.any():
        return {
            "status": "no_built_up_in_aoi",
            "reason": (
                "The AOI contains no built-up pixels, so there is nothing "
                "whose damage this method can assess."
            ),
            "damage_mask": None,
        }

    evidence = []

    # 1. Intensity log-ratio.
    intensity = _log_ratio(post_vv, pre_vv)
    intensity_hit = valid & (np.abs(intensity) >= INTENSITY_CHANGE_DB)
    evidence.append("intensity_change")

    # 2. VH/VV ratio change — the SCATTERING MECHANISM shift. This is the
    #    specific evidence; the rest is corroboration.
    depol = None
    depol_hit = np.zeros_like(valid)
    if post_vh is not None and pre_vh is not None:
        post_ratio = _log_ratio(post_vh, post_vv)
        pre_ratio = _log_ratio(pre_vh, pre_vv)
        depol = post_ratio - pre_ratio
        depol_hit = valid & np.isfinite(depol) & (depol >= VHVV_DEPOLARISATION_DB)
        evidence.append("vh_vv_depolarisation")

    # 3. Local correlation loss (Matsuoka-Yamazaki).
    corr = local_correlation(pre_vv, post_vv)
    corr_hit = valid & (corr < CORRELATION_LOSS_THRESHOLD)
    evidence.append("local_correlation")

    # 4. Texture change.
    tex_change = texture_contrast(post_vv) - texture_contrast(pre_vv)
    finite_tex = tex_change[valid & np.isfinite(tex_change)]
    tex_cut = float(np.percentile(finite_tex, 90)) if finite_tex.size else np.inf
    tex_hit = valid & np.isfinite(tex_change) & (tex_change >= tex_cut)
    evidence.append("texture_change")

    # COMBINE by agreement, not by any single indicator. Intensity alone moves
    # for many non-damage reasons; requiring two independent lines of evidence
    # is what makes a detection defensible. When VH is present the
    # depolarisation term is available and carries the mechanism evidence.
    votes = (
        intensity_hit.astype("uint8") + depol_hit.astype("uint8")
        + corr_hit.astype("uint8") + tex_hit.astype("uint8")
    )
    damage = valid & (votes >= 2)

    try:
        from scipy.ndimage import binary_opening, label
        damage = binary_opening(damage, iterations=1)
        lab, n = label(damage)
        if n:
            counts = np.bincount(lab.ravel())
            small = np.where(counts < MIN_DAMAGE_PATCH_PIXELS)[0]
            if small.size:
                damage[np.isin(lab, small)] = False
    except ImportError:
        pass

    n_valid = int(valid.sum())
    n_dmg = int(damage.sum())
    px_km2 = (pixel_size_m ** 2) / 1e6

    return {
        "status": "complete",
        "damage_mask": damage,
        "intensity_change_db": intensity,
        "vh_vv_change_db": depol,
        "local_correlation": corr,
        "damaged_area_km2": round(n_dmg * px_km2, 4),
        "damaged_percent_of_built_up": (
            round(100.0 * n_dmg / n_valid, 2) if n_valid else 0.0
        ),
        "built_up_pixels_assessed": n_valid,
        "mean_intensity_change_db": (
            round(float(np.nanmean(intensity[damage])), 3) if n_dmg else None
        ),
        "mean_vh_vv_change_db": (
            round(float(np.nanmean(depol[damage])), 3)
            if (depol is not None and n_dmg) else None
        ),
        "mean_correlation_in_damage": (
            round(float(np.nanmean(corr[damage])), 3) if n_dmg else None
        ),
        # --- Method / evidence audit ------------------------------------
        "method": "sar_polarimetric_damage_detection",
        "evidence_used": evidence,
        "polarimetric_evidence_available": depol is not None,
        "combination_rule": ">=2 of 4 independent indicators agree",
        "thresholds": {
            "vh_vv_depolarisation_db": VHVV_DEPOLARISATION_DB,
            "intensity_change_db": INTENSITY_CHANGE_DB,
            "correlation_loss": CORRELATION_LOSS_THRESHOLD,
            "correlation_window": CORRELATION_WINDOW,
            "min_damage_patch_px": MIN_DAMAGE_PATCH_PIXELS,
        },
        "thresholds_basis": (
            "engineering judgement encoding the documented double-bounce -> "
            "volume-scattering collapse mechanism (Matsuoka & Yamazaki, Kobe "
            "1995 / Bam 2003). NOT calibrated against a damage inventory: xBD "
            "is sub-metre commercial optical over a single earthquake and "
            "cannot score a 10 m Sentinel-1 detector."
        ),
        # THE LIMIT, in every result rather than only in the docs.
        "resolution_limit": (
            "Sentinel-1 at 10 m cannot resolve individual buildings. This "
            "detects LARGE-SCALE destruction (collapsed blocks, levelled "
            "districts), NOT per-structure damage. Any per-building claim "
            "from this data would be false."
        ),
        "upgrade_path": (
            "InSAR coherence change (as in NASA/JPL ARIA Damage Proxy Maps) "
            "is the stronger method but requires SLC products and an "
            "interferometric chain — documented, not built here."
        ),
        # A difference is self-referenced, so it is defensible without
        # absolute radiometric calibration — same argument as the flood
        # log-ratio.
        "index_calibrated": True,
        "index_units": "dB_change_ratio",
    }
