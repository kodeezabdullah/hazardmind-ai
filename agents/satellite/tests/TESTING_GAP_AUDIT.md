# Testing gap audit: internals-only vs. orchestration-exercising (2026-07-28)

Follow-up to the CHANGE 6 observability gap (`agents/satellite/CLAUDE.md`'s
"AOI-restricted cloud measurement" section): its 12 tests imported
`processor`/`sentinel` and called the functions directly. They proved
`select_satellite`/`peek_aoi_cloud_percent` correct in isolation without ever
proving the result reaches `agent.py`'s `structured` payload — and it didn't
(`structured["selection_reason"]` read the wrong dict key, `scene_cloud_percent`/
`aoi_cloud_percent` were never copied over at all). The feature passed its
tests and was invisible in production. This audits every suite written during
this hardening effort for the same pattern.

## Method

For each suite: does any test call the agent's real entry point
(`node.py`'s node function, or `agent.py`'s `run_pipeline`/`analyze_hazard`/
`run_report_pipeline`), or does every test import internals and call them
directly? For any field a fix in this effort added, does any test assert the
field survives into the structured result / `PipelineState` / DB row, or only
that the function computing it returns correctly?

## Table

| Suite | Tests exercising orchestration | Tests exercising internals only | Fields verified end-to-end vs. function-level only |
|---|---|---|---|
| `agents/satellite/tests/test_coverage_tolerance.py` | 0 / 33 | 33 / 33 — every test calls `processor.process_satellite_imagery(...)` or `sentinel.select_satellite(...)` directly. `test_budget_params_thread_from_analyze_request` hand-builds `AnalyzeRequest`→`disaster_data`→`PipelineState`-shaped dicts and calls `agent._coverage_budget_kwargs` — a closer simulation, but still never calls `agent.run_pipeline` or `node.py`. | `selection_reason`, `scene_cloud_percent`, `aoi_cloud_percent`, `scl_reused`, `scene_age_days`, `min_coverage_percent`/`max_scenes`/`max_download_gb`/`max_search_seconds` threading — all verified only at the function return value. **None asserted against `agent.py`'s `structured` dict or a DB row** before this session's Fix 1. |
| `agents/satellite/tests/test_correctness_fixes_20260727.py` | 0 / 20 | 20 / 20 | Weaker than internals-only: `test_structured_carries_total_zones_and_scene_id`, `test_structured_carries_artifacts_incomplete`, `test_persist_retries_then_fails_honestly` etc. use `inspect.getsource(agent._run_pipeline_sync)` **and grep the source text** for expected patterns, rather than running the function and inspecting a real `structured` dict. A field could be assigned to the wrong dict key (exactly Fix 1's bug) and a source-text grep for `"total_zones"` would still pass, because the string appears in the file regardless of which dict it's assigned into. `test_scene_id_threaded_into_merged_result` does the same against `processor.process_satellite_imagery`'s source. |
| `agents/hazard/test_hazard_provenance.py` | 0 / 5 | 5 / 5 — four call `analyzer.run_parallel_analysis` directly (patching `analyzer.fetch_gdacs`/`fetch_usgs`/`fetch_slope`/`smart_llm_call` module globals). | `test_evidence_basis_survives_into_db_write_confirmed_by` is the most direct instance of this defect class found: its own docstring says "`agent.write_to_db`'s `confirmed_by` JSONB must carry `evidence_basis`," it imports `agent as hazard_agent`, **but the import is never used** — the test body defines its own local `_confirmed_by` closure that "replicate[s] write_to_db's inner `_confirmed_by` closure logic directly" (the test's own comment) instead of calling the real one. If `write_to_db`'s actual closure drifts from this hand-copied twin, the test keeps passing while the DB row silently stops carrying `evidence_basis`. This is the CHANGE 6 defect class exactly, on the field this session's Fix #3 (hazard provenance) added. |
| `agents/hazard/test_confidence_cap.py` | 0 / 5 | 5 / 5 — all five patch `analyzer.fetch_*`/`smart_llm_call` and call `analyzer.run_parallel_analysis` directly. | The satellite-confidence cap (this session's Fix #3, confidence-cap) is verified only at `run_parallel_analysis`'s return dict — never through `agent.analyze_hazard` (the real pipeline entry, which also runs `_normalise_satellite_payload` and `write_to_db`) or `node.py`. No test confirms the capped confidence value actually reaches `hazard_zones`'s `overall_confidence` column. |
| `agents/satellite/tests/test_index_label_integrity.py` | 0 / 3 | 3 / 3 | `test_validation_input_index_type_matches_result`'s own docstring: "Simulates `agent.py`'s `validation_input` construction contract" — it builds a dict by hand mirroring `agent.py`'s shape and asserts the assertion logic works on the simulated dict, rather than calling `agent.run_pipeline` and inspecting the real `validation_input`/assertion. If `agent.py`'s real dict construction diverges from this simulation (as it did for `selection_reason` in Fix 1), this test would not catch it. |
| `agents/report/test_confidence_aggregation.py` | 0 / 8 | 8 / 8 — all call `calculate_confidence_level`/`_collect_confidence_values` from `db_client.py` directly with hand-built `report` dicts. | `test_satellite_zero_via_dedicated_channel_ONLY_cannot_yield_high`'s own docstring names `agents/report/node.py`'s wiring as the thing it's protecting, then tests a hand-built dict shaped like what that wiring is *supposed* to produce — never invoking `node.py` or `run_report_pipeline` to confirm the wiring itself still does that. |

## Pattern

**0 of 74 tests across the 6 suites call an agent's real entry point**
(`node.py`'s node function, `agent.py`'s `run_pipeline`/`analyze_hazard`, or
`run_report_pipeline`). Every suite imports internal modules
(`processor`/`sentinel`/`analyzer`/`confidence_tracker`/`cross_validator`/
`db_client`) and calls their functions directly. This is uniform, not
occasional — it is how every suite in this hardening effort was written, not
a lapse in one of them.

A secondary, worse variant appears in 2 of the 6 (`test_correctness_fixes_
20260727.py`'s `inspect.getsource` + string-match checks, and both
`test_hazard_provenance.py`'s `_confirmed_by` closure and
`test_index_label_integrity.py`'s `validation_input` simulation): rather than
calling the real orchestration code and inspecting its real output, these
tests either grep the orchestration function's *source text* for an expected
pattern, or hand-copy a piece of its logic and test the copy. Both variants
can diverge from the real code silently — a source-text grep passes as long
as the expected string appears anywhere in the file, regardless of which
dict/branch it ends up in; a hand-copied closure passes as long as nobody
edits the original without remembering to edit the copy.

## Confirmed at-risk fixes from this session (not yet independently verified end-to-end)

- **This session's Fix #3, hazard provenance** (`evidence_basis` →
  `confirmed_by` JSONB) — `test_hazard_provenance.py`'s own test for this
  exercises a hand-copied closure, not `agent.write_to_db`. The real
  `write_to_db` has never been asserted to actually write `evidence_basis`
  into a real (or realistically mocked) DB row.
- **This session's Fix #4, scene age** (`scene_age_days`) —
  `test_coverage_tolerance.py`'s `test_scene_age_days_present_and_correct`
  and `test_old_scene_reduces_confidence_and_appends_anomaly` both stop at
  `processor.process_satellite_imagery`'s return value. Whether
  `scene_age_days` reaches `agent.py`'s `structured` dict (and thus
  `satellite_results`/`PipelineState`) was **not** independently verified by
  any existing test before this session — this audit confirms `agent.py`
  line ~1036 does carry it (`"scene_age_days": result.get("scene_age_days")`),
  but that confirmation came from reading the source during this audit, not
  from a test that would catch a future regression.

## Scope note

Per the task, this audit does not rewrite the suites. `tests/verify_islamabad_fixes.py`
(added this session) is deliberately narrow: it exercises `agent.run_pipeline`
for real (mocking only the network/DB boundary) specifically for the three
fixes in this session's scope (CHANGE 6 field survival, the duplicate-search
fix, and the cross-validator unit fix), to establish that at least one test
in this codebase exercises the real entry point — it does not attempt to
backfill orchestration coverage for the other five findings above.

## Follow-up pass (2026-07-28, same day): field-survival extension

The gap this document identifies was closed for every field the hardening
effort added, and the two actively-misleading test patterns were retired.

**Survival assertions added**, each checked at (a) the agent's structured
result, (b) PipelineState (via a real node-function call, not just an
assumption that "node.py copies the whole dict so it must cross"), and (c)
the DB row (via a real write function call with a faked DB connection that
records the actual SQL parameters, not a hand-copied twin of the write
logic):

- **Satellite** (`tests/test_verify_islamabad_fixes.py`,
  `test_all_hardening_fields_survive_structured_and_state` +
  `test_db_persisted_fields_survive_real_write` +
  `test_gap_fields_survive_success_path_structured_but_not_db`): all 14
  CHANGE-6/BUG-5/islamabad-findings-#4 fields (confidence_basis,
  evidence_count, total_zones, scene_id, artifacts_incomplete,
  failed_artifacts, index_calibrated, index_units, coverage_percent,
  scene_age_days, scl_reused, selection_reason, scene_cloud_percent,
  aoi_cloud_percent) confirmed present in structured + PipelineState via a
  real `agent.run_pipeline()` + real `satellite_node()` call. Of those, only
  total_zones/scene_id/scene_age_days are actual `satellite_results` INSERT
  columns — confirmed via a real `_persist_satellite_result()` call with a
  DB double that records the real INSERT parameters. The other 11 reach
  structured/PipelineState but are NOT DB columns — asserted absent
  explicitly, a real (pre-existing, out-of-scope-to-fix) gap, not a silent
  omission.
