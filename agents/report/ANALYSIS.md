# Report Agent — Analysis

> Scope: `agents/report/`. Verified against current code on branch
> `analysis/all-agents` (tip `b1be94f`, same as `main`). CLAUDE.md/CODEBASE.md
> claims are checked against source and flagged where stale.

---

## 1. RESPONSIBILITY

The report agent is the pipeline's **final synthesis stage**. Given an
`event_id`, it reads the durable DB record left by satellite/hazard/impact
(it never reads `PipelineState` directly for content — see §2) and produces
three deliverables: a PDF executive report, a static risk-map PNG, and an
`executive_summary` + `confidence_level` row in `final_reports`. It is the
only agent whose output a human decision-maker is expected to read directly.

**The question it uniquely answers:** "given everything the other three
agents found, what should a responder actually do in the next 6/24/72 hours,
and how much should they trust this?" No other agent produces a
recommendation timeline, a decision brief, or an aggregate confidence label.

**What it explicitly does NOT do:**
- No new hazard science — it does not recompute NDWI/USGS/slope, does not
  re-derive risk levels, does not touch a satellite raster.
- No population/infrastructure counting — it reads impact's numbers verbatim.
- No spatial analysis beyond rendering geometry it's handed (`map_generator.py`
  projects and draws GeoJSON it receives; it never computes new geometry).

**Where it blurs with impact (its only direct upstream by data volume):** the
report's `impact` context block *is* impact's `impact_data` row, lightly
renamed (`db_client.py:355-368`, e.g. `impact_data.total_affected` →
`impact.population_affected`). The report agent adds no new impact facts, but
its LLM narrative *interprets* those facts (e.g. deciding response priorities
from hospital/population counts) — a soft boundary: impact assesses exposure,
report decides what to say about it and how urgently.

---

## 2. EXECUTION FLOW

### Entry point

`node.py:19` `report_node(state)` is the only production entry point post-
migration. It reads `state["event_id"]` and calls
`pipeline.run_report_pipeline(event_id, fetch_from_db=True, upload_r2=True,
write_db=True, use_llm=True)` (`node.py:30-36`) — **all four side-effect flags
hardcoded True**. `agent.py` is a separate, still-present CLI variant
(`--contract-test`, `--from-db`, `--no-llm`) used for manual/offline runs, not
imported by `node.py`.

Note: the node reads `event_id` from state and nothing else — it does **not**
pass `state["impact_result"]` or `state["confidence_scores"]` into the
pipeline as `incoming_payload`. `run_report_pipeline`'s `incoming_payload`
parameter (which would merge live in-memory hand-off data, per
`_merge_incoming_payload_into_context`, `pipeline.py:305-334`) is simply never
supplied on the graph path — the report stage is **100% DB-sourced** in the
current wiring, contradicting the repo CLAUDE.md's framing of `PipelineState`
as "the source of truth... not a re-read from DB" for this specific stage.
(`PipelineState["confidence_scores"]["satellite"]` from earlier nodes is
therefore inert for report — see §5.)

### `pipeline.run_report_pipeline` (`pipeline.py:54-219`)

1. **Contract-test guard** (`:78-85`) — if `use_llm=False` and either upload
   flag is set without `allow_contract_side_effects`, returns `failed`
   immediately. Not reachable on the graph path (`use_llm=True` always).
2. **Context fetch** (`:89-100`) — `fetch_report_context_from_db(event_id)`
   requires a valid UUID (`is_valid_uuid`, `db_client.py:25-33`); a non-UUID
   `event_id` short-circuits to `failed` before any LLM call. Missing rows
   produce **warnings, not failures** — `_missing_context_warnings` (`:283-302`)
   appends free-text warnings if satellite/hazard/impact rows are absent, but
   the pipeline proceeds with whatever it has (zeros/empty lists via
   `db_context_to_report_context`'s `or 0` / `or ""` defaults throughout
   `db_client.py:313-373`).
3. **`generator.generate_report`** (`generator.py:271-370`) — the LLM
   narrative core, traced in full below.
4. **Output paths** (`_resolve_output_paths`, `pipeline.py:222-251`) — temp
   dir under `REPORT_OUTPUT_DIR` (default OS temp), unless
   `frontend_demo_mode` or explicit paths given.
5. **Map** (`map_generator.generate_static_map`) then **PDF**
   (`pdf_generator.generate_pdf_report`, embeds the map PNG) — both pure,
   synchronous, no external calls, cannot fail on network conditions.
