"""Phase 0 offline verification: bidirectional SAR change detection.

Tests the property the naive `abs(ratio) > 3.0` fix would NOT have: that the
tiled KI estimator itself sees the rise mode, not just the final comparison.
"""
import sys, os
sys.path.insert(0, os.path.abspath("d:/hazardmind-ai/agents/satellite"))
import numpy as np
import sar_change_detection as scd

rng = np.random.default_rng(42)
PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {detail}")

def speckled(shape, mean):
    """Multiplicative speckle around a mean linear intensity."""
    return (mean * rng.gamma(shape=4.4, scale=1/4.4, size=shape)).astype("float32")

print("\n=== 1. Flooded vegetation (RISE) is now detected ===")
# Pre: uniform farmland. Post: a block rises ~6 dB (double-bounce).
pre = [speckled((600, 600), 200.0) for _ in range(3)]
post = speckled((600, 600), 200.0)
post[150:400, 150:400] = speckled((250, 250), 200.0 * 10**(6.0/10))

r_both = scd.detect_flood_change(post, pre, direction="both")
r_drop = scd.detect_flood_change(post, pre, direction="drop")
check("both-direction detects the rise block",
      r_both["water_percent"] > 5.0, f"got {r_both['water_percent']}%")
check("drop-only is BLIND to it (the bug)",
      r_drop["water_percent"] < 1.0, f"got {r_drop['water_percent']}%")
check("attributed to flooded-vegetation rise",
      (r_both["flooded_vegetation_rise_percent"] or 0) > 90.0,
      f"rise%={r_both['flooded_vegetation_rise_percent']}")
print(f"    both={r_both['water_percent']}%  drop-only={r_drop['water_percent']}%")
print(f"    rise_threshold_db={r_both['rise_threshold_db']} "
      f"method={r_both['rise_threshold_method']}")

print("\n=== 2. Open water (DROP) still detected — no regression ===")
pre2 = [speckled((600, 600), 200.0) for _ in range(3)]
post2 = speckled((600, 600), 200.0)
post2[150:400, 150:400] = speckled((250, 250), 200.0 * 10**(-8.0/10))
r2_both = scd.detect_flood_change(post2, pre2, direction="both")
r2_drop = scd.detect_flood_change(post2, pre2, direction="drop")
check("both-direction detects open water", r2_both["water_percent"] > 5.0,
      f"got {r2_both['water_percent']}%")
check("drop-only detects it too (unchanged behaviour)",
      r2_drop["water_percent"] > 5.0, f"got {r2_drop['water_percent']}%")
check("attributed to open-water drop",
      (r2_both["open_water_drop_percent"] or 0) > 90.0,
      f"drop%={r2_both['open_water_drop_percent']}")
print(f"    both={r2_both['water_percent']}%  drop-only={r2_drop['water_percent']}%")

print("\n=== 3. No change -> no phantom flood (the guard that matters) ===")
pre3 = [speckled((600, 600), 200.0) for _ in range(3)]
post3 = speckled((600, 600), 200.0)
r3 = scd.detect_flood_change(post3, pre3, direction="both")
check("unchanged scene stays near-zero", r3["water_percent"] < 2.0,
      f"got {r3['water_percent']}%")
print(f"    water_percent={r3['water_percent']}%")

print("\n=== 4. Calibration cancellation still holds (bit-identical) ===")
k = 7.3
ra = scd.detect_flood_change(post, pre, direction="both")
rb = scd.detect_flood_change(post * k, [p * k for p in pre], direction="both")
check("arbitrary calibration factor k=7.3 gives identical mask",
      np.array_equal(ra["flood_mask"], rb["flood_mask"]))

print("\n=== 5. Tiled KI sees BOTH modes (what abs() would have missed) ===")
# Mixed scene: one region drops, another rises.
pre5 = [speckled((800, 800), 200.0) for _ in range(3)]
post5 = speckled((800, 800), 200.0)
post5[100:350, 100:350] = speckled((250, 250), 200.0 * 10**(-8.0/10))
post5[450:700, 450:700] = speckled((250, 250), 200.0 * 10**(6.0/10))
valid5 = np.ones((800, 800), dtype=bool)
change5 = scd.log_ratio(scd.refined_lee(post5),
                        scd.build_baseline([scd.refined_lee(s) for s in pre5])["baseline"])
thr5 = scd.tiled_threshold(change5, valid5, direction="both")
check("KI found bimodal tiles in the DROP direction", thr5["bimodal_tiles"] > 0,
      f"got {thr5['bimodal_tiles']}")
check("KI found bimodal tiles in the RISE direction", thr5["rise_bimodal_tiles"] > 0,
      f"got {thr5['rise_bimodal_tiles']}")
check("the two thresholds are independently estimated, not mirrored",
      thr5["threshold"] is not None and thr5["rise_threshold"] is not None
      and abs(abs(thr5["threshold"]) - thr5["rise_threshold"]) > 0.01,
      f"drop={thr5['threshold']} rise={thr5['rise_threshold']}")
print(f"    drop_thr={thr5['threshold']} ({thr5['bimodal_tiles']} tiles)  "
      f"rise_thr={thr5['rise_threshold']} ({thr5['rise_bimodal_tiles']} tiles)")
r5 = scd.detect_flood_change(post5, pre5, direction="both")
print(f"    mixed scene: drop={r5['open_water_drop_percent']}% "
      f"rise={r5['flooded_vegetation_rise_percent']}% of detections")

print("\n=== 6. insufficient_reference unchanged ===")
r6 = scd.detect_flood_change(post, [], direction="both")
check("empty stack still refuses, no absolute fallback",
      r6["status"] == "insufficient_reference")

print(f"\n{'='*55}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*55}")
# Run as a script -> exit code. Imported by pytest -> assert instead, so
# collection of the whole directory is not aborted by a SystemExit.
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
