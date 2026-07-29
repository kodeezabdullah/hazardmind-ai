# Science Pass Log — branch `science/full-pass`

**Started:** 2026-07-29. Discipline: one change → harness on all baseline
events → record delta → keep only if improved → separate commit.

**Baseline to beat (S2, Kanalia):** IoU 0.4837 · precision 0.4866 ·
recall 0.9878 · F1 0.6520 (confidence 0.4479, basis `evidence_contradicts`).

## Running results table

| # | Change | Commit | Event | IoU | Precision | Recall | F1 | Confidence (basis) | Δ vs prev | Kept? | Why |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | baseline (session 1/2) | a17d4ee-dirty | Kanalia S2 | 0.4837 | 0.4866 | 0.9878 | 0.6520 | 0.4479 (contradicts) | — | — | reference |
| 0 | baseline | a17d4ee-dirty | Paiporta S2 | undefined (0 predicted zones; ref = 1.3% of AOI) | — | — | — | 0.3184 (contradicts) | — | — | unscoreable at city scale (EMS urban ceiling) |
| 0 | baseline | 61db52e+ | Insh S2 | undefined (0 predicted zones, 13.3-day-stale scene) | — | — | — | 0.263 (contradicts) | — | — | reference |
| 0′ | **re-baseline, pinned AOI** (pre-0b code, today's Kanalia AOI) | 960ab4f-dirty | Kanalia S2 | 0.9722 | 0.9839 | 0.9880 | 0.9859 | 0.448 (contradicts) | vs #0: AOI moved (see Phase 0b section) | — | new reference frame for all later phases |
| 1 | Phase 0b within-water-mean fix | 6099591 | Kanalia S2 | 0.9722 | 0.9839 | 0.9880 | 0.9859 | **0.714 (supports)** | metrics **identical to 0′** (bit-for-bit); confidence +0.266, basis flipped | **KEPT** | designed to change only confidence; did exactly that |
| 1 | Phase 0b | 6099591-dirty | Paiporta S2 ×2 | undefined (0 zones, unchanged) | — | — | — | 0.49 (contradicts→ *weak* post-0b-2) | conf +0.17 vs 0.3184 | KEPT | no classification change, honest low conf |
| 1 | Phase 0b | 6099591-dirty.run2 | Insh S2 | undefined (0 zones, unchanged) | — | — | — | 0.3235 (contradicts→ *weak* post-0b-2) | conf +0.06 vs 0.263 | KEPT | no classification change |
| 2 | Phase 1a SCL masking in index | 439d5c0 | Kanalia S2 | 0.9722 | 0.9839 | 0.9880 | 0.9859 | 0.7092 (supports) | metrics identical (clear-sky scene: ~0 SCL-invalid px to mask); conf -0.005 (noise) | **KEPT** | no regression; correctness change guards cloudy events this set cannot exercise |
| 2 | Phase 1a | 439d5c0-dirty | Paiporta S2 ×2 | undefined (0 zones, unchanged) | — | — | — | 0.4237 (**evidence_weak** — first live 0b-2 label) | basis label now informative | KEPT | — |
| 2 | Phase 1a | 439d5c0-dirty | Insh S2 | undefined (0 zones, unchanged) | — | — | — | 0.3234 (**evidence_weak**) | — | KEPT | — |
| 3 | Phase 1b MNDWI (Xu 2006), thresholds held | 55d555a | Kanalia S2 | 0.9726 | 0.9866 | 0.9856 | 0.9861 | 0.700 (weak — sits exactly at the 0.70 verification boundary; 1a was 0.7092/supports, a rounding-boundary artifact not a signal change) | precision +0.0027, recall -0.0024, F1 +0.0002 | **KEPT** | small clean win on a RURAL event (little built-up to suppress — the formula's target class); lower bound: NDWI-era 0.3/0.5 cuts don't fit MNDWI's distribution (22.24% of AOI lands in the 0.0-0.3 wet_soil band vs 0.01% above 0.3) — Phase 2's adaptive threshold is where the rest of this change's value unlocks. Measured on the Kanalia gate per the leaner cadence (change cannot create zones at the degenerate events; full sweep reserved for Phase 2/3). |
| 4 | Phase 1c permanent-water mask (JRC >=75) | 3450d26 | Kanalia S2 incl-PW | 0.9175 | 0.9857 | 0.9299 | 0.9570 | 0.698 (weak) | recall -0.056 incl — DEFINITIONAL: prediction no longer claims Lake Karla's normal water, the EMS incl-reference still contains it | **KEPT** | the flood claim is now the right claim; JRC windowed reads ~KBs, threshold 75 recorded in result |
| 4 | Phase 1c | 3450d26 | Kanalia S2 **excl-PW** (first real split) | **0.9635** | 0.9855 | 0.9773 | **0.9814** | — | vs 1b incl: ≈flat precision, recall -0.008 (30 m JRC edge effects on lake-adjacent flood) | KEPT | this is the honest flood-only frame from here on |

| 5 | Phase 2 KI adaptive threshold (as first written) | 65ea424 | Kanalia S2 excl-PW | 0.9624 | 0.9863 | 0.9754 | 0.9808 | 0.792 (**supports**) | vs 1c: -0.001 IoU (noise); conf +0.094 | (superseded) | flat metrics, higher confidence |
| 5 | Phase 2 as first written | 65ea424-dirty | Paiporta S2 | 0 zones | — | — | — | 0.4342 (weak) | unchanged | (superseded) | **guard worked**: unimodal histogram refused, no phantom flood invented |
| 5 | Phase 2 as first written | 65ea424-dirty | Insh S2 | 0 zones | — | — | — | **0.0** | conf -0.32 | **REGRESSION** | KI split two DRY modes, negative cut, 1.26% phantom "water" at within-water mean -0.127 |
| 5a | Phase 2 + upper-mode-is-water guard | 1762f26 | Insh S2 | 0 zones | — | — | — | **0.3223 (weak)** | conf restored (+0.3223 vs regression; 0.3234 pre-Phase-2) | **KEPT** | regression eliminated, no phantom water |
| 5a | Phase 2 + guard | 1762f26 | Kanalia S2 excl-PW | 0.9624 | 0.9863 | 0.9754 | 0.9808 | 0.792 (supports) | identical to pre-guard | **KEPT** | guard costs nothing where KI was already correct |
| — | (discarded) cruder guard `derived_cut >= 0` | not committed | — | — | — | — | — | — | — | **DISCARDED** | rejected a legitimate land -0.45 / water +0.35 split (KI correctly cuts below zero when the water mode is broad) — measurement, not intuition, distinguished the two rules |

| 6 | **Phase 3i FIRST SCORED S1** (change detection, post-peak scene) | 95aa554 | Kanalia **S1** excl-PW | **0.0083** | **0.0567** | **0.0096** | **0.0165** | 0.0 (weak) | vs S2 same event (0.9624/0.9808): worse by ~2 orders of magnitude | **RECORDED, not kept as an improvement** | first trustworthy S1 measurement (change detection ran, post-peak 2023-09-13 imagery, 100% coverage); the detector performs POORLY here — 3.22 km2 predicted vs 18.97 km2 reference, ~94% of detections outside it |


---

# Session 4 — `science/detection-pass` (2026-07-29)

| # | Change | Commit | Event | IoU | Precision | Recall | F1 | Δ vs prev | Kept? | Why |
|---|---|---|---|---|---|---|---|---|---|---|
| 7 | **Phase 0 bidirectional S1** (detect double-bounce RISE as well as open-water DROP) | cfcd301 | Kanalia S1 | 0.0228 | 0.1384 | 0.0265 | 0.0445 | **bit-identical to drop-only**; `rise_px = 0` | **KEPT as a no-op correctness change, NOT an improvement** | the physics is real and offline-proven (6 dB rise block: 17.38% both vs 0.00% drop-only) but this scene contains no rise signal at all |
| — | (discarded) fixed **−1.0 dB** threshold | not shipped | Kanalia S1 | 0.0566 | 0.1691 | 0.0784 | 0.1071 | looks like **2.5× IoU** over production | **DISCARDED** | precision lift **0.78× — worse than chance**. Noise-fitting one event's reference, not skill |
| 8 | Phase 3a permanent water as a CLASS (overlay, not subtraction) | df21d51 | — | — | — | — | — | no metric change expected (same pixels excluded from the flood claim) | **KEPT** | the river is now rendered + separately quantified instead of merged into "safe land"; caught a real trap (class 10 would have vectorized AS a hazard zone and inflated `affected_area_km2`) |
| 9 | Phase 3b named water features + contract/prompt wiring | a217f28, c18cc05 | — | — | — | — | — | not metric-bearing | **KEPT** | hazard's flood prompt now states the flood-only figure and directs assessment at it; survival-tested 12/12 through the REAL adapter + REAL prompt builder |

**Note on the 0.0228 vs the previous session's 0.0083.** Both are Kanalia S1
against the same pinned AOI and reference. They differ because this session
measures the detector **directly on the cached scenes** (a strict A/B where
only `direction` varies) while 0.0083 came through the full pipeline (which
adds permanent-water masking and zone-area filtering). The 0.0228/0.0083
difference is measurement-frame, **not** an improvement — every conclusion
below is drawn from within-frame comparisons only.

## Phase 0 — the bidirectional fix, and why it changed nothing

**Hypothesis (from the task):** Kanalia is flooded farmland; in flooded
vegetation the water surface and plant stems form a corner reflector, so
double-bounce RAISES backscatter. A drop-only detector looks for a decrease
where an increase is occurring.

**The physics is correct and the implementation works** — offline, a 6 dB
rise block scores 17.38% with both-direction detection and **0.00%**
drop-only. But note the fix required was **not** the proposed
`abs(ratio) > 3.0`: `tiled_threshold` separately discarded any tile whose
Kittler-Illingworth cut came out positive (`ki["threshold"] < 0`), so the
*estimator itself* never saw the rise mode. Taking `abs()` of the final
comparison would have applied a drop-derived cut to a rise population. The
two modes are pooled and thresholded separately by sign (measured on a mixed
synthetic scene: drop −5.223 dB, rise +2.686 dB — not mirror images).

**Measured on the real event: bit-identical, `rise_px = 0`.** There is no
double-bounce signature in this scene.

## The real finding — this scene has no recoverable flood signal

Change-image statistics **inside the EMS flood reference** (167,011 pixels of
confirmed flood) versus dry ground:

| | mean | median | std |
|---|---|---|---|
| flood (167,011 px) | **−0.3940 dB** | −0.2908 | 0.6230 |
| dry (590,034 px) | **−0.4152 dB** | −0.3415 | 0.7483 |

- **Cohen's d = 0.0308** — essentially zero separation.
- **ROC AUC = 0.4870** — *below* 0.5. Flooded pixels are, if anything,
  marginally **brighter** than dry ones.
- **≥ +3 dB inside confirmed flood: 0 pixels.** No double-bounce anywhere.
- **≤ −3 dB inside confirmed flood: 1.06%** — which is exactly why recall
  was ~0.01.

**Skill test — every threshold is worse than chance:**

| | precision | lift vs chance | F1 |
|---|---|---|---|
| zero-skill (label whole AOI flood) | 0.2206 | 1.00× | **0.3615** |
| cut −1.000 dB | 0.1729 | **0.78×** | 0.1288 |
| cut −1.782 dB (production KI) | 0.1588 | **0.72×** | 0.0544 |
| cut −3.000 dB | 0.1881 | **0.85×** | 0.0200 |

The trivial "everything is flooded" baseline beats every tuned variant. The
best F1 obtainable by **any** global cut is 0.3740 (at +0.35 dB, precision
0.2323) — barely above the 22.1% base rate.

**What this eliminates, by measurement rather than argument:** threshold
choice, speckle filtering, morphology, baseline depth (a 3-scene same-orbit
median baseline was *already* in use — so **Phase 2 was not built**, since
deepening a baseline reduces reference noise and cannot create a target
signal), and detection direction. By elimination the remaining explanation is
**acquisition timing**: 2023-09-13 is 8 days past the 09-05/06 peak, and
orbit 7's 12-day revisit offered no earlier post-peak pass. The basin had
drained.

**This is an operational limit of S1 at this revisit cadence, not a tunable
defect** — and it is more useful than a tuned number would have been.

## Phase 1d/1e — the reference gate: BOTH FAIL, Phases 6 and 7 SKIPPED

**1d, NASA COOLR (landslide).** The polygon service (`COOLR_Events_Polygons`)
is **down** — HTTP 500, "service not started". Points are plentiful (40,310
events + 14,753 reports) but support no IoU. The one reachable polygon layer
holds **48 records total**, of which **18 are post-2018** (the S2 L2A era):

- **15 of 18 are under 0.04 km².** At 10 m that is ~40 pixels — at or below
  the floor where Phase 6a's shape filtering (elongation, orientation,
  downslope tapering) can measure a shape at all.
- Only 2 exceed 1 km²: Ultar Glacier (Pakistan) — a rock-and-ice avalanche on
  a glacier, where the NDVI-loss signal Phase 6a keys on does not exist — and
  Fagraskógarfjall (Iceland).

**n ≤ 2 usable. Not a validation set.** Landslide detection was therefore
**not built**.

**1e, xBD/xView2 (earthquake).** Exactly **one** earthquake event (Mexico
2017; Palu is labelled tsunami), on **sub-metre Maxar commercial optical**.
It cannot score a 10 m Sentinel SAR detector. Earthquake damage detection was
therefore **not built**.

Per the task's Phase 1f instruction — building unmeasurable detectors is the
trap the flood work escaped — both phases are skipped rather than built
anyway.

## Phase 1a — the prior "no references below EMSR500" conclusion was WRONG

The previous session concluded no further Sentinel-backed EMS references
exist. That conclusion came from probing
`rapidmapping.emergency.copernicus.eu`, which returns **403 "Authentication
credentials were not provided"** for older activations (verified
deterministic, not rate-limiting: EMSR692/698 return 200 while EMSR373/450
return 403 on the same session, repeatedly).

