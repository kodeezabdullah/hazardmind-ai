"""STEP 3 / STEP 4 analysis — precision vs post-peak acquisition latency, and
confidence vs measured accuracy, across every scored S1 flood event.

WHY THIS FILE EXISTS AS CODE RATHER THAN A HAND-WRITTEN TABLE
------------------------------------------------------------
SCIENCE_LOG's own "CORRECTION — post-peak latency does NOT explain the flood
scores" section was written after a latency claim had already been published
in the log on three events, one of which (Tychero) had no recovered
acquisition date at the time. The claim did not survive the fourth number.

The lesson taken from that is procedural, not statistical: the latency of a
scored run must be DERIVED from persisted evidence every time the table is
regenerated, never re-typed from a previous version of the table. So this
script recomputes latency from:

    satellite_results.scene_id   (the CDSE product UUID, persisted per run)
      -> CDSE OData catalogue    (ContentDate/Start = the real acquisition)
    minus reference_events/<event>.yaml : event_peak_date

and refuses to report a correlation for any event whose latency it could not
derive that way. `scene_age_days` is deliberately NOT used as the latency
source: it is measured against the run's frozen "now", not against the flood
peak, so it answers a different question.

STATISTICS DISCIPLINE
---------------------
n is tiny (single digits). Pearson r on n<10 is reported WITH its p-value and
WITH Spearman alongside, because a single influential point can manufacture
an r of 0.9 in either direction. Where p > 0.05 the finding is reported as
"not significant", never as a trend, and never as a reason to tune anything.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
EVENTS_DIR = HERE / "reference_events"


# --------------------------------------------------------------------------
# Latency derivation
# --------------------------------------------------------------------------
def _parse_utc(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def acquisition_datetime_from_cdse(scene_id: str) -> datetime | None:
    """Resolve a persisted CDSE product UUID to its real acquisition instant.

    Returns None (never a guess) when the catalogue cannot resolve it — an
    unresolvable scene means the event drops out of the correlation rather
    than entering it with an estimated x-value.
    """
    import requests

    url = f"https://catalogue.dataspace.copernicus.eu/odata/v1/Products({scene_id})"
    try:
        r = requests.get(url, timeout=90)
        if r.status_code != 200:
            return None
        return _parse_utc(r.json()["ContentDate"]["Start"])
    except Exception:
        return None


def post_peak_latency_days(peak_date: str, acquired: datetime) -> float:
    return (acquired - _parse_utc(peak_date + "T00:00:00Z")).total_seconds() / 86400.0


# --------------------------------------------------------------------------
# Correlation helpers (no scipy dependency — this repo's venv varies)
# --------------------------------------------------------------------------
def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _rank(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    ranks = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return pearson(_rank(xs), _rank(ys))


def p_value_two_sided(r: float, n: int) -> float | None:
    """Two-sided p for Pearson r, from the exact Student-t survival function.

    Uses scipy when available; otherwise falls back to a numerically integrated
    t density. An EARLIER version of this function hand-rolled a continued
    fraction for the incomplete beta and returned p == |r| on the real data —
    a wrong p-value is worse than no p-value in a validation report, because it
    launders a null result as a measured one. It is verified against known
    reference values in `test_latency_stats.py` rather than trusted.
    """
    if n < 3 or r is None:
        return None
    if abs(r) >= 1.0:
        return 0.0
    df = n - 2
    t = abs(r) * math.sqrt(df / (1.0 - r * r))
    try:
        from scipy import stats

        return float(2.0 * stats.t.sf(t, df))
    except Exception:
        return _t_sf_two_sided(t, df)


def _t_sf_two_sided(t: float, df: int) -> float:
    """2 * P(T > t) for Student-t with `df` degrees of freedom, scipy-free.

    Integrates on the substitution u = 1/x over (0, 1/t], which maps the
    infinite tail onto a FINITE interval and so has no truncation error at
    all. A first version integrated x over [t, t+60+10*sqrt(df)] and was wrong
    in the 4th decimal at df=2 — Student-t tails decay only polynomially
    (~x^-(df+1)), so at low df a large but finite cutoff still discards real
    mass. Low df is exactly this analysis's regime (n=4 means df=2), i.e. the
    error was worst precisely where it would be used.

    Verified against scipy to 1e-9 in test_latency_stats.py.
    """
    if t <= 0:
        return 1.0
    lognorm = (
        math.lgamma((df + 1) / 2.0)
        - math.lgamma(df / 2.0)
        - 0.5 * math.log(df * math.pi)
    )

    def integrand(u: float) -> float:
        """density(1/u) / u^2 — the Jacobian of x = 1/u.

        As u -> 0 this tends to df^((df+1)/2) * exp(lognorm) * u^(df-1), i.e.
        it vanishes for df > 1 but is CONSTANT at df = 1 (Cauchy). Computing it
        via the log form keeps that limit exact instead of evaluating 0 * inf.
        """
        if u <= 0.0:
            # Exact u -> 0+ limit: 0 for df > 1, a finite constant at df == 1.
            if df > 1:
                return 0.0
            return math.exp(lognorm + 0.5 * math.log(df) * (df + 1))
        x2_over_df = 1.0 / (u * u * df)
        # log[ density(1/u) / u^2 ] rearranged so no term overflows for small u:
        #   = lognorm - ((df+1)/2)*log1p(1/(u^2 df)) - 2 log u
        return math.exp(
            lognorm - ((df + 1) / 2.0) * math.log1p(x2_over_df) - 2.0 * math.log(u)
        )

    hi = 1.0 / t
    steps = 40000  # even, for Simpson
    h = hi / steps
    total = integrand(0.0) + integrand(hi)
    for i in range(1, steps):
        total += integrand(i * h) * (4 if i % 2 else 2)
    return min(1.0, 2.0 * total * h / 3.0)


def report_correlation(label: str, xs: list[float], ys: list[float]) -> str:
    n = len(xs)
    r = pearson(xs, ys)
    rho = spearman(xs, ys)
    if r is None:
        return f"  {label:34s} n={n}  (too few points for a correlation)"
    p = p_value_two_sided(r, n)
    sig = "NOT significant" if (p is None or p > 0.05) else "significant at p<0.05"
    ps = f"p={p:.3f}" if p is not None else "p=n/a"
    return (
        f"  {label:34s} n={n}  Pearson r={r:+.3f} ({ps}, {sig})"
        f"  Spearman rho={rho:+.3f}"
    )