- **Satellite gap telemetry — REAL BUG FOUND AND FIXED**: `coverage_status`/
  `gap_count`/`gap_area_km2`/`gap_attribution`/`gap_limited_by` are computed
  by `processor.py`'s `_finish_success` on **every** success path (not just
  the `insufficient_coverage` failure path this document originally
  described), but `agent.py` was only ever copying them into a payload on
  the failure branch — a `below_target_coverage`-but-`"complete"` run
  silently dropped its own gap telemetry before reaching
  structured/PipelineState/the DB. Fixed in `agent.py`'s `structured{}`
  construction (this pass); verified via a real `run_pipeline()` call with
  a `below_target_coverage` fixture. They still do not reach the DB
  (schema change, out of scope) — asserted absent there explicitly.
- **Hazard** (`agents/hazard/test_field_survival.py`): `evidence_basis`
  (earthquake + landslide) confirmed to survive into the REAL
  `write_to_db`'s `confirmed_by` JSONB (not the hand-copied closure the old
  test exercised) via a real `agent.analyze_hazard()` call with a faked
  asyncpg connection that records the real INSERT's confirmed_by argument.
  `primary_hazard_risk` confirmed to reach `analyze_hazard`'s payload (and
  thus PipelineState) but NOT `confirmed_by` — real, structural gap
  (`write_to_db(raw_result)` runs *before* `primary_hazard_risk` is even
  computed in `agent.py`), asserted absent explicitly rather than omitted.
  `confidence_cap_applied` confirmed absent from BOTH the payload and
  `confirmed_by` via the real entry point — a real gap the analyzer-level
  test (`test_confidence_cap.py`) could not see because it never calls
  `analyze_hazard`.
