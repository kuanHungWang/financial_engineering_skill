"""Model components: turn market data into processes that simulate the state observables read
(underlying fixings, short-rate state) and provide discount factors, plus QuantLib analytic
engines when a closed form exists.

Processes, Heston calibration and analytic engines are QuantLib's. Black-Scholes path generation
is done here, vectorised over paths, because the engine needs:
  * antithetic variates,
  * correlated Brownian drivers across several underlyings,
  * one refined time grid shared by every underlying, on the Actual365Fixed axis the curves
    are re-expressed on upstream.

Black-Scholes paths come from a vectorised log-Euler scheme in numpy. The drift comes from the
curves' discount-factor ratios, so each step is exactly martingale. The vol is the forward Black
vol (constant / term) or QuantLib's Dupire local vol tabulated on a strike grid per step. Heston
uses QuantLib's GaussianMultiPathGenerator. Hull-White (rates.py) is simulated exactly.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import QuantLib as ql
from pydantic import Field, model_validator

from . import conventions as cv
from .observables import RATES
from .registry import SpecModel, register


@dataclass
class UnderlyingData:
    name: str
    spot: float
    dividend: ql.YieldTermStructureHandle
    vol: ql.BlackVolTermStructureHandle
    vol_factory: type
    vol_spec: object
    calendar: ql.Calendar


@dataclass
class Scenario:
    """Simulated state. Observables, legs and discounting read the market only through this.

    Arrays have one column per path, or a single column when the quantity is deterministic."""
    valuation_date: dt.date
    n_paths: int
    discount_curve: ql.YieldTermStructureHandle
    curves: dict                                     # market curve name -> handle (forecast curves)
    equity: dict = field(default_factory=dict)       # underlying -> DataFrame (dates x paths)
    rates: object = None                             # rates.HullWhitePaths, or None for deterministic rates

    def discount(self, dates) -> np.ndarray:
        """Discount factor from the valuation date to each date: (len(dates), n_paths or 1)."""
        base = np.array([self.discount_curve.discount(cv.to_ql(d)) for d in dates])[:, None]
        return base if self.rates is None else base * self.rates.deflator(dates)

    def bond(self, curve, t, maturities) -> np.ndarray:
        """P(t, T) on `curve` as seen at t, for each T: (len(maturities), n_paths or 1).
        Stochastic rates move every curve with the model's factor (deterministic basis)."""
        c, qt = self.curves[curve], cv.to_ql(t)
        base = np.array([c.discount(cv.to_ql(T)) / c.discount(qt) for T in maturities])[:, None]
        return base if self.rates is None else base * self.rates.bond_factor(t, maturities)

    def growth(self, curve, start, end) -> np.ndarray:
        """Money-market growth exp(int r) from start to end on `curve`: (n_paths,) or (1,)."""
        c = self.curves[curve]
        base = c.discount(cv.to_ql(start)) / c.discount(cv.to_ql(end))
        return np.array([base]) if self.rates is None else base * self.rates.growth_factor(start, end)


