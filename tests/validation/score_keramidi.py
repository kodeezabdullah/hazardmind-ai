"""Phase 0 A/B: drop-only vs both-direction SAR change detection.

Runs BOTH detector variants over the IDENTICAL cached real scenes from the
Kanalia forced-S1 run (post-event 2023-09-13 + three same-orbit pre-event
scenes), clips to the pinned AOI, and scores each against the same EMS
reference. Nothing differs between the two arms except `direction`, so the
metric delta is attributable to the change alone.

This is a STRICTER isolation than two full pipeline runs, which would also
differ in scene selection, LLM calls and run-to-run network variation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "agents" / "satellite"))


def _fix_proj():
    bundled = (
        _HERE.parents[1] / "agents" / "satellite" / "venv" / "Lib"
        / "site-packages" / "rasterio" / "proj_data"
    )
    if bundled.is_dir():
        os.environ["PROJ_LIB"] = str(bundled)
        os.environ["PROJ_DATA"] = str(bundled)


_fix_proj()

import yaml  # noqa: E402
from shapely.geometry import mapping, shape  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

import sar_change_detection as scd  # noqa: E402

CACHE = Path(
    os.environ.get(
        "KERAMIDI_CACHE",
        r"C:\Users\Abdullaaa\AppData\Local\Temp\hazardmind-satellite"
        r"\0d974293-9a61-4ada-9daa-32f1658866e7",
    )
)
EVENT = "emsr271_keramidi"


def _clip(scene_dir: Path, merged):
    from processor import clip_to_polygon, stack_bands

    vv = scene_dir / "bands" / "VV.tiff"
    if not vv.exists():
        print(f"  missing {vv}")
        return None
    stacked = stack_bands({"VV": str(vv)}, "sentinel-1")
    if not stacked:
        print(f"  stack failed for {scene_dir.name}")
        return None
    return clip_to_polygon(stacked, merged)


def main():
    import aoi_pin
    import metrics
    import reference_loader
    from rasterio.features import shapes as rio_shapes
    from rasterio.warp import transform_geom

    cfg = yaml.safe_load(
        (_HERE / "reference_events" / f"{EVENT}.yaml").read_text(encoding="utf-8")
    )
    loc = cfg["pipeline_location"]

    # Resolve the AOI through the SAME pinned path every scored run used, so
    # this comparison sits in the identical geometric frame as IoU 0.0083.
    with aoi_pin.pinned_aoi():
        from boundary import get_risk_city_boundaries, merge_risk_boundaries

        _parts = [x.strip() for x in loc.split(",")]
        headline = _parts[0]
        _region = ", ".join(_parts[1:]) or loc
        city_polys = get_risk_city_boundaries(_region, [headline])
        merged = merge_risk_boundaries(city_polys) if city_polys else None

    if merged is None:
        print("FATAL: could not resolve the pinned AOI")
        return 1
    print(f"AOI resolved (pinned): {shape(merged).bounds}")

    post = _clip(CACHE / "t1", merged)
    if not post:
        print("FATAL: post-event clip failed")
        return 1
    shp = post["bands"]["VV"].shape
    # GRID ALIGNMENT — identical to the pipeline's own _fetch_pre_event_stack:
    # each scene clips to its OWN footprint-derived grid, so pre-event clips
    # generally differ in shape AND origin from the post clip. Reproject onto
    # the post grid rather than crop/pad, which would mis-register the ratio.
    import numpy as np
    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    pres = []
    for d in ("pre_0", "pre_1", "pre_2"):
        c = _clip(CACHE / d, merged)
        if c is None:
            continue
        vv = c["bands"]["VV"]
        if vv.shape != shp:
            dest = np.full(shp, np.nan, dtype="float32")
            reproject(
                source=vv.astype("float32"), destination=dest,
                src_transform=c["transform"], src_crs=c["crs"],
                dst_transform=post["transform"], dst_crs=post["crs"],
                resampling=Resampling.bilinear,
                src_nodata=np.nan, dst_nodata=np.nan,
            )
            print(f"  {d}: resampled {vv.shape} -> {shp}")
            vv = dest
        pres.append(vv)
    print(f"post shape={shp}  aligned pre scenes={len(pres)}")
    if not pres:
        print("FATAL: no aligned baseline")
        return 1

    ref_geom, ref_crs = reference_loader.load_reference_geometry(
        cfg["reference_products"]["sentinel1"]["download_url"],
        cfg["reference_products"]["sentinel1"]["vector_layer_of_record"],
        EVENT,
    )
    ref_clipped = ref_geom.intersection(shape(merged))
    print(f"reference clipped to AOI: {ref_clipped.area:.6f} deg^2")

    rows = []
    for direction in ("drop", "both"):
        cd = scd.detect_flood_change(
            post["bands"]["VV"], pres, valid_mask=post.get("mask"),
            orbit_direction="DESCENDING", direction=direction,
        )
        print(
            f"\n--- direction={direction} ---\n"
            f"  status={cd['status']}  water_percent={cd.get('water_percent')}\n"
            f"  drop_thr={cd.get('threshold_db')}  rise_thr={cd.get('rise_threshold_db')}\n"
            f"  drop_px={cd.get('open_water_drop_pixels')}  "
            f"rise_px={cd.get('flooded_vegetation_rise_pixels')}\n"
            f"  mean_change_db={cd.get('mean_change_db')}"
        )
        m = cd.get("flood_mask")
        if m is None or not m.any():
            print("  -> 0 predicted zones (undefined score)")
            rows.append((direction, None))
            continue
        polys = [
            shape(g)
            for g, v in rio_shapes(m.astype("uint8"), mask=m,
                                   transform=post["transform"])
            if v == 1
        ]
        pred = unary_union(polys)
        if post.get("crs") is not None:
            pred = shape(transform_geom(post["crs"], "EPSG:4326", mapping(pred)))
        sc = metrics.compute_extent_metrics(pred, "EPSG:4326",
                                            ref_clipped, ref_crs)
        rows.append((direction, sc))
        print(
            f"  IoU={sc.iou:.4f}  P={sc.precision:.4f}  R={sc.recall:.4f}  "
            f"F1={sc.f1:.4f}\n"
            f"  pred={sc.predicted_area_km2:.3f} km2  "
            f"ref={sc.reference_area_km2:.3f} km2"
        )

    print("\n" + "=" * 62)
    print("PHASE 0 A/B — identical scenes, identical AOI, identical reference")
    print("=" * 62)
    for direction, sc in rows:
        if sc is None:
            print(f"  {direction:5} : 0 zones (undefined)")
        else:
            print(
                f"  {direction:5} : IoU {sc.iou:.4f}  P {sc.precision:.4f}  "
                f"R {sc.recall:.4f}  F1 {sc.f1:.4f}  pred {sc.predicted_area_km2:.2f} km2"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
