# Impact Agent — Analysis

Scope: `agents/impact/`. Verified against code as of branch `analysis/all-agents`
(from `main` @ `b1be94f`). Every claim below is checked against source, not
copied from `CLAUDE.md`/`CODEBASE.md`/`root_cause.md` — disagreements are called
out explicitly.

---

## 1. RESPONSIBILITY

The impact agent answers one question: **given a hazard verdict (risk level +
bbox + affected cities), how many people and how much critical infrastructure
sit in harm's way, and how vulnerable is that population?** It is the third of
four pipeline stages (`satellite -> hazard -> impact -> report`).

What it uniquely owns:
- Population-affected estimation (`tasks/population.py`)
- Infrastructure-at-risk counting — hospitals/schools/roads/bridges
  (`tasks/infrastructure.py`)
- Vulnerability scoring + evacuation planning (`tasks/vulnerability.py`)
- The **no-significant-disaster gate** — refusing to fabricate a population
  when the hazard stage found nothing (`agent.py:35-47`)

What is explicitly NOT its job:
- Determining whether a hazard exists or its severity — it takes `risk_level`
  as given from `hazard_result`, it never re-derives risk.
- Any raster/satellite processing — it never touches satellite imagery,
  NDWI/SAR indices, or R2 image URLs in the live pipeline path
  (`services/r2_reader.py` exists but is not called from
  `agent.py`/`node.py`/`tasks/*` — see section 9).
- Report formatting, PDF/map rendering — downstream of impact.

Where it blurs with **hazard** (upstream): `node.py:32-38` reconstructs a
`risk_level`/`severity` from `hazard_result["hazard"]` with a 3-way fallback
chain (`flood_risk` -> `overall_severity` -> `risk_level` -> `"UNKNOWN"`),
meaning impact re-interprets hazard's output shape rather than hazard handing
over a single canonical field. It also **completely discards** hazard's own
earthquake/landslide risk levels: `agent.py:137-138` hardcodes
`"earthquake_risk": "LOW", "landslide_risk": "LOW"` regardless of what hazard
actually computed for those two disaster types (see section 3, and gap list
item 3). Only `flood_risk` (as `risk_level`) survives the handoff.

Where it blurs with **report** (downstream): impact's `agent.py` builds a
Band-message-shaped `json_data` dict (`from`/`to`/`step` fields,
`agent.py:206-230`) that is transport-shaped from the pre-migration Band era,
not pure `PipelineState` data — `node.py:61` does `json.loads(raw)` to unwrap
it. Report's own intelligence layer re-derives some vulnerability/evacuation
narrative from impact's numbers (out of scope here, see report's own
ANALYSIS.md).

---

## 2. EXECUTION FLOW

### Entry: `node.py:impact_node(state)`

1. Reads `state["event_id"]`, `state.get("hazard_result")` (default `{}`),
   drills into `hazard_result["hazard"]` (default `{}`).
2. Derives `risk_level` via fallback chain: `hazard.get("flood_risk")` or
   `hazard.get("overall_severity")` or `hazard.get("risk_level")` or
   `"UNKNOWN"` (`node.py:32-37`).
3. Derives `severity` similarly (`overall_severity` or `severity` or
   `risk_level`) (`node.py:38`).
4. Derives `overall_conf`: if `hazard["confidence_scores"]` is a non-empty
   dict, **averages its values**; on any `TypeError`/`ValueError`/
   `ZeroDivisionError`, or if the dict is missing/empty, falls back to
   `hazard.get("overall_confidence", 0.0)` (`node.py:39-46`). This is the
   **only place upstream (hazard) confidence is read at all** — see section 5
   for whether it's used correctly downstream.
5. Extracts `bounds` and `risk_cities` with a `hazard_result`-then-`hazard`
   fallback (`node.py:48-49`).
6. Calls `await run_impact_analysis(event_id, bounds, risk_level, severity,
   hazard_zones_geojson=hazard.get("risk_polygons") or {},
   flood_depth_estimate=..., overall_confidence=overall_conf, risk_cities=...)`
   (`node.py:51-60`). Note: `hazard.get("risk_polygons")` — CLAUDE.md flags
   hazard's `risk_polygons` as **always `{}`** (unimplemented); if true,
   impact always receives an empty GeoJSON here regardless of real hazard
   zones. Not independently re-verified in this pass (hazard agent's own
   analysis owns that claim) but the wiring here is confirmed: whatever
   hazard puts in `risk_polygons` is what impact gets, no transformation.
7. `raw = json.loads(...)` the returned JSON string.
8. If `result.get("status") != "complete"` -> returns `status: "failed"`,
   appends to `state["errors"]`, still writes `impact_result` (partial).
9. On success: builds `confidence_scores` dict (copies state's existing one,
   adds `"impact"` key from `result["data"]["overall_confidence"]` if
   present), builds `anomalies` list (state's existing + impact's own,
   tagged `{"stage": "impact", "message": a}`), returns `impact_result`,
   `status: "report"`, `current_step: "impact"`, `progress: 75`,
   `confidence_scores`, `anomalies`.

### `agent.py:run_impact_analysis(...)`

1. Builds `bbox = [west, south, east, north]` from `bounds` dict, defaulting
   each missing key (`west`/`south` -> 0, `east`/`north` -> 1) — i.e. an
   empty `bounds` dict silently becomes bbox `[0, 0, 1, 1]`, a valid-looking
   1x1 degree box at the Gulf of Guinea (0,0). No validation/rejection of
   this degenerate case (contrast with hazard's own `bbox` length check per
   `root_cause.md`).
2. Builds `hazard_data` dict — the shape every task function consumes. This
   is where `earthquake_risk`/`landslide_risk` get hardcoded to `"LOW"`
   (`agent.py:137-138`) — confirmed, see section 1.
