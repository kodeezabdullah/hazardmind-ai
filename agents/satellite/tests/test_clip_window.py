"""Unit test: windowed clip_to_polygon is correct AND fast.

Builds a synthetic UTM cube and a small polygon covering a known sub-region,
clips, and verifies:
  - the clipped extent matches the polygon window (not the whole grid),
  - inside-polygon pixels keep their values, outside are NaN,
  - re-clipping a large cube to a tiny polygon is fast (the perf fix).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rasterio
from rasterio.warp import transform_geom

from processor import clip_to_polygon

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


def main():
    print("=" * 60)
    print("UNIT: windowed clip_to_polygon")
    print("=" * 60)

    # Large synthetic cube in UTM zone 51N (EPSG:32651), 10 m pixels.
    H, W = 6000, 6000  # 36M px — big enough that full-grid masking is slow
    crs = rasterio.crs.CRS.from_epsg(32651)
    # Origin near Mindanao-ish eastings/northings.
    transform = rasterio.Affine(10.0, 0, 500000.0, 0, -10.0, 1000000.0)
    band = np.arange(H * W, dtype="float32").reshape(H, W) % 1000 + 1.0  # all > 0
    stacked = {
        "bands": {"B04": band.copy(), "B08": band.copy()},
        "tci": None,
        "transform": transform,
        "crs": crs,
        "shape": (H, W),
    }

    # A small polygon in WGS84 covering a known interior window. Compute its UTM
    # bbox so we know the expected clipped extent. Pick UTM pixel rows/cols
    # [1000:1400, 2000:2500] and convert that box to a UTM polygon, then to WGS84.
    r0, r1, c0, c1 = 1000, 1400, 2000, 2500
    ux0 = transform.c + c0 * transform.a
    ux1 = transform.c + c1 * transform.a
    uy0 = transform.f + r0 * transform.e
    uy1 = transform.f + r1 * transform.e
    utm_poly = {
        "type": "Polygon",
        "coordinates": [[[ux0, uy0], [ux1, uy0], [ux1, uy1], [ux0, uy1], [ux0, uy0]]],
    }
    wgs_poly = transform_geom(crs, "EPSG:4326", utm_poly)

    t0 = time.time()
    clipped = clip_to_polygon(stacked, wgs_poly)
    dt = time.time() - t0
    if clipped is None:
        return bad("clip returned None")

    ch, cw = clipped["shape"]
    print(f"  clipped shape = {clipped['shape']} (input {(H, W)}), took {dt:.2f}s")
    # Expected window ~ 400 rows x 500 cols (allow +/- a few px for rounding).
    if abs(ch - (r1 - r0)) <= 3 and abs(cw - (c1 - c0)) <= 3:
        ok(f"clipped extent matches polygon window (~{r1-r0}x{c1-c0})")
    else:
        bad(f"clipped extent {ch}x{cw} != expected {(r1-r0)}x{(c1-c0)}")

    if ch < H and cw < W:
        ok("clip is a sub-window, not the whole grid")
    else:
        bad("clip did not shrink the grid")

    # Inside-polygon pixels must equal the source values; outside must be NaN.
    out = clipped["bands"]["B04"]
    mask = clipped["mask"]
    inside_vals = out[mask]
    if np.all(np.isfinite(inside_vals)) and inside_vals.size > 0:
        ok(f"inside-polygon pixels finite ({inside_vals.size} px)")
    else:
        bad("inside pixels not all finite")
    if (~mask).any():
        if np.all(np.isnan(out[~mask])):
            ok("outside-polygon pixels are NaN")
        else:
            bad("outside-polygon pixels not all NaN")

    # Cross-check values against the source grid at the window offset.
    col_off = round((clipped["transform"].c - transform.c) / transform.a)
    row_off = round((clipped["transform"].f - transform.f) / transform.e)
    src_sub = band[row_off:row_off + ch, col_off:col_off + cw]
    diff_ok = np.allclose(out[mask], src_sub[mask])
    if diff_ok:
        ok("clipped values match source grid at the window offset (no shift bug)")
    else:
        bad("clipped values do NOT match source — window offset is wrong!")

    # Perf: clipping this 36M-px cube to a tiny polygon should be well under a
    # second now (was multiple seconds when masking the full grid).
    if dt < 2.0:
        ok(f"fast: {dt:.2f}s for a tiny polygon on a 36M-px cube")
    else:
        bad(f"slow: {dt:.2f}s (windowing not effective?)")


if __name__ == "__main__":
    main()
    print("=" * 60)
    print(f"UNIT clip-window: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
