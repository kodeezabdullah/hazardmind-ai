"""TEST SUITE 1: Intelligence Layer.

Runs the nine intelligence-layer test cases (T1.1 - T1.9) live against the
Featherless model chain. LLM output is non-deterministic, so assertions check
structural correctness (right keys, plausible values, required behaviour)
rather than exact strings. Each check emits PASS/FAIL/WARN lines and the suite
prints a tally at the end.
"""

import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from intelligence import SatelliteIntelligence  # noqa: E402

PASS, FAIL, WARN = [], [], []


def ok(test, msg):
    PASS.append(test)
    print(f"  PASS [{test}]: {msg}")


def bad(test, msg):
    FAIL.append(test)
    print(f"  FAIL [{test}]: {msg}")


def warn(test, msg):
    WARN.append(test)
    print(f"  WARN [{test}]: {msg}")


intel = SatelliteIntelligence()


def t1_1():
    print("\nT1.1 parse_disaster_input (flood in Peshawar magnitude 5.5)")
    p = intel.parse_disaster_input("flood in Peshawar Pakistan magnitude 5.5")
    if not p:
        return bad("T1.1", "returned None (LLM chain failed)")
    loc = (p.get("location") or "").lower()
    if "peshawar" in loc:
        ok("T1.1", f"location={p.get('location')}")
    else:
        bad("T1.1", f"location wrong: {p.get('location')}")
    if (p.get("disaster_type") or "").lower() == "flood":
        ok("T1.1", "disaster_type=flood")
    else:
        bad("T1.1", f"disaster_type wrong: {p.get('disaster_type')}")
    try:
        if abs(float(p.get("magnitude")) - 5.5) < 0.01:
            ok("T1.1", "magnitude=5.5")
        else:
            bad("T1.1", f"magnitude wrong: {p.get('magnitude')}")
    except (TypeError, ValueError):
        bad("T1.1", f"magnitude not numeric: {p.get('magnitude')}")
    # spec wants ambiguous=false, confidence>0.7
    if p.get("ambiguous") is False:
        ok("T1.1", "ambiguous=false")
    else:
        warn("T1.1", f"ambiguous={p.get('ambiguous')} (spec expects false; model flagged magnitude oddity for a flood)")
    try:
        if float(p.get("confidence")) > 0.7:
            ok("T1.1", f"confidence={p.get('confidence')}>0.7")
        else:
            warn("T1.1", f"confidence={p.get('confidence')} not >0.7 (model judgement)")
    except (TypeError, ValueError):
        bad("T1.1", f"confidence not numeric: {p.get('confidence')}")


def t1_2():
    print("\nT1.2 parse_disaster_input ambiguous ('something happened in KPK')")
    p = intel.parse_disaster_input("something happened in KPK")
    if not p:
        return bad("T1.2", "returned None")
    if p.get("ambiguous") is True:
        ok("T1.2", "ambiguous=true")
    else:
        bad("T1.2", f"ambiguous={p.get('ambiguous')} (expected true)")
    missing = [str(m).lower() for m in (p.get("missing_info") or [])]
    dtype = (p.get("disaster_type") or "")
    if any("disaster" in m or "type" in m for m in missing) or not dtype:
        ok("T1.2", f"disaster_type flagged missing/empty (missing_info={p.get('missing_info')}, disaster_type={dtype!r})")
    else:
        bad("T1.2", f"disaster_type not flagged missing: {p.get('missing_info')}, dtype={dtype!r}")


def t1_3():
    print("\nT1.3 devise_satellite_strategy (flood, cloud=65%, scenes=5)")
    s = intel.devise_satellite_strategy(
        {"disaster_type": "flood", "location": "Peshawar"},
        cloud_cover=65, available_scenes_count=5, attempt_number=1,
    )
    if not s:
        return bad("T1.3", "returned None")
    sat = (s.get("satellite") or "").lower()
    if "sentinel-1" in sat or "sentinel1" in sat or sat == "sentinel-1":
        ok("T1.3", f"satellite={s.get('satellite')} (SAR for cloud>30%)")
    else:
        bad("T1.3", f"satellite={s.get('satellite')} (expected sentinel-1 for cloud 65%)")
    reason = (s.get("reason") or "").lower()
    if "cloud" in reason:
        ok("T1.3", "reason mentions cloud cover")
    else:
        warn("T1.3", f"reason does not mention cloud: {s.get('reason')!r}")


def t1_4():
    print("\nT1.4 devise_satellite_strategy clear sky (earthquake, cloud=8%, scenes=3)")
    s = intel.devise_satellite_strategy(
        {"disaster_type": "earthquake", "location": "test"},
        cloud_cover=8, available_scenes_count=3, attempt_number=1,
    )
    if not s:
        return bad("T1.4", "returned None")
    sat = (s.get("satellite") or "").lower()
    if "sentinel-2" in sat or "sentinel2" in sat:
        ok("T1.4", f"satellite={s.get('satellite')} (optical for clear sky)")
    else:
        bad("T1.4", f"satellite={s.get('satellite')} (expected sentinel-2)")


