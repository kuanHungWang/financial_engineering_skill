"""path_dependent leg: a coupon that depends on a state carried from period to period.

Each accrual period k of the schedule (the same periods as fixed_leg) runs one step, on every path:

    x_k   = inputs fixed for period k            (observable fixings, compounded rates, range-accrual fractions)
    d     = define(x_k, S_{k-1})                 named intermediate values, in order (optional)
    S_k   = evolve(S_{k-1}, x_k, d)              every state updates at once: right-hand sides read S_{k-1}
    c_k   = payoff(x_k, S_k, d, prev(S) = S_{k-1})
    cashflow_k = position * notional_k * c_k     paid on the period's payment date, gated by leg conditions

The functions are formulas of pricer/expressions.py (piecewise polynomial with digital switches). They read
inputs, per-period `parameters`, the leg's states and defines, and the built-ins below. A snowball is
`C: max(C + K - L, 0)` with payoff `C * tau`; a memory coupon carries the unpaid coupons; a ratchet floater
clips the new coupon around the previous one.

    tau          accrual year fraction of the period (day_count)
    period       0-based position of the period in the schedule (paid periods included)
    n_periods    number of periods of the schedule
    is_first     1 on the first period of the schedule, else 0
    is_last      1 on the last period of the schedule, else 0

The state starts at each state's `initial` before the first period and evolves through every period, paid
ones included, so a seasoned trade reads its past fixings from history like any other component. `known_state`
instead gives the state after a paid period (when the past fixings are not available) and starts there.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Literal

import numpy as np
import pandas as pd
import QuantLib as ql
from pydantic import Field, model_validator

from . import conventions as cv
from . import observables as obs
from .coupons import (FloatingRate, PeriodValue, RangeAccrual, _FixingFields, _parse, _PeriodLeg, _PeriodSpec,
                      _PV, _Range, per_period)
from .expressions import FUNCTIONS, Expr, ExprError
from .observables import OvernightRate
from .payoffs import PCtx, VCtx, _resolve_date, conditions_requirements, merge_requirements
from .registry import SpecModel, register
from .spec import DateLike

BUILTINS = ("tau", "period", "n_periods", "is_first", "is_last")
_RANGE_FIELDS = {"range", "observation_calendar", "day_basis", "range_cutoff_days"}
_FIXING_FIELDS = set(_FixingFields.model_fields)


class _Input(_FixingFields):
    observable: str | None = Field(None, description="observable fixed once per period (or give `range`)")
    fixing: Literal["in_advance", "in_arrears", "compounded_in_arrears"] | None = Field(
        None, description="required with `observable`: in_advance (fixing_lag business days before the accrual "
                          "start), in_arrears (before the accrual end; an equity observation date), or "
                          "compounded_in_arrears (an overnight_rate over the period, optional lockout_days)")
    fixing_lag: int | None = Field(None, ge=0, description="default: the index's fixing days; 0 for an observable "
                                                           "without one (an equity: the period date itself)")
    range: list[_Range] | None = Field(
        None, description="instead of `observable`: the input is the period's range-accrual fraction in [0, 1], the "
                          "weighted share of observation days on which every range holds (as range_accrual)")
    observation_calendar: str = "WeekendsOnly"
    day_basis: Literal["business", "calendar"] = "business"
    range_cutoff_days: int = Field(0, ge=0)

    @model_validator(mode="after")
    def _kind(self):
        if (self.observable is None) == (self.range is None):
            raise ValueError("give exactly one of `observable` (a fixing) or `range` (an accrual fraction)")
        if self.observable is not None:
            if self.fixing is None:
                raise ValueError("give `fixing`: in_advance, in_arrears or compounded_in_arrears")
            extra = sorted(self.model_fields_set & (_RANGE_FIELDS - {"range"}))
            if extra:
                raise ValueError(f"{extra} apply to a `range` input")
        else:
            extra = sorted(self.model_fields_set & _FIXING_FIELDS)
            if extra:
                raise ValueError(f"{extra} apply to an `observable` input")
        return self


class _State(SpecModel):
    initial: float = Field(description="value before the first period of the schedule")
    evolve: str = Field(description="formula of the new value; names of states read their previous values")


class _KnownState(SpecModel):
    period_end: DateLike = Field(description="accrual end of a paid period")
    values: dict[str, float] = Field(description="every state's value after that period")


@register("leg", "path_dependent")
class PathDependentLeg(_PeriodLeg):
    description = ("Path-dependent coupon carrying a state from period to period: per period, S = evolve(S_prev, x) "
                   "for every `state` at once, then coupon = notional * payoff(x, S) (prev(S) = S_prev), with x the "
                   "period's `inputs` (observable fixings, compounded rates, range-accrual fractions). Formulas use "
                   "+ - * / **, min, max, clip, abs, comparisons as 0/1 indicators, where, and/or/not, exp, log, "
                   "sqrt, and the built-ins tau, period, n_periods, is_first, is_last. Snowball, snowrange, memory / "
                   "phoenix coupons, ratchet floaters, cliquets, lookbacks, cumulative caps. A state can be a "
                   "bermudan_call regressor as '<leg id>.<state>'.")

    class Spec(_PeriodSpec):
        day_count: str = Field("Thirty360.BondBasis", description="accrual fraction `tau`")
        inputs: dict[str, _Input] = Field(default_factory=dict, description="name -> what is fixed each period")
        parameters: dict[str, PeriodValue] = Field(
            default_factory=dict, description="name -> constant or per-period value" + _PV)
        define: dict[str, str] = Field(
            default_factory=dict, description="name -> formula of an intermediate value, evaluated in order before "
                                              "the states update (states read their previous values)")
        state: dict[str, _State] = Field(default_factory=dict, description="name -> {initial, evolve}")
        payoff: str = Field(description="coupon as a fraction of notional, e.g. 'C * tau'; states read their "
                                        "updated values, prev(S) the value before the update")
        known_state: _KnownState | None = Field(
            None, description="state after a paid period, when its past fixings are unavailable; evolution "
                              "starts after it")

        @model_validator(mode="after")
        def _names(self):
            seen = {}
            for section in ("inputs", "parameters", "define", "state"):
                for name in getattr(self, section):
                    if not name.isidentifier():
                        raise ValueError(f"{section}.{name}: a name must be an identifier (letters, digits, _)")
                    if name in BUILTINS or name in FUNCTIONS:
                        raise ValueError(f"{section}.{name}: '{name}' is reserved (built-ins {list(BUILTINS)}, "
                                         f"functions {sorted(FUNCTIONS)})")
                    if name in seen:
                        raise ValueError(f"'{name}' is defined in both {seen[name]} and {section}")
                    seen[name] = section
            return self

    # ------------------------------------------------------------------ formulas
    @staticmethod
    def formula_texts(spec):
        """(path suffix, name or None, text) of every formula, in evaluation order."""
        return ([(f"define.{k}", k, e) for k, e in spec.define.items()] +
                [(f"state.{k}.evolve", k, s.evolve) for k, s in spec.state.items()] + [("payoff", None, spec.payoff)])

    @classmethod
    def formulas(cls, spec):
        """(path suffix, name or None, Expr) of every formula, in evaluation order. Raises ExprError."""
        return [(suffix, name, Expr(text)) for suffix, name, text in cls.formula_texts(spec)]

    @classmethod
    def check_formulas(cls, spec, v: VCtx, path):
        """Report syntax errors, unknown names and misplaced prev(); return the parsed formulas or None."""
        parsed, ok = [], True
        for suffix, name, text in cls.formula_texts(spec):
            try:
                parsed.append((suffix, name, Expr(text)))
            except ExprError as e:
                v.report.error(f"{path}.{suffix}", str(e))
                ok = False
        if not ok:
            return None
        base = set(spec.inputs) | set(spec.parameters) | set(BUILTINS) | set(spec.state)
        defined = []
        for suffix, name, e in parsed:
            known = base | set(defined if suffix.startswith("define.") else spec.define)
            unknown = sorted(e.names - known)
            if unknown:
                later = [u for u in unknown if u in spec.define]
                v.report.error(f"{path}.{suffix}", f"unknown name(s) {unknown} in '{e.text}'"
                               + (f": a define reads only the defines before it, and {later} come later"
                                  if later else ""),
                               "reorder the defines" if later else
                               f"available: inputs {sorted(spec.inputs)}, parameters {sorted(spec.parameters)}, "
                               f"states {sorted(spec.state)}, defines {sorted(spec.define)}, built-ins "
                               f"{list(BUILTINS)}")
                ok = False
            if e.prev_names and suffix != "payoff":
                v.report.error(f"{path}.{suffix}", "prev() is only for the payoff: here a state name already reads "
                                                   "its previous value")
                ok = False
            bad_prev = sorted(e.prev_names - set(spec.state))
            if bad_prev and suffix == "payoff":
                v.report.error(f"{path}.{suffix}", f"prev() takes a state name, not {bad_prev}")
                ok = False
            if name and suffix.startswith("define."):
                defined.append(name)
        return parsed if ok else None

    @staticmethod
    def input_expander(spec, parsed):
        """A function mapping names to the inputs they ultimately depend on (through defines and states, whose
        mutual dependence across periods is a fixed point)."""
        exprs = {name: e for _, name, e in parsed if name}
        dep = {k: {k} for k in spec.inputs} | {k: set() for k in exprs}

        def expand(names):
            return set().union(*(dep.get(n, set()) for n in names))
        changed = True
        while changed:
            changed = False
            for k, e in exprs.items():
                new = dep[k] | expand(e.names)
                if new != dep[k]:
                    dep[k], changed = new, True
        return expand

    # ------------------------------------------------------------------ periods, inputs
    @classmethod
    def start_index(cls, spec, v: VCtx) -> int:
        """Index of the first period evolved: 0, or the one after `known_state`."""
        if spec.known_state is None:
            return 0
        end = v.dates.resolve(spec.known_state.period_end)
        return next(q.i for q in cls.all_periods(spec, v) if q.end == end) + 1

    @classmethod
    def run_periods(cls, spec, v: VCtx, pers):
        allp = cls.all_periods(spec, v)
        return allp[cls.start_index(spec, v):max(q.i for q in pers) + 1]

    @staticmethod
    def conventions(inp, v: VCtx):
        """(fixing lag, ql calendar or None) of an observable input: its own, else its index's, else (0, None)."""
        idx = obs.fixing_conventions(v, inp.observable)
        lag = inp.fixing_lag if inp.fixing_lag is not None else (idx[0] if idx else 0)
        cal = cv.calendar(inp.fixing_calendar) if inp.fixing_calendar else (idx[1] if idx else None)
        return lag, cal

    @classmethod
    def fixing_date(cls, inp, v: VCtx, per) -> dt.date:
        ref = per.start if inp.fixing == "in_advance" else per.end
        lag, cal = cls.conventions(inp, v)
        if cal is None:             # lag 0, no calendar: the period date itself
            return ref
        return cv.from_ql(cal.advance(cv.to_ql(ref), -lag, ql.Days))

    @staticmethod
    def compounding(inp):
        return SimpleNamespace(index=inp.observable, fixing="compounded_in_arrears", lockout_days=inp.lockout_days)

    @staticmethod
    def ranged(spec, inp):
        """The leg's period fields plus the input's range fields, as RangeAccrual reads them."""
        return SimpleNamespace(**dict(spec), **{k: getattr(inp, k) for k in _RANGE_FIELDS})

    @classmethod
    def known_date(cls, spec, v: VCtx, per) -> dt.date:
        """When period `per`'s inputs are all fixed (its state is known)."""
        out = []
        for inp in spec.inputs.values():
            if inp.range is not None:
                out.append(max(RangeAccrual.observations(cls.ranged(spec, inp), per)[0]))
            elif inp.fixing == "compounded_in_arrears":
                out.append(per.end)
            else:
                out.append(cls.fixing_date(inp, v, per))
        return max(out) if out else per.start

    # ------------------------------------------------------------------ validation
    @classmethod
    def validate(cls, spec, v: VCtx, path):
        pers = cls.validate_periods(spec, v, path)
        if not pers:
            return
        allp = cls.all_periods(spec, v)
        for name, value in spec.parameters.items():
            per_period(v, value, allp, f"{path}.parameters.{name}")
        if not cls.validate_known_state(spec, v, path, allp):
            return
        run = cls.run_periods(spec, v, pers)
        stochastic = {}
        for name, inp in spec.inputs.items():
            stochastic[name] = cls.validate_input(spec, inp, v, f"{path}.inputs.{name}", run)
        parsed = cls.check_formulas(spec, v, path)
        if parsed is None or None in stochastic.values():
            return
        expand = cls.input_expander(spec, parsed)
        for suffix, _, e in parsed:
            for names in e.nonlinear:
                ins = expand(names)
                if ins and not any(stochastic[i] for i in ins):
                    v.report.error(f"{path}.{suffix}",
                                   f"min/max/clip/abs or an indicator in '{e.text}' depends only on {sorted(ins)}, "
                                   f"deterministic under model '{v.model_name}', so it would get only intrinsic value",
                                   "use model hull_white for rate optionality, or remove the nonlinearity")
                    break

    @classmethod
    def validate_known_state(cls, spec, v: VCtx, path, allp) -> bool:
        k = spec.known_state
        if k is None:
            return True
        p = path + ".known_state"
        end = _resolve_date(v, k.period_end, p + ".period_end")
        if end is None:
            return False
        q = next((q for q in allp if q.end == end), None)
        if q is None:
            v.report.error(p + ".period_end", f"{end} is not an accrual end of schedule '{spec.schedule}'",
                           f"accrual ends: {[str(x.end) for x in allp[:6]]}{' ...' if len(allp) > 6 else ''}")
            return False
        if q.pay > v.valuation_date:
            v.report.error(p + ".period_end", f"the period ending {end} is paid on {q.pay}, after the valuation date",
                           "give the state after the last period paid on or before the valuation date")
            return False
        missing, extra = sorted(set(spec.state) - set(k.values)), sorted(set(k.values) - set(spec.state))
        if missing or extra:
            v.report.error(p + ".values", f"give exactly the states {sorted(spec.state)}"
                                          + (f"; missing {missing}" if missing else "")
                                          + (f"; unknown {extra}" if extra else ""))
            return False
        return True

    @classmethod
    def validate_input(cls, spec, inp, v: VCtx, path, run):
        """Check one input; return whether it is stochastic, or None if invalid (already reported)."""
        if inp.range is not None:
            ns = cls.ranged(spec, inp)
            if not _parse(v, path, [("observation_calendar", inp.observation_calendar, cv.calendar)]):
                return None
            bounds = RangeAccrual.bounds(ns, v, run, path)
            for i, (lo, hi) in enumerate(bounds or []):
                if (lo > hi).any():
                    v.report.error(f"{path}.range[{i}]", "lower > upper in some period")
            ok = bounds is not None
            for q in run:
                o = RangeAccrual.observations(ns, q)
                if o is None or (inp.range_cutoff_days and inp.range_cutoff_days >= len(o[0])):
                    v.report.error(path, f"period {q.start}..{q.end} has no free observation day")
                    return None
            for i, r in enumerate(inp.range):
                rp = f"{path}.range[{i}].observable"
                ok &= obs.known(v, r.observable, rp) and obs.require_stochastic(v, r.observable, rp, "a range condition")
            return True if ok else None
        if not obs.known(v, inp.observable, path + ".observable"):
            return None
        if inp.fixing_calendar and not _parse(v, path, [("fixing_calendar", inp.fixing_calendar, cv.calendar)]):
            return None
        if inp.fixing == "compounded_in_arrears":
            if obs.lookup(v, inp.observable)[0] is not OvernightRate:
                v.report.error(path + ".fixing", f"compounded_in_arrears needs an overnight_rate observable; "
                                                 f"'{inp.observable}' is {obs.lookup(v, inp.observable)[0].type_name}")
                return None
            for q in run:
                _, days, _, k, _, _, _ = FloatingRate._split(cls.compounding(inp), v, q)
                if k < 1:
                    v.report.error(path + ".lockout_days", f"lockout_days {inp.lockout_days} leaves no free day in "
                                                           f"the period {q.start}..{q.end}")
                    return None
        else:
            if inp.lockout_days:
                v.report.error(path + ".lockout_days", "lockout_days applies to fixing: compounded_in_arrears only")
            lag, cal = cls.conventions(inp, v)
            if lag and cal is None:
                v.report.error(path + ".fixing_calendar", f"fixing_lag {lag} needs a calendar: '{inp.observable}' has "
                                                          "no index fixing calendar", "set fixing_calendar")
                return None
        return obs.is_stochastic(v, inp.observable)

    # ------------------------------------------------------------------ reads
    @classmethod
    def fixing_dates(cls, spec, v: VCtx, path, pers=None):
        pers = cls.periods(spec, v) if pers is None else pers
        if min(q.i for q in pers) < cls.start_index(spec, v):
            v.report.error(path + ".known_state", "coupons before known_state are recomputed (by a target event "
                                                  "accumulating this leg) but their state is not known",
                           "give the target event's `accumulated`, or drop known_state and give the past fixings")
            return {}
        run = cls.run_periods(spec, v, pers)
        reqs = []
        for name, inp in spec.inputs.items():
            if inp.range is not None:
                dates = sorted({d for q in run for d in RangeAccrual.observations(cls.ranged(spec, inp), q)[0]})
                for i, r in enumerate(inp.range):
                    with obs.reading(v, f"{path}.inputs.{name}.range[{i}].observable"):
                        reqs.append(obs.requirements(v, r.observable, dates))
            else:
                with obs.reading(v, f"{path}.inputs.{name}.observable"):
                    if inp.fixing == "compounded_in_arrears":
                        reqs.append(FloatingRate.requirements(cls.compounding(inp), v, run))
                    else:
                        reqs.append(obs.requirements(v, inp.observable, [cls.fixing_date(inp, v, q) for q in run]))
        return merge_requirements(*reqs, conditions_requirements(spec, v, path, max(q.end for q in pers)))

    @classmethod
    def input_values(cls, spec, inp, p: PCtx, run) -> np.ndarray:
        """(len(run), n_paths) values of one input."""
        n = p.n_paths
        if inp.range is not None:
            ns = cls.ranged(spec, inp)
            bounds = RangeAccrual.bounds(ns, p.v, run)
            return np.vstack([RangeAccrual.fraction(ns, p, q, [(lo[k], hi[k]) for lo, hi in bounds])
                              for k, q in enumerate(run)])
        if inp.fixing == "compounded_in_arrears":
            ns = cls.compounding(inp)
            return np.vstack([np.broadcast_to(FloatingRate._compounded(ns, p, q), (n,)) for q in run])
        return p.obs(inp.observable, [cls.fixing_date(inp, p.v, q) for q in run])

    # ------------------------------------------------------------------ cashflows
    @classmethod
    def evolve(cls, spec, p: PCtx, path, run):
        """Run the recursion over `run`: (payoff (len(run), n), {state: (len(run), n)}, initial states)."""
        v, n = p.v, p.n_paths
        allp = cls.all_periods(spec, v)
        parsed = cls.formulas(spec)
        defines = [(name, e) for suffix, name, e in parsed if suffix.startswith("define.")]
        evolves = [(name, e) for suffix, name, e in parsed if suffix.startswith("state.")]
        payoff = parsed[-1][2]
        idx = [q.i for q in run]
        x = {name: cls.input_values(spec, inp, p, run) for name, inp in spec.inputs.items()}
        par = {name: per_period(v, value, allp)[idx] for name, value in spec.parameters.items()}
        init = dict(spec.known_state.values) if spec.known_state else {k: s.initial for k, s in spec.state.items()}
        state = {k: np.full(n, float(init[k])) for k in spec.state}
        pay = np.empty((len(run), n))
        hist = {k: np.empty((len(run), n)) for k in spec.state}
        for j, q in enumerate(run):
            env = {name: x[name][j] for name in x}
            env.update({name: par[name][j] for name in par})
            env.update(tau=q.tau, period=float(q.i), n_periods=float(len(allp)),
                       is_first=float(q.i == 0), is_last=float(q.i == len(allp) - 1))
            env.update(state)
            for name, e in defines:
                env[name] = e(env)
            new = {name: np.array(np.broadcast_to(e(env), (n,))) for name, e in evolves}
            env.update(new)
            pay[j] = np.broadcast_to(payoff(env, state), (n,))
            for name, value in [(f"state.{k}.evolve", new[k]) for k in new] + [("payoff", pay[j])]:
                bad = ~np.isfinite(value)
                if bad.any():
                    # a define may be non-finite where a where()/min()/max() masks it, so it is only named here,
                    # as the likely cause, when a state or the payoff it feeds is non-finite on the same paths
                    causes = [f"define.{d}" for d, _ in defines if not np.isfinite(
                        np.broadcast_to(env[d], (n,))[bad]).all()]
                    raise FloatingPointError(f"{path}.{name}: non-finite value in the period {q.start}..{q.end} on "
                                             f"{int(bad.sum())} path(s) (division by zero, log or sqrt of a negative "
                                             "number?)" + (f"; non-finite on those paths: {causes}" if causes else ""))
            for k in new:
                hist[k][j] = new[k]
            state = new
        return pay, hist, init

    @classmethod
    def cashflows(cls, spec, p: PCtx, path, pers=None):
        pers = cls.periods(spec, p.v) if pers is None else pers
        run = cls.run_periods(spec, p.v, pers)
        pay, hist, init = cls.evolve(spec, p, path, run)
        rows = [q.i - run[0].i for q in pers]
        diag = {"mean_payoff_by_period": [round(float(c.mean()), 8) for c in pay[rows]],
                "payoff_nonzero_probability_by_period": [round(float((c != 0).mean()), 6) for c in pay[rows]]}
        if hist:
            diag["mean_state_by_period"] = {k: [round(float(s.mean()), 8) for s in h[rows]] for k, h in hist.items()}
        lr = cls.finish(spec, p, path, pers, pay[rows], diag)
        known = [cls.known_date(spec, p.v, q) for q in run]
        known = [max(known[:j + 1]) for j in range(len(known))]   # a state is known once every earlier one is
        lr.states = {k: pd.DataFrame(h, index=known) for k, h in hist.items()}
        lr.initial_states = {k: float(init[k]) for k in spec.state}
        return lr

    # ------------------------------------------------------------------ state as a regressor
    @staticmethod
    def state_names(spec):
        return list(spec.state)

    @staticmethod
    def state_on(lr, name, dates) -> np.ndarray:
        """(len(dates), n_paths): the state known on each date (after the last period whose inputs are all fixed
        on or before it; the initial state before any). Uses no information after the date."""
        df = lr.states[name]
        known = np.array(df.index, dtype="datetime64[D]")
        rows = np.vstack([np.full((1, df.shape[1]), lr.initial_states[name]), df.values])
        j = np.searchsorted(known, np.array(dates, dtype="datetime64[D]"), side="right")
        return rows[j]


def leg_state_ref(v: VCtx, name):
    """(leg id, state name) when `name` is '<leg id>.<state>' of a leg with states; None otherwise. A model
    underlying or declared observable of that name (a ticker such as BRK.B) is never a leg state: the one
    classifier for validation, reads and graph wiring."""
    if name in v.observables or name in v.underlyings or "." not in name:
        return None
    lid, state = name.split(".", 1)
    if lid in v.legs and hasattr(v.legs[lid][0], "state_names"):
        return lid, state
    return None
