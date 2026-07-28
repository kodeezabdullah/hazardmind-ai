"""Phase 1c (science/full-pass): permanent-water masking (JRC GSW occurrence).

Flood means water where water is not normally present. Pixels classified
water that the JRC occurrence layer (>= 75% of observed months, 1984-2021)
says are NORMALLY water are reclassified out of the flood extent, with the
share and the threshold/source recorded for auditability.

Offline and deterministic — the JRC fetch is monkeypatched; no network.
"""

import os
import sys

import numpy as np
import rasterio
from rasterio.transform import from_origin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import permanent_water
import processor

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


ROWS = COLS = 10
# 10 m UTM grid at a real-ish location (43S zone).
CLIP_TRANSFORM = from_origin(500000, 4375000, 10, 10)
CLIP_CRS = rasterio.crs.CRS.from_epsg(32643)


def _flood_clip():
    """Left 4 cols water (MNDWI +0.6): cols 0-1 will be marked PERMANENT
    water by the fake JRC window, cols 2-3 are genuine flood."""
    b03 = np.full((ROWS, COLS), 700.0, dtype="float32")
    b11 = np.full((ROWS, COLS), 1300.0, dtype="float32")
    b03[:, :4] = 3000.0
    b11[:, :4] = 750.0
    return {
        "bands": {"B03": b03, "B08": np.full((ROWS, COLS), 1000.0, "float32"), "B11": b11},
        "tci": None,
        "transform": CLIP_TRANSFORM,
        "crs": CLIP_CRS,
        "shape": (ROWS, COLS),
        "mask": np.ones((ROWS, COLS), dtype=bool),
    }


def _clip_bounds_wgs84():
    """The synthetic clip's own bounds reprojected to WGS84."""
    from rasterio.warp import transform_bounds

    left, bottom, right, top = rasterio.transform.array_bounds(
        ROWS, COLS, CLIP_TRANSFORM
    )
    return transform_bounds(CLIP_CRS, "EPSG:4326", left, bottom, right, top)


def _fake_window_factory(occ_left_cols: int):
    """Fake JRC fetch: occurrence 100 over the geographic strip covering the
    clip's left `occ_left_cols` columns (computed from the clip's REAL WGS84
    bounds, so the 0.005-degree fetch pad — huge relative to a 100 m test
    clip — cannot misplace the region), 0 elsewhere."""
    cw, cs, ce, cn = _clip_bounds_wgs84()
    cut_lon = cw + (occ_left_cols / COLS) * (ce - cw)

    def _fake(west, south, east, north, timeout_attempts=2):
        n = 400
        from rasterio.transform import from_bounds as tf_from_bounds

        transform = tf_from_bounds(west, south, east, north, n, n)
        lons = west + (np.arange(n) + 0.5) * (east - west) / n
        arr = np.zeros((n, n), dtype="uint8")
        arr[:, (lons >= cw - 1e-9) & (lons < cut_lon)] = 100
        return {
            "array": arr,
            "transform": transform,
            "crs": rasterio.crs.CRS.from_epsg(4326),
        }

    return _fake


def test_permanent_water_reclassified():
    real = permanent_water.fetch_occurrence_window
    permanent_water.fetch_occurrence_window = _fake_window_factory(2)
    try:
        res = processor.calculate_indices(_flood_clip(), "sentinel-2", "flood")
    finally:
        permanent_water.fetch_occurrence_window = real
    if res is None:
        bad("calculate_indices returned None")
        return
    if not res.get("permanent_water_mask_applied"):
        bad(f"mask not applied: {res.get('permanent_water_mask_applied')}")
        return
    ok("permanent-water mask applied")
    # 2 of 4 water cols are permanent -> flood water drops 40% -> 20%.
    if abs(res["water_percent"] - 20.0) < 2.5:
        ok(f"permanent water excluded from flood extent (water={res['water_percent']}%)")
    else:
        bad(f"water_percent {res['water_percent']} (expected ~20 after masking)")
    if res.get("permanent_water_percent") and abs(res["permanent_water_percent"] - 20.0) < 2.5:
        ok(f"permanent_water_percent recorded ({res['permanent_water_percent']}%)")
    else:
        bad(f"permanent_water_percent {res.get('permanent_water_percent')} (expected ~20)")
    if (res.get("permanent_water_occurrence_threshold") == permanent_water.DEFAULT_OCCURRENCE_THRESHOLD
            and res.get("permanent_water_source") == permanent_water.JRC_SOURCE_LABEL):
        ok("threshold + source recorded — mask re-derivable from the result")
    else:
        bad(f"audit fields wrong: {res.get('permanent_water_occurrence_threshold')} "
            f"{res.get('permanent_water_source')}")


def test_fetch_failure_degrades_gracefully():
    real = permanent_water.fetch_occurrence_window
    permanent_water.fetch_occurrence_window = lambda *a, **k: None
    try:
        res = processor.calculate_indices(_flood_clip(), "sentinel-2", "flood")
    finally:
        permanent_water.fetch_occurrence_window = real
    if res and res.get("permanent_water_mask_applied") is False and abs(res["water_percent"] - 40.0) < 0.01:
        ok("JRC unreachable: run proceeds unmasked, applied=False explicit")
    else:
        bad(f"degradation wrong: applied={res and res.get('permanent_water_mask_applied')} "
            f"water={res and res.get('water_percent')}")


def test_nonflood_untouched():
    clip = _flood_clip()
    clip["bands"]["B04"] = np.full((ROWS, COLS), 900.0, "float32")
    res = processor.calculate_indices(clip, "sentinel-2", "earthquake")
    if res and res.get("permanent_water_mask_applied") is False and res.get("permanent_water_percent") is None:
        ok("non-flood (NDVI) path: no permanent-water masking attempted")
    else:
        bad(f"non-flood affected: {res and res.get('permanent_water_mask_applied')}")


def test_geojson_helper_threshold():
    def _half_water(west, south, east, north, timeout_attempts=2):
        n = 100
        from rasterio.transform import from_bounds as tf_from_bounds

        arr = np.zeros((n, n), dtype="uint8")
        arr[:, : n // 2] = 100
        return {
            "array": arr,
            "transform": tf_from_bounds(west, south, east, north, n, n),
            "crs": rasterio.crs.CRS.from_epsg(4326),
        }

    real = permanent_water.fetch_occurrence_window
    permanent_water.fetch_occurrence_window = _half_water
    try:
        gj = permanent_water.permanent_water_geojson(22.8, 39.4, 23.0, 39.6)
    finally:
        permanent_water.fetch_occurrence_window = real
    from shapely.geometry import shape as shp_shape

    if gj is not None and not shp_shape(gj).is_empty:
        ok("harness geojson helper vectorizes the >=threshold region")
    else:
        bad(f"geojson helper returned {gj}")


if __name__ == "__main__":
    print("=" * 64)
    print("PHASE 1c — PERMANENT-WATER MASKING (JRC GSW)")
    print("=" * 64)
    test_permanent_water_reclassified()
    test_fetch_failure_degrades_gracefully()
    test_nonflood_untouched()
    test_geojson_helper_threshold()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
