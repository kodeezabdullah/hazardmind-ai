# Root-Cause Attribution — Earthquake Built-Up-Area False Positive

**Question:** Is the earthquake built-up-area false-positive bug caused by the
**satellite** agent producing a bad/uncalibrated signal, or by the **hazard**
agent misusing a correct signal?

**Short answer: NEITHER, in the way the question frames it.** The earthquake
risk path does **not consume any satellite output at all** — not a built-up
layer, not SAR backscatter, not a raw DN layer, nothing. Earthquake risk is
computed **deterministically from USGS seismic data that the hazard agent fetches
itself** from the bbox. So:

- The bug is **NOT** caused by the satellite agent (its output is never read by
  the earthquake path). → **not (a)**.
- The bug is **NOT** the hazard agent misinterpreting a satellite signal as
  earthquake hazard (there is no satellite signal on the earthquake path to
  misinterpret). → **not (b)** as literally worded.
- There is **no built-up/urban classification layer anywhere in the codebase** —
  neither agent produces one. The premise "built-up-area false positive from a
  built-up layer" does not map to any real data flow.

The closest real defect is a **contract/plumbing failure in the hazard agent**:
when the satellite hands off an empty/invalid `bbox`, the hazard agent's
`run_parallel_analysis` returns **every risk `UNKNOWN` with a hardcoded
`overall_severity: "HIGH"`** — i.e. a non-event stamped HIGH. That is a hazard
agent logic issue, independent of satellite signal quality. See §3, verdict.

---

## 1. Satellite Agent Output — What It Hands Off

### 1.1 The exact handoff surface

The satellite agent's structured result dict is built at
[agent.py:749](agents/satellite/agent.py#L749) and carries these keys (per
`fix.md` §1.3, confirmed against the DB write contract):

```
event_id, status, satellite_type, cloud_cover, selection_reason, index_type,
water_percent, mean_index, class_counts, affected_area_km2, bbox, bounds,
region_boundary, risk_cities, true_color_url, index_url, classification_url,
geojson_url, image_url, cached, cities[], interpretation, confidence, concerns,
validations, needs_verification, should_alert, summary_message
```

### 1.2 Every field — RAW vs PROCESSED

| Field | Nature | Notes |
|---|---|---|
| `satellite_type` | metadata | `"sentinel-1"` \| `"sentinel-2"` |
| `cloud_cover` | processed (metadata) | from catalogue peek |
| `index_type` | metadata | `"NDWI"` / `"NDVI"` / `"SAR"` |
| `mean_index` / `mean_value` | **PROCESSED** | mean of the computed index over valid pixels |
| `water_percent` | **PROCESSED** | % of pixels classified affected |
| `class_counts` | **PROCESSED** | % per graded hazard class |
| `affected_area_km2` | **PROCESSED** | vectorized affected-zone area |
| `classification_url` | **PROCESSED product** | graded hazard overlay PNG |
| `index_url` | **PROCESSED product** | index colormap PNG |
| `true_color_url` | processed product | TCI/greyscale RGBA PNG |
| `geojson_url` | **PROCESSED product** | vectorized zone polygons |
| `bbox` / `bounds` / `region_boundary` / `risk_cities` | geometry/metadata | the AOI |
| `confidence` / `concerns` / `validations` | derived scores | `ConfidenceTracker` heuristic |

**There is NO raw layer in the handoff.** The satellite agent does not hand off a
calibrated σ⁰ backscatter raster, a raw-DN raster, or any per-pixel array — only
**already-classified scalar summaries** (`mean_index`, `water_percent`,
`class_counts`, `affected_area_km2`) plus **URLs to rendered PNG/GeoJSON
products**. All raster processing happens inside the satellite agent; downstream
agents receive only its distilled outputs.

### 1.3 Is there a built-up / urban classification layer? — NO

