"""Verify the statistics in latency_analysis.py against known reference values.

WHY THIS EXISTS
---------------
The first version of `p_value_two_sided` hand-rolled a continued fraction for
the regularised incomplete beta and returned p == |r| on the real data — for
r=-0.435 it reported p=0.435, for r=-0.201 it reported p=0.201. Those numbers
look like plausible p-values, which is exactly what makes the bug dangerous:
in a validation report a wrong p launders a null result as a measured one, and
nothing about the output would have flagged it.

A statistic used to decide whether a scientific claim holds must itself be
verified against values computed independently, not eyeballed for plausibility.
Reference values below are standard published figures (any t-table) and
scipy-computed spot checks.

Run:  python -m pytest test_latency_stats.py -q
"""

from __future__ import annotations

import math

import pytest

from latency_analysis import (
    p_value_two_sided,
    pearson,
    spearman,
)


# --------------------------------------------------------------------------
# Correlation coefficients
# --------------------------------------------------------------------------
def test_pearson_perfect_positive():
    assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)


def test_pearson_perfect_negative():
    assert pearson([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)


def test_pearson_known_value():
    # Textbook pair: r = 0.9749 (3 s.f.) for this classic dataset.
    xs = [1, 2, 3, 4, 5]
    ys = [2, 4, 5, 4, 5]
    assert pearson(xs, ys) == pytest.approx(0.7746, abs=1e-4)


def test_pearson_zero_variance_returns_none():
    # A constant series has no correlation defined — must return None, not 0.0,
    # so the caller cannot mistake "undefined" for "no relationship".
    assert pearson([1, 1, 1, 1], [1, 2, 3, 4]) is None


def test_pearson_too_few_points_returns_none():
    assert pearson([1, 2], [3, 4]) is None


def test_spearman_is_rank_based_and_monotone_invariant():
    # Spearman must be 1.0 for ANY strictly increasing relationship, even a
    # wildly non-linear one where Pearson is not 1.0.
    xs = [1, 2, 3, 4, 5]
    ys = [1, 10, 1000, 100000, 10000000]
    assert spearman(xs, ys) == pytest.approx(1.0)
    assert pearson(xs, ys) < 0.95  # Pearson is NOT 1.0 here


def test_spearman_handles_ties():
    # Tied values must share an averaged rank; a naive implementation
    # silently biases rho here.
    assert spearman([1, 2, 2, 3], [1, 2, 2, 3]) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# p-values — the part that was wrong
# --------------------------------------------------------------------------
def test_p_value_is_not_just_abs_r():
    """The exact bug that shipped: p came back equal to |r|."""
    for r, n in ((-0.435, 4), (-0.201, 4), (0.62, 5), (-0.9, 6)):
        p = p_value_two_sided(r, n)
        assert p is not None
        assert abs(p - abs(r)) > 1e-6, f"p={p} equals |r|={abs(r)} for n={n}"


@pytest.mark.parametrize(
    "r,n,expected",
    [
        # r, n, two-sided p (t-table / scipy reference values)
        (0.0, 10, 1.0),
        (0.5, 10, 0.1411),
        (0.8, 10, 0.0055),
        (0.632, 10, 0.0500),  # the classic n=10 5% critical value
        (0.878, 5, 0.0501),   # the n=5 5% critical value
        (-0.435, 4, 0.5650),
    ],
)
def test_p_value_against_reference_values(r, n, expected):
    p = p_value_two_sided(r, n)
    assert p == pytest.approx(expected, abs=5e-3), f"r={r} n={n}: got {p}, want {expected}"


def test_p_value_symmetric_in_sign():
    assert p_value_two_sided(0.6, 7) == pytest.approx(p_value_two_sided(-0.6, 7))


def test_p_value_decreases_with_n_for_fixed_r():
    """More data at the same effect size must be more significant."""
    ps = [p_value_two_sided(0.6, n) for n in (5, 8, 12, 20, 40)]
    assert all(a > b for a, b in zip(ps, ps[1:])), ps


def test_p_value_none_below_three_points():
    assert p_value_two_sided(0.9, 2) is None


def test_fallback_matches_scipy_when_scipy_present():
    """The scipy-free path must agree with scipy, since it is what runs in a
    deployment without scipy — the very condition that caused the Kosutarica
    incident."""
    scipy_stats = pytest.importorskip("scipy.stats")
    from latency_analysis import _t_sf_two_sided

    # df=2 is this analysis's own regime (n=4), and is where the first
    # implementation's finite-cutoff truncation was worst.
    for t, df in ((0.5, 1), (0.5, 2), (1.0, 3), (2.5, 4), (3.2, 8), (0.1, 15)):
        ref = float(2.0 * scipy_stats.t.sf(t, df))
        got = _t_sf_two_sided(t, df)
        assert got == pytest.approx(ref, abs=1e-9), f"t={t} df={df}: {got} vs {ref}"
