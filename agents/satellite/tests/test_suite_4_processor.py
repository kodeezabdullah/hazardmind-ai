"""TEST SUITE 4: Processor Module (live download of one Peshawar S2 scene).

Drives each processor stage in sequence against a real Sentinel-2 scene:
download -> stack -> clip -> indices -> export PNG -> vectorize. Verifies the
output of every stage. A single ~800 MB download is reused across all stages
(cached in the temp dir by event id), so this is one download, not six.
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import processor  # noqa: E402
from boundary import (  # noqa: E402
    get_risk_city_boundaries,
    merge_risk_boundaries,
)
from processor import (  # noqa: E402
    TEMP_ROOT,
    calculate_indices,
    clip_to_polygon,
    download_imagery,
    export_png,
    stack_bands,
    vectorize_classification,
)
from sentinel import (  # noqa: E402
    SENTINEL_2,
    authenticate_copernicus,
    search_imagery,
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


EVENT = "suite4-peshawar"
DISASTER = "flood"
PESHAWAR_BBOX = (71.4, 33.9, 71.7, 34.1)


def run():
    print("=" * 60)
    print("TEST SUITE 4: Processor Module")
    print("=" * 60)

    token = authenticate_copernicus()
    if not token:
        bad("T4.0", "auth failed; cannot run processor suite")
        return

    # Resolve a real Peshawar polygon for the clip stage.
    cities = get_risk_city_boundaries(
        "Khyber Pakhtunkhwa, Pakistan", ["Peshawar"]
    )
    merged = merge_risk_boundaries(cities)
    if not merged:
        bad("T4.0", "could not resolve Peshawar polygon")
        return

    # Find a low-cloud S2 scene that overlaps the city well.
    scene = search_imagery(PESHAWAR_BBOX, SENTINEL_2, date_range=14, aoi_geom=merged)
    if not scene:
        scene = search_imagery(PESHAWAR_BBOX, SENTINEL_2, date_range=30, aoi_geom=merged)
    if not scene:
        bad("T4.0", "no S2 scene found for Peshawar")
        return
    print(f"  using scene {scene.get('Name')} (cloud={scene.get('_cloud')}%, overlap={scene.get('_overlap',0)*100:.0f}%)")

    selection = {"satellite_type": SENTINEL_2}

    # ---- T4.1 download_imagery ----
    print("\nT4.1 download_imagery (flood -> B03,B08,B11,TCI)")
    imagery = download_imagery(selection, scene, EVENT, token, DISASTER)
    if not imagery:
        bad("T4.1", "download_imagery returned None")
        return
    band_paths = imagery.get("band_paths") or {}
    expected = {"B03", "B08", "B11", "TCI"}
    got = set(band_paths.keys())
    if expected.issubset(got):
        ok("T4.1", f"correct flood bands present: {sorted(got)}")
    else:
        bad("T4.1", f"missing bands: expected {sorted(expected)}, got {sorted(got)}")
    sizes_ok = True
    for tok, p in band_paths.items():
        if not (p and os.path.exists(p) and os.path.getsize(p) > 0):
            sizes_ok = False
            bad("T4.1", f"band {tok} file missing/empty: {p}")
    if sizes_ok and band_paths:
        ok("T4.1", f"all {len(band_paths)} band files exist with size > 0")

    # ---- T4.2 stack_bands ----
    print("\nT4.2 stack_bands")
    stacked = stack_bands(band_paths, SENTINEL_2)
    if not stacked:
        bad("T4.2", "stack_bands returned None")
        return
    shape = stacked.get("shape")
    bands = stacked.get("bands") or {}
    if shape and len(shape) == 2 and shape[0] > 0 and shape[1] > 0:
        ok("T4.2", f"reference grid shape={shape}")
    else:
        bad("T4.2", f"bad shape: {shape}")
    same_res = all(arr.shape == shape for arr in bands.values())
    if same_res and bands:
        ok("T4.2", f"all {len(bands)} bands aligned to {shape} (B11 20m resampled to 10m)")
    else:
        bad("T4.2", f"bands not aligned: {[(t, a.shape) for t, a in bands.items()]}")

    # ---- T4.3 clip_to_polygon ----
    print("\nT4.3 clip_to_polygon (Peshawar polygon)")
    clipped = clip_to_polygon(stacked, merged)
    if not clipped:
        bad("T4.3", "clip_to_polygon returned None")
        return
    cshape = clipped.get("shape")
    in_h, in_w = shape
    c_h, c_w = cshape
    if c_h <= in_h and c_w <= in_w and (c_h < in_h or c_w < in_w):
        ok("T4.3", f"clipped {cshape} smaller than input {shape}")
    else:
        warn("T4.3", f"clipped {cshape} not smaller than input {shape}")
    # Outside polygon should be NaN.
    sample_band = next(iter(clipped["bands"].values()))
    mask = clipped.get("mask")
    if mask is not None:
        outside_nan = np.all(np.isnan(sample_band[~mask])) if (~mask).any() else True
        if outside_nan:
            ok("T4.3", "outside-polygon pixels are NaN (nodata)")
        else:
            bad("T4.3", "outside-polygon pixels not all NaN")
    else:
        warn("T4.3", "no mask on clipped result")

    # ---- T4.4 calculate_indices ----
    print("\nT4.4 calculate_indices (flood -> NDWI)")
    indices = calculate_indices(clipped, SENTINEL_2, DISASTER)
    if not indices:
        bad("T4.4", "calculate_indices returned None")
        return
    if indices.get("index_type") == "NDWI":
        ok("T4.4", "NDWI calculated")
    else:
        bad("T4.4", f"expected NDWI, got {indices.get('index_type')}")
    arr = indices.get("array")
    finite = arr[np.isfinite(arr)]
    if finite.size and finite.min() >= -1.0001 and finite.max() <= 1.0001:
        ok("T4.4", f"index values in [-1,1] (min={finite.min():.3f}, max={finite.max():.3f})")
    else:
        bad("T4.4", f"index values out of range: min={finite.min() if finite.size else 'NA'}, max={finite.max() if finite.size else 'NA'}")
    cls = indices.get("classification_array")
    if cls is not None and hasattr(cls, "shape"):
        ok("T4.4", f"classification array returned, shape={cls.shape}, classes={sorted(set(np.unique(cls).tolist()))}")
    else:
        bad("T4.4", "no classification array")

    # ---- T4.5 export_png ----
    print("\nT4.5 export_png (3 PNGs)")
    pngs = export_png(indices, clipped, EVENT, DISASTER)
    if not pngs:
        bad("T4.5", "export_png returned None")
        return
    for name in ("true_color", "index_map", "classification"):
        p = pngs.get(name)
        if p and os.path.exists(p) and os.path.getsize(p) > 0:
            try:
                im = Image.open(p)
                im.verify()
                ok("T4.5", f"{name}.png exists + valid ({im.mode}, {os.path.getsize(p)} bytes)")
            except Exception as e:
                bad("T4.5", f"{name}.png invalid image: {e}")
        else:
            bad("T4.5", f"{name}.png missing/empty: {p}")
    bounds = processor._compute_bounds(clipped)
    if bounds and bounds.get("bounds"):
        ok("T4.5", f"bounds computed for PNGs: {bounds['bounds']}")
    else:
        bad("T4.5", "could not compute PNG bounds")

    # ---- T4.6 vectorize_classification ----
    print("\nT4.6 vectorize_classification")
    geojson = vectorize_classification(
        indices["classification_array"], clipped["transform"], clipped["crs"],
        DISASTER, scheme_key=indices["scheme_key"],
    )
    if not geojson:
        bad("T4.6", "vectorize returned None")
        return
    if geojson.get("type") == "FeatureCollection":
        ok("T4.6", f"FeatureCollection returned ({len(geojson.get('features', []))} features, total_area={geojson.get('total_area')})")
    else:
        bad("T4.6", f"not a FeatureCollection: {geojson.get('type')}")
    feats = geojson.get("features", [])
    if not feats:
        warn("T4.6", "no hazard features (physically plausible: dry-season Peshawar, no flood)")
    else:
        all_have_risk = all("risk_type" in f.get("properties", {}) for f in feats)
        all_have_area = all("area_km2" in f.get("properties", {}) for f in feats)
        if all_have_risk:
            ok("T4.6", "each feature has risk_type")
        else:
            bad("T4.6", "some features missing risk_type")
        if all_have_area:
            ok("T4.6", "each feature has area_km2")
        else:
            bad("T4.6", "some features missing area_km2")


if __name__ == "__main__":
    start = time.time()
    run()
    dur = time.time() - start
    print("\n" + "=" * 60)
    print(f"SUITE 4 SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)} WARN={len(WARN)} in {dur:.1f}s")
    if FAIL:
        print("FAILED:", FAIL)
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
