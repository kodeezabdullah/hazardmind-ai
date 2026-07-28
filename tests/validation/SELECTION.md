# Reference Event Selection — Satellite Validation Harness

**Method.** Copernicus EMS Rapid Mapping activation pages
(`mapping.emergency.copernicus.eu/activations/EMSRxxx`) are a React SPA that
returns only a navigation shell to non-JS fetchers. The underlying data comes
from an undocumented public backend API:

```
https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations/?code=EMSRxxx
```

This returns real per-AOI product/sensor/date/download-URL JSON and was used
to confirm every claim below directly (not taken from search-result summaries).
It is rate-limited under repeated hits (a 4th consecutive query 403'd) — worth
knowing if this selection is ever re-verified or extended.

**Pakistan coverage is thin for Sentinel-backed events.** EMSR629/EMSR631
(the 2022 Pakistan floods — Jacobabad/Larkana/Shikarpur/Sanghar) were
delineated from Landsat-9, SPOT, and PlanetScope imagery, confirmed both via
the API and the independent NHESS paper (Sentinel-1-based analysis of the
severe flood over Pakistan 2022) — not Sentinel-backed, not usable here.
EMSR838 (Khyber Pakhtunkhwa flash flood, 15 Aug 2025) exists but its imagery
source could not be confirmed — the public page links out to an ArcGIS
StoryMap rather than exposing product metadata, and the API 403'd on this
code. **No genuinely Sentinel-backed Pakistan/arid EMSR event was locatable.**
This is reported plainly rather than substituted with a weaker forced match;
see "Event 3" below for how it's still represented (flagged, not used for
scored metrics).

---

## Event 1 — EMSR773, Valencia province, Spain (PRIMARY — S1 + S2 same AOI)

- **Activation:** EMSR773, "Flood", Spain. Trigger event: extraordinary
  rainfall affecting the Valencia region, **29 Oct 2024**.
- **AOI:** AOI01 "Valencia province" — extent polygon (WGS84):
  `POLYGON((-1.445179 39.781191, -1.485936 39.670665, -1.508716 39.470674, -1.236598 39.052786, -0.858222 38.872957, -0.456856 38.727721, -0.238921 38.723182, -0.040215 38.817867, 0.020432 38.892022, -0.26688 39.421202, -0.251454 39.615411, -0.539548 39.724123, -0.552116 39.724452, -0.551959 39.728807, -1.243609 39.989801, -1.445179 39.781191))`
  (bbox approx W -1.51 / E 0.02 / S 38.73 / N 39.99).
- **Reference products, same AOI:**
  - `DEL_MONIT02` — **Sentinel-2** optical, acquired **2024-11-05T10:52:00Z**.
    `https://rapidmapping.emergency.copernicus.eu/backend/EMSR773/AOI01/DEL_MONIT02/EMSR773_AOI01_DEL_MONIT02_v1.zip`
    Vector layers: `floodDepthA`, `maximumFloodExtentA`, `observedEventA`.
  - `DEL_MONIT03` — **Sentinel-1** SAR, acquired **2024-11-06T06:00:00Z**.
    `https://rapidmapping.emergency.copernicus.eu/backend/EMSR773/AOI01/DEL_MONIT03/EMSR773_AOI01_DEL_MONIT03_v1.zip`
    Same three vector layers.
- **Why this is the primary event:** the only candidate found where the SAME
  AOI has both a Sentinel-1 and a Sentinel-2 delineation product one day
  apart — lets the harness score both satellite paths against reference
  polygons that describe (nearly) the same flood state, rather than
  comparing S1-vs-event-A against S2-vs-event-B.
- **Caveat:** both products are ~1 week after the 29 Oct event peak (event
  peaked 29 Oct; these acquisitions are 5–6 Nov) — this is a **receding/late**
  flood state, not the peak. `maximumFloodExtentA` is the layer to use (it is
  explicitly the cumulative maximum extent product, not the single-scene
  observed extent), since it's the closest analogue to "what area did this
  flood affect" that the pipeline's own single-scene classification is trying
  to answer. Flagged so this isn't mistaken for a peak-state comparison.
- **Pre-event Sentinel-1 (for change-detection, not used by this harness pass
  since the current pipeline has no bi-temporal path — recorded for the
  future NDVI/SAR change-detection work `agents/satellite/CLAUDE.md`
  flags as a gap):** not stated in the API; a same-relative-orbit S1 pass
  within 19 Oct–29 Oct plausibly exists given Sentinel-1's ~6-12 day European
  revisit, but the exact orbit needs a live CDSE lookup — **orbit TBD**.

## Event 2 — EMSR692, Magnesia, Greece (Storm Daniel — S1 only, near-peak)

- **Activation:** EMSR692, "Coastal flood", Greece. Trigger event: Storm
  Daniel, extreme rainfall over Thessaly, **5 Sep 2023, ~10:30 UTC**
  (Zagora station recorded ~645mm in hours).