@dataclass
class BuiltModel:
    """What downstream components may rely on, whatever the model type."""
    name: str
    valuation_date: dt.date
    discount: ql.YieldTermStructureHandle
    processes: dict                      # underlying -> ql process (analytic engines, diagnostics)
    underlyings: dict                    # underlying -> UnderlyingData
    joint_simulator: object = None       # f(grid, times, n, seed, antithetic) -> {underlying: ndarray}
    rate_simulator: object = None        # f(dates, times, n, seed, antithetic, discount) -> HullWhitePaths
    stochastic_rates: bool = False
    european_engine: object = None       # callable(underlying) -> ql.PricingEngine | None
    barrier_engine: object = None
    diagnostics: dict = field(default_factory=dict)
    curves: dict = field(default_factory=dict)   # market curves by name, attached by the engine

    def discount_factors(self, dates) -> pd.DataFrame:
        """Deterministic discounting from today's curve: shape (len(dates), 1)."""
        return pd.DataFrame([self.discount.discount(cv.to_ql(d)) for d in dates], index=list(dates))

    def simulate(self, requirements: dict, pay_dates, n_paths: int, seed: int, antithetic: bool,
                 steps_per_year: int = 52) -> Scenario:
        """Simulate what `requirements` ({underlying | RATES: dates}) asks for.

        Equity grids get extra dates so that no step exceeds 1/steps_per_year, which keeps
        local-vol and Heston discretisation bias small when observations are sparse. The
        short-rate state is simulated exactly, on the requested dates plus the payment dates."""
        sc = Scenario(self.valuation_date, n_paths, self.discount, self.curves)
        eq_dates = set()
        for u in self.underlyings:
            eq_dates.update(requirements.get(u, []))
        if eq_dates:
            grid = _refined_grid(eq_dates | {self.valuation_date}, steps_per_year)
            times = [self.discount.timeFromReference(cv.to_ql(d)) for d in grid]
            paths = self.joint_simulator(grid, times, n_paths, seed, antithetic)
            want = sorted(eq_dates)
            sc.equity = {u: pd.DataFrame(paths[u], index=grid).loc[want] for u in self.underlyings}
        if self.stochastic_rates:
            dates = sorted(set(requirements.get(RATES, [])) | set(pay_dates) | {self.valuation_date})
            times = [self.discount.timeFromReference(cv.to_ql(d)) for d in dates]
            sc.rates = self.rate_simulator(dates, times, n_paths, seed + 104729, antithetic, self.discount)
        return sc


def _refined_grid(dates, steps_per_year):
    grid = set(dates)
    if steps_per_year > 0:
        gap = max(1, int(365 / steps_per_year))
        pts = sorted(grid)
        for a, b in zip(pts[:-1], pts[1:]):
            d = a + dt.timedelta(days=gap)
            while d < b:
                grid.add(d)
                d += dt.timedelta(days=gap)
    return sorted(grid)


