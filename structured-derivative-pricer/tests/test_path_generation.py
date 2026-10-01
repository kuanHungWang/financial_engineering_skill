"""Monte Carlo path generation, tested on the simulated paths themselves (not through a trade).

* Black-Scholes, constant vol: terminal spots price vanilla calls/puts as QuantLib's analytic
  European engine does, at several maturities and strikes; paths are martingales after carry and
  log-returns have variance sigma^2 dt with independent increments.
* Hull-White: the short rate read off the simulated bonds, r(t) = -d/dT ln P(t,T) at T = t, has
  mean f(0,t) (the curve's instantaneous forward) under the t-forward measure and
  f(0,t) + sigma^2/(2a^2) (1 - e^{-at})^2 under the risk-neutral measure; Var r(t) is
  sigma^2 (1 - e^{-2at}) / (2a).

Both models are built on a humped Nelson-Siegel curve so that a wrong drift or a wrong fit to the
curve cannot hide behind a flat term structure. Run:  python -m pytest tests -q
"""
import datetime as dt
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import QuantLib as ql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pricer import conventions as cv  # noqa: E402
from pricer.models import BlackScholes, build_underlying  # noqa: E402
from pricer.observables import RATES  # noqa: E402
from pricer.rates import HullWhite  # noqa: E402

VAL = dt.date(2026, 9, 28)
REF = cv.to_ql(VAL)


# ---------------------------------------------------------------- a curve with a known forward
NS = dict(b0=0.045, b1=-0.02, b2=0.03, tau=2.0)


def ns_forward(t):
    """Instantaneous forward f(0,t) of the Nelson-Siegel curve."""
    e = np.exp(-np.asarray(t, dtype=float) / NS["tau"])
    return NS["b0"] + NS["b1"] * e + NS["b2"] * np.asarray(t) / NS["tau"] * e


def ns_integral(t):
    """int_0^t f(0,s) ds, so P(0,t) = exp(-ns_integral(t))."""
    tau, t = NS["tau"], np.asarray(t, dtype=float)
    e = np.exp(-t / tau)
    return NS["b0"] * t + (NS["b1"] + NS["b2"]) * tau * (1 - e) - NS["b2"] * t * e


@pytest.fixture(scope="module")
def curve():
    """P(0,t) = exp(-int f) sampled daily to 12Y, Actual365Fixed (the simulation time axis)."""
    ql.Settings.instance().evaluationDate = REF
    dates = [REF + i for i in range(0, 12 * 366)]
    dfs = [math.exp(-ns_integral(i / 365.0)) for i in range(len(dates))]
    c = ql.DiscountCurve(dates, dfs, ql.Actual365Fixed())
    c.enableExtrapolation()
    return ql.YieldTermStructureHandle(c)


# ---------------------------------------------------------------- Black-Scholes, constant vol
SPOT, DIV, VOL = 100.0, 0.02, 0.25
BS_DATES = [dt.date(2026, 12, 28), dt.date(2027, 9, 28), dt.date(2028, 9, 28)]
BS_PATHS = 200_000


@pytest.fixture(scope="module")
def bs(curve):
    eq = SimpleNamespace(spot=SPOT, calendar="NullCalendar", dividend={"type": "flat", "rate": DIV},
                         vol={"type": "constant", "value": VOL})
    u = build_underlying("UND", eq, REF, curve)
    spec = BlackScholes.Spec(underlying="UND", discount_curve="NS")
    model = BlackScholes.build(spec, VAL, curve, {"UND": u})
    sc = model.simulate({"UND": BS_DATES}, [], BS_PATHS, seed=7, antithetic=True)
    return model, u, sc.equity["UND"]


def _pair_mean(x):
    """Mean and standard error of an antithetic estimator (pairs sit in adjacent columns)."""
    p = 0.5 * (x[..., 0::2] + x[..., 1::2])
    return p.mean(-1), p.std(-1, ddof=1) / math.sqrt(p.shape[-1])


def _analytic(model, d, strike, kind):
    opt = ql.EuropeanOption(ql.PlainVanillaPayoff(kind, strike), ql.EuropeanExercise(cv.to_ql(d)))
    opt.setPricingEngine(model.european_engine("UND"))
    return opt.NPV()


def test_bs_paths_start_at_spot_and_sit_on_the_requested_dates(bs):
    _, _, paths = bs
    assert list(paths.index) == BS_DATES
    assert paths.shape == (len(BS_DATES), BS_PATHS)
    assert np.all(np.isfinite(paths.values)) and np.all(paths.values > 0)


def test_bs_paths_are_martingales_after_carry(bs, curve):
    """E[S_t] = S0 Q(0,t) / P(0,t): the drift follows the (non-flat) discount curve."""
    _, u, paths = bs
    for d in BS_DATES:
        q = cv.to_ql(d)
        fwd = SPOT * u.dividend.discount(q) / curve.discount(q)
        m, se = _pair_mean(paths.loc[d].values)
        assert abs(m - fwd) <= 4 * se + 1e-12, (d, m, fwd, se)


def test_bs_log_returns_have_constant_vol_and_independent_increments(bs, curve):
    _, _, paths = bs
    x = np.log(np.vstack([np.full(BS_PATHS, SPOT), paths.values]))
    t = np.array([0.0] + [curve.timeFromReference(cv.to_ql(d)) for d in BS_DATES])
    rets = np.diff(x, axis=0)
    n_eff = BS_PATHS // 2                               # antithetic twins give the same squared return
    for i, dt_ in enumerate(np.diff(t)):
        sd = rets[i].std(ddof=1)
        target = VOL * math.sqrt(dt_)
        assert abs(sd / target - 1) <= 4 / math.sqrt(2 * n_eff), (i, sd, target)
    for i in range(len(rets) - 1):
        rho = np.corrcoef(rets[i], rets[i + 1])[0, 1]
        assert abs(rho) <= 4 / math.sqrt(n_eff), (i, rho)


