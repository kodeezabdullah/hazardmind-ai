"""Driver for STEP 3 (precision vs post-peak latency) and STEP 4 (confidence
vs measured accuracy) across every scored S1 flood event.

Reads the scored event table from persisted evidence only:
  - metrics + confidence   <- tests/validation/results/<commit>.json
  - scene_id               <- Postgres satellite_results (the durable row)
  - acquisition instant    <- CDSE OData catalogue, keyed on that scene_id
  - event peak + layer     <- tests/validation/reference_events/<event>.yaml

Any event whose latency cannot be derived that way is EXCLUDED from the
correlation and listed separately, rather than entering it with an estimate.

Usage:  python run_latency_analysis.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from latency_analysis import (  # noqa: E402
    acquisition_datetime_from_cdse,
    post_peak_latency_days,
    report_correlation,
)

# Every S1 flood run scored to date, newest last. event_id is the pipeline's
# own UUID (the DB key); results_file is the versioned harness output.
# NOTE: Kosutarica's 795b070-dirty.run2 run is deliberately NOT listed as a
# scored event — see EXCLUDED_RUNS below.
SCORED_RUNS = [
    ("Kanalia", "emsr692_kanalia", "7f82f748-ddae-4d5d-869e-54d2dd094a21", "95aa554.json"),
    ("Keramidi", "emsr271_keramidi", None, None),  # scored from cached scenes
    ("Tychero", "emsr277_tychero", "e5619ac8-1130-49e8-867f-7e37043b31d9", "987983d.json"),
    ("Zalgiriai", "emsr267_zalgiriai", "aeb381ca-9673-4261-83a1-5665fb294a50", "49cf867.json"),
]

# Runs that produced a number but must NOT be treated as detector measurements.
EXCLUDED_RUNS = [
    (
        "Kosutarica",
        "emsr275_kosutarica",
        "0c963eda-3168-462e-b309-d66176121cf7",
        "scipy absent from the venv -> sar_change_detection raised ImportError "
        "-> processor fell through to the uncalibrated absolute-threshold path "
        "(index_units 'dB_uncalibrated', index_calibrated False). The zero "
        "measures a missing dependency, not the imagery. Fixed in d5124a6; "
        "needs a re-run to become a real data point.",
    ),
]

# Keramidi was scored directly on cached scenes (a strict A/B where only
# `direction` varied), so it has no harness results file and no DB row. Its
# numbers and acquisition date come from SCIENCE_LOG's Phase 0 re-measurement.
KERAMIDI = {
    "iou": 0.1684,
    "precision": 0.5858,
    "recall": 0.1911,
    "f1": 0.2882,
    "confidence": None,  # measured outside the pipeline; no tracker output
    "confidence_basis": None,
    "acquired": "2018-02-28T04:30:11Z",
    "rise_px": 43048,
    "drop_px": 2500,
}


def _load_cfg(event_key: str) -> dict:
    return yaml.safe_load((HERE / "reference_events" / f"{event_key}.yaml").read_text(encoding="utf-8"))


def _load_result(fname: str) -> dict:
    return json.loads((HERE / "results" / fname).read_text(encoding="utf-8"))["events"][0]


async def _scene_ids(event_ids: list[str]) -> dict[str, str]:
    load_dotenv(REPO / "backend" / ".env", override=False)
    import asyncpg

    conn = await asyncpg.connect(os.getenv("NEON_DATABASE_URL"))
    try:
        rows = await conn.fetch(
            "select event_id, scene_id, index_units, index_calibrated "
            "from satellite_results where event_id = any($1::uuid[])",
            event_ids,
        )
        return {str(r["event_id"]): dict(r) for r in rows}
    finally:
        await conn.close()


def main() -> None:
    ids = [e for _, _, e, _ in SCORED_RUNS if e]
    db = asyncio.run(_scene_ids(ids))

    rows = []
    unresolved = []
    for name, key, event_id, fname in SCORED_RUNS:
        cfg = _load_cfg(key)
        peak = cfg["event_peak_date"]
        layer = cfg["reference_products"]["sentinel1"]["vector_layer_of_record"]

        if name == "Keramidi":
            k = KERAMIDI
            acq = k["acquired"]
            m = {x: k[x] for x in ("iou", "precision", "recall", "f1")}
            conf, basis = k["confidence"], k["confidence_basis"]
            units, calib = "dB_change_ratio", True
        else:
            ev = _load_result(fname)
            m = ev.get("metrics_excluding_permanent_water") or ev.get(
                "metrics_including_permanent_water"
            )
            conf = ev.get("reported_confidence")
            basis = ev.get("confidence_basis")
            row = db.get(event_id, {})
            units, calib = row.get("index_units"), row.get("index_calibrated")
            sid = row.get("scene_id")
            if not sid:
                unresolved.append((name, "no scene_id persisted"))
                continue
            dt = acquisition_datetime_from_cdse(sid)
            if dt is None:
                unresolved.append((name, f"CDSE could not resolve {sid}"))
                continue
            acq = dt.isoformat()

        from latency_analysis import _parse_utc

        lat = post_peak_latency_days(peak, _parse_utc(acq))
        rows.append(
            {
                "event": name,
                "latency": lat,
                "iou": (m or {}).get("iou"),
                "precision": (m or {}).get("precision"),
                "recall": (m or {}).get("recall"),
                "f1": (m or {}).get("f1"),
                "conf": conf,
                "basis": basis,
                "layer": layer,
                "units": units,
                "calibrated": calib,
                "acquired": acq[:10],
                "peak": peak,
            }
        )

    rows.sort(key=lambda r: r["latency"])

    print("=" * 100)
    print("SCORED S1 FLOOD EVENTS — latency derived from persisted scene_id via CDSE")
    print("=" * 100)
    hdr = f"{'event':11s} {'peak':11s} {'acq':11s} {'lat_d':>6s} {'IoU':>7s} {'prec':>7s} {'rec':>7s} {'F1':>7s} {'conf':>6s}  layer"
    print(hdr)
    print("-" * 100)
    for r in rows:
        def f(v, w=7, p=4):
            return f"{v:{w}.{p}f}" if isinstance(v, (int, float)) else f"{'n/a':>{w}s}"
        print(
            f"{r['event']:11s} {r['peak']:11s} {r['acquired']:11s} {r['latency']:6.2f} "
            f"{f(r['iou'])} {f(r['precision'])} {f(r['recall'])} {f(r['f1'])} "
            f"{f(r['conf'],6,3)}  {r['layer']}"
        )

    print()
    print("PROVENANCE CHECK (a run that used the fallback path is not a measurement):")
    for r in rows:
        ok = "OK " if r["calibrated"] else "!! "
        print(f"  {ok}{r['event']:11s} index_units={r['units']} calibrated={r['calibrated']}")

    if EXCLUDED_RUNS:
        print()
        print("EXCLUDED FROM THE SCORED SET:")
        for name, _key, _eid, why in EXCLUDED_RUNS:
            print(f"  {name}: {why}")

    if unresolved:
        print()
        print("LATENCY NOT DERIVABLE (excluded from correlations):")
        for name, why in unresolved:
            print(f"  {name}: {why}")

    # ---------------- STEP 3 ----------------
    print()
    print("=" * 100)
    print("STEP 3 — does precision/recall/F1 vary with post-peak latency?")
    print("=" * 100)
    usable = [r for r in rows if r["precision"] is not None]
    lat = [r["latency"] for r in usable]
    for metric in ("precision", "recall", "f1", "iou"):
        print(report_correlation(f"latency vs {metric}", lat, [r[metric] for r in usable]))

    print()
    print("  Reference-layer semantics per event (the confound to check):")
    for r in usable:
        print(f"    {r['event']:11s} {r['layer']}")

    # ---------------- STEP 4 ----------------
    print()
    print("=" * 100)
    print("STEP 4 — does reported confidence track measured accuracy?")
    print("=" * 100)
    withconf = [r for r in usable if isinstance(r["conf"], (int, float))]
    for r in withconf:
        print(f"  {r['event']:11s} F1={r['f1']:.4f}  confidence={r['conf']:.3f}  basis={r['basis']}")
    if len(withconf) >= 3:
        print()
        print(report_correlation("confidence vs F1", [r["conf"] for r in withconf], [r["f1"] for r in withconf]))
    else:
        print(f"\n  n={len(withconf)} — too few points to report a correlation.")


if __name__ == "__main__":
    main()
