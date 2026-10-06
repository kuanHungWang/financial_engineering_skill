"""Hull-White one-factor short-rate model.

    r(t) = x(t) + phi(t),   dx = -a x dt + sigma dW,   x(0) = 0,

with phi fitted so that E[exp(-int_0^T r)] equals the discount curve's P(0,T) (Brigo-Mercurio,
ch. 3.3). The state (x, I = int_0^t x) is Gaussian and simulated exactly on the fixing and
payment dates, so no time-discretisation bias exists. Closed forms used:

    V(tau)      = sigma^2/a^2 [tau + 2/a e^{-a tau} - 1/(2a) e^{-2 a tau} - 3/(2a)]
    B(tau)      = (1 - e^{-a tau}) / a
    D(0,t)      = P(0,t) exp(-V(t)/2 - I(t))                                path discount factor
    P(t,T)      = P(0,T)/P(0,t) exp(0.5 [V(T-t) - V(T) + V(t)] - B(T-t) x(t))
    growth(s,e) = D(0,s) / D(0,e)                                           money-market growth

Other curves (forecast curves) move with the same factor: P_c(t,T) = P_c(0,T)/P_c(0,t) times the
stochastic factor above (deterministic basis to the discount curve).

Calibration: a and sigma (or sigma alone, a held fixed) are fitted to ATM swaptions read off a
market swaption vol matrix, with QuantLib's HullWhite model, SwaptionHelper and the analytic
Jamshidian engine on the same discount curve the simulation uses (single curve: the swaptions'
floating index is projected off the discount curve).
"""
from __future__ import annotations

from typing import Literal

import numpy as np
import QuantLib as ql
from pydantic import Field, model_validator

from . import conventions as cv
from .models import BuiltModel, ModelFactory, normals
from .registry import SpecModel, register


class HullWhitePaths:
    def __init__(self, a, sigma, dates, times, x, integral, time_of):
        self.a, self.sigma = a, sigma
        self.row = {d: i for i, d in enumerate(dates)}
        self.x, self.integral = x, integral          # (len(dates), n_paths)
        self._time_of = time_of                       # date -> model time
        self._t = dict(zip(dates, times))

    def time(self, d):
        return self._t[d] if d in self._t else self._time_of(d)

    def V(self, tau):
        a, s = self.a, self.sigma
        tau = np.asarray(tau, dtype=float)
        return s * s / (a * a) * (tau + 2 / a * np.exp(-a * tau) - 0.5 / a * np.exp(-2 * a * tau) - 1.5 / a)

    def B(self, tau):
        return (1 - np.exp(-self.a * np.asarray(tau, dtype=float))) / self.a

    def _state(self, d, arr):
        if d not in self.row:
            raise KeyError(f"Hull-White state was not simulated on {d} (engine requirement missing)")
        return arr[self.row[d]]

    def state(self, dates):
        """Short-rate factor x(t) per path: (len(dates), n). r(t) = x(t) + phi(t) with phi deterministic."""
        return np.array([self._state(d, self.x) for d in dates])

    def deflator(self, dates):
        """D(0,t) / P(0,t) per path: (len(dates), n)."""
        t = np.array([self.time(d) for d in dates])
        i = np.array([self._state(d, self.integral) for d in dates])
        return np.exp(-0.5 * self.V(t)[:, None] - i)

    def bond_factor(self, t_date, maturities):
        """P(t,T) / (P(0,T)/P(0,t)) per path: (len(maturities), n)."""
        t = self.time(t_date)
        big_t = np.array([self.time(m) for m in maturities])
        tau = np.maximum(big_t - t, 0.0)
        adj = 0.5 * (self.V(tau) - self.V(big_t) + self.V(t))
        return np.exp(adj[:, None] - self.B(tau)[:, None] * self._state(t_date, self.x)[None, :])

    def growth_factor(self, start, end):
        """exp(int_s^e r) / (P(0,s)/P(0,e)) per path: (n,)."""
        ts, te = self.time(start), self.time(end)
        di = self._state(end, self.integral) - self._state(start, self.integral)
        return np.exp(0.5 * (self.V(te) - self.V(ts)) + di)