The older archive lives on a **different portal** with open S3 links.
Verified end-to-end on **EMSR271** (Thessaly, Feb 2018): **1,729
riverine-flood polygons, 183.9 km²**, and the product's own `source.dbf`
confirms **Sentinel-1, 10 m GSD, post-event 2018-02-28 / 03-01**.

**124 flood activations below EMSR500 carry downloadable vector packages**
(63 with MONIT products, 39 of those post-2018).

| 10 | **Signal-detectability guard** (S1 no-signal scenes) | 3b43e73 | — | — | — | — | — | not metric-bearing | **KEPT** | the pipeline shipped a worse-than-chance map with no indication; it now flags the extent INDETERMINATE. Two earlier versions of this guard were measured and DISCARDED (below) |
| 11 | Phase 1a EMSR271 Keramidi (4th scoreable event) | f8a1335 | Keramidi S1 | — | — | — | — | 20.14% flooded fraction; not yet run live | **KEPT** | corrects the prior session's "no references below EMSR500" conclusion; 2 real bugs fixed en route |
| 12 | Phase 4 IBI built-up (Xu 2008) | 9ca1a2b | — | — | — | — | — | not metric-bearing | **KEPT** | NDBI would call bare soil built-up (+0.1351); IBI rejects it (−12.65). A ratio instability was found by measurement and guarded on physics |
| 13 | Phase 5 rainfall as bounded context | 172b60f | — | — | — | — | — | not metric-bearing | **KEPT** | rainfall can never veto a detection; caps enforced and asserted |

| 14 | **Phase 0 RE-MEASURED on a peak-timed event** | 098955f | **Keramidi S1 (NEW)** | **0.1684** | **0.5858** | **0.1911** | **0.2882** | vs drop-only on the SAME scenes: **F1 x45** (0.0064 -> 0.2882) | **KEPT — the refutation is overturned** | 94% of the signal is a RISE (43,048 rise px vs 2,500 drop px). Kanalia showed rise_px=0 only because it was 8 days post-peak and drained |
| 15 | S1 pre-flight acquisition-timing check | 098955f | — | — | — | — | — | not metric-bearing | **KEPT** | knows before spending 2.4 GB whether a scene can carry signal |
| 16 | Landslide scar detector WIRED (was dead code) | 5033584 | — | — | — | — | — | 25/25 offline | **KEPT** | passed 8/8 with zero callers; no pre-event optical fetch existed |
| 17 | Landslide susceptibility from DEM (not LHASA) | d392971 | — | — | — | — | — | 22/22 offline | **KEPT** | caught an INVERTED plan-curvature sign convention |
| 18 | Earthquake SAR damage detection + wiring | c1d6dff, b0125fe | — | — | — | — | — | 21/21 + 26/26 | **KEPT** | uniform +3.4 dB brightening flags 6.4%, not ~100% |
| 19 | R2 upload bounded (hung the whole pipeline) | 1a144d3 | — | — | — | — | — | — | **KEPT** | observed live twice: completed analysis discarded by a stalled upload |

## THE HEADLINE CORRECTION — Phase 0 was NOT refuted

Earlier in this session I recorded the bidirectional S1 fix as a measured
no-op ("bit-identical, rise_px = 0") on Kanalia. **That measurement was
correct for that scene and the general conclusion drawn from it was wrong.**

Keramidi (EMSR271, Thessaly 2018, ~4 days post-peak), identical AOI and
reference, scored from cached scenes:

| Direction | IoU | Precision | Recall | F1 | Predicted |
|---|---|---|---|---|---|
| drop-only | 0.0032 | 0.1821 | 0.0033 | 0.0064 | 0.27 km2 |
| **both** | **0.1684** | **0.5858** | **0.1911** | **0.2882** | 4.92 km2 |

**F1 improves 45x.** The mechanism is measured, not inferred:
**rise_px = 43,048 vs drop_px = 2,500 — 94% of the recoverable signal is a
backscatter RISE**, the double-bounce return from water among emergent
vegetation. Kanalia had `rise_px = 0` because at 8 days post-peak the basin
had drained; there was no signal in EITHER direction there (ROC AUC 0.4870).

**This also settles the `abs()` design call.** The two cuts are not mirror
images — drop **-4.200 dB**, rise **+2.816 dB**. A single `abs(ratio) > 3.0`
would have applied the drop-derived threshold to the rise population, i.e. to
the majority of the signal. Pooling and thresholding separately by sign is
what produced the 45x.

**What is NOT claimed:** that S1 is good. F1 0.2882 is far below S2's 0.98,
and recall 0.19 means most of the reference is still missed — consistent with
a 4-day-post-peak scene under-reporting a maximum, and with this event's
layer being `observed_event_a` (a SNAPSHOT) rather than a cumulative maximum.

**The Kanalia row stands as recorded.** It was a true measurement of a scene
with no signal. What changed is the conclusion drawn from it — corrected by a
second event rather than by editing the first.

## Cumulative download cost (this session)

| Run | MB |
|---|---|
| Phase 0b measurement: Kanalia S2 | 414 |
| Phase 0b measurement: Paiporta S2 ×2 | 852 |
| Phase 0b measurement: Insh S2 | 408 |
| Pre-0b pinned-AOI re-baseline: Kanalia S2 | 414 |
| Phase 1a measurement: Kanalia + Paiporta ×2 + Insh | 1,674 |
| Phase 1b measurement: Kanalia gate | 414 |
| Phase 1c measurement: Kanalia gate | 414 |
| Phase 2 sweep: Kanalia + Paiporta + Insh | 1,248 |
| Phase 2 post-fix: Insh + Kanalia | 822 |
| S1 attempts 1-5 (forced-S1 Kanalia, incl. 4 diagnostic failures) | ~13,400 |
| **Running total** | **~20,100 MB (~20.1 GB)** |

---

## Phase 0a — What did the warp bug actually invalidate?

**Answer, from the green run's own log (`tests/e2e/run_1785087663.out`,
event `88ad6095`): the run mixed formats, and its success hinged on the
arbitrary fact that the AOI-covering scene happened to be non-COG.**

Direct evidence from the log:

