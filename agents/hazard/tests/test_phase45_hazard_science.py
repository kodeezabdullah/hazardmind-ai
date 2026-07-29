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


def test_risk_polygons_points_at_real_source():
    """Phase 5d: risk_polygons must stop claiming {} and name the real
    source; hazard_zones.geometry gets a writer for the flood row only."""
    import agent as hazard_agent

    sample = {
        "boundaries": {"bbox": [73.0, 33.0, 73.2, 33.2], "risk_cities": ["X"]},
        "analysis": {"affected_area_km2": 12.0, "mean_value": 0.2},
        "artifacts": {"geojson_url": "https://example.invalid/zones.geojson"},
        "satellite": {"type": "sentinel-2"},
    }
    res = asyncio.run(analyzer.run_parallel_analysis(sample))
    rp = res.get("risk_polygons")
    if isinstance(rp, dict) and rp.get("geojson_url") == sample["artifacts"]["geojson_url"]:
        ok("risk_polygons names the real satellite geojson source (not {})")
    else:
        bad(f"risk_polygons still empty/wrong: {rp}")
    if res.get("geojson_url") == sample["artifacts"]["geojson_url"]:
        ok("geojson_url survives into the result write_to_db reads")
    else:
        bad(f"geojson_url did not survive: {res.get('geojson_url')}")
    # Unreachable URL -> None (NULL geometry), never a raise, never fabricated.
    if hazard_agent._hazard_geometry_geojson(
        {"geojson_url": "http://127.0.0.1:9/nope.json"}
    ) is None:
        ok("unreachable geometry source -> NULL, not a fabricated polygon")
    else:
        bad("geometry helper returned something for an unreachable URL")


def test_shakemap_supersedes_magnitude_heuristic():
    """Phase 5a: when USGS published a ShakeMap, modelled MMI decides the
    verdict instead of the magnitude heuristic — and the basis is recorded."""
    bbox = [73.0, 33.0, 73.2, 33.2]
    data = {"earthquakes": [_quake(4.2, 73.1, 33.1, "us_shake")], "count": 1,
            "source": "usgs"}

    async def _fake_shakemap(event_id, timeout_s=15):
        # A shallow M4.2 right under the city can shake hard (MMI VII)
        # even though the magnitude heuristic would call it MEDIUM.
        return {"available": True, "mmi": 7.2, "mmi_source": "usgs_shakemap",
                "pager_alert": "yellow", "fatalities_alert": "yellow",
                "economic_alert": "green"}

    orig = analyzer.fetch_shakemap_pager
    analyzer.fetch_shakemap_pager = _fake_shakemap
    try:
        res = asyncio.run(analyzer.analyze_earthquake(bbox, data))
    finally:
        analyzer.fetch_shakemap_pager = orig

    eb = res["evidence_basis"]
    if eb.get("verdict_basis") == "usgs_shakemap_mmi" and res["risk"] == "HIGH":
        ok(f"ShakeMap MMI 7.2 drove the verdict to HIGH (magnitude heuristic "
           f"would have said MEDIUM at M4.2)")
    else:
        bad(f"ShakeMap did not supersede: basis={eb.get('verdict_basis')} "
            f"risk={res['risk']}")
    if "Modified Mercalli" in res["diagnostics"]["threshold_applied"]:
        ok("threshold string cites the Modified Mercalli scale — a REAL named "
           "scale, unlike the magnitude cut points")
    else:
        bad(f"threshold string: {res['diagnostics']['threshold_applied']}")
    if eb.get("shakemap", {}).get("pager_alert") == "yellow":
        ok("PAGER loss alert carried into the evidence for the impact agent")
    else:
        bad(f"PAGER not carried: {eb.get('shakemap')}")


def test_no_shakemap_falls_back_to_heuristic():
    bbox = [73.0, 33.0, 73.2, 33.2]
    data = {"earthquakes": [_quake(6.2, 73.1, 33.1, "us_nosm")], "count": 1,
            "source": "usgs"}

    async def _none(event_id, timeout_s=15):
        return {"available": False, "reason": "no shakemap product"}

    orig = analyzer.fetch_shakemap_pager
    analyzer.fetch_shakemap_pager = _none
    try:
        res = asyncio.run(analyzer.analyze_earthquake(bbox, data))
    finally:
        analyzer.fetch_shakemap_pager = orig

    if res["evidence_basis"].get("verdict_basis") == "magnitude_heuristic":
        ok("no ShakeMap -> magnitude heuristic, basis labelled honestly")
    else:
        bad(f"basis wrong: {res['evidence_basis'].get('verdict_basis')}")
    if "engineering judgement" in res["diagnostics"]["threshold_applied"]:
        ok("fallback still states its thresholds are engineering judgement")
    else:
        bad(f"fallback string: {res['diagnostics']['threshold_applied']}")


if __name__ == "__main__":
    print("=" * 64)
    print("PHASES 4b/5b/5c — HAZARD SCIENCE (p90 slope, distance decay)")
    print("=" * 64)
    test_p90_beats_mean_on_local_valley()
    test_grid_density_raised()
    test_distance_decay_separates_near_and_far()
    test_driving_event_named_with_magtype()
    test_threshold_strings_are_honest()
    test_risk_polygons_points_at_real_source()
    test_shakemap_supersedes_magnitude_heuristic()
    test_no_shakemap_falls_back_to_heuristic()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
