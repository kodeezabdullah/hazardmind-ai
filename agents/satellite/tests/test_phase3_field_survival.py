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
sys.path.insert(0, os.path.join(_HERE, "..", "..", "hazard"))

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


import agent as hazard_agent  # noqa: E402
import analyzer  # noqa: E402

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
sys.exit(1 if FAIL else 0)
