"""TEST SUITE 5: R2 Upload + bounds + demo cache.

Uploads a small synthetic artifact set, verifies each public URL is fetchable
(HTTP 200), checks the bounds payload shape from a synthetic clip, and exercises
the demo-cache path. Self-contained: builds its own tiny PNGs/GeoJSON so it does
not depend on a live satellite download.
"""

import json
import logging
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import numpy as np  # noqa: E402
import requests  # noqa: E402
from PIL import Image  # noqa: E402

from r2_upload import check_demo_cache, upload_all_results  # noqa: E402
from processor import _compute_bounds  # noqa: E402

PASS, FAIL, WARN = [], [], []


def ok(t, m):
    PASS.append(t)
    print(f"  PASS [{t}]: {m}")


def bad(t, m):
    FAIL.append(t)
    print(f"  FAIL [{t}]: {m}")


def warn(t, m):
    WARN.append(t)
    print(f"  WARN [{t}]: {m}")


def _make_png(path, color):
    arr = np.zeros((16, 16, 4), dtype="uint8")
    arr[..., :3] = color
    arr[..., 3] = 255
    Image.fromarray(arr, "RGBA").save(path)


def run():
    print("=" * 60)
    print("TEST SUITE 5: R2 Upload")
    print("=" * 60)

    tmp = tempfile.mkdtemp(prefix="hm-r2-test-")
    tc = os.path.join(tmp, "true_color.png")
    ix = os.path.join(tmp, "index_map.png")
    cl = os.path.join(tmp, "classification.png")
    _make_png(tc, (10, 120, 200))
    _make_png(ix, (0, 80, 160))
    _make_png(cl, (200, 40, 40))
    geojson = {
        "type": "FeatureCollection",
        "total_area": 1.23,
        "features": [
            {
                "type": "Feature",
                "properties": {"risk_type": "water", "area_km2": 1.23, "severity": "high"},
                "geometry": {"type": "Polygon", "coordinates": [[[71.4, 34.0], [71.5, 34.0], [71.5, 34.1], [71.4, 34.1], [71.4, 34.0]]]},
            }
        ],
    }

    event_id = "selftest-suite5"

    print("\nT5.1 upload all artifacts -> HTTP 200")
    urls = upload_all_results(event_id, {
        "true_color": tc, "index_map": ix, "classification": cl, "geojson": geojson,
    })
    checks = [
        ("true_color_url", urls.get("true_color_url")),
        ("index_url", urls.get("index_url")),
        ("classification_url", urls.get("classification_url")),
        ("geojson_url", urls.get("geojson_url")),
    ]
    for name, url in checks:
        if not url:
            bad("T5.1", f"{name} not produced (upload failed / no creds)")
            continue
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                ok("T5.1", f"{name} -> HTTP 200 ({len(r.content)} bytes)")
            else:
                bad("T5.1", f"{name} -> HTTP {r.status_code}")
        except requests.RequestException as e:
            bad("T5.1", f"{name} fetch error: {e}")

    print("\nT5.2 bounds payload shape (synthetic clip)")
    # Build a minimal clip dict in WGS84 to exercise _compute_bounds.
    from affine import Affine
    import rasterio
    clip = {
        "shape": (100, 100),
        "transform": Affine(0.001, 0, 71.4, 0, -0.001, 34.1),
        "crs": rasterio.crs.CRS.from_epsg(4326),
    }
    bounds = _compute_bounds(clip)
    if not bounds:
        bad("T5.2", "_compute_bounds returned None")
    else:
        b = bounds.get("bounds") or {}
        if all(k in b for k in ("west", "east", "south", "north")):
            ok("T5.2", f"bounds.west/east/south/north present: {b}")
        else:
            bad("T5.2", f"bounds missing keys: {b}")
        corners = bounds.get("bounds_corners")
        if isinstance(corners, list) and len(corners) == 4:
            ok("T5.2", f"bounds_corners has 4 points")
        else:
            bad("T5.2", f"bounds_corners wrong: {corners}")
        leaflet = bounds.get("bounds_leaflet")
        if (isinstance(leaflet, list) and len(leaflet) == 2
                and len(leaflet[0]) == 2 and len(leaflet[1]) == 2):
            ok("T5.2", f"bounds_leaflet = [[S,W],[N,E]] : {leaflet}")
        else:
            bad("T5.2", f"bounds_leaflet wrong: {leaflet}")

    print("\nT5.3 demo cache check (event_id=peshawar)")
    # 'demo-peshawar' is not a demo key; the demo keys are peshawar/dhaka/kathmandu.
    res_nondemo = check_demo_cache("demo-peshawar")
    if res_nondemo is None:
        ok("T5.3", "non-demo id 'demo-peshawar' short-circuits to None (correct)")
    else:
        warn("T5.3", f"unexpected hit for non-demo id: {res_nondemo}")
    res = check_demo_cache("peshawar")
    if res is None:
        ok("T5.3", "check_demo_cache('peshawar') ran, returned None (not cached)")
    elif isinstance(res, str) and res.startswith("http"):
        ok("T5.3", f"check_demo_cache('peshawar') returned URL: {res}")
    else:
        bad("T5.3", f"unexpected demo cache result: {res!r}")


if __name__ == "__main__":
    start = time.time()
    run()
    dur = time.time() - start
    print("\n" + "=" * 60)
    print(f"SUITE 5 SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)} WARN={len(WARN)} in {dur:.1f}s")
    if FAIL:
        print("FAILED:", FAIL)
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
