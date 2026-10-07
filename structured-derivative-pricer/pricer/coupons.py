"""Coupon legs on an accrual schedule: fixed_leg, floating_leg, range_accrual.

Periods: the schedule lists accrual END dates. The first period starts at `accrual_start`
(default effective_date) and each later one at the previous end. Payment = end + payment_lag
business days of payment_calendar. Periods paid on or before the valuation date are dropped.

    coupon = position * notional * rate * yearfraction(start, end, day_count) [* accrual fraction]

Floating rate = clip(gearing * X + spread, floor, cap), with X read from the `index` observable:
    in_advance             fixed fixing_lag business days before the accrual start
    in_arrears             fixed fixing_lag business days before the accrual end
    compounded_in_arrears  X = (prod_i (1 + r_i d_i) - 1) / yearfraction_index(start, end) over the
                           business days of an overnight_rate index in [start, end). With
                           lockout_days n, the last n daily rates equal the rate of the business
                           day before them (rate cut-off). The unlocked days compound as the
                           model's money-market growth on the index curve: exact daily compounding
                           of the curve forwards under deterministic rates, exact continuous
                           accrual under Hull-White.
Range accrual fraction = weighted share of the period's observation days on which every `range`
condition holds (business days, or calendar days carrying the previous business day's value).

Per-period values: notional, rate, gearing, spread, cap, floor and range bounds each accept
    a number                 the same in every period
    a list                   one value per period of the schedule, paid periods included
    {date: value, ...}       steps keyed by period start dates, e.g. {effective_date: 0.03, 1Y: 0.04}:
                             a period takes the value of the last key before its midpoint, so a key
                             need not match business-day adjusted dates exactly
`null` in a cap, floor or range bound means none in that period.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal, Optional, Union

import numpy as np
import pandas as pd
import QuantLib as ql
from pydantic import Field, model_validator

from . import conventions as cv
from . import observables as obs
from .observables import RATES, OvernightRate
from .payoffs import (LegFactory, LegResult, PCtx, VCtx, _check_schedule, _resolve_date, _sign, apply_conditions,
                      conditions_requirements, merge_requirements, validate_conditions)
from .registry import SpecModel, register
from .spec import DateLike


@dataclass
class Period:
    start: dt.date
    end: dt.date
    pay: dt.date
    tau: float
    i: int = 0          # position among all periods of the schedule (paid ones included)


PeriodValue = Union[float, list[Optional[float]], dict[DateLike, Optional[float]]]
_PV = ("; a number, a list with one value per schedule period (paid ones included), or {date: value} steps "
       "keyed by period start (e.g. {effective_date: 0.03, 1Y: 0.04})")


def per_period(v, value, all_pers, path=None, allow_none=False, positive=False):
    """Values of a PeriodValue for every schedule period, NaN where null. Reports problems when a
    `path` is given (validation) and returns None; at run time the spec is already valid."""
    n = len(all_pers)

    def bad(msg, hint=""):
        if path:
            v.report.error(path, msg, hint)
        return None

    if value is None:
        out = np.full(n, np.nan)
    elif isinstance(value, (int, float)):
        out = np.full(n, float(value))
    elif isinstance(value, list):
        if len(value) != n:
            return bad(f"has {len(value)} values but the schedule has {n} periods "
                       f"({all_pers[0].start} .. {all_pers[-1].end})",
                       "one value per period, paid periods included; or use {date: value} steps")
        out = np.array([np.nan if x is None else float(x) for x in value])
    else:
        try:
            steps = sorted((v.dates.resolve(k), x) for k, x in value.items())
        except ValueError as e:
            return bad(str(e))
        out = np.empty(n)
        for j, q in enumerate(all_pers):
            mid = q.start + (q.end - q.start) / 2
            before = [x for d, x in steps if d <= mid]
            if not before:
                return bad(f"no step on or before the period {q.start}..{q.end}",
                           f"add a key on or before {q.start}, e.g. effective_date")
            out[j] = np.nan if before[-1] is None else float(before[-1])
    if not allow_none and np.isnan(out).any():
        return bad("null is not allowed here")
    if positive and (out <= 0).any():
        return bad("must be > 0 in every period")
    return out


def _stack(rows) -> np.ndarray:
    """Stack per-period rows of width 1 or n_paths into one (len(rows), width) array."""
    m = max(np.shape(r)[-1] for r in rows)
    return np.vstack([np.broadcast_to(r, (m,)) for r in rows])


def _parse(v, path, pairs):
    ok = True
    for field_name, value, fn in pairs:
        try:
            fn(value)
        except ValueError as e:
            v.report.error(f"{path}.{field_name}", str(e))
            ok = False
    return ok


# ------------------------------------------------------------------ periods
class _PeriodSpec(SpecModel):
    id: str
    notional: PeriodValue = Field(description="> 0" + _PV + "; a varying notional amortises the coupons only")
    schedule: str = Field(description="schedule of accrual END dates (the first period starts at accrual_start)")
    accrual_start: DateLike = Field("effective_date", description="start of the first accrual period")
    payment_lag: int = Field(0, ge=0, description="business days from accrual end to payment")
    payment_calendar: str = "WeekendsOnly"
    position: Literal["long", "short"] = "long"
    conditions: list[dict] = Field(
        default_factory=list, description="knock_in (kick-in) / knock_out conditions; a period's coupon counts "
                                          "barrier hits on or before its accrual end")


class _PeriodLeg(LegFactory):
    @staticmethod
    def accrual_schedule(spec, v: VCtx) -> ql.Schedule:
        """The QuantLib schedule of the periods. A generated schedule that starts at accrual_start is used
        as is, so its tenor and regular/stub flags reach the day counter (ActualActual.Bond stubs);
        otherwise the dates themselves form the schedule and each period is its own reference period."""
        ends = [cv.to_ql(d) for d in sorted(v.schedules[spec.schedule])]
        start = cv.to_ql(v.dates.resolve(spec.accrual_start))
        src = _PeriodLeg.source_schedule(spec, v)
        if src is not None:
            return src
        return ql.Schedule([start] + ends)

    @staticmethod
    def source_schedule(spec, v: VCtx):
        """The generated ql.Schedule behind `schedule` if it starts at accrual_start (as given, or as the
        schedule's own business-day convention adjusts it), else None."""
        src = getattr(v.schedules[spec.schedule], "ql_schedule", None)
        if src is None:
            return None
        dates = list(src)
        start = cv.to_ql(v.dates.resolve(spec.accrual_start))
        ends = [cv.to_ql(d) for d in sorted(v.schedules[spec.schedule])]
        if dates[1:] == ends and dates[0] in (start, src.calendar().adjust(start, src.businessDayConvention())):
            return src
        return None

    @classmethod
    def all_periods(cls, spec, v: VCtx):
        cal = cv.calendar(spec.payment_calendar)
        leg = ql.FixedRateLeg(cls.accrual_schedule(spec, v), cv.day_count(spec.day_count), [1.0], [1.0])
        out = []
        for i, c in enumerate(ql.as_coupon(x) for x in leg):
            s, e = cv.from_ql(c.accrualStartDate()), cv.from_ql(c.accrualEndDate())
            pay = cv.from_ql(cal.advance(c.accrualEndDate(), spec.payment_lag, ql.Days)) if spec.payment_lag else e
            out.append(Period(s, e, pay, c.accrualPeriod(), i))
        return out

    @classmethod
    def values(cls, spec, v: VCtx, pers, name, path=None, **kw):
        """A PeriodValue field on the given periods: shape (len(pers), 1)."""
        allp = cls.all_periods(spec, v)
        arr = per_period(v, getattr(spec, name), allp, f"{path}.{name}" if path else None, **kw)
        return None if arr is None else arr[[q.i for q in pers]][:, None]

    @classmethod
    def periods(cls, spec, v: VCtx):
        return [q for q in cls.all_periods(spec, v) if q.pay > v.valuation_date]

    @classmethod
    def validate_periods(cls, spec, v: VCtx, path):
        ok = _check_schedule(v, spec.schedule, path + ".schedule")
        ok &= _parse(v, path, [("day_count", spec.day_count, cv.day_count),
                               ("payment_calendar", spec.payment_calendar, cv.calendar)])
        first = _resolve_date(v, spec.accrual_start, path + ".accrual_start")
        if not ok or first is None:
            return None
        ends = sorted(v.schedules[spec.schedule])
        if ends[0] <= first:
            v.report.error(path + ".accrual_start", f"first accrual end {ends[0]} is not after accrual_start {first}",
                           "the schedule lists accrual end dates; leave its start date out (include_start: false)")
            return None
        pers = cls.periods(spec, v)
        if not pers:
            v.report.error(path + ".schedule", "every period is paid on or before the valuation date")
            return None
        validate_conditions(spec, v, path, pers[-1].end)
        cls.values(spec, v, pers, "notional", path, positive=True)
        if spec.day_count.startswith("ActualActual.Bond") and cls.source_schedule(spec, v) is None:
            v.report.warning(path + ".day_count", "ActualActual.Bond needs the schedule's tenor to size stub periods; "
                                                  "with a dates schedule every period is its own reference period",
                             "use a generated schedule that starts at accrual_start")
        return pers

    @classmethod
    def paid_periods(cls, spec, v: VCtx):
        """Periods paid on or before the valuation date. `fixing_dates` and `cashflows` accept them as `pers`,
        which reads their fixings from history like any other past read (e.g. coupons a TARN accumulated)."""
        return [q for q in cls.all_periods(spec, v) if q.pay <= v.valuation_date]

    @classmethod
    def payment_dates(cls, spec, v: VCtx):
        return [q.pay for q in cls.periods(spec, v)]

    @classmethod
    def finish(cls, spec, p: PCtx, path, pers, amounts, diag):
        """Apply sign and leg conditions (cut-off = accrual end) and wrap the result."""
        notional = cls.values(spec, p.v, pers, "notional")
        amounts = np.broadcast_to(_sign(spec) * notional * amounts, (len(pers), p.n_paths))
        amounts = apply_conditions(spec, p, path, amounts, [q.end for q in pers], diag)
        return LegResult(pd.DataFrame(np.array(amounts), index=[q.pay for q in pers]), diag, _sign(spec))


# ------------------------------------------------------------------ floating rate
class _FixingFields(SpecModel):
    fixing: Literal["in_advance", "in_arrears", "compounded_in_arrears"] = "in_advance"
    fixing_lag: int | None = Field(None, ge=0, description="business days before accrual start (in_advance) or end "
                                                           "(in_arrears); default: the index's fixing days. Unused "
                                                           "by compounded_in_arrears")
    fixing_calendar: str | None = Field(None, description="default: the index's fixing calendar")
    lockout_days: int = Field(0, ge=0, description="compounded_in_arrears only: the last n daily rates of a period "
                                                   "equal the rate of the business day before them")


class _RateFields(_FixingFields):
    gearing: PeriodValue = Field(1.0, description="multiplier on the index" + _PV)
    spread: PeriodValue = Field(0.0, description="added after gearing" + _PV)
    cap: PeriodValue | None = Field(None, description="cap on the period rate (after gearing and spread)" + _PV)
    floor: PeriodValue | None = Field(None, description="floor on the period rate (after gearing and spread)" + _PV)


_RATE_FIELDS = set(_RateFields.model_fields)


class FloatingRate:
    """The period rate of a floating (or floating-coupon range accrual) leg."""

    @staticmethod
    def conventions(spec, v):
        """(fixing lag, ql calendar): the leg's own, else the index's (as QuantLib's IborLeg does); None if unknown."""
        idx = obs.fixing_conventions(v, spec.index) if obs.lookup(v, spec.index) else None
        lag = spec.fixing_lag if spec.fixing_lag is not None else (idx[0] if idx else None)
        cal = cv.calendar(spec.fixing_calendar) if spec.fixing_calendar else (idx[1] if idx else None)
        return None if lag is None or cal is None else (lag, cal)

    @classmethod
    def fixing_date(cls, spec, v, per: Period):
        lag, cal = cls.conventions(spec, v)
        ref = per.start if spec.fixing == "in_advance" else per.end
        return cv.from_ql(cal.advance(cv.to_ql(ref), -lag, ql.Days))

    @staticmethod
    def ois_days(ispec, per: Period):
        """QuantLib's OvernightIndexedCoupon day structure for [start, end): the fixing (value) dates, the
        start of each one's accrual within the period, and its fraction dt. A period starting on a
        holiday uses the previous business day's fixing for its first days, as QuantLib does."""
        c = ql.OvernightIndexedCoupon(cv.to_ql(per.end), 1.0, cv.to_ql(per.start), cv.to_ql(per.end),
                                      OvernightRate.ql_index(ispec))
        days = [cv.from_ql(d) for d in list(c.valueDates())[:-1]]
        return days, [max(d, per.start) for d in days], list(c.dt())

    @classmethod
    def _split(cls, spec, v, per):
        """days[k:] are locked at the fixing of days[k-1]; days before j come from history; the model
        compounds from model_start to free_end (money-market growth over the unlocked accrual)."""
        _, ispec = obs.lookup(v, spec.index)
        days, acc, deltas = cls.ois_days(ispec, per)
        k = len(days) - spec.lockout_days
        free_end = acc[k] if spec.lockout_days else per.end
        j = next((i for i in range(k) if days[i] >= v.valuation_date), k)
        model_start = acc[j] if j < k else free_end
        return ispec, days, deltas, k, j, free_end, model_start

    @classmethod
    def validate(cls, spec, v: VCtx, path, pers):
        if not obs.known(v, spec.index, path + ".index") or (
                spec.fixing_calendar and not _parse(v, path, [("fixing_calendar", spec.fixing_calendar, cv.calendar)])):
            return
        icls, _ = obs.lookup(v, spec.index)
        if spec.fixing == "compounded_in_arrears":
            if icls is not OvernightRate:
                v.report.error(path + ".index", f"compounded_in_arrears needs an overnight_rate observable; "
                                                f"'{spec.index}' is {icls.type_name}")
                return
            for q in pers:
                _, days, _, k, _, _, _ = cls._split(spec, v, q)
                if k < 1:
                    v.report.error(path + ".lockout_days", f"lockout_days {spec.lockout_days} leaves no free day in "
                                                           f"the period {q.start}..{q.end} ({len(days)} days)")
                    return
        else:
            if spec.lockout_days:
                v.report.error(path + ".lockout_days", "lockout_days applies to fixing: compounded_in_arrears only")
            if cls.conventions(spec, v) is None:
                v.report.error(path + ".fixing_lag", f"index '{spec.index}' has no single rate-index fixing convention",
                               "set fixing_lag and fixing_calendar")
                return
        pv = {name: _PeriodLeg.values(spec, v, pers, name, path, allow_none=name in ("cap", "floor"))
              for name in ("gearing", "spread", "cap", "floor")}
        if any(x is None for x in pv.values()):
            return
        optional = ~np.isnan(pv["cap"]) | ~np.isnan(pv["floor"])
        if (optional & (pv["gearing"] != 0)).any():
            obs.require_stochastic(v, spec.index, path + ".index", "a cap/floor")
        crossed = [q for q, c, f in zip(pers, pv["cap"][:, 0], pv["floor"][:, 0]) if c < f]
        if crossed:
            v.report.error(path + ".cap", f"cap is below floor in {len(crossed)} period(s), first "
                                          f"{crossed[0].start}..{crossed[0].end}")

    @classmethod
    def requirements(cls, spec, v: VCtx, pers) -> dict:
        if spec.fixing != "compounded_in_arrears":
            return obs.requirements(v, spec.index, [cls.fixing_date(spec, v, q) for q in pers])
        reqs = []
        for q in pers:
            _, days, _, k, j, free_end, model_start = cls._split(spec, v, q)
            reqs.append(obs.requirements(v, spec.index, days[:j]))        # the known daily rates
            if v.stochastic_rates and model_start < free_end:
                reqs.append({RATES: [model_start, free_end]})
            if spec.lockout_days:
                reqs.append(obs.requirements(v, spec.index, [days[k - 1]]))
        return merge_requirements(*reqs)

    @classmethod
    def _compounded(cls, spec, p: PCtx, per):
        ispec, days, deltas, k, j, free_end, model_start = cls._split(spec, p.v, per)
        g = np.ones(1)
        if j:
            known = obs.evaluate(p, spec.index, days[:j])
            g = np.prod(1 + known * np.array(deltas[:j])[:, None], axis=0)
        if model_start < free_end:
            g = g * p.scenario.growth(ispec.curve, model_start, free_end)
        if spec.lockout_days:
            r = obs.evaluate(p, spec.index, [days[k - 1]])[0]
            for i in range(k, len(days)):
                g = g * (1 + r * deltas[i])
        tau = OvernightRate.ql_index(ispec).dayCounter().yearFraction(cv.to_ql(per.start), cv.to_ql(per.end))
        return (g - 1.0) / tau

    @classmethod
    def rates(cls, spec, p: PCtx, pers, diag) -> np.ndarray:
        """(len(pers), n_paths or 1) period rates after gearing, spread, cap and floor."""
        if spec.fixing == "compounded_in_arrears":
            x = _stack([cls._compounded(spec, p, q) for q in pers])
        else:
            x = obs.evaluate(p, spec.index, [cls.fixing_date(spec, p.v, q) for q in pers])
        pv = {name: _PeriodLeg.values(spec, p.v, pers, name, allow_none=name in ("cap", "floor"))
              for name in ("gearing", "spread", "cap", "floor")}
        r = pv["gearing"] * x + pv["spread"]
        cap, floor = np.nan_to_num(pv["cap"], nan=np.inf), np.nan_to_num(pv["floor"], nan=-np.inf)
        if spec.cap is not None:
            diag["cap_binding_probability"] = float(np.broadcast_to(r > cap, r.shape).mean())
        if spec.floor is not None:
            diag["floor_binding_probability"] = float(np.broadcast_to(r < floor, r.shape).mean())
        r = np.clip(r, floor, cap)
        diag["mean_rate"] = float(r.mean())
        return r


# ------------------------------------------------------------------ legs

@register("leg", "fixed_leg")
class FixedLeg(_PeriodLeg):
    description = ("Fixed-rate coupons: notional * rate * yearfraction(start, end) per accrual period of "
                   "`schedule`, paid payment_lag business days after each end. Optional leg conditions.")
    deterministic = True

    class Spec(_PeriodSpec):
        rate: PeriodValue = Field(description="coupon rate" + _PV + " (a step-up / step-down coupon)")
        day_count: str = "Thirty360.BondBasis"

    @classmethod
    def validate(cls, spec, v, path):
        pers = cls.validate_periods(spec, v, path)
        if pers:
            cls.values(spec, v, pers, "rate", path)

    @classmethod
    def fixing_dates(cls, spec, v, path, pers=None):
        pers = cls.periods(spec, v) if pers is None else pers
        return conditions_requirements(spec, v, path, pers[-1].end) if pers else {}

    @classmethod
    def cashflows(cls, spec, p: PCtx, path, pers=None):
        pers = cls.periods(spec, p.v) if pers is None else pers
        amounts = cls.values(spec, p.v, pers, "rate") * np.array([[q.tau] for q in pers])
        return cls.finish(spec, p, path, pers, amounts, {})


@register("leg", "floating_leg")
class FloatingLeg(_PeriodLeg):
    description = ("Floating coupons: notional * clip(gearing * X + spread, floor, cap) * yearfraction per period. "
                   "X = `index` observable fixed in_advance (IBOR), in_arrears, or compounded_in_arrears over an "
                   "overnight_rate index with optional lockout_days. An inverse floater is gearing -m, spread K, "
                   "floor 0. Any observable can be the index (a CMS rate, a spread, an equity performance).")

    class Spec(_PeriodSpec, _RateFields):
        index: str = Field(description="observable name")
        day_count: str = "Actual360"

    @classmethod
    def validate(cls, spec, v, path):
        pers = cls.validate_periods(spec, v, path)
        if pers:
            FloatingRate.validate(spec, v, path, pers)

    @classmethod
    def fixing_dates(cls, spec, v, path, pers=None):
        pers = cls.periods(spec, v) if pers is None else pers
        with obs.reading(v, path + ".index"):
            own = FloatingRate.requirements(spec, v, pers)
        return merge_requirements(own, conditions_requirements(spec, v, path, pers[-1].end))

    @classmethod
    def cashflows(cls, spec, p: PCtx, path, pers=None):
        pers = cls.periods(spec, p.v) if pers is None else pers
        diag = {}
        r = FloatingRate.rates(spec, p, pers, diag)
        return cls.finish(spec, p, path, pers, r * np.array([q.tau for q in pers])[:, None], diag)


class _Range(SpecModel):
    observable: str
    lower: PeriodValue | None = Field(None, description="lower bound" + _PV + "; null = unbounded")
    upper: PeriodValue | None = Field(None, description="upper bound" + _PV + "; null = unbounded")
    lower_inclusive: bool = True
    upper_inclusive: bool = True

    @model_validator(mode="after")
    def _bounds(self):
        if self.lower is None and self.upper is None:
            raise ValueError("give `lower`, `upper` or both")
        if isinstance(self.lower, (int, float)) and isinstance(self.upper, (int, float)) and self.lower > self.upper:
            raise ValueError("lower must be <= upper")
        return self


@register("leg", "range_accrual")
class RangeAccrual(_PeriodLeg):
    description = ("Daily range accrual: coupon = notional * rate * yearfraction * (weighted days on which EVERY "
                   "`range` condition holds / weighted observation days). Rate: fixed `rate`, or a floating `index` "
                   "with the floating_leg fields. A range reads any observable: one underlying, a rate, a spread or "
                   "basket (`linear`), a worst-of (`min_of`); several ranges = all underlyings must be in range.")

    class Spec(_PeriodSpec, _RateFields):
        rate: PeriodValue | None = Field(None, description="fixed coupon rate (or give `index`)" + _PV)
        index: str | None = Field(None, description="floating coupon index observable (or give `rate`)")
        day_count: str = "Thirty360.BondBasis"
        range: list[_Range] = Field(min_length=1, description="all must hold on a day for it to accrue; a period "
                                                              "where every bound is null accrues every day")
        observation_calendar: str = "WeekendsOnly"
        day_basis: Literal["business", "calendar"] = Field(
            "business", description="business: each business day weighs 1; calendar: every calendar day counts, "
                                    "carrying the previous business day's observation")
        range_cutoff_days: int = Field(0, ge=0, description="the last n observation days of a period use the "
                                                            "observation of the business day before them")

        @model_validator(mode="after")
        def _rate(self):
            if (self.rate is None) == (self.index is None):
                raise ValueError("give exactly one of `rate` (fixed) or `index` (floating)")
            if self.rate is not None:
                extra = sorted(self.model_fields_set & _RATE_FIELDS)
                if extra:
                    raise ValueError(f"{extra} apply to a floating `index`, not to a fixed `rate`")
            return self

    @staticmethod
    def observations(spec, per: Period):
        """(fixing date per observation day, weight per observation day) or None if no business day."""
        cal = cv.calendar(spec.observation_calendar)
        bdays = [cv.from_ql(d) for d in cal.businessDayList(cv.to_ql(per.start), cv.to_ql(per.end) - 1)]
        if not bdays:
            return None
        fix = list(bdays)
        n = spec.range_cutoff_days
        if 0 < n < len(bdays):          # n >= len(bdays) is reported by validate
            fix = bdays[:len(bdays) - n] + [bdays[len(bdays) - n - 1]] * n
        if spec.day_basis == "calendar":
            # each business day also counts the non-business days up to the next one (or the period end);
            # non-business days before the first business day count with it
            nxt = bdays[1:] + [per.end]
            w = np.array([(b - a).days for a, b in zip(bdays, nxt)], dtype=float)
            w[0] += (bdays[0] - per.start).days
        else:
            w = np.ones(len(bdays))
        return fix, w

    @classmethod
    def bounds(cls, spec, v, pers, path=None):
        """Per range entry: (lower, upper) arrays over `pers`, NaN = unbounded; None if invalid."""
        allp = cls.all_periods(spec, v)
        idx = [q.i for q in pers]
        out = []
        for i, r in enumerate(spec.range):
            lo = per_period(v, r.lower, allp, path and f"{path}.range[{i}].lower", allow_none=True)
            hi = per_period(v, r.upper, allp, path and f"{path}.range[{i}].upper", allow_none=True)
            if lo is None or hi is None:
                return None
            out.append((lo[idx], hi[idx]))
        return out

    @classmethod
    def validate(cls, spec, v, path):
        pers = cls.validate_periods(spec, v, path)
        if not _parse(v, path, [("observation_calendar", spec.observation_calendar, cv.calendar)]) or not pers:
            return
        if spec.index is not None:
            FloatingRate.validate(spec, v, path, pers)
        else:
            cls.values(spec, v, pers, "rate", path)
        bounds = cls.bounds(spec, v, pers, path)
        for i, (lo, hi) in enumerate(bounds or []):
            crossed = [q for q, a, b in zip(pers, lo, hi) if a > b]
            if crossed:
                v.report.error(f"{path}.range[{i}]", f"lower > upper in {len(crossed)} period(s), first "
                                                     f"{crossed[0].start}..{crossed[0].end}")
        for q in pers:
            o = cls.observations(spec, q)
            if o is None:
                v.report.error(path + ".observation_calendar", f"period {q.start}..{q.end} has no business day")
                return
            if spec.range_cutoff_days and spec.range_cutoff_days >= len(o[0]):
                v.report.error(path + ".range_cutoff_days", f"{spec.range_cutoff_days} cut-off days leave no free "
                                                            f"observation in the period {q.start}..{q.end}")
                return
        for i, r in enumerate(spec.range):
            rp = f"{path}.range[{i}].observable"
            if obs.known(v, r.observable, rp):
                obs.require_stochastic(v, r.observable, rp, "a range condition")

    @classmethod
    def fixing_dates(cls, spec, v, path, pers=None):
        pers = cls.periods(spec, v) if pers is None else pers
        dates = sorted({d for q in pers for d in cls.observations(spec, q)[0]})
        reqs = []
        for i, r in enumerate(spec.range):
            with obs.reading(v, f"{path}.range[{i}].observable"):
                reqs.append(obs.requirements(v, r.observable, dates))
        if spec.index is not None:
            with obs.reading(v, path + ".index"):
                reqs.append(FloatingRate.requirements(spec, v, pers))
        return merge_requirements(*reqs, conditions_requirements(spec, v, path, pers[-1].end))

    @classmethod
    def fraction(cls, spec, p: PCtx, per, bounds):
        """Accrual fraction per path; bounds = [(lower, upper)] for this period, NaN = unbounded."""
        fix, w = cls.observations(spec, per)
        uniq = sorted(set(fix))
        ok = np.ones((len(uniq), p.n_paths), dtype=bool)
        for r, (lo, hi) in zip(spec.range, bounds):
            if np.isnan(lo) and np.isnan(hi):
                continue
            x = p.obs(r.observable, uniq)
            if not np.isnan(lo):
                ok &= (x >= lo) if r.lower_inclusive else (x > lo)
            if not np.isnan(hi):
                ok &= (x <= hi) if r.upper_inclusive else (x < hi)
        pos = {d: i for i, d in enumerate(uniq)}
        return w @ ok[[pos[d] for d in fix]] / w.sum()

    @classmethod
    def cashflows(cls, spec, p: PCtx, path, pers=None):
        pers = cls.periods(spec, p.v) if pers is None else pers
        diag = {}
        bounds = cls.bounds(spec, p.v, pers)
        frac = np.vstack([cls.fraction(spec, p, q, [(lo[k], hi[k]) for lo, hi in bounds])
                          for k, q in enumerate(pers)])
        rate = cls.values(spec, p.v, pers, "rate") if spec.index is None else FloatingRate.rates(spec, p, pers, diag)
        diag["mean_accrual_fraction_by_period"] = [round(float(f.mean()), 6) for f in frac]
        return cls.finish(spec, p, path, pers, rate * frac * np.array([q.tau for q in pers])[:, None], diag)