3. **Gate check**: `_no_significant_disaster(risk_level, overall_confidence)`
   (`agent.py:35-47`). Pure string match on `risk_level.strip().upper()` in
   `{"LOW","NONE","UNKNOWN","","MINIMAL","NEGLIGIBLE"}`. `overall_confidence`
   parameter is accepted but **never read** inside the function body — the
   gate is entirely risk-level-driven, confidence plays no role despite
   being in the signature. Override via `IMPACT_FORCE_ASSESS=true` env var
   (checked first, short-circuits to always run the full pipeline).
   - **If gated**: calls `_emit_no_impact(...)` — builds an all-zero
     `json_data`, attempts a DB write of zero values (non-fatal on
     failure), returns the JSON string. **Population/infrastructure/
     vulnerability tasks never run.** No LLM calls happen on this path.
4. **If not gated** — the real pipeline:
   ```python
   pop, infra = await asyncio.gather(
       run_population_task(hazard_data, event_id),
       run_infrastructure_task(hazard_data, event_id),
   )
   vuln = await run_vulnerability_task(hazard_data=hazard_data,
                                        population_result=pop,
                                        infrastructure_result=infra)
   ```
   **Confirmed**: population + infrastructure run in parallel via
   `asyncio.gather` (`agent.py:162-165`), vulnerability runs sequentially
   after, consuming both results (`agent.py:168-172`) — matches CLAUDE.md's
   claim exactly.
   - Wrapped in try/except: any exception -> logs full traceback, returns
     `{"status": "error", "error": str(tb[-400:])}` (last 400 chars of
     traceback only — could truncate the actual exception message if the
     traceback is long).
5. **DB write** (non-fatal): if `NEON_DATABASE_URL` set, calls
   `write_impact_data(event_id, pop, infra, vuln)`; any exception is caught
   and logged, pipeline continues regardless (`agent.py:179-186`).
6. **Anomaly detection**: `hospitals > 10` -> CRITICAL anomaly string;
   `overall_confidence < 0.7` -> low-confidence anomaly string
   (`agent.py:189-200`). Both purely string messages appended to a list, no
   structured severity field.