def simulate_hull_white(a, sigma, dates, times, n_paths, seed, antithetic, discount) -> HullWhitePaths:
    """Exact joint Gaussian step of (x, int x) between consecutive dates."""
    rng = np.random.default_rng(seed)
    n_t = len(times)
    x = np.zeros((n_t, n_paths))
    integral = np.zeros((n_t, n_paths))
    for i in range(n_t - 1):
        h = times[i + 1] - times[i]
        e = np.exp(-a * h)
        var_x = sigma ** 2 * (1 - e * e) / (2 * a)
        var_i = max(sigma ** 2 / a ** 2 * (h + 2 / a * e - 0.5 / a * e * e - 1.5 / a), 0.0)
        cov = sigma ** 2 / (2 * a * a) * (1 - e) ** 2
        z1, z2 = normals(rng, n_paths, antithetic), normals(rng, n_paths, antithetic)
        sx = np.sqrt(var_x)
        c = cov / sx if sx > 0 else 0.0
        x[i + 1] = x[i] * e + sx * z1
        integral[i + 1] = integral[i] + x[i] * (1 - e) / a + c * z1 + np.sqrt(max(var_i - c * c, 0.0)) * z2
    return HullWhitePaths(a, sigma, dates, times, x, integral,
                          lambda d: discount.timeFromReference(cv.to_ql(d)))


_ERROR_TYPES = {
    "relative_price": ql.BlackCalibrationHelper.RelativePriceError,
    "price": ql.BlackCalibrationHelper.PriceError,
    "implied_vol": ql.BlackCalibrationHelper.ImpliedVolError,
}


def coterminal_points(horizon_years):
    """(expiry, tenor) month pairs of swaptions into the swap ending at the trade maturity:
    annual expiries (half the maturity for trades under 2Y), each with at least one step of swap left."""
    total = max(2, round(horizon_years * 12))
    step = 12 if total >= 24 else total // 2
    return [(ql.Period(e, ql.Months), ql.Period(total - e, ql.Months)) for e in range(step, total - step + 1, step)]


def _tenor(p: ql.Period) -> str:
    """'108M' -> '9Y' for display."""
    if p.units() == ql.Months and p.length() % 12 == 0:
        return f"{p.length() // 12}Y"
    return str(ql.Period(p.length(), p.units()))


MEAN_REVERSION_STARTS = (0.01, 0.03, 0.1, 0.3)   # extra starting points when mean reversion is fitted


def calibrate_hull_white(discount, vols, points, a0, sigma0, fix_mean_reversion, error="relative_price"):
    """Fit QuantLib's HullWhite(discount, a, sigma) to ATM swaptions [(expiry, tenor)] read off the
    vol matrix, by Levenberg-Marquardt with the analytic Jamshidian engine. The mean reversion is
    held at a0 when fix_mean_reversion; otherwise the fit is restarted from a few mean reversions
    (the objective is not convex in a) and the best is kept.
    Returns (a, sigma, per-instrument fit, RMS vol error)."""
    conv = vols.conventions
    index = conv.ibor_index().clone(discount)

    def fit_from(a_start):
        model = ql.HullWhite(discount, a_start, sigma0)
        engine = ql.JamshidianSwaptionEngine(model)
        helpers = []
        for expiry, tenor in points:
            h = ql.SwaptionHelper(expiry, tenor, ql.QuoteHandle(ql.SimpleQuote(vols.vol(expiry, tenor))), index,
                                  ql.Period(conv.fixed_frequency), conv.fixed_day_count, index.dayCounter(),
                                  discount, _ERROR_TYPES[error], ql.nullDouble(), 1.0, vols.vol_type, vols.shift)
            h.setPricingEngine(engine)
            helpers.append(h)
        model.calibrate(helpers, ql.LevenbergMarquardt(1e-8, 1e-8, 1e-8),
                        ql.EndCriteria(1000, 300, 1e-8, 1e-8, 1e-8), ql.NoConstraint(), [],
                        [fix_mean_reversion, False])
        return sum(h.calibrationError() ** 2 for h in helpers), model, helpers

    starts = [a0] if fix_mean_reversion else [a0] + [x for x in MEAN_REVERSION_STARTS if x != a0]
    _, model, helpers = min((fit_from(x) for x in starts), key=lambda r: r[0])
    a, sigma = list(model.params())
    lo, hi = (1e-6, 0.5) if vols.vol_type == ql.Normal else (1e-4, 4.0)
    fit = []
    for (expiry, tenor), h in zip(points, helpers):
        market = h.volatility().value()
        try:
            implied = h.impliedVolatility(h.modelValue(), 1e-8, 1000, lo, hi)
        except RuntimeError:
            implied = float("nan")
        fit.append({"expiry": _tenor(expiry), "tenor": _tenor(tenor), "market_vol": market, "model_vol": implied,
                    "vol_error": implied - market})
    rmse = float(np.sqrt(np.mean([f["vol_error"] ** 2 for f in fit])))
    return a, sigma, fit, rmse


