"""Monte Carlo path generation, tested on the simulated paths themselves (not through a trade).

* Black-Scholes, constant vol: terminal spots price vanilla calls/puts as QuantLib's analytic
  European engine does, at several maturities and strikes; paths are martingales after carry and
  log-returns have variance sigma^2 dt with independent increments.
* Hull-White: the short rate read off the simulated bonds, r(t) = -d/dT ln P(t,T) at T = t, has
  mean f(0,t) (the curve's instantaneous forward) under the t-forward measure and
  f(0,t) + sigma^2/(2a^2) (1 - e^{-at})^2 under the risk-neutral measure; Var r(t) is
  sigma^2 (1 - e^{-2at}) / (2a).
* Heston: spot paths are martingales after carry, E[ln S_T] has its closed form, and calls/puts
  across the smile match QuantLib's AnalyticHestonEngine (already with monthly steps); a vanishing
  vol of vol gives back Black-Scholes. The variance path has the CIR mean and variance, never goes
  negative (Feller condition met or violated), and Cov(ln S_T, v_T) has its closed form, which pins
  down rho. Calibration recovers the parameters that generated a vol surface.

All models are built on a humped Nelson-Siegel curve so that a wrong drift or a wrong fit to the
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
from pricer.models import BlackScholes, Heston, _simulate_process, build_underlying, calibrate_heston  # noqa: E402
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


def underlying(curve, vol=VOL):
    eq = SimpleNamespace(spot=SPOT, calendar="NullCalendar", dividend={"type": "flat", "rate": DIV},
                         vol={"type": "constant", "value": vol})
    return build_underlying("UND", eq, REF, curve)


@pytest.fixture(scope="module")
def bs(curve):
    u = underlying(curve)
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


def _check_vanillas(model, u, curve, paths, dates, moneyness, kinds=(ql.Option.Call, ql.Option.Put)):
    """DF(T) E[payoff(S_T)] from the paths vs the model's analytic European engine, every date and strike."""
    for d in dates:
        q = cv.to_ql(d)
        df = curve.discount(q)
        fwd = SPOT * u.dividend.discount(q) / df
        s_t = paths.loc[d].values
        for k in fwd * np.asarray(moneyness):
            for kind in kinds:
                pay = np.maximum(s_t - k, 0) if kind == ql.Option.Call else np.maximum(k - s_t, 0)
                mc, se = _pair_mean(df * pay)
                target = _analytic(model, d, float(k), kind)
                assert abs(mc - target) <= 4 * se, (d, k, kind, mc, target, se)
                # tight, not vacuous: < 5 cents on 100 at 200k paths, scaled by strike and path count
                assert se < 5e-4 * SPOT * math.sqrt((1 + k / SPOT) * 200_000 / s_t.size)


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
    _check_vanillas(model, u, curve, paths, BS_DATES, [0.7, 0.85, 1.0, 1.2, 1.4], kinds=(kind,))


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


# ---------------------------------------------------------------- Heston
# Feller condition 2 kappa theta > sigma^2 violated (the variance touches zero) and met.
HESTON_ROUGH = dict(v0=0.04, kappa=1.5, theta=0.06, sigma=0.6, rho=-0.7)
HESTON_SMOOTH = dict(v0=0.03, kappa=2.0, theta=0.05, sigma=0.3, rho=-0.5)
HESTON_DATES = [dt.date(2027, 9, 28), dt.date(2028, 9, 28)]
HESTON_PATHS = 100_000


def heston(curve, params):
    spec = Heston.Spec(underlying="UND", discount_curve="NS", params=params)
    return Heston.build(spec, VAL, curve, {"UND": underlying(curve)})


@pytest.fixture(scope="module")
def heston_spot(curve):
    """Spot paths as the engine simulates them (weekly refined grid, the default)."""
    model = heston(curve, HESTON_ROUGH)
    sc = model.simulate({"UND": HESTON_DATES}, [], HESTON_PATHS, seed=7, antithetic=True)
    return model, model.underlyings["UND"], sc.equity["UND"]


def _expected_integrated_variance(p, t):
    """int_0^t E[v_s] ds."""
    k = p["kappa"]
    return p["theta"] * t + (p["v0"] - p["theta"]) * (1 - np.exp(-k * t)) / k


def test_heston_spot_paths_are_martingales_after_carry(heston_spot, curve):
    _, u, paths = heston_spot
    for d in HESTON_DATES:
        q = cv.to_ql(d)
        fwd = SPOT * u.dividend.discount(q) / curve.discount(q)
        m, se = _pair_mean(paths.loc[d].values)
        assert abs(m - fwd) <= 4 * se, (d, m, fwd, se)


