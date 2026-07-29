"""Phases 4b/5b/5c (science/full-pass): hazard-agent science fixes.

Covers:
  1. p90 slope replaces the mean — a district with one steep valley and
     otherwise flat terrain must NOT average to LOW (the exact failure the
     mean produced);
  2. the slope trace records both statistics and the grid size, so past
     runs stay re-derivable;
  3. distance decay — a M6.0 at 240 km and a M6.0 at 10 km must NOT produce
     the same verdict;
  4. the driving event is named by USGS id with its magnitude_type, so the
     mb/ml/mw conflation is visible;
  5. threshold_applied strings state their basis honestly and cite nothing
     false.

Offline and deterministic — no network.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analyzer

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


def test_p90_beats_mean_on_local_valley():
    """10x10 grid: flat everywhere except one steep valley column. The mean
    dilutes it to LOW; the p90 sees it."""
    n = 10
    lats = [39.0 + 0.01 * i for i in range(n)]
    lngs = [22.0 + 0.01 * j for j in range(n)]
    elevations = []
    for i in range(n):
        for j in range(n):
            # Flat plain at 100 m, except a steep gorge in columns 4-5.
            elevations.append(100.0 if j not in (4, 5) else 100.0 + 900.0 * (j - 4))
    out = analyzer._slope_from_grid_traced(elevations, lats, lngs)
    if out is None:
        bad("_slope_from_grid_traced returned None")
        return
    slope, trace = out
    if trace["statistic"] == "p90" and trace["resulting_value_deg"] == trace["p90_slope_deg"]:
        ok(f"p90 is the applied statistic ({trace['p90_slope_deg']} deg)")
    else:
        bad(f"statistic wrong: {trace['statistic']}")
    if trace["p90_slope_deg"] > trace["mean_slope_deg"]:
        ok(f"p90 {trace['p90_slope_deg']} > mean {trace['mean_slope_deg']} — "
           "the local steep feature survives instead of being averaged away")
    else:
        bad(f"p90 {trace['p90_slope_deg']} did not exceed mean "
            f"{trace['mean_slope_deg']}")
    if "mean_slope_deg" in trace and "grid_n" in trace:
        ok(f"trace records both statistics + grid_n={trace['grid_n']} "
           "(past runs stay re-derivable)")
    else:
        bad(f"trace missing audit fields: {sorted(trace)}")


def test_grid_density_raised():
    if analyzer._DEM_GRID == 10:
        ok("_DEM_GRID raised to 10x10 = 100 points (OpenTopoData's per-request max)")
    else:
        bad(f"_DEM_GRID is {analyzer._DEM_GRID}, expected 10")


def _quake(mag, lon, lat, eid, magtype="mw"):
    return {
        "id": eid,
        "properties": {"mag": mag, "magType": magtype, "time": 0},
        "geometry": {"coordinates": [lon, lat, 10.0]},
    }


def test_distance_decay_separates_near_and_far():
    bbox = [73.0, 33.0, 73.2, 33.2]  # centroid ~ (73.1, 33.1)
    near = {"earthquakes": [_quake(6.0, 73.1, 33.15, "us_near")], "count": 1,
            "source": "usgs"}
    # ~240 km east.
    far = {"earthquakes": [_quake(6.0, 75.68, 33.1, "us_far")], "count": 1,
           "source": "usgs"}
    rn = asyncio.run(analyzer.analyze_earthquake(bbox, near))
    rf = asyncio.run(analyzer.analyze_earthquake(bbox, far))
    en = rn["evidence_basis"]["effective_magnitude"]
    ef = rf["evidence_basis"]["effective_magnitude"]
    if en > ef:
        ok(f"M6.0 near ({en}) outranks M6.0 at ~240 km ({ef}) — "
           "identical raw magnitudes now separate")
    else:
        bad(f"distance decay ineffective: near={en} far={ef}")
    if rn["risk"] != rf["risk"]:
        ok(f"verdicts differ by distance: near={rn['risk']} far={rf['risk']}")
    else:
        bad(f"same verdict despite 240 km difference: {rn['risk']}")


def test_driving_event_named_with_magtype():
    bbox = [73.0, 33.0, 73.2, 33.2]
    data = {
        "earthquakes": [
            _quake(6.4, 76.0, 33.1, "us_big_far", "mb"),   # bigger but far
            _quake(5.2, 73.1, 33.12, "us_small_near", "ml"),  # smaller, close
        ],
        "count": 2, "source": "usgs",
    }
    res = asyncio.run(analyzer.analyze_earthquake(bbox, data))
    drv = res["evidence_basis"]["verdict_driving_event"]
    if drv and drv.get("usgs_event_id") and drv.get("magnitude_type"):
        ok(f"driving event named: {drv['usgs_event_id']} M{drv['magnitude']} "
           f"[{drv['magnitude_type']}] at {drv['distance_km']} km")
    else:
        bad(f"driving event not recorded: {drv}")
    if drv and drv["usgs_event_id"] == "us_small_near":
        ok("nearer moderate quake outranks the distant larger one after decay")
    else:
        bad(f"decay did not reorder: driving={drv and drv['usgs_event_id']}")
    if "engineering judgement" in res["evidence_basis"]["distance_decay_basis"]:
        ok("decay model basis stated as engineering judgement, no false citation")
    else:
        bad("decay basis text missing the honesty statement")


def test_threshold_strings_are_honest():
    bbox = [73.0, 33.0, 73.2, 33.2]
    res = asyncio.run(analyzer.analyze_earthquake(
        bbox, {"earthquakes": [_quake(3.0, 73.1, 33.1, "us_tiny")], "count": 1,
               "source": "usgs"}))
    ta = res["diagnostics"]["threshold_applied"]
    if "effective_magnitude" in ta and "engineering judgement" in ta:
        ok("earthquake threshold string names the decayed basis honestly")
    else:
        bad(f"threshold string: {ta}")
    ls = asyncio.run(analyzer.analyze_landslide(
        bbox, {"events": [], "count": 0}, {"slope_estimate": 35.0, "available": True}))
    lta = ls["diagnostics"]["threshold_applied"]
    if "p90_slope" in lta and "repose" in lta:
        ok("landslide threshold string states the repose-angle basis for >30")
    else:
        bad(f"landslide threshold string: {lta}")
    ls_low = asyncio.run(analyzer.analyze_landslide(
        bbox, {"events": [], "count": 0}, {"slope_estimate": 8.0, "available": True}))
    if "no physical basis claimed" in ls_low["diagnostics"]["threshold_applied"]:
        ok("the 15 deg screening floor admits it has no physical basis")
    else:
        bad(f"low-slope string: {ls_low['diagnostics']['threshold_applied']}")


if __name__ == "__main__":
    print("=" * 64)
    print("PHASES 4b/5b/5c — HAZARD SCIENCE (p90 slope, distance decay)")
    print("=" * 64)
    test_p90_beats_mean_on_local_valley()
    test_grid_density_raised()
    test_distance_decay_separates_near_and_far()
    test_driving_event_named_with_magtype()
    test_threshold_strings_are_honest()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
