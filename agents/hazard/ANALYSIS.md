# Hazard Agent — Deep Analysis (2026-07-27)

**Scope:** `agents/hazard/` as it exists on `main` (tip `b1be94f`). Files read
in full: `node.py`, `agent.py`, `analyzer.py`, `intelligence.py`. Downstream
consumers checked directly (`agents/impact/node.py`, `agents/impact/agent.py`,
`agents/report/db_client.py`) rather than trusted from other docs. No code
was changed to produce this document.

---

## 1. Responsibility

The hazard agent answers: **"given the satellite's read of the ground and
independent third-party disaster feeds, what is the flood/earthquake/
landslide risk level, and how confident is that verdict?"** It owns: the
satellite-payload flat→nested contract adapter, three independent risk
computations (flood — hybrid LLM/deterministic; earthquake — pure
deterministic from USGS; landslide — pure deterministic from DEM slope), and
writing `hazard_zones` (3 rows/event).

**Not its job:** producing satellite imagery/indices (consumes them), deciding
population/infrastructure impact (impact agent's job — hazard only hands off a
risk level + confidence), narrative report generation.

**Where responsibility blurs:** `_normalise_satellite_payload` is real
contract-translation work that arguably belongs at the satellite→hazard graph
edge rather than inside the hazard agent, but there is no separate "edge"
layer in this codebase — each node owns its own upstream adapter. The
`risk_polygons` field is always `{}` — despite `hazard_zones.geometry` being a
real PostGIS column, nothing in this agent ever populates a polygon; the
column exists but the responsibility to fill it belongs to no one currently.

---

## 2. Execution Flow

### 2.1 Entry point

`node.py:hazard_node(state)` reads `state["satellite_result"]`, calls
`await analyze_hazard(satellite_result, event_id)`, and branches on
`result["status"]`. On success it also computes `confidence_scores["hazard"]`
as a **flat unweighted average** of `result["hazard"]["confidence_scores"]`'s
values (flood/earthquake/landslide) — `sum(hazard_confidences.values()) /
len(hazard_confidences)` (node.py:45) — collapsing three independently-scaled
numbers into one before it ever reaches `PipelineState`.

### 2.2 `analyze_hazard` (agent.py:177-232)

```
analyze_hazard(satellite_payload, event_id)
├─ satellite_data = _normalise_satellite_payload(satellite_payload, event_id)   [flat→nested adapter]
├─ raw_result = await run_parallel_analysis(satellite_data)                      [analyzer.py — see 2.3]
├─ qc = await quality_check(raw_result)                                          [intelligence.py]
│    branch: not qc["passed"] → return {"status":"error","error":"quality check failed"}
├─ await write_to_db(raw_result)                                                 [hazard_zones INSERT, 3 rows]
└─ return {"status":"complete","hazard":{flood_risk,earthquake_risk,landslide_risk,
           overall_severity,confidence_scores,risk_polygons:{},risk_polygons_url:""}}

except Exception → return {"status":"error","error":str(e)}   [BLANKET CATCH]
```

Order confirmed by direct read: parse → analyze → quality_check → (gate) →
write_to_db → return. A failed quality check correctly prevents a DB write
(the `write_to_db` call sits after the `qc["passed"]` gate, not before it).

### 2.3 `run_parallel_analysis` (analyzer.py:333-455) — the real risk computation

```
run_parallel_analysis(satellite_data)
├─ bbox = boundaries.bbox; branch: not bbox or len(bbox)<4
│    → return ALL THREE risks "UNKNOWN", confidence 0.0/0.0/0.0,
│      overall_severity HARDCODED "HIGH", error "Invalid bbox received from satellite agent"
├─ asyncio.gather(fetch_gdacs(bbox), fetch_usgs(bbox), fetch_slope(bbox))          [independent 3rd-party fetches]
│    each wrapped in return_exceptions=True; any exception → safe empty default dict, no crash
├─ asyncio.gather(analyze_flood(...), analyze_earthquake(...), analyze_landslide(...))
│    each wrapped in return_exceptions=True; any exception → {"risk":"UNKNOWN","confidence":0.0}
├─ FLOOD-ONLY confidence cap: if satellite_confidence is not None and satellite_confidence <
│    flood["confidence"]: flood["confidence"] = satellite_confidence  [fa0d9bd, confirmed live]
├─ overall_severity = highest of {flood,quake,landslide}.risk by severity_map (UNKNOWN maps to 1, no escalation)
└─ return {flood_risk, earthquake_risk, landslide_risk, overall_severity, unknown_count,
           primary_unknown, confidence_scores:{flood,earthquake,landslide},
           satellite_confidence, confidence_cap_applied, risk_polygons:{}, raw:{gdacs,usgs,slope}}
```

