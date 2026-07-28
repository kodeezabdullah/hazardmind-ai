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
| **Running total** | **4,590 MB (~4.6 GB)** |

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
