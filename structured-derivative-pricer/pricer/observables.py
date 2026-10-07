"""Observables: named quantities that legs, range conditions and barriers read on fixing dates.

An observable evaluates to an array of shape (len(dates), n_paths), or (len(dates), 1) when it
is the same on every path (a forward rate under an equity model with deterministic rates).

    equity          level S_t, or performance S_t / S_ref, of a simulated underlying
    term_rate       simple forward rate of an IBOR-style index (tenor, day count, spot lag)
    swap_rate       par swap rate (CMS) on one curve
    overnight_rate  one-business-day simple rate (SOFR, ESTR, TONA, ...)
    linear          constant + sum of weight * observable (spread, basket, inverse floater)
    min_of, max_of  worst-of / best-of several observables

Every model underlying is also an observable under its own name (its level), so `SPX` works
without a declaration. Rate observables are read from the model: stochastic under hull_white,
deterministic forwards of today's curve under the equity models. Fixing dates before the
valuation date are read from the observable's `history` (equity: closing levels, also from
trade.history), so a seasoned trade mixes known fixings with simulated ones.
"""
from __future__ import annotations

import contextlib
import datetime as dt
from typing import Literal

import numpy as np
import QuantLib as ql
from pydantic import Field, model_validator

from . import conventions as cv
from .registry import SpecModel, register

RATES = "rates"     # simulation-state key of a stochastic rates model (Hull-White x and its integral)


# ------------------------------------------------------------------ lookup / evaluation
def lookup(v, name):
    """(factory, spec) of a declared observable or of a model underlying used by name; None if unknown."""
    if name in v.observables:
        return v.observables[name]
    if name in v.underlyings:
        return EquityObs, EquityObs.Spec(underlying=name)
    return None


def known(v, name, path) -> bool:
    if lookup(v, name) is not None:
        return True
    choices = list(v.observables) + list(v.underlyings)
    v.report.error(path, f"observable '{name}' is not defined.{cv.suggest(name, choices)}",
                   f"declare it under `observables:`; defined: {sorted(v.observables)}; "
                   f"model underlyings usable by name: {v.underlyings}")
    return False


def is_stochastic(v, name) -> bool:
    cls, spec = lookup(v, name)
    if cls.is_leaf:
        return cls.stochastic(spec, v)
    return any(is_stochastic(v, n) for n in cls.deps(spec))


def require_stochastic(v, name, path, what) -> bool:
    """Error when a nonlinear feature (cap, floor, range, barrier) reads a deterministic observable."""
    if lookup(v, name) is None or is_stochastic(v, name):
        return True
    v.report.error(path, f"{what} on observable '{name}', which is deterministic under model "
                         f"'{v.model_name}' (rates are not simulated), so it would get only intrinsic value",
                   "use model hull_white for rate optionality, or remove the nonlinearity")
    return False


def requirements(v, name, dates) -> dict:
    """Simulation state needed to evaluate `name` on the future dates among `dates`.

    This is the one place a component declares what it reads. Dates before the valuation date are not
    simulated: they are recorded in v.past_reads (leaf observable -> reader path -> dates) and served
    from history by `evaluate`, whatever the component. `check_history` then validates them all at once."""
    cls, spec = lookup(v, name)
    if not cls.is_leaf:
        out = {}
        for n in cls.deps(spec):
            for k, ds in requirements(v, n, dates).items():
                out.setdefault(k, set()).update(ds)
        return {k: sorted(ds) for k, ds in out.items()}
    past = {d for d in dates if d < v.valuation_date}
    if past:
        v.past_reads.setdefault(name, {}).setdefault(v.reader, set()).update(past)
    future = sorted({d for d in dates if d >= v.valuation_date})
    return cls.requirements(spec, v, future) if future else {}


@contextlib.contextmanager
def reading(v, path, sticky=False):
    """Attribute the reads declared inside the block to `path` (for error messages). A sticky reader keeps its
    attribution through nested blocks, e.g. a TARN re-reading a leg's paid coupons."""
    before = (v.reader, v.reader_sticky)
    if not v.reader_sticky:
        v.reader, v.reader_sticky = path, sticky
    try:
        yield
    finally:
        v.reader, v.reader_sticky = before


def source(spec) -> str:
    """The observable a component spec reads: its `underlying` (an underlying is the observable of its own
    name) or its `observable`. Barriers, options and exercise options take exactly one of the two."""
    return spec.underlying if spec.underlying is not None else spec.observable


