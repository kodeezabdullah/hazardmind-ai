# HazardMind AI — System Analysis (Cross-Agent, 2026-07-27)

Scope: the full pipeline (`satellite -> hazard -> impact -> report`) as it
exists on `main`/`analysis/all-agents` (tip `b1be94f` + this branch's docs
commits). This document is built on top of, and cross-checks, the four
per-agent documents (`agents/{satellite,hazard,impact,report}/ANALYSIS.md`),
each independently verified against source. Where the four documents
disagree with each other or with `root_cause.md`/`CLAUDE.md`, that is called
out explicitly rather than silently resolved.

No code was changed to produce this document.

---

## A. RESPONSIBILITY MAP

| Agent | Owns | Depends on | Depended on by | Split/ownerless responsibility |
|---|---|---|---|---|
| **Satellite** | Boundary resolution, Sentinel-1/2 scene search/mosaic/clip, NDWI/NDVI/SAR index computation, classification, vectorization, R2 artifact upload, self-assessed confidence (`ConfidenceTracker`) | CDSE, geoBoundaries/Nominatim, GDACS/USGS (cross-check only), LLM chain | Hazard (bbox, index/area, confidence), impact (bbox, risk_cities via hazard), report (artifact URLs, index_type label — DB path only, no confidence) | The `index_calibrated`/`index_units` labels exist specifically so downstream can branch correctly on calibration status — enforcing that downstream actually uses them is nobody's job; it silently fails one hop later (hazard's flood fallback) and is silently discarded two hops later (report's `db_context_to_report_context`). |
| **Hazard** | Flood (hybrid LLM/deterministic)/earthquake (deterministic USGS)/landslide (deterministic DEM) risk classification, `hazard_zones` (3 rows), satellite-payload flat->nested contract adapter | Satellite (bbox, index/area, confidence — flood only), GDACS/USGS/OpenTopoData (self-fetched), LLM chain | Impact (risk_level/severity/confidence/bounds/risk_cities/risk_polygons/flood_depth_estimate), report (hazard_zones rows, confidence) | `risk_polygons` is a genuine PostGIS column (`hazard_zones.geometry`, GIST-indexed) with **no writer anywhere in the codebase** — a documented capability that does not exist, owned by no one. |
| **Impact** | Population (GeoNames+LLM)/infrastructure (Overpass+LLM)/vulnerability (LLM) assessment, no-significant-disaster gate, `impact_data` | Hazard (risk_level, confidence, bounds, risk_cities) | Report (impact_data row, `overall_confidence` — IF the column exists live, see Section E) | Impact's own `overall_confidence` output is a **pure pass-through of hazard's number** — none of the three task-level LLM confidences (population/infrastructure/vulnerability each return their own `confidence` field) are read, averaged, or folded in anywhere. This is ownerless: impact computes three real confidence numbers and discards all three. |
| **Report** | Narrative synthesis (7-section intelligence layer), PDF/map rendering, `confidence_level` aggregation, `final_reports` | Satellite/hazard/impact **DB rows only** (never `PipelineState` in production — see Section B) | Frontend, human responders (terminal consumer) | Report is the only agent expected to explain confidence to a human, but two independently-computed confidence figures (`confidence_level` via `min()`-aggregation, `intelligence.criticality.overall_confidence` via a separate LLM/formula) appear in the same PDF with no reconciliation between them — nobody owns making these agree. |

**Flagged responsibility gaps, consolidated:**
1. **`risk_polygons`** — documented (CODEBASE.md per root CLAUDE.md) as a
   PostGIS feature; never implemented; owned by no one.
2. **Calibration-flag enforcement** (`index_calibrated`) — satellite produces
   it honestly; no downstream consumer's fallback path is required to branch
   on it, and one (report's DB-context builder) actively discards it.
3. **Confidence reconciliation across agents** — each agent computes its own
   confidence by its own method (weighted-evidence-minus-penalty for
   satellite, flat-tier-constants for hazard, pure pass-through for impact,
   `min()`-of-everything for report); nothing owns making these methods
   compatible or even comparable in meaning.

---

## B. THE CONFIDENCE CHAIN, END TO END

### B.1 The claim to verify

Commit `fa0d9bd` ("propagate satellite confidence into hazard and report")
is confirmed present on `main` (verified: `git log --oneline main | grep
fa0d9bd` returns it, well before the branch tip). The task is to verify the
code it introduced **actually takes effect on the live runtime call path**,
not merely that it exists in the diff.

### B.2 Satellite — the source

`ConfidenceTracker.overall_confidence()` (confidence_tracker.py) computes a
weighted average of evidence minus flat per-concern penalties, clamped
[0,1]. This number is written to `structured["confidence"]`
(`agent.py:876`) and returned as part of the JSON result. `node.py`
(satellite) writes it into `PipelineState["confidence_scores"]["satellite"]`
(node.py:49-50). **Confirmed: this happens on every successful run,
unconditionally.**

**Critical fact confirmed by direct SQL inspection**
(`agents/satellite/agent.py`'s `_persist_satellite_result` INSERT and
`agents/report/db_client.py`'s `_fetch_satellite_results` SELECT, both
independently read): **`satellite_results` has no confidence column at
all.** The confidence value exists ONLY in the in-memory `PipelineState` for
the duration of one graph run — it is never written to any durable table.

### B.3 Satellite -> Hazard — the first hop, VERIFIED LIVE

`agents/hazard/agent.py:_normalise_satellite_payload` (line ~89) carries
`p.get("confidence")` into the normalized payload's `analysis.confidence`
key. `agents/hazard/analyzer.py:run_parallel_analysis` reads it:

```python
# analyzer.py:342
satellite_confidence = _to_float(analysis.get("confidence")) if analysis.get("confidence") is not None else None
```

and caps flood's confidence:

```python
# analyzer.py:409-414
confidence_cap_applied = False
if satellite_confidence is not None and flood.get("confidence") is not None:
    flood_confidence = _to_float(flood.get("confidence"))
    if satellite_confidence < flood_confidence:
        flood = {**flood, "confidence": satellite_confidence}
        confidence_cap_applied = True
```

**This DOES take effect on the live call path.** The call chain is:
`hazard_node(state)` (node.py:19) reads `state["satellite_result"]` — the
in-memory `PipelineState` value written by satellite's node one hop earlier
in the same graph run, not a DB re-read — and passes it straight to
`analyze_hazard` -> `run_parallel_analysis`. Since `PipelineState` is
in-memory for the single graph invocation, satellite's confidence (computed
moments earlier in the same process) is genuinely present and read. **This
one hop is real and verified**, not merely present in a diff.

**Scope limits, confirmed:**
- Only `flood`'s confidence is capped. Earthquake/landslide are deliberately
  NOT capped — correct, since they never consume satellite output at all
  (`analyze_earthquake(bbox, usgs_data)`, `analyze_landslide(bbox, gdacs_data,
  slope_data)` — confirmed, no satellite-derived parameter beyond `bbox` for
  query scoping, matching `root_cause.md`'s finding exactly, independently
  re-verified by direct code read in this session).
- `write_to_db` (agent.py:102-174) writes `overall_confidence` per
  `hazard_zones` row from `confidence_scores.get(hazard_type, 0.0)` — so the
  **flood row specifically** carries the satellite-capped value into the
  durable `hazard_zones` table. This is the only durable trace of satellite's
  confidence anywhere in the system.

### B.4 Hazard -> `PipelineState` — the first dilution

`node.py:hazard_node` (hazard) computes `confidence_scores["hazard"]` as a
**flat unweighted average** of flood/earthquake/landslide confidences
(`sum(hazard_confidences.values()) / len(hazard_confidences)`, node.py:45).
This is a NEW finding (not in `root_cause.md` or root `CLAUDE.md`): the
satellite-capped flood confidence, once averaged with earthquake/landslide's
independently-scaled, uncapped constants (0.7-0.85 typical), is diluted
before it ever reaches `PipelineState["confidence_scores"]["hazard"]`. A
satellite confidence of 0.0 capping flood to 0.0, averaged with earthquake
0.85 and landslide 0.8, yields `PipelineState`'s hazard confidence as
`(0.0+0.85+0.8)/3 = 0.55` — not 0.0. **The in-memory hand-off signal is
already diluted one hop after the cap that fixed it.**

### B.5 Hazard -> Impact — a second dilution

`agents/impact/node.py:impact_node` (line 39-46) reads
`hazard_result["hazard"]["confidence_scores"]` and **averages it again**
(`sum(conf.values()) / len(conf)`) — the same flat average computed
independently a second time from the same three numbers (since
`hazard_result["hazard"]["confidence_scores"]` still carries the
per-hazard-type breakdown, not the already-averaged `PipelineState` value).
This produces the same `0.55`-style number as B.4 in this example, then
passes it into `run_impact_analysis` as `overall_confidence`.

**Confirmed, independently verified by the impact agent's own
`ANALYSIS.md`:** impact's own `overall_confidence` output is a **pure
pass-through** of this value — `agent.py:228` writes it verbatim into
`json_data["data"]["overall_confidence"]`. None of population/
infrastructure/vulnerability's own LLM-reported confidences are read,
averaged, or folded in anywhere (confirmed: no `pop.get("confidence")`/
`infra.get("confidence")`/`vuln.get("confidence")` reference exists in
`agent.py`). So impact's confidence is hazard's diluted average, unchanged,
with zero contribution from impact's own three real confidence
computations.

### B.6 Impact -> Report — the critical, possibly-broken hop

`agents/impact/services/db.py`'s `impact_data` DDL has **no
`overall_confidence` column** (confirmed independently by both this
document's own read and the impact agent's `ANALYSIS.md`, which quotes the
DDL verbatim: `id, event_id, total_affected, high_risk_people,
medium_risk_people, hospitals_at_risk, schools_at_risk, roads_blocked,
bridges_at_risk, vulnerability_score, evacuation_routes,
estimated_evacuation_time, created_at, updated_at` — no confidence field).

`agents/report/db_client.py`'s `_fetch_impact_data` (line ~227-252)
explicitly `SELECT`s `overall_confidence` from `impact_data`. **If the live
Neon table matches this repo's DDL exactly (which is the only version of
truth available from static analysis), this SELECT raises
`asyncpg.exceptions.UndefinedColumnError`.**

