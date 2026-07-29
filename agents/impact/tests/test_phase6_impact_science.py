"""Phase 6 (science/full-pass): impact-agent science fixes.

Covers:
  6a — gridded population exposure: the intersection sums real raster
       pixels inside the hazard polygon, and an unreachable raster reports
       method="unavailable" with population None (never a fabricated or
       silently-zero number);
  6b — infrastructure subsetting: facilities are constrained geometrically,
       and any LLM "at risk" figure is clamped so it can never exceed the
       real OSM count;
  6c — the vulnerability rubric is enforced in CODE, with the model's
       original score and the rules that fired both recorded.

Offline and deterministic — the raster read is exercised against a locally
written GeoTIFF, no network.
"""

import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.population_exposure import (  # noqa: E402
    clamp_at_risk,
    facilities_in_polygon,
    population_in_polygon,
)
from tasks.vulnerability import enforce_vulnerability_rubric  # noqa: E402

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


def _write_pop_raster(path):
    """10x10 grid over lon 73.0-73.1, lat 33.0-33.1; every pixel = 100 people."""
    import rasterio
    from rasterio.transform import from_bounds

    arr = np.full((10, 10), 100.0, dtype="float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=10, width=10, count=1,
        dtype="float32", crs="EPSG:4326",
        transform=from_bounds(73.0, 33.0, 73.1, 33.1, 10, 10),
    ) as dst:
        dst.write(arr, 1)


def test_population_intersection_is_real():
    from shapely.geometry import box

    tmp = os.path.join(tempfile.mkdtemp(prefix="pop_"), "pop.tif")
    _write_pop_raster(tmp)
    # Quarter of the raster (lower-left 5x5 pixels) = 25 px * 100 = 2500.
    poly = box(73.0, 33.0, 73.05, 33.05)
    res = population_in_polygon(poly, "PAK", source_url=tmp)
    if res["method"] == "worldpop_polygon_intersection" and 2000 <= res["population"] <= 3000:
        ok(f"population summed from real pixels inside the polygon "
           f"({res['population']} from {res['pixels']} px)")
    else:
        bad(f"exposure wrong: {res}")
    if res["polygon_area_km2"] > 0:
        ok(f"hazard polygon area computed equal-area ({res['polygon_area_km2']} km2)")
    else:
        bad(f"area not computed: {res['polygon_area_km2']}")


def test_missing_raster_reports_unavailable():
    from shapely.geometry import box

    res = population_in_polygon(box(73.0, 33.0, 73.05, 33.05), "PAK",
                                source_url="/nonexistent/nope.tif")
    if res["method"] == "unavailable" and res["population"] is None:
        ok("unreachable raster -> method=unavailable, population None (not 0)")
    else:
        bad(f"degradation wrong: {res}")


def test_no_geometry_refuses():
    res = population_in_polygon(None, "PAK")
    if res["method"] == "unavailable" and res["population"] is None:
        ok("no hazard geometry -> exposure not computed, stated explicitly")
    else:
        bad(f"expected refusal, got {res}")


def test_facilities_constrained_geometrically():
    from shapely.geometry import box

    poly = box(73.0, 33.0, 73.05, 33.05)
    facilities = [
        {"name": "in1", "lat": 33.01, "lon": 73.01},
        {"name": "in2", "lat": 33.04, "lon": 73.04},
        {"name": "out1", "lat": 33.09, "lon": 73.09},
        {"name": "nocoord"},
    ]
    res = facilities_in_polygon(facilities, poly)
    if res["inside"] == 2 and res["total"] == 4 and res["unlocatable"] == 1:
        ok("facilities constrained to the hazard extent "
           f"(2 inside / 4 total / 1 unlocatable, counted not dropped)")
    else:
        bad(f"subsetting wrong: {res}")


def test_llm_at_risk_is_clamped():
    # The audit's failure mode: LLM claims more at risk than exist.
    c = clamp_at_risk(999, real_total=12, geometric=None)
    if c["value"] == 12 and c["clamped"] and c["basis"].startswith("llm_clamped"):
        ok("LLM at-risk count clamped to the real OSM total (999 -> 12)")
    else:
        bad(f"clamp failed: {c}")
    g = clamp_at_risk(999, real_total=12, geometric=3)
    if g["value"] == 3 and g["basis"] == "geometric_intersection":
        ok("geometric intersection takes precedence over the LLM figure")
    else:
        bad(f"precedence wrong: {g}")


def test_rubric_enforced_in_code():
    # LLM under-scores a case the rules say is at least 8.0.
    r = enforce_vulnerability_rubric(3.0, pop=2_000_000, hospitals=15)
    if r["vulnerability_score"] == 8.0 and r["vulnerability_rubric_adjusted"]:
        ok(f"rubric floor enforced in code (LLM 3.0 -> 8.0)")
    else:
        bad(f"floor not enforced: {r}")
    if r["vulnerability_score_llm_raw"] == 3.0 and r["vulnerability_rubric_applied"]:
        ok(f"original LLM score and applied rules both recorded "
           f"({r['vulnerability_rubric_applied']})")
    else:
        bad(f"audit fields missing: {r}")
    # All-elevated risks -> 9.0 floor, the highest applicable.
    r2 = enforce_vulnerability_rubric(
        5.0, pop=2_000_000, hospitals=15,
        flood="HIGH", eq="CRITICAL", ls="HIGH")
    if r2["vulnerability_score"] == 9.0:
        ok("highest applicable floor wins (all-elevated -> 9.0)")
    else:
        bad(f"floor precedence wrong: {r2['vulnerability_score']}")
    # A score already above every floor is left alone.
    r3 = enforce_vulnerability_rubric(9.5, pop=2_000_000, hospitals=15)
    if r3["vulnerability_score"] == 9.5 and not r3["vulnerability_rubric_adjusted"]:
        ok("a compliant LLM score is not altered")
    else:
        bad(f"compliant score altered: {r3}")
    # Cap at 10.
    r4 = enforce_vulnerability_rubric(50.0, pop=100, hospitals=1)
    if r4["vulnerability_score"] == 10.0:
        ok("score capped at 10.0")
    else:
        bad(f"cap not applied: {r4['vulnerability_score']}")
    # Garbage input degrades to the neutral default, never raises.
    r5 = enforce_vulnerability_rubric("not-a-number", pop=100, hospitals=1)
    if r5["vulnerability_score"] == 5.0:
        ok("non-numeric LLM score degrades to the neutral default")
    else:
        bad(f"garbage handling wrong: {r5}")


if __name__ == "__main__":
    print("=" * 64)
    print("PHASE 6 — IMPACT SCIENCE (exposure, subsetting, rubric)")
    print("=" * 64)
    test_population_intersection_is_real()
    test_missing_raster_reports_unavailable()
    test_no_geometry_refuses()
    test_facilities_constrained_geometrically()
    test_llm_at_risk_is_clamped()
    test_rubric_enforced_in_code()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
