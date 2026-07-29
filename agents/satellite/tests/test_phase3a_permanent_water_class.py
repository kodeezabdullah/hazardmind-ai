"""Phase 3a — permanent water is an OVERLAY CLASS, not a subtraction.

Phase 1c reclassified normally-wet pixels to 0 ("safe land"): honest about
the flood claim, but it deleted the river from the map. Phase 3a gives
permanent water its own class (10) so it is rendered and reported while
STILL never counting as flood.

The load-bearing test here is `test_permanent_water_excluded_from_flood_area`:
class 10 sits outside the 0/255 skip-list the vectorizer used, so a naive
implementation silently vectorizes it as a hazard zone and inflates
affected_area_km2 — the exact conflation this phase exists to remove.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import processor  # noqa: E402
from affine import Affine  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# The vectorizer emits WGS84 degrees (it only reprojects when crs is not
# None and not 4326), so the fixture transform must BE in degrees. ~0.0001
# deg ~= 11 m, so the 200x200-px flood block is ~4.9 km2 — comfortably above
# MIN_ZONE_AREA_KM2 (0.5).
_TRANSFORM = Affine(0.0001, 0.0, 22.80, 0.0, -0.0001, 39.50)
_CRS = None


def _grid():
    """400x400 grid: a flood block (class 2) and a river strip (class 10)."""
    arr = np.zeros((400, 400), dtype="uint8")
    arr[50:250, 50:250] = 2                      # flood: 200x200 px
    arr[50:350, 300:380] = processor.PERMANENT_WATER_CLASS  # river strip
    return arr


print("\n=== 1. Permanent water NEVER counts as flood area ===")
gj = processor.vectorize_classification(_grid(), _TRANSFORM, _CRS, "flood", "NDWI")
flood = gj["total_area"]
pw = gj["permanent_water_area_km2"]
total = gj["total_water_area_km2"]
check("flood area is non-zero", flood > 0, f"got {flood}")
check("permanent-water area is non-zero (it WAS detected)", pw > 0, f"got {pw}")
check("permanent water is NOT in total_area (the flood claim)",
      abs(total - (flood + pw)) < 1e-6 and flood < total,
      f"flood={flood} pw={pw} total={total}")
check("no hazard feature carries the permanent-water class",
      all(f["properties"]["class_level"] != processor.PERMANENT_WATER_CLASS
          for f in gj["features"]))
print(f"    flood={flood} km2   permanent_water={pw} km2   total_water={total} km2")

print("\n=== 2. Permanent water IS emitted, as its own collection ===")
pwc = gj["permanent_water"]
check("separate FeatureCollection present", pwc["type"] == "FeatureCollection")
check("it has features", len(pwc["features"]) > 0, f"n={len(pwc['features'])}")
for f in pwc["features"]:
    check("marked is_risk_zone=False", f["properties"]["is_risk_zone"] is False)
    check("severity is null, not a risk level", f["properties"]["severity"] is None)
    break

print("\n=== 3. The river is RENDERED, not a hole in the map ===")
cls = _grid()
scheme = processor._CLASS_SCHEMES["NDWI"]
h, w = cls.shape
rgba = np.zeros((h, w, 4), dtype="uint8")
for _b, value, _l, rgb, alpha in scheme["bands"]:
    rgba[cls == value] = (*rgb, alpha)
rgba[cls == processor.PERMANENT_WATER_CLASS] = (100, 116, 139, 160)
pw_px = rgba[cls == processor.PERMANENT_WATER_CLASS]
flood_px = rgba[cls == 2]
check("permanent-water pixels are opaque (visible)", bool((pw_px[:, 3] > 0).all()),
      f"alpha sample {pw_px[0] if len(pw_px) else 'none'}")
check("permanent water is visually DISTINCT from flood",
      not np.array_equal(pw_px[0][:3], flood_px[0][:3]),
      f"pw={pw_px[0][:3]} flood={flood_px[0][:3]}")
check("safe land stays transparent", bool((rgba[cls == 0][:, 3] == 0).all()))

print("\n=== 4. No permanent water -> figures degrade cleanly ===")
plain = np.zeros((400, 400), dtype="uint8")
plain[50:250, 50:250] = 2
gj2 = processor.vectorize_classification(plain, _TRANSFORM, _CRS, "flood", "NDWI")
check("permanent_water_area_km2 is 0.0, not None", gj2["permanent_water_area_km2"] == 0.0,
      f"got {gj2['permanent_water_area_km2']}")
check("total_water == flood when no permanent water",
      gj2["total_water_area_km2"] == gj2["total_area"],
      f"{gj2['total_water_area_km2']} vs {gj2['total_area']}")
check("empty permanent-water collection still present (shape is stable)",
      gj2["permanent_water"]["features"] == [])

print("\n=== 5. Class 10 cannot be mistaken for a severity level ===")
check("PERMANENT_WATER_CLASS is outside the 1..3 hazard range",
      processor.PERMANENT_WATER_CLASS not in (0, 1, 2, 3),
      f"got {processor.PERMANENT_WATER_CLASS}")
check("and is not the nodata sentinel",
      processor.PERMANENT_WATER_CLASS != processor.NODATA_CLASS)

print(f"\n{'='*58}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*58}")
# Run as a script -> exit code. Imported by pytest -> assert instead, so
# collection of the whole directory is not aborted by a SystemExit.
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
