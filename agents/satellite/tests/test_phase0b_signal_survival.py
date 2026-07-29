"""Does `signal_detectable` actually REACH the places that act on it?

This exists because writing the guard was not enough. `_render_clip` maps
index fields EXPLICITLY, so `signal_detectable` was silently dropped on its
way out of `calculate_indices` — the HIGH-severity concern in
`_finish_success` could never have fired, and the guard would have passed its
own unit tests while being invisible in production. That is the exact failure
mode TESTING_GAP_AUDIT.md records for CHANGE 6.

Rather than re-deriving the mapping (which drifts silently), this reads the
real source of each hop and asserts the field is carried.
"""
import os
import re
import sys

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT_DIR)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


proc = open(os.path.join(_AGENT_DIR, "processor.py"), encoding="utf-8").read()
agent = open(os.path.join(_AGENT_DIR, "agent.py"), encoding="utf-8").read()


def _body(src, start_marker, end_marker=None):
    i = src.index(start_marker)
    j = src.index(end_marker, i) if end_marker else len(src)
    return src[i:j]


print("\n=== 1. Produced by the detector ===")
import sar_change_detection as scd  # noqa: E402
import numpy as np  # noqa: E402

rng = np.random.default_rng(3)
sp = lambda s, m: (m * rng.gamma(4.4, 1 / 4.4, size=s)).astype("float32")
pre = [sp((400, 400), 200.0) for _ in range(3)]
post = sp((400, 400), 200.0)
res = scd.detect_flood_change(post, pre, direction="both")
check("detect_flood_change emits signal_detectable", "signal_detectable" in res)
check("detect_flood_change emits deep_tail_fraction", "deep_tail_fraction" in res)
check("no-signal scene verdict is False", res["signal_detectable"] is False,
      f"got {res['signal_detectable']}")

print("\n=== 2. Carried out of calculate_indices ===")
ci = _body(proc, "def calculate_indices(", "def export_png(")
check("calculate_indices returns signal_detectable",
      '"signal_detectable": cd.get("signal_detectable")' in ci)
check("calculate_indices returns deep_tail_fraction",
      '"deep_tail_fraction": cd.get("deep_tail_fraction")' in ci)

print("\n=== 3. Carried through _render_clip (the hop that DROPPED it) ===")
rc = _body(proc, "def _render_clip(", "def _render_per_city(")
check("_render_clip carries signal_detectable",
      '"signal_detectable": indices.get("signal_detectable")' in rc,
      "this is the hop where the field was silently lost")
check("_render_clip carries deep_tail_fraction",
      '"deep_tail_fraction": indices.get("deep_tail_fraction")' in rc)

print("\n=== 4. Acted on in _finish_success (the concern actually fires) ===")
fs = proc[proc.index("def _finish_success("):]
check("_finish_success tests signal_detectable is False",
      'merged_result.get("signal_detectable") is False' in fs)
check("it raises a HIGH-severity concern", 'INDETERMINATE' in fs and '"HIGH"' in fs)
# The message is built from adjacent string literals, so the SOURCE contains
# quote + newline boundaries mid-sentence. Join the literals the way Python
# does before matching (and normalise CRLF, which this repo checks out).
_joined = re.sub(r'"\s*"', "", fs.replace("\r\n", "\n"))
check("it says the extent is NOT evidence of low flood",
      "must NOT be read as evidence of little or no flooding" in _joined,
      "the concern must state the extent is indeterminate, not low flood")

print("\n=== 5. Persisted in agent.py's structured result ===")
check("agent.py persists signal_detectable",
      '"signal_detectable": result.get("signal_detectable")' in agent)
check("agent.py persists deep_tail_fraction",
      '"deep_tail_fraction": result.get("deep_tail_fraction")' in agent)

print("\n=== 6. The concern is HIGH, not a whisper ===")
seg = fs[fs.index('merged_result.get("signal_detectable") is False'):]
seg = seg[:seg.index("if city_boundaries")] if "if city_boundaries" in seg else seg
check("severity is HIGH (silent failure, normal-looking output)",
      '"HIGH"' in seg, seg[-200:])

print(f"\n{'='*60}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*60}")
# Run as a script -> exit code. Imported by pytest -> assert instead, so
# collection of the whole directory is not aborted by a SystemExit.
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
