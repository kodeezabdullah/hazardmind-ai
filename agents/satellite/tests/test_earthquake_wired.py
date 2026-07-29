"""Is the earthquake damage detector REACHABLE, and did wiring it break flood?

Two risks this guards, both of which were live during the change:

  1. The detector could be built and tested (21/21) yet never called — the
     same dead-code trap landslide_detection.py fell into.
  2. Earthquake needs pre-event VH, but the flood path's pre-event contract
     is a FLAT LIST OF VV ARRAYS that sar_change_detection iterates directly.
     Changing that shape to carry VH would silently break flood. The VH stack
     therefore rides as an ATTRIBUTE on a list subclass, and this asserts the
     flood contract is byte-identical.
"""
import os
import sys

import numpy as np

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT)

import processor as p  # noqa: E402

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
ci = proc[proc.index("def calculate_indices("):proc.index("def export_png(")]

print("\n=== 1. VH is fetched for earthquake, NOT for flood ===")
check("flood stays VV-only (the ~40% bandwidth saving)",
      p._s1_polarizations_for("flood") == ["VV"],
      str(p._s1_polarizations_for("flood")))
check("earthquake fetches VV+VH (the depolarisation signature)",
      p._s1_polarizations_for("earthquake") == ["VV", "VH"],
      str(p._s1_polarizations_for("earthquake")))
check("unknown disaster falls back to VV-only",
      p._s1_polarizations_for("wildfire") == ["VV"])
check("download_imagery uses the per-disaster selector",
      "band_tokens = _s1_polarizations_for(disaster)" in proc)

print("\n=== 2. The detector is CALLED from the earthquake path ===")
check("imported in processor",
      "from earthquake_damage import detect_earthquake_damage" in proc)
check("called inside calculate_indices", "detect_earthquake_damage(" in ci)
check("gated on disaster == earthquake AND a pre-event reference",
      'disaster == "earthquake" and pre_event_vv' in ci)
check("runs BEFORE the flood change-detection branch (they are exclusive)",
      ci.index("detect_earthquake_damage(") < ci.index("detect_flood_change("))

print("\n=== 3. Built-up (IBI) is used as the exposure mask ===")
check("compute_ibi called in the earthquake branch",
      "from built_up import compute_ibi" in ci)
check("its mask is passed as built_up_mask", "built_up_mask=built" in ci)
check("no-exposure / no-built-up statuses are logged, not swallowed",
      '"no_exposure_mask", "no_built_up_in_aoi"' in ci)

print("\n=== 4. THE FLOOD CONTRACT IS UNCHANGED (the real risk) ===")
check("_PreEventStack is a list subclass",
      issubclass(p._PreEventStack, list))
st = p._PreEventStack([np.zeros((2, 2)), np.ones((2, 2))])
st.vh = [np.full((2, 2), 7.0)]
check("iterates exactly like a plain list of VV arrays",
      len(list(st)) == 2 and isinstance(list(st)[0], np.ndarray))
check("indexes like a plain list", st[0].shape == (2, 2))
check("a flood-path reader cannot observe the VH attribute",
      list(st) == [a for a in st] and len(st) == 2)
check("but the earthquake path CAN reach it",
      getattr(st, "vh", None) is not None and st.vh[0][0, 0] == 7.0)
check("a plain list (no VH) degrades safely via getattr",
      getattr([1, 2], "vh", None) is None)
check("call site uses getattr with an empty-list default",
      'getattr(pre_event_vv, "vh", None) or []' in ci)

print("\n=== 5. The result is labelled honestly ===")
check("index_type is SAR_DAMAGE (not NDVI)", '"index_type": "SAR_DAMAGE"' in ci)
check("index_units is a change ratio",
      '"index_units": "dB_change_ratio"' in ci)
check("resolution_limit is carried into the result",
      "resolution_limit" in ci)
check("upgrade_path (InSAR coherence) carried", "upgrade_path" in ci)
check("thresholds_basis (uncalibrated) carried", "thresholds_basis" in ci)

print("\n=== 6. End-to-end through the real detector ===")
from earthquake_damage import detect_earthquake_damage  # noqa: E402

rng = np.random.default_rng(9)
N = 120
built = np.zeros((N, N), bool)
built[10:110, 10:110] = True
dmg = np.zeros((N, N), bool)
dmg[30:70, 30:70] = True
pre_vv = (300 * rng.gamma(4.4, 1 / 4.4, (N, N))).astype("float32")
pre_vh = (60 * rng.gamma(4.4, 1 / 4.4, (N, N))).astype("float32")
post_vv = pre_vv.copy()
post_vh = pre_vh.copy()
post_vv[dmg] *= 0.45   # double-bounce destroyed
post_vh[dmg] *= 2.6    # volume scattering -> depolarisation

r = detect_earthquake_damage(post_vv, pre_vv, post_vh, pre_vh,
                             built_up_mask=built)
check("damage detected with VH present", r["status"] == "complete"
      and r["damaged_area_km2"] > 0, str(r.get("damaged_area_km2")))
check("polarimetric evidence reported as available",
      r["polarimetric_evidence_available"] is True)
r_novh = detect_earthquake_damage(post_vv, pre_vv, None, None,
                                  built_up_mask=built)
check("without VH it still runs but declares the loss",
      r_novh["status"] == "complete"
      and r_novh["polarimetric_evidence_available"] is False)
print(f"    with VH: {r['damaged_area_km2']} km2, evidence {r['evidence_used']}")

print(f"\n{'='*62}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*62}")
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
