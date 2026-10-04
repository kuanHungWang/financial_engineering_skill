"""Bermudan early exercise as a trade-level event: callable / puttable bonds, cancellable swaps, Bermudan options.

The event only prepares the exercise inputs on its observation dates (exercise amount and regression state per path).
The decision needs every cashflow of the trade, discounted path-wise, so the engine's aggregation runs
Longstaff-Schwartz (pricer/exercise.py) after the other legs and events (see engine._apply_exercise).
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Literal, Optional, Union

import numpy as np
from pydantic import Field, model_validator

from . import observables as obs
from .observables import RATES
from .payoffs import (EventFactory, FixedAmount, PCtx, VCtx, _check_ref, _check_schedule, merge_requirements)
from .registry import SpecModel, register
from .spec import DateLike

DateValue = Union[float, list[Optional[float]], dict[DateLike, Optional[float]]]


class ExerciseOption(SpecModel):
    """Intrinsic value received on exercise: notional * max(phi * (X - K), 0), units as in option_payoff."""
    underlying: Optional[str] = None
    observable: Optional[str] = None
    option_type: Literal["call", "put"]
    strike: float
    strike_mode: Optional[Literal["relative", "absolute"]] = Field(
        None, description="default relative for an underlying; must be absolute for an observable")
    notional: float = Field(gt=0)

    @model_validator(mode="after")
    def _source(self):
        if (self.underlying is None) == (self.observable is None):
            raise ValueError("give exactly one of `underlying` or `observable`")
        if self.observable is not None and self.strike_mode == "relative":
            raise ValueError("strike_mode relative applies to `underlying`")
        return self


@dataclass
class ExerciseInputs:
    """What the engine needs to run Longstaff-Schwartz for one Bermudan event."""
    spec: object
    dates: list                 # future exercise dates
    amount: np.ndarray          # (len(dates), n) received by the holder on exercise
    state: np.ndarray           # (len(dates), n, k) regression state
    diagnostics: dict = field(default_factory=dict)


def per_date(v: VCtx, value, all_dates, path=None):
    """A number, one value per schedule date (past ones included), or {date: value} steps (the last key on or
    before each date applies). NaN where null: no exercise on that date."""
    n = len(all_dates)
    if isinstance(value, (int, float)):
        return np.full(n, float(value))
    if isinstance(value, list):
        if len(value) != n:
            if path:
                v.report.error(path, f"has {len(value)} values but schedule has {n} dates "
                                     f"({all_dates[0]} .. {all_dates[-1]})",
                               "one value per schedule date, past ones included; or use {date: value} steps")
            return None
        return np.array([np.nan if x is None else float(x) for x in value])
    try:
        steps = sorted((v.dates.resolve(k), x) for k, x in value.items())
    except ValueError as e:
        if path:
            v.report.error(path, str(e))
        return None
    out = np.full(n, np.nan)
    for j, d in enumerate(all_dates):
        before = [x for k, x in steps if k <= d]
        if before and before[-1] is not None:
            out[j] = float(before[-1])
    return out


@register("event", "bermudan_call")
class BermudanCall(EventFactory):
    description = ("Bermudan early exercise priced by Longstaff-Schwartz regression. On each `observation` date the "
                   "`exerciser` may end the trade: the holder receives `exercise_price` (cash, e.g. the call price "
                   "1,000,000 for par) plus the intrinsic value of `exercise_option` if given; later cashflows are "
                   "cancelled (a cashflow ON the date is paid if settle_on_event_date). issuer = callable bond / "
                   "cancellable note (exercised when that is cheaper than continuing); holder = puttable bond / "
                   "Bermudan option (exercised when that beats continuing). The exercise rule is fitted on "
                   "pricing.exercise_fit_paths independent paths, so it is slightly sub-optimal and the price leans "
                   "against the exerciser; `regressors` (observables or underlyings) default to the model state.")

    exercise = True

    class Spec(SpecModel):
        id: str
        observation: str = Field(description="schedule of exercise dates (notice already taken into account)")
        exerciser: Literal["issuer", "holder"] = Field(
            "issuer", description="issuer: callable / cancellable by the payer; holder: puttable / Bermudan option")
        exercise_price: DateValue = Field(
            0.0, description="cash received by the holder on exercise (e.g. notional for a call at par); a number, "
                             "one value per schedule date, or {date: value} steps; null = no exercise on that date")
        exercise_option: Optional[ExerciseOption] = Field(
            None, description="add the intrinsic value of an option on exercise (Bermudan option: price 0 + option)")
        regressors: list[str] = Field(
            default_factory=list, description="observables / underlyings the continuation value is regressed on; "
                                              "default: every model underlying, plus the short rate under hull_white")
        degree: int = Field(2, ge=1, le=4, description="polynomial degree of the regression basis")
        settle_on_event_date: bool = Field(
            True, description="a cashflow paid on the exercise date is still paid (the coupon before the call)")

    @staticmethod
    def validate(spec, v: VCtx, path):
        if not _check_schedule(v, spec.observation, path + ".observation"):
            return
        all_dates = v.schedules[spec.observation]
        dates = [d for d in all_dates if d > v.valuation_date]
        if not dates:
            v.report.error(path + ".observation", f"schedule '{spec.observation}' has no dates after the valuation "
                                                  "date")
            return
        late = [d for d in dates if d > v.maturity]
        if late:
            v.report.error(path + ".observation", f"exercise date(s) after maturity {v.maturity}: "
                                                  f"{[str(d) for d in late]}")
        price = per_date(v, spec.exercise_price, all_dates, path + ".exercise_price")
        if price is not None and np.isnan(price[[all_dates.index(d) for d in dates]]).all():
            v.report.error(path + ".exercise_price", "no future date has an exercise price (all null)")
        for i, r in enumerate(spec.regressors):
            obs.known(v, r, f"{path}.regressors[{i}]")
        if not spec.regressors and not v.underlyings and not v.stochastic_rates:
            v.report.error(path + ".regressors", "the model simulates nothing to regress on",
                           "name the observables that drive the exercise decision")
        o = spec.exercise_option
        if o is not None:
            if o.underlying is not None:
                _check_ref(v, o.underlying, path + ".exercise_option.underlying")
            elif obs.known(v, o.observable, path + ".exercise_option.observable"):
                obs.require_stochastic(v, o.observable, path + ".exercise_option.observable", "an exercise option")
        if spec.settle_on_event_date and spec.exercise_option is None:
            redemptions = set()
            for c, s in v.legs.values():
                if c is FixedAmount:
                    with contextlib.suppress(ValueError):
                        redemptions.add(v.dates.resolve(s.payment_date))
            both = sorted(redemptions & set(dates))
            if both:
                v.report.warning(path + ".observation",
                                 f"a fixed_amount leg also pays on exercise date(s) {[str(d) for d in both]}: if "
                                 "exercised there, both the exercise price and that amount are paid",
                                 "drop the redemption date from the exercise schedule")

    @staticmethod
    def dates(spec, v: VCtx):
        all_dates = v.schedules[spec.observation]
        price = per_date(v, spec.exercise_price, all_dates)
        return [(d, p) for d, p in zip(all_dates, price) if d > v.valuation_date and not np.isnan(p)]

    @classmethod
    def fixing_dates(cls, spec, v: VCtx, path):
        ds = [d for d, _ in cls.dates(spec, v)]
        reqs = [obs.requirements(v, r, ds) for r in spec.regressors]
        if not spec.regressors:
            reqs += [{u: ds} for u in v.underlyings] + ([{RATES: ds}] if v.stochastic_rates else [])
        o = spec.exercise_option
        if o is not None:
            reqs.append({o.underlying: ds} if o.underlying else obs.requirements(v, o.observable, ds))
        return merge_requirements(*reqs)

    @classmethod
    def payment_dates(cls, spec, v: VCtx):
        return [d for d, _ in cls.dates(spec, v)]

    @classmethod
    def evaluate(cls, spec, p: PCtx, path, legs):
        v, n = p.v, p.n_paths
        pairs = cls.dates(spec, v)
        ds = [d for d, _ in pairs]
        amount = np.broadcast_to(np.array([x for _, x in pairs])[:, None], (len(ds), n)).copy()
        o = spec.exercise_option
        if o is not None:
            x = p.fixings[o.underlying].loc[ds].values if o.underlying else p.obs(o.observable, ds)
            if o.underlying is not None and (o.strike_mode or "relative") == "relative":
                ref = v.reference_levels[o.underlying]
                units, k = o.notional / ref, o.strike * ref
            else:
                units, k = o.notional, o.strike
            phi = 1.0 if o.option_type == "call" else -1.0
            amount += units * np.maximum(phi * (x - k), 0.0)
        cols = [p.obs(r, ds) for r in spec.regressors]
        if not spec.regressors:
            cols += [p.fixings[u].loc[ds].values for u in v.underlyings]
            if p.scenario.rates is not None:
                cols.append(p.scenario.rates.state(ds))
        state = np.stack([np.broadcast_to(c, (len(ds), n)) for c in cols], axis=-1)
        return ExerciseInputs(spec, ds, amount, state)
