# Satellite Validation — Session Notes (2026-07-27)

**Status: harness built and validated; no successful live baseline score
obtained this session.** Every live pipeline attempt failed for a real,
diagnosed reason — none were silently wrong, and none were mysteries. This
document records what was learned so the baseline measurement itself (the
original task's step 3) is not lost when picked back up.

---

## What works, confirmed

- `tests/validation/metrics.py`, `results_store.py`, `reference_loader.py`,
  `predicted_extent.py`, `pipeline_runner.py`, `sentinel_clock_patch.py`,
  `run_baseline.py` — the full harness, dry-run validated end to end
  against both EMSR773 (Valencia/Paiporta) and EMSR692 (Magnesia) reference
  data. Reference geometry download, parsing, and equal-area reprojection
  all work correctly (see `SELECTION.md` for event selection detail).
- The clock-patch approach (`sentinel_clock_patch.py`) for pointing the
  pipeline's scene search at a historical date is confirmed sound — it
  successfully drove real CDSE searches anchored to the 2023/2024 event
  dates, not "today."
- City-scale rescoping (`emsr773_paiporta.yaml`, clipping the province-wide
  EMS reference polygon down to Paiporta's real town boundary) is confirmed
  working — `boundary.py`'s resolver correctly returns a true town-scale
  polygon (~2.4km x 2.7km bbox) for "Paiporta, Spain", not the province.

## Three real bugs found this session (not pipeline-logic changes, all
## either fixed in the harness or flagged for a future pipeline session)

### 1. `PROJ_LIB` environment conflict — silently broke every live run for ~3 hours

This machine has a system-wide PostgreSQL/PostGIS install whose `PROJ_LIB`
env var pointed rasterio/pyproj at PostGIS's own (incompatible, older)
`proj.db`. Every reprojection call in the pipeline (`clip_to_polygon`'s
WGS84->UTM warp, `_polygon_area_km2`'s EPSG:6933 area calc) failed silently,
logged as `PROJ: proj_identify: ... DATABASE.LAYOUT.VERSION.MINOR = 2
whereas a number >= 6 is expected`. The tiered coverage search has no way to
distinguish "this scene doesn't cover the AOI" from "the clip call itself is
broken" — it just recorded 0% coverage and moved to the next candidate,
forever, across all 4 tiers, every retry, silently.

**Fixed in the harness**: `run_baseline.py` now force-sets `PROJ_LIB` to
rasterio's own bundled `proj_data` directory at process start
(`_fix_proj_lib()`), confirmed via an isolated test (WGS84->UTM transform +
EPSG:6933 resolution both succeed cleanly with the fix). This is an
environment fix, not a pipeline logic change — the pipeline's own code is
untouched.

**Not yet fixed in the pipeline itself**: the pipeline has no way to detect
"reprojection is silently broken" vs. "this scene has no valid overlap" —
both currently look identical (0% coverage, retry the next scene). A future
session should consider a startup self-check (transform a known point,
verify the round-trip) that fails fast and loud instead of degrading into
an unbounded retry loop.

### 2. No historical-date search seam in `sentinel.py`

`search_imagery`'s date window is `datetime.now(timezone.utc) - timedelta(days=date_range)`
— hardcoded to "now," with no parameter, env var, or other injection point
anywhere in the agent. This means the production pipeline can only ever
search recent imagery; it cannot be pointed at a historical date to validate
against a past event.

**Worked around in the harness** (`sentinel_clock_patch.py`): patches
`sentinel.datetime` via `unittest.mock` to freeze "now" at the reference
event's own acquisition timestamp, for the duration of the harness's
pipeline call only. This is a narrow, harness-only workaround — the
underlying pipeline code and its date-window arithmetic are unchanged.

**Not fixed in the pipeline itself**: if retrospective/historical validation
becomes a recurring need (which this task suggests it will), the pipeline
would benefit from an explicit `as_of` parameter threaded through
`ProcessDisasterInput` -> `search_imagery`, rather than relying on a
monkeypatch. Flagged, not implemented — out of scope for a
transport/correctness pass.

### 3. Zero-tolerance 100% coverage requirement compounds badly with network flakiness — the actual cause of tonight's multi-hour runs

`processor.py`'s `compute_coverage()` requires `interior_coverage_percent ==
100.0` exactly (processor.py:1876) — there is no partial-credit threshold.
Any gap at all (a cloud-shadow pixel, a swath edge, a single row at the AOI
boundary) fails the whole attempt and forces the tiered search to pull in
another scene, escalating through all 4 tiers (same-orbit same-date -> +-3d
-> +-7d -> +-14d any-orbit) if needed.

This is a **deliberate, documented design choice** (`agents/satellite/CLAUDE.md`'s
BUG 3/BUG 2 history) made after a prior incident where a partial-coverage
result was silently accepted and reported as complete. The fix at the time
swung to the opposite extreme: never accept anything less than perfect. But
satellite imagery essentially never covers an arbitrary polygon at exactly
100% due to cloud, nodata edges, and swath geometry — so on a city as small
as Paiporta (2.4km x 2.7km, well within a single Sentinel-1 swath), the
search still needed multiple candidate scenes and reached the widest tier
before being killed, and every scene download that failed due to CDSE's
flaky connection (`IncompleteRead`/`ConnectionBroken`, observed repeatedly
this session on multi-GB Sentinel-1 archives, which have no per-band
download shortcut per CLAUDE.md's own documentation) compounded directly
into "try yet another scene," not just "retry this one."

**Net effect observed live tonight**: a single small-town AOI, with a
network connection dropping roughly 1 in 3 large-file downloads, took over
80 minutes and reached the pipeline's last-resort tier without ever
producing a scoreable result, before being killed by the user.

**Not fixed this session** (would be a pipeline-logic/threshold change,
explicitly out of scope for this validation-harness task). Flagged plainly
as the most actionable finding from tonight: a coverage tolerance (e.g.
99.5% or a documented small-gap allowance with the gap geometry surfaced,
rather than 100.0% exactly) would likely eliminate most of the
retry-cascade behavior observed, independent of any network issue.

## What was NOT obtained this session

- No successful `metrics.py` score (IoU/precision/recall/F1) for any event.
- No confidence-vs-accuracy comparison (needs a successful run to compare
  against).
- No confirmation of whether the SAR/S1 path produces anything scorable in
  practice — every S1 attempt tonight failed on network/coverage grounds
  before reaching the point of producing a result to score.

## Recommended next steps (not started this session)

1. Decide whether to fix finding #3 (coverage tolerance) before attempting
   another live baseline run — every symptom observed tonight traces back
   to it, and retrying without addressing it risks the same outcome.
2. If proceeding without a pipeline fix: re-attempt Paiporta (already
   correctly scoped and dry-run validated) at a time when the CDSE
   connection is more stable, ideally with a lower per-scene timeout so a
   single flaky scene fails fast rather than consuming the full outage
   budget.
3. Magnesia (S1-only, `emsr773_paiporta.yaml`'s sibling
   `emsr692_magnesia.yaml`) has not yet been attempted with the PROJ_LIB fix
   in place — its earlier attempts (before the fix) are not representative.
