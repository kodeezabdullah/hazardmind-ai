"""Harness-only forced-satellite-selection override for validation runs.

BASELINE_REPORT.md §3 documented that every baseline run selected Sentinel-2,
because `select_satellite`'s cloud-aware logic (agents/satellite/sentinel.py,
"physics over assumption" per CLAUDE.md) always overrides the harness's
intent to test a specific path — both historical dates this baseline
searched happened to have clear sky. Two consecutive sessions have now
failed to produce a single scored S1 result for two different reasons; this
is the harness-only fix flagged (not attempted) in that report.

**What this does**: monkey-patches `sentinel.select_satellite` for the
duration of a `with forced_satellite(...)` block so it returns a fixed
selection instead of running its real cloud-aware decision logic. Everything
downstream of selection (scene search, download, stack, clip, indices,
vectorize, R2 upload, DB persistence) runs completely unmodified — the
override touches nothing but which branch `agent.py` takes immediately after
calling `select_satellite`.

**Why this is safe / cannot leak into production**:
  - This module lives entirely under tests/validation/ and is never imported
    by any agent, backend, or production entry point — grep confirms no
    production code path exists.
  - It is a `unittest.mock.patch` context manager (the same technique
    sentinel_clock_patch.py already uses for the harness's clock freeze), not
    a new pipeline code path, env var, or CLI flag that pipeline code reads.
  - It requires callable Python code (`from forced_satellite_override import
    forced_satellite`) to activate — there is no request field, header, or
    config value on ANY production request path (`/analyze`'s
    AnalyzeRequest, ProcessDisasterInput, PipelineState) that could reach it.
  - `selection_reason` on every forced result is always exactly
    "harness_forced_selection" — grep-distinguishable from every real
    selection_reason value real `select_satellite` can produce
    ("aoi_scl_measured" / "scene_metadata_clear" / "scene_metadata_cloudy" /
    "no_s2_candidates" / "scl_unavailable_fallback"), so a forced result can
    never be silently mistaken for (or silently merged into a dataset of) a
    real selection in any stored evidence — the exact non-negotiable named
    in the task.

Returned selection dict shape matches sentinel.select_satellite's real
return value exactly (agents/satellite/agent.py only ever reads
satellite_type/cloud_cover/reason/selection_reason/scene_cloud_percent/
aoi_cloud_percent off it — confirmed by reading every `selection.get(...)`
call site in agent.py before writing this).
"""

from __future__ import annotations

import contextlib
from typing import Optional
from unittest.mock import patch


@contextlib.contextmanager
def forced_satellite(satellite_type: str):
    """Force agents/satellite/agent.py's satellite selection for one run.

    ``satellite_type`` must be "sentinel-1" or "sentinel-2" (the same string
    constants sentinel.SENTINEL_1/SENTINEL_2 use).

    agents/satellite/agent.py does ``from sentinel import ... select_satellite``
    (confirmed by reading its import block), which binds agent.py's OWN
    module-level name `select_satellite` at import time — patching
    `sentinel.select_satellite` would NOT reach agent.py's already-bound
    reference (the standard `unittest.mock.patch` "patch where it's looked
    up, not where it's defined" gotcha; sentinel_clock_patch.py's docstring
    names this same rule but its own target, `sentinel.datetime`, is looked
    up inside sentinel.py itself, so it doesn't hit this trap — this override
    does, since the call site is one module further downstream). So this
    patches `agent.select_satellite` — the name as looked up inside agent.py,
    where every call this override needs to intercept actually happens. The
    catalogue search, download, processing, and persistence code that follows
    in agent.py is completely untouched — this only changes which
    satellite_type that code operates on.
    """
    if satellite_type not in ("sentinel-1", "sentinel-2"):
        raise ValueError(
            f"forced_satellite requires 'sentinel-1' or 'sentinel-2', got {satellite_type!r}"
        )

    def _forced_select_satellite(
        disaster_type: str,
        bbox: Optional[tuple] = None,
        token: Optional[str] = None,
        cloud_cover: Optional[float] = None,
        aoi_geom: Optional[dict] = None,
        aoi_cloud_percent: Optional[float] = None,
        aoi_cloud_reason: Optional[str] = None,
    ) -> dict:
        return {
            "satellite_type": satellite_type,
            "reason": f"harness_forced_selection: forced to {satellite_type} for validation",
            "cloud_cover": cloud_cover,
            "user_hint": (disaster_type or "").lower(),
            "scene_cloud_percent": cloud_cover,
            "aoi_cloud_percent": aoi_cloud_percent,
            # Deliberately distinct from every real selection_reason value
            # (see module docstring) so no forced result can ever be mistaken
            # for a real selection in stored evidence.
            "selection_reason": "harness_forced_selection",
        }

    with patch("agent.select_satellite", _forced_select_satellite):
        yield


if __name__ == "__main__":
    # Offline structural smoke test — no network, no pipeline invocation.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents" / "satellite"))
    import agent  # noqa: E402

    real = agent.select_satellite("flood", bbox=(0, 0, 1, 1))
    print("real (unforced) selection_reason:", real["selection_reason"])
    assert real["selection_reason"] != "harness_forced_selection"

    with forced_satellite("sentinel-1"):
        forced = agent.select_satellite("flood", bbox=(0, 0, 1, 1))
        print("forced selection:", forced)
        assert forced["satellite_type"] == "sentinel-1"
        assert forced["selection_reason"] == "harness_forced_selection"

    after = agent.select_satellite("flood", bbox=(0, 0, 1, 1))
    print("real selection restored after context exit:", after["selection_reason"])
    assert after["selection_reason"] != "harness_forced_selection"
    print("OK — forced_satellite context manager behaves correctly, offline.")