- **Impact** (`agents/impact/test_field_survival.py`): `overall_confidence`
  confirmed to survive from `run_impact_analysis`'s real
  no-significant-disaster path into the real `write_impact_data()` INSERT
  parameter (Gate A's fix, previously only confirmed by reading source
  during the 2026-07-28 audit, not by a test).
- **Report** (`agents/report/test_field_survival.py`): the H#6 guarantee
  (satellite confidence 0.0 cannot yield a HIGH/MEDIUM report
  `confidence_level`) confirmed via a real `run_report_pipeline()` call with
  a real `incoming_payload={"confidence_scores": {...}}` — the exact shape
  `report/node.py` actually passes — rather than a hand-built report dict
  shaped like what the wiring is supposed to produce.

**This session's own at-risk fixes, now verified (not just flagged):**
hazard-provenance (`evidence_basis` → `confirmed_by`) and scene-age
(`scene_age_days` → `satellite_results`) both confirmed to genuinely survive
via the real write paths above. Scene-age turned out fine (it was already
correctly wired, just untested); hazard-provenance was also fine. Neither
was a silent regression — but neither was verified before this pass, and
now both are.

**Misleading tests retired:**
- `test_correctness_fixes_20260727.py`'s `test_scene_id_threaded_into_merged_result`,
  `test_structured_carries_total_zones_and_scene_id`,
  `test_structured_carries_artifacts_incomplete`, and
  `test_per_city_flag_reaches_process_satellite_imagery_call` — all four
  rewritten to call the real functions (`_render_clip`,
  `agent.run_pipeline`, or a real flag-flip + captured kwarg) instead of
  `inspect.getsource` + string matching. The known-failing
  `scene_id`-via-source-grep check no longer exists in that form — resolved
  by rewriting it to call `_render_clip` against a synthetic real raster.
