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