@pytest.mark.parametrize("kind", [ql.Option.Call, ql.Option.Put], ids=["call", "put"])
def test_bs_paths_price_vanillas_like_quantlib_analytic_engine(bs, curve, kind):
    """DF(T) E[(S_T - K)^+] from the simulated paths vs AnalyticEuropeanEngine on the same process,
    for every simulated maturity and strikes from 70% to 140% of the forward."""
    model, u, paths = bs
    for d in BS_DATES:
        q = cv.to_ql(d)
        df = curve.discount(q)
        fwd = SPOT * u.dividend.discount(q) / df
        s_t = paths.loc[d].values
        for k in fwd * np.array([0.7, 0.85, 1.0, 1.2, 1.4]):
            pay = np.maximum(s_t - k, 0) if kind == ql.Option.Call else np.maximum(k - s_t, 0)
            mc, se = _pair_mean(df * pay)
            target = _analytic(model, d, float(k), kind)
            assert abs(mc - target) <= 4 * se, (d, k, mc, target, se)
            assert se < 5e-4 * SPOT                     # the check is tight (< 5 cents on 100), not vacuous


# ---------------------------------------------------------------- Hull-White mean path
A, SIGMA = 0.05, 0.015
HW_DATES = [VAL + dt.timedelta(days=round(365 * m / 12)) for m in range(1, 121)]   # monthly to 10Y
HW_PATHS = 50_000
DELTA = 1                                                                          # days, for -d/dT ln P


@pytest.fixture(scope="module")
def hw(curve):
    """Short rate r(t) and path discount factor D(0,t) on every date, independent normals (no
    antithetics: with them the mean of x(t) would be zero by construction)."""
    spec = HullWhite.Spec(discount_curve="NS", mean_reversion=A, volatility=SIGMA)
    model = HullWhite.build(spec, VAL, curve, {})
    model.curves = {"NS": curve}
    sc = model.simulate({RATES: HW_DATES}, [], HW_PATHS, seed=3, antithetic=False)
    h = DELTA / 365.0
    r = np.array([-np.log(sc.bond("NS", d, [d + dt.timedelta(days=DELTA)])[0]) / h for d in HW_DATES])
    t = np.array([curve.timeFromReference(cv.to_ql(d)) for d in HW_DATES])
    p0 = np.array([curve.discount(cv.to_ql(d)) for d in HW_DATES])
    return t, r, sc.discount(HW_DATES), p0


def _mean_se(x):
    return x.mean(-1), x.std(-1, ddof=1) / math.sqrt(x.shape[-1])


def _convexity(t):
    return SIGMA ** 2 / (2 * A ** 2) * (1 - np.exp(-A * t)) ** 2


def test_hw_curve_has_the_nelson_siegel_forward(curve):
    """Sanity check of the fixture: the QuantLib curve's instantaneous forward is the analytic one."""
    for t in (0.5, 2.0, 5.0, 10.0):
        f = curve.forwardRate(t, t, ql.Continuous, ql.NoFrequency, True).rate()
        assert f == pytest.approx(float(ns_forward(t)), abs=2e-6)


def test_hw_discount_factors_reprice_the_curve(hw):
    t, _, disc, p0 = hw
    m, se = _mean_se(disc)
    assert np.all(np.abs(m - p0) <= 4 * se), np.max(np.abs(m - p0) / se)


def test_hw_mean_short_rate_under_forward_measure_is_the_instantaneous_forward(hw):
    """E^{Q^t}[r(t)] = E[D(0,t) r(t)] / P(0,t) = f(0,t) on every date out to 10Y."""
    t, r, disc, p0 = hw
    m, se = _mean_se(disc * r / p0[:, None])
    f = ns_forward(t)                               # the 1-day bond adds O(delta^2) only
    dev = np.abs(m - f)
    assert np.all(dev <= 4 * se + 2e-5), (t[dev.argmax()], m[dev.argmax()], f[dev.argmax()], se[dev.argmax()])


def test_hw_risk_neutral_mean_path_is_forward_plus_convexity(hw):
    """E^Q[r(t)] = f(0,t) + sigma^2/(2a^2)(1 - e^{-at})^2; at 10Y the gap is resolved by the MC,
    so this would fail if the model were fitted to f(0,t) without the convexity term."""
    t, r, _, _ = hw
    m, se = _mean_se(r)
    target = ns_forward(t) + _convexity(t)
    dev = np.abs(m - target)
    assert np.all(dev <= 4 * se + 2e-5), (t[dev.argmax()], m[dev.argmax()], target[dev.argmax()])
    assert _convexity(t[-1]) > 10 * se[-1]
    assert np.abs(m[-1] - ns_forward(t[-1])) > 6 * se[-1]


def test_hw_short_rate_variance(hw):
    t, r, _, _ = hw
    var = r.var(-1, ddof=1)
    target = SIGMA ** 2 * (1 - np.exp(-2 * A * t)) / (2 * A)
    assert np.all(np.abs(var / target - 1) <= 4 * math.sqrt(2 / HW_PATHS))
