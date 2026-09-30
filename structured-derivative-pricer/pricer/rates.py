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
"""
from __future__ import annotations

import numpy as np
from pydantic import Field

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


@register("model", "hull_white")
class HullWhite(ModelFactory):
    description = ("One-factor Hull-White short rate dr = (theta(t) - a r) dt + sigma dW, fitted to the discount "
                   "curve. Rates only (no equity underlyings): every rate observable is stochastic and cashflows are "
                   "discounted path-wise. Other curves keep a deterministic basis to the discount curve. Exact "
                   "simulation on fixing/payment dates. Give mean_reversion and volatility; calibration to "
                   "swaptions is not supported yet.")
    has_european_engine = False
    has_barrier_engine = False
    stochastic_rates = True

    class Spec(SpecModel):
        discount_curve: str
        mean_reversion: float = Field(ge=1e-4, le=5.0, description="a, per year (e.g. 0.03)")
        volatility: float = Field(gt=0, lt=0.2, description="sigma, absolute short-rate vol (0.01 = 100bp/yr)")

    @staticmethod
    def underlyings(spec):
        return []

    @staticmethod
    def curves(spec):
        return [spec.discount_curve]

    @staticmethod
    def validate(spec, market, report, path):
        pass

    @staticmethod
    def build(spec, valuation_date, discount, und, horizon_years=1.0):
        a, s = spec.mean_reversion, spec.volatility

        def rate_sim(dates, times, n, seed, anti, disc):
            return simulate_hull_white(a, s, dates, times, n, seed, anti, disc)
        return BuiltModel(name="hull_white", valuation_date=valuation_date, discount=discount,
                          processes={}, underlyings={}, rate_simulator=rate_sim, stochastic_rates=True,
                          diagnostics={"mean_reversion": a, "volatility": s})
