"""Phase 1a (science/full-pass): per-pixel SCL masking inside the index.

SCL was used for the coverage metric but never when computing the index —
cloud shadow (SCL class 3) is spectrally almost identical to water in any
water index. These tests assert that SCL-invalid pixels are excluded from
the index support (classification, water_percent, mean_value,
affected_mean_index, affected area), not silently treated as clear land or
zero, and that the exclusion is auditable via `scl_masked_percent`.

Also covers the resampling correctness prerequisite: SCL is categorical, so
stack_bands must resample it nearest-neighbour (bilinear invents class ids
that don't exist at class boundaries).

Offline and deterministic — no network, no CDSE, no LLM.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import processor

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


def _clip_with_shadow(rows: int = 10, cols: int = 10):
    """Left 2 cols: real water (NDWI +0.5, SCL water=6). Next 2 cols: cloud
    shadow that LOOKS like water spectrally (NDWI +0.5, SCL shadow=3).
    Rest: dry land (NDWI -0.3, SCL vegetation=4)."""
    b03 = np.full((rows, cols), 700.0, dtype="float32")
    b08 = np.full((rows, cols), 1300.0, dtype="float32")
    b03[:, :4] = 3000.0
    b08[:, :4] = 1000.0
    scl = np.full((rows, cols), 4.0, dtype="float32")
    scl[:, :2] = 6.0   # water
    scl[:, 2:4] = 3.0  # cloud shadow — the water-lookalike false positive
    return {
        "bands": {"B03": b03, "B08": b08, "SCL": scl},
        "tci": None,
        "transform": None,
        "crs": None,
        "shape": (rows, cols),
        "mask": np.ones((rows, cols), dtype=bool),
    }


def test_shadow_excluded_from_water():
    clip = _clip_with_shadow()
    res = processor.calculate_indices(clip, "sentinel-2", "flood")
    if res is None:
        bad("calculate_indices returned None")
        return
    # Without masking, 40% of pixels would classify as water (4/10 cols).
    # With SCL masking only the 2 genuine water cols remain; support shrinks
    # to 80 pixels, so water = 20/80 = 25%.
    if abs(res["water_percent"] - 25.0) < 0.01:
        ok(f"cloud-shadow water-lookalikes excluded (water={res['water_percent']}%)")
    else:
        bad(f"water_percent {res['water_percent']} (expected 25.0 — shadow cols "
            "must be excluded from both numerator and denominator)")
    if res.get("scl_masked_percent") == 20.0:
        ok("scl_masked_percent records the 20% shadow exclusion")
    else:
        bad(f"scl_masked_percent {res.get('scl_masked_percent')} (expected 20.0)")
    cls = res["classification_array"]
    if (cls[:, 2:4] == processor.NODATA_CLASS).all():
        ok("masked pixels are NODATA_CLASS, not 'safe land' or water")
    else:
        bad(f"masked pixels classified as {np.unique(cls[:, 2:4])}")
    # mean_value support excludes the shadow cols: (2*0.5 + 6*(-0.3))/8
    expected_mean = round((2 * 0.5 + 6 * -0.3) / 8, 4)
    if abs(res["mean_value"] - expected_mean) < 0.001:
        ok(f"mean_value computed over unmasked support only ({res['mean_value']})")
    else:
        bad(f"mean_value {res['mean_value']} (expected {expected_mean})")


def test_no_scl_unchanged():
    clip = _clip_with_shadow()
    del clip["bands"]["SCL"]
    res = processor.calculate_indices(clip, "sentinel-2", "flood")
    if res is None:
        bad("calculate_indices returned None without SCL")
        return
    if res.get("scl_masked_percent") is None and abs(res["water_percent"] - 40.0) < 0.01:
        ok("no SCL band: masking skipped, scl_masked_percent None, water 40%")
    else:
        bad(f"no-SCL run wrong: water={res['water_percent']} "
            f"scl_masked={res.get('scl_masked_percent')}")


def test_s1_unaffected():
    vv = np.full((6, 6), 200.0, dtype="float32")
    clip = {
        "bands": {"VV": vv},
        "tci": None, "transform": None, "crs": None,
        "shape": (6, 6), "mask": np.ones((6, 6), dtype=bool),
    }
    res = processor.calculate_indices(clip, "sentinel-1", "flood")
    if res and res.get("scl_masked_percent") is None:
        ok("sentinel-1 path untouched (scl_masked_percent None)")
    else:
        bad(f"S1 result affected: {res and res.get('scl_masked_percent')}")


def test_scl_resampled_nearest():
    """stack_bands must resample SCL nearest — a checkerboard of classes 3/4
    upsampled 2x must contain ONLY 3.0 and 4.0, never interpolated values."""
    import tempfile
    import rasterio
    from rasterio.transform import from_origin

    tmp = tempfile.mkdtemp(prefix="scl_nearest_")
    t10 = from_origin(500000, 4000000, 10, 10)
    t20 = from_origin(500000, 4000000, 20, 20)
    crs = rasterio.crs.CRS.from_epsg(32643)

    b03 = np.random.default_rng(0).uniform(500, 3000, (8, 8)).astype("float32")
    scl = np.indices((4, 4)).sum(axis=0) % 2 + 3  # checkerboard 3/4
    paths = {}
    for name, arr, tr in (("B03", b03, t10), ("SCL", scl.astype("float32"), t20)):
        p = os.path.join(tmp, f"{name}.tif")
        with rasterio.open(
            p, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
            count=1, dtype="float32", crs=crs, transform=tr,
        ) as dst:
            dst.write(arr, 1)
        paths[name] = p

    stacked = processor.stack_bands(paths, "sentinel-2")
    if stacked is None:
        bad("stack_bands returned None")
        return
    out = stacked["bands"]["SCL"]
    uniq = set(np.unique(out).tolist())
    if uniq <= {3.0, 4.0}:
        ok(f"SCL upsampled nearest — only original class ids present ({sorted(uniq)})")
    else:
        bad(f"SCL interpolated — non-class values present: {sorted(uniq)[:6]}")


if __name__ == "__main__":
    print("=" * 64)
    print("PHASE 1a — PER-PIXEL SCL MASKING INSIDE THE INDEX")
    print("=" * 64)
    test_shadow_excluded_from_water()
    test_no_scl_unchanged()
    test_s1_unaffected()
    test_scl_resampled_nearest()
    print("=" * 64)
    print(f"RESULT: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