def test_heston_mean_log_spot_has_its_closed_form(heston_spot, curve):
    """E[ln S_T] = ln F(T) - 1/2 int_0^T E[v_s] ds: checks kappa, theta and v0 in the variance drift."""
    _, u, paths = heston_spot
    for d in HESTON_DATES:
        q = cv.to_ql(d)
        fwd = SPOT * u.dividend.discount(q) / curve.discount(q)
        target = math.log(fwd) - 0.5 * _expected_integrated_variance(HESTON_ROUGH, curve.timeFromReference(q))
        m, se = _pair_mean(np.log(paths.loc[d].values))
        assert abs(m - target) <= 4 * se, (d, m, target, se)


def test_heston_paths_price_the_smile_like_analytic_heston(heston_spot, curve):
    """Calls and puts from 60% to 150% of the forward. With rho = -0.7 and vol of vol 0.6 the smile is
    steep, so a wrong sign of rho or a wrong vol of vol fails in the wings."""
    model, u, paths = heston_spot
    _check_vanillas(model, u, curve, paths, HESTON_DATES, [0.6, 0.8, 1.0, 1.25, 1.5])


def test_heston_smile_is_far_from_black_scholes(heston_spot, curve):
    """Guard for the previous test: the 60% and 150% strikes are many standard errors away from
    Black-Scholes at the at-the-money implied vol, so matching AnalyticHeston there means something."""
    model, u, paths = heston_spot
    d = HESTON_DATES[0]
    q = cv.to_ql(d)
    df = curve.discount(q)
    fwd = SPOT * u.dividend.discount(q) / df
    process = model.processes["UND"]

    def bs_price(k, kind, vol):
        opt = ql.EuropeanOption(ql.PlainVanillaPayoff(kind, k), ql.EuropeanExercise(q))
        flat = ql.BlackVolTermStructureHandle(ql.BlackConstantVol(REF, ql.NullCalendar(), vol, ql.Actual365Fixed()))
        opt.setPricingEngine(ql.AnalyticEuropeanEngine(ql.BlackScholesMertonProcess(
            process.s0(), process.dividendYield(), process.riskFreeRate(), flat)))
        return opt.NPV()

    atm = ql.EuropeanOption(ql.PlainVanillaPayoff(ql.Option.Put, fwd), ql.EuropeanExercise(q))
    atm.setPricingEngine(model.european_engine("UND"))
    atm_vol = atm.impliedVolatility(atm.NPV(), ql.BlackScholesMertonProcess(
        process.s0(), process.dividendYield(), process.riskFreeRate(), underlying(curve).vol))
    s_t = paths.loc[d].values
    for k, kind in ((0.6 * fwd, ql.Option.Put), (1.5 * fwd, ql.Option.Call)):   # out of the money
        _, se = _pair_mean(df * np.maximum(kind * (s_t - k), 0))
        assert abs(_analytic(model, d, k, kind) - bs_price(k, kind, atm_vol)) > 20 * se, (k, se)


def test_heston_monthly_steps_already_agree_with_analytic(curve):
    """QuantLib's default scheme (quadratic-exponential, martingale-corrected) has no visible time-step
    bias even on a monthly grid, so the engine's weekly default leaves a wide margin."""
    model = heston(curve, HESTON_ROUGH)
    sc = model.simulate({"UND": HESTON_DATES}, [], HESTON_PATHS, seed=8, antithetic=True, steps_per_year=12)
    _check_vanillas(model, model.underlyings["UND"], curve, sc.equity["UND"], HESTON_DATES,
                    [0.6, 0.8, 1.0, 1.25, 1.5], kinds=(ql.Option.Call,))


def test_heston_with_vanishing_vol_of_vol_is_black_scholes(curve):
    """sigma -> 0 and v0 = theta: constant variance, so paths price vanillas like Black-Scholes at sqrt(v0)."""
    vol = 0.25
    model = heston(curve, dict(v0=vol ** 2, kappa=1.0, theta=vol ** 2, sigma=1e-4, rho=-0.5))
    sc = model.simulate({"UND": BS_DATES}, [], 50_000, seed=9, antithetic=True, steps_per_year=12)
    bs_model = BlackScholes.build(BlackScholes.Spec(underlying="UND", discount_curve="NS"), VAL, curve,
                                  {"UND": underlying(curve, vol)})
    _check_vanillas(bs_model, model.underlyings["UND"], curve, sc.equity["UND"], BS_DATES, [0.7, 1.0, 1.4])


# ---------------------------------------------------------------- Heston variance path
VAR_TIMES = np.linspace(0.0, 2.0, 53)                  # every two weeks to 2Y
VAR_PATHS = 50_000


@pytest.fixture(scope="module", params=[HESTON_ROUGH, HESTON_SMOOTH], ids=["feller_violated", "feller_met"])
def heston_states(request, curve):
    """(params, ln S paths, v paths), each (len(VAR_TIMES), VAR_PATHS), independent normals."""
    p = request.param
    out = _simulate_process(heston(curve, p).processes["UND"], list(VAR_TIMES), VAR_PATHS, seed=5,
                            antithetic=False, all_states=True)
    return p, np.log(out[0]), out[1]