6. **R2 upload** — PDF and map uploaded independently; each wrapped in its
   own `try/except` (`pipeline.py:134-161`) so **either can fail without
   failing the pipeline** — a failed PDF upload sets `pdf_url = None` and adds
   a warning; a failed map upload leaves `map_url` at whatever it was (empty
   string, since it's set to `""` at `:123` before either upload attempt).
7. **DB write** — `write_final_report_metadata`, skipped (warning, not error)
   for non-UUID `event_id`; real failures propagate as exceptions caught by
   the outer `try/except` at `:211`.
8. **Local cleanup** — `shutil.rmtree` on the temp dir, only if R2 upload
   succeeded and no explicit `output_dir` was passed.
9. **Status derivation** (`:192`) — `"complete_with_warnings"` if any warning
   accumulated, else `"complete"`. Any exception anywhere in the whole
   function is caught at `:211-219` and returns `status: "failed"` with a
   redacted error message (`_safe_error_message`, `:429-443`).

### `generator.generate_report` (`generator.py:271-370`) — the LLM core

Call graph, `use_llm=True` path:
1. `generate_detailed_report(result)` (`:568-591`) → tries
   `generate_composite_detailed_report_with_featherless` (three **parallel**
   sub-calls: incident interpretation, technical analysis,
   assumptions/limitations, each independently Gemini-primary/Featherless-
   fallback via `featherless_json_cascade`) → if not all three components came
   from a live model (`_all_components_are_live`, `:601-605`) **and**
   `model_cascade_enabled()` is true, falls through to
   `generate_detailed_report_with_aiml_fallback` (single AIML call). If model
   cascade is disabled and Featherless didn't fully succeed, **raises
   `LLMGenerationError` immediately** (`:578-579`).
2. `generate_executive_summary(result, detailed_report)` (`:594-598`) → AIML
   primary → Gemini → Featherless (`llm_clients.py:312-342`); raises
   `LLMGenerationError` only if literally every provider in that chain fails
   AND returns nothing to fall back to — but `generate_executive_summary_with_aiml`
   always returns `ok: False` + a deterministic summary rather than raising,
   so in practice `generate_executive_summary` at `generator.py:594-598`
   raises only if `response["ok"]` is `False`, which **is** the deterministic-
   fallback case — i.e. the executive summary funnels through
   `LLMGenerationError` whenever it degrades to template text.
3. `_assert_required_report_sections(...)` (`:623-637`) — see below.
4. `generate_intelligence(result)` (`:373-393`) — the 7-section layer, traced
   in §"intelligence.py" below.
5. `_assert_live_intelligence_sources(...)` (`:608-620`) — see below.
6. `_frontend_ready_result` (`:532-554`) trims the dict to the fields the
   frontend/PDF actually consume.

### `_assert_required_report_sections` — what strict mode actually checks

`generator.py:623-637`. Fails (raises `LLMGenerationError`) if **any** of:
- `detailed_source` starts with `"deterministic_fallback"` OR
  `detailed_report["detailed_body"]` is falsy
- `technical_analysis` is falsy
- `recommendations` is falsy
- `summary_source` starts with `"deterministic_fallback"` OR `summary` is
  blank

**This is a hard fail, not a degrade.** `LLMGenerationError` is a
`RuntimeError` subclass (`generator.py:27-28`) — nothing in `generate_report`
or `run_report_pipeline` catches it specifically; it propagates up to
`run_report_pipeline`'s blanket `except Exception` (`pipeline.py:211`), which
converts it to `status: "failed"` with the message preserved verbatim
(`pipeline.py:216`: `if error_message.startswith("LLM generation failed")`).
So: **strict mode always hard-stops the whole report** (no PDF, no map, no DB
write reaches completion) rather than shipping a report silently built on
template text. This matches the CLAUDE.md claim and is verified correct.

**One gap in the assertion's coverage:** it checks `detailed_source`/
`summary_source` for the `deterministic_fallback` prefix but does **not**
check `response_priorities`/`assumptions`/`limitations` — these three fields
can silently be template defaults (`FALLBACK_RECOMMENDATIONS`,
`FALLBACK_PRIORITIES`, etc. via `list_or_default`, `llm_clients.py:912-917`)
even when `detailed_body`/`technical_analysis`/`recommendations` are live,
because `normalize_detailed_report` falls back to the module-level constants
per-field, independently, whenever the LLM's JSON omits or empties that key.
A partially-templated report (live body/analysis/recommendations, templated
assumptions/limitations) passes strict mode.

### `_assert_live_intelligence_sources` — narrower than the name suggests

`generator.py:608-620`. Only checks `anomaly_check`. If
`anomaly_source == "llm_required_failed"` (i.e. rule-based validation found
real anomalies but the LLM interpretation of them failed) **or** the source
string starts with `"deterministic_fallback"`, it raises. **Every other
intelligence section — criticality, map narrative, priority timeline,
decision brief, quality check, band-ready message — can silently fall back
to deterministic/template content with no assertion failure.** This is a real
asymmetry: the "no silent fake success" philosophy from CLAUDE.md is enforced
for the top-level report body/summary and (narrowly) for anomaly detection,
but not for 5 of the 7 intelligence sections. `model_sources.intelligence`
records which path each section took (`generator.py:340-356`), but nothing
downstream refuses to ship a report where e.g. `decision_brief` fell all the
way back to `_fallback_decision_brief`'s templated summary.

### `intelligence.py` — 7-section layer, call graph

`generate_intelligence` (`generator.py:373-393`) runs, in order:
1. `assess_event_criticality` — sequential.
2. `asyncio.gather(detect_anomalies, generate_map_narrative,
   generate_priority_recommendations)` — parallel.
3. `generate_decision_brief(context, partial)` — sequential, needs 1+2's
   output as input context.
4. `run_quality_check(context, partial)` — sequential, needs everything above.
5. `generate_band_ready_message(context, partial)` — sequential, needs
   `quality_check` for its status field.

Each section has its own fallback:
- `assess_event_criticality` (`intelligence.py:19-47`) — Featherless/Gemini
  cascade (Kimi primary), coerced via `_coerce_criticality` against
  `_fallback_criticality` (a deterministic rule using flood confidence,
  population, hospitals — `:802-826`).
- `detect_anomalies` (`:50-87`) — **rule-based gate runs first**
  (`_validate_anomaly_inputs`, `:557-651`); if it finds nothing, returns
  immediately with **zero LLM cost** (`:55-65`). If it finds something, calls
  the LLM to interpret/prioritize; if that LLM call itself fails
  (`source == "deterministic_fallback"`), the result is explicitly tagged
  `_source: "llm_required_failed"` (`:77-82`) — the one path
  `_assert_live_intelligence_sources` actually enforces.
- `generate_map_narrative` (`:90-129`) — always computes
  `build_cartographic_data_summary` first (a genuinely metadata-derived,
  non-fabricated summary — counts zones/routes/facilities, checks bbox
  presence, `:360-425`); if the LLM cascade fails, returns that summary
  directly rather than fake prose (verified: matches CLAUDE.md's claim).
- `generate_priority_recommendations` (`:132-160`) — Featherless/Gemini
  cascade, DeepSeek primary, coerced against a static 72-hour-timeline
  fallback.
- `generate_decision_brief` (`:163-200`) — the one section that calls
  **AIML/Opus directly**, not the Featherless/Gemini cascade
  (`call_aiml(..., model=AIML_OPUS)`), falling back to
  `AIML_GPT_LAST_RESORT` then to a deterministic template. Confirmed: matches
  CLAUDE.md's claim.
- `run_quality_check` (`:203-239`) — always starts from a deterministic
  checklist (`_fallback_quality_check`), then optionally merges LLM-added
  warnings/blocking issues on top; never LLM-only.
- `generate_band_ready_message` (`:242-275`) — pure template, no LLM call at
  all (despite living in the "intelligence" module) — string-formats a
  message from `criticality`/`quality_check`/`decision_brief` fields already
  computed. Its hardcoded `target: "@muhammad-abdullah"` (`:270`) is dead
  data post-Band-migration (nothing sends this to anyone; see §9).

---

## 3. DECISION LOGIC

This agent is overwhelmingly LLM-driven for narrative content, but every
**severity/response-level classification** is deterministic. Split:

**Deterministic (numeric thresholds, file:line):**
- `determine_recommended_response_level` (`generator.py:707-728`):
  - `NDMA Level-3` if `severity=="CRITICAL"` OR `hospitals_at_risk>=10` OR
    `total_affected>=1_000_000` OR (`vulnerability=="HIGH"` AND
    `high_risk_people>=100_000`)
  - `NDMA Level-2` if `severity=="HIGH"` OR `hospitals_at_risk>=3` OR
    `total_affected>=100_000`
  - else `NDMA Level-1`
  - **Basis: not stated anywhere** — no citation to an NDMA doctrine document,
    no comment explaining why 10/3, 1M/100K, or 100K are the cutoffs. Read as
    chosen by feel.
- `calculate_confidence_level` (`db_client.py:403-418`): `>=0.8` → `HIGH`,
  `>=0.6` → `MEDIUM`, else `LOW`. Same 0.6/0.8 banding pattern used elsewhere
  in the codebase (e.g. hazard's own confidence bands) — internally
  consistent but, again, no external calibration citation.
- `deterministic_intelligence`'s criticality rule (`generator.py:448`):
  `critical` if `severity=="CRITICAL"` OR `hospitals>=10` OR
  `population>=250000`. Note this 250,000 threshold **differs** from
  `determine_recommended_response_level`'s 100,000/1,000,000 — two different
  population cutoffs for two different "how bad is this" judgments, both
  unsourced, easy to read as inconsistent if compared side by side.
- `_validate_anomaly_inputs`'s `unusual_extent` check (`intelligence.py:627-637`):
  `affected_area_km2 > 500` AND `population < 10000` → anomaly. Arbitrary-
  looking pairing, no stated basis.
- `_minimum_confidence` threshold for a `low_confidence` anomaly
  (`intelligence.py:617`): `< 0.7`.

**LLM-driven (with deterministic coercion/validation on top):** all 7
intelligence sections' *content* (rationale text, hotspots, priority items,
decision brief prose) — but every LLM response is passed through a `_coerce_*`
function that clamps types/ranges and falls back per-field to deterministic
defaults if the LLM's JSON is malformed or missing a key. So even the "LLM"
outputs have a deterministic floor; nothing from the LLM reaches the PDF
unvalidated.

**Deterministic fallback vs LLM path — equivalence:**
`deterministic_detailed_report` (`generator.py:676-694`,
`llm_clients.py:920-941`) is a **hardcoded English paragraph template with
string-interpolated numbers** — not equivalent in depth/nuance to the LLM
path (no situation interpretation, no spatial risk drivers, generic
recommendations list). Whether the reader knows which path was used: **only
if they read `model_sources` in the PDF's "Model Source Note" section**
(`pdf_generator.py:129-148`, `186-200`) — this note is real and present in
every PDF, listing `detailed_report`/`executive_summary`/each intelligence
section's source string. However this section is far down the PDF (after all
narrative content), in small-print table form, and requires the reader to
recognize that e.g. `"deterministic_fallback"` in that table means "this text
was not actually AI-generated, less nuanced than it may read." **In production
(`node.py`'s hardcoded `use_llm=True`), `_assert_required_report_sections`
means the top-level `detailed_body`/`technical_analysis`/`recommendations`/
`summary` can never actually be the fully-deterministic template** — that
template path is only reachable via `agent.py --contract-test`/`--no-llm`
(where it's clearly labeled) or via `generate_offline_contract_report`
(`generator.py:396-436`, also clearly labeled `"offline_contract_test"`
throughout). So in the live pipeline the deterministic *top-level* report body
is unreachable; only the *intelligence* sub-sections (decision brief,
criticality, etc.) can silently degrade, per the `_assert_live_intelligence_sources`
gap above.

---

## 4. THE ANALYSIS ITSELF (the science) — confidence aggregation

**CLAUDE.md's claim ("averages only hazard+impact, satellite never included")
is STALE.** Commit `fa0d9bd` (2026-07-27, same day as the CLAUDE.md file's own
last dated section) rewrote both the hazard-side propagation and the report-
side aggregation:

### `_collect_confidence_values` (`db_client.py:490-511`) — current code

```python
_append_confidence(values, confidence.get("satellite_confidence"))
_append_confidence(values, report.get("satellite", {}).get("confidence"))
for key in ("hazard_overall_confidence", "impact_overall_confidence", "combined_confidence"):
    _append_confidence(values, confidence.get(key))
_append_confidence(values, report.get("impact", {}).get("overall_confidence"))
hazard_scores = report.get("hazard", {}).get("confidence_scores", {})
for key in ("overall", "flood", "earthquake", "landslide"):
    _append_confidence(values, hazard_scores.get(key))
_append_confidence(values, report.get("intelligence", {}).get("criticality", {}).get("overall_confidence"))
```

Satellite confidence **is** now a read key — two of them
(`confidence.satellite_confidence` and `satellite.confidence`). But per the
node-flow trace in §2, **the DB-fetch path used in production never populates
either key**: `db_context_to_report_context` (`db_client.py:291-373`) builds
the `satellite` block from `satellite_results` DB columns
(`type`/`reason`/`cloud_cover`/`scene_id`, `:318-323`) — there is no
`confidence` column in `satellite_results` (confirmed against
`_fetch_satellite_results`'s SELECT list, `db_client.py:172-198` — no
`confidence` field). So `report.satellite.confidence` is always absent on the
DB path, and `confidence.satellite_confidence` is only ever populated via
`incoming_payload`, which (§2) the graph node never supplies. **The two new
satellite-confidence keys are dead on the current production code path.**

### The actual channel satellite confidence has today: indirect, via hazard

`fa0d9bd` also patched `agents/hazard/analyzer.py`'s `run_parallel_analysis`
(confirmed by reading the commit diff) to cap **flood** confidence at
satellite's self-reported confidence
(`flood = {**flood, "confidence": satellite_confidence}` when
`satellite_confidence < flood_confidence`). Since hazard's own
`hazard_zones.overall_confidence` (read by report via `hazard_scores["flood"]`,
sourced from `_fetch_hazard_zones`) inherits that capped value, satellite's
low confidence **does** eventually reach `_collect_confidence_values` — but
only through the flood hazard-zone row, only when flood is the active hazard,
and only because hazard capped it first. Earthquake/landslide are explicitly
NOT capped (self-sourced from USGS/DEM, correctly independent per
`root_cause.md`'s finding that they never consume satellite output).

### Is `min()` aggregation statistically defensible?

**Not in a rigorous statistical sense, but the design intent is explicit and
documented** (`db_client.py:406-413` comment, and mirrored by
`test_confidence_aggregation.py`'s regression tests). `min()` over independent
uncertainty estimates is not how you'd combine truly independent probability
estimates (that would call for something like a weighted geometric mean or
explicit Bayesian combination) — but the stated goal isn't statistical rigor,
it's a **conservative floor**: "the report's reliability can never exceed its
least-confident input." That is a defensible *engineering* choice for a
disaster-response system (never overstate confidence) even though it is not a
defensible *statistical* combination of independent estimates. It also means
the metric is dominated entirely by whichever single stage is worst — one
noisy, poorly-calibrated self-rating (e.g. hazard's LLM-generated
`overall_confidence` for flood, `intelligence.py`'s prompt just asks the model
for "a number from 0.0 to 1.0" with no calibration guidance,
`intelligence.py:33`) can single-handedly cap the whole report's confidence
label regardless of how good the other three stages' assessments were. This
is arguably the right failure direction (conservative) but the input values
themselves are heuristic LLM self-ratings, not calibrated probabilities, so
`min()` of heuristics is still a heuristic, not a rigorous bound.

---

## 5. CONFIDENCE — full arithmetic

**Field:** `final_reports.confidence_level` (TEXT: `HIGH`/`MEDIUM`/`LOW`/`UNKNOWN`).

**Derivation, exact chain (`db_client.py:130-147`, `403-418`):**

```
build_final_report_db_values(report)
  → confidence_level = calculate_confidence_level(report)
       values = _collect_confidence_values(report)   # see §4 list above
       if not values: return "UNKNOWN"
       combined = min(values)
       HIGH if combined >= 0.8
       MEDIUM if combined >= 0.6
       else LOW
```

No weighting, no cap beyond the banding itself, no floor other than
`UNKNOWN` when the list is empty. `_numeric_confidence` (`:520-527`) rejects
any value outside `[0, 1]` (silently drops it, does not clamp) before it
enters `min()`.

**What satellite reporting 0.0 confidence actually produces, traced with real
code:**
- On the **graph/production path** (no `incoming_payload`): satellite 0.0
  reaches report only if it already propagated through hazard's flood-
  confidence cap (`fa0d9bd`'s `analyzer.py` patch). If the active
  `hazard_type` is flood, `hazard_scores["flood"]` would then be capped at
  0.0, `min(values)` would include 0.0, and `combined >= 0.8`/`>= 0.6` both
  fail → `confidence_level = "LOW"`. This is the fix CLAUDE.md describes
  landing, and it does land — but **only for the flood/SAR case**, and only
  because hazard did the capping before report ever sees it; report's own two
  new satellite-confidence read keys are not what carries the value.
- If the disaster type is earthquake/landslide, satellite's 0.0 (if it were
  ever non-flood-relevant, which per `root_cause.md` it structurally never
  informs) has **no path into `_collect_confidence_values` at all** — those
  hazards are fully insulated from satellite confidence by design (confirmed,
  §4/`root_cause.md`).
- `test_confidence_aggregation.py` (`agents/report/`) has a live regression
  test, `test_satellite_zero_confidence_cannot_yield_high` (line ~48),
  asserting the exact scenario the 2026-07-26 run hit (satellite 0.0,
  hazard/impact ~0.83) now yields LOW, plus
  `test_old_behavior_would_have_masked_this` (line ~58) proving the pre-fix
  averaging code would have returned HIGH on the same inputs. This is a real,
  targeted regression guard for the exact bug CLAUDE.md documents — confirms
  the fix is intentional and tested, not accidental.

**Calibrated or heuristic:** heuristic throughout. Every input to `min()` is
itself either an LLM's self-reported confidence (no calibration curve, no
historical accuracy tracking) or a hand-set deterministic value (e.g. hazard's
earthquake path returns flat `0.85`/`0.8`/`0.7`/`0.85` by magnitude band,
`root_cause.md` §earthquake — not derived from any USGS uncertainty figure).
The banding cutoffs (0.6/0.8) are round numbers with no cited derivation.

---

## 6. DATA CONTRACT

### Consumed (from DB, `db_client.py`'s `_fetch_*` functions)

- `disaster_events`: `event_id, disaster_type, location, magnitude, bbox,
  status, step, progress, created_at, updated_at` (`:150-169`).
- `satellite_results`: `satellite_type, cloud_cover, scene_id,
  true_color_url, index_url, classification_url, geojson_url,
  affected_area_km2, damage_percent, total_zones, bounds, bbox, risk_cities`
  (`:172-198`) — **no `confidence` column**, confirming §4/§5's finding that
  satellite confidence cannot reach report via the DB path.
- `hazard_zones` (multi-row): includes `overall_confidence` per-row
  (`:201-224`) — this IS present and read.
- `impact_data`: `total_affected, high_risk_people, medium_risk_people,
  hospitals_at_risk, schools_at_risk, roads_blocked, bridges_at_risk,
  vulnerability_score, evacuation_routes, estimated_evacuation_time,
  overall_confidence` (`:227-252`).

**On the `overall_confidence`-missing-from-impact question (CLAUDE.md's
flagged gap):** the SELECT at `:227-252` explicitly lists
`overall_confidence` as a column to read from `impact_data`. If that column
does not actually exist on the live table (as CLAUDE.md's Known Issues
section claims for `agents/impact/services/db.py`'s DDL), this query would
raise `asyncpg.exceptions.UndefinedColumnError` — which
`fetch_report_context_from_db`'s `except Exception` (`:50-53`) catches
specifically via `_schema_mismatch` (checks for `UndefinedColumnError`/
`UndefinedTableError` or "column ... does not exist" in the message) and
re-raises as a **loud** `RuntimeError` with a schema-mismatch-specific message
— not silently defaulted, not swallowed. This means: **if impact_data truly
lacks `overall_confidence` on live Neon, every report generation currently
throws and the whole pipeline stage fails outright** (surfaces as
`status: "failed"` from `run_report_pipeline`'s outer catch). This is a
sharper failure mode than CLAUDE.md implies ("missing... impact confidence
gap") — it isn't a silent gap, it's a hard stop, assuming the column really is
absent live. Not independently verified against the live Neon schema in this
review (no DB access) — flagged as the single highest-value thing to check
against production, since it would mean **every report for every event is
currently failing**, or the CLAUDE.md claim about the missing column is itself
stale and the column was added since.

### Produced (`final_reports`, `db_client.py:14-22`, `130-147`)

`event_id, pdf_url, map_url, executive_summary, agent_log (JSONB),
total_time_seconds, confidence_level`. Matches CLAUDE.md/CODEBASE.md exactly.
Upsert semantics: looks up the latest existing row by `event_id`
(`ORDER BY created_at DESC LIMIT 1`) and `UPDATE`s if found, else `INSERT`s
(`:69-120`) — not a DB-level `ON CONFLICT`, a read-then-write race is
theoretically possible under concurrent writes to the same event, though the
pipeline design (one report run per event) makes this unlikely in practice.

### Label vs content mismatches

- **The uncalibrated-SAR caveat CLAUDE.md says should appear: it does not.**
  `_compact_context` (`intelligence.py:437-463`, feeds every intelligence
  prompt) includes `analysis.index_type` but never `index_calibrated` —
  confirmed exactly as CLAUDE.md states. Searched all report-agent prompt
  builders (`generator.py`'s `build_detailed_report_prompt`,
  `build_executive_summary_prompt`; `llm_clients.py`'s
  `compact_report_context`; `intelligence.py`'s `_compact_context`,
  `_anomaly_prompt`) — none pass a calibration flag or caveat text to any LLM
  call. The PDF's "Technical Analysis" section
  (`deterministic_detailed_report`, both copies in `generator.py:684-689` and
  `llm_clients.py:929-936`) states `index_type` and `mean_value` but never
  calibration status either. **A SAR-path report can state a `mean_value` in
  the PDF with no caveat that the number is an uncalibrated raw-DN log, i.e.
  physically uninterpretable** — this is a real, unfixed gap that would let a
  reader draw a false wet/dry conclusion from the printed number.
- **Does the PDF ever say "HIGH confidence" language when the number is
  mediocre?** No language-vs-score mismatch found: `confidence_level` is
  surfaced as a raw label (`HIGH`/`MEDIUM`/`LOW`) in `model_source_note`/DB
  only — no narrative text independently asserts a confidence adjective. The
  LLM-generated `rationale`/`decision_brief` prose does not reference
  confidence numerically except via `criticality.overall_confidence`, which
  is its own separately-computed (not `min()`-aggregated) value — see next
  point.
- **A second, DIFFERENT confidence number exists and could visually conflict
  with `confidence_level`.** `intelligence.criticality.overall_confidence`
  (shown in the PDF's "Intelligence Assessment" table,
  `pdf_generator.py:267`) is computed independently — either by the LLM
  itself (`assess_event_criticality`, no connection to `min()` aggregation)
  or by `_fallback_criticality`'s formula
  (`round(min(0.95, max(0.65, flood_confidence - 0.03)), 2)`,
  `intelligence.py:814`). This is a **second confidence figure, shown in the
  same PDF**, computed by an entirely different method than
  `confidence_level`, with no cross-check between them. A report could
  plausibly show `Overall Confidence: 78%` in the intelligence table
  (from the criticality LLM/fallback) while `confidence_level: LOW` appears
  elsewhere (from `min()` aggregation) — two numbers, two methods, no
  reconciliation, both visible to the same reader. Not confirmed against a
  real generated PDF in this review, but the code paths are fully independent
  and nothing forces them into agreement.

---

## 7. FAILURE MODES

| Trigger | Swallowed or surfaces | What's returned | Distinguishable from success? |
|---|---|---|---|
| `event_id` not a UUID (DB fetch requested) | Surfaces | `status: "failed"`, warning listed | Yes — explicit `failed` status |
| DB row(s) missing (event/satellite/hazard/impact) | Swallowed to warning | Pipeline continues with defaults (0/empty); `status: "complete_with_warnings"` | Only via `warnings` array — a caller checking only `status=="complete"` cannot tell |
| `impact_data.overall_confidence` column absent (if live-stale per CLAUDE.md) | Surfaces (schema-mismatch detection) | `RuntimeError` → outer catch → `status: "failed"` | Yes, but see §6 — this would fail every event, not a rare edge case |
| Featherless+AIML both fail for detailed report/summary | Surfaces via `LLMGenerationError` | `status: "failed"`, error text starts `"LLM generation failed: ..."` | Yes |
| Anomaly-check LLM fails while rule-based anomalies exist | Surfaces via `_assert_live_intelligence_sources` | `status: "failed"` | Yes |
| Any OTHER intelligence section (criticality, map narrative, priority timeline, decision brief, quality check, band-ready message) falls back to deterministic | **Silently succeeds** | `status: "complete"`, `model_sources.intelligence.<section>` says `deterministic_fallback` or similar | **No** — only visible by reading `model_sources` in the response/PDF; nothing flags it at the `status` level |
| `response_priorities`/`assumptions`/`limitations` individually template-defaulted while sibling fields are live | **Silently succeeds** | `status: "complete"` | No — `_assert_required_report_sections` doesn't check these three keys |
| R2 PDF upload fails | Swallowed | `pdf_url: None`, warning appended, pipeline continues | Only via `warnings`/`pdf_url` being null |
| R2 map upload fails | Swallowed | `map_url: ""` (never set from the pre-upload empty default), warning appended | Only via `warnings` |
| DB write (`write_final_report_metadata`) throws for a real (UUID) event | Surfaces | Uncaught inside `run_report_pipeline`'s inner try, caught by outer `except Exception` at `:211` | `status: "failed"` |
| Any unexpected exception anywhere in `run_report_pipeline` | Surfaces | `status: "failed"`, `_safe_error_message` redacts known secret env values, truncated to 500 chars | Yes |

**Can `_assert_required_report_sections` fail silently anywhere?** No — every
path that reaches it either passes (proceeds) or raises `LLMGenerationError`,
which is never caught locally; it always propagates to `status: "failed"`.
It cannot fail silently. Its **coverage gap** (not checking
`response_priorities`/`assumptions`/`limitations`, and
`_assert_live_intelligence_sources` only checking one of seven sections) is
the real issue — not silent failure of the assertion itself, but an
incomplete assertion surface.

---

## 8. EXTERNAL DEPENDENCIES

| Dependency | Timeout | Retry/backoff | Behavior when unavailable |
|---|---|---|---|
| Gemini (`call_gemini`, multi-key) | `report_llm_timeout_seconds()` — env `REPORT_LLM_TIMEOUT_SECONDS` or default 30s, capped at 30s regardless of a larger passed default (`llm_clients.py:76-88`) | Tries each of up to 5 keys in sequence (`_GEMINI_KEY_VARS`) until one succeeds | Falls through to Featherless (`call_featherless`) |
| Featherless (`call_featherless_model`) | Same 30s cap; separate `FEATHERLESS_RETRY_TIMEOUT_SECONDS=75` used for a **content-empty retry** (`_create_completion_with_retry`, `:728-763`, doubles `max_tokens` up to `MAX_RETRY_TOKENS=2500` if the first response came back empty or truncated) | One retry-with-more-tokens only, no exponential backoff | `featherless_json_cascade` moves to the next model in its cascade list |
| AIML (Opus/GPT) | Same timeout helper, `AIML_TIMEOUT_SECONDS=35` default | No retry beyond the built-in empty-content retry; `aiml_text_call` explicitly falls back Opus→GPT | `generate_decision_brief` falls to deterministic template; `generate_executive_summary_with_aiml` falls to Gemini then Featherless then deterministic |
| Neon (asyncpg, `db_client.py`) | Library default (no explicit timeout set on `asyncpg.connect`) | None — single attempt, wrapped in `try/except` → `RuntimeError("Neon connection failed")` | Read: propagates as failure (event context fetch fails → `status: "failed"`). Write: same, but only for a valid UUID event | 
| Cloudflare R2 (`storage_client.upload_file_to_r2`, boto3) | Not reviewed in this pass (file not read — out of the explicitly-requested file list) but call sites wrap it in `try/except Exception` individually for PDF and map (`pipeline.py:134-161`) | Not visible from call sites; presumably boto3 defaults | Non-fatal for both PDF and map — warnings appended, `status` degrades to `complete_with_warnings`, never `failed` solely for an upload failure |

**Concurrency limiter:** `FEATHERLESS_CONCURRENCY = 2`
(`_FEATHERLESS_SEMAPHORE`, `llm_clients.py:32-33`) caps in-flight Featherless
calls from this agent to 2 at a time — a self-imposed guard against the
provider's shared 4-unit cap, independent of the other agents' own limits.

---

## 9. DEAD AND UNREACHABLE CODE

- **`band_contract.py` — fully deleted, not merely unused.** Grep across the
  whole repo for `band_contract` or `build_report_completion_message` returns
  zero hits outside `CLAUDE.md`/`CODEBASE.md` (docs-only). The file itself
  does not exist in `agents/report/` (confirmed via directory listing).
  CLAUDE.md's instruction to "let its *shape* inform `report_result`'s schema"
  was evidently already acted on and the file removed — CLAUDE.md's own
  migration-status line for report ("`band_agent.py`/`band_contract.py`/
  `room_drain.py`/... deleted") is actually correct here, just not updated to
  say the file is gone rather than merely a migration target.
- **`band_agent.py` — also fully deleted**, confirmed absent from the
  directory listing, consistent with CLAUDE.md's migration-status claim
  (unlike the File Map section further up the same doc, which still lists it
  as a migration *target* rather than already-done — an internal
  inconsistency within CLAUDE.md itself, not a code issue).
- **`generate_band_ready_message`'s hardcoded target** (`intelligence.py:270`,
  `"@muhammad-abdullah"`) — the function still runs every report (it's part
  of `generate_intelligence`'s mandatory sequence) and its output is embedded
  in the PDF ("Band-Ready Final Message" section,
  `pdf_generator.py:108-109`), but nothing sends this message anywhere
  post-migration — it's inert data displayed in a PDF section whose name
  ("Band-Ready") no longer means anything operationally. Not fully dead code
  (it does render into a real PDF section) but its purpose (routing to a Band
  room) is gone; the section is now just more narrative text under a stale
  label.
- **`_frontend_map_url`** (`pipeline.py:262-269`) — defined, never called
  anywhere in `pipeline.py` or elsewhere in the agent (the actual `map_url`
  is always the public R2 URL, set at `pipeline.py:158`). Dead function.
- **`deterministic_summary`/`deterministic_detailed_report` exist in TWO
  places** — `generator.py:676-704` and `llm_clients.py:920-951` — nearly
  identical implementations. `generator.py`'s copies are used by
  `generate_offline_contract_report`/`normalize_detailed_report` (this
  module's own callers); `llm_clients.py`'s copies are used by its own
  `generate_detailed_report_with_featherless`/`normalize_detailed_report`
  (different function, same name, in a different module) and
  `generate_composite_detailed_report_with_featherless`'s fallback
  construction. Not strictly dead, but a duplicated-logic smell — a threshold
  or wording change made in one will silently not apply to the other.
- **`generate_detailed_report_with_featherless`** (`llm_clients.py:115-137`)
  — a single-call (non-composite) variant. Grepped for callers: not called
  from `generator.py` (which uses the composite version,
  `generate_composite_detailed_report_with_featherless`). Appears superseded/
  unreachable from the production path.
- **`featherless_json_call`** (`llm_clients.py:552-571`) — a simplified
  wrapper around `featherless_json_cascade`; not called from `generator.py`
  or `intelligence.py` in this review (all call sites use
  `featherless_json_cascade` directly). Likely dead or reach-only.
- **`smart_critical_call`** (`llm_clients.py:597-621`) — not called anywhere
  in `generator.py`/`intelligence.py`/`pipeline.py`. Dead.

---

## 10. GAP LIST

| # | Issue | Type | Severity | Effort | Evidence |
|---|---|---|---|---|---|
| 1 | Node never passes `incoming_payload`/live `PipelineState` data into `run_report_pipeline` — report stage is 100% DB-sourced despite CLAUDE.md's "PipelineState is the source of truth" framing; the two new satellite-confidence keys `_collect_confidence_values` reads are dead on this path | contract | Medium | Small | `node.py:19-36`; `db_client.py:501-502` |
| 2 | `impact_data.overall_confidence` may not exist on live Neon (per CLAUDE.md's flagged schema gap); if true, `_fetch_impact_data`'s SELECT throws and **every** report generation currently fails outright, not degrades | contract | Critical (if column truly absent) | Small (verify), Small (fix: `ADD COLUMN`) | `db_client.py:227-252`, `:761-764`; CLAUDE.md Database section |
| 3 | No prompt or rendered PDF text ever states SAR calibration status (`index_calibrated`) — a SAR-path report's printed `mean_value` carries no caveat that it is physically uninterpretable raw-DN log data | science | High | Small | `intelligence.py:437-463` (`_compact_context`); `generator.py:684-689`; `llm_clients.py:929-936` |
| 4 | `_assert_required_report_sections` does not check `response_priorities`/`assumptions`/`limitations` — these can silently be template text even when strict mode "passes" | correctness | Medium | Small | `generator.py:623-637` vs `llm_clients.py:912-917`/`normalize_detailed_report` |
| 5 | `_assert_live_intelligence_sources` only enforces the anomaly-check section — criticality, map narrative, priority timeline, decision brief, quality check, and band-ready message can all silently fall back to deterministic/template content with `status: "complete"` | correctness | Medium | Small–Medium | `generator.py:608-620` |
| 6 | Two independently-computed confidence numbers appear in the same PDF (`confidence_level` via `min()` aggregation, `intelligence.criticality.overall_confidence` via LLM/separate formula) with no reconciliation — could visually conflict for the same reader | correctness | Medium | Medium | `db_client.py:403-418` vs `intelligence.py:19-47`/`802-826`; `pdf_generator.py:267` |
| 7 | `min()`-dominant confidence aggregation is a defensible conservative-engineering choice but is not a statistically rigorous combination of independent uncertainty estimates; every input is itself an uncalibrated LLM self-rating or hand-set constant | science | Low–Medium (documented, intentional trade-off) | Large (would need real calibration work) | `db_client.py:403-418` comment; `intelligence.py:33` ("number from 0.0 to 1.0", no calibration guidance) |
| 8 | `determine_recommended_response_level` and `deterministic_intelligence`'s criticality rule use different, unreconciled population thresholds (100K/1M vs 250K) for similar "how bad" judgments, neither cited to any source | correctness | Low | Small | `generator.py:707-728` vs `generator.py:448` |
| 9 | `band_contract.py`/`band_agent.py` fully deleted already — CLAUDE.md's File Map section still lists them as pending migration targets (inconsistent with its own Agent Migration Status section, which correctly says deleted) | dead code (docs) | Low | Small | repo file listing; CLAUDE.md File Map vs Migration Status sections |
| 10 | `generate_band_ready_message`'s hardcoded `"@muhammad-abdullah"` target and "Band-Ready Final Message" PDF section render every report but no longer route anywhere post-migration — vestigial labeling | dead code | Low | Small | `intelligence.py:242-275`; `pdf_generator.py:108-109` |
| 11 | Duplicate `deterministic_summary`/`deterministic_detailed_report` implementations in `generator.py` and `llm_clients.py` — a wording/threshold fix in one will not propagate to the other | dead code / maintainability | Low | Small | `generator.py:676-704`; `llm_clients.py:920-951` |
| 12 | `generate_detailed_report_with_featherless`, `featherless_json_call`, `smart_critical_call`, `_frontend_map_url` defined but not called from any production path in this review | dead code | Low | Small | `llm_clients.py:115-137, 552-571, 597-621`; `pipeline.py:262-269` |
| 13 | `node.py` hardcodes all four pipeline flags (`fetch_from_db/upload_r2/write_db/use_llm=True`) with no way to run report in a degraded/offline mode from the graph — any transient DB/LLM outage fails the whole event rather than degrading (contrast with the CLI's `--contract-test`) | performance/resilience | Low | Medium | `node.py:30-36` |

---

## Worst finding, summarized

If `impact_data` on live Neon truly lacks the `overall_confidence` column (as
CLAUDE.md's Database section claims), then `_fetch_impact_data`'s SELECT at
`db_client.py:227-252` throws on every single report generation, and the
error path (`_schema_mismatch` → loud `RuntimeError` → `status: "failed"`)
means **every event currently fails at the report stage in production**, not
degrades — a far more severe consequence than the "known schema mismatch,
flagged not fixed" framing in CLAUDE.md implies, and the single most
important thing to verify against the live database before trusting the
pipeline's green/red status.
