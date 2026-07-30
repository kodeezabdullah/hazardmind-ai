"""Forces search_pre_event_same_orbit's max_scenes to 1 for one diagnostic
run, so a change-detection result can be inspected against the SIMPLEST
possible baseline (1 pre-event scene, 1 post-event scene) rather than the
default 3-scene median baseline (sentinel.BASELINE_TARGET_SCENES).

WHY THIS EXISTS
---------------
Muzaffargarh/Trimmu's classification showed near-zero detected water despite
visually obvious flooding in the true-color image and a JRC-confirmed absence
of permanent water at this location. The `_normalise_percent` 100x-inflation
bug (fixed 2026-07-30) explains why the DIAGNOSTIC TEXT overstated the
detection, but not why the underlying detection itself was low-signal.

A 3-scene median baseline is a reasonable design for noise reduction, but it
also means the reported signal is diluted through 3 comparisons rather than
1. This tool isolates that variable: does a single, simple pre/post
comparison recover more signal than the 3-scene median did? If yes, the
median-baseline construction is implicated. If no, the low signal is a
property of the detection threshold or the scene itself, not the baseline
depth — narrowing the search rather than guessing at a fix.

This is diagnostic-only. It does not change any pipeline default; it exists
to run ONE forced-single-scene comparison, read the result, then be deleted
or left unused. Not intended to ship.

Usage: same as forced_satellite_override.py — a context manager imported by
pipeline_runner.py / run_baseline.py for one run.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def forced_single_baseline_scene():
    """Patches sentinel.search_pre_event_same_orbit's max_scenes default to 1
    for the duration of the context. Reverts automatically on exit — the
    module-level constant and function are never mutated on disk.
    """
    import sentinel

    original = sentinel.search_pre_event_same_orbit

    def _wrapped(post_scene, bbox, merged_polygon=None, max_scenes=1, **kw):
        # Force max_scenes=1 regardless of what processor.py passes (it
        # currently passes nothing, taking the module default of 3).
        return original(post_scene, bbox, merged_polygon=merged_polygon,
                         max_scenes=1, **kw)

    with patch("sentinel.search_pre_event_same_orbit", _wrapped):
        # processor.py did `from sentinel import search_pre_event_same_orbit`
        # at call time (inside the try block), so patching the sentinel
        # module attribute is sufficient — no need to also patch processor's
        # namespace, since it re-imports fresh on every call.
        yield
