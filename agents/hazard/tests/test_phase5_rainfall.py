"""Phase 5 — rainfall as BOUNDED context.

The tests that matter here are the NEGATIVE ones. Anyone can check that
heavy rain raises confidence; the load-bearing property is that an ABSENCE
of rain never suppresses a detection, because that would silently kill
exactly the events that kill most people — upstream-driven floods, snowmelt,
and glacial lake outburst floods, all of which arrive with normal local rain.
"""
import os
import sys

_HAZARD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HAZARD)

import rainfall as rf  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


print("\n=== 1. Percentile, not millimetres ===")
ctx = rf.assess_rainfall_context(180.0, 97.0, seasonal_norm_mm=53.0)
check("band is extreme at p97", ctx["band"] == "extreme", ctx["band"])
check("percent of seasonal norm computed",
      ctx["percent_of_seasonal_norm"] == 339.6,
      str(ctx["percent_of_seasonal_norm"]))
check("climatology source named", "GPM IMERG" in ctx["climatology_source"])
print(f"    180mm/72h = {ctx['percent_of_seasonal_norm']}% of norm, "
      f"p{ctx['climatology_percentile']} -> {ctx['band']}")
check("50mm is extraordinary in a dry climatology",
      rf.classify_percentile(96.0) == "extreme")
check("the SAME 50mm is ordinary in a wet one",
      rf.classify_percentile(45.0) == "below_normal")

print("\n=== 2. THE CONSTRAINT: no-rain NEVER vetoes a detection ===")
dry = rf.assess_rainfall_context(8.0, 30.0, seasonal_norm_mm=60.0)
adj = rf.apply_confidence_adjustment(dry, water_detected=True)
check("detection is NOT vetoed", adj["vetoed"] is False)
check("confidence goes DOWN, not to zero", -0.15 <= adj["capped_delta"] < 0,
      str(adj["capped_delta"]))
check("an investigate flag is raised", adj["investigate"] is True)
check("the driver states the detection STANDS", "STANDS" in adj["driver"])
check("it names the upstream/snowmelt/GLOF cases",
      "snowmelt" in adj["driver"] and "glacial" in adj["driver"].lower())
print(f"    no rain + water -> delta {adj['capped_delta']}, "
      f"investigate={adj['investigate']}, vetoed={adj['vetoed']}")

print("\n=== 3. Rain + water -> confidence up, driver stated ===")
wet = rf.assess_rainfall_context(180.0, 97.0, seasonal_norm_mm=53.0)
adj2 = rf.apply_confidence_adjustment(wet, water_detected=True)
check("confidence goes UP", adj2["capped_delta"] > 0, str(adj2["capped_delta"]))
check("a meteorological driver is stated",
      "driver" in adj2["driver"] or "percentile" in adj2["driver"])
check("no investigate flag needed", adj2["investigate"] is False)

print("\n=== 4. BOUNDED influence — the caps hold ===")
check("single layer never exceeds +-0.15",
      abs(adj2["capped_delta"]) <= 0.15 and abs(adj["capped_delta"]) <= 0.15)
# Stack it against other context layers already at the cap.
stacked = rf.apply_confidence_adjustment(
    wet, water_detected=True, other_context_adjustments=[0.15, 0.15]
)
check("total context is capped at +-0.30",
      abs(stacked["context_total_after_cap"]) <= 0.30,
      str(stacked["context_total_after_cap"]))
check("this layer's share is reduced to respect the total cap",
      stacked["capped_delta"] < adj2["capped_delta"],
      f"{stacked['capped_delta']} vs {adj2['capped_delta']}")
print(f"    0.15+0.15 others + {adj2['capped_delta']} rain -> "
      f"total {stacked['context_total_after_cap']} (cap 0.30)")

print("\n=== 5. Rainfall NEVER changes a risk level, and is NEVER spatial ===")
check("changed_risk_level invariant is exposed and False",
      adj["changed_risk_level"] is False and adj2["changed_risk_level"] is False)
check("context is declared an AOI-level scalar", ctx["scope"] == "aoi_level_scalar")
check("spatially_varying is False", ctx["spatially_varying"] is False)
# Structural, not just declarative: the module must expose no spatial product.
src = open(os.path.join(_HAZARD, "rainfall.py"), encoding="utf-8").read()
check("module exposes no per-pixel/per-zone rainfall product",
      "per_pixel" not in src and "rainfall_grid" not in src
      and "per_zone" not in src)

print("\n=== 6. Timing relative to the imagery ===")
for days, expect in ((0.5, "rain_ongoing_or_just_ended"),
                     (3.0, "water_may_be_receding"),
                     (14.0, "possibly_different_event"),
                     (-2.0, "rain_after_imagery")):
    t = rf.timing_relative_to_imagery(days)
    check(f"{days:+.1f} days -> {expect}", t["interpretation"] == expect,
          t["interpretation"])

print("\n=== 7. Mountainous catchment limitation is declared ===")
mnt = rf.assess_rainfall_context(50.0, 40.0, mountainous_catchment=True)
check("catchment_limited flagged", mnt["catchment_limited"] is True)
check("the Swat case is named", "Swat" in (mnt["catchment_note"] or ""))
check("HydroSHEDS named as the correct eventual answer",
      "HydroSHEDS" in (mnt["catchment_note"] or ""))
flat = rf.assess_rainfall_context(50.0, 40.0, mountainous_catchment=False)
check("not flagged on flat terrain", flat["catchment_limited"] is False)

print("\n=== 8. Unavailable rainfall degrades cleanly ===")
none_ctx = rf.assess_rainfall_context(None, None)
check("available is False", none_ctx["available"] is False)
a = rf.apply_confidence_adjustment(none_ctx, water_detected=True)
check("no adjustment when unavailable", a["capped_delta"] == 0.0)
check("still never vetoes", a["vetoed"] is False)

print(f"\n{'='*60}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*60}")
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