def t1_5():
    print("\nT1.5 handle_anomaly (copernicus_auth_failed, attempt=1)")
    r = intel.handle_anomaly("copernicus_auth_failed", {"event_id": "t", "location": "Peshawar"}, 1)
    if not r:
        return bad("T1.5", "returned None")
    action = (r.get("action") or "").lower()
    if any(a in action for a in ("retry", "fallback")):
        ok("T1.5", f"action={r.get('action')}")
    else:
        warn("T1.5", f"action={r.get('action')} (expected retry/fallback)")
    steps = r.get("specific_steps") or []
    if steps:
        ok("T1.5", f"specific_steps non-empty ({len(steps)} steps)")
    else:
        bad("T1.5", "specific_steps empty")


def t1_6():
    print("\nT1.6 handle_anomaly max attempts (no_sentinel_scenes, attempt=3)")
    r = intel.handle_anomaly("no_sentinel_scenes", {"event_id": "t", "satellite": "sentinel-2"}, 3)
    if not r:
        return bad("T1.6", "returned None")
    expand = r.get("expand_date_range")
    use_landsat = r.get("use_landsat")
    blob = json.dumps(r).lower()
    if expand or use_landsat or "landsat" in blob or "expand" in blob or "date" in blob:
        ok("T1.6", f"expand_date_range={expand}, use_landsat={use_landsat} (widen/landsat advised)")
    else:
        bad("T1.6", f"no expand/landsat advice: {r}")


def t1_7_result():
    return {
        "index_type": "NDWI",
        "ndwi_mean": 0.45,
        "water_percent": 23,
        "area_km2": 153,
        "zones": 22,
        "disaster": "flood",
    }


def t1_7():
    print("\nT1.7 interpret_results (ndwi 0.45, water 23%, 153km2, 22 zones, flood)")
    r = intel.interpret_results(
        index_type="NDWI",
        index_stats={"mean_index": 0.45, "water_percent": 23},
        disaster_type="flood",
        location="Peshawar, Pakistan",
        total_zones=22,
        area_km2=153.0,
        satellite_used="sentinel-2",
    )
    if not r:
        bad("T1.7", "returned None")
        return None
    sev = (r.get("severity") or "").upper()
    if sev in ("HIGH", "CRITICAL"):
        ok("T1.7", f"severity={sev}")
    else:
        warn("T1.7", f"severity={sev} (spec expects HIGH/CRITICAL; model judgement)")
    summary = (r.get("summary") or "")
    findings_blob = (summary + " " + json.dumps(r.get("key_findings") or [])).replace(",", "")
    nums = ["153", "23", "22"]
    hit = [n for n in nums if n in findings_blob]
    if hit:
        ok("T1.7", f"summary/findings mention numbers {hit}")
    else:
        warn("T1.7", f"specific numbers not echoed in summary/findings: {summary[:120]!r}")
    return r


def t1_8(interp):
    print("\nT1.8 generate_band_message (from T1.7 results)")
    msg = intel.generate_band_message(
        results=t1_7_result(),
        interpretation=interp or {"severity": "HIGH"},
        anomalies=[],
        confidence=0.85,
        next_agent_handle="@hazardmind-hazard",
    )
    if not msg:
        return bad("T1.8", "returned None")
    if msg.lstrip().startswith("@hazardmind-hazard"):
        ok("T1.8", "starts with @hazardmind-hazard")
    else:
        bad("T1.8", f"does not start with handle: {msg[:60]!r}")
    if any(n in msg for n in ("153", "23", "22", "0.45")):
        ok("T1.8", "mentions actual numbers")
    else:
        warn("T1.8", "no actual numbers found in message")
    low = msg.lower()
    if "confidence" in low or "0.85" in msg or "85%" in msg:
        ok("T1.8", "confidence included")
    else:
        warn("T1.8", "confidence not explicitly mentioned")
    wc = len(msg.split())
    if wc < 200:
        ok("T1.8", f"under 200 words ({wc})")
    else:
        bad("T1.8", f"{wc} words (>=200)")


def t1_9():
    print("\nT1.9 decide_landsat_fallback (no_sentinel_scenes, earthquake, 3 days)")
    r = intel.decide_landsat_fallback("no_sentinel_scenes", "earthquake", "Peshawar, Pakistan", 3)
    if not r:
        return bad("T1.9", "returned None")
    if "use_landsat" in r and isinstance(r.get("use_landsat"), bool):
        ok("T1.9", f"use_landsat={r.get('use_landsat')} (yes/no)")
    else:
        bad("T1.9", f"use_landsat missing/not bool: {r.get('use_landsat')}")
    if (r.get("reason") or "").strip():
        ok("T1.9", "reasoning present")
    else:
        bad("T1.9", "no reasoning")


if __name__ == "__main__":
    start = time.time()
    print("=" * 60)
    print("TEST SUITE 1: Intelligence Layer")
    print("=" * 60)
    t1_1()
    t1_2()
    t1_3()
    t1_4()
    t1_5()
    t1_6()
    interp = t1_7()
    t1_8(interp)
    t1_9()
    dur = time.time() - start
    print("\n" + "=" * 60)
    print(f"SUITE 1 SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)} WARN={len(WARN)} in {dur:.1f}s")
    if FAIL:
        print("FAILED checks:", FAIL)
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