def past_value(v, cls, spec, d):
    """Known fixing of a leaf observable on a past date d, or None. Equity history holds levels: an
    equity observable's own `history`, else trade.history of its underlying."""
    if hasattr(cls, "past_value"):
        return cls.past_value(spec, v, d)
    return getattr(spec, "history", {}).get(d)


def missing_history(v, name, dates) -> list:
    """Past dates among `dates` on which `name` (or a leaf it depends on) has no known fixing."""
    cls, spec = lookup(v, name)
    if not cls.is_leaf:
        return sorted({d for n in cls.deps(spec) for d in missing_history(v, n, dates)})
    return sorted(d for d in dates if d < v.valuation_date and past_value(v, cls, spec, d) is None)


def check_history(v):
    """Report, per reader, every past fixing date recorded by `requirements` that history does not cover."""
    for name, readers in v.past_reads.items():
        cls, spec = lookup(v, name)
        if cls is EquityObs:
            where = f"the fixings file under equities.{spec.underlying}, or trade.history.{spec.underlying}"
            what = "closing level"
        else:
            key = getattr(spec, "fixings_key", None) or name
            where = f"the fixings file under rates.{key}, or observables.{name}.history"
            what = "fixing"
        for path, dates in readers.items():
            missing = missing_history(v, name, dates)
            if missing:
                v.report.error(path, f"'{name}' needs {len(missing)} past {what}(s) before the valuation date "
                                     f"(first {missing[0]}, last {missing[-1]})",
                               f"give them in {where} as {{YYYY-MM-DD: value}}")


def evaluate(p, name, dates) -> np.ndarray:
    """Values on `dates` (any order): shape (len(dates), n_paths) or (len(dates), 1)."""
    cls, spec = lookup(p.v, name)
    if not cls.is_leaf:
        x = cls.combine(spec, [evaluate(p, n, dates) for n in cls.deps(spec)])
    else:
        x = evaluate_leaf(p, cls, spec, dates)
    if getattr(p, "reads", None) is not None:          # inspecting: keep what was computed, date by date
        p.reads.setdefault(name, {}).update(zip(dates, x))
    return x


def evaluate_leaf(p, cls, spec, dates) -> np.ndarray:
    """A leaf's values: simulated on dates >= the valuation date, known fixings before it. With only past
    dates no simulation is read, so validation can evaluate history with a context whose scenario is None."""
    v = p.v
    dates = list(dates)
    fut = [d for d in dates if d >= v.valuation_date]
    fvals = cls.values(spec, p, fut) if fut else np.zeros((0, 1))
    if len(fut) == len(dates):
        return fvals
    out = np.empty((len(dates), fvals.shape[1]))
    j = 0
    for i, d in enumerate(dates):
        if d >= v.valuation_date:
            out[i] = fvals[j]
            j += 1
        else:
            out[i] = past_value(v, cls, spec, d)
    return out


def fixing_conventions(v, name):
    """(fixing days, fixing calendar) shared by every rate index `name` depends on, or None."""
    cls, spec = lookup(v, name)
    if cls.is_leaf:
        return cls.fixing_conventions(spec) if hasattr(cls, "fixing_conventions") else None
    got = [fixing_conventions(v, n) for n in cls.deps(spec)]
    if not got or any(g is None for g in got) or len({(g[0], g[1].name()) for g in got}) != 1:
        return None
    return got[0]


def validate_all(v):
    """Validate every declared observable; report reference cycles."""
    state = {}

    def visit(name, stack):
        if state.get(name) == "done":
            return True
        if name in stack:
            v.report.error(f"observables.{name}", "circular observable reference: " + " -> ".join(stack + (name,)))
            return False
        cls, spec = v.observables[name]
        ok = True
        for i, n in enumerate(cls.deps(spec)):
            if not known(v, n, f"observables.{name}.{cls.dep_field}[{i}]"):
                ok = False
            elif n in v.observables:
                ok &= visit(n, stack + (name,))
        if ok:
            cls.validate(spec, v, f"observables.{name}")
        state[name] = "done"
        return ok

    for name in v.observables:
        if name in v.underlyings:
            v.report.error(f"observables.{name}", f"'{name}' is a model underlying; pick another observable name")
        visit(name, ())


# ------------------------------------------------------------------ factories
class ObservableFactory:
    """Leaves: validate, stochastic, requirements, values (future dates only), curves.
    Composites: deps, combine."""
    is_leaf = True
    dep_field = ""

    @staticmethod
    def deps(spec):
        return []

    @staticmethod
    def curves(spec):
        return []

    @staticmethod
    def validate(spec, v, path):
        pass


