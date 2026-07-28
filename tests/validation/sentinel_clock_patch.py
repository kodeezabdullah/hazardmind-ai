"""Pins agents/satellite/sentinel.py's scene-search clock to a historical
instant so the REAL pipeline can be run against a historical EMS event.

**Why this exists (read before touching it).** `sentinel.search_imagery`
(and `_recency_weight`, `select_satellite`'s cloud peek) all call
`datetime.now(timezone.utc)` directly and compute `start = now - date_range`
— there is no parameter, env var, or injection seam anywhere in the agent to
search a window around an arbitrary historical date. Confirmed by grep: the
only three `datetime.now(timezone.utc)` call sites in the satellite agent are
inside `sentinel.py` itself (agents/satellite/CLAUDE.md and ANALYSIS.md
don't mention this gap — it surfaced during this validation-harness build).

This is a REAL correctness gap in the pipeline (it means the deployed system
can only ever analyse "recent" imagery, which is fine for its actual
production use case — reacting to a disaster now — but makes retrospective
validation against historical ground truth otherwise impossible without
either (a) editing sentinel.py, which this task explicitly forbids this
session, or (b) patching the clock the module reads from the outside.

This module does (b), narrowly: it patches `sentinel.datetime` (the name
bound inside sentinel.py's own module namespace, per `unittest.mock.patch`'s
standard "patch where it's looked up" rule) so `datetime.now(timezone.utc)`
returns a fixed instant for the duration of a `with` block. Nothing else
about sentinel.py's logic changes — the same date-window arithmetic,
same-orbit tiering, and coverage search all run unmodified; they just believe
"now" is the reference event's acquisition time instead of wall-clock time.

Not a permanent fix. Flagged in the validation report as a pipeline gap the
science-phase maintainers should decide whether to close for real (e.g. an
optional `as_of` parameter threaded through `ProcessDisasterInput` ->
`search_imagery`), separate from this harness's narrow workaround.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from unittest.mock import patch


class _FrozenDateTime(datetime):
    """A datetime subclass whose .now()/.utcnow() always return the frozen
    instant, while every other datetime API (arithmetic, comparison,
    strftime, construction) behaves exactly like the real class — sentinel.py
    calls datetime(...), timedelta arithmetic, and .strftime() on the results,
    all of which must keep working unchanged."""

    _frozen_at: datetime

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls._frozen_at.replace(tzinfo=None)
        return cls._frozen_at.astimezone(tz)

    @classmethod
    def utcnow(cls):
        return cls._frozen_at.replace(tzinfo=None)


@contextlib.contextmanager
def frozen_sentinel_clock(as_of: datetime):
    """Patch agents/satellite/sentinel.py's `datetime` so `datetime.now(...)`
    returns `as_of` for the duration of the block. `as_of` must be tz-aware
    (UTC) — matches every call site's `datetime.now(timezone.utc)` usage.
    """
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    frozen_cls = type("_FrozenDateTime", (_FrozenDateTime,), {"_frozen_at": as_of})

    with patch("sentinel.datetime", frozen_cls):
        yield
