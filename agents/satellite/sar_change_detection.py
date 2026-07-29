"""Sentinel-1 change-detection flood mapping (Phase 3, science/full-pass).

**Why this replaces absolute thresholding.** The pipeline's SAR index is
`10*log10(raw GRD DN)` — uncalibrated, virtually always strongly positive
(~+23 dB measured live), so the `SAR_WATER_THRESHOLD_DB = -15.0` cut in
calibrated-sigma0 space can never fire: the S1 path classified zero water on
every run, always (BASELINE_REPORT_2 §3.4). Calibration alone would not fix
the deeper problem either — a single-scene absolute threshold cannot separate
water from roads, dry sand or airport runways, all of which are smooth and
dark. Operational flood mapping uses change detection for exactly this reason.

**Why calibration is not a prerequisite — VERIFIED, not assumed
(2026-07-29).** For two acquisitions of the same relative orbit the
calibration factor and terrain-induced backscatter are common to both scenes
and cancel in the ratio:

    10*log10(DN_post) - 10*log10(DN_pre) = 10*log10(DN_post / DN_pre)

This was verified against real CDSE metadata rather than taken on faith: the
`sigmaNought` calibration LUTs of three same-relative-orbit (107)
acquisitions over Rawalpindi spanning 24 days (2026-07-01 / 07-13 / 07-25)
agree to ~0.003% (704.4692 / 704.4496 / 704.4586 at the first vector
element) — a residual of **0.00024 dB** against a 3 dB flood criterion, five
orders of magnitude below the signal. The cancellation is real.

**The same-relative-orbit constraint is therefore load-bearing, not
stylistic** — it is the entire basis of the method's validity (incidence
angle, look direction and terrain geometry must match). It is enforced
strictly here: no same-orbit reference means `status="insufficient_reference"`,
never a fallback to absolute thresholding.

Pipeline: Refined Lee speckle filter -> median pre-event baseline ->
log-ratio -> HAND + layover/shadow masking -> tiled KI thresholding ->
morphological cleanup.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# --- Speckle filtering ------------------------------------------------------
# Refined Lee window. 7x7 is the operational standard for S1 GRD at 10 m
# (ESA SNAP's default): large enough that the local statistics are stable
# (49 samples), small enough to preserve flood-edge detail at ~70 m. A 3x3
# window leaves too much speckle variance for reliable thresholding; 11x11+
# blurs the shoreline the metric is scored on.
REFINED_LEE_WINDOW = 7
# Equivalent number of looks for S1 IW GRDH (ESA product spec ~4.4).
ENL = 4.4

# --- Baseline ---------------------------------------------------------------
# Median of N pre-event same-orbit scenes. 3 is the minimum for a median to
# reject a single transient (one rainy acquisition, one wet-soil day); more
# would push the window past a season over the ~12-day S1 repeat measured for
# this AOI class, and seasonal vegetation/soil-moisture difference would then
# leak into the flood signal.
BASELINE_TARGET_SCENES = 3
BASELINE_MAX_DAYS = 60  # ~5 repeat cycles at 12 days — same season, enough depth
BASELINE_MIN_SCENES = 1  # below target -> proceed, but penalise confidence

# --- Change threshold -------------------------------------------------------
# A backscatter DROP of >= 3 dB is the conventional OPEN-WATER flood
# indicator. Unlike -13/-15/-18 absolute cut points this one is physically
# justified BECAUSE it is relative: smooth water reflects energy away from
# the sensor, so newly-flooded ground loses several dB against its own
# pre-event value, whatever that value's absolute calibration was.
FLOOD_DROP_DB = 3.0

# A backscatter RISE of >= 3 dB is the FLOODED-VEGETATION indicator, and it
# is a different physical mechanism, not a symmetric convenience. Where water
# stands among emergent vegetation (flooded farmland, reed beds, inundated
# orchards) the water surface and the vertical plant stems form a dihedral
# corner reflector: energy bounces water->stem->sensor instead of scattering
# away, so backscatter RISES several dB. This double-bounce enhancement is
# well established in the SAR flood literature (Richards et al. 1987 first
# described it over flooded forest; Hess et al. 1995 and the whole
# ScanSAR/ALOS wetland-mapping line rest on it) and is the dominant
# signature over agricultural floodplains — precisely the terrain a
# drop-only detector is blind to.
#
# Kept as a SEPARATE constant from FLOOD_DROP_DB even though both are 3.0
# today: they encode different physics and there is no reason a future
# calibration pass must move them together.
FLOOD_RISE_DB = 3.0

# Detection direction. "both" enables open-water (drop) AND flooded-vegetation
# (rise) signatures; "drop" restores the pre-2026-07-29 drop-only behaviour.
# Exposed as a named constant so an ablation can turn one direction off
# without editing the detector.
DETECTION_DIRECTION = "both"

# --- HAND -------------------------------------------------------------------
# Height Above Nearest Drainage ceiling. Water cannot pond more than ~15 m
# above the nearest drainage channel except behind a dam; radar shadow in
# hilly terrain produces the same near-zero return as water and is the
# dominant SAR false positive. 15 m is the widely-used operational value for
# HAND-based flood-plain delineation (Nobre et al. 2011 / GFM-style
# workflows) and is recorded in the result so a different choice is auditable.
HAND_MAX_M = 15.0

# --- Morphology -------------------------------------------------------------
MORPH_OPENING_ITERATIONS = 1
MIN_FLOOD_PATCH_PIXELS = 50  # ~0.005 km2 at 10 m — below this is speckle residue

# --- Tiled thresholding -----------------------------------------------------
TILE_SIZE_PX = 512
MIN_BIMODAL_TILES = 1

# --- Signal detectability ---------------------------------------------------
# Measured 2026-07-29 on the Kanalia forced-S1 run: the change image inside
# the confirmed EMS flood extent was statistically INDISTINGUISHABLE from dry
# ground (Cohen's d 0.031, ROC AUC 0.487 — below chance). Every threshold
# scored a precision LIFT BELOW 1.0x, i.e. worse than labelling the whole AOI
# flooded. The pipeline nonetheless shipped that map with no indication the
# scene carried no signal.
#
# **A first attempt at this guard was measured and DISCARDED**, and the
# reason is worth recording. It compared the detected pixels against the
# undetected ones (Cohen's d) — which is CIRCULAR: the detector defines those
# two groups by thresholding the very values being compared, so they are
# separated by construction. On the real no-signal scene that test returned
# d = 4.18 and cheerfully passed, while the ground-truth-based separation was
# 0.031. It measured the threshold, not the signal.
#
# What actually distinguishes the two cases is the SHAPE OF THE WHOLE
# DISTRIBUTION, which is independent of where the cut lands. A real flood
# adds a second, distinctly darker population, so the change image becomes
# bimodal and heavy-tailed on the negative side. The no-signal scene was a
# single narrow near-Gaussian mode centred just below 0 dB (p1 -3.05,
# p50 -0.29, p99 +0.59) — the tail beyond -3 dB held only ~1% of pixels and
# was tail, not mode.
#
# **A SECOND attempt was also measured and discarded** — using the tiled KI
# estimator's bimodality vote. Measured: the no-signal scene returned a
# bimodal-tile fraction of 0.75, HIGHER than a synthetic real flood's 0.25.
# KI minimises classification error and will always split a tile somewhere,
# so on a smooth near-Gaussian noise field it happily reports "bimodal".
# Bimodality is evidence about a tile's histogram shape, not about whether a
# flood exists. Discarded on measurement, like the first attempt.
#
# What DID separate the two cases cleanly is the mass in the deep-change
# tail — the one quantity a real flood must produce and noise cannot fake:
#
#     no-signal Kanalia scene : 1.24% of pixels beyond +-3 dB
#     synthetic real flood    : 17.38%                          (14x)
#
# A flood covering a meaningful share of the AOI necessarily puts real mass
# there; a narrow noise distribution centred near 0 dB cannot. 2% is set an
# order of magnitude below the real-flood case and comfortably above the
# observed noise case, so it separates them without being tuned to either.
MIN_DEEP_TAIL_FRACTION = 0.02      # >=2% of valid px beyond the deep cut
DEEP_TAIL_DB = 3.0                 # |change| >= 3 dB — the conventional bar


def refined_lee(img: np.ndarray, window: int = REFINED_LEE_WINDOW) -> np.ndarray:
    """Refined Lee speckle filter (edge-aware local-statistics MMSE).

    Speckle in SAR intensity is multiplicative; a plain box filter smears
    edges. Lee's local-statistics estimator shrinks toward the local mean in
    proportion to how much of the local variance is explained by speckle
    (via the ENL-derived noise variance), so homogeneous areas are smoothed
    hard while high-contrast structure (shorelines) is preserved.

    Operates on linear intensity (NOT dB) — the multiplicative noise model
    only holds there.
    """
    from scipy.ndimage import uniform_filter

    arr = np.asarray(img, dtype="float32")
    finite = np.isfinite(arr)
    work = np.where(finite, arr, 0.0).astype("float32")

    mean = uniform_filter(work, size=window)
    mean_sq = uniform_filter(work * work, size=window)
    var = np.maximum(mean_sq - mean * mean, 0.0)

    # Speckle noise variance for L looks: sigma_v^2 = 1/L (multiplicative).
    cu2 = 1.0 / ENL
    # MMSE weight: how much of the local variance is real signal.
    denom = var + (mean * mean) * cu2
    weight = np.where(denom > 1e-12, np.maximum(var - (mean * mean) * cu2, 0.0) / denom, 0.0)
    out = mean + weight * (work - mean)
    return np.where(finite, out, np.nan).astype("float32")


def build_baseline(
    pre_event_stack: list[np.ndarray],
) -> Optional[dict]:
    """Median of N pre-event same-orbit scenes (linear intensity).

    A single pre-event scene carries full speckle plus whatever transient
    conditions that day held (a rain shower, a wet-soil morning). The median
    across acquisitions suppresses both: speckle because it is
    zero-median-biased noise across independent looks, transients because a
    one-off excursion cannot move a median of 3+.

    Returns `{"baseline", "scene_count", "confidence_penalty"}` or None when
    the stack is empty. Fewer than BASELINE_TARGET_SCENES proceeds but
    records a proportional confidence penalty rather than pretending the
    reference is as good as a full one.
    """
    stack = [np.asarray(s, dtype="float32") for s in pre_event_stack if s is not None]
    if not stack:
        return None
    cube = np.stack(stack, axis=0)
    with np.errstate(all="ignore"):
        baseline = np.nanmedian(cube, axis=0).astype("float32")
    n = len(stack)
    # Penalty scales with how far below target the reference is: a 1-scene
    # baseline is speckle-dominated and genuinely less trustworthy.
    penalty = 0.0 if n >= BASELINE_TARGET_SCENES else 0.15 * (BASELINE_TARGET_SCENES - n)
    return {
        "baseline": baseline,
        "scene_count": n,
        "confidence_penalty": round(penalty, 3),
    }


def log_ratio(post: np.ndarray, pre: np.ndarray) -> np.ndarray:
    """Change image rho = 10*log10(post/pre), in dB.

    The RATIO (not difference) is the statistically correct operator for
    SAR: speckle is multiplicative, so a ratio turns it into an additive,
    roughly-Gaussian term in log space — which is exactly what makes the
    subsequent thresholding statistically valid. Negative rho = backscatter
    dropped = candidate flood.
    """
    p = np.asarray(post, dtype="float32")
    q = np.asarray(pre, dtype="float32")
    out = np.full(p.shape, np.nan, dtype="float32")
    ok = np.isfinite(p) & np.isfinite(q) & (p > 0) & (q > 0)
    out[ok] = 10.0 * np.log10(p[ok] / q[ok])
    return out


def hand_mask(dem: np.ndarray, transform=None, max_hand_m: float = HAND_MAX_M) -> Optional[np.ndarray]:
    """Boolean mask: True where water is PHYSICALLY IMPLAUSIBLE (HAND too high).

    Height Above Nearest Drainage approximated from the DEM: drainage is
    taken as the local minimum surface (a large-window minimum filter, the
    standard cheap approximation when a full flow-routing solution is not
    available), and HAND is the elevation above that local drainage level.
    Pixels more than `max_hand_m` above their nearest drainage cannot hold
    ponded floodwater — a dark return there is radar shadow, not water.
    """
    if dem is None:
        return None
    try:
        from scipy.ndimage import minimum_filter
    except ImportError:
        return None
    d = np.asarray(dem, dtype="float32")
    finite = np.isfinite(d)
    if not finite.any():
        return None
    filled = np.where(finite, d, np.nanmax(d[finite]))
    # ~1 km window at 10 m: wide enough to reach a real drainage line in
    # typical terrain, narrow enough not to flatten a whole valley system.
    drainage = minimum_filter(filled, size=101, mode="nearest")
    hand = filled - drainage
    return (hand > max_hand_m) & finite


def layover_shadow_mask(
    dem: np.ndarray,
    incidence_deg: float = 39.0,
    orbit_direction: str = "DESCENDING",
) -> Optional[np.ndarray]:
    """Boolean mask: True where local geometry makes backscatter meaningless.

    Layover occurs where the local slope facing the sensor exceeds the
    incidence angle; shadow where the slope facing away exceeds its
    complement. Backscatter in either zone carries no ground information, so
    those pixels are excluded rather than allowed to contribute a spurious
    "dark = water" vote.
    """
    if dem is None:
        return None
    d = np.asarray(dem, dtype="float32")
    if d.ndim != 2 or min(d.shape) < 3:
        return None
    gy, gx = np.gradient(np.nan_to_num(d, nan=float(np.nanmean(d))))
    # Range direction: right-looking sensor -> range is +x for DESCENDING,
    # -x for ASCENDING (sign flips which slopes face the sensor).
    sign = 1.0 if str(orbit_direction).upper().startswith("DESC") else -1.0
    slope_range_deg = np.degrees(np.arctan(sign * gx))
    layover = slope_range_deg > incidence_deg
    shadow = slope_range_deg < -(90.0 - incidence_deg)
    return layover | shadow


def _ki_on_tile(values: np.ndarray) -> Optional[dict]:
    from adaptive_threshold import kittler_illingworth_threshold

    return kittler_illingworth_threshold(values)


def tiled_threshold(
    change_db: np.ndarray,
    valid: np.ndarray,
    tile_size: int = TILE_SIZE_PX,
    fallback_db: float = -FLOOD_DROP_DB,
    fallback_rise_db: float = FLOOD_RISE_DB,
    direction: str = DETECTION_DIRECTION,
) -> dict:
    """Hierarchical tile-based threshold estimation on the change image.

    A single global threshold fails when flooding occupies a small part of a
    large scene: the global histogram is dominated by unchanged land and the
    flood mode never registers. The operational approach splits the image
    into tiles, tests each for bimodality, estimates the threshold ONLY from
    tiles that actually contain both populations, and applies the aggregate
    to the whole scene.

    **Two thresholds, not one, and this is the load-bearing part of the
    bidirectional fix.** A tile whose bimodal split sits at a NEGATIVE cut
    carries the open-water (backscatter drop) population; a tile whose split
    sits POSITIVE carries the flooded-vegetation (double-bounce rise)
    population. Estimating them separately — rather than taking `abs()` of a
    single cut — matters because the two mechanisms have genuinely different
    magnitudes, so one pooled statistic would be a physically meaningless
    average of two unrelated distributions. Pooling by sign also keeps the
    audit trail honest about WHICH signature a given run actually fired on.

    Returns `{"threshold", "rise_threshold", "method", "bimodal_tiles",
    "rise_bimodal_tiles", "tiles_tested", "tile_thresholds",
    "rise_tile_thresholds"}`. With no bimodal tile in a direction, that
    direction falls back to its physically-justified +-3 dB criterion.
    """
    want_drop = direction in ("both", "drop")
    want_rise = direction in ("both", "rise")

    h, w = change_db.shape
    thresholds: list[float] = []
    rise_thresholds: list[float] = []
    tested = 0
    for r0 in range(0, h, tile_size):
        for c0 in range(0, w, tile_size):
            tile = change_db[r0:r0 + tile_size, c0:c0 + tile_size]
            tile_valid = valid[r0:r0 + tile_size, c0:c0 + tile_size]
            vals = tile[tile_valid & np.isfinite(tile)]
            if vals.size < 500:
                continue
            tested += 1
            ki = _ki_on_tile(vals)
            if not (ki and ki["bimodal"]):
                continue
            if ki["threshold"] < 0:
                thresholds.append(ki["threshold"])   # open-water drop mode
            else:
                rise_thresholds.append(ki["threshold"])  # double-bounce rise mode

    # Median across bimodal tiles — robust to one oddly-split tile.
    if want_drop and thresholds:
        drop_thr = round(float(np.median(thresholds)), 3)
        drop_method = "tiled_kittler_illingworth"
    else:
        drop_thr = fallback_db
        drop_method = "fixed_3db_drop_fallback"

    if want_rise and rise_thresholds:
        rise_thr = round(float(np.median(rise_thresholds)), 3)
        rise_method = "tiled_kittler_illingworth"
    else:
        rise_thr = fallback_rise_db
        rise_method = "fixed_3db_rise_fallback"

    return {
        # `threshold` keeps its original name/meaning (the drop cut) so every
        # existing reader and stored result stays interpretable.
        "threshold": drop_thr if want_drop else None,
        "rise_threshold": rise_thr if want_rise else None,
        "method": drop_method if want_drop else rise_method,
        "drop_threshold_method": drop_method if want_drop else None,
        "rise_threshold_method": rise_method if want_rise else None,
        "bimodal_tiles": len(thresholds),
        "rise_bimodal_tiles": len(rise_thresholds),
        "tiles_tested": tested,
        "tile_thresholds": [round(t, 3) for t in thresholds[:20]],
        "rise_tile_thresholds": [round(t, 3) for t in rise_thresholds[:20]],
        "direction": direction,
    }


def morphological_cleanup(
    flood: np.ndarray,
    min_patch_px: int = MIN_FLOOD_PATCH_PIXELS,
    opening_iterations: int = MORPH_OPENING_ITERATIONS,
) -> np.ndarray:
    """Opening + minimum-area filter. Flood is spatially contiguous; isolated
    pixels surviving the threshold are speckle residue, not water."""
    try:
        from scipy.ndimage import binary_opening, label
    except ImportError:
        return flood
    cleaned = binary_opening(flood, iterations=opening_iterations)
    lab, n = label(cleaned)
    if n == 0:
        return cleaned
    counts = np.bincount(lab.ravel())
    too_small = np.where(counts < min_patch_px)[0]
    if too_small.size:
        cleaned[np.isin(lab, too_small)] = False
    return cleaned


def detect_flood_change(
    post_vv: np.ndarray,
    pre_event_stack: list[np.ndarray],
    dem: Optional[np.ndarray] = None,
    valid_mask: Optional[np.ndarray] = None,
    orbit_direction: str = "DESCENDING",
    incidence_deg: float = 39.0,
    direction: str = DETECTION_DIRECTION,
) -> dict:
    """Full S1 change-detection flood map.

    `post_vv` and every entry of `pre_event_stack` must be LINEAR intensity
    (raw GRD DN is fine — the calibration factor cancels) from the SAME
    relative orbit. An empty stack returns
    `status="insufficient_reference"` — deliberately NOT a fallback to
    absolute thresholding, which is the defect this whole module removes.

    `direction` selects which flood signature(s) to detect:
      - "both" (default) — open-water backscatter DROP *and*
        flooded-vegetation double-bounce RISE. Over agricultural floodplains
        the rise is the dominant mechanism, so a drop-only detector is
        structurally blind there.
      - "drop" — open water only (the pre-2026-07-29 behaviour).
      - "rise" — flooded vegetation only. Provided for ablation.
    """
    if not pre_event_stack:
        return {
            "status": "insufficient_reference",
            "reason": (
                "No same-relative-orbit pre-event scene available. Change "
                "detection is the only defensible SAR flood method on "
                "uncalibrated GRD; absolute thresholding is not used as a "
                "fallback because it cannot separate water from roads/sand "
                "and its dB cut points do not apply to uncalibrated DN."
            ),
            "flood_mask": None,
        }

    # Shape contract, enforced explicitly (the first live forced-S1 run
    # failed here with a bare numpy broadcast error, which the caller then
    # swallowed into the unusable absolute-threshold fallback). Mismatched
    # references are dropped with a named reason rather than allowed to
    # abort the whole method: a 2-scene aligned baseline is worth more than
    # no change detection at all.
    post_shape = np.asarray(post_vv).shape
    aligned, dropped = [], 0
    for scene in pre_event_stack:
        arr = np.asarray(scene)
        if arr.shape == post_shape:
            aligned.append(arr)
        else:
            dropped += 1
            logger.warning(
                "Pre-event scene shape %s != post-event %s — excluded from "
                "the baseline (the log-ratio is elementwise; an unaligned "
                "reference cannot be compared)", arr.shape, post_shape,
            )
    if not aligned:
        return {
            "status": "insufficient_reference",
            "reason": (
                f"All {dropped} pre-event scene(s) were misaligned with the "
                "post-event grid. Absolute thresholding is NOT substituted."
            ),
            "flood_mask": None,
        }

    base = build_baseline([refined_lee(s) for s in aligned])
    if base is None:
        return {"status": "insufficient_reference", "reason": "empty baseline stack",
                "flood_mask": None}

    post_f = refined_lee(post_vv)
    change = log_ratio(post_f, base["baseline"])

    valid = np.isfinite(change)
    if valid_mask is not None:
        valid &= valid_mask

    masks_applied = []
    hand = hand_mask(dem) if dem is not None else None
    if hand is not None:
        valid &= ~hand
        masks_applied.append("hand")
    ls = layover_shadow_mask(dem, incidence_deg, orbit_direction) if dem is not None else None
    if ls is not None:
        valid &= ~ls
        masks_applied.append("layover_shadow")

    thr = tiled_threshold(change, valid, direction=direction)

    # Bidirectional detection. The two masks are built separately, then
    # unioned — NOT collapsed into a single `abs(change) > t` test, because
    # the drop and rise cuts are independently estimated and generally have
    # different magnitudes (see tiled_threshold's docstring).
    drop_flood = (
        valid & (change <= thr["threshold"])
        if thr["threshold"] is not None else np.zeros_like(valid)
    )
    rise_flood = (
        valid & (change >= thr["rise_threshold"])
        if thr["rise_threshold"] is not None else np.zeros_like(valid)
    )
    flood = drop_flood | rise_flood
    flood = morphological_cleanup(flood)

    # Attribute the surviving (post-cleanup) detections back to a mechanism,
    # so the result says which physics actually fired rather than implying
    # both did. Morphology can only remove pixels, so intersecting is exact.
    drop_final = flood & drop_flood
    rise_final = flood & rise_flood

    valid_count = int(valid.sum())
    flood_count = int(flood.sum())
    drop_count = int(drop_final.sum())
    rise_count = int(rise_final.sum())

    # --- Signal detectability (2026-07-29) --------------------------------
    # Does this scene contain a flood signal at all, or did the threshold cut
    # noise? Both criteria are computed WITHOUT reference to the flood mask,
    # so neither can be satisfied by the act of thresholding (see the
    # discarded-first-attempt note at MIN_BIMODAL_TILE_FRACTION).
    vals = change[valid]
    vals = vals[np.isfinite(vals)]
    deep_tail_fraction = (
        round(float((np.abs(vals) >= DEEP_TAIL_DB).mean()), 6)
        if vals.size else None
    )
    signal_detectable = None
    if deep_tail_fraction is not None:
        signal_detectable = bool(deep_tail_fraction >= MIN_DEEP_TAIL_FRACTION)
        if not signal_detectable:
            logger.warning(
                "SAR change detection: this scene shows NO flood signal — "
                "only %.3f%% of valid pixels exceed +-%.1f dB (criterion "
                "%.1f%%). The change image is a single narrow mode, so any "
                "threshold cuts noise rather than water and the resulting "
                "extent is INDETERMINATE, NOT a low flood reading. The most "
                "common cause is an acquisition post-dating the flood peak, "
                "with the water already receded.",
                100.0 * deep_tail_fraction, DEEP_TAIL_DB,
                100.0 * MIN_DEEP_TAIL_FRACTION,
            )

    return {
        "status": "complete",
        "flood_mask": flood,
        "change_db": change,
        "water_percent": round(100.0 * flood_count / valid_count, 2) if valid_count else 0.0,
        "mean_change_db": (
            round(float(np.nanmean(change[flood])), 4) if flood_count else None
        ),
        # --- Bidirectional audit trail -----------------------------------
        # Which mechanism produced the detection. On flooded farmland the
        # rise (double-bounce) share should dominate; on open water the drop
        # share should. A run reporting ~100% of one direction is a
        # meaningful physical statement, not a formatting detail.
        "detection_direction": direction,
        "open_water_drop_pixels": drop_count,
        "flooded_vegetation_rise_pixels": rise_count,
        "open_water_drop_percent": (
            round(100.0 * drop_count / flood_count, 2) if flood_count else None
        ),
        "flooded_vegetation_rise_percent": (
            round(100.0 * rise_count / flood_count, 2) if flood_count else None
        ),
        "rise_threshold_db": thr["rise_threshold"],
        "rise_threshold_method": thr["rise_threshold_method"],
        "rise_bimodal_tiles": thr["rise_bimodal_tiles"],
        # --- Signal detectability audit trail ----------------------------
        # `signal_detectable is False` means: a mask was produced, but the
        # detected pixels are not meaningfully different from the rest of
        # the scene. Consumers must treat the extent as indeterminate rather
        # than as a measurement of no/low flood. None = not computable
        # (nothing detected, or a degenerate population).
        "signal_detectable": signal_detectable,
        "deep_tail_fraction": deep_tail_fraction,
        
        "signal_criteria": {
            
            "min_deep_tail_fraction": MIN_DEEP_TAIL_FRACTION,
            "deep_tail_db": DEEP_TAIL_DB,
        },
        # Full audit trail — a future comparison against a different filter,
        # baseline depth or threshold needs to know exactly what ran.
        "method": "sar_change_detection_log_ratio",
        "speckle_filter": "refined_lee",
        "speckle_window": REFINED_LEE_WINDOW,
        "enl": ENL,
        "baseline_scene_count": base["scene_count"],
        "baseline_scenes_dropped_misaligned": dropped,
        "baseline_confidence_penalty": base["confidence_penalty"],
        "baseline_target_scenes": BASELINE_TARGET_SCENES,
        "threshold_db": thr["threshold"],
        "threshold_method": thr["method"],
        "bimodal_tiles": thr["bimodal_tiles"],
        "tiles_tested": thr["tiles_tested"],
        "hand_max_m": HAND_MAX_M if "hand" in masks_applied else None,
        "masks_applied": masks_applied,
        "min_flood_patch_px": MIN_FLOOD_PATCH_PIXELS,
        "index_calibrated": True,  # the RATIO is calibration-independent
        "index_units": "dB_change_ratio",
    }