**Every external I/O call is individually fault-isolated** (`return_exceptions=True`
at both gather sites) — one feed failing cannot crash the whole analysis, it
degrades to a documented safe default (empty GDACS/USGS, DEM `slope_estimate:
15.0` "estimated", or an UNKNOWN/0.0-confidence risk result for that one
hazard).

---

## 3. Decision Logic

| Decision | Driver | Deterministic or LLM? | Basis |
|---|---|---|---|
| Flood risk | LLM primary (`analyze_flood`, criticality="normal" -> Gemini-first then Featherless chain), deterministic fallback only on total LLM failure | **Hybrid** | LLM prompt correctly branches index label/context by `satellite_type` (SAR vs NDWI); deterministic fallback does NOT (see Section 6). |
| Earthquake risk | `max_mag` thresholds: >=7.0 CRITICAL/.85, >=5.5 HIGH/.8, >=4.0 MEDIUM/.7, else LOW/.85 | **Pure deterministic**, LLM path exists in the function body (prompt/system built) but its result is never used — the LLM call is dead weight here (see Section 9) | Round numbers; USGS magnitude-to-risk-class mapping is a defensible engineering simplification (higher magnitude -> more damage) but the specific cut points (7.0/5.5/4.0) are not cited from any seismic-hazard standard (e.g. MMI scale, USGS ShakeMap intensity bands) in the code. |
| Landslide risk | Slope thresholds: >45 CRITICAL/.8, >30 HIGH/.75, >15 MEDIUM/.65, else LOW/.8 | **Pure deterministic**, same dead-LLM-path pattern | Same as earthquake — round numbers, plausible ordering (steeper = more landslide-prone, well-established in the literature), specific degree cutoffs uncited. GDACS `count` deliberately excluded from the decision (documented reason: GDACS bbox filter is unreliable, returns global events — confirmed correct engineering judgment, not a bug). |
| Invalid-bbox fallback | `not bbox or len(bbox) < 4` | Deterministic | **Hardcodes `overall_severity: "HIGH"` on a non-event** — this is `root_cause.md`'s originally-documented defect, confirmed STILL PRESENT verbatim in current code (analyzer.py:350). A satellite-to-hazard plumbing miss (empty bbox) produces a false-HIGH severity stamp with zero real signal behind it. |
| Confidence cap (flood only) | `satellite_confidence < flood_confidence` | Deterministic | Real, `fa0d9bd`-added fix — correctly scoped to flood only (the only hazard that consumes a satellite signal at all, see Section 6 / SYSTEM_ANALYSIS.md Section D). |
| Overall severity | `max(severity_map[flood], severity_map[quake], severity_map[landslide])`, UNKNOWN->1 | Deterministic | Sound design — UNKNOWN correctly does not escalate severity (this was previously buggy per an in-code comment: "Previously `unknown_count >= 2` force-set HIGH... Removed" — confirmed as a real prior fix, not a currently-open issue). |
| Overall hazard confidence into `PipelineState` | Flat unweighted average of flood/earthquake/landslide confidences | Deterministic | **Arbitrary** — a flood-only event's earthquake/landslide legs return confident-sounding UNKNOWN (0.0 confidence per the invalid-bbox path, or a real number if bbox is valid), diluting or inflating the average depending on which legs fired. See SYSTEM_ANALYSIS.md Section B for the full downstream trace. |

**LLM fallback equivalence:** flood's fallback (analyzer.py:211-227) IS a
genuinely different computation from its LLM path — the LLM path branches
`index_label`/`index_context` by `satellite_type` (SAR vs NDWI, analyzer.py:
180-185), the deterministic fallback does not (flat `flood_index > 0.5`/`0.3`
NDWI-scale check regardless of index type). **This is not an equivalent
fallback** — it is a degraded one that silently applies the wrong unit scale
to SAR-path `mean_value`, confirmed unchanged from `root_cause.md`'s original
finding (Section 4.3 there). Earthquake/landslide have no LLM path that
materially differs from their deterministic result (the LLM call exists in
the source but its return value is discarded — see Section 9), so there is no
fallback degradation risk for those two; the deterministic result is the only
result that ever ships.