`fetch_report_context_from_db`'s `except Exception` (db_client.py:50-53)
specifically detects this via `_schema_mismatch` (checks for
`UndefinedColumnError`/`UndefinedTableError`/"column ... does not exist" in
the message) and **re-raises as a loud `RuntimeError`**, not a silent
default. This propagates to `run_report_pipeline`'s outer catch
(`pipeline.py:211`), which returns `status: "failed"`.

**Consequence, if the live schema matches the repo DDL: every single report
generation currently fails outright at this exact line, not degrades.**
This is the single highest-severity, highest-uncertainty finding in this
entire system audit — it cannot be resolved by static code analysis alone
(it depends on whether an out-of-repo manual `ALTER TABLE` was applied to
the live Neon database that no migration file in this repo records). Both
the impact and report `ANALYSIS.md` documents flag this independently and
identically, arriving at the same conclusion from opposite ends of the
contract — strong corroboration that this is a real, not speculative,
finding.

### B.7 Report's own aggregation — `_collect_confidence_values`

```python
# agents/report/db_client.py:490-511 (current code, post-fa0d9bd)
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

**`fa0d9bd` added the first two lines** — reading `satellite_confidence` and
`satellite.confidence` directly. **Neither is ever populated on the
production call path**, verified two independent ways:

1. `report_node.py` calls `run_report_pipeline(event_id, fetch_from_db=True,
   ...)` with **no `incoming_payload`** — `PipelineState`'s
   `confidence_scores` is never passed into the report pipeline at all. The
   `incoming_payload` parameter (which would populate
   `confidence.satellite_confidence` via
   `_merge_incoming_payload_into_context`, pipeline.py:305-334) exists in
   the function signature but the graph node never supplies it. Confirmed
   independently by the report agent's own `ANALYSIS.md` (Section 2): "the
   report stage is 100% DB-sourced in the current wiring."
2. `db_context_to_report_context` (db_client.py:291-373, the function that
   DOES run on the live path) builds the `satellite` context block from
   `satellite_results` DB columns only: `{type, reason: "loaded_from_latest_
   neon_schema", cloud_cover, scene_id}` — **no `confidence` key at all**,
   because (per B.2) `satellite_results` has no such column to read from.

**So the two `fa0d9bd`-added read keys are dead code on the production call
path.** They would only ever populate if some future caller supplies
`incoming_payload` with those exact keys — a codepath that exists (unit
tests exercise it, per the report agent's own `test_confidence_aggregation.py`)
but is never invoked by the graph.

**The `hazard_scores["flood"]` read (line ~508, pre-existing before
`fa0d9bd`, unchanged by it) IS the real channel.** `report.get("hazard",
{}).get("confidence_scores", {})` — this is populated from the merged
`hazard_zones` rows via `db_context_to_report_context`, which does read the
real `overall_confidence` per row (including the satellite-capped flood
row). So satellite's confidence DOES reach `_collect_confidence_values`,
but through the pre-existing `hazard_scores["flood"]` path, not through
either of the two new keys `fa0d9bd` added specifically for this purpose.

### B.8 The final arithmetic

```python
# db_client.py:403-418
combined = min(values)          # min-dominant, not averaged (also a fa0d9bd change)
HIGH if combined >= 0.8
MEDIUM if combined >= 0.6
else LOW
```

**If satellite reports 0.0 confidence today, what does the final report say?**

Trace with real numbers, flood/S1 disaster (the exact scenario the
2026-07-26 live incident hit):

1. Satellite: confidence 0.0 (empty evidence set).
2. Hazard's flood confidence: capped to 0.0 (`fa0d9bd`, verified real, B.3).
3. Hazard's `hazard_zones` flood row: `overall_confidence = 0.0` (written
   correctly, B.3).
4. `PipelineState["confidence_scores"]["hazard"]`: diluted to the 3-way
   average with earthquake/landslide (e.g. `~0.55`, not 0.0 — B.4). **This
   figure is never read by report anyway** (B.6/B.7 — report doesn't use
   `PipelineState`).
5. `impact`'s `overall_confidence`: same diluted `~0.55` (pass-through of
   the diluted hazard average, B.5). Persisted to `impact_data` IF the
   column exists (B.6's open question).
6. Report's `_collect_confidence_values` collects, among others:
   `hazard_overall_confidence` (an average across ALL THREE `hazard_zones`
   rows — flood 0.0, earthquake ~0.8, landslide ~0.8 — via
   `_average_confidence`, yielding `~0.53`, NOT 0.0),
   `impact_overall_confidence` (`~0.55` per step 5, IF readable),
   `hazard_scores["flood"]` (`0.0` — this IS the real, undiluted signal),
   `hazard_scores["earthquake"]`/`["landslide"]` (`~0.8` each, uncapped).
7. `min(values)` over this full list: **`min(0.53, 0.55, 0.0, 0.8, 0.8, ...)
   = 0.0`** — because `hazard_scores["flood"]` (raw per-hazard-type, not the
   averaged `hazard_overall_confidence`) is in the list and it IS 0.0.
8. `combined = 0.0` -> `confidence_level = "LOW"`.

**Answer: today, for a flood disaster where satellite reports 0.0
confidence, the final report DOES correctly say LOW — because the
per-hazard-type `hazard_scores["flood"]` figure (unaveraged, correctly
capped) is one of the values fed into `min()`, and the raw `0.0` survives
that min regardless of how many other diluted/averaged figures are also in
the list.** The `min()`-of-everything design is what saves this — even
though the aggregate `hazard_overall_confidence`/`impact_overall_confidence`
figures are themselves diluted averages (B.4/B.5), the raw flood-specific
number rides in via a second, independent path
(`hazard_scores["flood"]`) and `min()` picks it up regardless.

**This is confirmed by a live regression test**
(`agents/report/test_confidence_aggregation.py`,
`test_satellite_zero_confidence_cannot_yield_high`), independently
identified by the report agent's `ANALYSIS.md`, asserting this exact
scenario yields LOW, with a companion test
(`test_old_behavior_would_have_masked_this`) proving the pre-`fa0d9bd`
averaging code would have returned HIGH on the same inputs.

**But this only holds for FLOOD.** If satellite reports 0.0 confidence on an
EARTHQUAKE or LANDSLIDE disaster (structurally impossible today per B.3/
`root_cause.md`, since those hazards never read satellite output at all —
but worth stating precisely): satellite's confidence would have no path
into `hazard_scores` at all for that hazard type, and `min()` would never
see it. This is not a live bug (earthquake/landslide correctly don't depend
on satellite), but it does mean the fix is narrower than "satellite
confidence now flows through" might suggest — it flows through **only
through the flood-specific channel, only because hazard capped it there
first**, not through either of the two new report-side read keys that were
purpose-built for this fix.

### B.9 Summary — is the fix real?

**Yes, on the flood path, via a channel `fa0d9bd` did not primarily intend
(`hazard_scores["flood"]`, pre-existing) rather than the channel it added
(`confidence.satellite_confidence`/`satellite.confidence`, both dead on the
live path).** The commit's stated goal — "satellite 0.0 cannot yield report
HIGH" — is achieved, verified live via regression test, and the code path
that achieves it was independently traced and confirmed in this session by
reading `analyzer.py`, `node.py` (hazard and impact), and `db_client.py`
directly. But two of the three code changes `fa0d9bd` introduced
specifically for this purpose (the two new `_collect_confidence_values` read
keys) are unreachable on the production call path — the fix works, but not
for the reason its own commit message implies.

---

## C. UNIT AND LABEL CONSISTENCY

| Value | Producer unit | Consumer(s) assumed unit | Consistent? |
|---|---|---|---|
| `mean_index`/`mean_value` | NDWI/NDVI: ratio [-1,1]; SAR: uncalibrated dB (unbounded) — `index_type`/`index_calibrated`/`index_units` ride alongside as explicit contract fields | Hazard's LLM-facing flood prompt: reads `satellite_type`, branches label correctly. Hazard's **deterministic fallback**: assumes NDWI-ratio scale unconditionally (`flood_index > 0.5`/`0.3`) — **inconsistent, confirmed unfixed in both this session and the satellite agent's own ANALYSIS.md**. | **NO** on the hazard-fallback path. |
| `index_type` | satellite's real computed value (`NDWI`/`NDVI`/`SAR`) | Report's `db_context_to_report_context`: **hardcodes `"database_result"`**, discarding the real label entirely — every report-stage LLM prompt sees a placeholder string, not the real index type, on the DB-fetch path. | **NO** — this is a new finding this session, not previously documented anywhere in this repo. |
| `mean_value` (again, at the report layer) | Same as above | Same function: **hardcodes `0`** regardless of the real value. | **NO** — same new finding. |
| `affected_area_km2` | satellite: km² via EPSG:6933 equal-area reprojection (correct method; the previous silent degrees²-as-km² fallback is now closed per satellite's ANALYSIS.md) | Hazard, impact, report: all assume km². Consistent since satellite's fix landed. | **YES**, post-fix. |
| `roads_blocked_km` -> `roads_blocked` | impact's task layer: float, kilometres, explicit `_km` suffix | `services/db.py`'s DDL column: `roads_blocked INTEGER`, no unit suffix — genuinely ambiguous to a schema-only reader (looks like a count). Separately: the DB rounds to nearest integer km (`int(round(...))`) while the in-memory payload rounds to 1 decimal (`round(..., 1)`) — **the same run can show two different values for the same quantity depending on which surface you read.** | **NO**, confirmed by impact's own ANALYSIS.md — a genuine unit-label mismatch plus an independent rounding mismatch. |
| `confidence` (all agents) | Satellite: heuristic weighted-evidence-minus-penalty, [0,1], meaning "how much cross-validated evidence supports this specific run." Hazard: flat per-risk-tier constants, [0,1], meaning "how typical is this risk tier" — NOT a measure of evidence quality except for flood (now capped). Impact: pure pass-through of hazard's diluted average — carries no independent meaning at all. Report: `min()` over a heterogeneous mix of all of the above. | Every consumer implicitly treats "confidence" as a single comparable [0,1] scale. **It is not** — the four agents compute four methodologically incompatible numbers under the same field name, and nothing in any contract distinguishes "self-reported LLM confidence," "hand-set tier constant," "pass-through of an upstream average," and "min of a heterogeneous list." | **NO** — this is the single most systemic label/content issue in the whole pipeline: the field name `confidence` is stable, its meaning is not. |
| `evacuation_routes` | impact task layer computes TWO genuinely different things (`priority_zones`: named places; `evacuation_routes`: named roads with distance/geojson) | impact's payload/DB field named `evacuation_routes` is actually fed `priority_zones` data (confirmed by impact's own ANALYSIS.md, `agent.py:223`); the LLM's real `evacuation_routes` output is computed then silently discarded, never persisted or forwarded. | **NO** — confirmed real content/label mismatch, place-name data under a route-data field name. |
| `overall_severity` | Hazard: `"HIGH"` in the invalid-bbox fallback path means "we could not determine anything" | Everything downstream (impact's gate, report's response-level classification) treats `"HIGH"` as "this is a real high-severity disaster." | **NO** — the string value is identical whether the severity is real or a plumbing-failure artifact; there is no separate field distinguishing the two. |

**Pattern:** every genuine unit/label inconsistency found traces back to the
same root defect class satellite's own audit named first (SAR-as-NDWI): **a
downstream consumer infers a unit/meaning from context rather than reading
it from an explicit, enforced contract field**, and at least one consumer at
every hop in this pipeline has at least one instance of this defect.

---

## D. WHAT EACH AGENT ACTUALLY USES FROM UPSTREAM

### D.1 `root_cause.md`'s claim — verified, still true

"Earthquake/landslide hazard analysis never reads satellite's analytical
output, only bbox." **Confirmed true, independently re-verified this
session** by direct read of `analyzer.py`:
`analyze_earthquake(bbox, usgs_data)` and `analyze_landslide(bbox,
gdacs_data, slope_data)` — neither function signature nor body touches
`mean_value`/`affected_area_km2`/`water_percent`/any satellite-derived
analytical field. `bbox` is used only to scope the USGS/GDACS/DEM queries.
This claim holds exactly as stated, unchanged since `root_cause.md` was
written.

### D.2 Full cross-agent field-usage table

| Field crossing the boundary | Genuinely consumed, or pass-through/decorative? |
|---|---|
| Satellite `bbox` -> hazard | **Genuinely consumed** — scopes every third-party fetch (GDACS/USGS/DEM); gates the whole analysis (invalid bbox short-circuits everything). |
| Satellite `mean_value`(index)/`affected_area_km2`/`satellite_type` -> hazard | **Genuinely consumed, flood only.** Zero consumption for earthquake/landslide (D.1). |
| Satellite `index_calibrated`/`index_units` -> hazard | **Carried through the adapter (`_normalise_satellite_payload`), never read by `run_parallel_analysis`.** Decorative on this hop — present in the normalized payload, unused. |
| Satellite `confidence` -> hazard | **Genuinely consumed**, flood-only cap (B.3). Real, verified. |
| Satellite `risk_cities` -> hazard -> impact | **Genuinely consumed** — used by impact's population/infrastructure tasks as the primary city-lookup key (`_primary_city`, `_city_label`). |
| Hazard `flood_risk`/`overall_severity`/`risk_level` -> impact | **Genuinely consumed, but with a 3-way fallback chain that prefers `flood_risk` first** (`impact/node.py:32-37`) — meaning a non-flood event's `risk_level` is derived from `flood_risk`, which correctly returns LOW/UNKNOWN when there's no flood, potentially masking a real earthquake/landslide `overall_severity` of HIGH/CRITICAL. **Flagged as a real risk by both this document and hazard's own ANALYSIS.md**, not confirmed as exercised in a live run in this session (no live trace available), but the code path is real and the failure mode is structurally possible on any real non-flood dispatch. |
| Hazard `confidence_scores` -> impact | **Consumed but re-averaged, then discarded downstream** — impact computes its own average of hazard's three numbers, then passes that average through unchanged as its own `overall_confidence`, never blending in its own tasks' confidences (B.5). |
| Hazard `earthquake_risk`/`landslide_risk` -> impact | **NOT consumed at all — hardcoded to `"LOW"`** regardless of hazard's real computed value (`agent.py:137-138`, confirmed by impact's own ANALYSIS.md). This means impact's population/infrastructure/vulnerability prompts always describe the disaster as a flood (`_build_prompt` in `population.py`/`infrastructure.py` literally interpolates `"{severity} flood"` into the prompt text unconditionally), **for every disaster type**, including earthquakes and landslides. This is a severe, previously undocumented finding — see Section F. |
| Hazard `risk_polygons` -> impact | **Always `{}`** (D.4, hazard never populates it) — impact receives and passes through an empty dict every time, structurally decorative. |
| Hazard `flood_depth_estimate` -> impact | Hazard never sets this field at all (confirmed: no producer in `analyzer.py`'s return dict); impact defaults it to `0.0`. Fully decorative/absent field. |
| Impact `overall_confidence` -> report | **Consumed IF the DB column exists** (B.6, unresolved). If it doesn't, the whole report stage fails, not degrades. |
| Impact `total_affected`/`hospitals_at_risk`/etc. -> report | **Genuinely consumed** — read verbatim into report's `impact` context block, feeds narrative generation and the NDMA response-level thresholds. |
| Satellite artifact URLs (`geojson_url` etc.) -> report | **Genuinely consumed** — feeds `map_generator.py`'s rendering and the PDF's linked artifacts. |
| Satellite `index_type`/`mean_value` -> report | **Discarded, replaced with placeholders** (`"database_result"`/`0`) on the live DB-fetch path — see Section C, a new finding this session. |

**Pattern, extending `root_cause.md`'s exercise system-wide:** genuine
cross-agent coupling exists for bbox/risk_cities everywhere, and for
index/area/confidence specifically on the flood path. Every OTHER
disaster-type-specific field (earthquake_risk, landslide_risk,
flood_depth_estimate, risk_polygons) is either hardcoded, always-empty, or
simply never produced — meaning **impact and report are effectively
flood-only in practice**, regardless of the actual `disaster_type` the
pipeline was dispatched to assess, a system-wide consequence of hazard
never surfacing a disaster-type-aware "primary risk" field for downstream
agents to consume correctly.

---

## E. FAILURE PROPAGATION

### E.1 Can a failed/degraded upstream stage still produce a confident-looking final PDF?

**Concrete, traceable example: the invalid-bbox path.**

1. Satellite fails to hand off a valid bbox (any plumbing miss — e.g. a
   region-boundary resolution edge case, or a truncated/malformed
   `PipelineState` field).
2. Hazard's `run_parallel_analysis` hits `not bbox or len(bbox) < 4` ->
   returns all three risks `UNKNOWN`, confidence `0.0/0.0/0.0`, but
   `overall_severity` **hardcoded to `"HIGH"`** (analyzer.py:344-358,
   confirmed unchanged from `root_cause.md`'s original finding, re-verified
   this session).
3. This payload **passes `quality_check`** — `"HIGH"` is a valid enum value
   per the check's `overall_severity in {"CRITICAL","HIGH","MEDIUM","LOW"}`
   test; nothing in `quality_check` rejects a HIGH-severity-with-all-UNKNOWN
   combination as internally inconsistent. Gets written to `hazard_zones` as
   a real row.
4. Impact's `_no_significant_disaster` gate checks `risk_level` — which per
   D.2's fallback chain would be `flood_risk == "UNKNOWN"` here, which IS
   in the gate's no-significant-disaster set (`{"LOW","NONE","UNKNOWN","",
   "MINIMAL","NEGLIGIBLE"}`) — so impact correctly reports zero impact via
   `_emit_no_impact`, honestly. **This particular failure mode is
   self-correcting one hop later**, because impact's gate keys off
   `flood_risk` (UNKNOWN), not `overall_severity` (HIGH) — an accidental
   save, not a designed one, since D.2 also documents this same
   `flood_risk`-preference as a *separate* risk for genuine non-flood
   events.
5. **But if `overall_severity` (not `flood_risk`) is what a different
   consumer reads** — e.g. report's `determine_recommended_response_level`
   (`severity=="CRITICAL"` OR ... — reads `overall_severity`, not
   `flood_risk`) — **the hardcoded HIGH from a pure plumbing failure would
   flow straight into an NDMA Level-2 response-level recommendation**, with
   zero real disaster behind it, UNLESS impact's honest zero-impact gate
   (step 4) already produced `total_affected=0`/`hospitals_at_risk=0`,
   which would keep `determine_recommended_response_level`'s
   `hospitals_at_risk>=3`/`total_affected>=100_000` legs from firing — the
   response-level classification uses BOTH `overall_severity` and impact
   numbers, so a zero-impact override from step 4 does partially guard
   against this, but the CRITICAL/HIGH branch is an OR condition
   (`severity=="CRITICAL" OR ...`), meaning `overall_severity` alone
   (independent of the honestly-zeroed impact numbers) can still trigger
   `NDMA Level-2` purely from the plumbing-corrupted severity string.

**Verdict: yes, a failed upstream stage (satellite handing off an invalid
bbox) CAN produce a confident-looking response-level recommendation in the
final report, via a route (hardcoded `overall_severity: "HIGH"` surviving
`quality_check` unflagged) that this document traces completely, even
though the specific *population/hospital numbers* would correctly read
zero from the impact gate in the same scenario.** This is a genuine,
demonstrated (via code trace, not live incident) degradation-smoothed-over
case: two different downstream signals (severity string vs. impact
numbers) disagree with each other after a single upstream plumbing failure,
and nothing in the pipeline reconciles that disagreement before it reaches
the PDF.

### E.2 The impact_data-column question (B.6) as a failure-propagation case

If the column truly doesn't exist live: this isn't a "degrade smoothed
over" case, it's the opposite — a **hard stop** at the report stage for
every event. This is arguably the healthiest failure mode in the whole
system (loud, unambiguous, `status: "failed"`) IF it's actually firing —
but if it IS firing, the entire pipeline would appear completely broken in
production, which contradicts CLAUDE.md's "Pipeline running live... end to
end" framing. This tension (a documented schema gap vs. a documented "it
runs live") is itself worth flagging: either the column exists live and the
repo's DDL is stale, or every live report generation is currently failing
and nobody has noticed/documented it as a production incident. **Cannot be
resolved without live DB access — flagged as the highest-priority thing to
verify before trusting anything else in this system.**

---

## F. THE HONESTY AUDIT

| # | What it says | What the evidence actually supports | How a reader is misled |
|---|---|---|---|
| 1 | Hazard's `overall_severity: "HIGH"` on an invalid-bbox plumbing failure | Zero real disaster signal — all three risks UNKNOWN, the payload's own `error` field says "Invalid bbox received from satellite agent" | A responder reading `hazard_zones`/a downstream severity field sees "HIGH" with no visible distinction from a genuine HIGH verdict — the honest `error` text is not surfaced past the hazard agent's own return dict. **Verbatim quote:** `analyzer.py:350`, `"overall_severity": "HIGH"` inside the same dict as `"error": "Invalid bbox received from satellite agent"` — the code itself holds both an honest error string and a dishonest severity label simultaneously. |
| 2 | Impact's population/infrastructure prompts state `"Disaster event: {severity} flood"` / `"Disaster: {severity} flood in {city}"` unconditionally | The actual `disaster_type` may be earthquake or landslide — hazard's `earthquake_risk`/`landslide_risk` are hardcoded `"LOW"` in the data impact receives (agent.py:137-138), and the prompt templates never branch on disaster type at all | **Every non-flood disaster's population/infrastructure LLM reasoning is conducted under a false premise stated directly in the prompt text.** Quote, `agents/impact/tasks/population.py:86`: `f"Disaster event: {severity} flood"`. Quote, `agents/impact/tasks/infrastructure.py:133`: `f"Disaster: {severity} flood in {city}"`. This is the single most severe honesty-audit finding in the whole system — an LLM is told it's assessing a flood when it may be assessing an earthquake, and the resulting population/infrastructure-at-risk numbers are generated, persisted, and reported as if they were reasoned from the correct disaster type. (Vulnerability's prompt, by contrast, correctly uses `flood`/`eq`/`ls` separately — this defect is specific to the population and infrastructure tasks.) |
| 3 | Report's DB-fetch context states `"index_type": "database_result"` in the analysis block fed to every report-stage LLM prompt | The real index type (`NDWI`/`NDVI`/`SAR`) and calibration status were computed by satellite and are available in `satellite_results`... except `satellite_results` doesn't store `index_type` either in a way `db_context_to_report_context` reads it — the function hardcodes the placeholder string regardless | A report reader/LLM sees a meaningless label where a real, disaster-science-relevant fact belongs. Quote, `agents/report/db_client.py`'s `db_context_to_report_context`: `"analysis": {"index_type": "database_result", "mean_value": 0, ...}` — both values are placeholders, not derived from the real satellite result, on every DB-fetched report. |
| 4 | Every prompt/deterministic-check surveyed states `index_type` where it states anything at all about the satellite index, but none state `index_calibrated` | SAR data is explicitly uncalibrated (`index_calibrated: False`) with no radiometric LUT, no speckle filter, no terrain correction — a fact the satellite agent computes and labels honestly | A reader of any generated narrative (satellite's own `interpret_results`, report's `_compact_context`, report's `deterministic_detailed_report` template) sees an index type stated as if it were a normal, comparable measurement, with no caveat that a SAR-path `mean_value` is not physically interpretable at all. This was originally documented in root `CLAUDE.md` and is **confirmed still true, independently re-verified this session** by both satellite's and report's `ANALYSIS.md` documents. |
| 5 | `PipelineState["confidence_scores"]["hazard"]` presents a single number to any consumer reading it | That number is a flat average of three methodologically-incompatible confidences (a satellite-capped flood figure, and two flat hand-set constants for earthquake/landslide) — it is not a measure of "how confident is the hazard verdict," it's an arithmetic artifact of averaging three different kinds of number together | Any future consumer of `PipelineState` (not report, which doesn't read it, but any other future integration) would reasonably interpret a hazard confidence of e.g. 0.55 as "moderate confidence in the hazard analysis" when the real signal is "satellite reported total uncertainty on the one hazard type it actually informs, diluted by two constants." |
| 6 | `report.confidence_level` (`HIGH`/`MEDIUM`/`LOW`) is presented as the report's overall trustworthiness figure | It is `min()` over a list containing both raw per-hazard-type values (the honest signal) and pre-averaged aggregate values (diluted signals) mixed together — the arithmetic happens to work out correctly for the flood-confidence-0.0 case (B.8) because the raw value is *also* in the list, but this is closer to a lucky redundancy than a designed invariant: if `hazard_scores["flood"]` were ever removed from the read list (e.g. a future refactor that only reads the pre-averaged `hazard_overall_confidence`), the same 0.0-satellite-confidence scenario would silently start reporting the diluted `~0.53` instead, which still rounds to LOW today but would not if the other two hazards' confidences were higher. | A maintainer reading `_collect_confidence_values` and seeing `hazard_overall_confidence` in the list might reasonably assume that's the "real" hazard confidence signal and remove the seemingly-redundant `hazard_scores["flood"]` read as a cleanup — which would silently break the exact guarantee the regression test (`test_satellite_zero_confidence_cannot_yield_high`) is protecting, without the test itself catching it (the test presumably still passes today because BOTH values happen to currently support the same conclusion in the test's specific numbers). |
| 7 | Two independently-computed confidence figures (`confidence_level`, `intelligence.criticality.overall_confidence`) both appear in the same PDF | They are computed by entirely different methods (`min()`-aggregation vs. an LLM/formula-based criticality assessment) with no cross-check | A reader could see "Overall Confidence: 78%" in one PDF section and "confidence_level: LOW" elsewhere in the same document, with no explanation of why they differ — independently identified by the report agent's own `ANALYSIS.md`, not confirmed against a real generated PDF in this session but the code paths are fully independent with nothing forcing agreement. |

**The single most important finding in this table:** #2 (the flood-labeled
prompts for non-flood disasters). It is more severe than the confidence
chain's dilution (#5/#6) because it does not merely misstate a meta-level
trust figure — it feeds a **false factual premise** directly into the LLM
reasoning that produces population and infrastructure numbers a human
responder will read and act on, for every earthquake or landslide event
this pipeline has ever processed or will process until fixed.

---

## G. THE SCIENCE GAP TABLE (consolidated)

| Computation | Agent | Literature method | What the code does | Defensible? |
|---|---|---|---|---|
| SAR flood index | Satellite | Calibrated σ⁰ backscatter (radiometric LUT + speckle filter + terrain correction) | `10*log10(raw GRD DN)` — no calibration, no filter, no correction | **No** — correctly labeled uncalibrated, but still classified/thresholded and shipped as if meaningful |
| NDWI/NDVI thresholds | Satellite | Should be validated against the actual input product level | Tuned against L1C (top-of-atmosphere), never retuned after the L1C->L2A (surface reflectance) switch | **No**, acknowledged and unfixed |
| Earthquake/landslide damage detection | Satellite | Bi-temporal (before/after) NDVI differencing | Single post-event scene, absolute threshold — conflates disaster damage with naturally bare terrain | **No**, materially weaker than standard practice, undisclosed to the operator |
| Flood/earthquake/landslide risk cut points | Hazard | Should cite a seismic-hazard or geotechnical-slope standard | Round numbers (7.0/5.5/4.0 magnitude; 45/30/15 degree slope; 0.5/0.3 flood index) chosen by feel | **Partially** — the ordering/direction is defensible engineering judgment; the specific numbers are uncited |
| Confidence scores (all four agents) | All | Should be calibrated against historical accuracy data | Every agent's confidence is a heuristic: hand-picked weights (satellite), flat tier constants (hazard), pure pass-through (impact), or `min()` of heterogeneous heuristics (report) | **No, anywhere in the pipeline** — zero calibration exists at any stage |
| Population-affected | Impact | Population-raster x hazard-extent geospatial intersection (e.g. WorldPop x flood polygon) | Single city-level GeoNames administrative figure, LLM-asserted 2x-5x urbanization multiplier (prompt text, not computed), LLM-asserted fixed 20%/50% risk-fraction splits | **No** — no gridded population product used anywhere in the codebase, no flood-extent polygon ever reaches the population prompt at all |
| Infrastructure-at-risk subsetting | Impact | Should constrain "at risk" facilities to the actual hazard-extent geometry | Real OSM counts fetched, but the "at risk" fraction is an unconstrained LLM guess with no code-side clamp against the real count | **Partially** — the base counts are real, the risk-subsetting is not defensible |
| Vulnerability scoring | Impact | Should be a documented, code-enforced rubric | Explicit scoring "rules" exist only as prompt text; never validated against the LLM's returned score in code | **No** — the rules exist nowhere except as an instruction the LLM might ignore |

**What it would take to fix, system-wide:** (1) real SAR calibration
(satellite, large effort); (2) an L2A-specific threshold revalidation pass
(satellite, medium effort); (3) bi-temporal change detection for
earthquake/landslide (satellite, large effort); (4) a literature-grounding
pass for hazard's risk cut points (hazard, medium effort); (5) an actual
population-raster x hazard-polygon intersection for impact's population
estimate (impact, large effort — this is the single most consequential gap
for a life-safety system, since population-affected drives every NDMA
response-level decision downstream); (6) code-side enforcement of
vulnerability's own stated scoring rules (impact, low effort); (7) a
genuine calibration study for confidence across all four agents (all, very
large effort — no accuracy ground-truth data currently exists in this repo
to calibrate against).

---

## H. CONSOLIDATED GAP LIST (system-wide re-ranking)

Re-ranked by how badly each issue can mislead an operator once its full
downstream propagation is accounted for — not by ease of fix, and not by
severity as scored within a single agent's own document (several
per-agent "Medium" items re-rank to system-level "Critical" once traced
through to the final report).

| # | Issue | Origin agent(s) | Type | System-level severity | Effort | Evidence |
|---|---|---|---|---|---|---|
| 1 | **Impact's population/infrastructure prompts hardcode "flood" regardless of actual disaster_type.** Every earthquake/landslide event's population and infrastructure numbers are reasoned from a false premise stated directly in the LLM prompt. | Impact (root cause: hazard hardcoding `earthquake_risk`/`landslide_risk` to `"LOW"` so impact never even receives the real disaster type in a form its prompts branch on) | correctness/science | **Critical** — misleads on the exact numbers (population/hospitals at risk) a life-safety responder acts on, for the majority of non-flood disaster types this system claims to support | Low-Medium (branch the prompt text on `disaster_type`; also stop hazard hardcoding earthquake/landslide risk to LOW when handing off to impact) | `agents/impact/tasks/population.py:86`, `agents/impact/tasks/infrastructure.py:133`; `agents/impact/agent.py:137-138` |
| 2 | **`impact_data.overall_confidence` may not exist on live Neon**, and if it doesn't, every report generation fails outright (not degrades) — a system-level unknown that determines whether the "live pipeline" framing in root CLAUDE.md is even currently true. | Impact/Report contract mismatch | contract | **Critical, and unresolved by static analysis** | Low to verify, Low to fix once verified | `agents/impact/services/db.py` DDL vs `agents/report/db_client.py:_fetch_impact_data` SELECT — independently confirmed by both agents' own ANALYSIS.md |
| 3 | **Hazard's invalid-bbox fallback hardcodes `overall_severity: "HIGH"` on a non-event**, passes `quality_check` unflagged, and can independently drive an NDMA response-level escalation even when impact's own gate correctly zeroes out population/infrastructure numbers for the same event (E.1). | Hazard | correctness | **High** — a pure plumbing failure can still produce part of a confident-looking escalation | Low | `agents/hazard/analyzer.py:344-358` |
| 4 | **Hazard's deterministic flood fallback applies NDWI-scale thresholds to SAR-dB `mean_value`** without checking `satellite_type`, unlike the LLM-facing prompt in the same file — can misclassify an S1 flood as CRITICAL or LOW purely from unit confusion. | Satellite (uncalibrated input) + Hazard (unit-blind fallback) | contract/science | **High**, conditional on the LLM-failure path firing | Low | `agents/hazard/analyzer.py:180-185` vs `:211-227` |
| 5 | **Report's `db_context_to_report_context` discards the real `index_type`/`mean_value`**, replacing them with placeholder values (`"database_result"`/`0`) for every DB-fetched report — the report-generation LLM never sees the real index label at all on the live path. New finding, previously undocumented. | Report | contract | **High** — undermines every downstream narrative claim about what the satellite actually observed | Medium | `agents/report/db_client.py` `db_context_to_report_context` |
| 6 | **Confidence is diluted twice on the way from hazard to report** (hazard's own flat 3-way average into `PipelineState`, then impact's re-average of the same numbers, then a pure pass-through) even though the raw per-hazard-type signal ultimately survives into report's `min()` via a separate, pre-existing path — the fix "works" but through redundancy, not design, making it fragile to future refactors (F.6). | Hazard + Impact + Report | contract | **High** — currently masked by a lucky redundancy, not a guaranteed invariant | Medium | `agents/hazard/node.py:42-46`; `agents/impact/node.py:39-46`; `agents/report/db_client.py:490-511` |
| 7 | **No calibration LUT/speckle filter/terrain correction for SAR** — every S1 flood run ships a classification/area/mean-index not scientifically defensible, though honestly labeled uncalibrated. | Satellite | science | **High** — but honestly disclosed, unlike most other items in this table | High | `agents/satellite/processor.py` SAR index construction |
| 8 | **Population-affected has no real geospatial exposure model** — an LLM point estimate anchored to one city-level administrative figure with a prompt-asserted (not computed) urbanization multiplier and fixed risk-fraction splits. | Impact | science | **High** — the single most consequential science gap for a life-safety pipeline, since this number drives NDMA response-level thresholds | High | `agents/impact/tasks/population.py` |
| 9 | **`risk_polygons` is always `{}`** — a documented PostGIS capability that does not exist, with no writer anywhere in the codebase. | Hazard | contract | Medium | High | `agents/hazard/analyzer.py:453` |
| 10 | **`impact_node`'s risk_level derivation prefers `flood_risk` over `overall_severity`** — can understate impact for a genuine non-flood disaster (structurally overlapping with #1's root cause but a distinct, separately-fireable defect). | Hazard/Impact boundary | contract | Medium-High | Medium | `agents/impact/node.py:32-37` |
| 11 | **No SAR-calibration caveat anywhere in any prompt or PDF text**, systemic across satellite/hazard/report — every consumer states `index_type` where it states anything, none state `index_calibrated`. | Satellite/Hazard/Report | science/contract | Medium — real but partially mitigated by the underlying cross-validator correctly skipping SAR-as-NDWI evidence | Low-Medium | multiple prompt builders, listed in F #4 |
| 12 | **`evacuation_routes` field is fed `priority_zones` data**, and the LLM's real route-level output is discarded. | Impact | contract | Medium | Low | `agents/impact/agent.py:223` |
| 13 | **Two independent confidence figures in the same PDF** with no reconciliation. | Report | correctness | Medium | Medium | `agents/report/db_client.py` vs `intelligence.py` |
| 14 | **`roads_blocked` DB column stores km under an ambiguous name**, plus a separate DB-vs-payload rounding mismatch. | Impact | contract | Low-Medium | Low | `agents/impact/services/db.py:93` vs `agent.py:220` |
| 15 | **No code-side enforcement of vulnerability's own stated scoring rules or LLM hospital/school "at risk" subsetting against real OSM counts.** | Impact | science | Low-Medium | Low | `agents/impact/tasks/vulnerability.py`, `infrastructure.py` |
| 16 | Assorted dead code (unused earthquake/landslide LLM prompt construction, orphaned mosaic set-cover, `r2_reader.py`, several report `llm_clients.py` functions, etc.) — no wrongness, just maintenance debt. | All four | dead code | Low | Low each | see each agent's own ANALYSIS.md Section 9 |

---

## I. WHAT I WOULD FIX FIRST

Ranked by how much wrongness is removed from the operator's view, not by
ease of implementation.

1. **Fix impact's flood-hardcoded prompts (H#1) and hazard's
   earthquake/landslide-to-LOW hardcoding that feeds it.** This is the
   single highest-impact fix available: it corrects a false factual premise
   fed directly to the LLM producing the numbers a life-safety responder
   reads and acts on, for every non-flood disaster this system claims to
   handle. Nothing else in this audit misleads as directly or as
   consequentially. Fixing this requires touching two agents (hazard must
   stop discarding earthquake/landslide risk when handing off to impact;
   impact must branch its prompts on real disaster type) but each change is
   individually small.

2. **Resolve the `impact_data.overall_confidence` schema question (H#2)
   against live Neon.** Until this is verified, nobody can trust whether
   the described pipeline behavior (degrading gracefully) or the worse
   behavior (every report failing) is what's actually happening in
   production. This is a five-minute verification (one query against the
   live schema) that determines whether item 6 below, and much of this
   document's Section B, describes a working system or a currently-broken
   one. Verify before investing further engineering effort anywhere else.

3. **Stop hazard's invalid-bbox fallback from emitting `overall_severity:
   "HIGH"` (H#3).** A single-field change (return a distinct sentinel, or
   an explicit `plumbing_error: true` flag) removes a pure plumbing failure
   from ever masquerading as a real disaster signal in a downstream
   response-level decision.

4. **Branch hazard's deterministic flood fallback on `satellite_type`
   (H#4).** Small, well-scoped, mirrors logic that already exists correctly
   one function up in the same file — removes a genuine unit-confusion
   defect from the one path (LLM failure) where it can currently fire.

5. **Fix report's `db_context_to_report_context` to carry the real
   `index_type`/`mean_value` instead of placeholder values (H#5).** Restores
   a fact the satellite agent worked hard to compute and label correctly,
   currently thrown away one hop before it would matter most (the
   human-facing report).

6. **De-fragilize the confidence chain (H#6)** — either by removing the two
   dead `_collect_confidence_values` read keys and documenting clearly that
   `hazard_scores["flood"]` is the real satellite-confidence channel, or by
   actually wiring `incoming_payload` from `PipelineState` into the report
   node so the two `fa0d9bd`-added keys become live. Either fixes the
   fragility; leaving it as-is means the next well-intentioned refactor
   could silently reintroduce the exact bug `fa0d9bd` was written to fix,
   with the regression test possibly not catching it depending on how the
   refactor is shaped.

7. **A real population-raster x hazard-extent geospatial model (H#8).**
   This is the largest effort item on this list, but it is also the
   science gap with the most direct life-safety consequence — every NDMA
   response-level recommendation this system produces is downstream of a
   population-affected number that currently has no defensible geospatial
   basis at all.

Everything else in the consolidated gap list (H#7, #9-16) is real and worth
fixing but ranks below these seven because each either (a) is already
honestly disclosed to a careful reader (SAR uncalibration), (b) affects a
capability that's simply absent rather than actively wrong (risk_polygons),
or (c) is a lower-consequence contract/maintenance issue that doesn't
actively mislead a responder's life-safety decision.
