"""Satellite validation harness — entry point.

Runs the REAL satellite pipeline (agents/satellite/agent.py, no mocking of
the science path) against each confirmed reference event under
reference_events/, scores the predicted flood extent against the EMS
delineation ground truth (both including and excluding permanent water),
and writes a versioned JSON report keyed by the current git commit.

Usage:
    cd tests/validation
    python run_baseline.py                  # run every confirmed event
    python run_baseline.py --event emsr773_valencia --path sentinel2
    python run_baseline.py --dry-run         # load configs + reference data only, no pipeline run

No index, threshold, or algorithm change happens here or is triggered by
running this script — it only invokes the existing pipeline and measures it.

Results are read back via the SAME code path GET /results/{event_id}/evidence
uses (backend/db.py's get_event_evidence), not the pipeline's in-memory JSON
return — see pipeline_runner.py's module docstring for why this matters.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _fix_proj_lib() -> None:
    """A system-wide PostgreSQL/PostGIS install can leave PROJ_LIB pointing at
    its own (older, incompatible) proj.db, which makes every rasterio/pyproj
    reprojection in the pipeline fail silently — `clip_to_polygon` logs
    "Failed to reproject clip polygon" and the tiered coverage search just
    keeps retrying, burning hours before reporting insufficient_coverage with
    no indication the real cause was an environment variable, not the data.

    This is now ALSO fixed at the source (agents/satellite/tests/conftest.py,
    landed 2026-07-28) for pytest runs, but this script is invoked directly
    with `python run_baseline.py`, not via pytest, so conftest.py never loads
    here — the same fix is kept in this entry point too, redundant with
    conftest.py but not in conflict with it (both compute the identical
    bundled-proj_data path and set the same two env vars before rasterio's
    first import in THIS process).
    """
    bundled = (
        Path(__file__).resolve().parents[2]
        / "agents" / "satellite" / "venv" / "Lib" / "site-packages"
        / "rasterio" / "proj_data"
    )
    if bundled.is_dir():
        os.environ["PROJ_LIB"] = str(bundled)
        os.environ["PROJ_DATA"] = str(bundled)


_fix_proj_lib()

from metrics import compute_with_permanent_water_split  # noqa: E402
from predicted_extent import fetch_predicted_extent  # noqa: E402
from pipeline_runner import REPO_ROOT, run_pipeline_for_event  # noqa: E402
from reference_loader import load_reference_geometry  # noqa: E402
from results_store import EventResult, print_summary_table, write_run_report  # noqa: E402

EVENTS_DIR = Path(__file__).resolve().parent / "reference_events"

# Search/download budgets applied to every harness run — the SAME
# caller-controlled knobs any production /analyze request can set
# (fix/coverage-tolerance, backend/models.py's AnalyzeRequest). Keeping the
# harness on the same defaults a production caller gets (rather than an
# unbounded research-only budget) means a validation run's cost is
# representative of a real run, and bounds the harness's own worst case.
BUDGET_MIN_COVERAGE_PERCENT = 90.0
BUDGET_MAX_SCENES = 3
BUDGET_MAX_DOWNLOAD_GB = 4.0
BUDGET_MAX_SEARCH_SECONDS = 900.0


def _load_event_configs() -> list[dict]:
    configs = []
    for f in sorted(EVENTS_DIR.glob("*.yaml")):
        with open(f, encoding="utf-8") as fh:
            configs.append(yaml.safe_load(fh))
    return configs


def _run_one_path(
    event_cfg: dict,
    path_key: str,
    product_cfg: dict,
    dry_run: bool,
    forced_satellite_type: str | None = None,
) -> EventResult:
    event_key = event_cfg["event_key"]
    label = f"{event_key}::{path_key}"
    notes: list[str] = list(event_cfg.get("caveats", []))
    if forced_satellite_type:
        notes.append(
            f"SATELLITE SELECTION FORCED to {forced_satellite_type} for this run "
            "(tests/validation/forced_satellite_override.py) — this bypasses "
            "select_satellite's real cloud-aware decision entirely; the DB row's "
            "selection_reason will read 'harness_forced_selection', never a real "
            "selection_reason value, so this cannot be mistaken for an "
            "organically-selected result."
        )

    # 1. Reference geometry (ground truth) — always loaded, even in a
    #    dry run, since this is what step 2/3 of the task asks the harness
    #    to validate first.
    try:
        ref_geom, ref_crs = load_reference_geometry(
            download_url=product_cfg["download_url"],
            vector_layer_of_record=product_cfg["vector_layer_of_record"],
            event_key=f"{event_key}_{path_key}",
        )

        # City-scale rescope: the EMS product itself is published at whatever
        # AOI the activation covers (often province-wide) — no city-level EMS
        # product exists to download instead. When the event config asks for
        # it, clip the reference geometry down to the SAME boundary the
        # pipeline itself resolves for `pipeline_location`, so both sides of
        # the comparison describe the same city, not "pipeline analysed a
        # city" vs "reference covers an entire province".
        if event_cfg.get("clip_reference_to_pipeline_boundary"):
            sys.path.insert(0, str(REPO_ROOT / "agents" / "satellite"))
            # get_region_boundary (used for the map's background region) does
            # NOT apply boundary.py's _ensure_areal zero-area-point buffer —
            # only get_risk_city_boundaries does. For a point-only city (like
            # Kanalia — no OSM/geoBoundaries admin polygon), get_region_boundary
            # returns a bare zero-area Point, which trivially clips the
            # reference to empty every time, regardless of how close the point
            # actually is to real reference polygons — a harness bug found live
            # (this event's first run reported an empty reference at a
            # correctly-resolved location because of exactly this). The real
            # pipeline never clips against a raw get_region_boundary point for
            # a single-city AOI; it goes through detect_risk_cities ->
            # get_risk_city_boundaries -> _ensure_areal's ~6km buffer disk.
            # Using the SAME function here means the harness scores against
            # the same geometry the pipeline itself actually analyses.
            from boundary import get_risk_city_boundaries  # local import, needs agents/satellite on sys.path
            from aoi_pin import pinned_aoi  # deterministic AOI replay (see aoi_pin.py)

            # boundary._resolve_city_geometry builds its Nominatim query as
            # f"{city}, {region_name}", so passing the FULL pipeline_location
            # as region_name duplicates the city token:
            #   "Keramidi" + "Keramidi, Trikala, Greece"
            #     -> "Keramidi, Keramidi, Trikala, Greece"   (MISS)
            # Kanalia only ever worked by luck — "Kanalia, Kanalia, Magnesia,
            # Greece" happens to resolve anyway. Strip the headline city from
            # the region so the query is the well-formed
            #   "Keramidi, Trikala, Greece"                  (HIT)
            # which is also exactly what the production pipeline builds, since
            # detect_risk_cities yields the city and the location carries the
            # region. Verified against live Nominatim 2026-07-29.
            _parts = [p.strip() for p in event_cfg["pipeline_location"].split(",")]
            headline_city = _parts[0]
            region_for_query = ", ".join(_parts[1:]) or event_cfg["pipeline_location"]
            with pinned_aoi():
                import boundary as _boundary_mod  # patched inside the context
                city_boundaries = _boundary_mod.get_risk_city_boundaries(
                    region_for_query, [headline_city]
                )
            if not city_boundaries:
                raise RuntimeError(
                    f"Could not resolve pipeline boundary for "
                    f"{event_cfg['pipeline_location']!r} to clip the reference "
                    f"geometry against."
                )
            import geopandas as gpd
            from shapely.geometry import shape

            city_geom = shape(city_boundaries[0]["geojson"])
            ref_gs = gpd.GeoSeries([ref_geom], crs=ref_crs).to_crs("EPSG:4326")
            clipped = ref_gs.iloc[0].intersection(city_geom)
            ref_empty_after_clip = clipped.is_empty
            if ref_empty_after_clip:
                notes.append(
                    "Reference geometry clipped to the pipeline's city "
                    "boundary is EMPTY — no EMS-mapped flood polygons fall "
                    "inside this specific city, even though nearby polygons "
                    "exist in the province-wide product."
                )
            ref_geom, ref_crs = clipped, "EPSG:4326"
    except Exception as exc:
        return EventResult(
            event_id=label,
            location=event_cfg["pipeline_location"],
            satellite_type=path_key,
            satellite_type_reason=None,
            coverage_tier=None,
            temporal_spread_days=None,
            reported_confidence=None,
            confidence_basis=None,
            metrics_including_permanent_water=None,
            metrics_excluding_permanent_water=None,
            permanent_water_note="",
            pipeline_status="reference_load_failed",
            pipeline_error=f"{type(exc).__name__}: {exc}",
            harness_notes=notes + ["Harness itself is the weak link here — reference geometry could not be loaded."],
        )

    # Symmetric to the "complete_zero_zones" guard below (predicted side
    # empty -> undefined 0/0 score, not a real 0): if the REFERENCE side is
    # empty after clipping to the pipeline's city boundary, IoU/precision/
    # recall are equally undefined (0/0), not a real 0 — scoring anyway
    # would silently report a city-selection miss as a pipeline accuracy
    # failure (this is exactly what happened on this harness's first live
    # Kanalia run: a mis-disambiguated OSM location clipped the reference to
    # empty, and the run still "scored" IoU=0.0/F1=0.0 as if the pipeline had
    # failed to detect a real, present flood). Fails fast here (before
    # spending a live CDSE budget) rather than after a multi-minute pipeline
    # run, since an empty reference can never become scoreable regardless of
    # what the pipeline finds.
    if event_cfg.get("clip_reference_to_pipeline_boundary") and ref_empty_after_clip:
        return EventResult(
            event_id=label,
            location=event_cfg["pipeline_location"],
            satellite_type=path_key,
            satellite_type_reason=None,
            coverage_tier=None,
            temporal_spread_days=None,
            reported_confidence=None,
            confidence_basis=None,
            metrics_including_permanent_water=None,
            metrics_excluding_permanent_water=None,
            permanent_water_note="",
            pipeline_status="reference_empty_after_clip",
            pipeline_error=None,
            harness_notes=notes + [
                "IoU/precision/recall/F1 against this reference are undefined (0/0), "
                "not a score of 0 — the pipeline was not even run for this path."
            ],
        )

    if dry_run:
        return EventResult(
            event_id=label,
            location=event_cfg["pipeline_location"],
            satellite_type=path_key,
            satellite_type_reason=None,
            coverage_tier=None,
            temporal_spread_days=None,
            reported_confidence=None,
            confidence_basis=None,
            metrics_including_permanent_water=None,
            metrics_excluding_permanent_water=None,
            permanent_water_note="dry-run: reference geometry loaded, pipeline not invoked",
            pipeline_status="dry_run",
            harness_notes=notes,
        )

    # 2. Run the real pipeline, clock pinned to this reference product's
    #    acquisition time, on the same budgets a production caller gets.
    as_of = datetime.fromisoformat(
        product_cfg["acquisition_datetime_utc"].replace("Z", "+00:00")
    )
    # The event's flood peak, when the config records one. Two uses:
    #   1. scenes before the peak are rejected (post_peak_scene_floor), so a
    #      backward-looking window cannot select pre-flood imagery;
    #   2. as_of is advanced past the peak so a post-peak same-orbit pass is
    #      inside the searchable window at all (S1 revisit here is ~12 days,
    #      and the reference acquisition is only ~1 day post-peak).
    peak_raw = event_cfg.get("event_peak_date")
    event_peak_utc = None
    if peak_raw:
        event_peak_utc = datetime.fromisoformat(
            str(peak_raw).replace("Z", "+00:00")
        )
        if event_peak_utc.tzinfo is None:
            event_peak_utc = event_peak_utc.replace(tzinfo=timezone.utc)
        if as_of < event_peak_utc + timedelta(days=14):
            as_of = event_peak_utc + timedelta(days=14)
    try:
        run = run_pipeline_for_event(
            location=event_cfg["pipeline_location"],
            disaster_type=event_cfg["disaster_type"],
            as_of=as_of,
            search_window_days=product_cfg.get("search_window_days", 5),
            min_coverage_percent=BUDGET_MIN_COVERAGE_PERCENT,
            max_scenes=BUDGET_MAX_SCENES,
            max_download_gb=BUDGET_MAX_DOWNLOAD_GB,
            max_search_seconds=BUDGET_MAX_SEARCH_SECONDS,
            forced_satellite_type=forced_satellite_type,
            event_peak_utc=event_peak_utc,
        )
    except Exception as exc:
        return EventResult(
            event_id=label,
            location=event_cfg["pipeline_location"],
            satellite_type=None,
            satellite_type_reason=None,
            coverage_tier=None,
            temporal_spread_days=None,
            reported_confidence=None,
            confidence_basis=None,
            metrics_including_permanent_water=None,
            metrics_excluding_permanent_water=None,
            permanent_water_note="",
            pipeline_status="pipeline_raised",
            pipeline_error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            harness_notes=notes,
            elapsed_seconds=None,
            bytes_downloaded=None,
        )

    event_id = run["event_id"]
    elapsed_seconds = run["elapsed_seconds"]
    bytes_downloaded = run.get("bytes_downloaded")
    raw_status = run["_raw_pipeline_status"]
    evidence = run.get("evidence") or {}
    # satellite_results row as returned by db.get_event_evidence — the exact
    # shape GET /results/{id}/evidence's `satellite` field carries. May be
    # None if the pipeline never reached a durable write (e.g. it failed
    # before _persist_satellite_result, or the DB write itself failed after
    # PERSIST_MAX_ATTEMPTS — both are real, reportable outcomes, not harness
    # bugs, so they're surfaced via pipeline_status below rather than masked).
    sat_row = evidence.get("satellite") if evidence else None
    # coverage_tier/temporal_spread_days are NOT their own satellite_results
    # columns (confirmed against shared/db/schema.sql) — they're written
    # into the row's `diagnostics` JSONB blob (see agent.py's
    # _persist_satellite_result). Recovered from there rather than reported
    # as unavailable, since the evidence endpoint DOES surface `diagnostics`
    # as part of the `satellite` dict.
    diag = (sat_row or {}).get("diagnostics") or {}

    if raw_status != "complete" or sat_row is None:
        return EventResult(
            event_id=event_id,
            location=event_cfg["pipeline_location"],
            satellite_type=(sat_row or {}).get("satellite_type"),
            satellite_type_reason=(sat_row or {}).get("selection_reason"),
            coverage_tier=diag.get("coverage_tier"),
            temporal_spread_days=diag.get("temporal_spread_days"),
            reported_confidence=(sat_row or {}).get("confidence"),
            confidence_basis=(sat_row or {}).get("confidence_basis"),
            metrics_including_permanent_water=None,
            metrics_excluding_permanent_water=None,
            permanent_water_note="",
            pipeline_status=(
                raw_status if raw_status != "complete" else "db_row_missing_after_complete"
            ),
            pipeline_error=run.get("_raw_pipeline_error"),
            harness_notes=notes + (
                [] if raw_status != "complete" else [
                    "Pipeline reported status=complete but GET /results/{id}/evidence's own "
                    "DB read found no satellite_results row for this event_id — a real "
                    "durability gap the harness must not paper over by falling back to the "
                    "in-memory result."
                ]
            ),
            elapsed_seconds=elapsed_seconds,
            bytes_downloaded=bytes_downloaded,
        )

    # 3. Predicted extent from the DB row's own geojson_url (the same URL a
    #    fresh GET /results/{id}/evidence caller would see) — not the
    #    in-memory pipeline result.
    predicted_geom = fetch_predicted_extent(sat_row.get("geojson_url"))
    if predicted_geom is None or predicted_geom.is_empty:
        return EventResult(
            event_id=event_id,
            location=event_cfg["pipeline_location"],
            satellite_type=sat_row.get("satellite_type"),
            satellite_type_reason=sat_row.get("selection_reason"),
            coverage_tier=diag.get("coverage_tier"),
            temporal_spread_days=diag.get("temporal_spread_days"),
            reported_confidence=sat_row.get("confidence"),
            confidence_basis=sat_row.get("confidence_basis"),
            metrics_including_permanent_water=None,
            metrics_excluding_permanent_water=None,
            permanent_water_note="",
            pipeline_status="complete_zero_zones",
            pipeline_error=None,
            harness_notes=notes + [
                "Pipeline completed but produced zero hazard zones — IoU/precision/recall "
                "against the EMS reference are undefined (0/0), not a score of 0."
            ],
            elapsed_seconds=elapsed_seconds,
            bytes_downloaded=bytes_downloaded,
        )

    # 4. Score. Phase 1c: the excluding-permanent-water split is now REAL —
    # the permanent-water geometry comes from the SAME source and threshold
    # the pipeline masks with (agents/satellite/permanent_water.py, JRC GSW
    # occurrence >= 75), so the split measures the pipeline's own definition
    # of "normally water", not a diverging harness-side one. Best-effort: an
    # unreachable JRC bucket degrades to the pre-1c no-split behaviour with
    # the note metrics.py already emits.
    pw_geom = None
    pw_source = None
    try:
        from permanent_water import (  # agents/satellite on sys.path
            JRC_SOURCE_LABEL,
            DEFAULT_OCCURRENCE_THRESHOLD,
            permanent_water_geojson,
        )
        from shapely.geometry import shape as _shp_shape

        bounds_geom = ref_geom.union(predicted_geom)
        west, south, east, north = bounds_geom.bounds
        pw_json = permanent_water_geojson(
            west - 0.005, south - 0.005, east + 0.005, north + 0.005
        )
        if pw_json is not None:
            pw_shape = _shp_shape(pw_json)
            if not pw_shape.is_empty:
                pw_geom = pw_shape
            pw_source = (
                f"{JRC_SOURCE_LABEL} (occurrence >= "
                f"{DEFAULT_OCCURRENCE_THRESHOLD}%)"
            )
    except Exception as exc:  # noqa: BLE001 — split is optional, scoring is not
        print(f"[permanent-water] split unavailable: {exc}")

    split = compute_with_permanent_water_split(
        predicted_geom=predicted_geom,
        predicted_crs="EPSG:4326",
        reference_geom=ref_geom,
        reference_crs=ref_crs,
        permanent_water_geom=pw_geom,
        permanent_water_source=pw_source,
    )

    return EventResult(
        event_id=event_id,
        location=event_cfg["pipeline_location"],
        satellite_type=sat_row.get("satellite_type"),
        satellite_type_reason=sat_row.get("selection_reason"),
        coverage_tier=diag.get("coverage_tier"),
        temporal_spread_days=diag.get("temporal_spread_days"),
        reported_confidence=sat_row.get("confidence"),
        confidence_basis=sat_row.get("confidence_basis"),
        metrics_including_permanent_water=split.including_permanent_water.as_dict(),
        metrics_excluding_permanent_water=(
            split.excluding_permanent_water.as_dict()
            if split.excluding_permanent_water
            else None
        ),
        permanent_water_note=split.note,
        pipeline_status=raw_status,
        harness_notes=notes,
        elapsed_seconds=elapsed_seconds,
        bytes_downloaded=bytes_downloaded,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", help="Only run this event_key")
    parser.add_argument("--path", help="Only run this reference_products path key (e.g. sentinel1)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force-satellite",
        choices=["sentinel-1", "sentinel-2"],
        help=(
            "Bypass select_satellite's real cloud-aware decision and force this "
            "satellite type for every run in this invocation (harness-only, see "
            "forced_satellite_override.py). Use this to validate a specific "
            "S1/S2 path when the reference date's real sky conditions would "
            "otherwise select the other satellite."
        ),
    )
    args = parser.parse_args()

    configs = _load_event_configs()
    results: list[EventResult] = []

    for cfg in configs:
        if args.event and cfg["event_key"] != args.event:
            continue

        if cfg.get("status") != "confirmed":
            results.append(
                EventResult(
                    event_id=cfg["event_key"],
                    location=cfg.get("pipeline_location", cfg.get("aoi_name", "?")),
                    satellite_type=None,
                    satellite_type_reason=None,
                    coverage_tier=None,
                    temporal_spread_days=None,
                    reported_confidence=None,
                    confidence_basis=None,
                    metrics_including_permanent_water=None,
                    metrics_excluding_permanent_water=None,
                    permanent_water_note="",
                    pipeline_status="excluded_unconfirmed_reference",
                    harness_notes=list(cfg.get("caveats", [])),
                )
            )
            continue

        for path_key, product_cfg in cfg.get("reference_products", {}).items():
            if path_key.endswith("_reference_only"):
                continue  # recorded for completeness, not run as a scored path
            if args.path and path_key != args.path:
                continue
            results.append(
                _run_one_path(
                    cfg, path_key, product_cfg, args.dry_run,
                    forced_satellite_type=args.force_satellite,
                )
            )

    print_summary_table(results)
    if not args.dry_run:
        out_path = write_run_report(results)
        print(f"\nWrote versioned results to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