class _Leaf(SpecModel):
    history: dict[dt.date, float] = Field(
        default_factory=dict, description="past fixings {YYYY-MM-DD: value} for fixing dates before the valuation date")


@register("observable", "equity")
class EquityObs(ObservableFactory):
    description = ("A simulated underlying: `level` is the price S_t; `performance` is S_t / S_ref "
                   "(S_ref = trade.reference_levels, default today's spot). Every model underlying is also "
                   "usable by name as its level without declaring it. Past closing levels come from `history` "
                   "or, shared by every observable of the underlying, from trade.history.")

    class Spec(_Leaf):
        underlying: str
        mode: Literal["level", "performance"] = "level"
        history: dict[dt.date, float] = Field(
            default_factory=dict, description="past values of THIS observable {YYYY-MM-DD: x}, in its own units: a "
                                              "level, or S/S_ref in performance mode. Usually closing levels are "
                                              "given once per underlying (fixings file / trade.history) instead")

    @staticmethod
    def validate(spec, v, path):
        if spec.underlying not in v.underlyings:
            v.report.error(path + ".underlying",
                           f"underlying '{spec.underlying}' is not simulated by the model."
                           f"{cv.suggest(spec.underlying, v.underlyings)}", f"model underlyings: {v.underlyings}")
        bad = sorted(d for d, x in spec.history.items() if not x > 0)
        if bad:
            v.report.error(path + ".history", f"equity {spec.mode}s must be > 0 ({len(bad)} bad, first {bad[0]})")

    @staticmethod
    def stochastic(spec, v):
        return True

    @staticmethod
    def requirements(spec, v, dates):
        return {spec.underlying: list(dates)}

    @staticmethod
    def values(spec, p, dates):
        x = p.scenario.equity[spec.underlying].loc[list(dates)].values
        return x / p.v.reference_levels[spec.underlying] if spec.mode == "performance" else x

    @staticmethod
    def past_value(spec, v, d):
        """Own history as given (the observable's units, like any observable's history); else the underlying's
        closing level from the fixings file / trade.history, divided by S_ref in performance mode."""
        if d in spec.history:
            return spec.history[d]
        x = v.history.get(spec.underlying, {}).get(d)
        if x is None:
            return None
        return x / v.reference_levels[spec.underlying] if spec.mode == "performance" else x


# Rate observables delegate every convention (value date, index maturity, end-of-month, day count,
# fixing calendar, swap fixed-leg schedule) to QuantLib index objects. Only the discount factors
# come from the model, so the value is QuantLib's forecast fixing under deterministic rates and the
# same formula on each simulated curve under Hull-White.
_IBOR_FAMILIES = {n: getattr(ql, n) for n in ("Euribor", "USDLibor", "GBPLibor", "JPYLibor", "Tibor", "Bibor", "Jibar",
                                              "Shibor", "Bbsw", "Pribor", "Robor", "Wibor", "Zibor", "THBFIX")}
_OVERNIGHT = {n: getattr(ql, n) for n in ("Sofr", "Estr", "Sonia", "Saron", "Tonar", "Corra", "Aonia", "Eonia",
                                          "FedFunds") if hasattr(ql, n)}
_SWAP_FAMILIES = {n: getattr(ql, n) for n in ("EuriborSwapIsdaFixA", "EuriborSwapIsdaFixB", "EuriborSwapIfrFix",
                                              "UsdLiborSwapIsdaFixAm", "UsdLiborSwapIsdaFixPm", "JpyLiborSwapIsdaFixAm",
                                              "GbpLiborSwapIsdaFix", "ChfLiborSwapIsdaFix")}
_NO_CURVE = ql.YieldTermStructureHandle()


class _RateSpec(_Leaf):
    curve: str = Field(description="market curve the index is projected from")
    fixings_key: str | None = Field(
        None, description="entry of the fixings file's `rates` this index reads (default: the observable's name)")
    quantlib_index: str | None = Field(
        None, description="use a QuantLib index's conventions instead of the custom fields below")

    @model_validator(mode="after")
    def _preset(self):
        if self.quantlib_index is not None:
            custom = sorted(self.model_fields_set & set(self._custom_fields()))
            if custom:
                raise ValueError(f"{custom} come from quantlib_index '{self.quantlib_index}'; remove them")
            if self.quantlib_index not in self._presets():
                raise ValueError(f"unknown quantlib_index '{self.quantlib_index}'. Allowed: {sorted(self._presets())}")
        return self


