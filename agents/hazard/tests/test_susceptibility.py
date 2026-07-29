"""Landslide susceptibility computed from the DEM — not imported.

The discriminating tests are the ones that show susceptibility is MORE than
slope: two hillsides at the SAME gradient must score differently when one is
a converging hollow (water collects) and the other a diverging spur, and
when one is clay-rich and the other granite. A module that only ranked slope
would pass a naive test and add nothing over the p90-slope figure the hazard
agent already had.
"""
import os
import sys

import numpy as np

_HAZARD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HAZARD)

import susceptibility as su  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


N = 120
yy, xx = np.mgrid[0:N, 0:N].astype("float32")

print("\n=== 1. Slope, aspect and BOTH curvatures are derived ===")
planar = (N - xx) * 10.0          # falls east, constant gradient
g = su.slope_aspect_curvature(planar, pixel_size_m=30.0)
for k in ("slope_deg", "aspect_deg", "plan_curvature", "profile_curvature"):
    check(f"{k} computed", k in g and g[k].shape == (N, N))
check("planar slope is uniform (a plane has one gradient)",
      float(np.std(g["slope_deg"][5:-5, 5:-5])) < 0.5,
      f"std={float(np.std(g['slope_deg'][5:-5, 5:-5])):.3f}")
check("aspect points downslope (east ~90 deg)",
      abs(float(np.mean(g["aspect_deg"][5:-5, 5:-5])) - 90.0) < 15.0,
      f"{float(np.mean(g['aspect_deg'][5:-5, 5:-5])):.1f}")
# On a PERFECT PLANE both curvatures are identically zero, so they compare
# equal there and prove nothing. The distinction only exists on curved
# terrain: plan curvature describes convergence ACROSS the slope, profile
# curvature acceleration ALONG it.
_curved = (N - xx) * 10.0 + ((yy - N / 2) ** 2) * 0.05 + ((xx - N / 2) ** 2) * 0.03
_gc = su.slope_aspect_curvature(_curved, pixel_size_m=30.0)
check("plan and profile curvature are SEPARATE (they answer different "
      "questions on curved terrain)",
      not np.allclose(_gc["plan_curvature"][10:-10, 10:-10],
                      _gc["profile_curvature"][10:-10, 10:-10]))

print("\n=== 2. DISCRIMINATING: same slope, different SHAPE ===")
# A converging hollow vs a diverging spur, both on the same mean gradient.
hollow = (N - xx) * 10.0 + ((yy - N / 2) ** 2) * 0.05    # concave across
spur = (N - xx) * 10.0 - ((yy - N / 2) ** 2) * 0.05      # convex across
r_hollow = su.compute_susceptibility(hollow, pixel_size_m=30.0)
r_spur = su.compute_susceptibility(spur, pixel_size_m=30.0)
check("hollow and spur have comparable slope",
      abs(r_hollow["p90_slope_deg"] - r_spur["p90_slope_deg"]) < 8.0,
      f"{r_hollow['p90_slope_deg']} vs {r_spur['p90_slope_deg']}")
check("but the CONVERGING hollow scores higher susceptibility",
      r_hollow["mean_susceptibility"] > r_spur["mean_susceptibility"],
      f"hollow {r_hollow['mean_susceptibility']} vs spur "
      f"{r_spur['mean_susceptibility']}")
print(f"    slope p90 {r_hollow['p90_slope_deg']} vs {r_spur['p90_slope_deg']} "
      f"deg; susceptibility {r_hollow['mean_susceptibility']} vs "
      f"{r_spur['mean_susceptibility']}")

print("\n=== 3. p90 not mean — one steep valley must not average away ===")
# A district that is mostly flat with ONE steep valley. The valley must span
# more than 10% of the area for a p90 to see it at all — that is the honest
# limit of the statistic, not a trick: p90 catches "a meaningful steep
# minority", not "one pixel". ~17% here (20 of 120 columns).
mostly_flat = np.zeros((N, N), dtype="float32")
_ramp = np.concatenate([np.arange(10), np.arange(10)[::-1]]) * 90.0
mostly_flat[:, 50:70] = _ramp.astype("float32")
r_gorge = su.compute_susceptibility(mostly_flat, pixel_size_m=30.0)
check("p90 slope sees the gorge", r_gorge["p90_slope_deg"] > 20.0,
      f"p90={r_gorge['p90_slope_deg']}")
check("mean slope would have hidden it",
      r_gorge["mean_slope_deg"] < r_gorge["p90_slope_deg"] / 2.0,
      f"mean={r_gorge['mean_slope_deg']} p90={r_gorge['p90_slope_deg']}")
print(f"    mean {r_gorge['mean_slope_deg']} deg vs p90 "
      f"{r_gorge['p90_slope_deg']} deg — the reason p90 is used")

print("\n=== 4. DISCRIMINATING: lithology changes the answer at equal slope ===")
steep = (N - xx) * 25.0
weak = np.full((N, N), 0.9, dtype="float32")    # clay-rich
strong = np.full((N, N), 0.1, dtype="float32")  # granite
r_weak = su.compute_susceptibility(steep, 30.0, lithology_score=weak)
r_strong = su.compute_susceptibility(steep, 30.0, lithology_score=strong)
check("clay-rich scores higher than granite at IDENTICAL slope",
      r_weak["mean_susceptibility"] > r_strong["mean_susceptibility"],
      f"{r_weak['mean_susceptibility']} vs {r_strong['mean_susceptibility']}")
check("lithology is listed as a contributing factor",
      "lithology" in r_weak["factors_used"])
print(f"    clay {r_weak['mean_susceptibility']} vs granite "
      f"{r_strong['mean_susceptibility']} on the same terrain")

print("\n=== 5. Road cuts raise susceptibility (Pakistan mountain roads) ===")
far = np.full((N, N), 1000.0, dtype="float32")
near = np.full((N, N), 20.0, dtype="float32")
r_far = su.compute_susceptibility(steep, 30.0, distance_to_roads_m=far)
r_near = su.compute_susceptibility(steep, 30.0, distance_to_roads_m=near)
check("a slope cut by a nearby road scores higher",
      r_near["mean_susceptibility"] > r_far["mean_susceptibility"],
      f"{r_near['mean_susceptibility']} vs {r_far['mean_susceptibility']}")

print("\n=== 6. Absent factors are declared, and weights redistributed ===")
r_min = su.compute_susceptibility(steep, 30.0)   # no lithology, no roads
check("absent factors listed explicitly",
      set(r_min["factors_absent"]) == {"lithology", "distance_to_roads"},
      str(r_min["factors_absent"]))
check("weights still sum to 1 (partial != deflated)",
      abs(sum(r_min["weights_applied"].values()) - 1.0) < 1e-6,
      str(sum(r_min["weights_applied"].values())))
check("score stays bounded in [0,1]",
      0.0 <= r_min["mean_susceptibility"] <= 1.0)
check("full run also sums to 1",
      abs(sum(su.compute_susceptibility(
          steep, 30.0, lithology_score=weak, distance_to_roads_m=near
      )["weights_applied"].values()) - 1.0) < 1e-6)

print("\n=== 7. Honesty: not LHASA, not fitted, TWI approximated ===")
check("basis says it is NOT imported from LHASA", "NOT imported from LHASA"
      in r_min["basis"])
check("basis admits the weights are unfitted",
      "NOT fitted" in r_min["basis"])
check("and names why (no usable inventory)", "COOLR" in r_min["basis"])
check("TWI is declared an approximation, not routed accumulation",
      "APPROXIMATED" in r_min["twi_caveat"])

print(f"\n{'='*62}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*62}")
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
