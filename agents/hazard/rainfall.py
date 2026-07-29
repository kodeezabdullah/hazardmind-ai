"""Phase 5 — rainfall as BOUNDED context (GPM IMERG).

Lives in the hazard agent, not satellite: it is not a pixel operation, the
landslide path needs it too, and hazard already owns the third-party context
fetches (GDACS, USGS, OpenTopoData).

**Percentile, not millimetres.** "180 mm fell" carries no meaning alone:
50 mm in Karachi is extraordinary, 50 mm in Murree in July is an ordinary
day. Rainfall is therefore reported against local climatology — GPM's archive
starts in 2000, so the climatology comes from the same source and needs no
additional integration.

**THE CONSTRAINT THAT MATTERS — rainfall may never veto a detection.**

Two rules, both non-negotiable, and both encoded structurally here rather
than left to a prompt:

1. *Rainfall is an AOI-LEVEL SCALAR.* It must never spatially modulate risk
   within the AOI. Rain falls on the catchment; water arrives at the river.
   Using local rainfall to reduce risk in part of an AOI is physically wrong.
   This module therefore returns a single scalar per AOI and exposes no
   per-pixel or per-zone product at all — the wrong thing is not merely
   discouraged, it is absent.

2. *No-rain never suppresses a detection.* The Swat case makes this concrete:
   rainfall falls in the upper valley and flooding arrives 60-100 km
   downstream hours later, so local rainfall at Mingora can read as normal
   during a catastrophic flood. Snowmelt and glacial lake outburst floods
   produce flooding with no rainfall at all. A rainfall-based veto would
   suppress exactly the events that kill most people — those arriving with no
   local warning. So:
       rain + water detected  -> confidence UP,   driver stated
       no rain + water        -> confidence DOWN, "investigate" flag,
                                 DETECTION STANDS
   `apply_confidence_adjustment` can only move confidence, never risk level,
   and `CONTEXT_TOTAL_CAP` bounds the whole context stack.

**AOI-level limitation, stated.** For a mountainous catchment the AOI-level
figure is a poor proxy for what drove the flood, because the rain fell
outside the AOI. Upstream catchment tracing (HydroSHEDS) is the correct
eventual answer and is explicitly out of scope; until then this module
reports `catchment_limited: True` so no consumer mistakes an AOI-level
reading for a catchment one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# --- Bounded influence ------------------------------------------------------
# Per the task's constraint: no single context layer may move confidence by
# more than +-0.15, and all context layers together by more than +-0.30.
# These are the ONLY knobs by which context touches the output — no context
# layer may change a risk LEVEL, ever.
MAX_SINGLE_LAYER_INFLUENCE = 0.15
CONTEXT_TOTAL_CAP = 0.30

# Percentile bands against local climatology. Round numbers, chosen for
# legibility and labelled as engineering judgement rather than derived — the
# defensible part is that the comparison is to LOCAL climatology at all,
# not the specific cut points.
EXTREME_PERCENTILE = 95.0
HIGH_PERCENTILE = 80.0
NORMAL_PERCENTILE = 50.0

# Accumulation window for flood-relevant rainfall.
DEFAULT_WINDOW_HOURS = 72

# GPM IMERG archive start — the climatology baseline's real lower bound.
GPM_ARCHIVE_START_YEAR = 2000


def classify_percentile(pct: Optional[float]) -> str:
    if pct is None:
        return "unknown"
    if pct >= EXTREME_PERCENTILE:
        return "extreme"
    if pct >= HIGH_PERCENTILE:
        return "high"
    if pct >= NORMAL_PERCENTILE:
        return "normal"
    return "below_normal"


def timing_relative_to_imagery(
    peak_rain_days_before_imagery: Optional[float],
    scene_age_days: Optional[float] = None,
) -> dict:
    """Pair rainfall timing with the imagery date (`scene_age_days` exists).

    Interpretation, not a number: whether the extent is likely receding,
    growing, or possibly a different event entirely.
    """
    if peak_rain_days_before_imagery is None:
        return {"interpretation": "unknown", "detail": "no rainfall timing available"}
    d = peak_rain_days_before_imagery
    if d < 0:
        return {"interpretation": "rain_after_imagery",
                "detail": "Peak rainfall postdates the imagery — the scene "
                          "may predate the flood entirely."}
    if d <= 1:
        return {"interpretation": "rain_ongoing_or_just_ended",
                "detail": "Rain fell within a day of the acquisition — "
                          "extent may still be GROWING."}
    if d <= 5:
        return {"interpretation": "water_may_be_receding",
                "detail": f"Peak rain {d:.1f} days before the imagery — "
                          "water may be receding from its maximum."}
    return {"interpretation": "possibly_different_event",
            "detail": f"Peak rain {d:.1f} days before the imagery — this "
                      "may not be the same event."}


def assess_rainfall_context(
    accumulation_mm: Optional[float],
    climatology_percentile: Optional[float],
    seasonal_norm_mm: Optional[float] = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    peak_rain_days_before_imagery: Optional[float] = None,
    scene_age_days: Optional[float] = None,
    mountainous_catchment: bool = False,
) -> dict:
    """AOI-level rainfall context. A SCALAR — never a spatial field.

    Returns a dict describing what fell and how unusual it was; it makes no
    risk claim of its own. `apply_confidence_adjustment` is the only path by
    which any of this touches an output, and it is bounded.
    """
    band = classify_percentile(climatology_percentile)
    pct_of_norm = None
    if accumulation_mm is not None and seasonal_norm_mm:
        pct_of_norm = round(100.0 * accumulation_mm / seasonal_norm_mm, 1)

    return {
        "available": accumulation_mm is not None,
        "accumulation_mm": accumulation_mm,
        "window_hours": window_hours,
        "climatology_percentile": climatology_percentile,
        "percent_of_seasonal_norm": pct_of_norm,
        "band": band,
        "climatology_source": (
            f"GPM IMERG, {GPM_ARCHIVE_START_YEAR}-present, same source as the "
            "accumulation (no additional integration)"
        ),
        "timing": timing_relative_to_imagery(
            peak_rain_days_before_imagery, scene_age_days
        ),
        # Structural honesty: this is an AOI-level scalar and must never be
        # read as a catchment figure.
        "scope": "aoi_level_scalar",
        "spatially_varying": False,
        "catchment_limited": bool(mountainous_catchment),
        "catchment_note": (
            "AOI-level rainfall is a poor proxy for the driver in a "
            "mountainous catchment: rain falls upstream and the flood arrives "
            "60-100 km downstream hours later (the Swat case). Upstream "
            "tracing via HydroSHEDS is the correct answer and is out of scope."
            if mountainous_catchment else None
        ),
    }


def apply_confidence_adjustment(
    rainfall: dict,
    water_detected: bool,
    other_context_adjustments: Optional[list[float]] = None,
) -> dict:
    """The ONLY path by which rainfall touches an output. Bounded, and it
    can never veto.

    Returns `{"delta", "capped_delta", "driver", "investigate", "vetoed"}`.
    `vetoed` is always False — it exists so the invariant is visible in the
    output and assertable by a test, not merely documented.
    """
    delta = 0.0
    driver = None
    investigate = False

    if not rainfall.get("available"):
        driver = "rainfall unavailable — no adjustment applied"
    elif water_detected:
        band = rainfall.get("band")
        if band == "extreme":
            delta = MAX_SINGLE_LAYER_INFLUENCE
            driver = (
                f"Water detected AND rainfall at the "
                f"{rainfall.get('climatology_percentile')}th percentile of "
                "local climatology — the detection has a stated meteorological "
                "driver."
            )
        elif band == "high":
            delta = MAX_SINGLE_LAYER_INFLUENCE * 0.6
            driver = "Water detected with above-normal rainfall."
        elif band in ("normal", "below_normal"):
            # DOWN, but the detection STANDS — see the module docstring.
            delta = -MAX_SINGLE_LAYER_INFLUENCE * 0.5
            investigate = True
            driver = (
                "Water detected WITHOUT unusual local rainfall. The detection "
                "STANDS — this is flagged for investigation, not suppressed. "
                "Upstream rainfall, snowmelt and glacial lake outburst floods "
                "all produce flooding with normal local rain."
            )
    else:
        driver = "No water detected — rainfall context not applied to a detection."

    # Cap the single layer, then the whole context stack.
    delta = max(-MAX_SINGLE_LAYER_INFLUENCE, min(MAX_SINGLE_LAYER_INFLUENCE, delta))
    others = list(other_context_adjustments or [])
    total = sum(others) + delta
    capped_total = max(-CONTEXT_TOTAL_CAP, min(CONTEXT_TOTAL_CAP, total))
    # Attribute the cap back to this layer's share.
    capped_delta = delta - (total - capped_total) if total != capped_total else delta
    capped_delta = max(-MAX_SINGLE_LAYER_INFLUENCE,
                       min(MAX_SINGLE_LAYER_INFLUENCE, capped_delta))

    return {
        "delta": round(delta, 4),
        "capped_delta": round(capped_delta, 4),
        "context_total_before_cap": round(total, 4),
        "context_total_after_cap": round(capped_total, 4),
        "single_layer_cap": MAX_SINGLE_LAYER_INFLUENCE,
        "total_context_cap": CONTEXT_TOTAL_CAP,
        "driver": driver,
        "investigate": investigate,
        # INVARIANTS, surfaced so they are assertable rather than assumed.
        "vetoed": False,
        "changed_risk_level": False,
    }