class _RateObs(ObservableFactory):
    @staticmethod
    def curves(spec):
        return [spec.curve]

    @classmethod
    def validate(cls, spec, v, path):
        if spec.curve not in v.curve_names:
            v.report.error(path + ".curve", f"curve '{spec.curve}' not in market data.{cv.suggest(spec.curve, v.curve_names)}",
                           f"market curves: {sorted(v.curve_names)}")
        for f, parse in (("calendar", cv.calendar), ("day_count", cv.day_count), ("fixed_day_count", cv.day_count),
                         ("convention", cv.bdc), ("tenor", cv.period), ("fixed_frequency", cv.period)):
            if hasattr(spec, f):
                try:
                    parse(getattr(spec, f))
                except ValueError as e:
                    v.report.error(f"{path}.{f}", str(e))

    @staticmethod
    def stochastic(spec, v):
        return v.stochastic_rates

    @staticmethod
    def requirements(spec, v, dates):
        return {RATES: list(dates)} if v.stochastic_rates else {}

    @classmethod
    def fixing_conventions(cls, spec):
        """(fixing days, fixing calendar) of the QuantLib index, the defaults of legs fixed on it."""
        idx = cls.ql_index(spec)
        return idx.fixingDays(), idx.fixingCalendar()

    @staticmethod
    def _fixing(idx, d):
        """QuantLib fixing date for observation date d (a non-fixing day reads the previous fixing day)."""
        return idx.fixingCalendar().adjust(cv.to_ql(d), ql.Preceding)


@register("observable", "term_rate")
class TermRate(_RateObs):
    description = ("IBOR-style term rate fixed on date t, as QuantLib's IborIndex forecasts it: "
                   "(P(t,start)/P(t,end) - 1) / yearfraction with start = index.valueDate(t) and "
                   "end = index.maturityDate(start), on the model's curve as seen at t. Conventions from "
                   "`quantlib_index` (Euribor, USDLibor, Tibor, ...) or the custom fields.")

    class Spec(_RateSpec):
        tenor: str = Field(description="index tenor, e.g. 3M")
        calendar: str = "WeekendsOnly"
        day_count: str = "Actual360"
        spot_lag: int = Field(2, ge=0, description="business days from fixing to the index start")
        convention: str = "ModifiedFollowing"
        end_of_month: bool = False

        @staticmethod
        def _custom_fields():
            return ("calendar", "day_count", "spot_lag", "convention", "end_of_month")

        @staticmethod
        def _presets():
            return _IBOR_FAMILIES

    @staticmethod
    def ql_index(spec):
        if spec.quantlib_index:
            return _IBOR_FAMILIES[spec.quantlib_index](cv.period(spec.tenor), _NO_CURVE)
        return ql.IborIndex("term_rate", cv.period(spec.tenor), spec.spot_lag, ql.Currency(), cv.calendar(spec.calendar),
                            cv.bdc(spec.convention), spec.end_of_month, cv.day_count(spec.day_count), _NO_CURVE)

    @classmethod
    def values(cls, spec, p, dates):
        idx = cls.ql_index(spec)
        dc = idx.dayCounter()
        rows = []
        for d in dates:
            s = idx.valueDate(cls._fixing(idx, d))
            e = idx.maturityDate(s)
            b = p.scenario.bond(spec.curve, d, [cv.from_ql(s), cv.from_ql(e)])
            rows.append((b[0] / b[1] - 1.0) / dc.yearFraction(s, e))
        return np.vstack(rows)


