"""Path-wise early termination on simulated cashflow tables: Longstaff-Schwartz (Bermudan / callable / puttable),
autocall, and a summarizer that turns discounted cashflow tables into prices.

Every table has the engine's layout: rows = periods (observation / payment dates), columns = paths, as a
(T, N) ndarray or a DataFrame whose index holds the dates. Anything that is the same on every path may be given
as (T,) or (T, 1) and is broadcast.

    lsm = longstaff_schwartz(cashflows, exercise_payoff, forward_df, observations, exerciser="issuer")
    ac  = autocall(cashflows, autocall_mask, autocall_cashflows, forward_df)
    s   = summarize(lsm)          # or summarize(ac), or summarize(any (T, N) table of discounted cashflows)
    s.price, s.confidence_interval, s.expected_cashflows, s.fair_values

Conventions shared by both modules:
- `cashflows[t]` is what the trade pays on period t while it is still alive (coupons, the maturity redemption).
- A termination on period t (exercise or autocall) pays its termination amount on t and cancels every later
  cashflow. The regular cashflow of t itself is paid too when `settle_on_event_date` (the default: a callable
  bond pays the coupon and is then redeemed; an autocall pays the period's coupon with the redemption).
- `forward_df[t]` is the forward-start discount factor D(t-1, t) from the previous period to t (D(0, t_0) for the
  first row). Path-wise under stochastic rates, (T,) / (T, 1) under deterministic ones. The cumulative factor
  D(0, t) is their running product. `forward_discount_factors` converts cumulative factors (the engine's
  `Scenario.discount`) to this form.
- The holder's view: positive amounts are received.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Literal

import numpy as np
import pandas as pd


# ------------------------------------------------------------------ input normalisation
def _index_of(*tables):
    for x in tables:
        if isinstance(x, (pd.DataFrame, pd.Series)):
            return list(x.index)
    return None


def _table(x, shape, name, dtype=float) -> np.ndarray:
    """(T, N) array from a scalar, (T,), (T, 1) or (T, N) array-like / DataFrame / Series."""
    t, n = shape
    a = np.asarray(x.values if isinstance(x, (pd.DataFrame, pd.Series)) else x, dtype=dtype)
    if a.ndim == 0:
        return np.full(shape, a, dtype=dtype)
    if a.ndim == 1:
        a = a[:, None]
    if a.ndim != 2 or a.shape[0] != t or a.shape[1] not in (1, n):
        raise ValueError(f"{name}: expected shape ({t},), ({t}, 1) or ({t}, {n}) (periods x paths), got {a.shape}")
    return np.broadcast_to(a, shape).astype(dtype, copy=True)


def _shape(cashflows, *others) -> tuple:
    """(T, N): T from `cashflows`, N from the widest input, so that any input given per period only ((T,) or
    (T, 1), e.g. deterministic coupons) broadcasts against the path-wise ones."""
    def arr(x):
        return np.asarray(x.values if isinstance(x, (pd.DataFrame, pd.Series)) else x)
    cf = arr(cashflows)
    if cf.ndim not in (1, 2):
        raise ValueError(f"cashflows: expected a (periods,) or (periods, paths) table, got shape {cf.shape}")
    n = 1 if cf.ndim == 1 else cf.shape[1]
    for x in others:
        for a in (map(arr, x.values() if isinstance(x, dict) else x) if isinstance(x, (dict, list, tuple))
                  else [arr(x)]):
            if a.ndim >= 2:
                n = max(n, a.shape[1])
    return cf.shape[0], n


def _observations(obs, shape) -> np.ndarray:
    """Regression state as (T, N, K): one (T, N) table, a (T, N, K) array, or a list / dict of (T, N) tables
    (a dict's keys name the factors, e.g. {"SPX": spx_paths, "rate": short_rate})."""
    if isinstance(obs, dict):
        obs = list(obs.values())
    if isinstance(obs, (list, tuple)):
        return np.stack([_table(o, shape, f"observations[{k}]") for k, o in enumerate(obs)], axis=-1)
    a = np.asarray(obs.values if isinstance(obs, (pd.DataFrame, pd.Series)) else obs, dtype=float)
    if a.ndim == 3:
        if a.shape[:2] != shape:
            raise ValueError(f"observations: expected shape {shape + (a.shape[2],)}, got {a.shape}")
        return a.copy()
    return _table(a, shape, "observations")[..., None]


def forward_discount_factors(cumulative) -> np.ndarray:
    """D(t_{i-1}, t_i) = D(0, t_i) / D(0, t_{i-1}) from cumulative factors D(0, t_i): (T,), (T, 1) or (T, N)."""
    c = np.asarray(cumulative.values if isinstance(cumulative, (pd.DataFrame, pd.Series)) else cumulative,
                   dtype=float)
    out = c.copy()
    out[1:] = c[1:] / c[:-1]
    return out


# ------------------------------------------------------------------ result shared by both modules
@dataclass
class TerminationResult:
    """Cashflows after early termination, path by path (all tables periods x paths).

    survival[t, j]: path j has not terminated before period t (it is alive on t, so its cashflow there can be
        paid). event[t, j]: path j terminates on period t (at most one True per column).
    cashflows: the cashflows actually paid (regular ones while alive, plus the termination amount on the event
        period), undiscounted. discount: the cumulative D(0, t)."""
    cashflows: np.ndarray
    discount: np.ndarray
    survival: np.ndarray
    event: np.ndarray
    index: list | None = None
    diagnostics: dict = field(default_factory=dict)

    @property
    def discounted(self) -> np.ndarray:
        return self.cashflows * self.discount

    @property
    def event_probability(self) -> np.ndarray:
        """P(terminate on period t), per period."""
        return self.event.mean(axis=1)

    @property
    def survival_probability(self) -> np.ndarray:
        """P(alive on period t), per period."""
        return self.survival.mean(axis=1)

    @property
    def termination_probability(self) -> float:
        return float(self.event.any(axis=0).mean())

    def table(self, name: str) -> pd.DataFrame:
        """One of the (T, N) tables as a DataFrame indexed by the input's dates (or period numbers)."""
        return pd.DataFrame(getattr(self, name), index=self.index)


def _survival(event: np.ndarray) -> np.ndarray:
    """alive on t <=> no event strictly before t."""
    before = np.cumsum(event, axis=0) - event
    return before == 0


def _settle(cashflows, termination_amount, event, settle_on_event_date):
    surv = _survival(event)
    paid = surv & ~event if not settle_on_event_date else surv
    return cashflows * paid + termination_amount * event, surv


def _first_true(mask: np.ndarray) -> np.ndarray:
    """Keep only the first True of each column."""
    return mask & (np.cumsum(mask, axis=0) == 1)


# ------------------------------------------------------------------ Longstaff-Schwartz
@dataclass
class ExercisePolicy:
    """Per-period least-squares fit of continuation value on a polynomial basis of the (standardised) state."""
    degree: int
    coefficients: dict          # period -> (beta, mean, scale), only on exercisable periods

    @staticmethod
    def basis(x: np.ndarray, degree: int) -> np.ndarray:
        """1, x_k, x_k^2 ... x_k^degree, and the pairwise cross products x_k x_l for degree >= 2."""
        n, k = x.shape
        cols = [np.ones(n)]
        for p in range(1, degree + 1):
            cols += [x[:, i] ** p for i in range(k)]
        if degree >= 2:
            cols += [x[:, i] * x[:, j] for i, j in itertools.combinations(range(k), 2)]
        return np.column_stack(cols)

    def fit(self, t, x, y):
        mean, scale = x.mean(axis=0), x.std(axis=0)
        scale = np.where(scale > 0, scale, 1.0)
        a = self.basis((x - mean) / scale, self.degree)
        beta = np.linalg.lstsq(a, y, rcond=None)[0]
        self.coefficients[t] = (beta, mean, scale)

    def predict(self, t, x):
        beta, mean, scale = self.coefficients[t]
        return self.basis((x - mean) / scale, self.degree) @ beta


def longstaff_schwartz(cashflows, exercise_payoff, forward_df, observations, *,
                       exercisable=None, exerciser: Literal["holder", "issuer"] = "holder",
                       settle_on_event_date: bool = True, degree: int = 2, itm_only: bool = False,
                       policy: ExercisePolicy | None = None) -> TerminationResult:
    """Optimal early exercise by Longstaff-Schwartz least-squares Monte Carlo.

    cashflows (T, N): regular cashflows paid on each period while the trade is alive (coupons, redemption).
    exercise_payoff (T, N): the termination payoff received on exercise at period t (e.g. the call price for a
        callable bond, max(K - S, 0) for a Bermudan put). NaN marks a period where exercise is impossible.
    forward_df (T, N): forward-start discount factors D(t-1, t); see the module docstring.
    observations: regression state on each period, (T, N), (T, N, K), or a list / dict of (T, N) tables.
    exercisable (T,) bool: periods with an exercise right (default: every period with a finite payoff on some path).
    exerciser: "holder" exercises when the payoff beats continuation (puttable, Bermudan option); "issuer"
        exercises when the payoff is below it (callable bond, cancellable swap held by the investor).
    settle_on_event_date: the period's regular cashflow is also paid when exercised on it.
    degree: polynomial degree of the regression basis (with cross terms for several factors).
    itm_only: regress (and exercise) only on paths whose payoff is positive, as in the original paper for options.
    policy: the `diagnostics["policy"]` of an earlier run. Its regressions are applied as they are (no refit),
        which gives the low-biased out-of-sample price on an independent set of paths.

    Returns a TerminationResult; event = the exercise mask, diagnostics["exercise_probability"] per period.
    """
    index = _index_of(cashflows, exercise_payoff, forward_df)
    shape = _shape(cashflows, exercise_payoff, forward_df, observations)
    t_n, n = shape
    cf = _table(cashflows, shape, "cashflows")
    payoff = _table(exercise_payoff, shape, "exercise_payoff")
    fdf = _table(forward_df, shape, "forward_df")
    x = _observations(observations, shape)
    can = np.isfinite(payoff).any(axis=1) if exercisable is None else np.asarray(exercisable, dtype=bool)
    if can.shape != (t_n,):
        raise ValueError(f"exercisable: expected shape ({t_n},), got {can.shape}")
    if not np.isfinite(cf).all() or not np.isfinite(fdf).all() or not np.isfinite(x).all():
        raise ValueError("cashflows, forward_df and observations must be finite")
    allowed = can[:, None] & np.isfinite(payoff)            # a NaN payoff blocks exercise on that path only
    payoff = np.where(allowed, payoff, 0.0)
    if exerciser not in ("holder", "issuer"):
        raise ValueError("exerciser must be 'holder' or 'issuer'")
    sign = 1.0 if exerciser == "holder" else -1.0           # exercise when sign * (payoff - continuation) > 0
    reg = policy if policy is not None else ExercisePolicy(degree, {})

    # Backward induction. y = realised value on period t, in t-money, of everything strictly after t under
    # the policy chosen for t+1 .. T-1.
    exercise = np.zeros(shape, dtype=bool)
    y = np.zeros(n)
    for t in range(t_n - 1, -1, -1):
        if can[t]:
            # compare the payoff with what is lost by exercising: the future, plus today's cashflow if unsettled
            lost_today = 0.0 if settle_on_event_date else cf[t]
            sel = allowed[t] & (payoff[t] > 0) if itm_only else allowed[t]
            if policy is None and sel.sum() > 0:
                reg.fit(t, x[t][sel], y[sel])
            if t in reg.coefficients:
                cont = reg.predict(t, x[t]) + lost_today
                exercise[t] = sel & (sign * (payoff[t] - cont) > 0)
        alive_value = cf[t] + y
        exercised_value = payoff[t] + (cf[t] if settle_on_event_date else 0.0)
        value_t = np.where(exercise[t], exercised_value, alive_value)
        y = fdf[t] * value_t                                # in (t-1)-money for the next step back
    event = _first_true(exercise)
    paid, surv = _settle(cf, payoff, event, settle_on_event_date)
    disc = np.cumprod(fdf, axis=0)
    diag = {"exercise_probability": event.mean(axis=1), "total_exercise_probability": float(event.any(axis=0).mean()),
            "exerciser": exerciser, "policy": reg, "in_sample": policy is None}
    return TerminationResult(paid, disc, surv, event, index, diag)


# ------------------------------------------------------------------ autocall
def autocall(cashflows, autocall_mask, autocall_cashflows, forward_df=1.0, *,
             settle_on_event_date: bool = True) -> TerminationResult:
    """Terminate each path on the first period its autocall condition holds.

    cashflows (T, N): regular cashflows paid while alive (coupons, maturity redemption).
    autocall_mask (T, N) bool: the autocall condition holds on period t (e.g. S_t >= barrier on a call date).
        Only the first True per path counts.
    autocall_cashflows (T, N), (T,) or scalar: the amount paid on autocall at t (e.g. par plus the call coupon).
    forward_df: forward-start discount factors D(t-1, t) (default 1: undiscounted).
    settle_on_event_date: the period's regular cashflow is also paid on the autocall date.

    Returns a TerminationResult; event = the autocall mask actually triggered, survival_probability per period.
    """
    index = _index_of(cashflows, autocall_mask, autocall_cashflows, forward_df)
    shape = _shape(cashflows, autocall_mask, autocall_cashflows, forward_df)
    cf = _table(cashflows, shape, "cashflows")
    mask = _table(autocall_mask, shape, "autocall_mask", dtype=bool)
    amount = _table(autocall_cashflows, shape, "autocall_cashflows")
    fdf = _table(forward_df, shape, "forward_df")
    event = _first_true(mask)
    paid, surv = _settle(cf, amount, event, settle_on_event_date)
    diag = {"autocall_probability": event.mean(axis=1), "total_autocall_probability": float(event.any(axis=0).mean())}
    return TerminationResult(paid, np.cumprod(fdf, axis=0), surv, event, index, diag)


# ------------------------------------------------------------------ summarizer
@dataclass
class Summary:
    price: float
    std_error: float
    confidence_interval: tuple
    confidence: float
    fair_values: np.ndarray              # (N,) PV per path: the vertical sums of the discounted table
    expected_cashflows: pd.DataFrame     # per period: the horizontal means (and survival / event probabilities)

    def to_dict(self) -> dict:
        d = {"price": self.price, "std_error": self.std_error, "confidence_interval": list(self.confidence_interval),
             "confidence": self.confidence, "num_paths": int(self.fair_values.size),
             "fair_value_quantiles": {f"{q:g}": float(np.quantile(self.fair_values, q))
                                      for q in (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)}}
        ec = self.expected_cashflows.reset_index().rename(columns={"index": "period"})
        d["expected_cashflows"] = [{k: (v.isoformat() if hasattr(v, "isoformat") else
                                        v.item() if hasattr(v, "item") else v) for k, v in row.items()}
                                   for row in ec.to_dict("records")]
        return d


def summarize(*results, confidence: float = 0.95, antithetic: bool = False) -> Summary:
    """Price and expected cashflows from one or more discounted cashflow tables (periods x paths).

    Each argument is a TerminationResult (from longstaff_schwartz / autocall) or a (T, N) ndarray / DataFrame of
    discounted cashflows; several are added up (e.g. a callable leg and a plain funding leg on the same paths).
    expected_cashflows: per period, the mean over paths (horizontal) of the discounted cashflow `pv`; for
        TerminationResults also the undiscounted `expected_amount`, `survival_probability` and
        `event_probability`. fair_values: per path, the sum over periods (vertical) of the discounted cashflows.
    antithetic: paths come in (2k, 2k+1) antithetic pairs; the standard error then uses pair averages.
    """
    if not results:
        raise ValueError("summarize needs at least one cashflow table")
    frames, fair = [], None
    for k, r in enumerate(results):
        if isinstance(r, TerminationResult):
            disc, idx = r.discounted, r.index
            f = pd.DataFrame({"expected_amount": r.cashflows.mean(axis=1), "pv": disc.mean(axis=1),
                              "discount_factor": r.discount.mean(axis=1),
                              "survival_probability": r.survival_probability,
                              "event_probability": r.event_probability},
                             index=idx if idx is not None else range(len(disc)))
        else:
            idx = _index_of(r)
            disc = np.asarray(r.values if isinstance(r, pd.DataFrame) else r, dtype=float)
            if disc.ndim == 1:
                disc = disc[:, None]
            if disc.ndim != 2:
                raise ValueError(f"results[{k}]: expected a (periods, paths) table, got shape {disc.shape}")
            f = pd.DataFrame({"pv": disc.mean(axis=1)}, index=idx if idx is not None else range(len(disc)))
        pv = disc.sum(axis=0)
        if fair is not None and pv.shape != fair.shape:
            raise ValueError(f"results[{k}] has {pv.size} paths, the first has {fair.size}")
        fair = pv if fair is None else fair + pv
        frames.append(f)
    expected = frames[0] if len(frames) == 1 else _combine(frames)
    n = fair.size
    if antithetic and n % 2:
        raise ValueError("antithetic needs an even number of paths")
    samples = (fair[0::2] + fair[1::2]) / 2 if antithetic else fair
    price = float(fair.mean())
    se = float(samples.std(ddof=1) / math.sqrt(samples.size)) if samples.size > 1 else float("nan")
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    return Summary(price, se, (price - z * se, price + z * se), confidence, fair, expected)


def _combine(frames):
    """Add per-period tables of several results; probabilities are not additive, so they are dropped."""
    additive = ["expected_amount", "pv"]
    out = pd.concat([f[[c for c in additive if c in f]] for f in frames]).groupby(level=0, sort=True).sum()
    return out
