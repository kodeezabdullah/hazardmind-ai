"""TEST SUITE 2: Boundary Module (live Nominatim)."""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from shapely.geometry import shape  # noqa: E402

from boundary import (  # noqa: E402
    get_analysis_bbox,
    get_region_boundary,
    get_risk_city_boundaries,
    merge_risk_boundaries,
)

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


# Punjab, Pakistan roughly spans lon 69-76, lat 27-34.
PUNJAB_LON = (69.0, 76.5)
PUNJAB_LAT = (27.0, 34.5)
PAK_LON = (60.0, 78.0)
PAK_LAT = (23.0, 37.5)


def run():
    print("=" * 60)
    print("TEST SUITE 2: Boundary Module")
    print("=" * 60)

    print("\nT2.1 get_region_boundary('Punjab, Pakistan')")
    region = get_region_boundary("Punjab, Pakistan")
    if not region:
        bad("T2.1", "returned None")
        return
    geo = region.get("geojson")
    if geo and geo.get("type") in ("Polygon", "MultiPolygon"):
        ok("T2.1", f"GeoJSON {geo['type']} returned")
    else:
        bad("T2.1", f"no polygon geojson: {type(geo)} {geo and geo.get('type')}")
    bbox = region.get("bbox")
    if bbox and PUNJAB_LON[0] <= bbox[0] and bbox[2] <= PUNJAB_LON[1] and PUNJAB_LAT[0] <= bbox[1] and bbox[3] <= PUNJAB_LAT[1]:
        ok("T2.1", f"bbox reasonable for Punjab: {tuple(round(x,2) for x in bbox)}")
    else:
        warn("T2.1", f"bbox outside expected Punjab range: {bbox}")

    print("\nT2.2 get_risk_city_boundaries('Punjab, Pakistan', ['Lahore','Multan'])")
    cities = get_risk_city_boundaries("Punjab, Pakistan", ["Lahore", "Multan"])
    if len(cities) == 2:
        ok("T2.2", "2 boundaries returned")
    else:
        bad("T2.2", f"expected 2, got {len(cities)}: {[c.get('name') for c in cities]}")
    valid = 0
    for c in cities:
        g = c.get("geojson")
        try:
            sh = shape(g)
            if sh.is_valid and sh.area > 0:
                valid += 1
        except Exception as e:
            warn("T2.2", f"city {c.get('name')} geometry error: {e}")
    if valid == len(cities) and cities:
        ok("T2.2", f"all {valid} cities have valid non-empty GeoJSON")
    else:
        bad("T2.2", f"only {valid}/{len(cities)} valid geometries")

    print("\nT2.3 merge_risk_boundaries(cities)")
    merged = merge_risk_boundaries(cities)
    if not merged:
        bad("T2.3", "returned None")
    else:
        try:
            msh = shape(merged)
            if msh.is_valid and msh.area > 0:
                ok("T2.3", f"merged polygon valid, area={msh.area:.4f} deg^2")
            else:
                bad("T2.3", "merged polygon invalid or zero area")
            indiv_areas = [shape(c["geojson"]).area for c in cities]
            if msh.area >= max(indiv_areas) - 1e-9:
                ok("T2.3", f"merged area ({msh.area:.4f}) >= largest individual ({max(indiv_areas):.4f})")
            else:
                bad("T2.3", f"merged area {msh.area:.4f} < largest individual {max(indiv_areas):.4f}")
        except Exception as e:
            bad("T2.3", f"merge geometry error: {e}")

    print("\nT2.4 get_analysis_bbox(merged)")
    if merged:
        abox = get_analysis_bbox(merged)
        if abox and len(abox) == 4:
            ok("T2.4", f"(minx,miny,maxx,maxy) = {tuple(round(x,3) for x in abox)}")
            minx, miny, maxx, maxy = abox
            if minx < maxx and miny < maxy:
                ok("T2.4", "ordering valid (minx<maxx, miny<maxy)")
            else:
                bad("T2.4", "bbox ordering invalid")
            if PAK_LON[0] <= minx and maxx <= PAK_LON[1] and PAK_LAT[0] <= miny and maxy <= PAK_LAT[1]:
                ok("T2.4", "values within Pakistan coords")
            else:
                bad("T2.4", f"bbox outside Pakistan coords: {abox}")
        else:
            bad("T2.4", f"bad bbox: {abox}")


if __name__ == "__main__":
    start = time.time()
    run()
    dur = time.time() - start
    print("\n" + "=" * 60)
    print(f"SUITE 2 SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)} WARN={len(WARN)} in {dur:.1f}s")
    if FAIL:
        print("FAILED:", FAIL)
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
