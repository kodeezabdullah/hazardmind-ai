"""Earthquake BUILDING-DAMAGE detection from SAR (offline).

The discriminating test is `collapse vs brightness change`: an intensity
change alone must NOT be called damage, because intensity moves for many
non-damage reasons (soil moisture, a wet roof, a different look). What marks
collapse specifically is the shift in SCATTERING MECHANISM — double-bounce
(standing walls) toward volume scattering (rubble), which depolarises and
raises VH relative to VV.

A test that only checked "big change -> damage" would pass for a naive
intensity detector and prove nothing.
"""
import os
import sys

import numpy as np

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT)

import earthquake_damage as eq  # noqa: E402

PASS = FAIL = 0
rng = np.random.default_rng(5)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


N = 160
BUILT = np.zeros((N, N), bool)
BUILT[20:140, 20:140] = True          # the built-up district
DAMAGED = np.zeros((N, N), bool)
DAMAGED[40:90, 40:90] = True          # the collapsed part of it


def speckled(mean, shape=(N, N), seed_scale=1.0):
    return (mean * rng.gamma(4.4, 1 / 4.4, size=shape) * seed_scale).astype("float32")


def build_scene():
    """Pre/post VV+VH where ONLY the damaged block changes mechanism."""
    pre_vv = speckled(300.0)
    pre_vh = speckled(60.0)            # low VH: double-bounce, VV-dominant
    post_vv = pre_vv * rng.normal(1.0, 0.05, (N, N)).astype("float32")
    post_vh = pre_vh * rng.normal(1.0, 0.05, (N, N)).astype("float32")
    # Collapse: VV falls (corner reflector destroyed), VH RISES (volume
    # scattering from rubble depolarises) -> VH/VV rises sharply.
    post_vv[DAMAGED] = (pre_vv[DAMAGED] * 0.45).astype("float32")
    post_vh[DAMAGED] = (pre_vh[DAMAGED] * 2.6).astype("float32")
    return pre_vv, pre_vh, post_vv, post_vh


print("\n=== 1. Damage is detected where buildings collapsed ===")
pre_vv, pre_vh, post_vv, post_vh = build_scene()
r = eq.detect_earthquake_damage(post_vv, pre_vv, post_vh, pre_vh,
                                built_up_mask=BUILT)
check("status complete", r["status"] == "complete", r.get("status"))
m = r["damage_mask"]
recall = float(m[DAMAGED].mean())
undamaged_built = BUILT & ~DAMAGED
fpr = float(m[undamaged_built].mean())
check("most of the collapsed block is flagged", recall > 0.7, f"{recall:.2%}")
check("intact built-up is mostly NOT flagged", fpr < 0.15, f"{fpr:.2%}")
check("polarimetric evidence was available",
      r["polarimetric_evidence_available"] is True)
print(f"    recall over collapsed {recall:.1%}, false-positive over intact "
      f"built-up {fpr:.1%}, area {r['damaged_area_km2']} km2")

print("\n=== 2. DISCRIMINATING: brightness change alone is NOT damage ===")
# Everything in the district gets uniformly brighter (e.g. a wet surface):
# intensity moves, but the SCATTERING MECHANISM does not — VH/VV is unchanged
# and the local texture pattern survives.
pre2_vv, pre2_vh, _, _ = build_scene()
post2_vv = (pre2_vv * 2.2).astype("float32")   # +3.4 dB everywhere
post2_vh = (pre2_vh * 2.2).astype("float32")   # SAME factor -> ratio unchanged
r2 = eq.detect_earthquake_damage(post2_vv, pre2_vv, post2_vh, pre2_vh,
                                 built_up_mask=BUILT)
flagged = float(r2["damage_mask"][BUILT].mean())
check("uniform brightening is NOT called damage", flagged < 0.15, f"{flagged:.2%}")
print(f"    +3.4 dB everywhere, ratio unchanged -> {flagged:.1%} flagged "
      "(a naive intensity detector would flag ~100%)")

print("\n=== 3. Built-up mask is REQUIRED, not optional ===")
r3 = eq.detect_earthquake_damage(post_vv, pre_vv, post_vh, pre_vh,
                                 built_up_mask=None)
check("no mask -> no_exposure_mask, no damage mask",
      r3["status"] == "no_exposure_mask" and r3["damage_mask"] is None)
check("and it says why farmland must not be scored",
      "irrigation" in r3["reason"])
empty = np.zeros((N, N), bool)
r4 = eq.detect_earthquake_damage(post_vv, pre_vv, post_vh, pre_vh,
                                 built_up_mask=empty)
check("no built-up in AOI -> honest status, not a zero",
      r4["status"] == "no_built_up_in_aoi")

print("\n=== 4. VH absent -> degrades, and SAYS it is less specific ===")
r5 = eq.detect_earthquake_damage(post_vv, pre_vv, None, None, built_up_mask=BUILT)
check("still runs without VH", r5["status"] == "complete")
check("but declares polarimetric evidence unavailable",
      r5["polarimetric_evidence_available"] is False)
check("evidence list omits depolarisation",
      "vh_vv_depolarisation" not in r5["evidence_used"])
print(f"    evidence without VH: {r5['evidence_used']}")

print("\n=== 5. Combination requires AGREEMENT, not one indicator ===")
check("rule is stated in the result", ">=2 of 4" in r["combination_rule"])
check("all four indicators present with VH", len(r["evidence_used"]) == 4,
      str(r["evidence_used"]))

print("\n=== 6. The resolution limit is in EVERY result, not just the docs ===")
check("resolution_limit present", "resolution_limit" in r)
check("it denies per-building claims",
      "NOT per-structure" in r["resolution_limit"])
check("upgrade path (InSAR coherence) recorded",
      "coherence" in r["upgrade_path"] and "ARIA" in r["upgrade_path"])
check("thresholds_basis admits it is uncalibrated",
      "NOT calibrated" in r["thresholds_basis"])
check("and names why xBD cannot score it",
      "xBD" in r["thresholds_basis"])

print("\n=== 7. Correlation behaves as Matsuoka-Yamazaki expects ===")
corr = r["local_correlation"]
c_dmg = float(np.nanmean(corr[DAMAGED]))
c_int = float(np.nanmean(corr[undamaged_built]))
check("correlation is LOWER over collapsed than intact", c_dmg < c_int,
      f"damaged {c_dmg:.3f} vs intact {c_int:.3f}")
print(f"    local correlation: collapsed {c_dmg:.3f}, intact {c_int:.3f}")

print("\n=== 8. NDVI_QUAKE is not the method (buildings, not vegetation) ===")
src = open(os.path.join(_AGENT, "earthquake_damage.py"), encoding="utf-8").read()
check("module states NDVI is the wrong signal",
      "Buildings collapse; vegetation does not change" in src.replace("\n", " "))
check("USGS is a trigger, ShakeMap/PAGER not consumed",
      "TRIGGER" in src and "not used" in src)

print(f"\n{'='*62}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*62}")
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