- **AOI:** AOI01 "Magnesia" — extent polygon (WGS84):
  `POLYGON((21.465407 39.704952, 21.719982 39.109924, 22.88329 39.140679, 22.822214 39.272573, 22.9454 39.294109, 22.944826 39.346231, 22.992373 39.342759, 23.013085 39.314989, 23.094809 39.304003, 23.149838 39.278925, 23.172909 39.238844, 23.198043 39.168044, 23.14873 39.13685, 23.084992 39.187582, 23.044167 39.191028, 23.042285 39.155262, 23.037486 39.102119, 23.037454 39.100072, 23.062034 39.072224, 23.217704 39.088054, 23.516032 39.10907, 23.55055 39.193414, 23.322504 39.264777, 23.259362 39.347248, 23.212783 39.402932, 23.140933 39.454778, 23.109855 39.496992, 23.037641 39.538584, 22.95727 39.57511, 22.924663 39.618361, 22.897693 39.6923, 22.86812 39.781723, 22.852013 39.816286, 22.752076 39.88767, 22.725902 39.916224, 22.35542 39.837662, 22.276497 39.802357, 22.275303 39.749278, 21.465407 39.704952))`
- **Reference products, same AOI, both Sentinel-1:**
  - `DEL_MONIT01` — **2023-09-06T04:39:00Z** (~1 day post-event, the earliest
    successful delineation; the very first attempted product for this
    activation, a PAZ-satellite `DEL_PRODUCT` on 5 Sep, was marked "Not
    produced").
    `https://rapidmapping.emergency.copernicus.eu/backend/EMSR692/AOI01/DEL_MONIT01/EMSR692_AOI01_DEL_MONIT01_v3.zip`
  - `DEL_MONIT02` — **2023-09-07T16:25:00Z** (~2 days post-event).
    `https://rapidmapping.emergency.copernicus.eu/backend/EMSR692/AOI01/DEL_MONIT02/EMSR692_AOI01_DEL_MONIT02_v4.zip`
- **Why this event:** the closest-to-peak Sentinel-backed delineation found
  in this pass (1 day post-event, vs. Valencia's ~1 week) — the best
  available test of the pipeline against a genuinely near-peak flood state,
  even though it only covers the SAR path (no Sentinel-2 product exists for
  this specific AOI; the activation's other AOIs — Palamas, Larissa,
  Stefanovikio — used GeoEye/SPOT/WorldView instead and are not usable here).
- **No Sentinel-2 counterpart for this AOI** — this event scores the S1/SAR
  path only. Given the CLAUDE.md-documented uncalibrated-SAR caveat, this is
  expected to be the harness's clearest test of "does the SAR path produce
  anything scorable at all."

## Event 3 — EMSR838, Khyber Pakhtunkhwa, Pakistan (FLAGGED, not scored)

- **Activation:** EMSR838, flash flood, Khyber Pakhtunkhwa province,
  Pakistan. Event: **15 Aug 2025**; report published ~29 Aug 2025.
- **Status: imagery source NOT confirmed.** The public page links to an
  ArcGIS StoryMap rather than exposing structured product metadata, and the
  backend API 403'd on this code during this research pass (unclear whether
  that's absence of published product data, a genuine access restriction, or
  transient rate-limiting — not resolved). Terrain (steep KP valleys) and
  August monsoon cloud cover make either VHR optical or SAR-only delineation
  plausible, but neither is confirmed.
- **Included in this selection only as the honestly-flagged arid/Pakistan
  candidate the task asked for** — it is carried through
  `reference_events/emsr838_kp_pakistan.yaml` with `status: unconfirmed` and
  the harness **skips it for scored metrics**, reporting it separately as
  "excluded — imagery source unconfirmed" rather than silently omitting it or
  forcing a score against unverified ground truth.

---

## Summary

| # | Event | Country | Sensors (same AOI) | Timing vs. peak | Role |
|---|---|---|---|---|---|
| 1 | EMSR773 Valencia | Spain | S1 (2024-11-06) + S2 (2024-11-05) | ~1 week post-peak | Primary — both satellite paths, comparable state |
| 2 | EMSR692 Magnesia | Greece | S1 only (2023-09-06, -07) | ~1 day post-peak | Near-peak SAR-only test |
| 3 | EMSR838 KP | Pakistan | Unconfirmed | Unknown | Flagged, excluded from scoring |

Only 2 of 3 candidates are scorable — short of the "3-4 events" asked for.
This reflects a genuine constraint of the domain (Sentinel-backed EMS
delineation products, over an arid/Pakistan-representative geography, at a
resolution comparable to what this pipeline itself produces, are simply rare
in the public EMS catalogue) rather than a research shortfall — see
`SELECTION.md`'s Pakistan-coverage note above and the baseline report's own
section on this.
