"""One-off diagnostic for BASELINE_REPORT.md task item 1: what actually
happened at Paiporta. Reproduces every geometry stage the harness/pipeline
went through and prints polygon counts + areas at each, plus the resolved
AOI vs the reference's own AOI, so (a)/(b)/(c) in the task can be
distinguished directly rather than inferred from the 0/0 summary alone.

Read-only: does not invoke the pipeline, does not touch the DB. Uses the
already-cached reference ZIP + a direct boundary.py resolution.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SATELLITE_DIR = REPO_ROOT / "agents" / "satellite"

bundled = SATELLITE_DIR / "venv" / "Lib" / "site-packages" / "rasterio" / "proj_data"
if bundled.is_dir():
    os.environ["PROJ_LIB"] = str(bundled)
    os.environ["PROJ_DATA"] = str(bundled)

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(SATELLITE_DIR))

import geopandas as gpd  # noqa: E402
from shapely.geometry import shape  # noqa: E402

from reference_loader import _find_layer_file  # noqa: E402
from boundary import get_risk_city_boundaries, get_region_boundary  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent / "cache"


def describe(gdf_or_geom, label):
    if hasattr(gdf_or_geom, "geometry"):
        n = len(gdf_or_geom)
        geom = gdf_or_geom.geometry
        total_area_deg2 = float(geom.area.sum())
        bounds = gdf_or_geom.total_bounds
    else:
        g = gdf_or_geom
        n = 1 if not g.is_empty else 0
        total_area_deg2 = g.area
        bounds = g.bounds
    print(f"[{label}] count={n} area_deg2={total_area_deg2:.8f} bounds={bounds}")


def main():
    for path_key, product_id in (
        ("sentinel2", "DEL_MONIT02"),
        ("sentinel1", "DEL_MONIT03"),
        ("sentinel2_monit01", "DEL_MONIT01"),
    ):
        print(f"\n===== path={path_key} product={product_id} =====")
        extract_dir = CACHE_DIR / f"emsr773_paiporta_{path_key}"
        layer_file = _find_layer_file(extract_dir, "maximumFloodExtentA")
        print("layer_file:", layer_file)

        gdf_raw = gpd.read_file(layer_file)
        describe(gdf_raw, "0_raw_shapefile_rows")
        print("  raw CRS:", gdf_raw.crs)
        print("  raw total_bounds (native CRS):", gdf_raw.total_bounds)

        gdf_wgs84 = gdf_raw.to_crs("EPSG:4326")
        print("  raw total_bounds (WGS84):", gdf_wgs84.total_bounds)

        from shapely.ops import unary_union
        dissolved = unary_union(gdf_raw.geometry.values)
        describe(dissolved, "1_dissolved_native_crs")

        dissolved_wgs84 = gpd.GeoSeries([dissolved], crs=gdf_raw.crs).to_crs("EPSG:4326").iloc[0]
        describe(dissolved_wgs84, "2_dissolved_wgs84")
        print("  dissolved_wgs84 bounds:", dissolved_wgs84.bounds)

        # Pipeline's own boundary resolution for "Paiporta, Spain"
        city_boundaries = get_risk_city_boundaries("Paiporta, Spain", ["Paiporta"])
        if not city_boundaries:
            print("  get_risk_city_boundaries returned NOTHING")
            continue
        city_geom = shape(city_boundaries[0]["geojson"])
        print("  pipeline AOI (Paiporta boundary) bounds:", city_geom.bounds, "area_deg2=", city_geom.area)

        clipped = dissolved_wgs84.intersection(city_geom)
        describe(clipped, "3_clipped_to_paiporta_boundary")
        clipped_ea = gpd.GeoSeries([clipped], crs="EPSG:4326").to_crs("EPSG:6933").iloc[0]
        print(f"  clipped_to_paiporta real area_km2 = {clipped_ea.area / 1e6:.6f}")

        # Does the reference even reach this AOI at all? Check distance.
        if clipped.is_empty:
            dist_deg = dissolved_wgs84.distance(city_geom)
            print(f"  clipped result EMPTY. distance(reference, Paiporta boundary) = {dist_deg:.6f} deg")

        # Also compare against the raw region boundary (non-buffered) for context
        region_geom_raw = get_region_boundary("Paiporta, Spain")
        if region_geom_raw:
            rg = shape(region_geom_raw["geojson"]) if isinstance(region_geom_raw, dict) and "geojson" in region_geom_raw else None
            if rg is not None:
                print("  get_region_boundary bounds:", rg.bounds, "area_deg2=", rg.area)


if __name__ == "__main__":
    main()