def _cir_mean(p, t):
    return p["theta"] + (p["v0"] - p["theta"]) * np.exp(-p["kappa"] * t)


def _cir_var(p, t):
    k, th, s, v0 = p["kappa"], p["theta"], p["sigma"], p["v0"]
    return v0 * s * s / k * (np.exp(-k * t) - np.exp(-2 * k * t)) + th * s * s / (2 * k) * (1 - np.exp(-k * t)) ** 2


def test_heston_variance_starts_at_v0_and_is_never_negative(heston_states):
    p, _, v = heston_states
    assert np.all(v[0] == p["v0"])
    assert np.all(v >= 0)
    feller = 2 * p["kappa"] * p["theta"] > p["sigma"] ** 2
    near_zero = (v[1:] < 1e-4).mean()
    assert (near_zero < 1e-3) if feller else (near_zero > 1e-3), near_zero   # the cases really differ


def test_heston_variance_mean_reverts_to_theta(heston_states):
    """E[v_t] = theta + (v0 - theta) e^{-kappa t} on every date."""
    p, _, v = heston_states
    m = v[1:].mean(1)
    se = v[1:].std(1, ddof=1) / math.sqrt(VAR_PATHS)
    z = np.abs(m - _cir_mean(p, VAR_TIMES[1:])) / se
    assert np.all(z <= 4), z.max()


def test_heston_variance_has_the_cir_variance(heston_states):
    """Var v_t = v0 s^2/k (e^{-kt} - e^{-2kt}) + theta s^2/(2k) (1 - e^{-kt})^2: checks the vol of vol."""
    p, _, v = heston_states
    c = v[1:] - v[1:].mean(1, keepdims=True)
    var = (c ** 2).mean(1)
    se = np.sqrt((c ** 4).mean(1) - var ** 2) / math.sqrt(VAR_PATHS)
    z = np.abs(var - _cir_var(p, VAR_TIMES[1:])) / se
    assert np.all(z <= 4), z.max()


def test_heston_spot_variance_covariance_pins_down_rho(heston_states):
    """Cov(ln S_T, v_T) = rho sigma int_0^T e^{-k(T-s)} E[v_s] ds - 1/2 int_0^T e^{-k(T-s)} Var(v_s) ds."""
    p, x, v = heston_states
    k = p["kappa"]
    for i in (26, 52):                                 # 1Y and 2Y
        t_end = VAR_TIMES[i]
        s = np.linspace(0.0, t_end, 4001)
        w = np.exp(-k * (t_end - s))
        integral = lambda y: float(np.sum((y[1:] + y[:-1]) * np.diff(s)) / 2)  # noqa: E731
        target = p["rho"] * p["sigma"] * integral(w * _cir_mean(p, s)) - 0.5 * integral(w * _cir_var(p, s))
        prod = (x[i] - x[i].mean()) * (v[i] - v[i].mean())
        se = prod.std(ddof=1) / math.sqrt(VAR_PATHS)
        assert abs(prod.mean() - target) <= 4 * se, (t_end, prod.mean(), target, se)
        assert abs(target) > 10 * se                   # the rho term is resolved, not lost in the noise


# ---------------------------------------------------------------- Heston calibration
def test_heston_calibration_recovers_the_parameters_behind_a_surface(curve):
    """Vols generated by AnalyticHeston with known parameters, then fitted from the default guess."""
    u = underlying(curve)
    spot = ql.QuoteHandle(ql.SimpleQuote(SPOT))
    truth = ql.HestonModel(ql.HestonProcess(curve, u.dividend, spot, *HESTON_ROUGH.values()))
    engine = ql.AnalyticHestonEngine(truth)
    quotes = []
    for tenor in ("3M", "6M", "1Y", "2Y", "3Y"):
        for k in (70.0, 80.0, 90.0, 100.0, 110.0, 120.0, 130.0):
            h = ql.HestonModelHelper(ql.Period(tenor), ql.NullCalendar(), SPOT, k,
                                     ql.QuoteHandle(ql.SimpleQuote(0.2)), curve, u.dividend)
            h.setPricingEngine(engine)
            quotes.append((ql.Period(tenor), k, h.impliedVolatility(h.modelValue(), 1e-12, 5000, 1e-3, 4.0)))
    _, model, rmse = calibrate_heston(curve, u.dividend, ql.NullCalendar(), SPOT, quotes)
    fitted = dict(v0=model.v0(), kappa=model.kappa(), theta=model.theta(), sigma=model.sigma(), rho=model.rho())
    assert rmse < 1e-3
    for name, value in HESTON_ROUGH.items():
        assert fitted[name] == pytest.approx(value, rel=1e-2), (name, fitted)
