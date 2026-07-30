"""Runs the REAL satellite pipeline (no mocking of the science path) against
a historical EMS reference event, with the scene-search clock pinned to the
reference product's own acquisition date via sentinel_clock_patch, and reads
the result back through the backend's durable-evidence trail (the
GET /results/{event_id}/evidence code path), not the pipeline's in-memory
JSON return.

Why go through the DB/evidence path instead of trusting the in-memory return:
this repo's own root CLAUDE.md documents a real defect class where a field
computed correctly never reached its persistence point (the
`total_zones`/`scene_id` always-NULL bug, closed 2026-07-27) — proving
correctness in memory is not the same as proving durability. Scoring the
value the production frontend/report would actually read (a DB row, via the
same `get_event_evidence` query GET /results/{id}/evidence uses) is what
makes this a validation of the SYSTEM, not just of one function's return
value.

Isolation approach for the satellite agent mirrors backend/graph.py's
`_load_node`: agents/satellite/ has bare module names (sentinel.py,
processor.py, intelligence.py, ...) that would collide with any other
agent's same-named modules if this repo's other agents were ever imported in
the same process. This harness only ever loads the satellite agent's modules
(never hazard/impact/report — importing backend/router.py directly would
pull those in via orchestrator.py -> graph.py, which this satellite-only
harness has no business doing), so the isolation is simpler: just prepend
agents/satellite/ to sys.path for the pipeline call, and separately prepend
backend/ to sys.path for the DB read-back, without ever importing
backend/router.py or backend/orchestrator.py.

**`forced_satellite_type`** (optional): validates the specific satellite path
a reference event was chosen to test, bypassing `select_satellite`'s real
cloud-aware decision for this run only (see forced_satellite_override.py's
module docstring for the full safety argument). When set, the run's
`selection_reason` in the persisted satellite_results row is always exactly
"harness_forced_selection" — a value the real pipeline can never produce —
so a forced result is never mistaken for a real one in any stored evidence.
BASELINE_REPORT.md §3 documented that every baseline run's clear-sky
historical date defeated the harness's intent to test S1, for two different
reasons across two sessions; this parameter is the fix flagged there.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
SATELLITE_DIR = REPO_ROOT / "agents" / "satellite"
BACKEND_DIR = REPO_ROOT / "backend"


def _ensure_satellite_on_path() -> None:
    p = str(SATELLITE_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


def _ensure_backend_on_path() -> None:
    p = str(BACKEND_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_backend_db():
    """Import backend/db.py as a bare module ('db'), the same way
    backend/router.py itself does (`from db import ...`). Loaded on demand
    (not at module import time) so a harness --dry-run or a metrics-only
    invocation never requires NEON_DATABASE_URL to be configured.

    backend/db.py calls a bare, cwd-relative `load_dotenv()` at import time
    (matching how every agent's own entry point behaves per root CLAUDE.md's
    per-agent .env note) — since this harness runs from tests/validation/,
    not backend/, that bare call would silently find no .env and leave
    NEON_DATABASE_URL unset. Load backend/.env explicitly BEFORE importing
    db.py, with override=False so a variable already present in this
    process's environment (e.g. an operator explicitly pointing the harness
    at a different DB) still wins — same convention CLAUDE.md documents for
    the pipeline agents' own .env loading.
    """
    _ensure_backend_on_path()
    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env", override=False)
    import db as backend_db  # type: ignore  # noqa: E402

    return backend_db


def run_pipeline_for_event(
    location: str,
    disaster_type: str,
    as_of: datetime,
    search_window_days: int = 5,
    min_coverage_percent: Optional[float] = None,
    max_scenes: Optional[int] = None,
    max_download_gb: Optional[float] = None,
    max_search_seconds: Optional[float] = None,
    forced_satellite_type: Optional[str] = None,
    event_peak_utc=None,
    force_single_baseline_scene: bool = False,
) -> dict:
    """Invoke the real satellite pipeline end to end:

      1. INSERT a real disaster_events row (create_disaster_event) — the same
         write /analyze performs, so the DB read-back in step 4 has a real
         parent row to join against, not a fabricated fixture.
      2. Run the REAL `agent._run_pipeline_sync` (via agent.run_pipeline's
         sync body) with the scene-search clock frozen at `as_of` (the
         reference product's acquisition instant) — see
         sentinel_clock_patch.py for why the freeze is necessary. This is the
         exact function agents/satellite/node.py's satellite_node calls in
         production; nothing about the science path is mocked. It persists
         its own satellite_results row internally
         (agent.py:_persist_satellite_result, retried up to 3x) before ever
         returning "complete" — this harness does not write that row itself.
      3. Mirror the disaster_events status update backend/graph.py's satellite
         node + orchestrator would perform on this outcome (update_event_status),
         so a human reading disaster_events afterwards sees an honest status,
         not a row stuck at "received" forever.
      4. Read the result BACK from the DB via `db.get_event_evidence` — the
         exact function GET /results/{id}/evidence calls — rather than
         trusting the in-memory dict step 2 returned. Any field present in
         the in-memory result but absent from the DB row (e.g. confidence
         fields that predate the durable-evidence-trail migration on an
         older schema) surfaces here as a real, reportable gap, not a
         silently-inherited value.

    Returns a dict with the DB-sourced evidence payload under "evidence",
    plus harness/timing metadata. Never returns the pipeline's raw in-memory
    JSON as the thing to be scored — only as `_raw_pipeline_result`, kept for
    debugging when the DB and the in-memory view disagree.
    """
    _ensure_satellite_on_path()

    # Imported only after sys.path is set up, and imported fresh each call so
    # a prior harness run's frozen-clock patch can't leak into this one via a
    # cached module-level import of `datetime` elsewhere.
    import agent as satellite_agent  # type: ignore
    from sentinel_clock_patch import frozen_sentinel_clock  # local harness module
    from forced_satellite_override import forced_satellite  # local harness module
    from aoi_pin import pinned_aoi  # local harness module
    from post_peak_scene_floor import post_peak_floor  # local harness module
    from force_single_baseline_scene import (  # local harness module
        forced_single_baseline_scene,
    )
    import contextlib

    satellite_override_cm = (
        forced_satellite(forced_satellite_type)
        if forced_satellite_type
        else contextlib.nullcontext()
    )
    # DIAGNOSTIC ONLY (2026-07-30) — isolates whether a 3-scene median
    # baseline dilutes signal vs. a simple 1-scene comparison. See
    # force_single_baseline_scene.py's module docstring for why this exists.
    baseline_scene_cm = (
        forced_single_baseline_scene()
        if force_single_baseline_scene
        else contextlib.nullcontext()
    )
    # Post-peak floor: for a flood the useful post-event scene is the first
    # same-orbit pass at/after the peak. The search window looks BACKWARDS
    # from as_of, so without this it can select pre-flood imagery and the
    # change detection then correctly reports no change (run 4 did exactly
    # that: 2023-09-01 selected for a 09-05/06 event). See
    # post_peak_scene_floor.py.
    peak_floor_cm = (
        post_peak_floor(event_peak_utc)
        if event_peak_utc is not None
        else contextlib.nullcontext()
    )

    backend_db = _load_backend_db()

    event_id = str(uuid.uuid4())
    params = satellite_agent.ProcessDisasterInput(
        event_id=event_id,
        location=location,
        disaster_type=disaster_type,
        magnitude=None,
        raw_message=f"{disaster_type} in {location}",
        min_coverage_percent=min_coverage_percent,
        max_scenes=max_scenes,
        max_download_gb=max_download_gb,
        max_search_seconds=max_search_seconds,
    )

    import asyncio

    # backend/db.py's get_pool() caches a single module-level asyncpg.Pool.
    # A pool's connections are bound to the event loop that created them, so
    # calling asyncio.run() more than once for the same event (each call
    # spins up and tears down its OWN loop) leaves the cached pool holding
    # connections against a now-closed loop — the second asyncio.run() call
    # then fails with "cannot perform operation: another operation is in
    # progress" / "Event loop is closed" the moment it tries to use the
    # pool (confirmed live during this harness's first real run). All DB
    # calls for one event must therefore share exactly one asyncio.run(),
    # wrapping create_disaster_event -> the sync pipeline call -> the status
    # update -> the evidence read-back in a single coroutine.
    async def _drive() -> dict:
        # backend/db.py caches its asyncpg.Pool at module level, and a
        # harness run scores several events per process (run_baseline.py's
        # main loop) — each via its OWN asyncio.run() call here. A pool's
        # connections are bound to the event loop that created them, so once
        # THIS function's asyncio.run() returns and closes its loop, the
        # cached pool becomes unusable for the NEXT event's asyncio.run()
        # (fresh loop). The whole body is wrapped in try/finally so the pool
        # is always closed before returning — success or exception — forcing
        # backend_db.get_pool() to lazily recreate a fresh pool on the next
        # event's own loop. Confirmed live: the harness's first real
        # multi-event run reproduced "cannot perform operation: another
        # operation is in progress" on event 2 without this.
        try:
            await backend_db.create_disaster_event(
                event_id=event_id,
                location=location,
                disaster_type=disaster_type,
                magnitude=None,
            )
            await backend_db.update_event_status(event_id, status="processing", step="satellite")

            started = time.monotonic()
            # pinned_aoi: boundary resolution replayed from the on-disk cache
            # (tests/validation/cache/aoi/) so Nominatim drift can never move
            # the AOI between runs of the same event — see aoi_pin.py for the
            # live confound this closed (Kanalia's reference area shifted
            # 16.082 -> 20.308 km² across two harness sessions).
            with frozen_sentinel_clock(as_of), satellite_override_cm,                     peak_floor_cm, baseline_scene_cm, pinned_aoi():
                # _run_pipeline_sync is blocking (it does its own internal
                # asyncio.run() for the DB persist and synchronous I/O for
                # everything else) — run it in a worker thread so it doesn't
                # block this coroutine's own event loop indefinitely,
                # matching how agents/satellite/agent.py's own run_pipeline
                # offloads it via asyncio.to_thread in production.
                raw_result = await asyncio.to_thread(satellite_agent._run_pipeline_sync, params)
            elapsed_seconds = time.monotonic() - started

            result = json.loads(raw_result)
            status = result.get("status")

            # Mirror satellite_node's own state transition
            # (agents/satellite/node.py): "complete" -> hand off to hazard
            # (which this satellite-only harness never runs, so status stays
            # "processing" rather than claiming a downstream stage occurred
            # — "complete" is graph.py's own value for "all 4 stages
            # finished", which is not true here); anything else -> failed.
            # Keeps disaster_events honest for a human inspecting the DB
            # directly. step is VARCHAR(20) on live Neon (schema.sql's
            # documented VARCHAR(50) does not match live — a schema-drift
            # instance this harness run surfaced, noted in the baseline
            # report rather than silently worked around further than
            # fitting the column).
            await backend_db.update_event_status(
                event_id,
                status=("failed" if status != "complete" else "processing"),
                step=("failed" if status != "complete" else "sat_only_done"),
            )

            evidence_row = await backend_db.get_event_evidence(event_id)

            return {
                "event_id": event_id,
                "as_of": as_of.isoformat(),
                "search_window_days": search_window_days,
                "elapsed_seconds": elapsed_seconds,
                "bytes_downloaded": result.get("bytes_downloaded"),
                "_raw_pipeline_status": status,
                "_raw_pipeline_error": result.get("error") or result.get("reason"),
                "_raw_pipeline_result": result,
                "evidence": evidence_row,
            }
        finally:
            await backend_db.close_pool()

    return asyncio.run(_drive())