7. **Payload assembly**: `total_affected`, `high_risk_people` (default
   `int(pop_count * 0.2)` if task didn't return one), `medium_risk_people`
   (default `int(pop_count * 0.5)`), `hospitals_at_risk`, `schools_at_risk`,
   `roads_blocked` (rounded to 1 decimal from `roads_blocked_km`),
   `bridges_at_risk`, `vulnerability_score` (stringified), `evacuation_routes`
   (from `vuln["priority_zones"]` — **not** `vuln["evacuation_routes"]`, see
   section 6 field-mismatch), `estimated_evacuation_time` (`infra`'s value or
   `vuln`'s, or `"4-6 hours"` default), `overall_confidence` (verbatim
   pass-through of the **input** parameter — hazard's confidence, never
   impact's own task confidences — see section 5).
8. Returns `json.dumps(json_data)`.

### Call graph summary
```
impact_node
 -> run_impact_analysis
     - _no_significant_disaster(risk_level, overall_confidence)   [gate]
         (gated) -> _emit_no_impact -> write_impact_data(zeros)
     - (not gated)
         - asyncio.gather(
               run_population_task    -> _fetch_geonames_population -> smart_llm_call
               run_infrastructure_task -> _fetch_overpass            -> smart_llm_call
           )
         - run_vulnerability_task -> smart_llm_call
         - write_impact_data
```

### Failure modes per stage (see also section 7 for the full table)
- GeoNames fetch fails -> `None`, population task falls back to LLM-only
  estimate with no real-data anchor.
- All 3 Overpass endpoints fail -> `None`, infrastructure task falls back to
  LLM-only estimate.
- LLM returns non-JSON / all providers fail -> population/infrastructure/
  vulnerability each have their own default/fallback (see sections 3/4).
- Whole-pipeline exception (e.g. an unexpected `KeyError`) -> caught at the
  `run_impact_analysis` try/except boundary, degrades to `status: "error"`,
  no partial data returned.
- DB write failure -> logged, swallowed, pipeline still reports `"complete"`.

---

## 3. DECISION LOGIC

### Deterministic decisions (no LLM)

| Decision | Logic | File:line |
|---|---|---|
| No-significant-disaster gate | `risk_level.upper()` in `{LOW,NONE,UNKNOWN,"",MINIMAL,NEGLIGIBLE}` | `agent.py:44-47` |
| Population criticality routing | `pop>2M->critical`, `pop>500k->high`, else `normal` | `tasks/population.py:147-152` |
| Population escalation trigger | `criticality in (high, critical)` -> re-run at that tier | `tasks/population.py:154-158` |
| Population LLM-zero retry | `not result or pop==0` -> one retry with "significant" nudge prompt | `tasks/population.py:165-172` |
| Population conservative floor | `pop==0` after retry -> `max(real_pop*0.02, 500)` | `tasks/population.py:177-184` |
| Infrastructure escalation trigger | `hospitals>10` -> re-run at `"high"` | `tasks/infrastructure.py:190-195` |
| Infrastructure hard-coded default (LLM None) | uses raw OSM counts, `roads_blocked_km=0`, `confidence=0.3` | `tasks/infrastructure.py:199-208` |
| Vulnerability criticality routing | `pop>2M or (flood==CRITICAL and eq==CRITICAL)->critical`; `pop>1M or hospitals>10 or all_high->high`; `pop>500k or hospitals>5->high` (duplicate branch, see below); else `normal` | `tasks/vulnerability.py:143-151` |
| Vulnerability LLM-None default | `score=5.0`, `confidence=0.3`, empty zones/routes | `tasks/vulnerability.py:160-169` |
| Anomaly: too many hospitals | `hospitals>10` | `agent.py:191-195` |
| Anomaly: low confidence | `overall_confidence<0.7` | `agent.py:196-200` |
| high_risk/medium_risk defaults | `pop_count*0.2` / `pop_count*0.5` when task omits them | `agent.py:216-217`, `services/db.py:89-90` |

**Bug found**: `tasks/vulnerability.py:146-148` has two branches that both
resolve to `criticality = "high"` — `pop > 1_000_000 or hospitals > 10 or
all_high` and `pop > 500_000 or hospitals > 5` are functionally the same
outcome, making the second `elif` add no new tier (it only broadens which
cases reach `"high"`, since there's no distinct `"critical"`/other tier it
could have been meant to reach at that point). No comment explains the
duplication; it reads like a copy-paste where one branch was meant to
produce a different level.

### LLM-driven decisions

All three tasks route through `services/llm_router.py:smart_llm_call(prompt,
criticality, task_name)`. See section 8 for the exact escalation chain. Every
substantive number (population_affected, hospitals_at_risk, roads_blocked_km,
vulnerability_score, priority_zones, evacuation_routes) is **LLM output**,
not computed from OSM/GeoNames data directly — OSM/GeoNames are fed into the
prompt as "REAL DATA" context, and the LLM is instructed to "reason" from it,
but nothing in code enforces the LLM's output is arithmetically derived from
those real numbers (see section 4 for how loose this coupling is).

### Every magic number, with stated-basis-or-not

| Value | File:line | Stated basis? |
|---|---|---|
| `pop > 2_000_000` -> critical | `tasks/population.py:147` | No — no citation, feels like a round-number author heuristic |
| `pop > 500_000` -> high | `tasks/population.py:149` | No |
| `real_pop * 0.02` floor, min 500 | `tasks/population.py:180` | No — arbitrary "2% of admin population" and arbitrary 500 floor |
| `high_risk_people ~= 0.2 * pop` | `agent.py:216`, prompt says "approx 20%" | Stated in prompt as a modeling assumption, not derived from any real disaster-response study cited in code |
| `medium_risk_people ~= 0.5 * pop` | `agent.py:217`, prompt "approx 50%" | Same — asserted, not sourced |
| `vulnerable_estimate ~= 0.18 * pop` | `agent.py:188-190` | No citation ("children under 5 + elderly over 65" — a real demographic category but 18% is not sourced) |
| `hospitals > 10` -> high/critical/anomaly | `tasks/infrastructure.py:190`, `agent.py:191`, `criticality.py:26` | No |
| Overpass timeout 30s (query) / 35s (HTTP) | `tasks/infrastructure.py:40,64` | No — operational choice, undocumented |
| GeoNames timeout 10s | `tasks/population.py:44` | No |
| `pop > 1_000_000` / `500_000` (vulnerability criticality) | `tasks/vulnerability.py:144,146,148` | No |
| Vulnerability score floor rules (`score>=8.0`, `>=6.5`, `>=9.0`) | `tasks/vulnerability.py:54-56` (prompt text, not code-enforced) | Stated as "rules" in the prompt but **not verified or clamped in code** — the LLM is trusted to apply them; nothing in `run_vulnerability_task` checks the returned score against these floors |
| `all_routes_blocked` when `roads_blocked_km > 100` | `tasks/vulnerability.py:70` (prompt only) | Same — prompt instruction, not code-enforced |
| `max_tokens = 8192` for Gemini, `2048` otherwise | `services/llm_router.py:116` | Explained in a comment (avoid truncated JSON) |
| Featherless `max_attempts=8` vs others `3` | `services/llm_router.py:129` | Explained (shared 4-concurrency cap) |
| 429 backoff `min(5+3*attempt, 20)` | `services/llm_router.py:153` | No formula justification, just a cap |
| `confidence >= 0.6` escalation threshold (normal tier) | `services/llm_router.py:259` | No |
| Cost-per-call table (`$0.001`/`$0.015`/`$0.01`/`$0.002`) | `services/cost_tracker.py:7-12` | No — approximate, unsourced, likely stale pricing |

### LLM fallback behavior when unavailable — degraded, not equivalent
- Population: falls back to a bare heuristic (`max(real_pop*0.02, 500)`) with
  **no infrastructure/vulnerability signal at all** — clearly a degraded,
  not-equivalent substitute for real reasoning.
- Infrastructure: falls back to **raw OSM counts verbatim** as "at risk"
  (i.e., every hospital/school/bridge in the bbox, not a "subset in the
  flood zone" as the LLM was asked to estimate) with `roads_blocked_km=0`
  and `confidence=0.3` — a materially different (more conservative on
  roads, more aggressive on facility counts) output shape than the LLM path.
- Vulnerability: falls back to a flat `score=5.0`, empty zones/routes,
  `confidence=0.3` — no real place names, no evacuation routes at all. This
  is a floor default, not a real analysis, and is silently indistinguishable
  from a genuine mid-range LLM score of exactly 5.0 downstream (no explicit
  "is_fallback" flag on the returned dict itself, only `confidence: 0.3` as
  the implicit signal — and nothing consumes that signal specially).

---

## 4. THE ANALYSIS ITSELF (the science)

### Population-affected — NOT a real model, a bounded-heuristic LLM guess

`tasks/population.py` fetches one real number: administrative city population
from GeoNames (`_fetch_geonames_population`, `tasks/population.py:40-60`) via
`GEONAMES_BASE = "http://api.geonames.org/searchJSON"`, `maxRows=1`. This is
a **single city-level population figure**, not a spatial population-density
raster (no WorldPop/GPWv4/LandScan or similar gridded population product is
used anywhere in the codebase — confirmed by absence of any raster
population read in `tasks/*` or `services/*`).

The prompt (`_build_prompt`, `tasks/population.py:63-122`) explicitly tells
the LLM: *"The actual metro/urban area population is significantly higher —
typically 2x to 5x the administrative figure... GeoNames figure is a minimum
floor only, not the ceiling."* This is an **assertion embedded in the prompt
text**, not a computed multiplier — there is no code that applies a 2x-5x
factor; the LLM is simply told this fact and asked to use "geographic
knowledge" to arrive at a number. The LLM is then asked to estimate what
fraction of that (self-estimated) metro population is in the "high-risk
flood zone" (~20%) and "medium risk" (~50%) — **both percentages are
prompt-supplied constants** ("approx 20%"/"approx 50%"), not derived from
any flood-extent geometry, actual bbox area, elevation, or land-cover data.
No flood polygon (`hazard_zones_geojson`) is ever passed into the population
prompt at all — `_build_prompt` never receives
`hazard_data.get("hazard_zones_geojson")`; only `severity`, `flood_risk`, and
`bbox` (as an area-in-sq-km number, `_area_sq_km`) reach the prompt.

**Verdict: population_affected is an LLM-generated point estimate, loosely
anchored to one real administrative population figure and a prompt-asserted
urbanization multiplier, with prompt-fixed 20%/50% risk-fraction splits. It
is not a defensible epidemiological or geospatial exposure model** — there
is no intersection of a population raster with a flood/hazard polygon, which
is the standard method (e.g., WorldPop x flood extent) for this kind of
estimate. The conservative fallback (`real_pop * 0.02`, min 500) when the
LLM returns 0 is even less defensible — a bare 2% multiplier with no stated
origin.

### Infrastructure-at-risk — real counts, unenforced "at risk" subsetting

`_fetch_overpass` (`tasks/infrastructure.py:59-106`) queries real OSM data
via Overpass QL for hospitals/clinics, schools/universities, bridges, and
major roads within the bbox — this **is** a real, verifiable data source
(unlike population). The query (`tasks/infrastructure.py:34-56`) is a
legitimate exact tag-match query, no obvious errors.

However, the "at risk" figure is NOT simply the OSM count — the LLM is asked
to determine "which fraction is in the actual flood zone." Nothing in code
constrains the LLM's `hospitals_at_risk` to be `<=` the real OSM `hospitals`
count; there is no clamp or sanity check comparing the LLM's returned count
against the real Overpass count. So the "real data" anchor can be silently
overridden or exceeded by LLM invention, and code has no guardrail against
that (confirmed: `run_infrastructure_task`, `tasks/infrastructure.py:165-226`,
never compares `result["hospitals_at_risk"]` to `osm["hospitals"]`).

`roads_blocked_km` has **no real-data anchor at all** — Overpass returns a
raw road-segment *count* (`major_roads`), and the LLM is asked to estimate a
**length in km** from that count and "flood extent" with no formula, no
average-segment-length constant, nothing. This is a pure LLM guess dressed
as a derived figure.

### Vulnerability score — LLM applies stated rules, code never checks them

`tasks/vulnerability.py`'s prompt embeds explicit scoring rules ("population
> 1,000,000 AND hospitals > 10: score minimum 8.0", etc., lines 53-57) but
these are **prompt instructions only** — `run_vulnerability_task` never
validates the returned `vulnerability_score` against them. If the LLM
ignores the rule (a documented LLM failure mode elsewhere in this codebase
per root CLAUDE.md's "LLMs inflate risk from reputation" note — here the
opposite risk, an LLM under-scoring, is equally unguarded), nothing catches
it. `all_routes_blocked` similarly has a stated rule ("true if
roads_blocked_km > 100") that is never verified against the actual `roads`
value passed into the prompt.

### Assumptions and whether inputs satisfy them
- Assumes `risk_cities[0]` is a resolvable, geocodable city name recognized
  by GeoNames — no validation of `risk_cities` content upstream in this
  agent (hazard/satellite own that).
- Assumes bbox is a real, non-degenerate box — `agent.py`'s `bbox`
  construction (section 2, step 1) silently defaults to `[0,0,1,1]` on
  missing `bounds` fields, which would then flow into `_area_sq_km` as a
  real ~146k sq km box in the Gulf of Guinea, an assumption violation with
  no safeguard.
- Assumes the LLM has reliable "geographic knowledge" of arbitrary global
  cities for place names/road names — no verification these are real (the
  prompt explicitly asks for "real place names... you actually know," which
  is an unenforceable instruction to an LLM; hallucinated place names are
  possible and undetectable in code).

**Bottom line on scientific soundness**: infrastructure counts are the most
defensible piece (real OSM data, though the "at risk" subset is
ungoverned). Population-affected is the least defensible — a heuristic LLM
estimate built on prompt-asserted multipliers and fixed percentage splits,
not a population-x-hazard-extent geospatial computation. Vulnerability
score is a prompt-only rules engine with zero code-side enforcement.

---

## 5. CONFIDENCE

### What feeds impact's own "overall_confidence"

**Critical finding: impact's `overall_confidence` in its output payload is
NOT computed from impact's own tasks at all.** Trace:

- `node.py:39-46` computes `overall_conf` **from hazard's confidence_scores**
  (averaged) or `hazard.get("overall_confidence")`.
- `node.py:58` passes this as `overall_confidence=overall_conf` into
  `run_impact_analysis`.
- `agent.py:150` uses it only for the no-disaster gate check (though the
  gate function itself never actually reads the parameter — see section 2,
  step 3 bug).
- `agent.py:196` uses it for the low-confidence anomaly trigger.
- `agent.py:228` writes it **verbatim, unchanged** into
  `json_data["data"]["overall_confidence"]`.

So the number impact reports as its own confidence is **hazard's
confidence, pass-through, with zero contribution from population/
infrastructure/vulnerability task confidences** — despite each of those
three tasks independently returning their own `confidence` field from the
LLM (`tasks/population.py` prompt asks for `0.7-0.95`;
`tasks/infrastructure.py` asks for `0.7-0.95`; `tasks/vulnerability.py`
asks for `0.7-0.99`). None of `pop["confidence"]`, `infra["confidence"]`,
`vuln["confidence"]` are read, averaged, or referenced anywhere in
`agent.py`'s payload assembly (confirmed: no `pop.get("confidence")` /
`infra.get("confidence")` / `vuln.get("confidence")` call exists in
`agent.py`). Those three task-level confidence numbers are computed by the
LLM and then **discarded**.

This means: root `CLAUDE.md`'s documented gap ("hazard's confidence never
folds satellite's in") has a mirror-image, equally real problem one hop
downstream — **impact's confidence never folds its OWN tasks' confidence in
either; it is 100% a copy of hazard's number.** The compounding described
in root CLAUDE.md (satellite 0.0 -> hazard 0.83 -> report HIGH) is thus
even more direct than stated there: impact doesn't blend hazard's number
with anything, it just relays it unchanged.

### Every penalty/concern, trigger+weight
There is no weighted composite at all — only two independent boolean-trigger
**anomaly strings** (not confidence adjustments):
- `hospitals > 10` -> CRITICAL anomaly text (`agent.py:191-195`) — does not
  change `overall_confidence`, only appends a warning string.
- `overall_confidence < 0.7` -> low-confidence anomaly text
  (`agent.py:196-200`) — again does not adjust the number, just flags it.

No arithmetic combination, no penalty subtraction, no multi-factor weighting
exists anywhere in the impact agent for its own confidence. It is
**calibrated nowhere** — it's a relay of an upstream number plus two
unrelated boolean flags.

### `overall_confidence` DB column — confirmed missing, confirmed unused downstream write, confirmed live-breaking read

Verified directly against `services/db.py`'s DDL (`services/db.py:16-33`):
the `CREATE TABLE IF NOT EXISTS impact_data` statement has **no
`overall_confidence` column** — columns are `id, event_id, total_affected,
high_risk_people, medium_risk_people, hospitals_at_risk, schools_at_risk,
roads_blocked, bridges_at_risk, vulnerability_score, evacuation_routes,
estimated_evacuation_time, created_at, updated_at`. The `INSERT`/`ON
CONFLICT` statement (`services/db.py:58-97`) matches the DDL exactly —
`overall_confidence` is never in the column list, never in the `$1..$11`
bind list. **CLAUDE.md's claim is confirmed still true.**

Repo-wide grep for `ALTER TABLE impact_data` / any `ADD COLUMN` targeting
this table returns **zero matches** — there is no migration anywhere in the
repo that could have added this column to the live table either.

Downstream: `agents/report/db_client.py:_fetch_impact_data` (line 227-252)
explicitly `SELECT`s `overall_confidence` from `impact_data`
(`db_client.py:243`). **If the live Neon table's schema matches this
repo's DDL exactly** (which `write_impact_data`'s idempotent `CREATE TABLE
IF NOT EXISTS` would produce on a fresh table, and which nothing in this
repo alters), **this SELECT will raise `asyncpg.exceptions.
UndefinedColumnError` at runtime** — a hard failure, not a silent null.
This is a stronger claim than CLAUDE.md's phrasing ("missing... what
report's reader does when the column doesn't exist" was posed as an open
question) — based on the code alone, this resolves to **the query
errors**, unless an out-of-repo manual schema patch on the live Neon DB
has already added the column (impossible to confirm from static analysis;
flagged as the key uncertainty). This is the single most severe concrete
finding in this analysis — see gap list item 1.

### Calibrated or heuristic?
Entirely heuristic / relay. No statistical calibration, no validation
against ground truth, no documented methodology for combining or scoring
confidence at the impact stage.

---

## 6. DATA CONTRACT

### Inputs consumed (from `PipelineState`/hazard_result via `node.py`)

| Field | Type | Producer | Notes |
|---|---|---|---|
| `state["event_id"]` | str (UUID) | backend | required |
| `state["hazard_result"]["hazard"]["flood_risk"]` / `overall_severity` / `risk_level` | str | hazard | 3-way fallback chain, first non-null wins |
| `state["hazard_result"]["hazard"]["confidence_scores"]` | dict[str,float] | hazard | averaged if present and non-empty |
| `state["hazard_result"]["hazard"]["overall_confidence"]` | float | hazard | fallback if `confidence_scores` missing/empty/unparseable |
| `state["hazard_result"]["bounds"]` / `hazard["bounds"]` | dict{west,south,east,north} | hazard | defaults to `{}` -> bbox `[0,0,1,1]` |
| `state["hazard_result"]["risk_cities"]` / `hazard["risk_cities"]` | list[str] | hazard | defaults `[]` |
| `hazard.get("risk_polygons")` | GeoJSON dict | hazard | passed to `hazard_zones_geojson` — per hazard's own known gap, likely always `{}` |
| `hazard.get("flood_depth_estimate")` | float | hazard | defaults `0.0` |

### Outputs produced

**`impact_result` (in `PipelineState`, via `node.py` return)** — the parsed
JSON from `run_impact_analysis`, shape:
```
event_id, agent, from, to, status, step, anomalies[],
data: {
  total_affected, high_risk_people, medium_risk_people,
  hospitals_at_risk, schools_at_risk, roads_blocked, bridges_at_risk,
  vulnerability_score (str), evacuation_routes, estimated_evacuation_time,
  overall_confidence, [no_significant_impact, assessment_note on gate path]
}
```

**`impact_data` DB table** (actual DDL, `services/db.py:16-33` — confirmed
current, NOT `schema.sql`'s version, matching root CLAUDE.md's claim):
```
id SERIAL, event_id TEXT UNIQUE NOT NULL, total_affected INTEGER,
high_risk_people INTEGER, medium_risk_people INTEGER,
hospitals_at_risk INTEGER, schools_at_risk INTEGER,
roads_blocked INTEGER, bridges_at_risk INTEGER,
vulnerability_score TEXT, evacuation_routes JSONB,
estimated_evacuation_time TEXT, created_at, updated_at
```

**Confirmed mismatches / label-vs-content ambiguities:**

1. **`roads_blocked` is an INTEGER column storing a rounded km figure, not
   a count.** `services/db.py:93`:
   `int(round(float(infra.get("roads_blocked_km", 0) or 0)))` — the source
   field is explicitly named `roads_blocked_km` (kilometres) in the task
   layer, and gets rounded to the nearest integer km for storage. The
   column name `roads_blocked` (no unit suffix) is genuinely ambiguous — a
   reader of the DB schema alone would reasonably assume "number of roads
   blocked" (a count), not "km of road blocked, rounded." The in-memory
   `json_data` payload disambiguates slightly (`agent.py:220`:
   `round(float(infra.get("roads_blocked_km", 0) or 0), 1)` under the key
   `roads_blocked`, kept to 1 decimal there — **note this differs from the
   DB's integer rounding**, so the DB and the live payload can show
   different values for the same run: e.g. `roads_blocked_km=4.6` -> DB
   stores `5`, payload reports `4.6`). This confirms CLAUDE.md's flagged
   ambiguity: yes, it is a km figure mislabeled as a bare count at the
   column-name level, and there is a second, independent DB-vs-payload
   rounding mismatch not previously documented.

2. **`overall_confidence` is computed and included in the in-memory payload
   (`agent.py:228`, `node.py:74-75`) but never written to the `impact_data`
   table** — produced but not persisted; report's SELECT expects it to
   exist as a column regardless (see section 5). Type mismatch in intent,
   not just a gap: report reads it from **the DB**, but the only place
   it's ever actually computed live is in-memory, one hop upstream, and
   thrown away before persistence.

3. **`evacuation_routes` (both in the JSON payload and DB column) is
   actually `vuln["priority_zones"]`, not `vuln["evacuation_routes"]`.**
   `agent.py:223`: `"evacuation_routes": vuln.get("priority_zones", [])`.
   `tasks/vulnerability.py`'s prompt asks the LLM for **both** a
   `priority_zones` array (named places with lat/lon/priority/reason) and
   a separate `evacuation_routes` array (named roads with distance_km/
   status/geojson) — two genuinely different concepts. The DB/payload
   field named `evacuation_routes` is fed `priority_zones` data (place
   names, not route geometries), and the LLM's actual `evacuation_routes`
   output (road-level routing info) is **never persisted or forwarded
   anywhere** — dead output, silently dropped. This is a real
   content/label mismatch, not just a naming quirk: any downstream
   consumer (report agent, frontend) reading `evacuation_routes` expecting
   route/road data gets priority-zone data instead.

4. **`high_risk_people`/`medium_risk_people` "produced" by two different,
   independently-computed default expressions** at two call sites —
   `agent.py:216-217` and `services/db.py:89-90` both independently
   compute `int(pop_count * 0.2)` / `int(pop_count * 0.5)` as the fallback
   when the population task didn't supply these keys. They're consistent
   today (same formula, copy-pasted) but this is two sources of truth for
   the same derived default, not one — a future edit to one is likely to
   silently diverge from the other.

5. `services/db.py`'s `write_results` "legacy alias"
   (`services/db.py:106-118`) is called only by `main.py` (the standalone
   Band-era local test server) — not on the live LangGraph pipeline path
   (`node.py`/`agent.py` call `write_impact_data` directly). Confirmed
   dead on the migrated path, kept alive only for the out-of-scope local
   test server.

6. `hazard_zones_geojson` is accepted as a parameter into
   `run_impact_analysis` (`agent.py:103,115,141`) and placed into
   `hazard_data["hazard_zones_geojson"]`, but **no task function
   (`population`/`infrastructure`/`vulnerability`) ever reads
   `hazard_data.get("hazard_zones_geojson")`** — confirmed via grep, none
   of the three task files reference that key. Produced/threaded through
   but never consumed — dead data on the impact side regardless of
   whether hazard ever populates it with something non-empty.

---

## 7. FAILURE MODES

| Trigger | Swallowed or surfaces? | What's returned | Can downstream tell it apart from success? |
|---|---|---|---|
| GeoNames API failure/timeout/no entries | Swallowed (`except Exception`, `tasks/population.py:58-60`) | `None` -> LLM-only estimate path | No explicit flag in output; `geonames_population` key simply absent from result dict (`agent.py` doesn't surface this to `json_data` at all — GeoNames success/failure is invisible past the population task) |
| All 3 Overpass endpoints fail | Swallowed per-endpoint (`tasks/infrastructure.py:102-103`), logged as error at exhaustion | `None` -> LLM-only estimate | `osm_data_quality: "llm_estimate"` is requested of the LLM in the prompt (`tasks/infrastructure.py:113,160`) but this is LLM-generated text, not code-set — the LLM could ignore the instruction; `osm_source` key is simply absent from result when OSM failed (`agent.py`/`node.py` never forward `osm_data_quality` into the final payload at all — confirmed absent from `json_data["data"]`) |
| LLM returns non-JSON | `_extract_json` returns `None`, logged warning (`services/llm_router.py:136-138`) | Propagates as `None` result up through `smart_llm_call`'s tier logic | Task-level: population retries once then floors to a heuristic; infrastructure/vulnerability substitute a hardcoded default dict — none of these set an explicit `is_fallback`/`degraded` flag consumable by report |
| All LLM providers fail (population) | Not swallowed silently — logged warnings at each tier, but ultimately swallowed into a numeric floor | `pop = max(real_pop*0.02, 500)` | No — looks like a normal small-population result |
| All LLM providers fail (infrastructure) | Swallowed | Raw OSM counts (or 0s) with `confidence: 0.3` | Only via `confidence` field, not read/checked by any consumer in this agent or (confirmed) not persisted to DB either |
| All LLM providers fail (vulnerability) | Swallowed | `score=5.0`, `confidence=0.3`, empty routes/zones | Same — `confidence:0.3` is the only signal, unused |
| Whole-task exception (any of the 3 in `asyncio.gather`/sequential call) | Caught at `run_impact_analysis`'s outer try/except (`agent.py:173-176`) | `{"status": "error", "error": str(tb[-400:])}` | Yes — `node.py:63-71` explicitly checks `result.get("status") != "complete"` and marks the whole pipeline `status: "failed"`, appending to `state["errors"]`. This is the one clean failure path. |
| DB write failure (`write_impact_data`) | Swallowed (`agent.py:183-184`, and `_emit_no_impact`'s own try/except `agent.py:92-93`) | Pipeline still returns `"complete"` | **No** — a DB write failure is fully invisible to `node.py`/downstream state; `impact_result` looks identical whether the DB write succeeded or failed |
| `NEON_DATABASE_URL` unset | Explicit warning log, write skipped (`agent.py:185-186`) | Pipeline still returns `"complete"` | No |
| Degenerate/empty `bounds` -> bbox `[0,0,1,1]` | Not detected at all | Silently proceeds with a real-looking but bogus bbox | No — no validation exists |
| No-significant-disaster gate fires | Not a failure — intentional honest zero, logged info | `status: "complete"`, `no_significant_impact: true`, `assessment_note` string | Yes, explicitly flagged via `no_significant_impact` key and descriptive `assessment_note` — this is the one place a "different kind of success" is clearly distinguishable |

---

## 8. EXTERNAL DEPENDENCIES

| Dependency | Purpose | Timeout | Error handling | Retry/backoff | Behavior when unavailable |
|---|---|---|---|---|---|
| GeoNames (`api.geonames.org/searchJSON`) | Real city population | 10s (`tasks/population.py:44`) | try/except around the whole call, logs warning | None — single attempt, no retry | Falls back to LLM-only estimate (no real-data anchor) |
| Overpass API (3 endpoints, sequential failover) | Real hospital/school/bridge/road counts | 35s per endpoint (`tasks/infrastructure.py:64`), 30s query-internal timeout | try/except per endpoint, continues to next on failure or empty `elements` | Sequential failover across `overpass-api.de` -> `overpass.kumi.systems` -> `maps.mail.ru` — no retry within a single endpoint, no exponential backoff | All 3 fail -> LLM-only estimate for infrastructure |
| Featherless (`api.featherless.ai/v1`, 3-model chain) | Primary/routine LLM tier | Not explicitly set (uses `AsyncOpenAI` defaults) | try/except per model in `_call_model`, distinguishes quota/funds (fail-fast) vs 429-concurrency (retry) | Up to 8 attempts for Featherless specifically, backoff `min(5+3*attempt, 20)`s (`services/llm_router.py:129,152-155`); chain falls through 3 models (gemma->Kimi->Qwen) if each returns `None` | Falls through to escalation tier (Opus/Gemini) per criticality routing, or returns `None` if `criticality=="low"` (low never escalates) |
| AIML API (Opus 4.8 / GPT-5.5) | High/critical escalation | 3 attempts max (`services/llm_router.py:129`) | Same `_call_model` fast-fail-on-quota logic | Up to 3 attempts, same backoff formula | Falls through to Gemini (if `PREFER_GEMINI_ESCALATION` true, which is the default and actually tried FIRST — see below) or GPT last-resort |
| Gemini (OpenAI-compat endpoint) | Preferred escalation (AIML is out of funds per project memory) | 3 attempts | Same | Same backoff | Falls through to AIML Opus, then GPT, then Featherless "safety net" — every tier of `smart_llm_call`'s "high"/"critical" paths has a final Featherless fallback so `None` is rare but possible if literally everything fails |

**Confirmed: impact reads only `GEMINI_API_KEY`, no `_2`..`_5` rotation.**
`services/llm_router.py:67`: `_gemini_client()` reads
`os.environ.get("GEMINI_API_KEY", "")` — a single key, no numbered
variants, no rotation loop. Grep confirms no `GEMINI_API_KEY_2`/`_3`/`_4`/
`_5` reference anywhere in `agents/impact/`. Per project memory, Gemini's
free tier caps at ~20 req/day — **practical effect: once that single key's
daily quota is exhausted, every "normal"-tier escalation and every
"high"/"critical" call in this agent loses its preferred escalation path
in one shot** (falls through to AIML Opus, which per project memory is out
of funds, then GPT — also AIML — then finally the Featherless safety net).
Unlike satellite/hazard/report (which the root CLAUDE.md says read
`_2..._5`), impact has no rotation headroom at all — it is the single most
exposed agent to Gemini quota exhaustion, confirmed by direct code read,
not just repeating the CLAUDE.md claim.

**Cost tracking**: `services/cost_tracker.py`'s singleton accumulates call
counts, but its `reset()` is documented as "reset once per request in
main.py" (module docstring) — `main.py` is the out-of-scope local test
server. On the live LangGraph pipeline path (`agent.py`/`node.py`),
**nothing calls `cost_tracker.reset()`** — confirmed via grep, no
`cost_tracker.reset` outside `main.py`. In a long-lived single process
(per root CLAUDE.md's "Single-Process Hardening" section, the whole graph
now runs in one process across many events), this counter accumulates
across every event indefinitely and is never read or exposed anywhere
except via `get_summary()`, which nothing in the live path calls either —
effectively dead instrumentation on the migrated path.

---

## 9. DEAD AND UNREACHABLE CODE

Confirmed dead (grep-verified, zero call sites on the live pipeline path):

- **`services/featherless.py`** — entire file (`call_with_fallback`,
  `extract_json`, module-level `_featherless_client`/`_aiml_client`). Zero
  references anywhere in `agents/impact/` outside itself. Superseded by
  `services/llm_router.py`'s `smart_llm_call`/`_call_model`. Confirmed
  already-dead per CLAUDE.md, verified independently.
- **`services/criticality.py`** — `determine_criticality` function. Zero
  call sites anywhere in `agents/impact/` (only self-reference in its own
  docstring/logging). The three task files each compute their OWN inline
  criticality logic (population/infrastructure/vulnerability each have
  their own thresholds duplicated in `tasks/*.py`, not delegating to this
  module) — this file is a superseded/orphaned earlier design, confirmed
  dead.
- **`services/band_client.py`** — `send_to_band_room`,
  `send_anomaly_to_band`, `receive_hazard_data`, `send_impact_result`,
  `set_active_room`. Not imported by `agent.py` or `node.py` (confirmed
  via grep — only `main.py` imports `send_impact_result` from it). Dead on
  the migrated LangGraph path, alive only for the out-of-scope Band-era
  local test server. Matches CLAUDE.md's claim exactly.
- **`services/r2_reader.py`** — `fetch_zones_geojson`,
  `get_satellite_urls`. Zero import references anywhere in
  `agent.py`/`node.py`/`tasks/*.py` (confirmed via grep — no `from
  services.r2_reader` outside the file itself). Not flagged in CLAUDE.md's
  dead-code list but **is dead on the live pipeline path** — a gap in the
  existing docs. Presumably a leftover from an earlier design where impact
  read satellite imagery URLs directly; the current pipeline never does
  (impact only reads `hazard_result`, not `satellite_result`, per
  `node.py`'s signature).
- **`main.py`** — the whole file is the pre-migration Band-era local
  FastAPI test server (`USE_MOCK_BAND`, `/assess-impact`). Confirmed out
  of scope by root CLAUDE.md's "Commands" section (still documented as a
  way to run a local test server), so not "dead" in the sense of being
  deletable without discussion, but **not part of the live LangGraph
  pipeline** — `graph.py` never imports it.
- **`services/db.py`'s `write_results`** — the "Legacy alias" function
  (`services/db.py:106-118`), called only by `main.py`. Dead on the
  migrated path specifically (see section 6, point 5).
- **`hazard_data["hazard_zones_geojson"]`** — produced/threaded by
  `agent.py` but never read by any task (see section 6, point 6) — dead
  data, not dead code, but worth flagging alongside.
- **`cost_tracker.reset()`** — never called outside `main.py` on the live
  path (see section 8) — the tracker itself isn't dead code (it IS
  incremented via `.track()` inside `_call_model`), but its
  `reset()`/`get_summary()` API is unreachable on the migrated pipeline,
  making the accumulated state useless in practice.

---

## 10. GAP LIST

| # | Issue | Type | Severity | Effort | Evidence |
|---|---|---|---|---|---|
| 1 | `impact_data` table has no `overall_confidence` column (DDL confirmed); report's `_fetch_impact_data` explicitly `SELECT`s it — almost certainly a hard runtime error (`UndefinedColumnError`) on every report-stage DB read, unless the live table was patched outside the repo | contract | Critical | Low (add column + backfill write) | `agents/impact/services/db.py:16-33` (DDL, no column); `agents/report/db_client.py:227-252` (SELECT includes it) |
| 2 | Impact's own `overall_confidence` output is a pure pass-through of hazard's confidence — none of population/infrastructure/vulnerability's own LLM-reported `confidence` fields are read, averaged, or folded in anywhere | science/correctness | High | Medium | `agent.py:228` (writes input param verbatim); no `pop.get("confidence")`/`infra.get("confidence")`/`vuln.get("confidence")` reference anywhere in `agent.py` |
| 3 | `agent.py` hardcodes `earthquake_risk`/`landslide_risk` to `"LOW"` regardless of hazard's actual computed values for those two disaster types — impact only ever reasons about flood risk even on non-flood events | correctness | High | Low | `agent.py:137-138` |
| 4 | `evacuation_routes` field (both JSON payload and DB column) is populated from `vuln["priority_zones"]`, not `vuln["evacuation_routes"]` — the LLM's actual route-level output (road names/distances/geojson) is generated then silently discarded | contract | Medium | Low | `agent.py:223` vs `tasks/vulnerability.py` prompt (asks for both `priority_zones` and separate `evacuation_routes` arrays) |
| 5 | `roads_blocked` DB column (INTEGER) stores a rounded km figure under a name that reads as a count; separately, the DB rounds to nearest integer km while the in-memory payload rounds to 1 decimal — same run can show two different `roads_blocked` values in DB vs live payload | contract | Medium | Low | `services/db.py:93` vs `agent.py:220` |
| 6 | `_no_significant_disaster` gate accepts `overall_confidence` as a parameter and it appears in the docstring's reasoning, but the function body never reads it — the gate is 100% risk-level string matching; the confidence check happens only later (anomaly flag, not gate logic) | correctness | Low | Low | `agent.py:35-47` (parameter unused in body) |
| 7 | Population-affected has no real geospatial exposure computation (no population raster x hazard-extent intersection) — it is an LLM point estimate anchored to one city-level GeoNames figure and prompt-asserted 2x-5x urbanization multiplier + fixed 20%/50% risk-fraction splits | science | High | High (would need a population raster source + real overlay) | `tasks/population.py:63-122` (prompt text, no code-side multiplier/overlay logic) |
| 8 | No code-side clamp ties LLM-reported `hospitals_at_risk`/`schools_at_risk` to the real Overpass counts fetched for the same request — LLM can report a number unrelated to (or exceeding) the real data it was given as "REAL DATA" context | science/correctness | Medium | Low | `tasks/infrastructure.py:165-226` (no comparison against `osm["hospitals"]` etc.) |
| 9 | Vulnerability scoring "rules" (score floors, `all_routes_blocked` threshold) are prompt text only — never validated/enforced in code after the LLM responds | science | Medium | Low | `tasks/vulnerability.py:53-70` (prompt) vs `run_vulnerability_task` (no post-hoc check) |
| 10 | `services/featherless.py` and `services/criticality.py` are fully dead (zero call sites), confirmed | dead code | Low | Low (delete) | grep, zero references outside self |
| 11 | `services/r2_reader.py` is dead on the live pipeline path (not documented as such anywhere) | dead code | Low | Low (delete or document) | grep, zero references in `agent.py`/`node.py`/`tasks/*` |
| 12 | Impact reads only `GEMINI_API_KEY` (no `_2..._5` rotation) unlike other agents — a single exhausted free-tier key (~20 req/day per project memory) removes the preferred escalation tier for every normal/high/critical call in this agent | performance/availability | Medium | Low | `services/llm_router.py:67` |
| 13 | DB write failures (`write_impact_data`, both the gate path and main path) are fully swallowed — `impact_result`/`PipelineState` cannot distinguish a persisted vs unpersisted impact row from a downstream consumer's perspective | failure-handling | Medium | Low | `agent.py:92-93,183-184` |
| 14 | Degenerate/empty `bounds` silently becomes bbox `[0,0,1,1]` with no validation, unlike hazard's own bbox-length check noted in `root_cause.md` | correctness | Low-Medium | Low | `agent.py:125-130` |
| 15 | `cost_tracker.reset()`/`get_summary()` never called on the live LangGraph path — call counts accumulate indefinitely across events in a long-lived process and are never surfaced anywhere | dead code / observability gap | Low | Low | grep, `cost_tracker.reset` only in `main.py` |
| 16 | `high_risk_people`/`medium_risk_people` default formula (`pop*0.2`/`pop*0.5`) is independently duplicated in two files (`agent.py` and `services/db.py`) — a maintenance/consistency risk, not yet a live bug | contract | Low | Low | `agent.py:216-217` vs `services/db.py:89-90` |
| 17 | Vulnerability's criticality routing has two `elif` branches both resolving to `"high"` with overlapping-looking conditions and no comment explaining the duplication — possible copy-paste where one was meant to be a distinct tier | correctness (suspected) | Low | Low | `tasks/vulnerability.py:146-148` |

---

## Note on root_cause.md relevance

`root_cause.md` is almost entirely about the **hazard** agent's earthquake
false-positive investigation and the satellite->hazard SAR/NDWI unit
mismatch — it does not analyze the impact agent's own logic at all. Its
only relevance here is confirming the general pattern (upstream
confidence/units silently mis-propagating downstream), which this analysis
independently found repeats one hop further down: impact's confidence is
a bare pass-through of hazard's number (gap list item 2), the same class
of defect root_cause.md diagnoses for satellite->hazard.