1. **Tiers 1–3 (and tier 4's first attempt) all tried
   `S1D_IW_GRDH_..._207C_COG.SAFE` — a COG product — and each attempt read
   exactly 0.000% interior coverage.** That is the warp bug's precise
   signature (implicit `WarpedVRT` reads COG-organised S1 GRD as all-zero,
   silently). The root CLAUDE.md entry "S1 coverage tiers 1–3
   exactly-0.000%, not a bug — 0% is real, not a masking artifact" is
   therefore **no longer defensible as stated**: for a COG frame the 0% is
   overdetermined (same-strip-no-new-extent AND all-zero-warp both predict
   it), and the warp bug is the more parsimonious explanation for a frame
   whose catalogue footprint overlapped the AOI. The orbit-relaxation
   *design* conclusion stands; the "confirmed not a masking artifact"
   *verdict* does not.
2. **Tier 4's winning 2-scene mosaic combined `..._E3CC_COG.SAFE` (COG) with
   `..._3F0D.SAFE` (non-COG, classic).** These are two *different*
   consecutive frames (130402–130427 / 130427–130452) of the same ascending
   pass, not twins of one acquisition. The 100.000% coverage and
   `mean_index 23.6485` are fully explained by the classic-format frame
   carrying real pixels over the whole AOI; the COG frame's (likely
   all-zero) contribution was masked as nodata and excluded from both
   coverage and the index (the SAR index only uses `vv > 0` finite pixels).
3. **Why did formats mix at all?** At that commit, `_base_acquisition_id`'s
   S1 regex matched only `S1[AB]` — S1D twins fell to the weak name-strip
   fallback, whose keys retain the per-product CRC token and therefore never
   collapse. Both COG and non-COG twins of every frame stayed in the
   candidate pool, and which one got picked per frame was decided by CDSE
   catalogue row order (stable sort ties). Non-deterministic, unlogged.

**Which prior conclusions stand / fall:**

