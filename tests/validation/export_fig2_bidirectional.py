"""Exports raw per-pixel SAR change-detection data for Fig. 2 (Bidirectional
Mechanism) — the change-image histogram showing both the decrease population
(open water, specular reflection) and the increase population (flooded
vegetation, double-bounce), their independently-derived asymmetric
thresholds, and a decrease-only vs. bidirectional detected-extent comparison
against the reference outline.

Re-runs sar_change_detection.detect_flood_change directly against Keramidi's
cached temp files (kept via HAZARDMIND_KEEP_TEMP=1), and saves:
  - the full per-pixel change array (dB) and the drop/rise/undetected masks
  - the two asymmetric threshold values (drop_threshold_db, rise_threshold_db)
  - the decrease-only mask (drop only, ignoring rise) and the bidirectional
    mask (drop | rise), both as boolean .npy arrays for side-by-side extent
    plotting against the reference polygon
  - the same summary numbers (F1/precision/recall for both variants) that
    SCIENCE_LOG.md records, as a cross-check

Usage: python export_fig2_bidirectional.py <event_temp_dir_uuid>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "agents" / "satellite"))

# Same PROJ_LIB fix run_baseline.py applies — a system-wide PostGIS install's
# proj.db shadows rasterio's bundled one and every warp/reproject call fails
# silently (see run_baseline.py's _fix_proj_lib docstring for the full story).
_bundled = (
    HERE.parents[1] / "agents" / "satellite" / "venv" / "Lib" / "site-packages"
    / "rasterio" / "proj_data"
)
if _bundled.is_dir():
    os.environ["PROJ_LIB"] = str(_bundled)
    os.environ["PROJ_DATA"] = str(_bundled)

import numpy as np
import yaml
from shapely.geometry import mapping, shape
from rasterio.enums import Resampling
from rasterio.warp import reproject, transform_geom
from rasterio.features import geometry_mask

import sar_change_detection as scd  # noqa: E402
import aoi_pin  # noqa: E402
import reference_loader  # noqa: E402
from processor import clip_to_polygon, stack_bands  # noqa: E402

OUT_DIR = HERE / "figure_data"
OUT_DIR.mkdir(exist_ok=True)


def _f1(tp, fp, fn):
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f1


def main(event_uuid: str) -> None:
    import os

    temp_root = Path(os.environ.get("TEMP", r"C:\Users\ABDULL~1\AppData\Local\Temp"))
    cache = temp_root / "hazardmind-satellite" / event_uuid
    if not cache.is_dir():
        raise SystemExit(f"Event temp dir not found: {cache}")

    cfg = yaml.safe_load(
        (HERE / "reference_events" / "emsr271_keramidi.yaml").read_text(encoding="utf-8")
    )
    loc = cfg["pipeline_location"]

    with aoi_pin.pinned_aoi():
        from boundary import get_risk_city_boundaries, merge_risk_boundaries

        # Mirrors agent.py's region_for_query fix (the exact "Keramidi,
        # Keramidi, Trikala, Greece" duplicate-token bug documented there):
        # strip the leading region token when it's exactly the single risk
        # city we're about to prepend, so Nominatim gets "Keramidi, Trikala,
        # Greece" instead of a self-duplicated query it cannot resolve.
        city = loc.split(",")[0].strip()
        region_for_query = loc
        rest = ", ".join(p.strip() for p in loc.split(",")[1:]).strip()
        if rest:
            region_for_query = rest

        merged = merge_risk_boundaries(
            get_risk_city_boundaries(region_for_query, [city])
        )

    def clip(scene_dir_name: str):
        vv_path = cache / scene_dir_name / "bands" / "VV.tiff"
        if not vv_path.is_file():
            vv_path = cache / scene_dir_name / "bands" / "VV.tif"
        return clip_to_polygon(
            stack_bands({"VV": str(vv_path)}, "sentinel-1"), merged
        )

    post = clip("t1")
    shp = post["bands"]["VV"].shape

    pre_scenes = sorted(
        d.name for d in cache.iterdir() if d.is_dir() and d.name.startswith("pre_")
    )
    print(f"Found {len(pre_scenes)} pre-event scene(s): {pre_scenes}")

    pres = []
    for d in pre_scenes:
        c = clip(d)
        vv = c["bands"]["VV"]
        if vv.shape != shp:
            dest = np.full(shp, np.nan, dtype="float32")
            reproject(
                source=vv.astype("float32"),
                destination=dest,
                src_transform=c["transform"],
                src_crs=c["crs"],
                dst_transform=post["transform"],
                dst_crs=post["crs"],
                resampling=Resampling.bilinear,
                src_nodata=np.nan,
                dst_nodata=np.nan,
            )
            vv = dest
        pres.append(vv)

    # Run the REAL detector — both-direction, exactly as production does.
    cd = scd.detect_flood_change(
        post["bands"]["VV"], pres,
        dem=None, valid_mask=post.get("mask"), orbit_direction="ASCENDING",
    )
    if cd.get("status") != "complete":
        raise SystemExit(f"detect_flood_change did not complete: {cd}")

    change = cd["change_db"]
    valid = np.isfinite(change)
    if post.get("mask") is not None:
        valid &= post["mask"]

    ref_geom, ref_crs = reference_loader.load_reference_geometry(
        cfg["reference_products"]["sentinel1"]["download_url"],
        cfg["reference_products"]["sentinel1"]["vector_layer_of_record"],
        "emsr271_keramidi",
    )
    ref_clip = ref_geom.intersection(shape(merged))
    rr = shape(transform_geom("EPSG:4326", post["crs"], mapping(ref_clip)))
    inref = geometry_mask([mapping(rr)], out_shape=shp, transform=post["transform"], invert=True)

    # Re-derive threshold-based masks directly (mirrors detect_flood_change's
    # internal drop_flood/rise_flood construction) so drop-only vs
    # bidirectional can be compared as independent extents.
    thr = scd.tiled_threshold(change, valid, direction="both")
    drop_only_mask = (
        valid & (change <= thr["threshold"]) if thr["threshold"] is not None
        else np.zeros_like(valid)
    )
    rise_mask = (
        valid & (change >= thr["rise_threshold"]) if thr["rise_threshold"] is not None
        else np.zeros_like(valid)
    )
    bidirectional_mask = scd.morphological_cleanup(drop_only_mask | rise_mask)
    drop_only_cleaned = scd.morphological_cleanup(drop_only_mask)

    def score(mask):
        tp = int((mask & inref).sum())
        fp = int((mask & ~inref).sum())
        fn = int((inref & valid & ~mask).sum())
        p, r, f1 = _f1(tp, fp, fn)
        return {"tp": tp, "fp": fp, "fn": fn, "precision": round(p, 4),
                "recall": round(r, 4), "f1": round(f1, 4)}

    drop_only_score = score(drop_only_cleaned)
    bidirectional_score = score(bidirectional_mask)

    # Save arrays for the histogram + extent-map figure.
    np.save(OUT_DIR / "keramidi_change_dB.npy", change[valid].astype("float32"))
    np.save(OUT_DIR / "keramidi_drop_mask.npy", drop_only_cleaned)
    np.save(OUT_DIR / "keramidi_rise_mask.npy", rise_mask & valid)
    np.save(OUT_DIR / "keramidi_bidirectional_mask.npy", bidirectional_mask)
    np.save(OUT_DIR / "keramidi_reference_mask.npy", inref)
    np.save(OUT_DIR / "keramidi_valid_mask.npy", valid)

    summary = {
        "event": "Keramidi",
        "drop_threshold_db": thr["threshold"],
        "rise_threshold_db": thr["rise_threshold"],
        "drop_px_count": int((drop_only_cleaned).sum()),
        "rise_px_count": int((rise_mask & valid).sum()),
        "drop_only_score": drop_only_score,
        "bidirectional_score": bidirectional_score,
        "f1_improvement_factor": (
            round(bidirectional_score["f1"] / drop_only_score["f1"], 2)
            if drop_only_score["f1"] > 0 else None
        ),
    }
    (OUT_DIR / "keramidi_bidirectional_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\nSaved arrays to {OUT_DIR}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python export_fig2_bidirectional.py <event_temp_dir_uuid>")
    main(sys.argv[1])
