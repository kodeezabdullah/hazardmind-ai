"""Phase 4 — IBI built-up layer (Xu 2008), offline.

The discriminating test is `test_bare_soil_not_built_up`: NDBI alone
misclassifies dry bare soil as built-up, which in semi-arid Pakistan is most
of the landscape. IBI's whole purpose is removing that error, so a test that
only checked "concrete scores high" would pass for NDBI too and prove
nothing.
"""
import os
import re
import sys

import numpy as np

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT_DIR)

import built_up  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def scene(n=100):
    """Three surfaces with physically representative S2 reflectances.

    Values are surface-reflectance-scaled (L2A-like), chosen so each class
    has the documented spectral relationship rather than arbitrary numbers:
      built-up  : moderate green, high SWIR > NIR       (NDBI > 0)
      bare soil : high SWIR > NIR too (the NDBI trap), but ALSO higher red
                  and low vegetation -> SAVI small, MNDWI negative
      vegetation: NIR >> red, NIR > SWIR                (NDBI < 0)
    """
    b03 = np.zeros((n, n), "float32")
    b04 = np.zeros((n, n), "float32")
    b08 = np.zeros((n, n), "float32")
    b11 = np.zeros((n, n), "float32")
    # rows 0-32 built-up (concrete/asphalt)
    b03[:33], b04[:33], b08[:33], b11[:33] = 0.12, 0.15, 0.20, 0.30
    # rows 33-65 BARE SOIL — the NDBI false positive
    b03[33:66], b04[33:66], b08[33:66], b11[33:66] = 0.18, 0.26, 0.32, 0.42
    # rows 66-99 vegetation
    b03[66:], b04[66:], b08[66:], b11[66:] = 0.06, 0.04, 0.45, 0.22
    return {"B03": b03, "B04": b04, "B08": b08, "B11": b11}


BUILT, SOIL, VEG = slice(0, 33), slice(33, 66), slice(66, 100)

print("\n=== 1. IBI computes and separates the classes ===")
r = built_up.compute_ibi(scene())
check("returns a result", r is not None)
ibi = r["ibi"]
mb, ms, mv = (float(np.nanmean(ibi[s])) for s in (BUILT, SOIL, VEG))
print(f"    mean IBI  built-up={mb:+.4f}  bare soil={ms:+.4f}  vegetation={mv:+.4f}")
check("built-up scores highest", mb > ms and mb > mv, f"{mb} vs {ms}/{mv}")

# The CLASSIFICATION, not the raw ratio, is the operational output. Raw IBI
# is unstable where NDBI and the SAVI/MNDWI mixture share a sign — measured:
# vegetation returns IBI +1.1564 from two negatives (NDBI -0.3433 / denom
# -0.3184), which would score vegetation as built-up. The guard in
# compute_ibi rejects it on physics (built-up requires SWIR > NIR, NDBI > 0),
# so this asserts the mask each class actually lands in.
m = r["built_up_mask"]
frac = {n: float(m[s].mean()) for n, s in
        (("built", BUILT), ("soil", SOIL), ("veg", VEG))}
print(f"    masked as built-up: built={frac['built']:.0%} "
      f"soil={frac['soil']:.0%} veg={frac['veg']:.0%}")
check("built-up surface IS masked built-up", frac["built"] > 0.9, str(frac))
check("bare soil is NOT masked built-up", frac["soil"] < 0.05, str(frac))
check("vegetation is NOT masked built-up (the ratio-instability guard)",
      frac["veg"] < 0.05, str(frac))

print("\n=== 2. THE DISCRIMINATING CASE: bare soil is NOT built-up ===")
# NDBI alone would call bare soil built-up. Show NDBI does, and IBI does not.
b = scene()
ndbi = built_up._safe_ratio(b["B11"] - b["B08"], b["B11"] + b["B08"])
ndbi_soil = float(np.nanmean(ndbi[SOIL]))
check("NDBI alone WOULD misclassify bare soil (NDBI > 0)", ndbi_soil > 0,
      f"NDBI over soil = {ndbi_soil:+.4f}")
check("IBI correctly rejects bare soil (below threshold)",
      ms <= r["threshold"], f"IBI over soil = {ms:+.4f} vs thr {r['threshold']}")
check("IBI accepts real built-up", mb > r["threshold"], f"{mb:+.4f}")
print(f"    NDBI over bare soil = {ndbi_soil:+.4f} (would be 'built-up'); "
      f"IBI = {ms:+.4f} (correctly not)")

print("\n=== 3. Missing B04 -> None, NOT a substituted index ===")
no_red = {k: v for k, v in scene().items() if k != "B04"}
check("returns None when B04 absent", built_up.compute_ibi(no_red) is None)
src = open(os.path.join(_AGENT_DIR, "built_up.py"), encoding="utf-8").read()
check("NDBI is never returned as a fallback under the IBI name",
      "return None" in src and "NDBI is deliberately NOT" in src.replace("\n", " ")
      or "NDBI alone is deliberately NOT used" in re.sub(r"\s+", " ", src))

print("\n=== 4. Flood-over-built-up overlap ===")
flood = np.zeros((100, 100), bool)
flood[20:50, :] = True          # spans built-up rows 20-32 and soil 33-49
ov = built_up.flood_builtup_overlap(flood, r["built_up_mask"], 0.0001)
check("reports availability", ov["available"] is True)
check("some flood falls on built-up", ov["flood_over_built_up_km2"] > 0)
check("NOT all of it (the soil half is excluded)",
      ov["flood_over_built_up_percent"] < 100.0,
      f"{ov['flood_over_built_up_percent']}%")
print(f"    flood over built-up: {ov['flood_over_built_up_km2']} km2 "
      f"({ov['flood_over_built_up_percent']}% of flood); "
      f"built-up total {ov['built_up_area_km2']} km2")
check("degrades cleanly with no masks",
      built_up.flood_builtup_overlap(None, None, 0.0001)["available"] is False)

print("\n=== 5. Audit trail names the formula and constants ===")
check("formula recorded", "Xu 2008" in r["formula"])
check("SAVI L recorded", r["savi_l"] == 0.5)
check("threshold recorded", r["threshold"] == built_up.IBI_BUILTUP_THRESHOLD)
check("component means recorded", all(
    r["components"][k] is not None for k in ("ndbi_mean", "savi_mean", "mndwi_mean")))

print("\n=== 6. Survival: fields reach _render_clip and agent.py ===")
proc = open(os.path.join(_AGENT_DIR, "processor.py"), encoding="utf-8").read()
agent = open(os.path.join(_AGENT_DIR, "agent.py"), encoding="utf-8").read()
rc = proc[proc.index("def _render_clip("):proc.index("def _render_per_city(")]
for f in ("built_up_available", "built_up_percent", "flood_over_built_up_km2"):
    check(f"{f} carried through _render_clip",
          f'"{f}": indices.get("{f}")' in rc)
    check(f"{f} persisted in agent.py",
          f'"{f}": result.get("{f}")' in agent)
check("B04 is in the flood band set (IBI needs red for SAVI)",
      '"flood": ["B03", "B04", "B08", "B11", "TCI", "SCL"]' in proc)

print(f"\n{'='*60}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*60}")
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