def normals(rng, n_paths, antithetic):
    """Standard normals for one step; antithetic pairs sit in adjacent columns (0,1), (2,3), ..."""
    if not antithetic:
        return rng.standard_normal(n_paths)
    z = rng.standard_normal(n_paths // 2)
    out = np.empty(n_paths)
    out[0::2], out[1::2] = z, -z
    return out


def black_scholes_stepper(u: "UnderlyingData", discount, local_vol=None, n_strikes=161):
    """Log-Euler step for dS/S = (r - q) dt + sigma(t, S) dW, prepared once per time grid.

    local_vol: a ql.LocalVolTermStructure (surface case) or None (deterministic forward vol).
    Returns prepare(grid, times) -> step(i, log_spot, z) -> log_spot at times[i + 1]."""
    def prepare(grid, times):
        qd = [cv.to_ql(d) for d in grid]
        times = np.asarray(times)
        dts = np.diff(times)
        ln_dr = np.log([discount.discount(q) for q in qd])
        ln_dq = np.log([u.dividend.discount(q) for q in qd])
        drift = -np.diff(ln_dr) + np.diff(ln_dq)                    # (r - q) dt, exact per step
        if local_vol is None:
            w = np.array([u.vol.blackVariance(float(t), u.spot, True) for t in times])
            fwd_var = np.maximum(np.diff(w), 0.0)                   # sigma^2 dt per step
        else:
            ln_k = np.linspace(np.log(0.05 * u.spot), np.log(5.0 * u.spot), n_strikes)
            ks = np.exp(ln_k)

        def step(i, x, z):
            if local_vol is None:
                var = fwd_var[i]
            else:
                t = max(float(times[i]), 1e-4)
                row = np.array([local_vol.localVol(t, float(k), True) for k in ks])
                var = np.interp(x, ln_k, row) ** 2 * dts[i]
            return x + drift[i] - 0.5 * var + np.sqrt(var) * z
        return step
    return prepare


def correlated_simulator(names, spots, preparers, factor=None):
    """Joint simulation of several log-Euler assets. `factor` F (F F^T = correlation) mixes one
    independent normal per asset and step; None means a single asset."""
    def simulate(grid, times, n_paths, seed, antithetic):
        steps = [prep(grid, times) for prep in preparers]
        rng = np.random.default_rng(seed)
        out = {nm: np.empty((len(times), n_paths)) for nm in names}
        xs = [np.full(n_paths, np.log(s)) for s in spots]
        for nm, s in zip(names, spots):
            out[nm][0] = s
        for i in range(len(times) - 1):
            eps = np.array([normals(rng, n_paths, antithetic) for _ in names])
            z = eps if factor is None else factor @ eps
            for j, nm in enumerate(names):
                xs[j] = steps[j](i, xs[j], z[j])
                out[nm][i + 1] = np.exp(xs[j])
        return out
    return simulate


def _simulate_process(process, times, n_paths, seed, antithetic, all_states=False) -> np.ndarray:
    """Generic QuantLib path generator. Returns the first state variable (the asset), shape
    (len(times), n_paths), or with all_states every state variable, shape (size, len(times), n_paths)
    (Heston: spot, variance)."""
    n_steps = len(times) - 1
    n_t = len(times)
    states = process.size() if all_states else 1
    out = np.empty((states, n_t, n_paths))
    dim = process.factors()
    rsg = ql.GaussianRandomSequenceGenerator(
        ql.UniformRandomSequenceGenerator(dim * n_steps, ql.UniformRandomGenerator(seed)))
    gen = ql.GaussianMultiPathGenerator(process, list(times), rsg, False)
    step = 2 if antithetic else 1

    def store(j, multipath):
        for k in range(states):
            out[k, :, j] = np.fromiter(multipath[k], float, n_t)
    for j in range(0, n_paths, step):
        store(j, gen.next().value())
        if antithetic:
            store(j + 1, gen.antithetic().value())
    return out if all_states else out[0]


def build_underlying(name, eq, ref: ql.Date, discount: ql.YieldTermStructureHandle) -> UnderlyingData:
    """Dividend and vol are built on the discount curve's day counter so that the
    simulation time axis, the drift and the variance all agree."""
    from . import registry
    dc = discount.dayCounter()
    cal = cv.calendar(eq.calendar)
    dcls = registry.get("curve", eq.dividend["type"])
    dspec = dcls.Spec.model_validate({k: v for k, v in eq.dividend.items() if k != "type"})
    if eq.dividend["type"] == "flat":
        dividend = ql.YieldTermStructureHandle(ql.FlatForward(ref, dspec.rate, dc, ql.Continuous))
    else:
        dividend = dcls.build(dspec, ref)
    vcls = registry.get("vol", eq.vol["type"])
    vspec = vcls.Spec.model_validate({k: v for k, v in eq.vol.items() if k != "type"})
    vol = vcls.build(vspec, ref, dc, eq.spot, cal)
    return UnderlyingData(name, eq.spot, dividend, vol, vcls, vspec, cal)


def local_vol_health(u: UnderlyingData, discount, horizon_years, n=30) -> float:
    """Fraction of a (time x strike) grid where Dupire local variance is illegal.
    Grid: t in (0, horizon], strikes 50%..200% of spot, where paths actually travel."""
    lv = ql.LocalVolSurface(u.vol, discount, u.dividend, ql.QuoteHandle(ql.SimpleQuote(u.spot)))
    bad = 0
    for t in np.linspace(horizon_years / n, horizon_years, n):
        for k in np.linspace(0.5, 2.0, n) * u.spot:
            try:
                x = lv.localVol(float(t), float(k), True)
                bad += not np.isfinite(x) or x <= 0
            except RuntimeError:
                bad += 1
    return bad / n ** 2


def correlation_factor(m) -> np.ndarray:
    """F with F F^T = m, from the eigen-decomposition (works for singular matrices, e.g. rho = 1)."""
    w, vecs = np.linalg.eigh(np.asarray(m, dtype=float))
    return vecs @ np.diag(np.sqrt(np.clip(w, 0.0, None)))


class ModelFactory:
    """underlyings(spec) -> [names]; curves(spec) -> [names];
    validate(spec, market, report, path); build(spec, ...) -> BuiltModel"""
    stochastic_rates = False


@register("model", "black_scholes")
class BlackScholes(ModelFactory):
    description = ("Black-Scholes-Merton on one or several equity underlyings, deterministic rates. With a vol "
                   "surface each underlying follows QuantLib's Dupire local vol; with constant/term vol, plain GBM. "
                   "Several underlyings need an explicit `correlation` matrix. Rate observables are "
                   "deterministic forwards. Analytic engines: European vanilla, single barrier (continuous).")
    has_european_engine = True
    has_barrier_engine = True

    class Spec(SpecModel):
        underlying: str | None = Field(None, description="the single underlying (or use `underlyings`)")
        underlyings: list[str] | None = Field(None, description="several underlyings, simulated jointly")
        correlation: list[list[float]] | None = Field(
            None, description="correlation of the Brownian drivers, rows/columns in `underlyings` order")
        discount_curve: str

        @model_validator(mode="after")
        def _shape(self):
            if (self.underlying is None) == (self.underlyings is None):
                raise ValueError("give exactly one of `underlying` or `underlyings`")
            if self.underlyings is None:
                if self.correlation is not None:
                    raise ValueError("`correlation` needs `underlyings`")
                return self
            n = len(self.underlyings)
            if n == 0 or len(set(self.underlyings)) != n:
                raise ValueError("`underlyings` must be non-empty and distinct")
            if self.correlation is None:
                if n > 1:
                    raise ValueError("`correlation` is required with several underlyings (it is never assumed)")
                return self
            m = np.asarray(self.correlation, dtype=float)
            if m.shape != (n, n):
                raise ValueError(f"`correlation` must be {n}x{n}, got {m.shape[0]}x{m.shape[1] if m.ndim > 1 else 0}")
            if not np.allclose(m, m.T, atol=1e-10) or not np.allclose(np.diag(m), 1.0) or np.abs(m).max() > 1:
                raise ValueError("`correlation` must be symmetric with unit diagonal and entries in [-1, 1]")
            if np.linalg.eigvalsh(m).min() < -1e-10:
                raise ValueError("`correlation` is not positive semi-definite")
            return self

    @staticmethod
    def underlyings(spec):
        return [spec.underlying] if spec.underlying else list(spec.underlyings)

    @staticmethod
    def curves(spec):
        return [spec.discount_curve]

    @staticmethod
    def validate(spec, market, report, path):
        pass

    @classmethod
    def build(cls, spec, valuation_date, discount, und: dict[str, UnderlyingData], horizon_years=1.0):
        names = cls.underlyings(spec)
        procs, preps, diag = {}, [], {}
        for name in names:
            u = und[name]
            if u.vol_factory.type_name == "surface":
                # Dupire local vol from a quoted surface can have illegal (negative variance) points in
                # the wings; NoExceptLocalVolSurface floors them instead of aborting the simulation.
                atm = u.vol.blackVol(1.0, u.spot, True)
                spot_h = ql.QuoteHandle(ql.SimpleQuote(u.spot))
                lv = ql.NoExceptLocalVolSurface(u.vol, discount, u.dividend, spot_h, atm)
                procs[name] = ql.GeneralizedBlackScholesProcess(spot_h, u.dividend, discount, u.vol,
                                                                ql.LocalVolTermStructureHandle(lv))
                diag.setdefault("local_vol", {})[name] = {
                    "illegal_fraction": local_vol_health(u, discount, horizon_years), "floor": atm}
                preps.append(black_scholes_stepper(u, discount, lv))
            else:
                procs[name] = ql.BlackScholesMertonProcess(ql.QuoteHandle(ql.SimpleQuote(u.spot)),
                                                           u.dividend, discount, u.vol)
                preps.append(black_scholes_stepper(u, discount))
        factor = correlation_factor(spec.correlation) if spec.correlation is not None else None
        return BuiltModel(
            name="black_scholes", valuation_date=valuation_date, discount=discount,
            processes=procs, underlyings={n: und[n] for n in names},
            joint_simulator=correlated_simulator(names, [und[n].spot for n in names], preps, factor),
            european_engine=lambda name: ql.AnalyticEuropeanEngine(procs[name]),
            barrier_engine=lambda name: ql.AnalyticBarrierEngine(procs[name]), diagnostics=diag,
        )


_HESTON_GUESS = dict(v0=0.02, kappa=0.6, theta=0.01, sigma=0.4, rho=-0.15)


def calibrate_heston(discount, dividend, calendar, spot, quotes):
    """Fit Heston to Black vol quotes [(expiry tenor, strike, vol)] by Levenberg-Marquardt on
    QuantLib HestonModelHelpers (relative price errors, analytic engine).
    Returns (HestonProcess with the fitted parameters, ql.HestonModel, RMS calibration error)."""
    spot_h = ql.QuoteHandle(ql.SimpleQuote(spot))
    model = ql.HestonModel(ql.HestonProcess(discount, dividend, spot_h, *_HESTON_GUESS.values()))
    engine = ql.AnalyticHestonEngine(model)
    helpers = []
    for tenor, strike, vol in quotes:
        h = ql.HestonModelHelper(tenor, calendar, spot, strike, ql.QuoteHandle(ql.SimpleQuote(vol)),
                                 discount, dividend)
        h.setPricingEngine(engine)
        helpers.append(h)
    model.calibrate(helpers, ql.LevenbergMarquardt(1e-8, 1e-8, 1e-8),
                    ql.EndCriteria(500, 300, 1e-8, 1e-8, 1e-8))
    process = ql.HestonProcess(discount, dividend, spot_h,
                               model.v0(), model.kappa(), model.theta(), model.sigma(), model.rho())
    rmse = float(np.sqrt(np.mean([h.calibrationError() ** 2 for h in helpers])))
    return process, model, rmse


class _HestonParams(SpecModel):
    v0: float = Field(gt=0)
    kappa: float = Field(gt=0)
    theta: float = Field(gt=0)
    sigma: float = Field(gt=0)
    rho: float = Field(ge=-1, le=1)


@register("model", "heston")
class Heston(ModelFactory):
    description = ("Single-asset Heston stochastic vol with deterministic rates. Either give `params`, or "
                   "omit them to calibrate (Levenberg-Marquardt on QuantLib HestonModelHelpers) to every quote of "
                   "the underlying's vol surface. "
                   "Analytic engine: European vanilla.")
    has_european_engine = True
    has_barrier_engine = False

    class Spec(SpecModel):
        underlying: str
        discount_curve: str
        params: _HestonParams | None = Field(None, description="omit to calibrate to the vol surface")

    @staticmethod
    def underlyings(spec):
        return [spec.underlying]

    @staticmethod
    def curves(spec):
        return [spec.discount_curve]

    @staticmethod
    def validate(spec, market, report, path):
        eq = market.equities.get(spec.underlying)
        if spec.params is None and eq is not None and eq.vol.get("type") != "surface":
            report.error(path + ".params",
                         "Heston calibration needs a vol surface for the underlying "
                         f"(market vol type is '{eq.vol.get('type')}')",
                         "provide model.params {v0, kappa, theta, sigma, rho} or a surface in market data")

    @staticmethod
    def build(spec, valuation_date, discount, und: dict[str, UnderlyingData], horizon_years=1.0):
        u = und[spec.underlying]
        diag = {}
        if spec.params is None:
            proc, m, rmse = calibrate_heston(discount, u.dividend, u.calendar, u.spot,
                                             u.vol_factory.calibration_quotes(u.vol_spec, u.spot))
            diag["calibrated_params"] = dict(v0=m.v0(), kappa=m.kappa(), theta=m.theta(), sigma=m.sigma(), rho=m.rho())
            diag["calibration_rmse"] = rmse
        else:
            p = spec.params
            proc = ql.HestonProcess(discount, u.dividend, ql.QuoteHandle(ql.SimpleQuote(u.spot)),
                                    p.v0, p.kappa, p.theta, p.sigma, p.rho)
        hm = ql.HestonModel(proc)
        name = spec.underlying
        return BuiltModel(
            name="heston", valuation_date=valuation_date, discount=discount,
            processes={name: proc}, underlyings={name: u},
            joint_simulator=lambda g, t, n, s, a: {name: _simulate_process(proc, t, n, s, a)},
            european_engine=lambda _: ql.AnalyticHestonEngine(hm),
            barrier_engine=None, diagnostics=diag,
        )