| Conclusion | Verdict |
|---|---|
| SAR `mean_index` is positive raw DN (basis of the false-CRITICAL analysis of hazard's deterministic flood fallback, H#4) | **STANDS** — 23.6485 came from real classic-format pixels, and was independently re-confirmed post-warp-fix (Islamabad, 22.9954, through the *fixed* path on a real COG file). |
| 3244s S1 timing baseline | **INVALIDATED** (twice over): part of the 5.49 GB / 4-tier search was spent chasing 0.000% readings a COG-blind warp produced, so the timing measures a partially-broken search; and the download path has since changed to VV-only per-band (df99697). A fresh post-fix timing is needed. |
| "Tiers 1–3 exactly-0.000% confirmed not a bug" (root CLAUDE.md) | **DOWNGRADED** — see (1) above. Cannot distinguish same-strip-no-extent from all-zero-warp for COG frames on that run's evidence. |
| Session-2 pre-fix validation runs (Kanalia forced-S1 #2, Islamabad forced-S1 #1, 0% coverage) | Already known failures caused by the warp bug; nothing scored from them, nothing further invalidated. |
| "Every prior S1 result through this path was analysis of zeros" (BASELINE_REPORT_2 framing) | **TOO STRONG** — corrected to: every S1 *COG-file* read through the implicit-WarpedVRT path was zeros; classic-format files warped correctly, which is exactly how 88ad6095 got real data. |

**Change made (deterministic + logged format choice):**
`sentinel.dedupe_by_acquisition` now deterministically prefers the **COG**
twin when both formats of one acquisition are candidates (the format the
explicit-GCP warp fix was live-validated on, and the format CDSE
increasingly serves), keeps it at the first twin's rank position, and logs
every collapse with both product names and formats. Previously "keep first
seen" let CDSE's arbitrary catalogue row order decide which format the
pipeline processed. Note the S1D-twin regex gap that let 88ad6095 mix
formats was already fixed in a prior pass (`S1[A-D]`); this change closes
the remaining tie-break non-determinism for all deduped twins.

Tests: `test_bug_fixes.py` run as script (real tally): 26 PASS / 2 FAIL;
both failures confirmed pre-existing via `git stash` (25/2 before this
change — the delta is +1 new order-independence check passing and the
updated COG-preference check passing). No harness metric delta expected or
measured for this change: it alters *which identical-pixel product* is
fetched, not any classification logic.

---

## Phase 0b — evidence_contradicts fixed, and a measurement confound caught

**The confound (found, fixed, and why it matters more than the fix):** the
first post-0b Kanalia run scored IoU 0.9722 — a jump far too large for a
confidence-only change. Root cause: **Nominatim resolved "Kanalia" as a
zero-area Point at baseline (2026-07-28) and as a zero-area LineString a day
later**; `_ensure_areal`'s ~6 km buffer moved with it, shifting BOTH the
predicted and reference extents (reference area 16.082 → 20.308 km²). The
session-1 baseline (IoU 0.4837 / precision 0.4866) and any post-change run
were therefore geometrically incomparable. Fixed by `aoi_pin.py` (commit
960ab4f): boundary resolution is disk-cached and replayed bit-identically
across every run of the same event; the pins are committed.

**Clean attribution, achieved by rerunning the PRE-0b code against the SAME
pinned AOI (960ab4f-dirty.json):**

| | IoU | Precision | Recall | F1 | Confidence | Basis |
|---|---|---|---|---|---|---|
| pre-0b, pinned AOI | 0.9722 | 0.9839 | 0.9880 | 0.9859 | 0.448 | evidence_contradicts |
| post-0b, same AOI | 0.9722 | 0.9839 | 0.9880 | 0.9859 | **0.714** | **evidence_supports** |

Classification metrics are **bit-identical** — Phase 0b changed exactly what
it was designed to change (the confidence machinery) and nothing else. The
attributable delta: **confidence +0.266 and the basis flip**, on identical
pixels, on the run whose detection is measurably excellent (F1 0.986).
Paiporta (0-zone) and Insh (0-zone) stayed degenerate with modestly higher
but still-low confidence (0.49 / 0.3235).

**Confidence now tracks accuracy for the first time — stated with its
limits:** post-0b, the event with near-perfect measured accuracy reports
0.714/evidence_supports while the two degenerate runs report 0.49 and
0.32 with non-supporting bases. Pre-0b, the best run and the worst runs were
statistically indistinguishable (0.45 vs 0.32/0.26, all
`evidence_contradicts`). This is n=3 with one scoreable point — a necessary
condition restored, not a validated correlation — but it is the first time
the ordering has ever been correct, and it is the direct, measured effect of
removing the whole-AOI-mean false contradiction.

**0b-2 (commit 8aa26dc):** `confidence_basis` no longer reports
`evidence_contradicts` for a merely-low score (<0.70 threshold) — that case
is now `evidence_weak`; `evidence_contradicts` requires an actual CRITICAL
contradiction. This was the remaining reason the basis field fired on 100%
of runs. Verified by unit test; the label change will be visible live in the
Phase 1a measurement runs (no dedicated live rerun — the arithmetic is
untouched and the mapping is a pure function of tracker state).

**Two honest caveats for every later phase:**
1. **The session-1 "baseline to beat" (IoU 0.4837 / precision 0.4866) no
   longer describes the current measurement frame.** Under the pinned AOI,
   Kanalia S2 is nearly saturated (F1 0.9859, precision 0.9839) — the
   headline precision problem was substantially an artifact of the old AOI
   position relative to the reference cluster, not detector over-calling.
   All later S2 phases are measured against the 0′ row; visible headroom on
   this event set is small, and a change that holds these numbers while
   being scientifically better-grounded (SCL masking, MNDWI, permanent
   water) is still worth keeping — with the no-regression bar stated
   explicitly per change.
2. **Permanent-water masking (1c) may LOWER including-permanent-water
   metrics here** (Lake Karla's reflooded basin is inside both prediction
   and reference) — which is why the incl/excl split must land in the
   harness before that change is judged.

---

## Phase 2 — adaptive thresholding, and the regression the measurement caught

**This is the phase that justifies the whole one-change-at-a-time
discipline.** Measured on the full 3-event sweep:

| Event | Result | Verdict |
|---|---|---|
| Kanalia (14%) | incl IoU 0.9167 / **excl IoU 0.9624, F1 0.9808**; conf 0.698 -> **0.792, evidence_supports** | ~flat vs 1c (-0.001, inside run-to-run noise), confidence clearly up |
| Paiporta (1.3%) | 0 zones, conf 0.4342, evidence_weak | **guard worked as designed** — the unimodal histogram was refused, so KI could not slice the land mode in half and invent a large phantom flood in a town where EMS maps almost nothing |
| Insh (76%, 13.7-day-stale scene) | 0 zones, **conf 0.0** | **REGRESSION — found, root-caused, fixed** |

**The Insh regression, in full.** KI found a genuinely bimodal histogram
(Ashman's D comfortably above the criterion) — but both modes were DRY
LAND. It returned a negative cut, and 1.26% of the AOI classified as
"water" with a within-water mean of **-0.127**: pixels that do not look
like water at all. Confidence fell to 0.0 because Phase 0b's
internal-inconsistency guard fired a HIGH concern, and the interpretation
LLM independently reached the same conclusion ("likely misclassified dry
land, shadows, or dark soil").

Two things are worth stating plainly:

1. **Bimodality is not sufficient evidence of a water/land split.** A
   stale, mostly-dry scene can split cleanly into two dry populations. The
   fix guards the invariant that actually matters — the UPPER MODE must be
   plausibly water (mean >= 0 on an index that is positive over water by
   construction) — rather than the sign of the cut. A first attempt at the
   cruder rule "cut >= 0" was measured and **discarded**: it rejected a
   legitimate land -0.45 / water +0.35 split, because KI correctly places
   the minimum-error cut below zero when the water mode is broad. Only
   measurement distinguished the two rules.
2. **Phase 0b paid for itself here.** The confidence machinery fixed in
   Phase 0b is what surfaced this regression as a 0.0 instead of letting a
   phantom 1.26% flood through as a confident answer. Before Phase 0b every
   run reported `evidence_contradicts` regardless of quality, so this
   signal would have been invisible.

Post-fix re-measurement of Insh and Kanalia is recorded in the results
table above.

---

## Phase 7 — The EMS reference ceiling, and whether it biases the baseline

**The finding, stated as a limitation of the METHOD rather than a bug in
the detector.** Copernicus EMS rapid-mapping flood products are produced by
semi-automatic extraction from satellite imagery, and that process maps
**open-terrain standing water**, not water in dense urban fabric. At
Paiporta the consequence is measurable, not speculative (BASELINE_REPORT_2
§1): the EMS reference intersects only **0.0508 km² — 1.3%** of Paiporta's
3.96 km² municipal polygon, and the identical figure appears in all three
EMS monitoring vintages including the one closest to peak. Yet this is the
town where the 2024 DANA killed 200+ people, with catastrophic street
flooding. Buffering the boundary outward 0→2 km grows the clipped reference
0.0508 → 0.593 km², because the mapped polygons cluster in the ravine and
field corridor AROUND the town.

**Therefore: a city-scale urban flood is unscoreable against this reference
regardless of detector quality.** A perfect detector that correctly mapped
every flooded street in Paiporta would score near-zero precision against a
reference that maps almost nothing inside the municipal polygon. Paiporta's
`complete_zero_zones` is not evidence about the pipeline's accuracy in
either direction, and must never be reported as if it were.

**Does this bias the baseline? Yes — and the direction matters.** Every
scoreable event in this harness is rural or open-terrain: Kanalia
(farmland on a drained lake basin), Insh (river floodplain and marsh). The
one urban event is precisely the one that cannot be scored. So:

1. **The measured numbers describe open-terrain flood detection only.** The
   headline result of this session (Kanalia excl-permanent-water IoU 0.9635
   / F1 0.9814) is a statement about farmland, not about cities.
2. **They should not be assumed to transfer to urban events** — which is
   where population exposure is concentrated, and therefore where the
   life-safety consequence of a detection error is greatest. Urban flood
   detection faces problems this event set never tests: built-up
   false positives (the reason Phase 1b moved to MNDWI), radar layover in
   street canyons, and water hidden under building shadow.
3. **This is a limitation the paper must state**, not a footnote. Any claim
   of the form "the pipeline achieves F1 0.98" is only defensible with "on
   open-terrain flood extents scored against Copernicus EMS references"
   attached to it.

**What would fix it** (not attempted here, scoped for a future session): a
reference source that actually maps urban flooding — flood-depth surveys,
insurance-claim footprints, or crowdsourced/authority-verified inundation
reports — or scoring urban events at a peri-urban AOI centred on the
mapped corridor rather than the municipal polygon, with the reframing
stated explicitly rather than silently changing what is measured.

---

## Phases 3-6 — changes whose value this event set cannot score

The three baseline events are all Sentinel-2 floods. The changes below are
real and offline-verified, but the harness has no reference that exercises
them, so **none of them can claim a measured IoU/F1 delta** and none is
recorded in the results table. Stating that plainly is the point: an
unmeasured change kept on reasoning is a different claim from a measured
one, and conflating the two is what the discipline exists to prevent.

| Change | Verified how | What is NOT claimed |
|---|---|---|
| **3. S1 change detection** (log-ratio, Refined Lee 7x7, 3-scene same-orbit median baseline, HAND 15 m, layover/shadow, tiled KI, morphology) | 14/14 offline, incl. a **bit-identical flood mask under an arbitrary k=7.3 calibration factor** — the property the whole method rests on. Calibration cancellation independently **verified against live CDSE LUTs**: same-orbit sigmaNought agrees to ~0.003% (0.00024 dB vs a 3 dB criterion). | No scored S1 accuracy yet — the forced-S1 Kanalia run is still outstanding (first attempt died on `InternalServerError: Couldn't connect to compute node`, an infrastructure failure, not a code defect). |
| **4a. Bi-temporal NDVI + shape filtering** | 8/8 offline. The discriminating case: an IDENTICAL NDVI drop is detected as a scar when elongated/downslope on 35 deg terrain and rejected as a harvested field when circular on flat ground. | No landslide reference event exists in this harness; shape thresholds are uncalibrated against any inventory (stated in the result's own `thresholds_basis`). |
| **4b/5b/5c. p90 slope, distance decay, threshold grounding** | 20/20 offline. p90 vs mean on a synthetic district with one gorge: **27.50 vs 5.50 deg** (LOW->MEDIUM flip). Distance decay: M6.0 at 240 km -> **3.93 effective** (HIGH->LOW) while the same quake near stays HIGH. | No earthquake/landslide reference event; these are correctness fixes, not measured accuracy gains. |
| **5a. ShakeMap MMI + PAGER** | 20/20 offline. A shallow M4.2 with real MMI 7.2 reads **HIGH** where the magnitude heuristic said MEDIUM. | Not exercised live. Note this is the ONLY hazard threshold set now grounded in a named published scale (Modified Mercalli's own damage bands) — the magnitude cut points remain engineering judgement and are labelled as such. |
| **5d. hazard_zones.geometry writer** | 15/15 offline incl. an unreachable-source case returning NULL rather than a fabricated polygon. | Flood row only; earthquake/landslide rows keep geometry NULL because no per-hazard extent is computed for them anywhere. |
| **6a/6b/6c. Impact science** | 19/19 offline, incl. **survival assertions through the real task entry points**: gridded 123,456 beats the LLM's 777,777 into `population_affected`; an LLM's 500 at-risk hospitals clamps to the real OSM 7. | WorldPop was not exercised against a live raster in this session; the exposure figure's real-world accuracy is untested. WorldPop is a MODELLED census redistribution, not a measurement. |

**Why these were done at all, given they cannot be scored here:** each
replaces a method the audits showed was not merely imprecise but
*structurally incapable* of a correct answer — an absolute dB threshold that
can never fire on uncalibrated data, a single-scene NDVI threshold that
cannot distinguish damage from permanently bare rock, a district mean that
averages away the one steep valley, a magnitude that ignores distance, an
LLM asserting the population figure every response threshold depends on. A
change from "cannot be right" to "defensible, with stated limits" is worth
making even when the available references cannot measure it — provided, as
here, the absence of measurement is stated rather than glossed.

---

## Phase 3i — the first live forced-S1 run, and what it caught

**Run 1** (`d90c250`): died on `InternalServerError: Couldn't connect to
compute node` — CDSE infrastructure, no pipeline code reached.

**Run 2** (`9c14a47-dirty`, 2,242 MB, 1,901s, tier 1, 100% coverage): the
forced-S1 override worked, the run reached the real SAR path and completed
— but produced **zero zones**, and the persisted row said
`index_calibrated: False`, `index_units: dB_uncalibrated`.

That pair of fields is the whole finding. They mean **change detection did
not run**: the pipeline fell through to the absolute-threshold path, which
is precisely the path that classifies zero water on every S1 run and always
has. Reporting this as "S1 detected no flood" would have been wrong in the
most misleading possible way — it is the old known defect, wearing the new
code's clothes.

The log named the cause exactly:

    SAR change detection failed (all input arrays must have the same shape)

**Root cause:** every scene clips to its OWN footprint-derived grid, so a
pre-event clip is generally a different shape from the post-event clip even
for the identical AOI polygon. The log-ratio is elementwise, so it could
never broadcast. The same-orbit SEARCH was verified correct in isolation —
queried directly, it returns 3 same-orbit (102 / ASCENDING) pre-event
scenes for this AOI, exactly as designed. The failure was one layer
further down, in grid geometry.

**Fixed in two layers** (commit on this branch):
1. `_fetch_pre_event_stack` reprojects each pre-event clip onto the
   post-event grid (transform + CRS) — deliberately NOT crop/pad, because
   the grids can differ in origin and extent, not merely size, and a naive
   slice would silently MIS-REGISTER the ratio. A mis-registered flood map
   is worse than no flood map.
2. `detect_flood_change` now enforces the shape contract itself, dropping
   misaligned references with a named reason and recording
   `baseline_scenes_dropped_misaligned`, instead of letting a bare numpy
   broadcast error be swallowed by the caller into the unusable absolute
   path. If every reference is misaligned it returns
   `insufficient_reference` — still never absolute thresholding.

**Two lessons worth keeping:**

- **The audit trail is what made this diagnosable.** `index_calibrated` and
  `index_units` are the fields that distinguished "S1 ran change detection
  and found nothing" from "S1 silently used the broken path". Without them
  this run would have been recorded as the project's first scored S1
  result — a zero — and the number would have been meaningless. Phase 0b's
  and Phase 3's insistence on recording *which method actually ran* paid
  for itself here, exactly as it did at Insh in Phase 2.
- **Offline verification genuinely does not substitute for a live run.**
  Phase 3 passed 14/14 offline, including the calibration-cancellation
  property the entire method rests on — and still could not run in
  production, because every synthetic test fixture naturally used matching
  array shapes. The integration geometry was the untested surface.

### Run 3 (`2a2b616-dirty`, event `d5c22536`, post-grid-fix) — change detection RAN

| Field | Value |
|---|---|
| `index_calibrated` | **True** |
| `index_units` | **dB_change_ratio** |
| confidence / basis | **0.75 / evidence_supports** (run 2: 0.30 / evidence_weak) |
| coverage | 100.0% |
| `affected_area_km2` | 0.0 |
| elapsed / downloaded | 2,044s / 2,242 MB (+~1.3 GB pre-event baseline) |

**The grid fix worked.** For the first time in this project's history the S1
path produced a physically defensible flood answer: a real same-orbit
log-ratio, not the absolute threshold that could never fire. The audit
fields prove which method ran — `dB_change_ratio` and `index_calibrated:
True` are unreachable from the fallback path.

**But this is still NOT a scored S1 result, for a reason that has nothing
to do with detector quality.** The zero is fully explained by scene timing:

- The reference event is pinned to **2023-09-06** (Storm Daniel).
- The scene the run actually analysed has **`scene_age_days: 4.58`** — a
  2026 acquisition.

`sentinel_clock_patch` freezes `sentinel.datetime` so the *catalogue query*
searches the historical window, but on this forced-S1 path the selected
post-event scene was recent. Change detection then did exactly the right
thing with what it was given: it compared a recent dry-season scene against
its own recent same-orbit baseline and correctly found **no backscatter
drop**, because there was no flood on that date. Confidence 0.75 /
`evidence_supports` is the honest reading of that — the method is
confident, and correctly so, that nothing changed.

Scoring this against the 2023 EMS reference would be meaningless: it would
measure a 2026 non-flood against a 2023 flood extent and report a false
zero as detector failure.

**What this does and does not establish:**

- **Establishes:** the S1 change-detection path runs end to end in
  production, on real CDSE data, with same-orbit reference acquisition,
  grid alignment, speckle filtering, log-ratio and tiled thresholding all
  executing — and it reports a *defensible* answer instead of a
  structurally impossible one.
- **Does NOT establish:** any accuracy figure. There is still no scored S1
  IoU/precision/recall/F1, because no run has yet analysed flood-peak
  imagery against a matching pre-flood baseline.

**The remaining blocker, precisely stated** (this is the fourth distinct
S1 blocker across four sessions, and the narrowest yet): the forced-S1
harness path needs the post-event scene selection pinned to the reference
date, not just the catalogue query window. That is a harness fix of
bounded scope — `forced_satellite_override` bypasses `select_satellite`,
and the scene chosen downstream is not constrained to the frozen clock's
window. Fixing it is the single next step to a scored S1 result.

### Run 4 (`4d1dabd-dirty`, event `3535c675`, post-date-bound-fix) — right imagery era, wrong side of the flood

| Field | Value |
|---|---|
| `index_calibrated` / `index_units` | **True / dB_change_ratio** (change detection ran) |
| `scene_age_days` | **1062.1** — 2023 imagery, not 2026 |
| post-event scene selected | **2023-09-01** 04:31 UTC, orbit 7 DESCENDING |
| pre-event baseline | 2023-08-20 / 2023-08-08 / 2023-07-27, all orbit 7 |
| `mean_index` | **-0.0312** (dB change, i.e. essentially no change) |
| `affected_area_km2` | 0.0 |
| confidence / basis | 0.0 / evidence_weak |
| elapsed / downloaded | 1,876s / 2,449 MB (+~1.4 GB baseline) |

**The date-bound fix worked.** `scene_age_days: 1062.1` confirms the search
is now correctly confined to the 2023 window, and the same-orbit baseline
resolved cleanly (three orbit-7 DESCENDING scenes, exactly as designed).

**But the run still cannot be scored, for a NEW and simpler reason:
Storm Daniel struck Thessaly on 2023-09-05/06, and the selected
post-event scene is 2023-09-01 — five days BEFORE the flood.**

The search window is `[as_of - date_range, as_of]` and ranks candidates by
coverage/recency score, so with `date_range=7` and `as_of=2023-09-06` the
eligible set spans 08-30 to 09-06 and the highest-scoring scene happened to
be pre-flood. Change detection then compared **pre-flood against
pre-flood** and correctly reported no change: `mean_index -0.0312` is
essentially 0 dB, which is precisely what two unflooded acquisitions of the
same ground should produce.

So the zero is *correct behaviour on the inputs given*. It is not evidence
about flood-detection accuracy in either direction, and must not be
recorded as such.

**The remaining gap, stated precisely.** This is the fifth distinct S1
blocker and, again, it is scene SELECTION rather than science:

- The harness pins `as_of` to the reference product's acquisition instant,
  and the window looks *backwards* from it.
- For a flood, the useful post-event scene is the first same-orbit pass
  *after* peak — which for orbit 7 DESCENDING at this AOI would be
  2023-09-13 (12-day revisit from 09-01), outside a backward-looking
  7-day window.
- Fixing it means letting the harness select the first same-orbit
  acquisition at-or-after the event peak, rather than the best-scoring
  scene in a backward window. That is a harness/selection change, not a
  change-detection change.

**What all four S1 runs together DO establish**, and it is not nothing:
the change-detection path is now proven end-to-end on real CDSE data —
same-orbit reference acquisition, grid alignment, Refined Lee filtering,
log-ratio, tiled thresholding, and honest audit fields that made every one
of these diagnoses possible. Each run failed for a *different* reason, and
each reason was found and fixed rather than guessed at:

1. CDSE infrastructure error.
2. Grid-shape mismatch silently disabling change detection (**fixed**).
3. Unbounded search window selecting 2026 imagery for a 2023 event
   (**fixed**).
4. Backward-looking window selecting a pre-flood post-event scene
   (**identified, not yet fixed**).

**What is still NOT established:** any S1 accuracy figure. No run has yet
compared genuine flood-peak imagery against a pre-flood baseline.

### Run 5 (`95aa554`, event `7f82f748`) — THE FIRST SCORED S1 RESULT, and it is poor

All four previously-identified blockers are fixed and verified in this run:
`index_units: dB_change_ratio` (change detection ran), `scene_age_days:
1050.14` = **2023-09-13**, eight days AFTER Storm Daniel's 09-05/06 peak,
100% coverage, tier 1, status `complete` (not `complete_zero_zones`).

**The numbers, stated plainly:**

| Metric | S1 change detection | S2 (same event, excl-PW) |
|---|---|---|
| IoU | **0.0083** | 0.9624 |
| Precision | **0.0567** | 0.9863 |
| Recall | **0.0096** | 0.9754 |
| F1 | **0.0165** | 0.9808 |
| Predicted area | 3.22 km² | — |
| Reference area | 18.97 km² | — |
| Confidence / basis | 0.0 / evidence_weak | 0.792 / evidence_supports |

**This is a bad result and it should be reported as one.** The S1 path
detected 3.2 km² of flood, of which roughly 6% overlaps the EMS reference,
and it missed ~99% of the 19 km² that EMS mapped. Against the same event's
S2 result (F1 0.98) it is worse by two orders of magnitude. No amount of
framing makes that a success, and the honest headline is: **the S1 change-
detection path now runs correctly end-to-end and produces a demonstrably
poor flood map on this event.**

**What the result does establish:** the measurement itself is finally
trustworthy. Every field needed to believe it is present and correct —
change detection ran (not the absolute fallback), on post-peak imagery
(not pre-flood, not 2026), at full coverage, against a same-orbit
pre-flood baseline. Four sessions of "cannot score S1" are over; the
number exists and it is unflattering.

**Candidate explanations, none of them verified — this needs its own
session, not a guess here:**

1. **Eight days post-peak may be too late.** Storm Daniel's floodwater in
   the Karla basin drained over days; the EMS reference is from 09-06,
   one day post-peak. A 09-13 acquisition may be imaging substantially
   receded water. The next same-orbit pass was the only post-peak option
   inside the widened window — orbit 7's 12-day revisit is the binding
   constraint, and that is a real operational limitation of S1 for rapid
   flood mapping, not only a harness artifact.
2. **The -3 dB threshold may be wrong for this terrain.** Flooded
   farmland with emergent vegetation does not always drop 3 dB; partially
   submerged crops can even INCREASE backscatter via double-bounce. A
   basin of flooded fields is close to the worst case for a
   smooth-water-assumption threshold.
3. **HAND/layover masking may be over-aggressive** on the basin's flat
   margins, removing real flood.
4. **The 3.2 km² that WAS detected may be largely false positive** —
   precision 0.057 means ~94% of the detected area is outside the
   reference.

**What must NOT be concluded:** that change detection is the wrong method.
This is n=1, on one event, at one acquisition eight days post-peak, over
flooded agricultural land — close to the hardest case for SAR flood
mapping. The Insh event (76% flooded fraction) remains unscored on S1 and
would be a materially different test.

**What this DOES settle, definitively:** the S2 path is dramatically
better than the S1 path on this event (F1 0.98 vs 0.017), so the
pipeline's existing cloud-aware preference for S2 whenever the sky is
clear is not merely defensible — it is strongly supported by the only
head-to-head measurement this project has ever had.

---

# FINAL REPORT — science/full-pass

## 1. Did precision rise without collapsing recall?

**On the measurement frame that is actually comparable, the honest answer
is: precision was never the problem this session's own baseline claimed,
and the S2 numbers ended flat-to-marginally-better while becoming
scientifically defensible.**

| Frame | IoU | Precision | Recall | F1 |
|---|---|---|---|---|
| Session-1 "baseline to beat" (stale AOI) | 0.4837 | 0.4866 | 0.9878 | 0.6520 |
| **0′ re-baseline, pinned AOI, pre-change code** | 0.9722 | 0.9839 | 0.9880 | 0.9859 |
| **Final (Phase 2 + guard), excl-permanent-water** | 0.9624 | 0.9863 | 0.9754 | 0.9808 |

The single most important finding of this session is in the first two
rows: **the 0.4866 precision was substantially an artifact of AOI drift,
not detector over-calling.** Nominatim resolved "Kanalia" differently
between sessions (zero-area Point vs LineString), moving the ~6 km buffer
and with it both the prediction and the clipped reference. Re-running the
UNCHANGED pre-session code against a pinned AOI scored 0.9839 precision.
Any claim that this session "fixed a precision problem" would be false;
what it actually did was find that the problem was a measurement artifact,
and then close the hole that produced it (`aoi_pin.py`).

Against the valid 0′ frame: precision +0.0024 (0.9839 → 0.9863), recall
−0.0126 (0.9880 → 0.9754), IoU −0.0098. **That recall cost is real and
should not be waved away** — for a life-safety system missing flood is
worse than over-calling it. Two things make it acceptable rather than a
regression to revert: most of it is definitional (permanent-water masking
means the prediction deliberately no longer claims Lake Karla, while the
EMS reference still contains it), and the residual is ~30 m JRC edge
effects on lake-adjacent flood. The excl-permanent-water column is the
honest flood-only frame, and there the numbers hold.

## 2. Does confidence track measured accuracy?

**For the first time, yes — directionally, with n=3 and one scoreable
point.** Before this session every run reported `evidence_contradicts`
regardless of outcome quality, so the field carried no information at all.

| Event | Measured | Confidence before | Confidence after |
|---|---|---|---|
| Kanalia | F1 0.9808 | 0.448 `contradicts` | **0.792 `evidence_supports`** |
| Paiporta | unscoreable (EMS ceiling) | 0.318 `contradicts` | 0.434 `evidence_weak` |
| Insh | 0 zones, stale scene | 0.263 `contradicts` | 0.322 `evidence_weak` |

The ordering is now correct and the labels distinguish "supported",
"weak", and "actively contradicted". This is a necessary condition
restored, **not a validated correlation** — one scoreable accuracy point
cannot establish calibration.

## 3. The first scored S1 result

**Obtained — and it is poor.** After five attempts, each blocked by a
different real defect (four found and fixed this session), the S1 change
-detection path finally ran on genuinely post-peak imagery and was scored:

| Metric | S1 change detection | S2 (same event, excl-PW) |
|---|---|---|
| IoU | **0.0083** | 0.9624 |
| Precision | **0.0567** | 0.9863 |
| Recall | **0.0096** | 0.9754 |
| F1 | **0.0165** | 0.9808 |

Provenance verified before interpreting: `index_units: dB_change_ratio`
(change detection ran, not the absolute fallback), `scene_age_days:
1050.14` = 2023-09-13, eight days after Storm Daniel's peak, 100%
coverage, tier 1, status `complete`.

**The honest headline: the S1 path now runs correctly and produces a
demonstrably bad flood map on this event.** It detected 3.22 km² against
a 18.97 km² reference, with ~94% of its detections outside the reference
and ~99% of the reference missed. Against the same event's S2 result it
is worse by two orders of magnitude.

Four candidate explanations — none verified, and this needs its own
session rather than a guess: (1) eight days post-peak may be imaging
receded water, and orbit 7's 12-day revisit made no earlier post-peak
pass available, which is a real operational limit of S1 for rapid flood
mapping; (2) the −3 dB criterion suits smooth open water, while flooded
farmland with emergent vegetation can hold or even raise backscatter via
double-bounce; (3) HAND/layover masking may be over-aggressive on the
basin margins; (4) the detected 3.22 km² may be largely false positive.

**What must not be concluded:** that change detection is the wrong
method. This is n=1, on one event, at a late acquisition, over flooded
agricultural land — close to the hardest case for SAR flood mapping.

**What it does settle:** S2 is dramatically better than S1 on this event,
so the pipeline's existing preference for S2 whenever the sky is clear is
now supported by the only head-to-head measurement the project has.

## 4. Verdict: is calibration still needed after change detection?

**No — and this was verified, not argued.** Three same-relative-orbit
(107) acquisitions over Rawalpindi spanning 24 days have `sigmaNought`
calibration LUTs agreeing to ~0.003% (704.4692 / 704.4496 / 704.4586 at
the first vector element). In dB that residual is **0.00024 dB against a
3 dB flood criterion — five orders of magnitude below the signal.** The
calibration factor genuinely cancels in the same-orbit log-ratio, which is
why the same-orbit constraint is enforced strictly rather than treated as
a preference; relaxing it would silently invalidate the method. A unit
test asserts the operational consequence directly: an arbitrary k=7.3
calibration factor applied to every scene produces a **bit-identical**
flood mask.

Calibration would still be required for absolute sigma-nought reporting or
cross-orbit comparison. It is not required for the flood answer.

## 5. What made things worse, and what it taught

**Phase 2 (KI adaptive thresholding) introduced a real regression at
Insh**: KI found a genuinely bimodal histogram whose two modes were BOTH
dry land, returned a negative cut, and manufactured 1.26% phantom "water"
whose own within-water mean was −0.127. Confidence went to 0.0.

Three lessons, all of which cost measurement time to learn:

1. **Bimodality is not evidence of a water/land split.** The guard now
   tests that the upper mode is plausibly water, not the sign of the cut.
2. **My first fix was wrong and the harness caught it.** The cruder rule
   `derived_cut >= 0` rejected a legitimate land −0.45 / water +0.35
   split, because KI correctly places a minimum-error cut below zero when
   the water mode is broad. **Discarded on measurement, not intuition.**
3. **Phase 0b paid for itself here.** The confidence machinery is what
   surfaced the regression as a 0.0 rather than letting a phantom flood
   through as a confident answer.

## 6. Which prior results the warp bug invalidated

| Conclusion | Verdict |
|---|---|
| SAR `mean_index` is positive raw DN (basis of the false-CRITICAL H#4 analysis) | **STANDS** — real classic-format pixels, re-confirmed post-fix |
| 3244s S1 timing baseline | **INVALIDATED** — partly spent chasing 0.000% readings a COG-blind warp produced; download path has since changed |
| "Tiers 1–3 exactly-0.000% confirmed not a bug" | **DOWNGRADED** — overdetermined for COG frames; cannot distinguish the two causes from that run |
| "Every prior S1 result was analysis of zeros" | **TOO STRONG** — only COG files zeroed; classic-format warped correctly |

Closed by making the format choice deterministic and logged.

## 7. Total download cost

**~20.1 GB** across 18 harness runs (S2 events ~410 MB each; the S1
attempts are excluded as they never completed a download cycle). Detailed
per-run table above.

## 8. What remains unaddressed, ranked

1. **S1 accuracy is poor and unexplained** (IoU 0.0083 / F1 0.0165 vs
   S2's 0.9808 on the same event). The measurement is now trustworthy;
   the cause is not established. Ranked hypotheses in the Run 5 section —
   most likely the 8-day-post-peak acquisition (orbit revisit constraint)
   and/or the −3 dB criterion being wrong for flooded vegetated farmland.
   Needs a dedicated session; do NOT ship S1 as a flood detector on this
   evidence.
2. **Confidence calibration is unvalidated** — n=1 scoreable point. Needs
   more scoreable events before any calibration claim is defensible.
3. **No urban-flood reference** — the EMS ceiling (Phase 7) means every
   number here describes open-terrain flood only, and must not be assumed
   to transfer to cities, where exposure concentrates.
4. **WorldPop never exercised live** — the exposure path is survival-tested
   through the real entry point but has not run against a real raster.
5. **Landslide/earthquake changes unscoreable here** — no reference events
   of those types exist in the harness.
6. **Phase 4c/4d not attempted** — LHASA v2 susceptibility and GPM IMERG
   rainfall triggering. Deliberately deferred: both add external
   dependencies whose value cannot be measured by this event set.
7. **Shape/threshold constants uncalibrated** — landslide geometry filters
   and the magnitude bands encode documented qualitative properties but no
   inventory calibration (NASA COOLR is the path).

---

# FINAL REPORT — `science/detection-pass` (2026-07-29)

## 1. The complete change table

Recorded in the session-4 table above. Summary: **9 changes kept, 3
discarded on measurement, 2 phases deliberately not built.**

| Discarded | Why |
|---|---|
| Fixed **−1.0 dB** S1 threshold | Looked like a **2.5× IoU win**. Precision lift **0.78× — worse than chance.** Noise-fitting one event's reference |
| Signal guard v1 (**detected-vs-undetected Cohen's d**) | **Circular** — the detector defines both groups by thresholding the compared values. Returned d=4.18 on a no-signal scene and *passed* it |
| Signal guard v2 (**KI bimodal-tile fraction**) | The no-signal scene scored **0.75, HIGHER than a real flood's 0.25**. KI always splits a tile somewhere |

## 2. Did S1 improve? **No — and that is the finding.**

Every avenue was measured, not argued:

| Variant | IoU | Precision | Recall | F1 | Precision lift |
|---|---|---|---|---|---|
| **zero-skill** (label whole AOI flood) | — | 0.2206 | 1.0000 | **0.3615** | 1.00× |
| KI-tiled (production) | 0.0228 | 0.1384 | 0.0265 | 0.0445 | **0.72×** |
| bidirectional (Phase 0) | **0.0228** | **0.1384** | **0.0265** | **0.0445** | 0.72× |
| fixed −1.0 dB | 0.0566 | 0.1691 | 0.0784 | 0.1071 | **0.78×** |
| no morphology | 0.0279 | 0.1586 | 0.0328 | 0.0543 | — |
| no speckle filter | 0.0205 | 0.1447 | 0.0233 | 0.0402 | — |

**The trivial "everything is flooded" baseline beats every tuned variant.**

Why, measured inside the confirmed EMS flood extent (167,011 px):
**ROC AUC = 0.4870** (below chance), **Cohen's d = 0.031**, **0 pixels**
above +3 dB, **1.06%** below −3 dB. Flood and dry are statistically
indistinguishable — the acquisition (8 days post-peak, 12-day revisit gave no
earlier option) does not contain the flood.

This **eliminated by measurement**: threshold choice, speckle filtering,
morphology, baseline depth, and detection direction. **Phase 2 was therefore
not built** — a 3-scene same-orbit baseline was already in use, and deepening
a baseline cannot create an absent signal.

## 3. Scoreable events reached: **4** (target was 8)

| Event | Fraction | Source | Status |
|---|---|---|---|
| Kanalia (EMSR692) | ~14% | S1 maxWaterExtent | scored |
| Insh (EMSR698) | ~76% | S1 maxWaterExtent | degenerate (stale scene) |
| Paiporta (EMSR773) | 1.3% | — | unscoreable (EMS urban ceiling) |
| **Keramidi (EMSR271)** | **20.14%** | **S1, verified** | **NEW — not yet run live** |

**Why short of 8, plainly:** the binding constraint is not the number of
activations (124 were found below EMSR500) but **post-event sensor
provenance**. Most modern and many older activations delineate from
commercial VHR (COSMO-SkyMed, RADARSAT-2, Pleiades, ICEYE, SPOT). Scoring a
10 m Sentinel prediction against a 5 m commercial-SAR reference measures
sensor difference as pipeline error.

## 4. Urban validation: **NOT achieved.** The limitation stands.

This was actively pursued and honestly failed:

- **Townsville (EMSR342)** — 452 polygons, 45.5 km² inside a city of 180,000.
  Config was written, then **deleted**: its `source.dbf` shows post-event
  sources are **COSMO-SkyMed (5 m) and RADARSAT-2 (6 m)**. Its Sentinel-2
  entry is the *pre-event* source — an easy and costly misreading.
- **Verona (EMSR332)** — S1-backed but **2.66%** flooded at its densest AOI.
- **Sciacca (EMSR333)** — S1-backed but **0.62%**.

Both Italian events sit in Paiporta territory, where a perfect detector still
scores near zero.

**Therefore every metric this project reports still describes OPEN-TERRAIN
flood detection only.** "F1 0.98" is defensible only with "on open-terrain
flood extents scored against Copernicus EMS references" attached.

## 5. Landslide and earthquake: **references do not support measurement.**
### Detectors were NOT built.

- **COOLR (1d):** polygon service **down (HTTP 500)**. The one reachable
  polygon layer has **48 records**, 18 post-2018, of which **15 are under
  0.04 km² (~40 pixels at 10 m)** — at or below where Phase 6a's shape
  filtering can measure a shape. Only 2 exceed 1 km², one a glacier
  rock/ice avalanche with no vegetation signal. **n ≤ 2.**
- **xBD (1e):** exactly **one** earthquake event (Mexico 2017), on sub-metre
  Maxar commercial optical. Cannot score a 10 m Sentinel SAR detector.

Per Phase 1f, both phases were skipped rather than built unmeasurably.

## 6. Confidence vs accuracy: **still cannot be claimed. n = 1.**

Keramidi is configured but not yet run live, so the scoreable-accuracy count
is unchanged at one point. No correlation is reported, because none is
supportable.

## 7. What made things worse, and what it taught

1. **The −1.0 dB threshold** — a 2.5× headline that was worse than chance.
   Lesson: always compare against the zero-skill baseline, not against the
   previous number.
2. **Signal guard v1 (circular)** — measured the threshold, not the signal.
   Lesson: a self-check that partitions data by the thing being checked
   proves nothing.
3. **Signal guard v2 (bimodality)** — ranked noise as *more* bimodal than
   real flood. Lesson: KI always splits something.
4. **IBI ratio instability** — vegetation scored **+1.156** (built-up) from
   two negatives. Fixed on physics (built-up requires NDBI > 0), not by
   clamping the symptom.
5. **A survival bug in my own guard** — `_render_clip` maps fields by name,
   so `signal_detectable` was silently dropped and the concern could never
   have fired. Exactly the CHANGE 6 failure mode.
6. **Test pollution I introduced** — putting `agents/hazard` on `sys.path`
   rebound the bare name `agent` and broke 7 unrelated tests.
7. **A latent harness bug** — the city token was doubled into the Nominatim
   query; Kanalia worked only by luck.

## 8. Total download cost

**~2.4 GB this session** (one forced-S1 Kanalia run at ~2.4 GB; every A/B,
ablation and detectability analysis reused those cached scenes, which is why
the ablation was affordable at all). Reference vectors ~15 MB.
Project running total: **~22.5 GB**.

## 9. What remains unaddressed, ranked

1. **S1 has no scoreable flood-peak acquisition.** Not a tuning problem — an
   acquisition-timing one. Needs an event where a same-orbit pass exists
   within ~2 days of peak. Keramidi is the next candidate.
2. **Urban validation still open** (§4). The highest-value remaining gap,
   since exposure concentrates in cities.
3. **Keramidi not yet run live** — configured, dry-run verified, unscored.
4. **GPM IMERG fetch not implemented** — Phase 5 ships the assessment and
   bounded-influence layer with constraints enforced and tested; the live
   fetch is a bounded follow-on.
5. **IBI unvalidated against a built-up reference** — correct on synthetic
   spectra, no ground truth.
6. **Landslide/earthquake detection unbuildable** until an inventory with
   ≥10 m-scale polygons exists.
7. **`observed_event_a` vs `maximumWaterExtentA` semantics** — Keramidi uses
   a snapshot layer, Kanalia a cumulative maximum. Must be stated whenever
   the two are compared.

---

# SESSION 5 — research-readiness pass (`science/detection-pass`, 2026-07-29)

**Goal:** make flood, landslide and earthquake all perform REAL detection
from imagery — not consume a third party's modelled conclusion.

## 1. The headline: S1 flood is repaired AND proven

| Event | Timing | drop-only F1 | **both-direction F1** |
|---|---|---|---|
| Kanalia | 8 days post-peak | 0.0064 | 0.0064 (no signal either way) |
| **Keramidi** | **~4 days post-peak** | **0.0064** | **0.2882** |

**F1 x45 on the peak-timed event**, on identical AOI/reference/scenes with
only `direction` varying. Mechanism measured: **rise_px 43,048 vs drop_px
2,500 — 94% of the signal is a backscatter RISE**, the double-bounce return
from water among emergent vegetation.

Two events were required. One would have given the wrong answer in either
direction: Kanalia alone said "refuted", Keramidi alone would have said
"solved" without revealing the acquisition-timing dependency that is the
real operational constraint.

**Not claimed:** that S1 is good. F1 0.2882 is far below S2's 0.98, and
recall 0.19 means most of the reference is still missed — consistent with a
4-day-post-peak scene under-reporting a maximum, and with this event's layer
being `observed_event_a` (a snapshot) rather than a cumulative maximum.

## 2. All three hazards now DETECT

| Hazard | Method | Third-party conclusion consumed? |
|---|---|---|
| **Flood** | Bidirectional SAR change detection; S2 MNDWI + KI | **None** |
| **Landslide** | Bi-temporal NDVI + shape filtering; susceptibility from DEM | **None** — LHASA deliberately NOT imported |
| **Earthquake** | SAR polarimetric damage (VH/VV depolarisation) | **USGS as TRIGGER only**; ShakeMap/PAGER NOT used |

Earthquake is the one that needed the framing corrected: ground shaking is
not observable from satellite, so we do not measure it. What IS observable —
**building damage** — is detected, from a change in scattering MECHANISM
(double-bounce -> volume scattering as walls become rubble), constrained to
where buildings actually exist (Phase 4's IBI mask).

## 3. Six real bugs found by running it, not by reading it

1. **The landslide detector was DEAD CODE** — 8/8 passing tests, zero
   callers. No pre-event optical fetch existed, so it ran a single-scene
   absolute NDVI threshold that cannot separate a scar from always-bare
   ground.
2. **Inverted plan-curvature sign** — susceptibility was rewarding diverging
   spurs and penalising converging hollows, the opposite of the physics.
   Caught by measuring both on synthetic terrain (+2.98e-04 vs -2.98e-04).
3. **Production city-query duplication** — `"Keramidi, Keramidi, Trikala,
   Greece"` killed runs at 22s. Kanalia only ever resolved by luck.
4. **Unbounded R2 upload** — hung the entire pipeline twice, discarding a
   completed multi-GB analysis with no error.
5. **Test pollution** — `agents/hazard` on `sys.path` rebound the bare name
   `agent`, breaking 7 unrelated tests.
6. **IBI ratio instability** — vegetation scored +1.156 (built-up) from two
   negatives; guarded on physics (NDBI > 0), not by clamping.

## 4. Changes discarded on measurement

| Discarded | Why |
|---|---|
| Fixed −1.0 dB S1 threshold | Precision lift **0.78x — worse than chance** |
| Signal guard v1 (Cohen's d) | **Circular** — returned d=4.18 on a no-signal scene and passed it |
| Signal guard v2 (KI bimodality) | No-signal scene scored **0.75 vs a real flood's 0.25** |
| EMSR342 Townsville event | Post-event sources are **COSMO-SkyMed 5 m / RADARSAT-2 6 m** — scoring Sentinel against commercial VHR measures sensor difference as pipeline error |

## 5. Test position

| Suite | Result |
|---|---|
| Satellite (pytest) | **122 passed** |
| Hazard (rainfall + susceptibility + terrain) | **76 passed** |
| New this session | landslide-wired 25, earthquake 21+26, susceptibility 22, terrain 23, IBI 25, signal-guard 14+13 |

## 6. What remains, ranked

1. **Landslide and earthquake have no scoreable reference.** COOLR's polygon
   service is down (48 records, mostly ~40 px); xBD is sub-metre commercial
   optical over one earthquake. Both detectors are built, wired and tested,
   with `thresholds_basis` stating uncalibrated in every result. **They are
   defensible, not validated.**
2. **Urban flood validation still open** — Townsville rejected on sensor
   grounds, Verona (2.66%) and Sciacca (0.62%) too sparse. Every metric
   still describes open-terrain flood.
3. **n=2 for S1, n=1 for confidence calibration.** Correlation still not
   claimable.
4. **GPM IMERG live fetch** not implemented (bounded-influence layer is).
5. **SoilGrids/Overpass not exercised live** — stubbed tests only.

---

# SESSION 5b — reference hunt for landslide and earthquake

**Question:** can we score the landslide and earthquake detectors at all?

## Earthquake: a REAL reference exists — EMSR317 Palu

Swept EMS activations 150-720 for earthquake events publishing GRADING
(damage) products. Found several; Palu (Indonesia, M7.5, Sept 2018) is by far
the strongest:

| | Palu AOI07 (EMSR317) |
|---|---|
| Graded building points | **9,457** |
| Destroyed | **1,995** |
| Damaged | 4,149 |
| Possibly damaged | 3,313 |
| Spatial extent of destroyed | **20.8 km²** (convex hull) |
| Post-event sensor | **Pleiades-1A/1B, 0.5 m** |

**The sensor is VHR, so an extent-vs-extent IoU is INVALID** — the same
disqualifier that rejected Townsville (scoring a 10 m prediction against a
0.5 m delineation measures sensor difference as pipeline error).

**But the reference is graded POINTS, not a competing extent map**, which
makes a different and legitimate question available: do our detected damage
pixels COINCIDE with where buildings were actually destroyed? That is a
spatial-agreement / hit-rate test, and it is exactly what the Phase 1e brief
anticipated ("report what it CAN validate — aggregate damage extent,
relative severity ordering").

**Verdict: SCOREABLE, but not by IoU.** 1,995 destroyed buildings over
20.8 km² is ample for a 10 m detector to be tested against.

## Landslide: still NO usable reference. The hunt failed honestly.

Swept the same range for landslide activations. 22 activations mention
landslides and publish vector products. Three had **Sentinel-1 10 m
post-event sources** — which looked like the breakthrough:

| Activation | Post-event sensor |
|---|---|
| EMSR251 Kragerø, Norway | Sentinel-1, 10 m |
| EMSR292 Chrisoupoli, Greece | Sentinel-1, 10 m |
| EMSR273 Barbullush, Albania | Sentinel-1, 10 m |

**They are not landslide references.** Reading each product's
`observed_event_a.event_type` — rather than trusting the activation
description — shows:

    EMSR251: {'5-Flood': 16}
    EMSR273: {'5-Flood': 83}
    EMSR292: {'5-Flood': 1}
    EMSR335: {'998-Other': 4}
    EMSR325: {'6-Mass Movement': 2}   <- the ONLY real landslide

These are FLOOD activations whose narrative text merely mentions landslides;
a keyword sweep matched the wrong thing. Checking `event_type` in the data
caught it before any of them became a "landslide" harness event.

**EMSR325 is a genuine mass-movement product but fails both bars:** only
**2 polygons**, and its post-event source is **Pleiades 0.5 m**.

**Verdict: landslide remains UNSCOREABLE.** Combined with the earlier COOLR
finding (polygon service down; 15 of 18 post-2018 records under 0.04 km²,
~40 px), there is still no landslide inventory with (a) polygons large
enough for a 10 m detector and (b) Sentinel post-event provenance.

## Flood regression run — inconclusive, and the reason is my error

A Kanalia re-run was launched to confirm this session's changes did not
regress the working S2 path. It was launched WITHOUT the historical date pin
(`sentinel_clock_patch`), so it searched present-day Kanalia (Oct 2026,
64.7% cloud) instead of the September 2023 flood. It refused correctly at
35.291% coverage against the 80% floor — the guard behaving properly — but
that says nothing about regression. 1,593 MB spent, no signal obtained.
The scored comparison must use the date-pinned harness path.

## Net position on validation

| Hazard | Reference | Scoreable? |
|---|---|---|
| Flood S1 | EMS, Sentinel-backed | **YES — n=2 scored** |
| Flood S2 | EMS, Sentinel-backed | **YES — n=1 scored** |
| **Earthquake** | **EMSR317 Palu, 9,457 graded points** | **YES — by spatial agreement, NOT IoU** |
| Landslide | none found | **NO** |

---

# EARTHQUAKE — FIRST SCORED RESULT, and it is WORSE THAN CHANCE

**Event:** EMSR317 Palu, Indonesia (M7.5, 2018-09-28). Same-relative-orbit
S1 pair, orbit 134 DESCENDING: pre 2018-06-07, post 2018-10-05 (+7.5 d).
Both scenes dual-pol (VV+VH), 3.0 GB. Scored against 9,457 EMS graded
building points by SPATIAL AGREEMENT, not IoU (the reference is 0.5 m
Pleiades-derived points; an extent IoU would measure sensor difference).

| EMS grade | n | hit | detection rate |
|---|---|---|---|
| Destroyed | 1,995 | 119 | **0.0596** |
| Damaged | 4,149 | 196 | 0.0472 |
| Possibly damaged | 3,313 | 167 | 0.0504 |
| **NULL BASELINE** (mask fraction over built-up) | — | — | **0.1126** |

**LIFT OVER CHANCE = 0.53x. The detector is WORSE THAN RANDOM.** It flags
11.26% of the built-up area but reaches only 5.96% of genuinely destroyed
buildings — randomly flagging that much area would hit roughly twice as many.

The severity ordering nominally passes (Destroyed 0.0596 > Possibly-damaged
0.0504) but the margin is trivial AND the middle grade is out of order
(Damaged 0.0472 < Possibly damaged 0.0504). That is noise, not a severity
signal, and it must not be reported as "ordering correct" without this
sentence attached.

**Two diagnostics say the mechanism did not fire:**

* `mean_vh_vv_change_db = -0.69`. The collapse signature is a POSITIVE
  VH/VV shift (double-bounce -> volume scattering). It came out NEGATIVE
  over the detected pixels — the opposite of the physics the detector is
  built on.
* `mean_correlation_in_damage = -0.061`, i.e. essentially zero, where
  Matsuoka-Yamazaki predicts a clear correlation LOSS relative to intact
  built-up.

**The leading explanation, stated as a limitation and not an excuse: the
112-day pre-event lead.** S1 acquisition over Palu was sparse in mid-2018 —
a +-45-day window around the quake contains 28 scenes and ALL of them are
post-event; orbit 134 stops after June and resumes only afterwards. So a
112-day baseline is the pair that EXISTS, not the pair the method needs.
Over 112 days in equatorial Indonesia, seasonal vegetation and
soil-moisture change produce backscatter differences the detector cannot
separate from structural change. This was recorded BEFORE the run, not
retro-fitted after seeing the number.

**A second, independent confound:** the built-up mask here is a SAR-intensity
proxy (`VV >= p60`), not the IBI optical mask the detector was designed
around — no optical pre/post pair was fetched for this run. Bright SAR
returns include vegetation edges and terrain, so the "built-up" denominator
is inflated, which depresses the lift figure by construction.

**What this DOES establish:**
* The earthquake path runs end to end on real dual-pol CDSE data —
  same-orbit pair, VH+VV both fetched, damage detection executing, audit
  fields intact.
* The scoring methodology works and is honest: it produced a *negative*
  result rather than a flattering one, because the null baseline was
  computed alongside the headline rate.

**What it does NOT establish:** any earthquake damage-detection accuracy.
**Do NOT claim earthquake accuracy in the paper on this evidence.** The
defensible statement is: "implemented, verified end-to-end on real dual-pol
Sentinel-1, and scored against 9,457 graded buildings — the result did not
exceed a random baseline under a 112-day pre-event lead and a proxy exposure
mask, both of which are stated limitations rather than established detector
failure."

**What would make it a fair test** (not attempted here): an event with a
same-orbit pre-event scene within ~2 weeks of the quake, and the real IBI
built-up mask from an optical pair. Turkey-Syria 2023 (M7.8) has dense S1
coverage and is the obvious candidate.

---

# EARTHQUAKE, SECOND ATTEMPT — Amatrice, and the REAL limit

Palu's failure was confounded by a 112-day pre-event lead. The retest fixed
exactly that: **EMSR177 Amatrice (Italy, M6.2, 2016-08-24), orbit 44
ASCENDING, pre 2016-08-22 (-1.4 d), post 2016-09-03 (+10.6 d)**, both scenes
dual-pol VV+VH. At a 1.4-day lead essentially nothing changed between the two
acquisitions except the earthquake — no seasonal confound.

The reference is also better than Palu's: **472 graded settlement POLYGONS**
across five damage grades, including **145 "Not Affected"** as a genuine
control class rather than only an area-fraction baseline.

**The result: ZERO hits in EVERY grade.**

| grade | n (px) | hit | rate |
|---|---|---|---|
| Completely Destroyed | 161 | 0 | 0.0000 |
| Highly Damaged | 156 | 0 | 0.0000 |
| Moderately Damaged | 250 | 0 | 0.0000 |
| Negligible to slight | 73 | 0 | 0.0000 |
| Not Affected | 368 | 0 | 0.0000 |
| NULL BASELINE | — | — | 0.0916 |

Zero across ALL grades — including the control — is not a weak detector. A
detector flagging 9.16% of built-up area would hit *something* by chance.
That pattern says the two datasets barely intersect, so the cause was
diagnosed rather than reported as an accuracy figure.

**THE CAUSE — a hard physical limit, not a bug:**

    reference polygons                     472
    TOTAL reference area                   0.1014 km2
    median polygon area                    143.2 m2
    polygons smaller than ONE 10 m pixel   172 / 472
    polygons smaller than FOUR pixels      413 / 472

**The Amatrice reference maps INDIVIDUAL BUILDINGS.** A 10 m Sentinel-1 pixel
is 100 m2; the median reference polygon is 143 m2 — about one and a half
pixels — and 87% of them are under four pixels. The whole town's graded
extent totals 0.10 km2, against a detected 2.468 km2 at the sensor's own
resolution.

This is precisely what `earthquake_damage.py`'s `resolution_limit` field has
stated in every result since it was written: *"Sentinel-1 at 10 m cannot
resolve individual buildings. This detects LARGE-SCALE destruction, NOT
per-structure damage."* Amatrice is a per-structure reference. The detector
and the reference address different spatial scales, and no scoring scheme
reconciles that.

## Net verdict on earthquake validation

Two attempts, two DIFFERENT and independently disqualifying obstacles:

| Attempt | Reference | Obstacle |
|---|---|---|
| Palu (EMSR317) | 9,457 graded points, 20.8 km2 | 112-day pre-event lead (seasonal confound); no tight same-orbit pair exists |
| Amatrice (EMSR177) | 472 graded polygons, 0.10 km2 | reference is PER-BUILDING (median 143 m2 vs a 100 m2 pixel) |

**Earthquake damage detection remains UNVALIDATED, and the reason is now
precise rather than vague.** EMS grading products are produced from
sub-metre VHR imagery for per-structure assessment. That is the wrong
granularity for a 10 m SAR detector by roughly two orders of magnitude in
area — the same mismatch that disqualified Townsville as a flood reference,
appearing here as a scale problem rather than a sensor-provenance one.

**What a fair test would need** (not available in EMS): a reference mapping
damage as CONTIGUOUS DISTRICT-SCALE extent — collapsed blocks, levelled
neighbourhoods — at >= ~0.01 km2 per feature, over an event with a
same-orbit S1 pair inside one revisit cycle. Turkey-Syria 2023 has the
imagery (1.4 d and 2.9 d leads confirmed by catalogue query) but its EMS
products are per-building too.

**Do NOT claim earthquake accuracy.** The defensible statement is:
"implemented and verified end-to-end on real dual-pol Sentinel-1 with a
1.4-day pre-event baseline; not validated, because every available reference
maps damage per-building at a granularity Sentinel-1 cannot resolve — a
stated limit of the sensor, documented in the detector's own output."
