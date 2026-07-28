# Satellite Validation Harness — Session 2 Report

**Date:** 2026-07-28/29
**Branch:** `science/validation-harness`
**Base commit at session start:** `61db52e` (session 1's harness + baseline)
**Task:** resolve the three questions BASELINE_REPORT.md raised (Paiporta 0/0,
evidence_contradicts, unscoreable S1 path), then widen the baseline.
Mid-session, user-directed scope additions: S1 VV-only download and a live
Islamabad S1 run — which led to root-causing and fixing a silent
pipeline-breaking bug in the S1 warp path.

---

## 1. What actually happened at Paiporta (task item 1)

**Answer: none of (a)/(b)/(c) as framed. The reference is real and non-empty;
the 0/0 came from the PREDICTED side (`complete_zero_zones`), and the deeper
finding is that the EMS reference itself maps almost nothing inside Paiporta's
municipal polygon.**

Polygon counts at every stage (`diag_paiporta.py`, all three EMS vintages):

| Stage | S2 ref (DEL_MONIT02) | S1 ref (DEL_MONIT03) | DEL_MONIT01 (2024-11-03, closest to peak) |
|---|---|---|---|
| 0. raw shapefile rows | 915 | 912 | 938 |
| 1. dissolved (native CRS) | 1 multipolygon, area preserved | same | same |
| 2. reproject to WGS84 | no loss (source already EPSG:4326) | same | same |
| 3. clipped to Paiporta boundary | **non-empty, 0.0508 km²** | **0.0508 km²** | **0.0508 km²** |

- The pipeline AOI (Paiporta's real geoBoundaries polygon) is 3.96 km²; the
  reference intersects only **1.3%** of it.
- (a) ruled out: the clip is non-empty; the `reference_empty_after_clip` guard
  correctly did not fire; harness geometry/CRS handling is sound (§4.5's fix
  class does not recur here).
- (b) ruled out: counts/areas are consistent through every operation.
- (c) ruled out **as stated**: DEL_MONIT01 (~4 days closer to peak) carries the
  *identical* 0.0508 km² at Paiporta — no recession signal across EMS
  monitoring vintages. (Note: MONIT01's sensor is ICEYE, not Sentinel — fine
  for this reference-vs-reference comparison.)
- The real explanation: EMS semi-automatic flood extraction maps open
  terrain, not dense urban fabric. The EMSR773 flood polygons cluster in the
  ravine/field corridor *around* Paiporta — buffering the boundary 0→2 km
  grows the clipped reference 0.0508→0.593 km² — while the town's
  catastrophic street flooding (200+ deaths) is invisible to the product.
  The pipeline (S2, Nov 5, clear sky) also found zero zones; both methods
  agree "no mappable standing water inside the municipal polygon ~1 week
  post-peak".

**Verdict: not a scored result, and not a harness bug — Paiporta is
effectively unscoreable at city scale with this reference.** A scored
Valencia-area result would need either a peri-urban AOI centred on the
ravine corridor or a reference source that maps urban flooding (EMS's does
not). Worth noting for harness semantics: with a non-empty reference and an
empty prediction, recall=0 *is* defined (the harness's `complete_zero_zones`
early-return treats it as fully undefined); with a 1.3%-of-AOI reference the
practical conclusion is the same either way.

---

## 2. Is `evidence_contradicts` firing correctly? (task item 2)

**No. The confidence signal is systematically dragged down on every
low-fraction flood, by two mechanisms — and most real floods are
low-fraction relative to an administrative AOI.**

What the check actually compares (verified by reading the code, not the
logs):

1. **Deterministic index-physics check** (`cross_validator.py` `validate_all`,
   index step): compares `mean_index` — the mean NDWI over **the whole AOI**
   — against fixed thresholds (`>0.3` strong evidence, `>0.1` moderate, else
   weak 0.4 evidence). It never reads the flooded fraction, `water_percent`,
   or the mean over classified-water pixels.
2. **The Featherless "expert opinion" LLM** (`get_featherless_opinion`) sees
   the same whole-AOI `mean_index` in its prompt and independently reasons
   "mean NDWI is characteristic of dry soil… which contradicts the presence
   of water" — the phrase in every baseline run's log is LLM-generated, not
   hardcoded. Its concerns land as MEDIUM (−0.10 each) on the tracker.

The distributional fact the task hypothesised is confirmed and quantified:
with typical dry-land NDWI ≈ −0.3 and open-water NDWI ≈ +0.4, the whole-AOI
mean only crosses 0 when **~43% of the AOI is water**. So:

| Event | flooded fraction (ref / predicted) | whole-AOI mean NDWI | confidence_basis |
|---|---|---|---|
| Kanalia (best run, **F1 0.652**) | ~14% / ~29% | negative | `evidence_contradicts` |
| Insh (S2 run, this session) | ~76% ref / 0.33% predicted | −0.4932 | `evidence_contradicts` |
| Paiporta (both runs) | ~1.3% / 0% | negative | `evidence_contradicts` |

Every run in both sessions fired `evidence_contradicts`, including the one
that scored 0.65 F1 — a negative whole-AOI mean is *expected geometry* for
any partial flood, not a contradiction. The check should compare the mean
NDWI **over the classified water pixels** (which should be >0 if the
classification is physically sound) and treat the whole-AOI mean only as
context, never as counter-evidence against a low-fraction detection.

**Distinct from the wet_soil units bug**: that (islamabad-findings #3,
`_normalise_percent`) was a 0-1-fraction vs 0-100-percent *scaling* error;
this is a *conceptual* error (spatial mean over the wrong support). Both
live in the same neighbourhood and compound: the Insh run's LLM concern
misread `water_percent: 0.33` (i.e. 0.33%) as "33% water coverage" — a live
recurrence of the units-confusion class inside the LLM's own reasoning.

**Direct consequence for the confidence question: with this check in place,
reported confidence cannot track accuracy for low-fraction floods** — the
better the pipeline correctly detects a partial flood, the more the
whole-AOI mean "contradicts" it. Not fixed this session (task said do not
fix yet); flagged as the top candidate for the first science-phase change.

---

## 3. The S1 path: root-caused, fixed, and the scorability question answered
(task item 3 + user-directed scope)

### 3.1 The forced-satellite override (built as specified)

`tests/validation/forced_satellite_override.py` — a `unittest.mock.patch`
context manager over `agent.select_satellite` (patch-where-looked-up;
`agent.py` binds the name at import). Lives entirely in `tests/validation/`,
cannot be activated from any production request path, and stamps
`selection_reason='harness_forced_selection'` (a value the real pipeline can
never produce) into all stored evidence. Everything downstream of selection
runs unmodified. Wired through `run_baseline.py --force-satellite`.

### 3.2 What the forced runs found: a silent, total S1 pipeline failure

Both initial forced-S1 runs (Kanalia, then a live Islamabad run) failed with
**0.000% interior coverage, pure nodata, zero cloud** despite multi-GB
downloads. Systematic elimination:

- Catalogue search: **not at fault** — candidates with 100.0% GeoFootprint
  overlap of the AOI exist and rank first (verified against live CDSE).
- Raw downloaded pixels: **fine** — direct reads of the real VV GeoTIFF show
  uint16 backscatter (max 31593, mean 208, ~3% nodata edges).
- **`_open_georeferenced`'s `WarpedVRT`: the bug.** On the real CDSE file it
  reports a correct CRS/transform/shape but reads **all 616,543,888 pixels
  as exactly 0** — silently. `rasterio.warp.reproject(gcps=…)` on the same
  file produces real data (70% nonzero; zeros are the oblique swath edges).
- Synthetic elimination (why `test_bug1_gcp_raster_resolved` never caught
  it): plain-GTiff GCP fixtures warp fine through the old path at small
  scale, at the real 25807×16724 scale, and with the real scene's exact 210
  GCPs copied verbatim. The failure is specific to the real files' internal
  structure (COG-organised S1 GRD measurement TIFFs, which CDSE increasingly
  serves — including reprocessed archive dates). The 2026-07-26 green e2e's
  S1 run (mean_index 23.6, classic-format SAFE) predates hitting this.

**Fix (committed):** the GCP branch now warps via explicit-GCP
`rasterio.warp.reproject` into a cached on-disk GTiff and returns that
dataset (same contract), with an empty-warp probe guard that *raises* rather
than ever handing an all-zero raster downstream again. Verified on the real
scene: `valid_percent` 0.0 → **99.96%**.

### 3.3 VV-only per-band S1 download (user-directed pipeline change)

`calculate_indices`' SAR path only ever reads VV; VH was downloaded and never
used. `_S1_POLARIZATIONS` is now `["VV"]`, and `_download_bands_via_nodes`
gained an S1 GRD resolver (`_resolve_s1_band_nodes`, SAFE→measurement tree),
closing the CLAUDE.md-documented "no per-band Nodes path for S1" gap.
Live-verified: 3 scenes × 676 MB VV-only = 2.03 GB vs ~3.3–5.1 GB
whole-archive — **~40–60% less S1 bandwidth**. 85/85 offline tests pass
(3 new).

### 3.4 The scorability answer

With the warp fixed, the full Islamabad forced-S1 run completes with real
data: 99.96% valid coverage, `mean_index 22.9954` (dB-uncalibrated),
`water_percent 0.0`, zero zones — and the pipeline's own concerns correctly
state that on uncalibrated SAR this means *indeterminate*, not "no flood".

**The long-standing question — "is the uncalibrated SAR index scorable in
either direction?" — now has a precise answer: no, and structurally so.**
`SAR_WATER_THRESHOLD_DB = −15.0` lives in calibrated-sigma0 space; the
index is `10·log10(raw GRD DN)`, which is virtually always strongly positive
(~+23 here). The threshold can never fire, so the S1 path classifies zero
water *always* — every S1 IoU is a guaranteed `complete_zero_zones`
(undefined 0/0) until SAR calibration (CLAUDE.md H#7) lands. Two sessions of
"cannot score S1" were three *different* causes stacked: cloud-driven
selection (session 1), the silent all-zero warp (masked everything until
this session), and now the calibration gap — which is the only one left,
and is a known, deliberately-deferred science-phase item.

A scored S1-vs-EMS run (Kanalia forced-S1 post-fix) was launched but killed
mid-flight when the user redirected the session's remaining budget to the
Islamabad live test; it would in any case have produced `complete_zero_zones`
per the above. **First *scored* S1 result therefore still pending — but for
the first time the blocker is a single known science change, not an unknown.**

---

## 4. Widened baseline (task item 4)

New scoreable reference event: **EMSR698 Insh (River Spey, Storm Babet,
Oct 2023)** — Sentinel-1 `maximumWaterExtentA` (the newer EMS name for the
same cumulative-max layer role), rescoped from the ~105×87 km corridor AOI
to the village on the densest reference cluster, same grid-search method as
Paiporta/Kanalia. Its buffered AOI intersects **5.32 km² of reference flood
(~76% flooded fraction)** — the deliberate high end of the spread.

Flood-fraction spread now covered by the event set:

| Event | Reference flood fraction of AOI | Best scored result |
|---|---|---|
| Paiporta (EMSR773) | ~1.3% | unscoreable (see §1) |
| Kanalia (EMSR692) | ~14% | IoU 0.4837 / F1 0.652 (S2, session 1) |
| Insh (EMSR698) | ~76% | `complete_zero_zones` (S2, 13.3-day-old scene, conf 0.263) |

**The 5-non-degenerately-scored-events target was not reached** — stated
plainly, not papered over. Scan evidence: every flood activation in
EMSR500–850 reachable via the dashboard-api was checked
(657/659/662/668/680/692/698/710/720/750/770/775/796/800/850 inspected in
detail); only EMSR698 adds a Sentinel-backed maximum-extent product. Recent
EMS activations overwhelmingly use commercial VHR (Pleiades/ICEYE/
COSMO-SkyMed/PAZ) with `observedEventA`/`modelledEventA` only. The scored-n
bottleneck is now (a) the S1 calibration gap (§3.4) blocking every S1-backed
reference, and (b) historical-date S2 scene quality (Insh's only S2 scene in
window was 13.3 days stale). Permanent-water masks: still not sourced this
session; the including/excluding split remains an unmeasured caveat, most
material for Kanalia (Lake Karla) and Insh (Insh Marshes wetland).

---

## 5. Confidence vs accuracy, updated n

| Run | Confidence | Basis | Scored accuracy |
|---|---|---|---|
| Paiporta S2 ×2 (s.1) | 0.3184 | evidence_contradicts | undefined (0 zones) |
| Kanalia S2 (s.1) | 0.4479 | evidence_contradicts | IoU 0.4837 |
| Insh S2 (s.2) | 0.263 | evidence_contradicts | undefined (0 zones) |
| Islamabad S1 (s.2, post-fix, unscored) | 0.3141 | evidence_contradicts | n/a (no reference) |

Still exactly **one** non-degenerate accuracy point → the correlation
question remains unanswerable. What n=5 runs *does* now establish:
`evidence_contradicts` fired on **100% of runs** regardless of outcome
quality — §2's whole-AOI-mean defect makes the basis field uninformative in
its current form, which is itself the load-bearing finding: **fixing §2 is a
prerequisite for the confidence-tracks-accuracy question to ever be
answerable.**

---

## 6. Download cost (task item 5's running tally)

| Run | Downloaded |
|---|---|
| Insh S2 | 408 MB |
| Kanalia forced-S1 #1 (whole-archive; stalled at 1.10 GB, killed) | ~1.10 GB |
| Kanalia forced-S1 #2 (whole-archive, pre-VV-only; 0% coverage → warp bug) | 2.89 GB |
| Islamabad forced-S1 #1 (VV-only, pre-warp-fix; 0% coverage) | 2.03 GB |
| Warp-bug trace (1 VV scene, reused for all repro work) | 0.68 GB |
| Kanalia forced-S1 #3 (post-fix; killed mid-flight on user redirect) | ~0.7 GB (partial) |
| Islamabad forced-S1 #2 (post-fix; 99.96% coverage, real data) | ~2.0 GB (est.) |
| **Session 2 total** | **~9.8 GB** |

(Session 1: 1.24 GB. The pre-fix S1 spend was not wasted in hindsight — it
is what surfaced and isolated a bug that silently zeroed every S1 scene the
pipeline would ever have processed from CDSE's COG-format files.)

---

## 7. Commit map (one per item)

- `9cb4a48` Paiporta diagnostic (item 1)
- `fc3ae7b` forced-satellite override (item 3a)
- `a5be689` EMSR698 Insh reference event + first Insh result (item 4)
- `df99697` S1 VV-only per-band Nodes download (user-directed)
- `8123969` explicit-GCP reproject fix for the silent all-zero S1 warp
- (this file) session report — items 2, 3.4, 5, 6 are analysis/report-only

**Follow-ups queued for the science phase, in priority order:**
1. Fix the §2 index-physics check (mean over classified pixels, not AOI).
2. SAR calibration (H#7) — now the *sole* blocker to scored S1 results.
3. Re-run Kanalia + Insh forced-S1 post-calibration for the first scored S1.
4. Source permanent-water masks (Karla, Insh Marshes) to make the
   including/excluding split real.
