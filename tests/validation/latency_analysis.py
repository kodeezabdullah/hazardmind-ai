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
    """Two-sided p for Pearson r via the t-distribution, computed from the
    incomplete beta function so no scipy import is needed."""
    if n < 3 or r is None or abs(r) >= 1.0:
        return None
    df = n - 2
    t = abs(r) * math.sqrt(df / (1.0 - r * r))
    x = df / (df + t * t)
    return _betainc(df / 2.0, 0.5, x)


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a,b) via continued fraction."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
    f, c, d = 1.0, 1.0, 0.0
    for i in range(0, 300):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        c = 1.0 + num / c
        if abs(c) < 1e-30:
            c = 1e-30
        f *= c * d
        if abs(1.0 - c * d) < 1e-10:
            break
    r = front * (f - 1.0)
    return r if x < (a + 1) / (a + b + 2) else 1.0 - r


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
