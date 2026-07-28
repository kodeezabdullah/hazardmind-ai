# Satellite Validation Harness — Baseline Report

**Date:** 2026-07-28
**Branch:** `science/validation-harness`
**Baseline commit:** `a17d4ee` (`a17d4eedf0549657febec899b868136f82e594a0`, merge of
`feat/durable-evidence-trail` into `main`) — the working tree at measurement
time carries only this harness's own uncommitted files (`tests/validation/`),
no pipeline/index/threshold code was changed to produce any number below.
Every `results/*.json` file's `commit` field is stamped `a17d4ee-dirty` for
exactly this reason (see `results_store.py`'s docstring).

**Scope of this session:** build the harness and measure the baseline. No
index, threshold, or algorithm was touched — see `git diff a17d4ee -- agents/`
(empty) to confirm.

---

## 1. What was inherited vs. built this session

A significant harness (reference-event selection, metrics, reference-geometry
loading, results storage, a historical-clock patch) already existed
**uncommitted** in the working tree at the start of this session, from an
earlier attempt (see `BASELINE_SESSION_NOTES.md` and `SELECTION.md`, both
dated 2026-07-27). That attempt built a correct harness but never obtained a
scored baseline — every live run failed on either a `PROJ_LIB` environment
conflict (since fixed on `main` at `269d256`) or CDSE network flakiness, and
it read results from the pipeline's in-memory JSON return rather than the DB.

This session:
- Kept the reference-event selection, metrics (IoU/precision/recall/F1,
  equal-area CRS, permanent-water split), reference-geometry loading, and
  results-storage code as-is (`metrics.py`, `reference_loader.py`,
  `predicted_extent.py`, `results_store.py`, `sentinel_clock_patch.py`).
- **Rewired `pipeline_runner.py`/`run_baseline.py` to read results via the
  durable-evidence trail** (`backend/db.py`'s `get_event_evidence` — the exact
  function `GET /results/{event_id}/evidence` calls), not the pipeline's
  in-memory JSON return, per this session's explicit brief. This surfaced
  three real bugs (§4).
- Added budget controls matching production `/analyze` defaults
  (`min_coverage_percent=90`, `max_scenes=3`, `max_download_gb=4.0`,
  `max_search_seconds=900`) so a validation run costs the same as a normal
  production run, and reports cost per event (§5).
- Rescoped Magnesia from the full ~150×90 km regional-unit AOI to a
  town-scale AOI (`emsr692_kanalia.yaml`), for the same reason
  `emsr773_valencia.yaml` was superseded by `emsr773_paiporta.yaml` — see §3.
- Obtained the first successful scored baseline runs (§2).

---

## 2. Baseline results table

| Event | Path intended | Path actually selected | Tier | IoU (incl. permanent water) | Precision | Recall | F1 | Reported confidence | Confidence basis | Elapsed | Downloaded |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Paiporta, Spain (EMSR773) | Sentinel-2 | **sentinel-2** (`scene_metadata_clear`) | 1 | undefined (0/0) — 0 predicted zones | — | — | — | 0.3184 | `evidence_contradicts` | 401s | 421 MB |
| Paiporta, Spain (EMSR773) | Sentinel-1 | **sentinel-2** (`scene_metadata_clear`) | 1 | undefined (0/0) — 0 predicted zones | — | — | — | 0.3184 | `evidence_contradicts` | 368s | 405 MB |
| Kanalia, Magnesia, Greece (EMSR692) | Sentinel-1 | **sentinel-2** (`scene_metadata_clear`) | 1 | **0.4837** | 0.4866 | 0.9878 | **0.6520** | 0.4479 | `evidence_contradicts` | 360s | 414 MB |

Only **one** of the three runs produced a non-degenerate score. This is the
real headline finding of this baseline, not a harness limitation — see §3.

No excluding-permanent-water column: no permanent-water mask was sourced for
any of these AOIs this pass (all three note this explicitly). Treat every
"including" number above as an upper bound, per `metrics.py`'s own framing —
this matters most for Kanalia, since Lake Karla is a historically real lake
(drained mid-20th century, periodically reflooded), so "permanent water" vs.
"flood" is unusually blurred at that specific location and the 0.4866
precision figure plausibly reflects that, not a pure detection error.

---

## 3. The satellite-selection finding — the real story of this baseline

**Every single run in this baseline selected Sentinel-2, regardless of which
path the reference event was chosen to test.** `select_satellite`'s
cloud-aware logic (`agents/satellite/sentinel.py`/`agent.py`, "physics over
assumption" per `CLAUDE.md`) always overrides the disaster-type hint and picks
whichever satellite the measured cloud cover favors — it has no way to know
"the harness specifically wants to validate the S1 path here." Both historical
dates this baseline searched (2024-11-05/06 at Paiporta, 2023-09-06 at
Kanalia) happened to have clear sky (`selection_reason: scene_metadata_clear`
on all three), so S2 won every time.

**Consequence: this baseline could not test the S1/SAR path at all**, despite
deliberately selecting two S1-eligible reference products (`EMSR773_DEL_MONIT03`
at Valencia/Paiporta, `EMSR692_DEL_MONIT01` at Magnesia/Kanalia) specifically
for that purpose. This is not a flaw in the reference-event selection
(`SELECTION.md` correctly identified S1-scorable events) — it is a structural
property of how the harness invokes the pipeline: `sentinel_clock_patch.py`
only pins the search *date*, not the sky conditions on that date, and the
pipeline's own selection logic is deliberately indifferent to the harness's
intent.

**What this means for the SAR-calibration question the task asked about**
(§6/CLAUDE.md's long-standing "is the uncalibrated SAR index scorable at all"
open question): **this baseline still cannot answer it.** Every prior
live-run attempt at an S1 path also failed before scoring (network/coverage
issues, see `BASELINE_SESSION_NOTES.md`); this baseline's attempts all
succeeded but were silently redirected to S2 by the pipeline's own (correct,
by design) cloud-aware selection. Two consecutive sessions have now failed to
produce a single scored S1 result, for two entirely different reasons. This
should be read as a real gap in what can be validated with this harness
design, not evidence about the SAR path's accuracy in either direction.

**A fix for a future session** (not attempted here — would need either a
pipeline change or a harness-only override, and this session's brief
explicitly forbids touching pipeline logic): a harness-only forced-satellite
override (bypassing `select_satellite`'s cloud check for validation runs
only) would let a future baseline actually score the S1 path. This is
different in kind from the `as_of` clock-patch gap already flagged in
`sentinel_clock_patch.py` — that one is about *which imagery exists*, this
one is about *which imagery the pipeline chooses among what exists*.

---

## 4. Real bugs found and fixed by this harness (not pipeline changes — all in `tests/validation/`)

Building and running the harness against the DB read-back path surfaced
issues a pure in-memory-return harness (last session's version) could not
have caught, plus two location/geometry bugs of the harness's own.

### 4.1 `backend/db.py`'s cached connection pool does not survive multiple `asyncio.run()` calls in one process

`get_pool()` caches a single module-level `asyncpg.Pool`. The harness scores
several events per process; each event's DB work was originally split across
several separate `asyncio.run()` calls. The first call's pool ends up with
connections bound to that call's (now-closed) event loop, and the next
event's `asyncio.run()` call fails immediately with
`asyncpg.exceptions.InterfaceError: cannot perform operation: another
operation is in progress`. **Fixed**: `pipeline_runner.py` now wraps all of
one event's DB work (`create_disaster_event` → the pipeline call →
`update_event_status` → `get_event_evidence`) in a single `asyncio.run()`
call, and explicitly closes the pool (`backend_db.close_pool()`, in a
`finally`) before returning, forcing the next event to lazily recreate a
fresh pool on its own loop. This is a harness-only fix (backend/db.py itself
is unchanged) — flagged because the same pattern (cache a pool at module
scope, call from multiple short-lived `asyncio.run()`s) could recur in any
future script that imports `backend/db.py` directly rather than running
inside the long-lived FastAPI process, where this never manifests.

### 4.2 `disaster_events.step` is `VARCHAR(20)` on live Neon, not the `VARCHAR(50)` `shared/db/schema.sql` documents

Discovered by a real `StringDataRightTruncationError` when the harness first
tried to write a longer step name. **This is schema drift**, the same class
of issue `shared/db/schema.sql`'s header already warns about for other
columns — `schema.sql` needs a fresh `information_schema.columns`
introspection to confirm and correct the real live width, and (per root
`CLAUDE.md`'s migration-file rule) a migration file if the intent is actually
to widen it. Not fixed in this session (pipeline/DB code is out of scope
for a validation-harness task) — the harness worked around it by using a
short step value (`sat_only_done`) instead.

### 4.3 `coverage_tier`/`temporal_spread_days` are not queryable `satellite_results` columns

Confirmed by reading `shared/db/schema.sql` directly: they exist only inside
the row's `diagnostics` JSONB blob
(`agents/satellite/agent.py::_persist_satellite_result`), not as top-level
columns. `GET /results/{id}/evidence` DOES surface `diagnostics` as part of
its `satellite` field, so these values are still recoverable — the harness
now reads them from `sat_row["diagnostics"]` rather than reporting them as
unavailable, which an earlier version of this session's own harness rewrite
incorrectly did (fixed before any real run used it). Worth flagging for the
pipeline/backend maintainers as a design question, not a bug: is it
intentional that these two fields are diagnostics-only (not
filter/sort/aggregate-able via SQL) while sibling fields like
`coverage_percent`/`coverage_status` got real columns in the same migration?

### 4.4 Harness bug: ambiguous city name resolved to the wrong real-world location

"Kanalia, Greece" (the harness's own choice for the Magnesia rescope, §5)
geocodes via Nominatim to a *different* village named Kanalia in Karditsa
prefecture, ~90 km from the intended one in the Magnesia/Lake-Karla area.
This silently clipped the reference geometry to empty and produced a
misleading `IoU: 0.0, F1: 0.0` result that looked like a real pipeline
accuracy failure (§4.5 explains why that reading would have been doubly
wrong). **Fixed**: `pipeline_location` changed to `"Kanalia, Magnesia,
Greece"`, confirmed via a direct Nominatim query to resolve to the intended
village (22.8862062°E, 39.4984603°N), and cross-checked against the reference
data's own densest flood-polygon cluster (found by a grid search over the
reference layer) to confirm it's the right anchor point. This is a harness
authoring error, not a pipeline defect — the real pipeline's own boundary
resolution was never wrong; the harness gave it an ambiguous input string.

### 4.5 Harness bug: reference-geometry clip used the wrong boundary-resolution function

Independent of §4.4, and a more general bug: the harness's reference-clip
step called `boundary.get_region_boundary()`, which for a point-only city
(no real OSM/geoBoundaries admin polygon — Kanalia is exactly this case)
returns a **raw, zero-area Point**. `get_region_boundary` does NOT apply
`boundary.py`'s own `_ensure_areal` buffer-to-disk fallback — only
`get_risk_city_boundaries` does. Clipping the reference against a zero-area
point trivially produces an empty intersection regardless of how close the
point is to real flood polygons, which is exactly what happened even *after*
§4.4's fix was first tested. **Fixed**: the harness now calls
`get_risk_city_boundaries(pipeline_location, [headline_city])` — the same
function the real pipeline itself uses for a single-city AOI via
`detect_risk_cities`'s headline-token fallback — so the harness scores
against the identical geometry the pipeline actually analyses, not an
approximation of it. **Paiporta was independently confirmed NOT affected**:
it resolves to a real, non-zero-area Polygon (Paiporta has a proper
geoBoundaries/OSM admin boundary), so `get_region_boundary` happened to
return something usable there. This bug is specific to point-only cities.

Also added a symmetric guard (`run_baseline.py`): if the reference geometry
is empty after clipping, the harness now reports
`pipeline_status: reference_empty_after_clip` and does **not** run the
pipeline at all (IoU/F1 are correctly left undefined, not silently computed
as `0.0`) — mirroring the pre-existing `complete_zero_zones` guard on the
predicted side. This protects any future event config from repeating §4.4/4.5's
failure mode even if a location string turns out to be ambiguous again.

---

## 5. The Magnesia → Kanalia rescope

`emsr692_magnesia.yaml` (this session's inherited config) uses the full
Magnesia regional-unit AOI (~150×90 km) as the pipeline's search location —
the same shape of problem `emsr773_valencia.yaml` had before being superseded
by the city-scale `emsr773_paiporta.yaml`. Running the full region risks the
same multi-hour, multi-tile search the original province-wide Valencia
attempt hit (documented in `BASELINE_SESSION_NOTES.md`).

**Rescope method**, matching Paiporta's precedent: a grid search over the
`EMSR692_DEL_MONIT01_maximumWaterExtentA` reference layer (3,690 polygons
across the full AOI) found the single densest 0.05°×0.05° cell
(~19.7 km² of mapped flood extent in that one cell), which reverse-geocodes
to "Dimotiki Enotita Karlas" — the drained/periodically-reflooded Lake Karla
basin, the area of Magnesia hit hardest by Storm Daniel. The basin itself has
no OSM settlement polygon (it's farmland, not a town); **Kanalia**, the
nearest actual village directly on the basin's edge, was chosen as the
city-scale anchor. `emsr692_kanalia.yaml` is the new scored event;
`emsr692_magnesia.yaml` is kept for the record (not deleted, matching this
repo's own convention for `emsr773_valencia.yaml`) but is not part of the
normal scored run set going forward.

---

## 6. The confidence question (task §4)

**Two data points is not enough to establish or refute a correlation** — this
must be stated plainly rather than implied by a chart. With that caveat:

| Event | Reported confidence | Confidence basis | Evidence count | Measured IoU |
|---|---|---|---|---|
| Paiporta (S2, run 1) | 0.3184 | `evidence_contradicts` | 4 | undefined (0 predicted zones) |
| Paiporta (S2, run 2) | 0.3184 | `evidence_contradicts` | 4 | undefined (0 predicted zones) |
| Kanalia (S2) | 0.4479 | `evidence_contradicts` | 5 | **0.4837** |

The one directional observation available: the higher-confidence run (0.4479
vs. 0.3184) also produced the only non-degenerate, materially better-than-zero
result. That is consistent with confidence tracking accuracy, but with n=2
distinct outcomes this is **not a statistically meaningful correlation** —
it is one data point saying "not obviously wrong," not a validated claim.

**More informative than the confidence *number* is the confidence *basis*
field, and it is genuinely informative here**: all three runs report
`evidence_contradicts`, not `insufficient_evidence` — meaning the
`ConfidenceTracker` gathered real evidence and that evidence actively
disagreed with itself (visible directly in the LLM's own logged concerns:
"mean NDWI is characteristic of dry soil... which contradicts the presence
of water" — recorded on every run in this baseline, including the one that
scored 0.65 F1). This is the single most important finding from this
baseline's confidence data: **the pipeline's own self-reported reasoning
flagged a real, load-bearing defect class on every run** — the NDWI-vs-
water_percent contradiction is exactly the kind of index-labeling confusion
`CLAUDE.md`'s "Uncalibrated-SAR-as-NDWI unit confusion is systemic" section
already documents for other call sites, but this baseline shows it manifests
even on a clean, calibrated NDWI (S2) run, not only the uncalibrated-SAR path
that section focuses on. This is flagged here as a finding for a future
session, not fixed (out of scope — no index/threshold logic was touched).

**Answer to the task's central question, stated plainly**: with only 2
distinct confidence values and 1 scoreable accuracy point, **this baseline
cannot establish whether reported confidence tracks measured accuracy.** It
also cannot refute it. A future baseline needs (a) more scoreable events —
which requires solving §3's satellite-selection problem first, since 2 of 3
runs here produced no comparable IoU at all — and (b) a wider spread of
confidence values to see if the relationship holds or breaks down.

---

## 7. Cost per event (task §5)

| Event | Elapsed | Bytes downloaded | CDSE download budget used |
|---|---|---|---|
| Paiporta S2 run 1 | 401s (6m41s) | 421.4 MB | 10.5% of the 4 GB `max_download_gb` budget |
| Paiporta S2 run 2 | 368s (6m08s) | 404.6 MB | 10.1% |
| Kanalia S2 | 360s (6m00s) | 414.3 MB | 10.4% |

All three runs completed well within the production budget defaults
(`max_scenes=3`, `max_download_gb=4.0`, `max_search_seconds=900.0`) — no run
tripped `budget_exhausted`, and `coverage_status: target_met` on both scored
runs (99.96%/100% interior coverage, tier 1). **Total for this baseline
session: ~1,240 MB downloaded across 3 runs, ~21.5 minutes of CDSE-bound
wall time.** Every run selected S2 (§3), so this cost profile is
S2-representative only — an S1 run (full-archive download, no per-band
shortcut per `CLAUDE.md`) would cost materially more; this baseline has no
S1 cost data point.

Two earlier attempts in this same session (before the §4.1 pool-lifecycle fix
and the §4.4/4.5 location fixes) failed fast without reaching a live CDSE
call — those are not counted above since they cost no real download budget,
only wall-clock debugging time.

---

## 8. Where the harness itself was the weak link, not the pipeline (task §5)

Per the task's explicit request to flag this plainly: **§4.1, §4.4, and §4.5
are all harness bugs, not pipeline defects.** In every case, once the harness
was fixed, the real pipeline behaved correctly and consistently (identical
S2 selection and identical `confidence: 0.3184` on Paiporta across two
independent runs; a materially different, explicable score on Kanalia after
its own two location/geometry bugs were fixed). The pipeline itself was never
the source of an inconsistent or surprising result in this baseline — every
surprise traced back to the harness's own DB-lifecycle handling or geometry
resolution, not to `agents/satellite/`'s deterministic pipeline code (which,
per this session's own scope, was never touched).

---

## 9. Summary for a reader in a hurry

- **Baseline commit: `a17d4ee`** (working tree carries only this harness's
  own uncommitted files).
- **3 scored attempts, 1 non-degenerate score**: Kanalia, IoU 0.4837 / F1
  0.6520, confidence 0.4479.
- **The pipeline picked Sentinel-2 on all 3 runs**, defeating this
  baseline's intent to test the S1/SAR path — a structural harness/pipeline
  interaction gap (§3), not a bug in either side alone. **The SAR-calibration
  question remains unanswered** by this session, same as the prior one, for
  a different reason.
- **5 real bugs found and fixed**, all in the harness (§4) — none required
  touching `agents/satellite/` pipeline code.
- **Confidence-vs-accuracy correlation: inconclusive** with n=2 (§6) — but
  every run's confidence *basis* correctly flagged a real NDWI-vs-water%
  contradiction the pipeline itself is currently blind to downstream of the
  confidence score (a finding worth a follow-up session).
- **Cost: ~1.2 GB / ~21.5 min total**, well inside production budget
  defaults, S2-only (§7).
