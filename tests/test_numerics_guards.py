"""Regression tests for silent-failure traps in the numeric layer.

Each test here corresponds to a defect where a bad input produced a *plausible
looking* answer instead of an error — the failure mode that costs money,
because nothing alarms. Two families:

1. The `float(x) or default` NaN trap. NaN is truthy in Python, so the idiom
   passes NaN through as the value instead of substituting the default.
2. Library contract drift hidden behind a bare `except Exception`.
"""

from __future__ import annotations

import math
import warnings

import numpy as np
import pytest

from quantsys.data.features import ewma_vol_series
from quantsys.strategies import meanrev as mr


# --------------------------------------------------------------- NaN traps
def test_ewma_vol_series_all_nan_input_does_not_poison_the_series():
    """An all-NaN return window must fall back to the tiny variance seed.

    Before the fix: np.nanvar -> nan, `nan or 1e-12` -> nan (NaN is truthy),
    so every element of the vol series was NaN. np.clip(vol, 1e-8, None) in
    the regime detector does NOT remove NaN, so log(nan) flowed into the HMM
    feature vector and the regime posterior silently degraded.
    """
    r = np.full(64, np.nan)
    out = ewma_vol_series(r, halflife=10.0)
    assert out.shape == (64,)
    assert np.all(np.isfinite(out)), "all-NaN input produced a non-finite vol series"
    assert np.all(out > 0.0)


def test_ewma_vol_series_partial_nan_is_tolerated():
    rng = np.random.default_rng(0)
    r = rng.normal(0.0, 0.01, 128)
    r[5] = np.nan
    r[100] = np.inf
    out = ewma_vol_series(r, halflife=20.0)
    assert np.all(np.isfinite(out))


def test_ewma_vol_series_zero_variance_seeds_the_floor_not_zero():
    """All-equal returns have zero variance; the series must stay positive so
    downstream divisions by vol cannot blow up."""
    out = ewma_vol_series(np.zeros(32), halflife=10.0)
    assert np.all(out > 0.0)
    assert np.all(out <= 1e-5)


def test_ewma_vol_series_matches_the_recursion_on_clean_input():
    """Guard the fix against changing the happy-path numbers."""
    rng = np.random.default_rng(7)
    r = rng.normal(0.0, 0.02, 50)
    lam = math.exp(math.log(0.5) / 15.0)
    v = float(np.nanvar(r))
    expected = []
    for x in r:
        v = lam * v + (1.0 - lam) * x * x
        expected.append(math.sqrt(v))
    got = ewma_vol_series(r, halflife=15.0)
    np.testing.assert_allclose(got, expected, rtol=1e-12)


@pytest.mark.parametrize(
    ("value", "fallback", "expected"),
    [
        (0.5, 1e-9, 0.5),
        (0.0, 1e-9, 1e-9),
        (-1.0, 1e-9, 1e-9),
        (float("nan"), 1e-9, 1e-9),
        (float("inf"), 1e-9, 1e-9),
    ],
)
def test_positive_or_substitutes_on_every_bad_value(value, fallback, expected):
    assert mr._positive_or(value, fallback) == expected


# ------------------------------------------------- adfuller contract drift
def _stationary_residual(n: int = 300) -> np.ndarray:
    """Mean-reverting AR(1), so a correct ADF call returns a small p-value."""
    rng = np.random.default_rng(3)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = 0.5 * x[i - 1] + rng.normal(0.0, 1.0)
    return x


def test_adf_pvalue_returns_a_small_pvalue_for_a_stationary_series():
    p = mr._adf_pvalue(_stationary_residual())
    assert p is not None
    assert 0.0 <= p <= 1.0
    assert p < 0.05, "a strongly mean-reverting AR(1) should reject the unit root"


def test_adf_pvalue_emits_no_futurewarning():
    """statsmodels 0.15 warns that adfuller's return contract changes in 0.16.

    The call must pin the contract explicitly, otherwise the warning becomes an
    error under this project's -W error policy and the pair gate silently
    stops finding pairs.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        p = mr._adf_pvalue(_stationary_residual())
    assert p is not None


def test_adf_capability_probe_is_stateless_across_patched_functions():
    """The version probe must be keyed on the function, not a module global.

    A mutable module-level flag made test order matter: whichever test ran
    last decided which code path every later caller took.
    """

    def with_kwarg(x, regression="c", autolag="AIC", result_object=False):
        return (-3.0, 0.01, 1, 10, {}, 1.0)

    def without_kwarg(x, regression="c", autolag="AIC"):
        return (-3.0, 0.02, 1, 10, {}, 1.0)

    assert mr._accepts_result_object(with_kwarg) is True
    assert mr._accepts_result_object(without_kwarg) is False
    # Re-asserting in the other order must give the same answers.
    assert mr._accepts_result_object(without_kwarg) is False
    assert mr._accepts_result_object(with_kwarg) is True


def test_adf_pvalue_reads_the_new_result_object_contract(monkeypatch):
    """Forward-compat: statsmodels 0.16+ returns an ADFullerResult object."""

    class FakeResult:
        pvalue = 0.0123

    import statsmodels.tsa.stattools as st

    def new_adfuller(x, regression="c", autolag="AIC", result_object=True):
        return FakeResult()

    monkeypatch.setattr(st, "adfuller", new_adfuller)
    assert mr._adf_pvalue(np.zeros(10)) == pytest.approx(0.0123)


def test_adf_pvalue_reads_the_legacy_tuple_contract(monkeypatch):
    import statsmodels.tsa.stattools as st

    def legacy_adfuller(x, regression="c", autolag="AIC"):
        return (-3.1, 0.027, 1, 290, {}, 100.0)

    monkeypatch.setattr(st, "adfuller", legacy_adfuller)
    assert mr._adf_pvalue(np.zeros(10)) == pytest.approx(0.027)


def test_adf_pvalue_returns_none_and_logs_on_a_numerical_failure(monkeypatch, caplog):
    """A genuine numeric failure must be observable, not swallowed silently."""
    import statsmodels.tsa.stattools as st

    def boom(x, regression="c", autolag="AIC"):
        raise ValueError("sample size too short")

    monkeypatch.setattr(st, "adfuller", boom)
    with caplog.at_level("WARNING", logger="quantsys.strategies.meanrev"):
        assert mr._adf_pvalue(np.zeros(4)) is None
    assert any("ADF test could not be computed" in r.message for r in caplog.records)


def test_adf_pvalue_rejects_a_non_finite_pvalue(monkeypatch, caplog):
    import statsmodels.tsa.stattools as st

    def nan_adfuller(x, regression="c", autolag="AIC"):
        return (-1.0, float("nan"), 1, 10, {}, 1.0)

    monkeypatch.setattr(st, "adfuller", nan_adfuller)
    with caplog.at_level("WARNING", logger="quantsys.strategies.meanrev"):
        assert mr._adf_pvalue(np.zeros(10)) is None
    assert any("non-finite p-value" in r.message for r in caplog.records)


def test_adf_pvalue_does_not_swallow_programming_errors(monkeypatch):
    """The narrowed except must let a real bug (e.g. a typo) surface."""
    import statsmodels.tsa.stattools as st

    def boom(x, regression="c", autolag="AIC"):
        raise AttributeError("typo in caller")

    monkeypatch.setattr(st, "adfuller", boom)
    with pytest.raises(AttributeError):
        mr._adf_pvalue(np.zeros(10))
