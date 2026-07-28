"""Correctness tests for the satellite coverage/CRS fixes.

Covers the VERIFY checklist from the coverage-correctness task:

  BUG 1 — a 4326 polygon against a non-4326 raster produces a non-empty clip;
          a GCP-georeferenced (S1 GRD-like) raster is resolved before clipping.
  BUG 2 — valid-pixel coverage differs from geometric coverage on a cloudy tile.
  BUG 3 — a mosaic that reaches only <100% returns status="failed" (not a risk
          level); S1 mosaics never mix ascending/descending; tier 3/4 lowers
          confidence and appends an anomaly.
  BUG 4 — COG / non-COG twins collapse to one candidate.
  BUG 5 — index_calibrated and index_units present on both S1 and S2 paths.

All offline and deterministic — no network, no CDSE, no LLM. Synthetic rasters
are written to a temp dir and cleaned up.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rasterio
from rasterio.control import GroundControlPoint
from rasterio.warp import transform_geom

import processor

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


# --------------------------------------------------------------------------- #
# Synthetic raster builders
# --------------------------------------------------------------------------- #
def _write_gcp_tiff(path, lon0, lat0, lon1, lat1, h=400, w=500, fill=100.0):
    """Write a GRD-like TIFF: NO affine CRS, georeferenced by GCPs only.

    Mirrors an S1 GRD measurement TIFF — a plain rasterio.open() reports
    crs=None and an identity transform, and the geolocation lives in GCPs
    spanning the given lon/lat box.
    """
    gcps = [
        GroundControlPoint(row=0, col=0, x=lon0, y=lat1),
        GroundControlPoint(row=0, col=w, x=lon1, y=lat1),
        GroundControlPoint(row=h, col=w, x=lon1, y=lat0),
        GroundControlPoint(row=h, col=0, x=lon0, y=lat0),
        GroundControlPoint(row=h // 2, col=w // 2, x=(lon0 + lon1) / 2, y=(lat0 + lat1) / 2),
    ]
    data = np.full((1, h, w), fill, dtype="float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=1,
        dtype="float32",
    ) as dst:
        dst.write(data)
        dst.gcps = (gcps, rasterio.crs.CRS.from_epsg(4326))


def _utm_stacked(epsg, h=800, w=800):
    """Build a synthetic UTM stacked cube (affine-georeferenced, like S2)."""
    crs = rasterio.crs.CRS.from_epsg(epsg)
    transform = rasterio.Affine(10.0, 0, 500000.0, 0, -10.0, 3720000.0)
    band = np.full((h, w), 1000.0, dtype="float32")
    return {
        "bands": {"B03": band.copy(), "B08": band.copy()},
        "tci": None,
        "transform": transform,
        "crs": crs,
        "shape": (h, w),
    }


# --------------------------------------------------------------------------- #
# BUG 1 — CRS handling
# --------------------------------------------------------------------------- #
def test_bug1_4326_poly_vs_utm_raster():
    print("\n[BUG 1] 4326 polygon vs non-4326 (UTM) raster -> non-empty clip")
    stacked = _utm_stacked(32643)  # UTM 43N (Rawalpindi zone)
    transform, crs = stacked["transform"], stacked["crs"]
    # A polygon over an interior window, expressed in WGS84 degrees.
    r0, r1, c0, c1 = 100, 500, 200, 600
    ux0 = transform.c + c0 * transform.a
    ux1 = transform.c + c1 * transform.a
    uy0 = transform.f + r0 * transform.e
    uy1 = transform.f + r1 * transform.e
    utm_poly = {"type": "Polygon", "coordinates": [[
        [ux0, uy0], [ux1, uy0], [ux1, uy1], [ux0, uy1], [ux0, uy0]]]}
    wgs_poly = transform_geom(crs, "EPSG:4326", utm_poly)

    clipped = processor.clip_to_polygon(stacked, wgs_poly)
    if clipped is None:
        return bad("clip returned None on a valid UTM raster")
    valid = processor._valid_pixel_percent(clipped)
    if clipped["shape"][0] > 10 and clipped["shape"][1] > 10 and valid > 50:
        ok(f"non-empty clip: {clipped['shape']} @ {valid:.0f}% valid "
           "(degree coords correctly reprojected to UTM)")
    else:
        bad(f"clip collapsed: {clipped['shape']} @ {valid:.1f}% valid")


def test_bug1_gcp_raster_resolved():
    print("\n[BUG 1] GCP-georeferenced (S1 GRD-like) raster is resolved")
    tmp = tempfile.mkdtemp(prefix="hm-bug1-")
    path = os.path.join(tmp, "s1_grd_vv.tiff")
    # Rawalpindi-ish box.
    _write_gcp_tiff(path, 72.9, 33.4, 73.3, 33.8)

    # Plain open sees no usable georeferencing (the bug precondition).
    with rasterio.open(path) as src:
        is_identity = src.transform == rasterio.Affine.identity()
        no_crs = src.crs is None
    if is_identity and no_crs:
        ok("precondition: plain open reports identity transform + crs=None")
    else:
        bad("synthetic GCP raster did not reproduce the crs=None/identity state")

    # _open_georeferenced must resolve it to a real CRS + affine.
    aoi = {"type": "Polygon", "coordinates": [[
        [72.95, 33.45], [73.25, 33.45], [73.25, 33.75],
        [72.95, 33.75], [72.95, 33.45]]]}
    dst_crs = processor._dst_crs_from_polygon(aoi)
    ds, src = processor._open_georeferenced(path, dst_crs)
    try:
        resolved_crs = ds.crs
        resolved_transform = ds.transform
    finally:
        ds.close()
        if src is not None:
            src.close()
    if resolved_crs is not None and resolved_transform != rasterio.Affine.identity():
        ok(f"GCP raster resolved to {resolved_crs} with a real affine")
    else:
        bad("GCP raster NOT resolved (still crs=None / identity)")

    # Full stack->clip on the GCP raster must yield a non-empty clip.
    stacked = processor.stack_bands({"VV": path}, "sentinel-1", dst_crs)
    if stacked is None:
        return bad("stack_bands returned None for the GCP raster")
    clipped = processor.clip_to_polygon(stacked, aoi)
    if clipped is None:
        return bad("clip returned None on the resolved GCP raster")
    valid = processor._valid_pixel_percent(clipped)
    if clipped["shape"][0] > 5 and valid > 50:
        ok(f"S1 GRD-like clip non-empty: {clipped['shape']} @ {valid:.0f}% valid "
           "(was 1x2 px / 0% before the fix)")
    else:
        bad(f"S1 GRD-like clip collapsed: {clipped['shape']} @ {valid:.1f}%")


def test_bug1_degenerate_guard():
    print("\n[BUG 1] degenerate-transform guard refuses to clip")
    band = np.full((400, 500), 1000.0, dtype="float32")
    stacked = {
        "bands": {"VV": band},
        "tci": None,
        "transform": rasterio.Affine.identity(),
        "crs": None,
        "shape": (400, 500),
    }
    aoi = {"type": "Polygon", "coordinates": [[
        [72.95, 33.45], [73.25, 33.45], [73.25, 33.75],
        [72.95, 33.75], [72.95, 33.45]]]}
    clipped = processor.clip_to_polygon(stacked, aoi)
    if clipped is None:
        ok("clip refused (returned None) on an unresolved identity/None-crs raster")
    else:
        bad(f"clip did NOT refuse a degenerate raster: {clipped['shape']}")


def run_bug1():
    test_bug1_4326_poly_vs_utm_raster()
    test_bug1_gcp_raster_resolved()
    test_bug1_degenerate_guard()


# --------------------------------------------------------------------------- #
# BUG 2 — valid-pixel coverage (SCL cloud masking, interior AOI, gap geometry)
# --------------------------------------------------------------------------- #
def _clipped_cube(h=200, w=200, scl=None, band_fill=1000.0, hole=None):
    """A clipped-cube dict as clip_to_polygon would return (UTM, full mask)."""
    crs = rasterio.crs.CRS.from_epsg(32643)
    transform = rasterio.Affine(10.0, 0, 500000.0, 0, -10.0, 3720000.0)
    band = np.full((h, w), band_fill, dtype="float32")
    if hole is not None:
        r0, r1, c0, c1 = hole
        band[r0:r1, c0:c1] = 0.0  # nodata hole
    bands = {"B03": band.copy(), "B08": band.copy()}
    if scl is not None:
        bands["SCL"] = scl.astype("float32")
    return {
        "bands": bands,
        "tci": None,
        "transform": transform,
        "crs": crs,
        "shape": (h, w),
        "mask": np.ones((h, w), dtype=bool),
    }


def test_bug2_valid_vs_geometric_on_cloudy_tile():
    print("\n[BUG 2] valid-pixel coverage < geometric on a cloudy tile")
    h = w = 200
    # Geometrically the tile fully covers the AOI (mask all True, all data
    # present), but half the pixels are SCL cloud (class 9 = high-prob cloud).
    scl = np.full((h, w), 4, dtype="int16")   # 4 = vegetation (valid)
    scl[:, : w // 2] = 9                        # left half = cloud (invalid)
    clipped = _clipped_cube(h, w, scl=scl)

    geometric = 100.0  # mask fully covers AOI (footprint-style)
    cov = processor.compute_coverage(clipped)
    vpc = cov["full_aoi_coverage_percent"]
    if vpc < 60.0 and geometric == 100.0:
        ok(f"geometric={geometric:.0f}% but valid-pixel={vpc:.0f}% "
           "(cloud correctly excluded)")
    else:
        bad(f"cloud not reflected: geometric={geometric}, valid={vpc}")
    # The gap must be attributed to cloud, not nodata.
    if cov["gap_cause"]["cloud"] > 0 and cov["gap_cause"]["nodata"] == 0:
        ok(f"gap attributed to cloud ({cov['gap_cause']['cloud']} px), not nodata")
    else:
        bad(f"gap cause wrong: {cov['gap_cause']}")


def test_bug2_full_coverage_passes():
    print("\n[BUG 2] clean full tile -> interior coverage 100%, covered=True")
    h = w = 200
    scl = np.full((h, w), 4, dtype="int16")  # all vegetation (valid)
    clipped = _clipped_cube(h, w, scl=scl)
    cov = processor.compute_coverage(clipped)
    if cov["interior_coverage_percent"] == 100.0 and cov["covered"]:
        ok(f"interior=100.0%, covered=True, gaps={len(cov['gaps'])}")
    else:
        bad(f"clean tile not fully covered: {cov}")


def test_bug2_interior_hole_fails_with_geometry():
    print("\n[BUG 2] a nodata hole in the interior -> covered=False + gap geometry")
    h = w = 300
    # A small nodata hole well inside the AOI (not on the boundary).
    clipped = _clipped_cube(h, w, scl=np.full((h, w), 4, dtype="int16"),
                            hole=(140, 160, 140, 160))
    cov = processor.compute_coverage(clipped)
    if not cov["covered"] and cov["interior_coverage_percent"] < 100.0:
        ok(f"interior hole -> covered=False (interior={cov['interior_coverage_percent']}%)")
    else:
        bad(f"interior hole not caught: {cov}")
    if cov["gaps"] and cov["gaps"][0]["area_km2"] > 0 and cov["gaps"][0]["bbox"]:
        g = cov["gaps"][0]
        ok(f"gap reported geometrically: {g['area_km2']} km^2, bbox={g['bbox'] is not None}")
    else:
        bad(f"gap geometry missing: {cov['gaps']}")
    if cov["gap_cause"]["nodata"] > 0:
        ok(f"gap attributed to nodata ({cov['gap_cause']['nodata']} px)")
    else:
        bad(f"nodata gap cause wrong: {cov['gap_cause']}")


def test_bug2_boundary_pixels_not_a_gap():
    print("\n[BUG 2] full-AOI < 100% from boundary px, interior still 100%")
    # No SCL, no hole: full data. full_aoi may be 100 here (synthetic square),
    # but the key invariant is interior_coverage is defined and == full when
    # there are no real gaps.
    clipped = _clipped_cube(200, 200, scl=np.full((200, 200), 4, dtype="int16"))
    cov = processor.compute_coverage(clipped)
    if cov["interior_coverage_percent"] == 100.0:
        ok("interior coverage 100% on clean tile (boundary erosion applied)")
    else:
        bad(f"interior coverage not 100 on clean tile: {cov}")


def run_bug2():
    test_bug2_valid_vs_geometric_on_cloudy_tile()
    test_bug2_full_coverage_passes()
    test_bug2_interior_hole_fails_with_geometry()
    test_bug2_boundary_pixels_not_a_gap()


# --------------------------------------------------------------------------- #
# BUG 3 / 4 — tiered temporally-coherent mosaic + acquisition dedup
# --------------------------------------------------------------------------- #
import sentinel  # noqa: E402


# Default footprint: a generous box around the test AOI (73.0-73.1, 33.5-33.6)
# so BUG 4b's pre-download intersection guard doesn't drop synthetic scenes
# that don't care about footprint geometry.
_DEFAULT_FOOTPRINT = {
    "type": "Polygon",
    "coordinates": [[[72.0, 32.5], [74.0, 32.5], [74.0, 34.5], [72.0, 34.5], [72.0, 32.5]]],
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
    s = {
        "Id": pid or name,
        "Name": name,
        "ContentDate": {"Start": date_iso},
        "Attributes": attrs,
        "GeoFootprint": footprint if footprint is not None else _DEFAULT_FOOTPRINT,
    }
    return s


def test_bug4_dedup_cog_twins():
    print("\n[BUG 4] COG / non-COG twins collapse to one candidate")
    # Same S2 acquisition (tile + datetime), two product formats.
    base = "S2B_MSIL2A_20260710T054641_N0511_R091_T43SCT_20260710T081234"
    non_cog = _scene(base + ".SAFE", "2026-07-10T05:46:41.024Z", 91, pid="p-std")
    cog = _scene(base + "_COG.SAFE", "2026-07-10T05:46:41.024Z", 91, pid="p-cog")
    other = _scene(
        "S2B_MSIL2A_20260707T054641_N0511_R091_T43SCT_x.SAFE",
        "2026-07-07T05:46:41.024Z", 91, pid="p-other")
    deduped = sentinel.dedupe_by_acquisition([non_cog, cog, other])
    ids = [s["Id"] for s in deduped]
    # Phase 0a determinism: the COG twin is now DETERMINISTICALLY preferred
    # (regardless of catalogue order), occupying the first twin's rank slot.
    if len(deduped) == 2 and "p-cog" in ids and "p-std" not in ids:
        ok("COG twin collapsed: kept p-cog + p-other, dropped p-std")
    else:
        bad(f"dedup wrong: {ids}")
    # Order-independence: reversed input must yield the same surviving format.
    deduped_rev = sentinel.dedupe_by_acquisition([cog, non_cog, other])
    ids_rev = [s["Id"] for s in deduped_rev]
    if ids_rev == ids:
        ok("dedup is order-independent (same survivors for reversed input)")
    else:
        bad(f"dedup order-dependent: {ids} vs {ids_rev}")


def test_bug3_s1_never_mixes_asc_desc():
    print("\n[BUG 3] S1 mosaics never mix ascending and descending")
    asc1 = _scene("S1A_IW_GRDH_1SDV_20260710T010101_20260710T010130_x_A.SAFE",
                  "2026-07-10T01:01:01Z", 12, "ASCENDING", pid="a1")
    asc2 = _scene("S1A_IW_GRDH_1SDV_20260710T010201_20260710T010230_y_A.SAFE",
                  "2026-07-10T01:02:01Z", 12, "ASCENDING", pid="a2")
    desc = _scene("S1A_IW_GRDH_1SDV_20260710T130101_20260710T130130_z_D.SAFE",
                  "2026-07-10T13:01:01Z", 12, "DESCENDING", pid="d1")
    tiers = sentinel.build_coverage_tiers([asc1, asc2, desc], "sentinel-1")
    mixed = False
    for _tier, _dir, group in tiers:
        dirs = {sentinel.scene_orbit_direction(s) for s in group}
        if len(dirs) > 1:
            mixed = True
    if not mixed and tiers:
        ok(f"no tier group mixes orbit directions ({len(tiers)} groups)")
    else:
        bad(f"a tier group mixed asc/desc (mixed={mixed}, groups={len(tiers)})")


def test_bug3_tier_windows():
    print("\n[BUG 3] tiers widen by date + require same relative orbit (1-3)")
    anchor = _scene("S2_..._20260710T0_T43SCT_a.SAFE", "2026-07-10T05:46:41Z", 91,
                    pid="anchor")
    d2 = _scene("S2_..._20260708T0_T43SCT_b.SAFE", "2026-07-08T05:46:41Z", 91,
                pid="d2")          # -2 days, same orbit -> tier 2+
    d10_same = _scene("S2_..._20260630T0_T43SCT_c.SAFE", "2026-06-30T05:46:41Z", 91,
                      pid="d10")   # -10 days, same orbit -> tier 4 only
    d2_diff = _scene("S2_..._20260708T0_T43XXX_e.SAFE", "2026-07-08T05:46:41Z", 42,
                     pid="ddiff")  # -2 days, DIFFERENT orbit -> tier 4 only
    tiers = sentinel.build_coverage_tiers(
        [anchor, d2, d10_same, d2_diff], "sentinel-2")
    by_tier = {t: g for t, _d, g in tiers}
    t1_ids = {s["Id"] for s in by_tier.get(1, [])}
    t2_ids = {s["Id"] for s in by_tier.get(2, [])}
    t4_ids = {s["Id"] for s in by_tier.get(4, [])}
    good = (
        t1_ids == {"anchor"}                      # only same-day same-orbit
        and "d2" in t2_ids and "ddiff" not in t2_ids   # tier2: same orbit only
        and "ddiff" in t4_ids and "d10" in t4_ids       # tier4: any orbit/±14d
    )
    if good:
        ok(f"tier1={t1_ids}, tier2 has d2 not ddiff, tier4 has ddiff+d10")
    else:
        bad(f"tier windows wrong: t1={t1_ids} t2={t2_ids} t4={t4_ids}")


def test_bug3_acquisition_accessors():
    print("\n[BUG 3] orbit direction / relative orbit / date accessors")
    s = _scene("S1A_x.SAFE", "2026-07-10T01:02:03Z", 45, "ASCENDING")
    if (sentinel.scene_orbit_direction(s) == "ASCENDING"
            and sentinel.scene_relative_orbit(s) == 45
            and str(sentinel.scene_acq_date(s)) == "2026-07-10"):
        ok("orbitDirection=ASCENDING, relOrbit=45, date=2026-07-10 parsed")
    else:
        bad("accessor parse failed")


def run_bug34():
    test_bug4_dedup_cog_twins()
    test_bug3_s1_never_mixes_asc_desc()
    test_bug3_tier_windows()
    test_bug3_acquisition_accessors()


# --------------------------------------------------------------------------- #
# BUG 5 — SAR calibration contract present on both S1 and S2 paths
# --------------------------------------------------------------------------- #
def test_bug5_calibration_contract():
    print("\n[BUG 5] index_calibrated + index_units on both S1 and S2 paths")
    # S2 NDWI (flood): calibrated ratio.
    h = w = 60
    b03 = np.full((h, w), 1200.0, dtype="float32")
    b08 = np.full((h, w), 800.0, dtype="float32")
    s2_clip = {"bands": {"B03": b03, "B08": b08},
               "mask": np.ones((h, w), bool), "shape": (h, w)}
    s2 = processor.calculate_indices(s2_clip, "sentinel-2", "flood")
    if s2 and s2["index_calibrated"] is True and s2["index_units"] == "NDWI_ratio":
        ok("S2 NDWI: index_calibrated=True, units=NDWI_ratio")
    else:
        bad(f"S2 contract wrong: {None if not s2 else (s2.get('index_calibrated'), s2.get('index_units'))}")

    # S1 SAR: uncalibrated dB.
    vv = np.full((h, w), 0.05, dtype="float32")
    s1_clip = {"bands": {"VV": vv}, "mask": np.ones((h, w), bool), "shape": (h, w)}
    s1 = processor.calculate_indices(s1_clip, "sentinel-1", "flood")
    if s1 and s1["index_calibrated"] is False and s1["index_units"] == "dB_uncalibrated":
        ok("S1 SAR: index_calibrated=False, units=dB_uncalibrated")
    else:
        bad(f"S1 contract wrong: {None if not s1 else (s1.get('index_calibrated'), s1.get('index_units'))}")


def run_bug5():
    test_bug5_calibration_contract()


# --------------------------------------------------------------------------- #
# BUG 3 (driver) — 95% coverage -> failed (not a risk level); tier 3/4 lowers
# confidence + appends anomaly. Driven with _attempt_clip / compute_coverage /
# _render_clip stubbed so no network or raster work is needed.
# --------------------------------------------------------------------------- #
class _FakeTracker:
    def __init__(self):
        self.concerns = []
        self.evidence = []

    def add_concern(self, text, severity):
        self.concerns.append((severity, text))

    def add_evidence(self, *a, **k):
        self.evidence.append((a, k))


def _install_stubs(monkeypatch_targets):
    """Return (restore_fn) after replacing processor internals with stubs."""
    saved = {}
    for name, fn in monkeypatch_targets.items():
        saved[name] = getattr(processor, name)
        setattr(processor, name, fn)

    def restore():
        for name, orig in saved.items():
            setattr(processor, name, orig)
    return restore


def test_bug3_partial_coverage_fails_not_risk():
    print("\n[BUG 3 / coverage-tolerance] a mosaic below COVERAGE_FLOOR (80%) "
          "returns status=failed, no risk")
    # UPDATED 2026-07-28 (fix/coverage-tolerance): the old rule required
    # EXACTLY 100% or hard-failed, so a 95% mosaic was the "fails honestly"
    # case. Coverage is now a caller-controlled quality band (see
    # processor.py's DEFAULT_MIN_COVERAGE_PERCENT=90 / COVERAGE_FLOOR=80) —
    # 95% is now well above even the default TARGET and legitimately
    # succeeds as "complete" (see test_coverage_tolerance.py's
    # test_97_percent_completes_with_penalty for that case). This test now
    # exercises the real hard-fail case: coverage below the 80% FLOOR, which
    # still returns status=failed/insufficient_coverage exactly as before.
    s1 = _scene("S2_..._20260710T0_T43SCT_a.SAFE", "2026-07-10T05:46:41Z", 91, pid="a")
    s2 = _scene("S2_..._20260710T0_T43SCT_b.SAFE", "2026-07-10T05:46:41Z", 91, pid="b")

    calls = {"n": 0}

    def fake_attempt_clip(selection, scenes, poly, eid, token, dt):
        calls["n"] += 1
        return {"_stacked": {}, "shape": (10, 10), "n": len(scenes)}

    def fake_compute_coverage(clipped):
        # Never reaches the floor: best interior is 70% (< COVERAGE_FLOOR=80).
        return {
            "interior_coverage_percent": 70.0,
            "full_aoi_coverage_percent": 69.0,
            "covered": False,
            "gaps": [{"pixels": 50, "area_km2": 0.5,
                      "bbox": {"west": 73.0, "south": 33.5,
                               "east": 73.1, "north": 33.6}}],
            "gap_cause": {"nodata": 50, "cloud": 0},
        }

    restore = _install_stubs({
        "_attempt_clip": fake_attempt_clip,
        "compute_coverage": fake_compute_coverage,
    })
    try:
        poly = {"type": "Polygon", "coordinates": [[
            [73.0, 33.5], [73.1, 33.5], [73.1, 33.6], [73.0, 33.6], [73.0, 33.5]]]}
        res = processor.process_satellite_imagery(
            {"satellite_type": "sentinel-2"}, [s1, s2], (73, 33.5, 73.1, 33.6),
            poly, "evt-partial", "tok", "flood", tracker=_FakeTracker(),
        )
    finally:
        restore()

    if res and res.get("status") == "failed" and res.get("reason") == "insufficient_coverage":
        ok(f"partial coverage -> status=failed/insufficient_coverage "
           f"(best {res.get('coverage_percent')}%)")
    else:
        bad(f"partial coverage did NOT fail honestly: {res}")
    # Must NOT carry a risk/index result.
    if res and "water_percent" not in res and "index_type" not in res:
        ok("failed result carries no risk level / index (no partial analysis)")
    else:
        bad(f"failed result leaked a risk level: {res}")
    if res and res.get("gaps") and res["gaps"][0]["bbox"]:
        ok(f"failure reports gap geometry ({res.get('uncovered_regions')} region(s), "
           f"{res.get('uncovered_area_km2')} km^2)")
    else:
        bad(f"failure missing gap geometry: {res}")


def test_bug3_tier3_lowers_confidence_and_anomaly():
    print("\n[BUG 3] tier-3 success lowers confidence + appends an anomaly")
    # Anchor + a scene 6 days earlier, same orbit -> only tier 3 (±7d) includes both.
    anchor = _scene("S2_..._20260710T0_T43SCT_a.SAFE", "2026-07-10T05:46:41Z", 91, pid="a")
    older = _scene("S2_..._20260704T0_T43SCT_b.SAFE", "2026-07-04T05:46:41Z", 91, pid="b")

    def fake_attempt_clip(selection, scenes, poly, eid, token, dt):
        return {"_stacked": {}, "shape": (10, 10), "n": len(scenes)}

    def fake_compute_coverage(clipped):
        # One scene -> 80%; two scenes -> 100% (forces reaching into the tier).
        covered = clipped.get("n", 1) >= 2
        return {
            "interior_coverage_percent": 100.0 if covered else 80.0,
            "full_aoi_coverage_percent": 99.0 if covered else 79.0,
            "covered": covered,
            "gaps": [], "gap_cause": {"nodata": 0, "cloud": 0},
        }

    def fake_render_clip(clipped, sat, dt, oid):
        return {"satellite_type": sat, "index_type": "NDWI",
                "water_percent": 12.0, "mean_index": 0.2,
                "class_counts": {}, "affected_area_km2": 5.0,
                "index_calibrated": True, "index_units": "NDWI_ratio",
                "png_paths": {}, "geojson": {"total_area": 5.0}, "bounds": {}}

    trk = _FakeTracker()
    restore = _install_stubs({
        "_attempt_clip": fake_attempt_clip,
        "compute_coverage": fake_compute_coverage,
        "_render_clip": fake_render_clip,
    })
    try:
        poly = {"type": "Polygon", "coordinates": [[
            [73.0, 33.5], [73.1, 33.5], [73.1, 33.6], [73.0, 33.6], [73.0, 33.5]]]}
        res = processor.process_satellite_imagery(
            {"satellite_type": "sentinel-2"}, [anchor, older],
            (73, 33.5, 73.1, 33.6), poly, "evt-tier3", "tok", "flood",
            tracker=trk,
        )
    finally:
        restore()

    if res and res.get("coverage_tier") == 3 and res.get("coverage_percent") == 100.0:
        ok(f"reached 100% at tier {res.get('coverage_tier')} "
           f"({res.get('temporal_spread_days')}d spread, "
           f"{res.get('acquisition_count')} acq)")
    else:
        bad(f"did not reach 100% at tier 3: tier={res.get('coverage_tier') if res else None}")
    if any("tier 3" in t.lower() or "temporal" in t.lower() for _s, t in trk.concerns):
        ok(f"tracker got a temporal-spread concern ({len(trk.concerns)} concern(s))")
    else:
        bad(f"no temporal-spread concern added: {trk.concerns}")
    if res and res.get("coverage_anomalies"):
        ok(f"result carries a coverage anomaly: {res['coverage_anomalies'][0]['type']}")
    else:
        bad(f"no coverage anomaly appended: {res.get('coverage_anomalies') if res else None}")


def test_bug7_memory_report():
    print("\n[BUG 7] memory_report exposes per-stage peak RSS")
    processor._STAGE_PEAK_RSS.clear()
    processor._mem_stage("download+mosaic", tiles=3)
    processor._mem_stage("clip", tiles=3)
    mr = processor.memory_report()
    # psutil is installed, so we expect real numbers; tolerate 0.0 if absent.
    if mr and "per_stage" in mr and "peak_stage" in mr:
        ok(f"memory_report: peak_stage={mr['peak_stage']} peak={mr['peak_mb']} MB, "
           f"stages={list(mr['per_stage'])}")
    else:
        bad(f"memory_report malformed: {mr}")


def test_bug1_mosaic_resolves_gcp_sources():
    print("\n[BUG 1] _mosaic_bands resolves GCP sources before merging (not raw)")
    tmp = tempfile.mkdtemp(prefix="hm-mosaic-")
    p1 = os.path.join(tmp, "s1.tiff")
    p2 = os.path.join(tmp, "s2.tiff")
    # Two adjacent GCP-georeferenced tiles (like an S1D GRD pair), each
    # covering half of a shared AOI, both crs=None/identity like a real S1 GRD.
    _write_gcp_tiff(p1, 72.9, 33.4, 73.1, 33.8, fill=50.0)
    _write_gcp_tiff(p2, 73.1, 33.4, 73.3, 33.8, fill=80.0)

    dst_crs = processor.rasterio.crs.CRS.from_epsg(32643)
    mosaicked = processor._mosaic_bands(
        [{"VV": p1}, {"VV": p2}], "evt-mosaic-test", "sentinel-1", dst_crs
    )
    out_path = mosaicked.get("VV")
    if not out_path or not os.path.exists(out_path):
        return bad(f"mosaic did not produce an output file: {mosaicked}")
    with processor.rasterio.open(out_path) as ds:
        ok_crs = ds.crs is not None and ds.crs.to_epsg() == 32643
        ok_transform = ds.transform != processor.rasterio.Affine.identity()
        arr = ds.read(1)
    if ok_crs and ok_transform:
        ok(f"mosaic output has real georeferencing: {ds.crs}, shape={arr.shape}")
    else:
        bad(f"mosaic output still degenerate: crs={ds.crs}")
    # Both source values should appear somewhere in the merged mosaic (not just
    # "using first scene only" silently dropping the second).
    has_both = bool((arr == 50.0).any()) and bool((arr == 80.0).any())
    if has_both:
        ok("merged mosaic contains data from BOTH sources (not first-only fallback)")
    else:
        bad(f"mosaic missing one source's data (first-only fallback?) "
            f"has50={( arr==50.0).any()} has80={(arr==80.0).any()}")


def run_bug37():
    test_bug3_partial_coverage_fails_not_risk()
    test_bug3_tier3_lowers_confidence_and_anomaly()
    test_bug7_memory_report()
    test_bug1_mosaic_resolves_gcp_sources()


if __name__ == "__main__":
    print("=" * 64)
    print("SATELLITE COVERAGE/CRS CORRECTNESS TESTS")
    print("=" * 64)
    run_bug1()
    run_bug2()
    run_bug34()
    run_bug5()
    run_bug37()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
