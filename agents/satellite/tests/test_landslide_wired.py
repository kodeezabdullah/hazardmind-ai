"""Is the landslide scar detector actually REACHABLE in production?

`landslide_detection.py` passed 8/8 of its own offline tests while having
ZERO callers — dead code. The pipeline only ever fetched pre-event scenes for
Sentinel-1 (`_fetch_pre_event_stack`), so a pre-event NDVI never existed and
the landslide path silently ran the ABSOLUTE NDVI threshold instead, which
cannot separate a fresh scar from terrain that was always bare.

This is the CHANGE 6 failure mode exactly. These checks assert the wiring,
not the algorithm — the algorithm already had tests, and they proved nothing
about production.
"""
import os
import sys

import numpy as np

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


proc = open(os.path.join(_AGENT, "processor.py"), encoding="utf-8").read()
sent = open(os.path.join(_AGENT, "sentinel.py"), encoding="utf-8").read()

print("\n=== 1. The pre-event OPTICAL fetch exists (it did not before) ===")
check("sentinel.search_pre_event_optical is defined",
      "def search_pre_event_optical(" in sent)
import sentinel  # noqa: E402
check("and is importable/callable", callable(sentinel.search_pre_event_optical))
check("processor._fetch_pre_event_ndvi is defined",
      "def _fetch_pre_event_ndvi(" in proc)

print("\n=== 2. It ranks by SEASONAL match, not just recency ===")
# NDVI's annual cycle dwarfs a scar, so a same-month scene a year back beats
# a clear one from the opposite season three months back.
check("seasonal delta is computed", "_seasonal_delta" in sent)
check("and is the PRIMARY sort key",
      's.get("_seasonal_delta_days", 999),' in sent)
check("year wraparound handled", "365 - diff" in sent)

print("\n=== 3. The detector is CALLED from the landslide path ===")
check("detect_landslide_scars imported in processor",
      "from landslide_detection import detect_landslide_scars" in proc)
ci = proc[proc.index("def calculate_indices("):proc.index("def export_png(")]
check("called inside calculate_indices", "detect_landslide_scars(" in ci)
check("gated on disaster == landslide AND a pre-event reference",
      'disaster == "landslide" and pre_event_ndvi is not None' in ci)

print("\n=== 4. pre_event_ndvi is THREADED end to end ===")
for marker, where in [
    ("pre_event_ndvi=pre_ndvi", "process_satellite_imagery -> _finish_success"),
    ("pre_event_ndvi=pre_event_ndvi", "_finish_success -> _render_clip"),
]:
    check(f"threaded: {where}", marker in proc)
n_sig = proc.count("pre_event_ndvi=None,")
check("appears in all 3 signatures (calculate_indices/_render_clip/_finish_success)",
      n_sig >= 3, f"found {n_sig}")

print("\n=== 5. NO silent fallback to the absolute threshold ===")
check("insufficient_reference is logged, not swallowed",
      'ls.get("status") == "insufficient_reference"' in ci)
ld = open(os.path.join(_AGENT, "landslide_detection.py"), encoding="utf-8").read()
check("detector itself refuses without a pre-event scene",
      'return {\n            "status": "insufficient_reference"' in ld
      or '"status": "insufficient_reference"' in ld)
check("and says why absolute thresholding is not substituted",
      "always bare" in ld.replace("\n", " "))

print("\n=== 6. The result is labelled as a DIFFERENCE, not an absolute ===")
check("index_type is NDVI_CHANGE", '"index_type": "NDVI_CHANGE"' in ci)
check("index_units is NDVI_difference", '"index_units": "NDVI_difference"' in ci)
check("audit trail carries the shape thresholds",
      '"landslide_detection": {' in ci and "min_elongation" in ci)
check("thresholds_basis (uncalibrated) is carried, not hidden",
      "thresholds_basis" in ci)

print("\n=== 7. End-to-end: a scar is found, a harvested field is not ===")
from landslide_detection import detect_landslide_scars  # noqa: E402

n = 200
pre = np.full((n, n), 0.75, dtype="float32")   # healthy vegetation
# A hillslope: elevation falls to the east -> aspect ~90 deg, steep.
yy, xx = np.mgrid[0:n, 0:n]
dem = (n - xx).astype("float32") * 7.0

# A REAL scar: runs DOWNSLOPE (east-west here, since the DEM falls east so
# aspect is 90 deg), and TAPERS — wide at the head, narrow at the toe. Both
# properties are required; the filters correctly reject a scar that has
# neither. Building this fixture caught two of my own errors: a north-south
# rectangle was rejected on ORIENTATION (it ran across the slope, not down
# it) and an untapered one on TAPER — which is the filters working, not
# failing.
post_scar = pre.copy()
for _i, _x in enumerate(range(60, 150)):
    _half = max(1, int(12 * (1 - _i / 90.0)))
    post_scar[100 - _half:100 + _half + 1, _x] = 0.10
r_scar = detect_landslide_scars(post_scar, pre, dem=dem, pixel_size_m=10.0)

post_field = pre.copy()
cy, cx = 100, 100
post_field[(yy - cy) ** 2 + (xx - cx) ** 2 <= 30 ** 2] = 0.10  # circular
flat = np.zeros((n, n), dtype="float32")
r_field = detect_landslide_scars(post_field, pre, dem=flat, pixel_size_m=10.0)

check("elongated downslope drop IS detected as a scar",
      r_scar["status"] == "complete" and r_scar["scar_count"] >= 1,
      f"count={r_scar.get('scar_count')}")
check("circular drop on FLAT ground is NOT a scar",
      r_field["scar_count"] == 0, f"count={r_field.get('scar_count')}")
print(f"    scar: {r_scar.get('scar_count')} object(s), "
      f"{r_scar.get('affected_percent')}% | flat field: "
      f"{r_field.get('scar_count')} object(s)")
check("no pre-event scene -> insufficient_reference, no mask",
      detect_landslide_scars(post_scar, None)["status"] == "insufficient_reference")

print("\n=== 8. Each shape filter rejects the RIGHT thing ===")
# Across-slope (north-south while aspect is 90 deg): orientation must reject.
_across = pre.copy()
_across[60:150, 96:106] = 0.10
_r = detect_landslide_scars(_across, pre, dem=dem, pixel_size_m=10.0)
check("across-slope scar rejected on ORIENTATION",
      _r["scar_count"] == 0 and _r["rejected_by"]["orientation"] >= 1,
      str(_r.get("rejected_by")))
# Downslope but a uniform rectangle: taper must reject.
_flatbar = pre.copy()
_flatbar[96:106, 60:150] = 0.10
_r2 = detect_landslide_scars(_flatbar, pre, dem=dem, pixel_size_m=10.0)
check("untapered bar rejected on TAPER",
      _r2["scar_count"] == 0 and _r2["rejected_by"]["taper"] >= 1,
      str(_r2.get("rejected_by")))
check("min detectable scar size is stated, not implied",
      "thresholds_basis" in _r and "COOLR" in _r["thresholds_basis"])

print(f"\n{'='*62}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*62}")
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