Grep across `agents/` for `built[-_ ]?up|urban|exposure|land_cover|building|
settlement` returns **zero** hits in the satellite agent. The only classification
the satellite produces is an **index-threshold hazard mask**, and its class
schemes ([processor.py:152-180](agents/satellite/processor.py#L152)) are:

```python
_CLASS_SCHEMES = {
    "NDWI":  { water bands: wet_soil / water / deep_water },       # flood
    "SAR":   { water bands: possible_water / water / deep_water }, # flood (radar)
    "NDVI_QUAKE":     { sparse_veg / stressed / damage },          # earthquake
    "NDVI_LANDSLIDE": { sparse_veg / exposed / scar },             # landslide
}
```

For **earthquake**, the satellite classification is `NDVI_QUAKE` — a **vegetation
loss** proxy (low NDVI = bare/damaged ground), **not** a built-up density map.
There is no urban/settlement layer anywhere in the pipeline.

### 1.4 Is a calibrated SAR (σ⁰) layer available? — NO

Per `fix.md` §3.3, SAR is `10*log10(raw GRD DN)` — **uncalibrated**, no speckle
filter, no terrain correction ([processor.py:1074](agents/satellite/processor.py#L1074)).
And critically: this uncalibrated SAR product **is only computed for the flood
disaster type** (SAR/NDWI schemes are flood). It is never on the earthquake path.

### 1.5 Does the uncalibrated output get passed downstream?

Only its **scalar summaries** (`mean_index`, `water_percent`, `affected_area_km2`)
and rendered PNGs. And again — the SAR summary is a **flood** signal. The
earthquake path (below) never reads `mean_index`, `water_percent`, or any
satellite raster/summary. So the uncalibrated-SAR finding from `fix.md`, while
real, is **irrelevant to earthquake risk** — different disaster type, different
data path.

---

## 2. Hazard Agent Input — What It Actually Consumes

### 2.1 Where hazard reads the satellite result

[node.py:28-30](agents/hazard/node.py#L28):

```python
satellite_result = state.get("satellite_result") or {}
result = await analyze_hazard(satellite_result, event_id)
```

`analyze_hazard` normalises the flat payload
([agent.py:193](agents/hazard/agent.py#L193) →
`_normalise_satellite_payload`) into the nested shape the analyzer reads. The
**only** satellite-derived values that survive normalisation into the analysis
are: `bbox`, `risk_cities`, `affected_area_km2`, `mean_value` (= satellite's
`mean_index`), `water_percent`, `satellite_type`
([agent.py:81-99](agents/hazard/agent.py#L81)).

### 2.2 What the EARTHQUAKE risk logic consumes — exact code

`run_parallel_analysis` calls
[analyzer.py:382-384](agents/hazard/analyzer.py#L381):

```python
analysis_results = await asyncio.gather(
    analyze_flood(bbox, affected_area_km2, mean_value, gdacs_data, satellite_type),
    analyze_earthquake(bbox, usgs_data),          # <-- earthquake
    analyze_landslide(bbox, gdacs_data, slope_data),
    ...
)
```

`analyze_earthquake` takes **only `bbox` and `usgs_data`**
([analyzer.py:230-236](agents/hazard/analyzer.py#L230)):

```python
async def analyze_earthquake(bbox, usgs_data) -> dict:
    magnitudes = [
        feature.get("properties", {}).get("mag")
        for feature in usgs_data.get("earthquakes", [])
        if isinstance(feature, dict)
    ]
    max_mag = max((_to_float(mag) for mag in magnitudes), default=0.0)
    eq_count = usgs_data.get("count", 0)
```

And `usgs_data` is fetched by the **hazard agent itself**, independently, from
the bbox ([analyzer.py:359-373](agents/hazard/analyzer.py#L359) →
`fetch_usgs(bbox)`, hitting the USGS FDSN API). It is **not** a satellite output.

The earthquake risk is then a pure deterministic function of observed seismicity
([analyzer.py:262-269](agents/hazard/analyzer.py#L262)):

```python
    if max_mag >= 7.0:   risk, confidence, liq = "CRITICAL", 0.85, 0.8
    elif max_mag >= 5.5: risk, confidence, liq = "HIGH", 0.8, 0.5
    elif max_mag >= 4.0: risk, confidence, liq = "MEDIUM", 0.7, 0.3
    else:                risk, confidence, liq = "LOW", 0.85, 0.1
```

**Zero satellite fields are read.** No `mean_index`, no `water_percent`, no
`class_counts`, no `classification_url`, no built-up layer. `bbox` is used only to
scope the USGS query, not as a hazard signal. The earthquake path does **not**
perform its own image processing either (no rasterio, no raster reads — it hits
HTTP APIs only).

### 2.3 Does hazard do its own thresholding, or consume a pre-classified layer?

For earthquake: **neither of a satellite layer.** It performs its own
**thresholding on USGS magnitude** — its own independent data source. There is no
satellite-to-earthquake data dependency at all.

---

## 3. Verdict — Definitive

**None of (a)/(b)/(c)/(d) as literally worded, because the earthquake path and
the satellite output are disjoint.** With code proof:

### Not (a) — satellite output is not the cause
The satellite agent's (uncalibrated SAR / index) output is **never read** by
`analyze_earthquake`. `analyze_earthquake(bbox, usgs_data)` reads only
USGS-fetched seismicity ([analyzer.py:230](agents/hazard/analyzer.py#L230)).
Whatever is wrong with the satellite SAR calibration (`fix.md` HIGH finding)
cannot produce an earthquake false positive — it's a flood-path signal.

### Not (b) — hazard is not misinterpreting a satellite signal as earthquake hazard
There is no satellite signal on the earthquake path to misinterpret. The
earthquake path does not treat "raw backscatter or built-up density as earthquake
hazard" — it never touches either. (It could not: no built-up layer exists, and
SAR is only computed for floods.)

### Not (d) — no architecture violation on the earthquake path
`analyze_earthquake` does **no image processing** — no rasterio, no raster reads,
only an HTTP call to USGS. Image processing correctly stays in the satellite
agent. (The satellite classification PNGs it produces are simply not consumed by
the earthquake logic.)

### The premise itself is unsupported: there is NO built-up-area signal
A repo-wide grep finds **no built-up / urban / settlement / building classification
layer** in either agent. The only earthquake-related satellite product is the
`NDVI_QUAKE` **vegetation-loss** overlay, and even that is not read by the
earthquake risk logic. So a "built-up-area false positive" cannot originate from a
built-up layer, because none exists.

### The real, adjacent defect (hazard agent, contract-level)
If a false HIGH earthquake/severity is being observed, the mechanism that actually
exists is in the hazard agent's **invalid-bbox fallback**, not any satellite
signal. When `bbox` is empty/short (a satellite→hazard contract miss),
`run_parallel_analysis` returns [analyzer.py:343-357](agents/hazard/analyzer.py#L343):

```python
    if not bbox or len(bbox) < 4:
        return {
            ...
            "earthquake_risk": "UNKNOWN",
            ...
            "overall_severity": "HIGH",      # <-- hardcoded HIGH on a non-event
            "error": "Invalid bbox received from satellite agent",
        }
```

This is a **hazard-agent logic issue** (a non-event stamped `severity: HIGH`),
triggered by a satellite→hazard **plumbing** miss (empty bbox), and is
**orthogonal to satellite signal calibration**. It is the only path by which the
earthquake/severity output goes falsely high without any real USGS event —
and it is entirely inside the hazard agent.

Note also: `analyze_earthquake` returns `confidence 0.85` for the LOW/no-event
case ([analyzer.py:269](agents/hazard/analyzer.py#L269)) — i.e. it is *confident*
there is no earthquake when USGS is empty. So on the normal path a no-event bbox
yields `earthquake_risk: LOW`, not a false positive. A false earthquake HIGH can
only come from (i) a genuine USGS event in the bbox, or (ii) the invalid-bbox
`UNKNOWN`+`HIGH severity` fallback above — never from a satellite signal.

---

## Bottom Line

| Claim in the question | Reality |
|---|---|
| Satellite produces a bad signal earthquake uses | ✘ Earthquake reads **no** satellite signal |
| Hazard misuses a correct satellite signal | ✘ No satellite signal on the earthquake path |
| Built-up-area layer drives the false positive | ✘ **No built-up layer exists** in either agent |
| Fix belongs in satellite | ✘ Satellite output is disjoint from earthquake risk |
| Fix belongs in hazard | ✔ **The only real defect is here** — the invalid-bbox `severity:"HIGH"` fallback, a hazard-agent contract/logic issue, not a signal-quality issue |

**Whose fault:** If there is a false-positive earthquake/severity output, it is
the **hazard agent's**, via its own deterministic USGS thresholding or (more
likely for a "false positive on a non-event") its **invalid-bbox → hardcoded
`overall_severity: HIGH`** fallback. It is **not** the satellite agent's — the
satellite agent's output, calibrated or not, is never an input to earthquake
risk. Before any fix, confirm which of the two hazard-agent mechanisms is firing
(a real USGS event in-bbox vs. an empty bbox hitting the HIGH fallback) — that
determines whether the fix is in `fetch_usgs`/bbox plumbing or in the fallback's
severity default.

---

# Part 2 — The Other Two Hazards (Flood + Landslide)

To understand the **complete** satellite→hazard dependency gap, here is the same
trace for flood and landslide. The pattern that emerges: **the three hazards have
three completely different data lineages**, and only ONE of them (flood) actually
depends on a satellite signal.

## The dependency matrix (the whole gap in one table)

| Hazard | Satellite signal consumed? | Actual input(s) | Where the risk decision is made | Uncalibrated-SAR (`fix.md`) exposure? |
|---|---|---|---|---|
| **Flood** | **YES** — `mean_value` (NDWI **or SAR**), `water_percent`, `affected_area_km2`, `satellite_type` | satellite index + GDACS count | **LLM** (`analyze_flood`), deterministic fallback | **YES — this is the ONLY hazard that eats the uncalibrated SAR number** |
| **Earthquake** | **NO** | USGS seismicity (hazard fetches it) | deterministic magnitude threshold | none |
| **Landslide** | **NO** | OpenTopoData DEM slope (hazard fetches it) + GDACS | deterministic slope threshold | none |

So of the three, **only the flood path is coupled to the satellite agent's
output at all.** Earthquake and landslide are self-sourced from third-party APIs
the hazard agent calls directly — the satellite result is passed to them
(`bbox` for scoping) but its analytical content is never read.

---

## 4. FLOOD — the one hazard that DOES consume the satellite signal

### 4.1 What it consumes — exact code

[analyzer.py:382](agents/hazard/analyzer.py#L382):

```python
analyze_flood(bbox, affected_area_km2, mean_value, gdacs_data, satellite_type)
```

All three of `affected_area_km2`, `mean_value`, `satellite_type` are **satellite
outputs** (via `_normalise_satellite_payload`, [agent.py:57-79](agents/hazard/agent.py#L57)).
`mean_value` is the satellite's `mean_index` — and per
[processor.py:1066-1086](agents/satellite/processor.py#L1066), that number is:

- **NDWI** `(B03-B08)/(B03+B08)` for the Sentinel-2 flood path, **or**
- **SAR** `10*log10(raw GRD DN)` for the Sentinel-1 flood path — **the exact
  uncalibrated value `fix.md` flags as physically meaningless.**

`analyze_flood` even branches on `satellite_type` to relabel the index
([analyzer.py:180-185](agents/hazard/analyzer.py#L180)):

```python
    if satellite_type == "sentinel-1":
        index_label = "SAR backscatter ratio (VV-VH)"
        index_context = "Values near 0 indicate water. Negative values mean flooding."
    else:
        index_label = "NDWI flood index"
        index_context = "Values above 0.3 indicate flooding. Above 0.5 is severe."
```

### 4.2 How the decision is made — LLM first, deterministic fallback

Unlike earthquake/landslide (pure deterministic), flood **asks an LLM**
([analyzer.py:199](agents/hazard/analyzer.py#L199)) and only falls back to
thresholds if the LLM returns nothing ([analyzer.py:211-220](agents/hazard/analyzer.py#L211)):

```python
    area = _to_float(affected_area_km2)
    flood_index = _to_float(mean_value)
    if area > 200 or flood_index > 0.5:   risk = "CRITICAL"
    elif area > 100 or flood_index > 0.3: risk = "HIGH"
    elif area > 25:                        risk = "MEDIUM"
    else:                                  risk = "LOW"
```

### 4.3 THE REAL SATELLITE-CAUSED BUG LIVES HERE

Note the fallback thresholds: `flood_index > 0.5` / `> 0.3` are **NDWI** cut-offs
(NDWI ∈ [-1, 1]). But when `satellite_type == "sentinel-1"`, `mean_value` is
**SAR dB** — a number like `-12` or `-18`, i.e. always `< 0.3`. So:

- **SAR dB fed into NDWI thresholds → `flood_index > 0.3` is never true** →
  the index contribution collapses; risk is driven by `affected_area_km2` alone.
- And per `fix.md` **BUG B**, the S1 GRD clip collapses to ~0% valid pixels, so
  `affected_area_km2 ≈ 0` too → **the fallback returns LOW even during a real
  flood** (a false *negative*).
- The LLM path is handed the raw uncalibrated SAR dB with a hand-wave context
  string ("Negative values mean flooding") and no calibration — so its judgment
  is built on the same `fix.md`-flagged meaningless number.

**This is genuinely attributable to the satellite agent** (uncalibrated SAR +
BUG B clip collapse, per `fix.md`), AND independently to the hazard agent
(applying NDWI-scaled thresholds to a SAR-dB value — a unit mismatch in
`analyze_flood`'s fallback). It is a **(c) — both** situation, but **only for the
Sentinel-1 flood path**:

- **Satellite fault:** SAR is uncalibrated and (BUG B) the clip yields ~0 area —
  fix belongs in the satellite agent (`fix.md` #1/#2).
- **Hazard fault:** `analyze_flood`'s deterministic fallback uses NDWI thresholds
  on a SAR-dB `mean_value` without checking `satellite_type` — a real unit bug in
  the hazard agent, independent of calibration.

For the **Sentinel-2 (NDWI) flood path**, the satellite signal is well-formed
(`fix.md` says the S2 path is solid), the thresholds match the units, and flood
risk is trustworthy.

---

## 5. LANDSLIDE — no satellite signal, like earthquake

### 5.1 What it consumes — exact code

[analyzer.py:384](agents/hazard/analyzer.py#L384):

```python
analyze_landslide(bbox, gdacs_data, slope_data)
```

Neither `gdacs_data` nor `slope_data` is a satellite output:

- `slope_data` comes from `fetch_slope(bbox)`
  ([analyzer.py:108](agents/hazard/analyzer.py#L108)) — the hazard agent samples a
  **5×5 SRTM 30m DEM grid from OpenTopoData** and computes mean terrain slope
  itself. Not satellite-agent data.
- `gdacs_data` comes from `fetch_gdacs(bbox)` — also fetched by the hazard agent.

### 5.2 How the decision is made — deterministic slope threshold

[analyzer.py:310-318](agents/hazard/analyzer.py#L310):

```python
    slope = _to_float(slope_estimate)
    if slope > 45:   risk = "CRITICAL"
    elif slope > 30: risk = "HIGH"
    elif slope > 15: risk = "MEDIUM"
    else:            risk = "LOW"
```

And GDACS `count` is **deliberately NOT used** for the decision
([analyzer.py:304-309](agents/hazard/analyzer.py#L304)) because the GDACS bbox
filter is unreliable (returns global events). The DEM slope is the only signal.

### 5.3 Verdict — not satellite, not really hazard either

- **No satellite dependency.** Same as earthquake — the satellite result's
  analytical content is never read. `bbox` only scopes the DEM/GDACS queries.
- **No image processing in the hazard agent** — `fetch_slope` is an HTTP call to
  OpenTopoData, not raster work. Architecture is clean.
- The landslide path's correctness hinges entirely on the **DEM slope fetch**. If
  it wanted to be wrong, the failure mode is `fetch_slope` returning the
  `available: False` **conservative default of 10.0°** ([analyzer.py:166](agents/hazard/analyzer.py#L166)),
  which maps to **LOW** — i.e. a DEM failure produces a false *negative*, never a
  false positive. So landslide has the safest failure posture of the three.

---

## Complete Gap — Consolidated Verdict

| Hazard | Satellite-caused defect? | Hazard-caused defect? | Net attribution |
|---|---|---|---|
| **Earthquake** | ✘ none (signal not consumed) | ✔ invalid-bbox → hardcoded `severity:HIGH` fallback | **Hazard only** |
| **Flood (S2/NDWI)** | ✘ (S2 path is sound) | ✘ (thresholds match units) | **Neither — trustworthy** |
| **Flood (S1/SAR)** | ✔ uncalibrated SAR + BUG B clip collapse (`fix.md`) | ✔ NDWI thresholds applied to SAR-dB `mean_value` (unit mismatch) | **(c) BOTH** |
| **Landslide** | ✘ none (signal not consumed) | ✘ (deterministic slope, safe default) | **Neither — trustworthy; DEM-dependent** |

**Key structural insight:** the satellite agent's signal-quality problems
(`fix.md`: uncalibrated SAR, BUG B) can only ever affect the pipeline **through
the flood/Sentinel-1 path** — because that is the *only* place any satellite
analytical value is consumed downstream. Earthquake and landslide are fully
insulated from satellite signal quality by design (they self-source USGS/DEM).

So the "complete gap" is:

1. **Earthquake false HIGH** → hazard agent's invalid-bbox severity fallback (a
   plumbing/logic bug, satellite-independent).
2. **Flood on SAR** → the *real* satellite↔hazard coupling defect: `fix.md`'s
   uncalibrated-SAR + clip-collapse on the satellite side, **compounded** by
   `analyze_flood` feeding SAR dB into NDWI-scaled thresholds on the hazard side.
   Tends toward false *negatives* (missed floods), not false positives.
3. **Flood on NDWI (S2)** and **landslide** → clean, trustworthy, no cross-agent
   signal defect.

None of the three exhibits the "built-up-area" mechanism the original question
posited — no built-up layer exists, and the only genuine satellite→hazard signal
coupling is the flood/SAR one above.