---

## 4. The Analysis Itself — The Science

### 4.1 Flood (hybrid)

LLM path: prompted with `affected_area_km2`, the correctly-labeled index
(`SAR backscatter ratio (VV-VH)` vs `NDWI flood index`, with unit-appropriate
context text), and GDACS event count; asked for CRITICAL/HIGH/MEDIUM/LOW +
confidence + reasoning + affected_zones. This is sound in principle — it
correctly avoids the unit-confusion trap the deterministic fallback falls
into — but the actual reasoning quality is opaque (no rubric, no cited
literature thresholds visible to an auditor beyond what's in the prompt
string itself).

Deterministic fallback: `area > 200 or flood_index > 0.5` -> CRITICAL;
`area > 100 or flood_index > 0.3` -> HIGH; `area > 25` -> MEDIUM; else LOW. The
`flood_index` thresholds (0.5/0.3) are NDWI-ratio-scale constants baked in
regardless of what `flood_index` (== satellite's `mean_value`) actually is.
For a SAR-path event, `mean_value` is unbounded dB (e.g. `23.6485` on the live
2026-07-26 run) — `flood_index > 0.5` is trivially true for a positive-dB SAR
reading, meaning **the fallback can classify an S1 run as CRITICAL purely
because the uncalibrated dB number happens to exceed 0.5**, an artifact of
unit confusion, not evidence of flooding. This is the single most scientifically
indefensible decision point in this agent.

### 4.2 Earthquake (deterministic)

Magnitude-threshold classification against real USGS-fetched seismicity
(`fetch_usgs`, 7-day window, `minmagnitude: 2.0`). The core engineering
decision — "recent observed magnitude, not regional reputation, drives
risk" — is scientifically sound and explicitly, deliberately designed to
resist LLM tendency to inflate risk from a region's earthquake history even
with zero recent activity (confirmed by the in-code comments and system
prompt: "never invent elevated risk from a region's general reputation").
This is a genuinely good design decision, not merely documented as one.

The specific magnitude cut points (7.0/5.5/4.0) roughly track common public
intuition about earthquake severity but are not cross-referenced against a
named seismic intensity scale (Richter/Moment magnitude conflation is also
present — USGS `mag` in the GeoJSON feed can be any of several magnitude
scales depending on the event, and the code treats them uniformly).

### 4.3 Landslide (deterministic)

Slope-threshold classification against a real 5x5 SRTM 30m DEM grid sampled
from OpenTopoData (`fetch_slope`), with a proper elevation-gradient-to-slope
calculation (`np.gradient`, metres-per-degree conversion accounting for
latitude). This is scientifically legitimate terrain analysis, and the
documented decision to exclude GDACS `count` (unreliable bbox filtering, "93
events for Rawalpindi, all at coordinates in China/Mongolia") is a correct,
verified engineering judgment, not a shortcut.

The specific slope cut points (45/30/15 degrees) are plausible orderings but
uncited against any landslide-susceptibility literature standard (e.g. no
reference to a specific USGS/geotechnical slope-stability threshold study).
The conservative failure default (`slope_estimate: 10.0` when the DEM is
unreachable -> maps to LOW, never fabricating steepness) is a defensible,
documented safety choice — a DEM failure produces a false negative, never a
false positive, on this hazard.

---

## 5. Confidence

### 5.1 Per-hazard confidence — three independent, unrelated scales

- **Flood:** LLM self-reported (clamped [0,1], default 0.55 on parse failure),
  now capped at satellite's own confidence when satellite reports lower
  (`fa0d9bd`). Deterministic fallback assigns flat constants (0.7/0.65/0.6/0.55
  by risk tier) — these are NOT calibrated probabilities, just per-tier
  placeholders.
- **Earthquake:** flat constant per risk tier (0.85/0.8/0.7/0.85 — note LOW
  is *more* confident than MEDIUM, 0.85 vs 0.7, reflecting "confident there's
  no earthquake" rather than a monotonic uncertainty scale).
- **Landslide:** flat constant per risk tier (0.8/0.75/0.65/0.8, same
  LOW-more-confident-than-MEDIUM pattern as earthquake).

**None of these three confidences are calibrated against any accuracy data.**
They are hand-picked constants keyed to risk tier, not a measure of how
certain the underlying evidence actually is (e.g. earthquake confidence does
not vary with USGS event *count* or *catalog completeness*, only with the
single derived risk tier).

### 5.2 The satellite confidence cap — real but narrow

```python
# analyzer.py:409-414
confidence_cap_applied = False
if satellite_confidence is not None and flood.get("confidence") is not None:
    flood_confidence = _to_float(flood.get("confidence"))
    if satellite_confidence < flood_confidence:
        flood = {**flood, "confidence": satellite_confidence}
        confidence_cap_applied = True
```

This is a real, correctly-scoped fix (added by `fa0d9bd`) — flood is the only
hazard whose conclusion is derived from a satellite signal, so capping its
confidence at satellite's own uncertainty is the right mechanism.
Earthquake/landslide are deliberately, correctly left uncapped (self-sourced
from USGS/DEM, independent of satellite confidence).

### 5.3 What gets discarded before `PipelineState`

`node.py`'s `confidence_scores["hazard"] = avg(flood, earthquake, landslide)`
(flat unweighted average, node.py:45) throws away:
- Which hazard's confidence is low and why (a low-confidence flood cap vs. a
  low-confidence deterministic earthquake/landslide read the same in the
  average).
- `satellite_confidence` and `confidence_cap_applied` themselves — computed by
  `analyzer.py`, present in `raw_result["hazard"]`'s dict — but `node.py`
  extracts only `confidence_scores` from `result["hazard"]`, not
  `satellite_confidence`/`confidence_cap_applied`. **These two fields never
  leave the hazard agent's own return payload into `PipelineState`.**

### 5.4 What gets written to `hazard_zones`

`write_to_db` (agent.py:102-174) writes one `overall_confidence` value per
hazard-type row directly from `confidence_scores.get(hazard_type, 0.0)` — so
the flood row's `overall_confidence` DOES carry the satellite-capped value
into the durable DB record. This is the ONLY channel by which satellite
confidence reaches any DB-persisted table downstream of hazard — see
`SYSTEM_ANALYSIS.md` Section B for why this one channel is diluted again one
hop later by report's flat average across all three `hazard_zones` rows.

### 5.5 Calibrated or heuristic?

**Heuristic, on all three legs.** No historical accuracy data backs any of
the constants. The flood confidence cap is the one place where this agent's
confidence output is genuinely informed by an upstream measurement rather
than a fixed lookup table — everywhere else, confidence is a function of
which risk *tier* was reached, not of how much or how reliable the underlying
evidence was.

---

## 6. Data Contract

### 6.1 `_normalise_satellite_payload` — the flat->nested adapter

Confirmed real and necessary: satellite emits a flat payload (`bbox`,
`affected_area_km2`, `mean_index`, `water_percent`, `risk_cities`,
`satellite_type` all top-level); the analyzer reads a nested shape
(`boundaries.bbox`, `analysis.affected_area_km2`, `analysis.mean_value`,
`satellite.type`). Without this adapter, `run_parallel_analysis` would see an
empty `bbox` and hit the invalid-bbox path (hardcoded HIGH severity) on
*every* run — this adapter is load-bearing, not decorative.

**GATE B correction (2026-07-28, superseding this section's original
claim):** this document previously stated that `index_calibrated`/
`index_units` WERE carried through by the adapter (mapped into `analysis.*`
at agent.py:88-91, alongside `index_type`/`needs_verification`) but simply
unused downstream. That was **wrong** — confirmed by direct read of
`agents/hazard/agent.py`'s `_normalise_satellite_payload` prior to this
session's fix: the pre-fix `analysis{}` dict only carried `index_type`,
`confidence`, `needs_verification` — `index_calibrated`/`index_units` were
never in the returned dict at all.
`agents/satellite/ANALYSIS.md`'s corresponding claim (that these fields were
NOT carried) was the correct side of this disagreement.

Fixed as part of H#4: `_normalise_satellite_payload` now carries
`index_calibrated`, `index_units`, `confidence_basis`, and `evidence_count`
into `analysis{}`, and `analyzer.py`'s deterministic flood fallback (§6.2
below) reads `index_calibrated` to decide whether the raw index can be
threshold-compared at all, rather than reconstructing an equivalent
inference from `satellite_type` alone (which was correct today only because
`satellite_type` and `index_calibrated` happen to always agree in the
current implementation — a coincidence, not a guarantee).