- `test_index_label_integrity.py`'s `test_validation_input_index_type_matches_result`
  — rewritten to call `agent.run_pipeline()` with `cross_validator.validate_all`
  patched to capture the REAL `validation_input` dict, for both a SAR and an
  NDWI run, instead of asserting on a hand-built simulation.
- `test_hazard_provenance.py`'s `test_evidence_basis_survives_into_db_write_confirmed_by`
  — the hand-copied `_confirmed_by` closure replaced with a delegated call
  into `agents/hazard/test_field_survival.py`'s real-write-path test.
- `test_confidence_aggregation.py`'s
  `test_satellite_zero_via_dedicated_channel_ONLY_cannot_yield_high` — kept
  (it is a legitimate, fast unit test of `calculate_confidence_level`'s
  aggregation rule, not a grep or a hand-copy), but its docstring updated to
  point at the new real entry-point test
  (`test_satellite_zero_confidence_via_real_run_report_pipeline_cannot_yield_high`
  in `test_field_survival.py`) that actually exercises the wiring this test's
  original docstring claimed to protect.

**Left as legitimately narrow, not rewritten** (per the task: narrow is not
wrong): `test_coverage_tolerance.py` (function-level coverage/tiering
logic — the fields it computes are now covered end-to-end by
`test_verify_islamabad_fixes.py` instead of being re-derived here) and
`test_confidence_cap.py` (a fast, real unit test of the analyzer's cap
arithmetic — the orchestration-level gap it couldn't see is now covered by
`test_field_survival.py`'s `test_confidence_cap_applied_does_not_survive_any_boundary`).

**Verify results:** satellite offline suite — `test_coverage_tolerance.py`
65/65, `test_correctness_fixes_20260727.py` 20/20 (was 19/19 with 1
known-failing scene_id check; now fully green with a real check in its
place), `test_index_label_integrity.py` 5/5,
`test_verify_islamabad_fixes.py` 27/27 (up from 6). `test_bug_fixes.py`/
`test_clip_window.py` still fail on the pre-existing `PROJ_LIB` environment
conflict (system PostgreSQL/PostGIS `proj.db` shadowing rasterio's bundled
proj data) — confirmed present before this session's changes too, not a
regression. hazard: `test_hazard_provenance.py` 17/17,
`test_confidence_cap.py` 8/8, `test_field_survival.py` (new) 8/8;
`test_db.py` needs a live DB connection, pre-existing, unrelated to this
pass. impact: `test_field_survival.py` (new) 3/3. report:
`test_confidence_aggregation.py` 8/8, `test_field_survival.py` (new) 4/4.
No live e2e was run, per the task's scope.

## PROJ_LIB conflict resolved (2026-07-28, coherence pass on feat/durable-evidence-trail)

The "`test_bug_fixes.py`/`test_clip_window.py` still fail" note above
undercounted the real scope (8 failures, all in `test_bug_fixes.py` and one
in `test_correctness_fixes_20260727.py`; `test_clip_window.py` itself was not
actually failing) and treated it as an environment quirk to live with rather
than something fixable. It is fixable: GDAL/rasterio reads `PROJ_LIB` at
`import rasterio` time and caches it, so once a bad system `proj.db` has been
read in a process, no later env-var reassignment helps — the fix has to run
before rasterio's first import. `agents/satellite/tests/conftest.py` now
pins `PROJ_LIB`/`PROJ_DATA` to rasterio's own bundled `proj_data` directory
(located via a plain filesystem walk, without importing rasterio first) as
the very first thing pytest does in this directory. All 8 tests now pass
without any manual env setup. See root `CLAUDE.md`'s "PROJ_LIB RESOLVED"
entry for the full root-cause writeup.
