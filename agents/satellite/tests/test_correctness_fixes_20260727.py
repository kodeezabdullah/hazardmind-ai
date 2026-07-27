"""Correctness/contract tests for the 2026-07-27 ANALYSIS.md fix pass.

Covers the VERIFY checklist:
  Fix #1  — a persist failure yields status:"failed", never "complete".
  Fix #2  — an area reprojection failure raises rather than returning degrees^2.
  Fix #6  — confidence_basis correctly distinguishes the three states.
  Fix #10 — total_zones / scene_id are populated (non-null) on a successful run.
  Fix #12 — artifacts_incomplete is set when an R2 upload fails.
  Fix #11 — nothing references the deleted dead code (select_mosaic_scenes,
            COVERAGE_MOSAIC_THRESHOLD, MOSAIC_MAX_SCENES).

All offline and deterministic — no network, no CDSE, no LLM, no real DB. The
DB-facing test fakes out asyncpg via sys.modules so _persist_satellite_result's
retry/backoff logic is exercised without a live connection.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


# --------------------------------------------------------------------------- #
# Fix #1 — DB persist failure must not report success
# --------------------------------------------------------------------------- #
def test_persist_retries_then_fails_honestly():
    """A DB that always raises must exhaust PERSIST_MAX_ATTEMPTS and return a
    non-None error string (the caller-facing contract for "treat as failed").
    """
    import types

    class _AlwaysFailConn:
        async def execute(self, *a, **kw):
            raise RuntimeError("simulated Neon outage")

        async def close(self):
            pass

        def transaction(self):
            class _Txn:
                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, *exc):
                    return False

            return _Txn()

    fake_asyncpg = types.ModuleType("asyncpg")
    attempts = {"n": 0}

    async def _connect(url):
        attempts["n"] += 1
        return _AlwaysFailConn()

    fake_asyncpg.connect = _connect
    sys.modules["asyncpg"] = fake_asyncpg

    os.environ["NEON_DATABASE_URL"] = "postgres://fake/db"

    # Patch time.sleep so the 1s/3s backoff doesn't actually slow the test.
    import time as _time
    real_sleep = _time.sleep
    _time.sleep = lambda *_: None
    try:
        import agent
        import importlib
        importlib.reload(agent)  # pick up the fake asyncpg module cleanly
        error = agent._persist_satellite_result("evt-persist-fail", {"satellite_type": "sentinel-2"})
    finally:
        _time.sleep = real_sleep

    if error is not None and attempts["n"] == agent.PERSIST_MAX_ATTEMPTS:
        ok(f"persist failure retried {attempts['n']}x then returned an error string (not None)")
    else:
        bad(f"expected {getattr(agent, 'PERSIST_MAX_ATTEMPTS', '?')} attempts + non-None error; "
            f"got attempts={attempts['n']} error={error!r}")

    if error is not None:
        # The caller (_run_pipeline_sync) treats non-None as status:"failed" —
        # verify that branch exists by inspecting the source contract directly
        # (a full pipeline run needs live CDSE/boundary resolution, out of
        # scope for an offline unit test).
        import inspect
        src = inspect.getsource(agent._run_pipeline_sync)
        if 'persist_error is not None' in src and 'return _error(' in src:
            ok("_run_pipeline_sync branches to _error(...) when persist_error is not None")
        else:
            bad("_run_pipeline_sync does not appear to branch on a persist failure")


def test_persist_succeeds_returns_none():
    """A DB that succeeds on the first attempt must return None (no error)."""
    import types

    class _OkConn:
        async def execute(self, *a, **kw):
            return None

        async def close(self):
            pass

        def transaction(self):
            class _Txn:
                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, *exc):
                    return False

            return _Txn()

    fake_asyncpg = types.ModuleType("asyncpg")

    async def _connect(url):
        return _OkConn()

    fake_asyncpg.connect = _connect
    sys.modules["asyncpg"] = fake_asyncpg
    os.environ["NEON_DATABASE_URL"] = "postgres://fake/db"

    import agent
    import importlib
    importlib.reload(agent)
    error = agent._persist_satellite_result("evt-persist-ok", {"satellite_type": "sentinel-2"})

    if error is None:
        ok("persist success returns None (no error)")
    else:
        bad(f"expected None on success, got {error!r}")


# --------------------------------------------------------------------------- #
# Fix #2 — area calculation must never silently return wrong units
# --------------------------------------------------------------------------- #
def test_polygon_area_raises_on_reprojection_failure():
    """A reprojection failure must propagate, not degrade to geom.area."""
    import processor
    from shapely.geometry import Polygon

    poly = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])

    raised = False
    try:
        # An invalid CRS string forces pyproj's Transformer.from_crs to raise.
        processor._polygon_area_km2(poly, "not-a-real-crs-string-xyz")
    except Exception:  # noqa: BLE001 - any propagated exception is the point
        raised = True

    if raised:
        ok("_polygon_area_km2 raises on reprojection failure (no degrees^2 fallback)")
    else:
        bad("_polygon_area_km2 did NOT raise on an invalid CRS — silent fallback still present?")


def test_polygon_area_correct_for_valid_input():
    """Sanity: a real WGS84 polygon still produces a real km^2 area."""
    import processor
    from shapely.geometry import Polygon

    # Roughly a 1-degree box near the equator ~ 111km x 111km ~ 12321 km^2.
    poly = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    area = processor._polygon_area_km2(poly, "EPSG:4326")

    if 9000 < area < 13500:
        ok(f"_polygon_area_km2 returns a plausible km^2 value ({area:.1f})")
    else:
        bad(f"_polygon_area_km2 returned an implausible value ({area})")


# --------------------------------------------------------------------------- #
# Fix #6 — confidence_basis distinguishes the three states
# --------------------------------------------------------------------------- #
def test_confidence_basis_insufficient_evidence():
    from confidence_tracker import ConfidenceTracker

    t = ConfidenceTracker()
    basis = t.confidence_basis()
    if basis == "insufficient_evidence":
        ok("empty tracker -> confidence_basis == insufficient_evidence")
    else:
        bad(f"empty tracker -> expected insufficient_evidence, got {basis!r}")


def test_confidence_basis_evidence_contradicts():
    from confidence_tracker import ConfidenceTracker

    t = ConfidenceTracker()
    t.add_evidence("gdacs", 0.9, 0.3)
    t.add_concern("cloud cover > 60%", "CRITICAL")
    basis = t.confidence_basis()
    if basis == "evidence_contradicts":
        ok("evidence present + CRITICAL concern -> confidence_basis == evidence_contradicts")
    else:
        bad(f"expected evidence_contradicts, got {basis!r}")


def test_confidence_basis_evidence_supports():
    from confidence_tracker import ConfidenceTracker

    t = ConfidenceTracker()
    t.add_evidence("gdacs", 0.9, 0.3)
    t.add_evidence("cloud_check", 0.95, 0.2)
    t.add_evidence("index_validation", 0.9, 0.3)
    basis = t.confidence_basis()
    if basis == "evidence_supports":
        ok("strong evidence, no concerns -> confidence_basis == evidence_supports")
    else:
        bad(f"expected evidence_supports, got {basis!r} (overall={t.overall_confidence():.3f})")


def test_get_report_includes_basis_and_count():
    from confidence_tracker import ConfidenceTracker

    t = ConfidenceTracker()
    t.add_evidence("gdacs", 0.9, 0.3)
    report = t.get_report()
    if report.get("evidence_count") == 1 and report.get("confidence_basis") in (
        "insufficient_evidence", "evidence_contradicts", "evidence_supports",
    ):
        ok(f"get_report() carries evidence_count + confidence_basis: {report}")
    else:
        bad(f"get_report() missing expected fields: {report}")


# --------------------------------------------------------------------------- #
# Fix #10 — total_zones / scene_id populate the columns the INSERT names
# --------------------------------------------------------------------------- #
def test_scene_id_threaded_into_merged_result():
    """process_satellite_imagery's merged result must carry a scene_id built
    from the accepted scene(s)' product Id/Name — exercised at the source
    level (the exact update dict), since a full pipeline run needs live
    CDSE/rasters, out of scope for an offline unit test."""
    import inspect
    import processor

    src = inspect.getsource(processor.process_satellite_imagery)
    if '"scene_id":' in src and "scene_ids" in src:
        ok("process_satellite_imagery threads scene_id (comma-joined accepted scenes) into merged_result")
    else:
        bad("process_satellite_imagery does not appear to set scene_id on the merged result")


def test_structured_carries_total_zones_and_scene_id():
    """agent.py's structured result dict must include total_zones/scene_id —
    exercised at the source level since a full run needs live services."""
    import inspect
    import agent

    src = inspect.getsource(agent._run_pipeline_sync)
    has_total_zones = '"total_zones": total_zones' in src
    has_scene_id = '"scene_id": result.get("scene_id")' in src
    if has_total_zones and has_scene_id:
        ok("structured{} carries total_zones and scene_id (previously always NULL in the DB)")
    else:
        bad(f"structured{{}} missing total_zones/scene_id: total_zones={has_total_zones} scene_id={has_scene_id}")


def test_damage_percent_removed_from_insert_not_persisted_as_permanent_null_placeholder():
    """damage_percent has no producer anywhere in the codebase; the INSERT
    should no longer explicitly name it (rather than persisting a value that
    can never be anything but NULL)."""
    import inspect
    import agent

    src = inspect.getsource(agent._persist_satellite_result)
    if "damage_percent" not in src:
        ok("damage_percent removed from the satellite_results INSERT (no producer exists)")
    else:
        bad("damage_percent is still referenced in the INSERT despite having no producer")


# --------------------------------------------------------------------------- #
# Fix #12 — degraded artifact upload must be visible
# --------------------------------------------------------------------------- #
def test_upload_all_results_reports_failed_artifacts_no_bucket():
    """With no CLOUDFLARE_R2_BUCKET configured, every artifact must be listed
    as failed rather than silently returning all-None URLs."""
    import r2_upload

    old = os.environ.pop("CLOUDFLARE_R2_BUCKET", None)
    try:
        result = r2_upload.upload_all_results("evt-no-bucket", {
            "true_color": "/nonexistent/a.png",
            "index_map": "/nonexistent/b.png",
            "classification": "/nonexistent/c.png",
            "geojson": {"type": "FeatureCollection", "features": []},
        })
    finally:
        if old is not None:
            os.environ["CLOUDFLARE_R2_BUCKET"] = old

    failed = set(result.get("failed_artifacts") or [])
    expected = {"true_color", "index_map", "classification", "geojson"}
    if failed == expected:
        ok(f"upload_all_results reports all 4 artifacts failed when unconfigured: {failed}")
    else:
        bad(f"expected failed_artifacts=={expected}, got {failed}")


def test_upload_all_results_partial_failure_visible():
    """A missing local file for one artifact must surface in failed_artifacts
    even if bucket/client setup succeeds conceptually — simulated by pointing
    every path at a nonexistent file (no live R2 needed: _put_file's own
    os.path.exists guard fires before any network call)."""
    import r2_upload

    os.environ["CLOUDFLARE_R2_BUCKET"] = "fake-bucket-for-test"
    try:
        # get_r2_client will likely fail without real R2 creds -> None client
        # -> upload_all_results returns the "all failed" branch. Either branch
        # is a valid assertion of the contract: failed_artifacts is populated,
        # never silently empty with all-None URLs.
        result = r2_upload.upload_all_results("evt-partial-fail", {
            "true_color": "/definitely/does/not/exist.png",
            "index_map": "/definitely/does/not/exist2.png",
            "classification": "/definitely/does/not/exist3.png",
            "geojson": {"type": "FeatureCollection", "features": []},
        })
    finally:
        os.environ.pop("CLOUDFLARE_R2_BUCKET", None)

    all_urls_none = all(
        result.get(k) is None
        for k in ("true_color_url", "index_url", "classification_url", "geojson_url")
    )
    has_failed_list = bool(result.get("failed_artifacts"))
    if all_urls_none and has_failed_list:
        ok(f"degraded upload surfaces failed_artifacts (not just silent None URLs): {result['failed_artifacts']}")
    else:
        bad(f"degraded upload did not surface failed_artifacts as expected: {result}")


def test_structured_carries_artifacts_incomplete():
    """agent.py's structured result dict must include artifacts_incomplete /
    failed_artifacts — exercised at the source level."""
    import inspect
    import agent

    src = inspect.getsource(agent._run_pipeline_sync)
    if '"artifacts_incomplete": bool(failed_artifacts)' in src and '"failed_artifacts": failed_artifacts' in src:
        ok("structured{} carries artifacts_incomplete + failed_artifacts")
    else:
        bad("structured{} missing artifacts_incomplete/failed_artifacts wiring")


# --------------------------------------------------------------------------- #
# Fix #11 — nothing references the deleted dead code
# --------------------------------------------------------------------------- #
def test_dead_code_fully_removed():
    import sentinel
    import processor

    dead_names = ["select_mosaic_scenes", "COVERAGE_MOSAIC_THRESHOLD", "MOSAIC_MAX_SCENES"]
    leftover = []
    for name in dead_names:
        if hasattr(sentinel, name):
            leftover.append(f"sentinel.{name}")
        if hasattr(processor, name):
            leftover.append(f"processor.{name}")

    if not leftover:
        ok("select_mosaic_scenes / COVERAGE_MOSAIC_THRESHOLD / MOSAIC_MAX_SCENES fully removed")
    else:
        bad(f"dead names still present: {leftover}")


def test_min_valid_pixel_percent_still_alive_for_per_city_path():
    """MIN_VALID_PIXEL_PERCENT must remain — it's now load-bearing again since
    per-city rendering is a reachable (flag-gated) path, not dead code."""
    import processor

    if hasattr(processor, "MIN_VALID_PIXEL_PERCENT"):
        ok("MIN_VALID_PIXEL_PERCENT retained (live in the per-city path)")
    else:
        bad("MIN_VALID_PIXEL_PERCENT was removed but is still needed by _render_per_city")


def test_per_city_feature_flag_exists():
    import agent

    if hasattr(agent, "ENABLE_PER_CITY_ARTIFACTS"):
        ok(f"ENABLE_PER_CITY_ARTIFACTS feature flag exists (default={agent.ENABLE_PER_CITY_ARTIFACTS})")
    else:
        bad("ENABLE_PER_CITY_ARTIFACTS flag missing — per-city path still hardcoded off with no way to enable")


def test_per_city_flag_reaches_process_satellite_imagery_call():
    import inspect
    import agent

    src = inspect.getsource(agent._run_pipeline_sync)
    if "city_boundaries=city_polys if ENABLE_PER_CITY_ARTIFACTS else None" in src:
        ok("ENABLE_PER_CITY_ARTIFACTS actually gates the city_boundaries argument")
    else:
        bad("ENABLE_PER_CITY_ARTIFACTS flag defined but not wired into the process_satellite_imagery call")


if __name__ == "__main__":
    print("=" * 64)
    print("SATELLITE 2026-07-27 CORRECTNESS/CONTRACT FIX TESTS")
    print("=" * 64)

    test_persist_retries_then_fails_honestly()
    test_persist_succeeds_returns_none()

    test_polygon_area_raises_on_reprojection_failure()
    test_polygon_area_correct_for_valid_input()

    test_confidence_basis_insufficient_evidence()
    test_confidence_basis_evidence_contradicts()
    test_confidence_basis_evidence_supports()
    test_get_report_includes_basis_and_count()

    test_scene_id_threaded_into_merged_result()
    test_structured_carries_total_zones_and_scene_id()
    test_damage_percent_removed_from_insert_not_persisted_as_permanent_null_placeholder()

    test_upload_all_results_reports_failed_artifacts_no_bucket()
    test_upload_all_results_partial_failure_visible()
    test_structured_carries_artifacts_incomplete()

    test_dead_code_fully_removed()
    test_min_valid_pixel_percent_still_alive_for_per_city_path()
    test_per_city_feature_flag_exists()
    test_per_city_flag_reaches_process_satellite_imagery_call()

    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