### 6.2 The confirmed label/content mismatch — SAR-as-NDWI, hazard-side instance

**FIXED 2026-07-28 (SYSTEM_ANALYSIS.md H#4).** `analyze_flood`'s
deterministic **fallback** (analyzer.py:211-227, reached only when the LLM
call returns nothing) read `mean_value` — which is SAR-dB (unbounded, e.g.
`-12` or `23.6`) on an S1 run, or NDWI ratio (bounded [-1,1]) on an S2 run —
and applied the SAME flat NDWI-scale threshold (`flood_index > 0.5`/`> 0.3`)
regardless of which. Confirmed via direct read: no `satellite_type` branch
existed in this fallback block, even though the LLM-facing prompt one
function up (same file) correctly branches on it. This was the exact defect
class satellite's own ANALYSIS.md documents for `mean_index` (a field whose
numeric meaning silently depends on `satellite_type`) recurring one hop
downstream. As this document's Section 8 already correctly noted, this
codebase's SAR index is uncalibrated raw-DN log and is **positive**, so the
old threshold produced a **false CRITICAL**, not a false LOW — confirmed
live (`mean_value = 23.6485` on the 2026-07-26 S1 e2e run).

Fixed: the fallback now reads `index_calibrated` (carried through
`_normalise_satellite_payload` per §6.1's correction above) and, for
uncalibrated SAR, bases the flood decision on `affected_area_km2` alone
(never the raw index), with confidence capped at 0.4 and an explicit
`"sar_index_excluded_uncalibrated"` anomaly recorded. The NDWI/calibrated
path (S2) is unchanged.

### 6.3 Fields produced but not read by any downstream consumer (confirmed by grep)

- `risk_polygons` — always `{}`, both in the in-memory result and the
  `hazard_zones.geometry` PostGIS column (which the DDL supports but nothing
  writes to). Grep confirms zero writers of a non-empty polygon anywhere in
  `agents/hazard/`.
- `risk_polygons_url` — always `""`, same pattern.
- `unknown_count`/`primary_unknown` — computed in `analyzer.py`'s return dict,
  not read by `node.py`, not persisted, not consumed by impact or report.
- `raw` (the GDACS/USGS/slope raw payloads) — computed, not surfaced past
  `analyzer.py`'s own return value; `node.py` never forwards it into
  `PipelineState`.
- `satellite_confidence`/`confidence_cap_applied` — see Section 5.3, computed
  by `analyzer.py`, discarded by `node.py` before `PipelineState`.

### 6.4 Fields impact actually reads

`agents/impact/node.py` (`impact_node`) reads `hazard_result["hazard"]` for:
`flood_risk` (preferred first for `risk_level`, falling back to
`overall_severity`/`risk_level`), `overall_severity`, `confidence_scores`
(re-averaged a SECOND time, `impact_node.py:42`), `bounds`/`risk_cities`
(from `hazard_result` top-level, which hazard's payload never actually sets —
see `SYSTEM_ANALYSIS.md` Section D), `risk_polygons` (always `{}`),
`flood_depth_estimate` (hazard never sets this field at all — always absent,
defaults to `0.0` in impact). **`impact_node`'s risk_level derivation
preferring `flood_risk` over `overall_severity` means a pure earthquake or
landslide event (where `flood_risk` legitimately comes back LOW/UNKNOWN
because there's no flood) can read as risk_level LOW even when
`overall_severity` is HIGH/CRITICAL from the earthquake or landslide leg** —
this is a real, unverified-as-exercised risk of understating impact for
non-flood disasters. See `SYSTEM_ANALYSIS.md` Section D for the full trace.

---

## 7. Failure Modes

| Failure | Trigger | Surfaced or swallowed? | What's returned | Downstream can tell apart from success? |
|---|---|---|---|---|
| Invalid/missing bbox | satellite->hazard handoff produced empty/short bbox | Surfaced as `error` key, but **severity is hardcoded HIGH** | All risks UNKNOWN, `overall_severity: "HIGH"` | **NO for severity** — a plumbing failure and a real HIGH-severity event produce the identical `overall_severity` value; only the presence of the `error` key (if checked) distinguishes them, and `quality_check`'s checks (Section 3) do not reject "HIGH" as invalid, so this payload passes quality_check and gets persisted to `hazard_zones` as a real HIGH row. |
| GDACS/USGS/slope fetch exception | any network/parse failure | Swallowed via `return_exceptions=True`, safe default substituted | Empty events/quake list, or `slope_estimate: 15.0 "estimated"` | Partially — the `source` field in each raw payload says `"estimated"`/empty list, but this is never surfaced past `analyzer.py`'s own `raw` key (discarded by `node.py`, Section 6.3), so a downstream consumer cannot see it happened. |
| One of the three `analyze_*` tasks raises | any exception inside `analyze_flood`/`analyze_earthquake`/`analyze_landslide` | Swallowed via `return_exceptions=True` | `{"risk":"UNKNOWN","confidence":0.0,"reasoning":"task failed"}` for that hazard only | Partially — `risk: "UNKNOWN"` is visible, but "this specific hazard's task threw an exception" and "this hazard genuinely could not be assessed" (e.g. invalid bbox) are indistinguishable once collapsed to UNKNOWN. |
| `quality_check` fails | any of the 6 structural checks (Section 3, this doc doesn't repeat the full list — risk fields valid, confidence_scores present, event_id present) | Surfaced — `status: "error"`, DB write skipped | `{"status":"error","error":"quality check failed"}` | Yes — this correctly prevents a malformed payload from reaching `hazard_zones`. |
| Whole-pipeline unexpected exception | any uncaught exception in `analyze_hazard`'s try block | Surfaced, but `str(e)` only, no stage attribution | `{"status":"error","error":str(e)}` | Yes it's an error, but opaque as to which of `_normalise_satellite_payload`/`run_parallel_analysis`/`quality_check`/`write_to_db` actually threw. |
| `write_to_db` fails | any asyncpg exception during the 3-row INSERT | **NOT independently caught** — propagates up through `analyze_hazard`'s outer `try/except Exception` | `{"status":"error","error":str(e)}` (the blanket catch) | Yes, correctly surfaces as an error (unlike satellite's now-fixed silent-persist-failure pattern) — hazard's DB write failure was never silently swallowed to begin with, since there's no per-write try/except around it. |

**The single most dangerous failure mode:** the invalid-bbox path's hardcoded
`overall_severity: "HIGH"`. It passes `quality_check` (severity `"HIGH"` is a
valid enum value per the check), gets written to `hazard_zones` as a real row,
and is indistinguishable — from the DB alone — from a genuine HIGH-severity
verdict. Confirmed unchanged from `root_cause.md`'s original documentation of
this exact defect.

---

## 8. External Dependencies

| Dependency | Timeout/retry | On unavailable |
|---|---|---|
| GDACS GeoJSON feed (`fetch_gdacs`) | 15s timeout, no retry | Caught exception -> `{"events":[],"count":0,"source":"gdacs","error":str(e)}`; degrades cleanly, `analyze_landslide` explicitly ignores GDACS `count` anyway (documented reason, Section 4.3). |
| USGS FDSN query (`fetch_usgs`) | 15s timeout, no retry | Caught exception -> empty earthquake list; `analyze_earthquake` then reads `max_mag=0.0` -> LOW risk (a false negative on outage, never a false positive). |
| OpenTopoData DEM (`fetch_slope`) | 20s timeout, no retry, 5x5=25-point grid (within the 100-location/request public API limit) | Caught exception -> `slope_estimate: 10.0` (bad-bbox case) or `15.0` conservative default with `source: "no_dem_conservative_default"`/`"estimated"` — both map to LOW/near-LOW risk, a documented safe-failure design. |
| Gemini (primary LLM, `intelligence.py`) | 30s timeout, up to 5 keys chained | Falls through to Featherless chain. |
| Featherless (fallback LLM chain, 4 models) | 30s timeout per model, 1.5s sleep between failures | Falls through to Opus (AIML) on `criticality != "low"`. |
| Opus/AIML (last-resort LLM) | no explicit timeout override | Returns `None` on failure; every caller (`analyze_flood`, `quality_check`'s `handle_anomaly`) has a deterministic fallback that never depends on the LLM succeeding to produce SOME result — though as documented in Section 3/6.2, the flood fallback's result can be scientifically wrong (unit mismatch), not merely less rich. |
| Neon Postgres (`write_to_db`) | `asyncpg.connect()` per call, no pool, no explicit retry | Any exception propagates up to `analyze_hazard`'s blanket catch -> surfaced as `status:"error"` (correctly, unlike satellite's pre-fix silent-swallow pattern). |

---

## 9. Dead and Unreachable Code

| Item | Status | Apparent purpose | Real gap or should-delete? |
|---|---|---|---|
| `analyze_earthquake`'s and `analyze_landslide`'s built `prompt`/`system` strings and the LLM machinery they imply | The prompt/system variables are constructed (analyzer.py:238-255, 286-303) but **the function bodies never call `smart_llm_call` with them** — both functions compute their risk purely from the deterministic threshold block below the prompt construction, and the prompt variables are simply unused local variables. Confirmed by direct read: no `await smart_llm_call(prompt, ...)` call exists in either function body. | Looks like a deliberate design note (the prompt documents the reasoning an LLM *would* use, immediately followed by "we intentionally do NOT ask an LLM here" comments) rather than an accidental leftover — the comments explicitly explain why LLM reasoning is skipped for these two hazards. | **Not a bug, but genuinely dead code from an execution standpoint** — the `prompt`/`system` local variables do nothing at runtime, computed and discarded every single call. Should be deleted (or converted to a docstring/comment, which is apparently their real intent) since they cost a wasted string-formatting operation on every hazard run and could mislead a future maintainer into thinking an LLM call is about to happen. |
| `write_band_message` (`intelligence.py`) | Defined, never called from `agent.py`/`node.py`/`analyzer.py` in the current LangGraph pipeline (confirmed: the agent's own `CLAUDE.md` explicitly calls this out as a "leftover natural-language handoff generator... kept as dead code"). | Was the Band `@mention` handoff message generator. | Confirmed genuinely dead post-migration, correctly identified as such in the agent's own `CLAUDE.md`. Not re-flagged as a new finding, just independently confirmed. |
| `risk_polygons`/`risk_polygons_url` | Always `{}`/`""`, never populated by any code path in this agent. | Docs (`CODEBASE.md` per root `CLAUDE.md`'s "Known Issues") claim PostGIS polygon generation exists; it does not. | **Real gap, not dead code to delete** — the `hazard_zones.geometry` column is a genuine, GIST-indexed PostGIS column with no writer. Flagged, not a fix in scope here. |

---

## 10. The Gap List — Prioritized

| # | Issue | Type | Severity | Effort | Evidence |
|---|---|---|---|---|---|
| 1 | **Invalid-bbox fallback hardcodes `overall_severity: "HIGH"` on a non-event.** A satellite->hazard plumbing miss (empty/short bbox) produces a real HIGH-severity `hazard_zones` row indistinguishable from a genuine HIGH verdict — passes `quality_check`, gets persisted, propagates to impact and report as a real disaster signal. | correctness | **Very high** — the exact failure class `root_cause.md` flagged, confirmed still present verbatim; can trigger unwarranted downstream escalation (impact's hospital/response-level anomaly logic, report's severity narrative) from zero real signal. | Low (return a distinct status/severity like `"UNKNOWN"` or an explicit `plumbing_error: true` flag instead of overloading `"HIGH"`) | analyzer.py:344-358 |
| 2 | **Flood's deterministic fallback applies NDWI-scale thresholds to a SAR-dB `mean_value` without checking `satellite_type`**, unlike the LLM-facing prompt one function up in the same file. | contract/science | High but conditional (only the LLM-total-failure path) — can produce a false CRITICAL from a positive uncalibrated SAR dB value, or a false LOW from a negative one, purely from unit confusion. | Low (branch the fallback on `satellite_type`/`index_calibrated`, mirroring the prompt logic already present) | analyzer.py:180-185 (correct) vs 211-227 (not) |
| 3 | **`node.py`'s flat-average `confidence_scores["hazard"]` and `write_to_db`'s per-row confidence both discard `satellite_confidence`/`confidence_cap_applied` from `PipelineState`/durable visibility as distinct fields**, even though `analyzer.py` computes them. Only the flood row's already-capped `overall_confidence` survives into `hazard_zones`; earthquake/landslide rows carry uncapped, unrelated confidence scales averaged in with it one hop later at the report layer. | contract | High — this is the mechanism by which satellite's carefully-computed uncertainty gets diluted, not by an intentional decision but by two successive flat averages (hazard's own averaging, then report's `_average_confidence` across all three `hazard_zones` rows). See `SYSTEM_ANALYSIS.md` Section B. | Medium (surface `satellite_confidence`/`confidence_cap_applied` distinctly in `PipelineState`, and stop flat-averaging three unrelated-scale confidences into one number) | node.py:42-46; agent.py:110-172 |
| 4 | **`risk_polygons` is always `{}` despite `hazard_zones.geometry` being a real, GIST-indexed PostGIS column with no writer anywhere.** | contract | Medium — a documented capability (per `CODEBASE.md`/root `CLAUDE.md`) that does not exist; any consumer expecting spatial hazard zones from this table gets nothing. | High (requires actually generating/writing geometry, not a small fix) | analyzer.py:453; agent.py `write_to_db` (no `geometry` param anywhere) |
| 5 | **Earthquake/landslide magnitude and slope cut points are round numbers not cross-referenced against any named seismic/geotechnical standard**, though the ordering and general design (observed-data-only, no reputation-based inflation) is sound. | science | Medium — the overall approach is defensible; the exact numbers are not independently justified in-code. | Medium (a literature-grounding pass, not a redesign) | analyzer.py:262-269, 311-318 |
| 6 | **`analyze_earthquake`/`analyze_landslide` build unused LLM prompt/system strings on every call** — dead computation, not a correctness bug, but wasted work and a misleading read for a future maintainer. | dead code / performance | Low | Low (delete the unused prompt construction or convert to a comment) | analyzer.py:238-255, 286-303 |
| 7 | **`impact_node`'s risk_level derivation prefers `hazard.flood_risk` over `hazard.overall_severity`**, meaning a pure earthquake/landslide event with a legitimately LOW/UNKNOWN flood leg can read as low-risk to impact even when `overall_severity` is HIGH/CRITICAL from the actual disaster type. This is impact-side code, flagged here because the root cause is hazard not surfacing a disaster-type-aware "primary risk" field for impact to prefer. | contract | High if it fires on a real non-flood event — would understate population/infrastructure impact for the exact disaster type the pipeline was dispatched to assess. | Medium (hazard could expose an explicit `primary_hazard_risk` keyed by `disaster_type`, or impact could stop defaulting to `flood_risk` first) | agents/impact/node.py:32-37 |

---

## Appendix — Where This Document Confirms vs. Corrects Prior Docs

- **Confirms `root_cause.md`:** the invalid-bbox -> hardcoded HIGH severity
  defect is real and unfixed (Section 10 #1); the flood-fallback SAR-as-NDWI
  unit mismatch is real and unfixed (Section 10 #2); earthquake/landslide
  genuinely read no satellite signal at all (confirmed independently by
  direct trace of `analyze_earthquake(bbox, usgs_data)`/
  `analyze_landslide(bbox, gdacs_data, slope_data)` — neither function's
  parameter list nor body touches any satellite-derived field beyond `bbox`
  for query scoping).
- **Confirms root `CLAUDE.md`:** `_normalise_satellite_payload` is real and
  necessary (Section 6.1); `risk_polygons` is always `{}` (Section 9, Section
  10 #4).
- **Extends/corrects:** the satellite-confidence propagation is now real for
  the flood leg (`fa0d9bd`, Section 5.2) — but this document identifies a NEW,
  previously undocumented dilution: `node.py`'s own flat-average of
  flood/earthquake/landslide confidences, and `write_to_db`'s per-row split,
  mean the satellite-capped signal is preserved only in the `hazard_zones`
  flood row specifically, not in the single `confidence_scores["hazard"]`
  number that reaches `PipelineState` (Section 5.3, Section 10 #3). This is
  finer-grained than root `CLAUDE.md`'s framing ("hazard never reads
  satellite confidence at all," which was true pre-`fa0d9bd` and is
  corrected in the satellite agent's own `ANALYSIS.md`).
- **New finding not previously documented anywhere in this repo:** the
  `impact_node`'s `flood_risk`-preferred-over-`overall_severity` derivation
  (Section 6.4, Section 10 #7) — a plausible mechanism for understating
  non-flood disaster impact that neither `root_cause.md` nor any `CLAUDE.md`
  flags.
