"""Harness-only floor forcing the post-event scene to be AFTER the flood peak.

**The defect this closes (found live, science/full-pass run 4).** The harness
pins the pipeline's clock to the reference product's acquisition instant
(`as_of`) and `search_imagery` looks BACKWARDS from it over `date_range`
days. For a flood that is the wrong direction: the useful post-event scene
is the first same-orbit pass at or AFTER the peak, and a backward window
structurally cannot reach it.

Measured consequence at Kanalia (EMSR692, Storm Daniel):

  - event peak            2023-09-05
  - `as_of`               2023-09-06 04:39 (reference acquisition)
  - window                [2023-08-30, 2023-09-06]
  - scene actually chosen **2023-09-01** — five days BEFORE the flood

S1 change detection then compared pre-flood against pre-flood and correctly
reported ~0 dB change (`mean_index -0.0312`, zero zones). The method was
right; the input was wrong. Scoring that against the 2023 flood extent
would have recorded a correct no-change answer as a detector failure.

**What this does.** Wraps `sentinel.search_imagery` for the duration of a
`with post_peak_floor(peak)` block and drops any candidate acquired before
`peak`. Nothing else changes: ranking, coverage logic, dedupe, the tiered
search and every downstream stage run untouched on the surviving
candidates. Because the pre-event baseline is derived from the POST-event
scene's own timestamp (`search_pre_event_same_orbit`), fixing the
post-event side automatically fixes the reference side too.

**Why the window is also widened.** The first same-orbit repeat after peak
is ~12 days out (orbit 7 DESCENDING at this AOI: 2023-09-01 -> 2023-09-13),
so a 5-7 day window contains no post-peak pass at all. The block therefore
also raises `date_range` to at least `min_days_after` so a post-peak scene
is reachable.

**Why this is safe / cannot leak into production:** same argument as
`forced_satellite_override` — it lives entirely under tests/validation/, is
a `unittest.mock.patch` context manager rather than a pipeline code path,
and requires callable Python to activate. No request field, env var or
config value on any production path can reach it.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from unittest.mock import patch

logger = logging.getLogger("post_peak_scene_floor")


@contextlib.contextmanager
def post_peak_floor(peak: datetime, min_days_after: int = 14):
    """Force scene search to consider only acquisitions at/after `peak`.

    `peak` must be timezone-aware UTC. `min_days_after` is the minimum
    search window (days) — wide enough to contain the first same-orbit
    repeat pass after the peak (~12 days for S1 at mid-latitudes).
    """
    import sentinel  # agents/satellite on sys.path

    real_search = sentinel.search_imagery

    def _post_peak_search(bbox, satellite_type, date_range=7, **kwargs):
        # Widen so a post-peak pass is reachable at all, then filter.
        widened = max(date_range, min_days_after)
        result = real_search(bbox, satellite_type, date_range=widened, **kwargs)
        if not result:
            return result

        scenes = result if isinstance(result, list) else [result]
        kept, dropped = [], 0
        for scene in scenes:
            dt = sentinel.scene_datetime(scene)
            if dt is None or dt >= peak:
                kept.append(scene)
            else:
                dropped += 1

        logger.info(
            "POST-PEAK FLOOR: %d candidate(s) before %s dropped, %d kept "
            "(window widened %d -> %d days)",
            dropped, peak.isoformat(), len(kept), date_range, widened,
        )
        if not kept:
            logger.warning(
                "POST-PEAK FLOOR: no acquisition at/after the peak in a "
                "%d-day window — returning empty rather than silently "
                "scoring pre-flood imagery as a post-event scene",
                widened,
            )
            return [] if isinstance(result, list) else None
        return kept if isinstance(result, list) else kept[0]

    # agent.py binds `search_imagery` at import (from sentinel import ...),
    # and processor/backfill call it via the sentinel module. Patch both
    # lookup sites — the patch-where-it-is-looked-up rule
    # forced_satellite_override.py documents.
    import agent  # type: ignore

    with patch.object(sentinel, "search_imagery", _post_peak_search), \
         patch.object(agent, "search_imagery", _post_peak_search):
        yield


if __name__ == "__main__":
    # Offline structural smoke test — no network.
    import os
    import sys
    from datetime import timezone
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents" / "satellite"))
    logging.basicConfig(level=logging.INFO)
    import sentinel  # noqa: E402

    peak = datetime(2023, 9, 5, tzinfo=timezone.utc)
    fake = [
        {"Name": "pre", "ContentDate": {"Start": "2023-09-01T04:31:57.000Z"}},
        {"Name": "post", "ContentDate": {"Start": "2023-09-13T04:31:57.000Z"}},
    ]
    real = sentinel.search_imagery
    sentinel.search_imagery = lambda *a, **k: list(fake)  # type: ignore
    try:
        with post_peak_floor(peak):
            out = sentinel.search_imagery((0, 0, 1, 1), "sentinel-1", date_range=7)
        names = [s["Name"] for s in out]
        assert names == ["post"], names
        print("OK — pre-peak scene dropped, post-peak scene kept:", names)
    finally:
        sentinel.search_imagery = real  # type: ignore
