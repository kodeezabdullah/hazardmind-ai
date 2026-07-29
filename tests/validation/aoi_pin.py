"""Harness-only deterministic AOI pinning for validation runs.

**The defect this closes (found live, science/full-pass Phase 0b):** the
pipeline resolves its AOI through Nominatim/geoBoundaries at run time, and
Nominatim's answer for the same location string CHANGES over time — Kanalia
resolved as a zero-area Point at baseline (2026-07-28) and as a zero-area
LineString one day later, moving the `_ensure_areal` ~6 km buffer disk and
with it BOTH the predicted extent and the clipped reference extent
(reference area 16.082 -> 20.308 km² between two runs of the same event).
Any cross-run metric delta measured across such a shift is confounded: the
goalposts moved with the AOI. A science pass that keeps/discards changes on
per-event metric deltas requires the AOI to be bit-identical across every
run of the same event.

**What this does:** wraps `get_region_boundary` and
`get_risk_city_boundaries` (the only two boundary-resolution entry points
the pipeline's AOI depends on — everything downstream, `merge_risk_boundaries`
/`get_analysis_bbox`, is a pure function of their output, and
`detect_risk_cities` is a curated in-repo map with no network dependency)
in a disk-cache: the FIRST call for a given (function, args) key resolves
live and stores the result under tests/validation/cache/aoi/; every later
call — same session or months later — replays the stored geometry exactly.
Failed resolutions (None/empty) are never cached.

**Why this is safe / cannot leak into production:** same argument as
forced_satellite_override.py — lives entirely under tests/validation/,
activated only via a `with pinned_aoi():` context manager around harness
code, no env var / request field / config value on any production path can
reach it. It patches `agent.get_region_boundary`/`agent.get_risk_city_boundaries`
(the names as bound inside agents/satellite/agent.py via its
`from boundary import ...`) and `boundary.get_*` (for run_baseline.py's own
call-time `from boundary import get_risk_city_boundaries`), following the
patch-where-looked-up rule forced_satellite_override.py documents.

**What this deliberately does NOT pin:** scene search (pinned separately by
sentinel_clock_patch), cloud peeks, downloads — those are the measurand.
Only the geographic frame is pinned.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import patch

logger = logging.getLogger("aoi_pin")

CACHE_DIR = Path(__file__).resolve().parent / "cache" / "aoi"


def _cache_path(func_name: str, args: tuple, kwargs: dict) -> Path:
    payload = json.dumps([func_name, list(args), kwargs], default=str, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return CACHE_DIR / f"{func_name}-{digest}.json"


def _caching_wrapper(func_name: str, real_func):
    @functools.wraps(real_func)
    def wrapper(*args, **kwargs):
        path = _cache_path(func_name, args, kwargs)
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                entry = json.load(fh)
            logger.info(
                "AOI PINNED: %s%r replayed from %s (resolved %s)",
                func_name, args, path.name, entry.get("resolved_at"),
            )
            return entry["result"]
        result = real_func(*args, **kwargs)
        if result:  # never pin a failed/empty resolution
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            from datetime import datetime, timezone
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "func": func_name,
                        "args": list(args),
                        "kwargs": kwargs,
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                        "result": result,
                    },
                    fh, default=str,
                )
            logger.info(
                "AOI PIN CREATED: %s%r resolved live and stored to %s",
                func_name, args, path.name,
            )
        return result
    return wrapper


@contextlib.contextmanager
def pinned_aoi():
    """Pin boundary resolution for the duration of the block.

    Requires agents/satellite/ on sys.path (the harness call sites already
    guarantee this). Patches both the agent-module bindings (agent.py binds
    the names at import via `from boundary import ...`) and the boundary
    module itself (for call-time local imports like run_baseline.py's).
    """
    import boundary  # type: ignore  # agents/satellite on sys.path
    import agent  # type: ignore

    wrapped_region = _caching_wrapper(
        "get_region_boundary", boundary.get_region_boundary
    )
    wrapped_cities = _caching_wrapper(
        "get_risk_city_boundaries", boundary.get_risk_city_boundaries
    )

    with patch.object(agent, "get_region_boundary", wrapped_region), \
         patch.object(agent, "get_risk_city_boundaries", wrapped_cities), \
         patch.object(boundary, "get_region_boundary", wrapped_region), \
         patch.object(boundary, "get_risk_city_boundaries", wrapped_cities):
        yield


if __name__ == "__main__":
    # Offline structural smoke test — no network needed after first stub call.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents" / "satellite"))
    logging.basicConfig(level=logging.INFO)
    import agent  # noqa: E402
    import boundary  # noqa: E402

    calls = {"n": 0}

    def _fake_resolver(name, cities=None):
        calls["n"] += 1
        return [{"name": "X", "geojson": {"type": "Point", "coordinates": [1.0, 2.0]}}]

    real = boundary.get_risk_city_boundaries
    boundary.get_risk_city_boundaries = _fake_resolver  # type: ignore
    agent.get_risk_city_boundaries = _fake_resolver  # type: ignore
    try:
        with pinned_aoi():
            a = agent.get_risk_city_boundaries("SMOKE-TEST-LOC", ["X"])
            b = agent.get_risk_city_boundaries("SMOKE-TEST-LOC", ["X"])
        assert a == b, "replay differs from first resolution"
        assert calls["n"] == 1, f"expected 1 live call, got {calls['n']}"
        # A fresh context must replay from disk (0 further live calls).
        with pinned_aoi():
            c = agent.get_risk_city_boundaries("SMOKE-TEST-LOC", ["X"])
        assert c == a and calls["n"] == 1
        print("OK — pinned_aoi caches on first call and replays across contexts.")
    finally:
        boundary.get_risk_city_boundaries = real  # type: ignore
        agent.get_risk_city_boundaries = real  # type: ignore
        for p in CACHE_DIR.glob("*SMOKE*"):
            p.unlink()
        # cache key is hashed; remove by scanning content instead
        for p in CACHE_DIR.glob("get_risk_city_boundaries-*.json"):
            try:
                if "SMOKE-TEST-LOC" in p.read_text(encoding="utf-8"):
                    p.unlink()
            except OSError:
                pass
