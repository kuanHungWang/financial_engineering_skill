"""Tests for pricer/exercise.py: Longstaff-Schwartz early exercise, autocall termination and the summarizer.

LSM is checked against QuantLib's finite-difference Bermudan put and the Black-Scholes European limit; autocall and
the summarizer against hand-computed tables and exact pathwise identities. Run:  python -m pytest tests -q
"""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import QuantLib as ql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pricer.exercise import (TerminationResult, autocall, forward_discount_factors,  # noqa: E402
                             longstaff_schwartz, summarize)

Phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))  # noqa: E731
S0, K, R, SIG, T, STEPS = 36.0, 40.0, 0.06, 0.2, 1.0, 50


def gbm(n, seed, steps=STEPS):
    """Antithetic GBM on `steps` equally spaced dates after today: (steps, n)."""
    z = np.random.default_rng(seed).standard_normal((steps, n // 2))
    z = np.concatenate([z, -z], axis=1).reshape(steps, 2, n // 2).transpose(0, 2, 1).reshape(steps, n)
    dt = T / steps
    return S0 * np.exp(np.cumsum((R - 0.5 * SIG ** 2) * dt + SIG * math.sqrt(dt) * z, axis=0))


def bermudan_put(n=100_000, seed=7, **kw):
    s = gbm(n, seed)
    payoff = np.maximum(K - s, 0.0)
    fdf = np.full(STEPS, math.exp(-R * T / STEPS))
    return s, longstaff_schwartz(np.zeros_like(s), payoff, fdf, s, itm_only=True, **kw)


def ql_bermudan_put():
    today = ql.Date(28, 9, 2026)
    ql.Settings.instance().evaluationDate = today
    dc = ql.Actual365Fixed()
    dates = [today + int(round(365 * (i + 1) / STEPS)) for i in range(STEPS)]
    opt = ql.VanillaOption(ql.PlainVanillaPayoff(ql.Option.Put, K), ql.BermudanExercise(dates))
    proc = ql.BlackScholesMertonProcess(
        ql.QuoteHandle(ql.SimpleQuote(S0)), ql.YieldTermStructureHandle(ql.FlatForward(today, 0.0, dc)),
        ql.YieldTermStructureHandle(ql.FlatForward(today, R, dc)),
        ql.BlackVolTermStructureHandle(ql.BlackConstantVol(today, ql.NullCalendar(), SIG, dc)))
    opt.setPricingEngine(ql.FdBlackScholesVanillaEngine(proc, 800, 800))
    return opt.NPV()


# ------------------------------------------------------------------ Longstaff-Schwartz
def test_lsm_bermudan_put_matches_quantlib_fd():
    _, res = bermudan_put()
    s = summarize(res, antithetic=True)
    ref = ql_bermudan_put()
    # LSM is slightly low-biased (sub-optimal policy) and slightly high-biased in sample; both are a few cents
    assert abs(s.price - ref) < 4 * s.std_error + 0.02, (s.price, ref, s.std_error)
    assert s.price > 4.0                                                          # well above European (3.84)
    assert 0.5 < res.diagnostics["total_exercise_probability"] < 1.0


def test_lsm_out_of_sample_policy_is_low_biased_and_close():
    _, fit = bermudan_put(n=50_000, seed=1)
    _, oos = bermudan_put(n=100_000, seed=2, policy=fit.diagnostics["policy"])
    s, ref = summarize(oos, antithetic=True), ql_bermudan_put()
    assert not oos.diagnostics["in_sample"]
    assert s.price < ref + 3 * s.std_error
    assert s.price > ref - 0.05


def test_lsm_single_exercise_date_is_european():
    n = 200_000
    s = gbm(n, 3)
    payoff = np.full_like(s, np.nan)
    payoff[-1] = np.maximum(K - s[-1], 0.0)
    res = longstaff_schwartz(np.zeros_like(s), payoff, np.full(STEPS, math.exp(-R * T / STEPS)), s)
    out = summarize(res, antithetic=True)
    d1 = (math.log(S0 / K) + (R + 0.5 * SIG ** 2) * T) / (SIG * math.sqrt(T))
    bs = K * math.exp(-R * T) * Phi(-(d1 - SIG * math.sqrt(T))) - S0 * Phi(-d1)
    assert abs(out.price - bs) < 4 * out.std_error
    assert np.allclose(res.diagnostics["exercise_probability"][-1], (s[-1] < K).mean())
    assert not res.event[:-1].any()


def test_lsm_tables_are_consistent():
    s, res = bermudan_put(n=20_000)
    ev, surv = res.event, res.survival
    assert (ev.sum(axis=0) <= 1).all()
    assert surv[0].all()
    # alive on t+1 <=> alive on t and not exercised on t
    assert (surv[1:] == (surv[:-1] & ~ev[:-1])).all()
    # paid cashflow = exercise payoff on the exercise date, nothing else (no coupons)
    assert np.allclose(res.cashflows, np.maximum(K - s, 0.0) * ev)
    assert np.allclose(res.discount[:, 0], np.exp(-R * T * np.arange(1, STEPS + 1) / STEPS))
    assert np.allclose(res.survival_probability[1:], 1 - np.cumsum(res.event_probability)[:-1])


def test_lsm_issuer_call_deterministic_rates():
    """No randomness: a 5% annual coupon bond discounted at 3%, callable at par every year from year 1. The
    issuer calls on the first date; the holder gets coupon + par at year 1."""
    n, yrs = 1000, 5
    cf = np.full((yrs, n), 5.0)
    cf[-1] += 100.0
    call = np.full((yrs, n), 100.0)
    call[-1] = np.nan                                                          # maturity: redemption, no call
    fdf = np.full(yrs, math.exp(-0.03))
    res = longstaff_schwartz(cf, call, fdf, np.ones((yrs, n)), exerciser="issuer")
    assert res.event[0].all()
    assert np.allclose(res.cashflows[0], 105.0) and np.allclose(res.cashflows[1:], 0.0)
    assert summarize(res).price == pytest.approx(105.0 * math.exp(-0.03))
    # without settlement of the call-date coupon the issuer saves it
    res2 = longstaff_schwartz(cf, call, fdf, np.ones((yrs, n)), exerciser="issuer", settle_on_event_date=False)
    assert summarize(res2).price == pytest.approx(100.0 * math.exp(-0.03))
    # the same bond as a puttable (holder) with a below-market coupon: the holder puts on the first date
    cf_low = np.full((yrs, n), 1.0)
    cf_low[-1] += 100.0
    put = longstaff_schwartz(cf_low, call, fdf, np.ones((yrs, n)), exerciser="holder")
    assert put.event[0].all()


def test_lsm_callable_bond_stochastic_rates_bounds():
    """Vasicek short rate with path-wise forward DFs: the issuer's call can only lower the holder's value, and
    the callable never exceeds the call price plus the coupon on a call date."""
    rng = np.random.default_rng(5)
    n, yrs, a, b, sig, r0 = 40_000, 10, 0.1, 0.04, 0.015, 0.04
    r = np.empty((yrs + 1, n))
    r[0] = r0
    for t in range(yrs):                       # yearly Euler with 12 sub-steps; fdf from the trapezoid integral
        x = r[t]
        integral = np.zeros(n)
        for _ in range(12):
            nx = x + a * (b - x) / 12 + sig * math.sqrt(1 / 12) * rng.standard_normal(n)
            integral += 0.5 * (x + nx) / 12
            x = nx
        r[t + 1] = x
        if t == 0:
            fdf = np.empty((yrs, n))
        fdf[t] = np.exp(-integral)
    cf = np.full((yrs, n), 4.0)
    cf[-1] += 100.0
    call = np.full((yrs, n), 100.0)
    call[:2] = np.nan                                                          # 2Y non-call
    straight = summarize(np.cumprod(fdf, axis=0) * cf).price
    res = longstaff_schwartz(cf, call, fdf, r[1:], exerciser="issuer")
    callable_ = summarize(res).price
    assert callable_ < straight
    assert not res.event[:2].any()
    assert 0.0 < res.diagnostics["total_exercise_probability"] < 1.0
    # called paths: the discounted value at the call date is par + coupon, below the continuation on average
    assert np.allclose(res.cashflows[res.event], 104.0)


def test_lsm_accepts_dataframes_and_multi_factor_state():
    dates = pd.date_range("2027-01-01", periods=STEPS, freq="W").date
    s = gbm(20_000, 9)
    df = pd.DataFrame(s, index=dates)
    res = longstaff_schwartz(pd.DataFrame(0.0, index=dates, columns=df.columns), np.maximum(K - df, 0.0),
                             pd.Series(math.exp(-R * T / STEPS), index=dates), {"S": df, "S2": df ** 2},
                             itm_only=True)
    assert res.index == list(dates)
    assert res.table("event").index[0] == dates[0]
    summ = summarize(res)
    assert list(summ.expected_cashflows.index) == list(dates)
    assert 4.0 < summ.price < 4.7


def test_lsm_shape_errors():
    s = gbm(10, 1)
    with pytest.raises(ValueError, match="exercise_payoff"):
        longstaff_schwartz(np.zeros_like(s), np.zeros((3, 10)), 1.0, s)
    with pytest.raises(ValueError, match="exerciser"):
        longstaff_schwartz(np.zeros_like(s), s, 1.0, s, exerciser="bank")


# ------------------------------------------------------------------ autocall
def test_autocall_tables():
    cf = np.array([[1.0, 1.0, 1.0],
                   [1.0, 1.0, 1.0],
                   [101.0, 101.0, 101.0]])
    mask = np.array([[False, True, False],
                     [True, True, False],
                     [False, False, False]])
    amount = np.array([100.0, 100.0, 0.0])
    fdf = np.array([0.99, 0.98, 0.97])
    ac = autocall(cf, mask, amount, fdf)
    assert (ac.event == np.array([[0, 1, 0], [1, 0, 0], [0, 0, 0]], dtype=bool)).all()
    assert (ac.survival == np.array([[1, 1, 1], [1, 0, 1], [0, 0, 1]], dtype=bool)).all()
    assert np.allclose(ac.cashflows, [[1, 101, 1], [101, 0, 1], [0, 0, 101]])
    assert np.allclose(ac.survival_probability, [1, 2 / 3, 1 / 3])
    assert np.allclose(ac.diagnostics["autocall_probability"], [1 / 3, 1 / 3, 0])
    assert ac.termination_probability == pytest.approx(2 / 3)
    d = np.cumprod(fdf)
    s = summarize(ac)
    assert np.allclose(s.fair_values, [d[0] + 101 * d[1], 101 * d[0], d[0] + d[1] + 101 * d[2]])
    # unsettled: the call-date coupon is not paid
    ac2 = autocall(cf, mask, amount, fdf, settle_on_event_date=False)
    assert np.allclose(ac2.cashflows, [[1, 100, 1], [100, 0, 1], [0, 0, 101]])


def test_autocall_never_triggered_is_identity():
    cf = np.random.default_rng(0).random((6, 50))
    ac = autocall(cf, np.zeros((6, 50), dtype=bool), 100.0)
    assert np.array_equal(ac.cashflows, cf) and ac.survival.all() and not ac.event.any()
    assert np.allclose(ac.discount, 1.0)


def test_per_period_cashflows_broadcast_against_path_tables():
    """Deterministic cashflows given per period (T,) or (T, 1) broadcast against (T, N) payoffs, masks, state."""
    n = 50
    cf = np.array([1.0, 1.0, 1.0, 101.0])
    rng = np.random.default_rng(3)
    payoff, state = rng.uniform(95, 105, (4, n)), rng.normal(size=(4, n))
    payoff[-1] = np.nan
    full = longstaff_schwartz(np.repeat(cf[:, None], n, axis=1), payoff, 0.99, state, exerciser="issuer")
    for c in (cf, cf[:, None]):
        res = longstaff_schwartz(c, payoff, 0.99, state, exerciser="issuer")
        assert res.cashflows.shape == (4, n) and np.array_equal(res.event, full.event)
        assert np.allclose(res.cashflows, full.cashflows)
    res = longstaff_schwartz(cf, payoff, 0.99, {"x": state, "y": state ** 2}, exerciser="issuer")
    assert res.cashflows.shape == (4, n)
    mask = rng.random((4, n)) < 0.3
    ac = autocall(cf, mask, 100.0)
    assert np.allclose(ac.cashflows, autocall(np.repeat(cf[:, None], n, axis=1), mask, 100.0).cashflows)
    with pytest.raises(ValueError, match="cashflows"):
        autocall(np.ones((4, 3)), mask, 100.0)


# ------------------------------------------------------------------ summarizer
def test_summarize_raw_table():
    rng = np.random.default_rng(4)
    dates = pd.date_range("2027-03-31", periods=4, freq="QE").date
    table = pd.DataFrame(rng.normal(1.0, 0.5, (4, 1000)), index=dates)
    s = summarize(table, confidence=0.9)
    assert np.allclose(s.fair_values, table.sum(axis=0).values)                   # vertical sums
    assert np.allclose(s.expected_cashflows["pv"].values, table.mean(axis=1).values)  # horizontal means
    assert s.price == pytest.approx(s.expected_cashflows["pv"].sum())
    se = table.sum(axis=0).std(ddof=1) / math.sqrt(1000)
    assert s.std_error == pytest.approx(se)
    lo, hi = s.confidence_interval
    assert hi - lo == pytest.approx(2 * 1.6448536 * se, rel=1e-6)
    d = s.to_dict()
    assert d["num_paths"] == 1000 and d["expected_cashflows"][0]["period"] == dates[0].isoformat()


def test_summarize_combines_results_and_antithetic():
    s, res = bermudan_put(n=10_000)
    funding = -np.full((STEPS, 10_000), 0.01) * res.survival * res.discount       # pay 1c per period while alive
    both = summarize(res, funding, antithetic=True)
    one = summarize(res, antithetic=True)
    assert both.price == pytest.approx(one.price + funding.sum(axis=0).mean())
    assert np.allclose(both.expected_cashflows["pv"].values, res.discounted.mean(axis=1) + funding.mean(axis=1))
    pairs = (one.fair_values[0::2] + one.fair_values[1::2]) / 2
    assert one.std_error == pytest.approx(pairs.std(ddof=1) / math.sqrt(pairs.size))
    with pytest.raises(ValueError, match="paths"):
        summarize(res, np.zeros((STEPS, 7)))


def test_forward_discount_factors_round_trip():
    cum = np.exp(-0.03 * np.arange(1, 6))[:, None] * np.random.default_rng(1).uniform(0.9, 1.1, (5, 8))
    assert np.allclose(np.cumprod(forward_discount_factors(cum), axis=0), cum)
    res = TerminationResult(np.ones((5, 8)), cum, np.ones((5, 8), bool), np.zeros((5, 8), bool))
    assert summarize(res).price == pytest.approx(cum.sum(axis=0).mean())
