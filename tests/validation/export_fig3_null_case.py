"""Exports raw per-pixel SAR change-detection data for Fig. 3 (The Null Case)
— the flood-vs-dry backscatter-change distribution overlap plot showing
ROC AUC ~0.487 on a genuinely signal-absent scene.

WHY THIS EXISTS AS A SEPARATE SCRIPT
-------------------------------------
The original analysis (phase0_detectability_ceiling.py, prior session) only
PRINTED summary statistics (Cohen's d, ROC AUC, best-possible F1) — it never
saved the underlying per-pixel arrays, so the histogram this figure needs
could not be regenerated from that script's own output. This script re-runs
the identical analysis (same clip/baseline/log-ratio pipeline, same
flood/dry mask construction) against a FRESH Kanalia run's cached temp files
(kept on disk via HAZARDMIND_KEEP_TEMP=1, see agent.py) and additionally
saves:
  - the two full pixel arrays (flood-pixel dB values, dry-pixel dB values)
    as .npy, for a proper histogram/KDE plot
  - a small .json with the same summary statistics as before, so the
    printed numbers in SCIENCE_LOG.md can be cross-checked against this
    export rather than trusted from memory

Usage: python export_fig3_null_case.py <event_temp_dir_uuid>
  (the UUID printed by run_baseline.py as e.g. "for <uuid>" in its log lines)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import yaml
from shapely.geometry import mapping, shape
from rasterio.enums import Resampling
from rasterio.warp import reproject, transform_geom
from rasterio.features import geometry_mask

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "agents" / "satellite"))

import sar_change_detection as scd  # noqa: E402
import aoi_pin  # noqa: E402
import reference_loader  # noqa: E402
from processor import clip_to_polygon, stack_bands  # noqa: E402

OUT_DIR = HERE / "figure_data"
OUT_DIR.mkdir(exist_ok=True)


def main(event_uuid: str) -> None:
    import os

    temp_root = Path(os.environ.get("TEMP", r"C:\Users\ABDULL~1\AppData\Local\Temp"))
    cache = temp_root / "hazardmind-satellite" / event_uuid
    if not cache.is_dir():
        raise SystemExit(
            f"Event temp dir not found: {cache}\n"
            "Was this run launched with HAZARDMIND_KEEP_TEMP=1?"
        )

    cfg = yaml.safe_load(
        (HERE / "reference_events" / "emsr692_kanalia.yaml").read_text(encoding="utf-8")
    )
    loc = cfg["pipeline_location"]

    with aoi_pin.pinned_aoi():
        from boundary import get_risk_city_boundaries, merge_risk_boundaries

        merged = merge_risk_boundaries(
            get_risk_city_boundaries(loc, [loc.split(",")[0].strip()])
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

    base = scd.build_baseline([scd.refined_lee(s) for s in pres])["baseline"]
    ch = scd.log_ratio(scd.refined_lee(post["bands"]["VV"]), base)

    val = np.isfinite(ch)
    if post.get("mask") is not None:
        val &= post["mask"]

    ref_geom, ref_crs = reference_loader.load_reference_geometry(
        cfg["reference_products"]["sentinel1"]["download_url"],
        cfg["reference_products"]["sentinel1"]["vector_layer_of_record"],
        "emsr692_kanalia",
    )
    ref_clip = ref_geom.intersection(shape(merged))
    rr = shape(transform_geom("EPSG:4326", post["crs"], mapping(ref_clip)))
    inref = geometry_mask(
        [mapping(rr)], out_shape=shp, transform=post["transform"], invert=True
    )

    fl = val & inref
    dry = val & ~inref
    flood_vals = ch[fl]
    dry_vals = ch[dry]

    np.save(OUT_DIR / "kanalia_flood_px_dB.npy", flood_vals)
    np.save(OUT_DIR / "kanalia_dry_px_dB.npy", dry_vals)

    from scipy.stats import rankdata

    allv = np.concatenate([flood_vals, dry_vals])
    r = rankdata(-allv)
    auc = (r[: flood_vals.size].sum() - flood_vals.size * (flood_vals.size + 1) / 2) / (
        flood_vals.size * dry_vals.size
    )
    sep = abs(flood_vals.mean() - dry_vals.mean()) / np.sqrt(
        (flood_vals.std() ** 2 + dry_vals.std() ** 2) / 2
    )

    summary = {
        "event": "Kanalia",
        "flood_px_count": int(flood_vals.size),
        "dry_px_count": int(dry_vals.size),
        "flood_mean_dB": round(float(flood_vals.mean()), 4),
        "flood_median_dB": round(float(np.median(flood_vals)), 4),
        "flood_std_dB": round(float(flood_vals.std()), 4),
        "dry_mean_dB": round(float(dry_vals.mean()), 4),
        "dry_median_dB": round(float(np.median(dry_vals)), 4),
        "dry_std_dB": round(float(dry_vals.std()), 4),
        "cohens_d": round(float(sep), 4),
        "roc_auc": round(float(auc), 4),
        "reference_coverage_pct_of_valid_aoi": round(
            100 * inref[val].mean(), 2
        ),
    }
    (OUT_DIR / "kanalia_null_case_summary.json").write_text(
        json.dumps(summary, indent=2)
    )
    print(json.dumps(summary, indent=2))
    print(f"\nSaved arrays to {OUT_DIR}/kanalia_flood_px_dB.npy, kanalia_dry_px_dB.npy")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python export_fig3_null_case.py <event_temp_dir_uuid>")
    main(sys.argv[1])