@register("observable", "swap_rate")
class SwapRate(_RateObs):
    description = ("Par swap rate (CMS) fixed on date t, as QuantLib's SwapIndex defines its underlying swap "
                   "(value date, maturity, fixed-leg tenor / convention / day count, backward schedule): "
                   "(P(t,T0) - P(t,Tn)) / sum tau_i P(t,Ti) on one curve. Conventions from `quantlib_index` "
                   "(EuriborSwapIsdaFixA, UsdLiborSwapIsdaFixAm, ...) or the custom fields.")

    class Spec(_RateSpec):
        tenor: str = Field(description="swap tenor, e.g. 10Y")
        calendar: str = "WeekendsOnly"
        fixed_frequency: str = "1Y"
        fixed_day_count: str = "Thirty360.BondBasis"
        spot_lag: int = Field(2, ge=0)
        convention: str = "ModifiedFollowing"

        @staticmethod
        def _custom_fields():
            return ("calendar", "fixed_frequency", "fixed_day_count", "spot_lag", "convention")

        @staticmethod
        def _presets():
            return _SWAP_FAMILIES

    @staticmethod
    def ql_index(spec):
        if spec.quantlib_index:
            return _SWAP_FAMILIES[spec.quantlib_index](cv.period(spec.tenor), _NO_CURVE)
        cal, freq = cv.calendar(spec.calendar), cv.period(spec.fixed_frequency)
        float_leg = ql.IborIndex("swap_float", freq, spec.spot_lag, ql.Currency(), cal, cv.bdc(spec.convention),
                                 False, ql.Actual360(), _NO_CURVE)        # not used by the single-curve par rate
        return ql.SwapIndex("swap_rate", cv.period(spec.tenor), spec.spot_lag, ql.Currency(), cal, freq,
                            cv.bdc(spec.convention), cv.day_count(spec.fixed_day_count), float_leg)

    @classmethod
    def values(cls, spec, p, dates):
        idx = cls.ql_index(spec)
        dc, conv = idx.dayCounter(), idx.fixedLegConvention()
        rows = []
        for d in dates:
            start = idx.valueDate(cls._fixing(idx, d))
            sch = list(ql.Schedule(start, idx.maturityDate(start), idx.fixedLegTenor(), idx.fixingCalendar(),
                                   conv, conv, ql.DateGeneration.Backward, False))
            taus = np.array([dc.yearFraction(a, b) for a, b in zip(sch[:-1], sch[1:])])
            b = p.scenario.bond(spec.curve, d, [cv.from_ql(x) for x in sch])
            rows.append((b[0] - b[-1]) / (taus[:, None] * b[1:]).sum(axis=0))
        return np.vstack(rows)


@register("observable", "overnight_rate")
class OvernightRate(_RateObs):
    description = ("Overnight rate fixed on date t, applying from t to the next business day, as QuantLib's "
                   "OvernightIndex forecasts it. The index of compounded_in_arrears floating legs. Conventions from "
                   "`quantlib_index` (Sofr, Estr, Sonia, Tonar, ...) or the custom fields.")

    class Spec(_RateSpec):
        calendar: str = "WeekendsOnly"
        day_count: str = "Actual360"

        @staticmethod
        def _custom_fields():
            return ("calendar", "day_count")

        @staticmethod
        def _presets():
            return _OVERNIGHT

    @staticmethod
    def ql_index(spec):
        if spec.quantlib_index:
            return _OVERNIGHT[spec.quantlib_index](_NO_CURVE)
        return ql.OvernightIndex("overnight_rate", 0, ql.Currency(), cv.calendar(spec.calendar),
                                 cv.day_count(spec.day_count), _NO_CURVE)

    @classmethod
    def values(cls, spec, p, dates):
        idx = cls.ql_index(spec)
        dc = idx.dayCounter()
        rows = []
        for d in dates:
            s = idx.valueDate(cls._fixing(idx, d))
            e = idx.maturityDate(s)
            b = p.scenario.bond(spec.curve, d, [cv.from_ql(s), cv.from_ql(e)])
            rows.append((b[0] / b[1] - 1.0) / dc.yearFraction(s, e))
        return np.vstack(rows)


class _Composite(ObservableFactory):
    is_leaf = False


class _Term(SpecModel):
    observable: str
    weight: float


@register("observable", "linear")
class Linear(_Composite):
    description = ("constant + sum(weight_i * observable_i). Spread: weights [1, -1]; basket: positive weights; "
                   "inverse floater rate: constant K, weight -m on the index.")
    dep_field = "terms"

    class Spec(SpecModel):
        terms: list[_Term] = Field(min_length=1)
        constant: float = 0.0

    @staticmethod
    def deps(spec):
        return [t.observable for t in spec.terms]

    @staticmethod
    def combine(spec, arrays):
        out = spec.constant
        for t, a in zip(spec.terms, arrays):
            out = out + t.weight * a
        return out


class _ExtremumSpec(SpecModel):
    observables: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def _distinct(self):
        if len(set(self.observables)) != len(self.observables):
            raise ValueError("observables must be distinct")
        return self


@register("observable", "min_of")
class MinOf(_Composite):
    description = "Smallest of several observables on each date (worst-of; use performance observables)."
    dep_field = "observables"

    class Spec(_ExtremumSpec):
        pass

    @staticmethod
    def deps(spec):
        return list(spec.observables)

    @staticmethod
    def combine(spec, arrays):
        return np.minimum.reduce(np.broadcast_arrays(*arrays))


@register("observable", "max_of")
class MaxOf(_Composite):
    description = "Largest of several observables on each date (best-of)."
    dep_field = "observables"

    class Spec(_ExtremumSpec):
        pass

    @staticmethod
    def deps(spec):
        return list(spec.observables)

    @staticmethod
    def combine(spec, arrays):
        return np.maximum.reduce(np.broadcast_arrays(*arrays))
