"""TEST SUITE 3: Sentinel Module (live CDSE)."""

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

from sentinel import (  # noqa: E402
    SENTINEL_2,
    authenticate_copernicus,
    search_imagery,
    select_satellite,
)

PASS, FAIL, WARN = [], [], []


def ok(t, m):
    PASS.append(t)
    print(f"  PASS [{t}]: {m}")


def bad(t, m):
    FAIL.append(t)
    print(f"  FAIL [{t}]: {m}")


def warn(t, m):
    WARN.append(t)
    print(f"  WARN [{t}]: {m}")


# Small bbox around Peshawar, Pakistan.
PESHAWAR_BBOX = (71.4, 33.9, 71.7, 34.1)


def run():
    print("=" * 60)
    print("TEST SUITE 3: Sentinel Module")
    print("=" * 60)

    print("\nT3.1 authenticate_copernicus")
    token = authenticate_copernicus()
    if token:
        ok("T3.1", "token returned")
        if len(token) > 100:
            ok("T3.1", f"token length > 100 ({len(token)} chars)")
        else:
            bad("T3.1", f"token suspiciously short: {len(token)} chars")
    else:
        bad("T3.1", "authentication returned None (cannot continue suite 3)")
        return

    print("\nT3.2 select_satellite (Peshawar, flood) with cloud check")
    sel = select_satellite("flood", bbox=PESHAWAR_BBOX, token=token)
    if not isinstance(sel, dict):
        bad("T3.2", f"did not return dict: {type(sel)}")
    else:
        cc = sel.get("cloud_cover")
        if cc is not None:
            ok("T3.2", f"checked actual cloud cover: {cc}%")
        else:
            warn("T3.2", "no cloud cover observed (no recent S2 scene to peek)")
        st = sel.get("satellite_type")
        if st in ("sentinel-1", "sentinel-2"):
            ok("T3.2", f"satellite_type={st}")
        else:
            bad("T3.2", f"unexpected satellite_type: {st}")
        if sel.get("reason"):
            ok("T3.2", f"reason logged: {sel.get('reason')}")
        else:
            bad("T3.2", "no reason in selection")

    print("\nT3.3 search_imagery (Peshawar, sentinel-2)")
    # Use a 14-day window for robustness against sparse recent acquisitions.
    scene = search_imagery(PESHAWAR_BBOX, SENTINEL_2, date_range=14)
    if not scene:
        warn("T3.3", "no scene in 14d at <30% cloud; retrying 30d window")
        scene = search_imagery(PESHAWAR_BBOX, SENTINEL_2, date_range=30)
    if scene:
        ok("T3.3", f"at least 1 scene returned: {scene.get('Name','?')[:50]}")
        cc = scene.get("_cloud")
        if cc is not None:
            ok("T3.3", f"scene has cloud_cover field: {cc}%")
        else:
            bad("T3.3", "scene missing _cloud field")
        if "_score" in scene:
            ok("T3.3", f"coverage score calculated: {scene['_score']:.3f} (overlap={scene.get('_overlap',0)*100:.0f}%)")
        else:
            bad("T3.3", "scene missing _score")
    else:
        bad("T3.3", "no scene found even in 30d window")


if __name__ == "__main__":
    start = time.time()
    run()
    dur = time.time() - start
    print("\n" + "=" * 60)
    print(f"SUITE 3 SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)} WARN={len(WARN)} in {dur:.1f}s")
    if FAIL:
        print("FAILED:", FAIL)
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
