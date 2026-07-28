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

## Cumulative download cost (this session)

| Run | MB |
|---|---|
| (none yet) | 0 |

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
