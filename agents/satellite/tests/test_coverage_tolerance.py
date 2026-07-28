"""Tests for the 2026-07-28 coverage-tolerance fix pass (fix/coverage-tolerance).

Covers the VERIFY checklist from the task:
  CHANGE 1 — coverage becomes a caller-controlled quality band (target/floor/
             ceiling), instead of an exact-100%-or-fail rule.
  CHANGE 2 — hard search budgets (scenes / bytes / seconds), independent of
             coverage, bound the whole tiered search.
  CHANGE 3 — a cloud-attributed gap stops being chased once no remaining
             candidate has materially lower AOI cloud cover.
  CHANGE 4 — a scene that adds less than MIN_MARGINAL_COVERAGE_GAIN stops the
             search (distinct from the near-zero doomed-streak check).
  CHANGE 5 — S1 and S2 get different tier windows.
  CHANGE 6 — selection falls back to the scene-level cloud figure when an
             AOI-restricted figure isn't available, and says so.
  Threading — min_coverage_percent/max_scenes/max_download_gb/
             max_search_seconds flow from AnalyzeRequest -> PipelineState ->
             ProcessDisasterInput -> process_satellite_imagery.

All offline and deterministic — no network, no CDSE, no LLM, no real DB.
`_attempt_clip`/`compute_coverage`/`_render_clip` are monkeypatched the same
way `tests/test_bug_fixes.py` already does, so no real raster I/O happens.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import processor
import sentinel

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


_DEFAULT_FOOTPRINT = {
    "type": "Polygon",
    "coordinates": [[
        [72.5, 33.0], [73.5, 33.0], [73.5, 34.0], [72.5, 34.0], [72.5, 33.0],
    ]],
}

_POLY = {
    "type": "Polygon",
    "coordinates": [[
        [73.0, 33.5], [73.1, 33.5], [73.1, 33.6], [73.0, 33.6], [73.0, 33.5],
    ]],
}


def _scene(name, date_iso, rel_orbit=None, orbit_dir=None, footprint=None,
           pid=None, cloud=None):
    attrs = []
    if rel_orbit is not None:
        attrs.append({"Name": "relativeOrbitNumber", "Value": rel_orbit})
    if orbit_dir is not None:
        attrs.append({"Name": "orbitDirection", "Value": orbit_dir})
    if cloud is not None:
        attrs.append({"Name": "cloudCover", "Value": cloud})
    return {
        "Id": pid or name,
        "Name": name,
        "ContentDate": {"Start": date_iso},
        "Attributes": attrs,
        "GeoFootprint": footprint if footprint is not None else _DEFAULT_FOOTPRINT,
    }


class _FakeTracker:
    def __init__(self):
        self.concerns = []
        self.evidence = []

    def add_concern(self, text, severity):
        self.concerns.append((severity, text))

    def add_evidence(self, *a, **k):
        self.evidence.append((a, k))


def _install_stubs(targets):
    saved = {}
    for name, fn in targets.items():
        saved[name] = getattr(processor, name)
        setattr(processor, name, fn)

    def restore():
        for name, orig in saved.items():
            setattr(processor, name, orig)
    return restore


def _fake_render_clip(clipped, satellite_type, disaster_type, out_id):
    """Stand in for the real PNG/GeoJSON render — returns a minimal result dict."""
    return {
        "satellite_type": satellite_type,
        "index_type": "NDWI" if satellite_type == "sentinel-2" else "SAR",
        "index_calibrated": satellite_type != "sentinel-1",
        "index_units": "NDWI_ratio" if satellite_type == "sentinel-2" else "dB_uncalibrated",
        "water_percent": 0.0,
        "mean_index": 0.0,
        "affected_area_km2": 0.0,
        "png_paths": {},
        "geojson": {"type": "FeatureCollection", "features": []},
        "bounds": None,
    }


def _sequenced_coverage(values):
    """compute_coverage stub returning successive interior_coverage_percent
    values from `values` on each call (repeats the last once exhausted)."""
    state = {"i": 0}

    def fake(clipped):
        i = min(state["i"], len(values) - 1)
        state["i"] += 1
        pct = values[i]
        return {
            "interior_coverage_percent": pct,
            "full_aoi_coverage_percent": max(0.0, pct - 1.0),
            "covered": pct >= 100.0,
            "gaps": [] if pct >= 100.0 else [{
                "pixels": 10, "area_km2": round((100.0 - pct) * 0.05, 4),
                "bbox": {"west": 73.0, "south": 33.5, "east": 73.1, "north": 33.6},
            }],
            "gap_cause": {"nodata": 0 if pct >= 100.0 else 10, "cloud": 0},
        }
    return fake


# --------------------------------------------------------------------------- #
# CHANGE 1 — coverage bands
# --------------------------------------------------------------------------- #
def test_97_percent_completes_with_penalty():
    print("\n[CHANGE 1] 97% coverage -> complete, reduced confidence, gap geometry")
    s1 = _scene("S2_a.SAFE", "2026-07-10T05:46:41Z", 91, pid="a")
    restore = _install_stubs({
        "_attempt_clip": lambda *a, **k: {"_stacked": {}, "shape": (10, 10)},
        "compute_coverage": _sequenced_coverage([97.0]),
        "_render_clip": _fake_render_clip,
    })
    trk = _FakeTracker()
    try:
        res = processor.process_satellite_imagery(
            {"satellite_type": "sentinel-2"}, [s1], (73, 33.5, 73.1, 33.6),
            _POLY, "evt-97", "tok", "flood", tracker=trk,
        )
    finally:
        restore()

    if res and res.get("status") != "failed" and res.get("coverage_status") == "target_met":
        ok(f"97% -> coverage_status=target_met (coverage_percent={res.get('coverage_percent')})")
    else:
        bad(f"97% did not complete as target_met: {res}")
    if res and res.get("gaps"):
        ok("97% result carries gap geometry")
    else:
        bad(f"97% result missing gap geometry: {res}")
    if trk.evidence:
        ok("confidence tracker received a coverage_shortfall evidence entry")
    else:
        bad("no confidence penalty recorded for 97% coverage")


def test_85_percent_below_target_flagged():
    print("\n[CHANGE 1] 85% coverage -> complete but below_target_coverage, "
          "larger penalty + anomaly")
    s1 = _scene("S2_a.SAFE", "2026-07-10T05:46:41Z", 91, pid="a")
    restore = _install_stubs({
        "_attempt_clip": lambda *a, **k: {"_stacked": {}, "shape": (10, 10)},
        "compute_coverage": _sequenced_coverage([85.0]),
        "_render_clip": _fake_render_clip,
    })
    trk = _FakeTracker()
    try:
        res = processor.process_satellite_imagery(
            {"satellite_type": "sentinel-2"}, [s1], (73, 33.5, 73.1, 33.6),
            _POLY, "evt-85", "tok", "flood", tracker=trk,
        )
    finally:
        restore()

    if res and res.get("coverage_status") == "below_target_coverage":
        ok(f"85% -> coverage_status=below_target_coverage")
    else:
        bad(f"85% did not flag below_target_coverage: {res}")
    anomaly_types = [a.get("type") for a in (res.get("coverage_anomalies") or [])] if res else []
    if "below_target_coverage" in anomaly_types:
        ok("below_target_coverage anomaly appended")
    else:
        bad(f"missing below_target_coverage anomaly: {anomaly_types}")
    if any(sev == "HIGH" for sev, _t in trk.concerns):
        ok("HIGH-severity concern recorded for below-target coverage")
    else:
        bad(f"no HIGH concern recorded: {trk.concerns}")


def test_78_percent_insufficient_coverage():
    print("\n[CHANGE 1] 78% coverage (< COVERAGE_FLOOR=80) -> insufficient_coverage")
    s1 = _scene("S2_a.SAFE", "2026-07-10T05:46:41Z", 91, pid="a")
    restore = _install_stubs({
        "_attempt_clip": lambda *a, **k: {"_stacked": {}, "shape": (10, 10)},
        "compute_coverage": _sequenced_coverage([78.0]),
        "_render_clip": _fake_render_clip,
    })
    try:
        res = processor.process_satellite_imagery(
            {"satellite_type": "sentinel-2"}, [s1], (73, 33.5, 73.1, 33.6),
            _POLY, "evt-78", "tok", "flood", tracker=_FakeTracker(),
        )
    finally:
        restore()

    if res and res.get("status") == "failed" and res.get("reason") == "insufficient_coverage":
        ok(f"78% -> status=failed/insufficient_coverage")
    else:
        bad(f"78% did not hard-fail: {res}")
    if res and res.get("gaps"):
        ok("insufficient_coverage failure carries gap geometry")
    else:
        bad(f"insufficient_coverage missing gap geometry: {res}")


def test_min_coverage_percent_clamped_low():
    print("\n[CHANGE 1] caller min_coverage_percent=60 clamped to COVERAGE_FLOOR=80")
    clamped = processor._clamp_min_coverage_percent(60)
    if clamped == processor.COVERAGE_FLOOR:
        ok(f"60 clamped to {clamped} (COVERAGE_FLOOR)")
    else:
        bad(f"60 clamped to {clamped}, expected {processor.COVERAGE_FLOOR}")


def test_min_coverage_percent_clamped_high():
    print("\n[CHANGE 1] caller min_coverage_percent=120 clamped to COVERAGE_CEILING=100")
    clamped = processor._clamp_min_coverage_percent(120)
    if clamped == processor.COVERAGE_CEILING:
        ok(f"120 clamped to {clamped} (COVERAGE_CEILING)")
    else:
        bad(f"120 clamped to {clamped}, expected {processor.COVERAGE_CEILING}")


def test_coverage_below_100_lowers_confidence_and_anomaly():
    print("\n[CHANGE 1] any shortfall from 100% lowers confidence + appends anomaly")
    s1 = _scene("S2_a.SAFE", "2026-07-10T05:46:41Z", 91, pid="a")
    restore = _install_stubs({
        "_attempt_clip": lambda *a, **k: {"_stacked": {}, "shape": (10, 10)},
        "compute_coverage": _sequenced_coverage([99.5]),
        "_render_clip": _fake_render_clip,
    })
    trk = _FakeTracker()
    try:
        res = processor.process_satellite_imagery(
            {"satellite_type": "sentinel-2"}, [s1], (73, 33.5, 73.1, 33.6),
            _POLY, "evt-99_5", "tok", "flood", tracker=trk,
        )
    finally:
        restore()
    if trk.evidence and res and res.get("coverage_percent") == 99.5:
        ok("99.5% coverage still records a (small) confidence penalty")
    else:
        bad(f"expected a small penalty at 99.5%: evidence={trk.evidence} res={res}")


# --------------------------------------------------------------------------- #
# CHANGE 2 — search budgets
# --------------------------------------------------------------------------- #
def test_scene_budget_exhaustion_halts_search():
    print("\n[CHANGE 2] max_scenes exhaustion halts the search, returns best-effort")
    scenes = [
        _scene(f"S2_{i}.SAFE", "2026-07-10T05:46:41Z", 91, pid=f"p{i}")
        for i in range(5)
    ]
    # Coverage climbs slowly so the search would keep going past max_scenes
    # if the budget weren't enforced; land above COVERAGE_FLOOR so we can
    # tell a real best-effort "complete" apart from a hard failure.
    restore = _install_stubs({
        "_attempt_clip": lambda *a, **k: {"_stacked": {}, "shape": (10, 10)},
        "compute_coverage": _sequenced_coverage([50.0, 60.0, 82.0, 84.0, 86.0]),
        "_render_clip": _fake_render_clip,
    })
    try:
        res = processor.process_satellite_imagery(
            {"satellite_type": "sentinel-2"}, scenes, (73, 33.5, 73.1, 33.6),
            _POLY, "evt-budget-scenes", "tok", "flood", tracker=_FakeTracker(),
            min_coverage_percent=95.0, max_scenes=2, max_download_gb=100.0,
            max_search_seconds=100000.0,
        )
    finally:
        restore()
    if res and res.get("budget_exhausted") == "max_scenes":
        ok(f"max_scenes budget correctly halted the search (coverage={res.get('coverage_percent')})")
    else:
        bad(f"max_scenes budget did not halt the search: {res}")


def test_byte_budget_exhaustion_halts_search():
    print("\n[CHANGE 2] max_download_gb exhaustion halts the search")
    scenes = [
        _scene(f"S2_{i}.SAFE", "2026-07-10T05:46:41Z", 91, pid=f"p{i}")
        for i in range(5)
    ]

    def fake_clip(*a, **k):
        # Each accepted "download" adds 2 GB to the running total.
        processor._add_bytes_downloaded(2 * 1024 ** 3)
        return {"_stacked": {}, "shape": (10, 10)}

    restore = _install_stubs({
        "_attempt_clip": fake_clip,
        "compute_coverage": _sequenced_coverage([50.0, 82.0, 84.0, 86.0, 88.0]),
        "_render_clip": _fake_render_clip,
    })
    try:
        res = processor.process_satellite_imagery(
            {"satellite_type": "sentinel-2"}, scenes, (73, 33.5, 73.1, 33.6),
            _POLY, "evt-budget-bytes", "tok", "flood", tracker=_FakeTracker(),
            min_coverage_percent=95.0, max_scenes=100, max_download_gb=3.0,
            max_search_seconds=100000.0,
        )
    finally:
        restore()
    if res and res.get("budget_exhausted") == "max_download_gb":
        ok(f"max_download_gb budget correctly halted the search (coverage={res.get('coverage_percent')})")
    else:
        bad(f"max_download_gb budget did not halt the search: {res}")


def test_time_budget_exhaustion_halts_search():
    print("\n[CHANGE 2] max_search_seconds exhaustion halts the search")
    scenes = [
        _scene(f"S2_{i}.SAFE", "2026-07-10T05:46:41Z", 91, pid=f"p{i}")
        for i in range(5)
    ]

    def fake_clip(*a, **k):
        import time as _time
        _time.sleep(0.05)
        return {"_stacked": {}, "shape": (10, 10)}

    restore = _install_stubs({
        "_attempt_clip": fake_clip,
        "compute_coverage": _sequenced_coverage([50.0, 82.0, 84.0, 86.0, 88.0]),
        "_render_clip": _fake_render_clip,
    })
    try:
        res = processor.process_satellite_imagery(
            {"satellite_type": "sentinel-2"}, scenes, (73, 33.5, 73.1, 33.6),
            _POLY, "evt-budget-time", "tok", "flood", tracker=_FakeTracker(),
            min_coverage_percent=95.0, max_scenes=100, max_download_gb=100.0,
            max_search_seconds=0.03,
        )
    finally:
        restore()
    if res and res.get("budget_exhausted") == "max_search_seconds":
        ok(f"max_search_seconds budget correctly halted the search (coverage={res.get('coverage_percent')})")
    else:
        bad(f"max_search_seconds budget did not halt the search: {res}")


# --------------------------------------------------------------------------- #
# CHANGE 4 — marginal-return stopping
# --------------------------------------------------------------------------- #
def test_marginal_gain_stops_search():
    print("\n[CHANGE 4] a scene adding < 2 points of coverage stops the search")
    scenes = [
        _scene(f"S2_{i}.SAFE", "2026-07-10T05:46:41Z", 91, pid=f"p{i}")
        for i in range(4)
    ]
    # First acquisition establishes a baseline (50%); second jumps to 85%
    # (a real gain, above the marginal threshold); third only adds +1.0
    # (below MIN_MARGINAL_COVERAGE_GAIN=2.0) -> should stop right there.
    restore = _install_stubs({
        "_attempt_clip": lambda *a, **k: {"_stacked": {}, "shape": (10, 10)},
        "compute_coverage": _sequenced_coverage([50.0, 85.0, 86.0, 95.0]),
        "_render_clip": _fake_render_clip,
    })
    try:
        res = processor.process_satellite_imagery(
            {"satellite_type": "sentinel-2"}, scenes, (73, 33.5, 73.1, 33.6),
            _POLY, "evt-marginal", "tok", "flood", tracker=_FakeTracker(),
            min_coverage_percent=99.0,
        )
    finally:
        restore()
    anomaly_types = [a.get("type") for a in (res.get("coverage_anomalies") or [])] if res else []
    if res and "marginal_return_stop" in anomaly_types and res.get("coverage_percent") == 86.0:
        ok(f"search stopped at 86% after a +1.0pt marginal gain "
           f"(anomalies={anomaly_types})")
    else:
        bad(f"marginal-return stop did not fire as expected: {res}")


# --------------------------------------------------------------------------- #
# CHANGE 5 — per-satellite tier windows
# --------------------------------------------------------------------------- #
def test_s1_and_s2_get_different_tier_windows():
    print("\n[CHANGE 5] S1 and S2 get different tier windows")
    s2_windows = {t: d for t, d, _o in sentinel.COVERAGE_TIERS_S2}
    s1_windows = {t: d for t, d, _o in sentinel.COVERAGE_TIERS_S1}
    if s2_windows != s1_windows:
        ok(f"S2 tiers {s2_windows} differ from S1 tiers {s1_windows}")
    else:
        bad(f"S1 and S2 tier windows are identical: {s2_windows}")
    if sentinel.coverage_tiers_for("sentinel-1") == sentinel.COVERAGE_TIERS_S1:
        ok("coverage_tiers_for('sentinel-1') returns COVERAGE_TIERS_S1")
    else:
        bad("coverage_tiers_for('sentinel-1') routing wrong")
    if sentinel.coverage_tiers_for("sentinel-2") == sentinel.COVERAGE_TIERS_S2:
        ok("coverage_tiers_for('sentinel-2') returns COVERAGE_TIERS_S2")
    else:
        bad("coverage_tiers_for('sentinel-2') routing wrong")
    # S1's ±3/±7 intermediate steps collapse into a single ±10 window.
    if 10 in s1_windows.values() and 3 not in s1_windows.values() and 7 not in s1_windows.values():
        ok("S1 collapses the ±3/±7 steps into a single ±10-day window")
    else:
        bad(f"S1 windows did not collapse as expected: {s1_windows}")


def test_s2_tiers_unchanged():
    print("\n[CHANGE 5] S2 tiers are unchanged (0/3/7/14)")
    days = sorted(d for _t, d, _o in sentinel.COVERAGE_TIERS_S2)
    if days == [0, 3, 7, 14]:
        ok(f"S2 tier windows unchanged: {days}")
    else:
        bad(f"S2 tier windows changed unexpectedly: {days}")


# --------------------------------------------------------------------------- #
# CHANGE 6 — AOI-restricted cloud selection (documented as partial — see
# CLAUDE.md / sentinel.py's select_satellite docstring). These tests cover
# the fallback behaviour that WAS implemented: scene-level cloud drives
# selection when no AOI-restricted figure is available, and selection_reason
# says so.
# --------------------------------------------------------------------------- #
def test_selection_falls_back_to_scene_level_cloud():
    print("\n[CHANGE 6] selection falls back to scene-level cloud; "
          "selection_reason says so")
    result = sentinel.select_satellite("flood", cloud_cover=45.9)
    if result["satellite_type"] == "sentinel-1":
        ok(f"45.9% cloud (scene-level, no AOI figure) -> sentinel-1")
    else:
        bad(f"expected sentinel-1 at 45.9% cloud: {result}")
    if result.get("scene_cloud_percent") == 45.9 and result.get("aoi_cloud_percent") is None:
        ok("scene_cloud_percent=45.9, aoi_cloud_percent=None (not computed)")
    else:
        bad(f"cloud fields wrong: {result}")
    if "scl_unavailable" in result.get("selection_reason", ""):
        ok(f"selection_reason names the SCL-unavailable fallback: "
           f"{result['selection_reason']}")
    else:
        bad(f"selection_reason does not name the fallback: {result.get('selection_reason')}")


def test_selection_low_cloud_selects_sentinel2():
    print("\n[CHANGE 6 fallback] low scene-level cloud -> sentinel-2")
    result = sentinel.select_satellite("earthquake", cloud_cover=12.0)
    if result["satellite_type"] == "sentinel-2":
        ok(f"12% cloud -> sentinel-2")
    else:
        bad(f"expected sentinel-2 at 12% cloud: {result}")


def test_selection_reason_present_on_hint_fallback():
    print("\n[CHANGE 6] selection_reason present even on the no-cloud-data hint path")
    result = sentinel.select_satellite("earthquake")
    if result.get("selection_reason"):
        ok(f"selection_reason present with no cloud data: {result['selection_reason']}")
    else:
        bad(f"selection_reason missing: {result}")


# --------------------------------------------------------------------------- #
# CHANGE 3 — cloud-attributed gap stops being chased
# --------------------------------------------------------------------------- #
def test_cloud_gap_not_chased_without_lower_cloud_candidate():
    print("\n[CHANGE 3] a cloud-attributed gap does not trigger further "
          "downloads when no lower-cloud candidate exists")
    s1 = _scene("S2_a.SAFE", "2026-07-10T05:46:41Z", 91, pid="a", cloud=40.0)
    s2 = _scene("S2_b.SAFE", "2026-07-10T05:46:41Z", 91, pid="b", cloud=38.0)
    # After the first scene, coverage plateaus with a CLOUD-attributed gap
    # (cloud pixels > nodata pixels). The second candidate has no materially
    # lower cloud cover (only 2pts lower, under the 5pt "materially lower"
    # bar), so it should be skipped rather than downloaded.
    attempts = {"n": 0}

    def fake_clip(*a, **k):
        attempts["n"] += 1
        return {"_stacked": {}, "shape": (10, 10)}

    def fake_cov(clipped):
        return {
            "interior_coverage_percent": 88.0,
            "full_aoi_coverage_percent": 87.0,
            "covered": False,
            "gaps": [{"pixels": 10, "area_km2": 0.1,
                      "bbox": {"west": 73.0, "south": 33.5, "east": 73.1, "north": 33.6}}],
            "gap_cause": {"nodata": 2, "cloud": 20},  # cloud-dominated gap
        }

    restore = _install_stubs({
        "_attempt_clip": fake_clip,
        "compute_coverage": fake_cov,
        "_render_clip": _fake_render_clip,
    })
    try:
        res = processor.process_satellite_imagery(
            {"satellite_type": "sentinel-2"}, [s1, s2], (73, 33.5, 73.1, 33.6),
            _POLY, "evt-cloud-gap", "tok", "flood", tracker=_FakeTracker(),
            min_coverage_percent=95.0,
        )
    finally:
        restore()
    if attempts["n"] == 1:
        ok("only the first scene was attempted; second (no materially lower "
           "cloud) was skipped")
    else:
        bad(f"expected exactly 1 attempt, got {attempts['n']}")
    if res and res.get("gap_limited_by") == "cloud":
        ok(f"result marks gap_limited_by=cloud")
    else:
        bad(f"gap_limited_by not set to cloud: {res}")


# --------------------------------------------------------------------------- #
# Threading integration (no live network — just checks the params reach the
# innermost call with the right values/defaults, using stubs).
# --------------------------------------------------------------------------- #
def test_budget_params_thread_from_analyze_request():
    print("\n[Threading] min_coverage_percent/max_scenes/... reach "
          "process_satellite_imagery from AnalyzeRequest-shaped input")
    _repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )))
    sys.path.insert(0, _repo_root)
    sys.path.insert(0, os.path.join(_repo_root, "backend"))
    try:
        from models import AnalyzeRequest
    except Exception as exc:  # pragma: no cover - environment-dependent import
        bad(f"could not import backend.models.AnalyzeRequest: {exc}")
        return

    req = AnalyzeRequest(location="Rawalpindi", disaster_type="flood",
                          min_coverage_percent=93.0, max_scenes=5,
                          max_download_gb=6.0, max_search_seconds=1200.0)

    # Mirrors backend/router.py's disaster_data dict construction.
    disaster_data = {
        "location": req.location,
        "disaster_type": req.disaster_type,
        "magnitude": req.magnitude,
        "min_coverage_percent": req.min_coverage_percent,
        "max_scenes": req.max_scenes,
        "max_download_gb": req.max_download_gb,
        "max_search_seconds": req.max_search_seconds,
    }

    from shared.pipeline_state import PipelineState  # noqa: F401 (import-shape check)

    # Mirrors backend/orchestrator.py's initial_state construction.
    state = {
        "event_id": "evt-thread",
        "location": disaster_data["location"],
        "disaster_type": disaster_data["disaster_type"],
        "min_coverage_percent": disaster_data["min_coverage_percent"],
        "max_scenes": disaster_data["max_scenes"],
        "max_download_gb": disaster_data["max_download_gb"],
        "max_search_seconds": disaster_data["max_search_seconds"],
    }

    from agent import ProcessDisasterInput, _coverage_budget_kwargs

    params = ProcessDisasterInput(
        event_id=state["event_id"],
        location=state["location"],
        disaster_type=state["disaster_type"],
        min_coverage_percent=state.get("min_coverage_percent"),
        max_scenes=state.get("max_scenes"),
        max_download_gb=state.get("max_download_gb"),
        max_search_seconds=state.get("max_search_seconds"),
    )
    kwargs = _coverage_budget_kwargs(params)
    if (kwargs.get("min_coverage_percent") == 93.0 and kwargs.get("max_scenes") == 5
            and kwargs.get("max_download_gb") == 6.0
            and kwargs.get("max_search_seconds") == 1200.0):
        ok(f"AnalyzeRequest -> PipelineState -> ProcessDisasterInput -> "
           f"process_satellite_imagery kwargs: {kwargs}")
    else:
        bad(f"threading lost a value along the way: {kwargs}")


def run_all():
    test_97_percent_completes_with_penalty()
    test_85_percent_below_target_flagged()
    test_78_percent_insufficient_coverage()
    test_min_coverage_percent_clamped_low()
    test_min_coverage_percent_clamped_high()
    test_coverage_below_100_lowers_confidence_and_anomaly()
    test_scene_budget_exhaustion_halts_search()
    test_byte_budget_exhaustion_halts_search()
    test_time_budget_exhaustion_halts_search()
    test_marginal_gain_stops_search()
    test_s1_and_s2_get_different_tier_windows()
    test_s2_tiers_unchanged()
    test_selection_falls_back_to_scene_level_cloud()
    test_selection_low_cloud_selects_sentinel2()
    test_selection_reason_present_on_hint_fallback()
    test_cloud_gap_not_chased_without_lower_cloud_candidate()
    test_budget_params_thread_from_analyze_request()


if __name__ == "__main__":
    print("=" * 64)
    print("COVERAGE-TOLERANCE FIX-PASS TESTS")
    print("=" * 64)
    run_all()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
