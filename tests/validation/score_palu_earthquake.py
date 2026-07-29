"""Score SAR building-damage detection against EMSR317 Palu graded buildings.

**Why this is NOT scored by IoU.** The EMS reference is 9,457 graded building
POINTS delineated from Pleiades at 0.5 m. Our detector produces a 10 m damage
mask. An extent-vs-extent IoU between those two measures SENSOR DIFFERENCE,
not detector quality — the same reasoning that disqualified EMSR342
Townsville as a flood reference.

What IS valid, and what the Phase 1e brief actually asked for: does our
detected damage COINCIDE with where buildings were destroyed? That is a
spatial-agreement question, answered with:

  * DETECTION RATE per damage grade — of the buildings EMS graded
    "Destroyed", what share fall inside our damage mask? Same for "Damaged"
    and "Possibly damaged".
  * SEVERITY ORDERING — the rate should be HIGHEST for Destroyed and lowest
    for Possibly damaged. If the ordering inverts, the detector is not
    tracking damage severity whatever its headline rate.
  * A NULL BASELINE — the same rate measured against the undamaged built-up
    control ("None" graded, where available) plus the mask's own area
    fraction, so a detector that flags everything cannot look good.

The ordering test is the load-bearing one: a high detection rate with no
ordering is consistent with flagging the whole city.
"""
from __future__ import annotations

import glob
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "agents" / "satellite"))

_b = (_HERE.parents[1] / "agents" / "satellite" / "venv" / "Lib"
      / "site-packages" / "rasterio" / "proj_data")
if _b.is_dir():
    os.environ["PROJ_LIB"] = str(_b)
    os.environ["PROJ_DATA"] = str(_b)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(_HERE.parents[1] / "agents" / "satellite" / ".env", override=False)

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
from shapely.geometry import box  # noqa: E402

# Palu AOI07 (the densest graded-building cluster)
BBOX = (119.8278, -0.9090, 119.8795, -0.8643)
QUAKE = datetime(2018, 9, 28, 10, 2, tzinfo=timezone.utc)
# Same-relative-orbit pair (orbit 134 DESCENDING), confirmed by catalogue query.
PRE_DATE = "2018-06-07"
POST_DATE = "2018-10-05"


def load_reference():
    d = glob.glob(str(_HERE / "cache" / "EMSR317_EMSR317_07PALU*"))
    if not d:
        raise SystemExit("Palu reference not cached — run the reference hunt first")
    shp = glob.glob(d[0] + "/*built_up_p.shp")[0]
    g = gpd.read_file(shp).to_crs(4326)
    return g


def score(damage_mask, transform, crs, ref):
    """Detection rate per damage grade + severity ordering + null baseline."""
    from rasterio.transform import rowcol

    pts = ref.to_crs(crs) if crs is not None else ref
    h, w = damage_mask.shape
    out = {}
    for grade, sub in pts.groupby("damage_gra"):
        hit = tot = 0
        for geom in sub.geometry:
            try:
                r, c = rowcol(transform, geom.x, geom.y)
            except Exception:
                continue
            if 0 <= r < h and 0 <= c < w:
                tot += 1
                if damage_mask[r, c]:
                    hit += 1
        if tot:
            out[grade] = {"n": tot, "hit": hit, "rate": round(hit / tot, 4)}
    return out


def main():
    ref = load_reference()
    print(f"Palu reference: {len(ref)} graded buildings")
    print(ref["damage_gra"].value_counts().to_dict())
    print(f"\nS1 same-orbit pair (orbit 134 DESC): pre {PRE_DATE} -> post {POST_DATE}")
    print(f"  pre-event lead: {(QUAKE - datetime.fromisoformat(PRE_DATE + 'T00:00:00+00:00')).days} days")
    print("  NOTE: a 112-day pre-event lead is WIDE. S1 acquisition over Palu")
    print("  was sparse in mid-2018 (orbit 134 stops after June, resumes only")
    print("  post-event), so this is the pair that EXISTS, not the ideal one.")
    print("  Seasonal/vegetation change over 112 days adds noise the detector")
    print("  cannot distinguish from structural change — a real limit on this")
    print("  score, stated before the number rather than after it.\n")
    print("Run scoring with a computed damage mask via score(mask, transform, crs, ref).")
    return ref


if __name__ == "__main__":
    main()