class _SwaptionPoint(SpecModel):
    expiry: str = Field(description="option expiry tenor, e.g. 2Y")
    tenor: str = Field(description="underlying swap tenor, e.g. 8Y")

    @model_validator(mode="after")
    def _tenors(self):
        cv.period(self.expiry), cv.period(self.tenor)
        return self


class _HullWhiteCalibration(SpecModel):
    swaption_vol: str = Field(description="name of a market_data.swaption_vols matrix")
    instruments: Literal["coterminal", "all"] | list[_SwaptionPoint] = Field(
        "coterminal", description="coterminal: annual expiries into the swap ending at the trade maturity; "
                                  "all: every quoted matrix point; or an explicit list of {expiry, tenor}")
    calibrate_mean_reversion: bool = Field(
        False, description="also fit mean_reversion (else it is held at the given value and only volatility is fit)")
    error: Literal["relative_price", "price", "implied_vol"] = Field(
        "relative_price", description="QuantLib calibration error per swaption")


@register("model", "hull_white")
class HullWhite(ModelFactory):
    description = ("One-factor Hull-White short rate dr = (theta(t) - a r) dt + sigma dW, fitted to the discount "
                   "curve. Rates only (no equity underlyings): every rate observable is stochastic and cashflows are "
                   "discounted path-wise. Other curves keep a deterministic basis to the discount curve. Exact "
                   "simulation on fixing/payment dates. Give mean_reversion and volatility, or `calibration` to fit "
                   "volatility (and optionally mean_reversion) to ATM swaptions from a market swaption vol matrix "
                   "(QuantLib HullWhite + Jamshidian engine, Levenberg-Marquardt).")
    has_european_engine = False
    has_barrier_engine = False
    stochastic_rates = True

    class Spec(SpecModel):
        discount_curve: str
        mean_reversion: float | None = Field(
            None, ge=1e-4, le=5.0,
            description="a, per year (e.g. 0.03); held fixed during calibration unless calibrate_mean_reversion "
                        "(then the starting guess, default 0.03)")
        volatility: float | None = Field(
            None, gt=0, lt=0.2,
            description="sigma, absolute short-rate vol (0.01 = 100bp/yr); with calibration only the starting guess")
        calibration: _HullWhiteCalibration | None = Field(
            None, description="fit to swaptions instead of giving the parameters")

        @model_validator(mode="after")
        def _params(self):
            if self.calibration is None:
                if self.mean_reversion is None or self.volatility is None:
                    raise ValueError("give mean_reversion and volatility, or a `calibration` block")
            elif self.mean_reversion is None and not self.calibration.calibrate_mean_reversion:
                raise ValueError("give mean_reversion (held fixed while volatility is calibrated), "
                                 "or set calibration.calibrate_mean_reversion: true")
            return self

    @staticmethod
    def underlyings(spec):
        return []

    @staticmethod
    def curves(spec):
        return [spec.discount_curve]

    @staticmethod
    def swaption_vols(spec):
        return [spec.calibration.swaption_vol] if spec.calibration else []

    @staticmethod
    def validate(spec, market, report, path):
        pass

    @staticmethod
    def build(spec, valuation_date, discount, und, horizon_years=1.0, swaption_vols=None):
        a, s = spec.mean_reversion, spec.volatility
        diag = {}
        c = spec.calibration
        if c is not None:
            vols = swaption_vols[c.swaption_vol]
            if c.instruments == "coterminal":
                points = coterminal_points(horizon_years)
            elif c.instruments == "all":
                points = list(vols.quoted)
            else:
                points = [(cv.period(p.expiry), cv.period(p.tenor)) for p in c.instruments]
            a, s, fit, rmse = calibrate_hull_white(discount, vols, points, a if a is not None else 0.03,
                                                   s if s is not None else 0.01,
                                                   not c.calibrate_mean_reversion, c.error)
            diag["calibration"] = {
                "swaption_vol": c.swaption_vol, "vol_type": "normal" if vols.vol_type == ql.Normal else "lognormal",
                "calibrated": ["mean_reversion", "volatility"] if c.calibrate_mean_reversion else ["volatility"],
                "instruments": fit, "rmse_vol": rmse}

        def rate_sim(dates, times, n, seed, anti, disc):
            return simulate_hull_white(a, s, dates, times, n, seed, anti, disc)
        return BuiltModel(name="hull_white", valuation_date=valuation_date, discount=discount,
                          processes={}, underlyings={}, rate_simulator=rate_sim, stochastic_rates=True,
                          diagnostics={"mean_reversion": a, "volatility": s, **diag})
