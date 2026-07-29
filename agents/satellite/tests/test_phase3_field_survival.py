"""Phase 3a/3b SURVIVAL test — do the new fields actually reach consumers?

Per the repo rule: proving a function computes a field correctly is not the
same as proving the field reaches its persistence/consumption point. CHANGE 6
once passed 12 tests while being invisible in production, which is the
incident this rule exists for.

This checks the real contract chain by calling the REAL adapter and the REAL
prompt builder, not by re-implementing their logic:

  satellite result dict
    -> hazard's _normalise_satellite_payload  (the real adapter)
      -> analyze_flood's prompt               (the real prompt string)

It deliberately does NOT re-derive the logic in the test, and it asserts the
one thing that matters operationally: the LLM is told to assess flood on the
flood figure, not on a total that includes a permanent river.
"""
import asyncio
import os
import sys
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))

# The hazard agent's modules use the SAME bare names as the satellite agent's
# (`agent.py`, `intelligence.py`) — the exact collision backend/graph.py's
# isolated loader exists to work around. Importing them by putting
# agents/hazard on sys.path permanently rebinds `agent` to the HAZARD one, so
# every satellite test that runs after this file in the same pytest process
# gets the wrong module. (Measured: it broke 7 tests in
# test_verify_islamabad_fixes.py, which pass in isolation.)
#
# Load them under private names from an explicit file path instead, and leave
# sys.modules/sys.path exactly as they were found.
import importlib.util  # noqa: E402


def _load_isolated(mod_name: str, path: str):
    """Import a module from an explicit path without leaking its bare name."""
    hazard_dir = os.path.dirname(path)
    added = hazard_dir not in sys.path
    if added:
        sys.path.insert(0, hazard_dir)
    saved = {k: sys.modules[k] for k in ("agent", "analyzer", "intelligence")
             if k in sys.modules}
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        # Restore whatever the satellite tests had bound, and drop the bare
        # names this import created.
        for k in ("agent", "analyzer", "intelligence"):
            sys.modules.pop(k, None)
        sys.modules.update(saved)
        if added:
            try:
                sys.path.remove(hazard_dir)
            except ValueError:
                pass


_HAZARD = os.path.abspath(os.path.join(_HERE, "..", "..", "hazard"))

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


hazard_agent = _load_isolated(
    "_hazard_agent_for_test", os.path.join(_HAZARD, "agent.py")
)
analyzer = _load_isolated(
    "_hazard_analyzer_for_test", os.path.join(_HAZARD, "analyzer.py")
)

# A satellite payload shaped like the real one, carrying the Phase 3 fields.
SAT = {
    "event_id": "11111111-2222-3333-4444-555555555555",
    "satellite_type": "sentinel-2",
    "index_type": "MNDWI",
    "index_calibrated": True,
    "index_units": "MNDWI_ratio",
    "mean_index": 0.12,
    "water_percent": 8.4,
    "affected_area_km2": 3.1,
    "flood_area_km2": 3.1,
    "permanent_water_area_km2": 12.4,
    "total_water_area_km2": 15.5,
    "permanent_water_features": [
        {"name": "Ravi River", "kind": "river", "area_km2": 12.4,
         "area_basis": "jrc_measured_total_single_feature"},
    ],
    "permanent_water_context": (
        "The AOI contains these permanent water bodies: Ravi River (12.4 km2). "
        "Water detected BEYOND that permanent baseline is 3.1 km2. "
        "Assess flood risk on the latter, not the total."
    ),
    "bbox": [74.2, 31.4, 74.5, 31.7],
    "risk_cities": ["Lahore"],
    "confidence": 0.8,
}

print("\n=== 1. Fields survive the REAL satellite->hazard adapter ===")
norm = hazard_agent._normalise_satellite_payload(SAT, SAT["event_id"])
an = norm["analysis"]
for field, expected in [
    ("flood_area_km2", 3.1),
    ("permanent_water_area_km2", 12.4),
    ("total_water_area_km2", 15.5),
]:
    check(f"{field} crosses the boundary", an.get(field) == expected,
          f"got {an.get(field)}")
check("permanent_water_features crosses (named)",
      (an.get("permanent_water_features") or [{}])[0].get("name") == "Ravi River")
check("permanent_water_context crosses",
      "Ravi River" in (an.get("permanent_water_context") or ""))

print("\n=== 2. The context reaches the REAL flood prompt ===")
captured = {}


async def _fake_llm(prompt, system=None, **kw):
    captured["prompt"] = prompt
    return None  # force the deterministic path; we only want the prompt


async def _run():
    with patch.object(analyzer, "smart_llm_call", _fake_llm, create=True):
        try:
            await analyzer.analyze_flood(
                SAT["bbox"], 3.1, 0.12, {"count": 0},
                "sentinel-2", True, "MNDWI",
                an.get("permanent_water_context"),
            )
        except Exception:
            pass  # a downstream failure is fine — we captured the prompt


asyncio.run(_run())
p = captured.get("prompt", "")
if not p:
    # smart_llm_call may be imported under a different name; fall back to
    # building the prompt through the same code path via a direct call.
    check("prompt captured", False, "no prompt captured — check patch target")
else:
    check("prompt names the water body", "Ravi River" in p, p[:200])
    check("prompt states the permanent baseline", "12.4" in p, p[:200])
    check("prompt states the flood-only figure", "3.1" in p, p[:200])
    check("prompt directs assessment away from the total",
          "not the total" in p, p[:200])
    print(f"    prompt: {p[:220]}...")

print("\n=== 3. Absent Phase 3 fields degrade cleanly (old payloads) ===")
legacy = {k: v for k, v in SAT.items()
          if not k.startswith("permanent_water") and k not in
          ("flood_area_km2", "total_water_area_km2")}
norm2 = hazard_agent._normalise_satellite_payload(legacy, SAT["event_id"])
an2 = norm2["analysis"]
check("missing fields are None, not crashes",
      an2.get("permanent_water_area_km2") is None
      and an2.get("permanent_water_context") is None)
check("affected_area_km2 still present (no regression)",
      an2.get("affected_area_km2") == 3.1, f"got {an2.get('affected_area_km2')}")


async def _run_legacy():
    with patch.object(analyzer, "smart_llm_call", _fake_llm, create=True):
        try:
            await analyzer.analyze_flood(
                SAT["bbox"], 3.1, 0.12, {"count": 0}, "sentinel-2", True,
                "MNDWI", None,
            )
        except Exception:
            pass


captured.clear()
asyncio.run(_run_legacy())
p2 = captured.get("prompt", "")
check("no context -> no 'none found' noise injected into the prompt",
      "permanent water" not in p2.lower(), p2[:160])

print(f"\n{'='*58}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*58}")
# Run as a script -> exit code. Imported by pytest -> assert instead, so
# collection of the whole directory is not aborted by a SystemExit.
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
