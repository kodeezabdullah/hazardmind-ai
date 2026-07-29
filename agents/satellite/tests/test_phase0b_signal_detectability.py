"""Signal-detectability guard — do not ship a worse-than-chance flood map.

The measured problem (2026-07-29, Kanalia forced-S1): the change image inside
the CONFIRMED EMS flood extent was statistically indistinguishable from dry
ground (Cohen's d 0.031, ROC AUC 0.487 — below chance). Every threshold had a
precision lift below 1.0x, i.e. worse than labelling the whole AOI flooded.
The pipeline shipped that map anyway, with nothing to say the scene carried
no signal.

TWO earlier versions of this guard were measured and DISCARDED — both are
regression-tested here so neither can come back:

  1. detected-vs-undetected Cohen's d — CIRCULAR (the detector defines the
     two groups by thresholding the compared values). Returned d=4.18 on the
     no-signal scene and passed it.
  2. KI bimodal-tile fraction — the no-signal scene scored 0.75, HIGHER than
     a real flood's 0.25. KI always splits a tile somewhere, so it reports
     "bimodal" on smooth noise.

What survived: deep-tail mass, the one quantity a real flood must produce and
noise cannot fake.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sar_change_detection as scd  # noqa: E402

PASS = FAIL = 0
rng = np.random.default_rng(11)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def speckled(shape, mean):
    return (mean * rng.gamma(shape=4.4, scale=1 / 4.4, size=shape)).astype("float32")


print("\n=== 1. A real flood is NOT flagged (no false alarm) ===")
pre = [speckled((600, 600), 200.0) for _ in range(3)]
post = speckled((600, 600), 200.0)
post[150:400, 150:400] = speckled((250, 250), 200.0 * 10 ** (-8.0 / 10))
real = scd.detect_flood_change(post, pre, direction="both")
check("signal_detectable is True", real["signal_detectable"] is True,
      f"got {real['signal_detectable']}")
check("deep tail carries real mass",
      real["deep_tail_fraction"] >= scd.MIN_DEEP_TAIL_FRACTION,
      f"got {real['deep_tail_fraction']}")
print(f"    deep_tail_fraction={real['deep_tail_fraction']}")

print("\n=== 2. A no-signal scene IS flagged (the Kanalia case) ===")
# Pure speckle, no flood: the change image is one narrow mode near 0 dB —
# the same shape the real 8-days-post-peak Kanalia scene showed.
pre2 = [speckled((600, 600), 200.0) for _ in range(3)]
post2 = speckled((600, 600), 200.0)
nosig = scd.detect_flood_change(post2, pre2, direction="both")
check("signal_detectable is False", nosig["signal_detectable"] is False,
      f"got {nosig['signal_detectable']}")
check("deep tail is near-empty",
      nosig["deep_tail_fraction"] < scd.MIN_DEEP_TAIL_FRACTION,
      f"got {nosig['deep_tail_fraction']}")
print(f"    deep_tail_fraction={nosig['deep_tail_fraction']}")

print("\n=== 3. The two cases are separated by a wide margin ===")
ratio = (real["deep_tail_fraction"] / nosig["deep_tail_fraction"]
         if nosig["deep_tail_fraction"] else float("inf"))
check("real flood deep-tail mass >= 5x the no-signal case", ratio >= 5.0,
      f"ratio={ratio:.1f}x")
print(f"    real={real['deep_tail_fraction']} vs "
      f"no-signal={nosig['deep_tail_fraction']}  ({ratio:.1f}x)")

print("\n=== 4. DISCARDED criterion 1 (circular Cohen's d) stays gone ===")
check("no separation_cohens_d field", "separation_cohens_d" not in real)
check("no MIN_SEPARATION_COHENS_D constant",
      not hasattr(scd, "MIN_SEPARATION_COHENS_D"))

print("\n=== 5. DISCARDED criterion 2 (KI bimodality) stays gone ===")
check("no MIN_BIMODAL_TILE_FRACTION constant",
      not hasattr(scd, "MIN_BIMODAL_TILE_FRACTION"))
# WHY it was discarded is a REAL-DATA finding, not reproducible from pure
# synthetic speckle (which is cleaner than a real scene and here yields 0
# bimodal tiles). On the actual Kanalia no-signal scene the bimodal-tile
# fraction was 0.75 vs a real flood's 0.25 — i.e. it ranked the no-signal
# scene as MORE bimodal. Asserting the ordering the guard now relies on
# instead: deep-tail mass, which does separate them on real and synthetic
# data alike.
check("the surviving criterion separates the cases in the right direction",
      real["deep_tail_fraction"] > nosig["deep_tail_fraction"],
      f"real={real['deep_tail_fraction']} nosig={nosig['deep_tail_fraction']}")

print("\n=== 6. The audit trail states the criterion used ===")
for f in ("deep_tail_fraction", "signal_detectable", "signal_criteria"):
    check(f"{f} rides in the result", f in real)
check("criterion values recorded",
      real["signal_criteria"]["min_deep_tail_fraction"]
      == scd.MIN_DEEP_TAIL_FRACTION)

print("\n=== 7. Degenerate inputs do not crash ===")
empty = scd.detect_flood_change(post, [], direction="both")
check("insufficient_reference has no signal verdict to give",
      empty["status"] == "insufficient_reference")

print(f"\n{'='*60}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*60}")
# Run as a script -> exit code. Imported by pytest -> assert instead, so
# collection of the whole directory is not aborted by a SystemExit.
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
