"""Cashflow components and masks.

legs        produce a cashflow matrix (payment dates x paths) from simulated state
conditions  leg-level masks: knock_in (kick-in) / knock_out, gating each cashflow of the leg by
            whether a barrier was hit on or before that cashflow's cut-off date (option expiry,
            coupon accrual end, payment date of a fixed amount)
events      trade-level masks: knock_out terminates the whole trade on the first hit; target
            (TARN) terminates it once accumulated coupons reach a target. Both may pay a rebate.

A barrier reads a simulated `underlying` (relative or absolute level) or any `observable`
(absolute level in the observable's units: a spread, a worst-of performance, a rate).
European vs American style is not a field: it follows from the observation schedule. A
condition observed on a one-date schedule at expiry is European; observed on a daily ('1D')
schedule it is American (discretely monitored). `observation: continuous` is accepted only by
analytic pricers. Coupon legs live in coupons.py.
"""
from __future__ import annotations

import contextlib
import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from . import conventions as cv
from . import observables as obs
from . import registry
from .registry import SpecModel, register
from .spec import DateLike

CONTINUOUS = "continuous"
NAT = np.datetime64("NaT")


@dataclass
class VCtx:
    """What validation and date planning may look at (no simulation yet)."""
    dates: object                      # schedules.DateContext
    valuation_date: dt.date
    maturity: dt.date
    schedules: dict[str, list]         # resolved schedules
    underlyings: list[str]             # simulated by the model
    spots: dict[str, float]
    reference_levels: dict[str, float]
    report: object
    broken_schedules: set = field(default_factory=set)   # defined but invalid (already reported)
    observables: dict = field(default_factory=dict)      # name -> (factory, spec)
    stochastic_rates: bool = False
    model_name: str = ""
    curve_names: set = field(default_factory=set)        # market curves
    legs: dict = field(default_factory=dict)             # leg id -> (factory, spec)
    history: dict = field(default_factory=dict)          # underlying -> {date: past closing level}
    past_reads: dict = field(default_factory=dict)       # leaf observable -> reader path -> past dates read
    reader: str = ""                                     # path of the component declaring its reads
    reader_sticky: bool = False                          # nested reading() blocks keep `reader`
    target_paid: dict = field(default_factory=dict)      # target event id -> coupons paid before valuation

    def level(self, value, mode, und):
        return value * self.reference_levels[und] if mode == "relative" else value

    def future_obs(self, name):
        """Observation dates >= valuation date."""
        return [d for d in self.schedules[name] if d >= self.valuation_date]


@dataclass
class PCtx:
    """What cashflow construction may look at (after simulation)."""
    v: VCtx
    scenario: object            # models.Scenario (None for purely deterministic legs)
    n_paths: int
    reads: dict | None = None   # observable name -> {date: values} as computed (recorded only when inspecting)

    def obs(self, name, dates) -> np.ndarray:
        """Observable values, broadcast to (len(dates), n_paths): the only way a component reads the market
        on a date. Past dates come from history, later ones from the simulation. A model underlying is an
        observable under its own name (its level)."""
        x = obs.evaluate(self, name, dates)
        return np.broadcast_to(x, (len(dates), self.n_paths))


@dataclass
class LegResult:
    amounts: pd.DataFrame                   # rows = payment dates (may repeat), columns = paths
    diagnostics: dict = field(default_factory=dict)
    sign: float = 1.0                       # +1 long, -1 short (amounts already carry it)
    states: dict = field(default_factory=dict)          # path_dependent: state -> (date it is known x paths)
    initial_states: dict = field(default_factory=dict)  # path_dependent: state -> value before those dates


@dataclass
class EventResult:
    hit_date: np.ndarray                    # per path: datetime64[D] of the termination, NaT if never
    rebate: pd.DataFrame | None             # paid when this event terminates the path (dates x paths)
    settle_on_event_date: bool
    diagnostics: dict = field(default_factory=dict)
    leg_scale: dict = field(default_factory=dict)       # leg id -> per-path factor on its cashflow at hit_date
    conditional: pd.DataFrame | None = None             # paid only if the trade is alive on the date


def merge_requirements(*reqs) -> dict:
    out = {}
    for r in reqs:
        for k, ds in r.items():
            out.setdefault(k, set()).update(ds)
    return {k: sorted(ds) for k, ds in out.items()}


def _check_ref(v: VCtx, und, path):
    if und not in v.underlyings:
        v.report.error(path, f"underlying '{und}' is not simulated by the model.{cv.suggest(und, v.underlyings)}",
                       f"model underlyings: {v.underlyings}")
        return False
    return True


def _check_schedule(v: VCtx, name, path, allow_continuous=False):
    if allow_continuous and name == CONTINUOUS:
        return True
    if name in v.broken_schedules:
        return False
    if name not in v.schedules:
        v.report.error(path, f"schedule '{name}' is not defined.{cv.suggest(name, v.schedules)}",
                       f"defined schedules: {sorted(v.schedules)}"
                       + ("; or use 'continuous' (analytic pricers only)" if allow_continuous else ""))
        return False
    return True


def _resolve_date(v: VCtx, x, path):
    try:
        return v.dates.resolve(x)
    except ValueError as e:
        v.report.error(path, str(e))
        return None


def _breach(values: np.ndarray, direction: str, barrier: float) -> np.ndarray:
    return values >= barrier if direction == "up" else values <= barrier


def _sign(spec):
    return 1.0 if spec.position == "long" else -1.0


# ======================================================================= barrier triggers
class _TriggerSpec(SpecModel):
    underlying: str | None = Field(None, description="simulated underlying (or use `observable`)")
    observable: str | None = Field(None, description="any observable; the barrier is in its units")
    direction: Literal["up", "down"] = Field(description="up: hit when value >= barrier; down: when <= barrier")
    barrier: float
    barrier_mode: Literal["relative", "absolute"] | None = Field(
        None, description="underlying: relative (default, fraction of the reference level) or absolute; "
                          "observable: absolute only")
    observation: str = Field(description="schedule name, or 'continuous' (analytic only)")

    @model_validator(mode="after")
    def _source(self):
        if (self.underlying is None) == (self.observable is None):
            raise ValueError("give exactly one of `underlying` or `observable`")
        if self.observable is not None and self.barrier_mode == "relative":
            raise ValueError("barrier_mode relative applies to `underlying`; for a relative level on an observable "
                             "declare an equity observable with mode: performance")
        if self.underlying is not None and self.barrier <= 0:
            raise ValueError("barrier on an underlying must be > 0")
        return self


class Trigger:
    """Barrier reading shared by conditions and knock-out events."""

    @staticmethod
    def validate(spec, v: VCtx, path) -> bool:
        if spec.underlying is not None:
            ok = _check_ref(v, spec.underlying, path + ".underlying")
        else:
            ok = obs.known(v, spec.observable, path + ".observable") and \
                obs.require_stochastic(v, spec.observable, path + ".observable", "a barrier")
            if ok and spec.observation == CONTINUOUS:
                v.report.error(path + ".observation", "continuous monitoring needs an `underlying` barrier")
                ok = False
        ok &= _check_schedule(v, spec.observation, path + ".observation", allow_continuous=True)
        return ok

    @staticmethod
    def level(spec, v: VCtx):
        if spec.underlying is not None:
            return v.level(spec.barrier, spec.barrier_mode or "relative", spec.underlying)
        return spec.barrier

    @staticmethod
    def spot_breached(spec, v: VCtx) -> bool:
        """Today's value breaches the barrier (only checked for underlyings)."""
        if spec.underlying is None:
            return False
        return bool(_breach(np.array([v.spots[spec.underlying]]), spec.direction, Trigger.level(spec, v))[0])

    @staticmethod
    def requirements(spec, v: VCtx, dates) -> dict:
        return obs.requirements(v, obs.source(spec), dates)

    @staticmethod
    def values(spec, p: PCtx, dates) -> np.ndarray:
        return p.obs(obs.source(spec), dates)

    @classmethod
    def past_hits(cls, spec, v: VCtx, dates) -> list:
        """Past observation dates on which the barrier was breached; [] while history is incomplete (the
        missing dates are reported by the central history check)."""
        past = [d for d in dates if d < v.valuation_date]
        if not past or obs.missing_history(v, obs.source(spec), past):
            return []
        hits = _breach(cls.values(spec, PCtx(v, None, 1), past)[:, 0], spec.direction, cls.level(spec, v))
        return [d for d, h in zip(past, hits) if h]

    @classmethod
    def first_hit(cls, spec, p: PCtx, dates):
        """Per path: first date in `dates` with a breach (datetime64[D], NaT if none)."""
        if not dates:
            return np.full(p.n_paths, NAT, dtype="datetime64[D]")
        hits = _breach(cls.values(spec, p, dates), spec.direction, cls.level(spec, p.v))
        any_hit = hits.any(axis=0)
        first = hits.argmax(axis=0)
        return np.where(any_hit, np.array(dates, dtype="datetime64[D]")[first], NAT)


# ======================================================================= leg conditions
class BarrierCondition:
    description = ""
    knock_in = True

    class Spec(_TriggerSpec):
        pass

    @classmethod
    def validate(cls, spec, v: VCtx, path):
        if not Trigger.validate(spec, v, path):
            return
        if spec.observation == CONTINUOUS and v.dates.resolve("effective_date") < v.valuation_date:
            v.report.warning(path + ".observation", "continuous monitoring of a seasoned trade only looks "
                                                    "forward from today",
                             "confirm the barrier was not hit before the valuation date")
        if spec.observation != CONTINUOUS and v.valuation_date not in v.schedules[spec.observation]:
            return
        if Trigger.spot_breached(spec, v):
            v.report.warning(path, f"spot {v.spots[spec.underlying]:g} already breaches the {spec.direction} barrier "
                                   f"{Trigger.level(spec, v):g}"
                                   + (" (monitoring includes today)" if spec.observation != CONTINUOUS else ""))

    @staticmethod
    def obs_dates(spec, v: VCtx, until):
        """Observation dates up to `until`, past ones included: a hit before the valuation date counts."""
        return [d for d in v.schedules[spec.observation] if d <= until]

    @classmethod
    def fixing_dates(cls, spec, v: VCtx, path, until):
        if spec.observation == CONTINUOUS:
            return {}
        return Trigger.requirements(spec, v, cls.obs_dates(spec, v, until))

    @classmethod
    def masks(cls, spec, p: PCtx, cutoffs, path):
        """Per cut-off date, per path: does the cashflow survive this condition? Plus P(hit by last cut-off)."""
        hit = Trigger.first_hit(spec, p, cls.obs_dates(spec, p.v, max(cutoffs)))
        out = []
        for c in cutoffs:
            hit_by_c = ~np.isnat(hit) & (hit <= np.datetime64(c, "D"))
            out.append(hit_by_c if cls.knock_in else ~hit_by_c)
        return out, float((~np.isnat(hit)).mean())


@register("condition", "knock_in")
class KnockIn(BarrierCondition):
    description = ("Kick-in: a cashflow of the leg is paid only if the barrier was hit on an observation date on or "
                   "before its cut-off (option expiry / coupon accrual end / payment date). One-date schedule at "
                   "expiry = European knock-in; daily = American.")
    knock_in = True


@register("condition", "knock_out")
class KnockOut(BarrierCondition):
    description = ("A cashflow of the leg is paid only if the barrier was NOT hit on or before its cut-off. Kills "
                   "this leg only; use a termination event to end the whole trade.")
    knock_in = False


def parsed_conditions(spec, report, path):
    out = []
    for i, c in enumerate(getattr(spec, "conditions", None) or []):
        cls, cspec = registry.parse("condition", c, f"{path}.conditions[{i}]", report)
        if cls:
            out.append((cls, cspec, f"{path}.conditions[{i}]"))
    return out


def validate_conditions(spec, v: VCtx, path, last_cutoff):
    for ccls, cspec, cpath in parsed_conditions(spec, v.report, path):
        ccls.validate(cspec, v, cpath)
        if cspec.observation != CONTINUOUS and cspec.observation in v.schedules and last_cutoff:
            late = [d for d in v.schedules[cspec.observation] if d > last_cutoff]
            if late:
                v.report.warning(cpath + ".observation",
                                 f"{len(late)} observation date(s) after the leg's last cut-off {last_cutoff} are ignored")


def conditions_requirements(spec, v: VCtx, path, last_cutoff) -> dict:
    reqs = []
    for ccls, cspec, cpath in parsed_conditions(spec, v.report, path):
        with obs.reading(v, cpath + (".underlying" if getattr(cspec, "underlying", None) else ".observable")):
            reqs.append(ccls.fixing_dates(cspec, v, cpath, last_cutoff))
    return merge_requirements(*reqs)


def apply_conditions(spec, p: PCtx, path, amounts: np.ndarray, cutoffs, diag) -> np.ndarray:
    """Multiply each cashflow row by the leg's condition masks (rows align with cutoffs)."""
    conds = parsed_conditions(spec, p.v.report, path)
    if not conds:
        return amounts
    alive = np.ones((len(cutoffs), p.n_paths), dtype=bool)
    for i, (ccls, cspec, cpath) in enumerate(conds):
        masks, prob = ccls.masks(cspec, p, cutoffs, cpath)
        alive &= np.array(masks)
        diag[f"conditions[{i}].{ccls.type_name}.barrier_hit_probability"] = prob
    diag["payoff_active_probability"] = float(alive[-1].mean()) if len(cutoffs) == 1 else \
        [round(float(a.mean()), 6) for a in alive]
    return amounts * alive


# ======================================================================= legs
class LegFactory:
    """validate(spec, v, path); fixing_dates(spec, v, path) -> {state: [dates]};
    payment_dates(spec, v); cashflows(spec, p, path) -> LegResult; is_deterministic(spec)"""
    deterministic = False

    @classmethod
    def is_deterministic(cls, spec):
        return cls.deterministic and not getattr(spec, "conditions", None)


@register("leg", "option_payoff")
class OptionPayoff(LegFactory):
    description = ("Vanilla call/put fixed at `expiry` on an underlying or an observable, optionally gated by "
                   "conditions. underlying + relative strike: notional * max(phi*(S_T/S_ref - k), 0); "
                   "absolute strike: notional * max(phi*(X_T - K), 0) (notional = number of units). "
                   "An observable (basket, spread, worst-of performance) takes an absolute strike in its units.")

    class Spec(SpecModel):
        id: str
        underlying: str | None = None
        observable: str | None = None
        option_type: Literal["call", "put"]
        strike: float
        strike_mode: Literal["relative", "absolute"] | None = Field(
            None, description="default relative for an underlying; must be absolute for an observable")
        notional: float = Field(gt=0)
        position: Literal["long", "short"] = "long"
        expiry: DateLike = "maturity"
        payment_date: DateLike | None = Field(None, description="default: expiry")
        conditions: list[dict] = Field(default_factory=list)

        @model_validator(mode="after")
        def _source(self):
            if (self.underlying is None) == (self.observable is None):
                raise ValueError("give exactly one of `underlying` or `observable`")
            if self.observable is not None and self.strike_mode == "relative":
                raise ValueError("strike_mode relative applies to `underlying`; use an equity observable with "
                                 "mode: performance for a relative strike on a basket or worst-of")
            if self.underlying is not None and self.strike < 0:
                raise ValueError("strike must be >= 0")
            return self

    @classmethod
    def validate(cls, spec, v: VCtx, path):
        if spec.underlying is not None:
            _check_ref(v, spec.underlying, path + ".underlying")
        elif obs.known(v, spec.observable, path + ".observable"):
            obs.require_stochastic(v, spec.observable, path + ".observable", "an option")
        exp = _resolve_date(v, spec.expiry, path + ".expiry")
        pay = _resolve_date(v, spec.payment_date, path + ".payment_date") if spec.payment_date else exp
        if pay and pay <= v.valuation_date:
            v.report.error(path + ".payment_date", f"paid on {pay}, not after the valuation date {v.valuation_date}",
                           "a paid leg has no value left; remove it")
        if exp and pay and pay < exp:
            v.report.error(path + ".payment_date", f"payment date {pay} is before expiry {exp}")
        if exp and exp > v.maturity:
            v.report.warning(path + ".expiry", f"expiry {exp} is after trade maturity {v.maturity}")
        validate_conditions(spec, v, path, exp)

    @staticmethod
    def dates(spec, v: VCtx):
        exp = v.dates.resolve(spec.expiry)
        return exp, (v.dates.resolve(spec.payment_date) if spec.payment_date else exp)

    @classmethod
    def fixing_dates(cls, spec, v: VCtx, path):
        exp, _ = cls.dates(spec, v)
        own = obs.requirements(v, obs.source(spec), [exp])
        return merge_requirements(own, conditions_requirements(spec, v, path, exp))

    @classmethod
    def payment_dates(cls, spec, v: VCtx):
        return [cls.dates(spec, v)[1]]

    @classmethod
    def unit_scale(cls, spec, v: VCtx):
        """(units, absolute strike): payoff = units * max(phi*(X_T - K), 0)."""
        if spec.underlying is not None and (spec.strike_mode or "relative") == "relative":
            ref = v.reference_levels[spec.underlying]
            return spec.notional / ref, spec.strike * ref
        return spec.notional, spec.strike

    @classmethod
    def cashflows(cls, spec, p: PCtx, path):
        exp, pay = cls.dates(spec, p.v)
        x_t = p.obs(obs.source(spec), [exp])[0]
        units, k = cls.unit_scale(spec, p.v)
        phi = 1.0 if spec.option_type == "call" else -1.0
        amount = units * np.maximum(phi * (x_t - k), 0.0)
        diag = {"exercise_probability": float((amount > 0).mean())}
        amount = apply_conditions(spec, p, path, amount[None, :], [exp], diag)[0]
        return LegResult(pd.DataFrame([_sign(spec) * amount], index=[pay]), diag, _sign(spec))


@register("leg", "fixed_amount")
class FixedAmount(LegFactory):
    description = ("A single known cash amount (e.g. principal redemption) paid on payment_date, optionally gated "
                   "by conditions (cut-off = payment date), e.g. a bonus paid only if a barrier was touched.")
    deterministic = True

    class Spec(SpecModel):
        id: str
        amount: float
        payment_date: DateLike = "maturity"
        position: Literal["long", "short"] = "long"
        conditions: list[dict] = Field(default_factory=list)

    @staticmethod
    def validate(spec, v: VCtx, path):
        d = _resolve_date(v, spec.payment_date, path + ".payment_date")
        if d and d <= v.valuation_date:
            v.report.error(path + ".payment_date", f"payment date {d} is not after the valuation date")
        validate_conditions(spec, v, path, d)

    @staticmethod
    def fixing_dates(spec, v, path):
        return conditions_requirements(spec, v, path, v.dates.resolve(spec.payment_date))

    @staticmethod
    def payment_dates(spec, v):
        return [v.dates.resolve(spec.payment_date)]

    @staticmethod
    def cashflows(spec, p: PCtx, path):
        d = p.v.dates.resolve(spec.payment_date)
        diag = {}
        amount = apply_conditions(spec, p, path, np.full((1, p.n_paths), _sign(spec) * spec.amount), [d], diag)
        return LegResult(pd.DataFrame(amount, index=[d]), diag, _sign(spec))


# ======================================================================= events
class EventFactory:
    """validate(spec, v, path); fixing_dates; payment_dates; evaluate(spec, p, path, legs) -> EventResult"""
    leg_dependent = False

    @classmethod
    def leg_refs(cls, spec) -> list:
        """Ids of the legs whose results `evaluate` receives."""
        return list(spec.legs) if cls.leg_dependent else []


@register("event", "knock_out")
class KnockOutEvent(EventFactory):
    description = ("Terminates the whole trade on the first observation date the barrier is hit. Cashflows paid "
                   "after that date are cancelled; a cashflow ON that date is paid only if settle_on_event_date "
                   "(autocall style). Optional cash `rebate` paid at_hit or at_maturity.")

    class Spec(_TriggerSpec):
        id: str
        rebate: float = Field(0.0, description="cash amount (not a fraction of notional)")
        rebate_payment: Literal["at_hit", "at_maturity"] = "at_hit"
        settle_on_event_date: bool = False

    @staticmethod
    def validate(spec, v: VCtx, path):
        if not Trigger.validate(spec, v, path):
            return
        obs_dates = v.schedules.get(spec.observation, [])
        if Trigger.spot_breached(spec, v) and (spec.observation == CONTINUOUS or v.valuation_date in obs_dates):
            v.report.error(path, f"spot {v.spots[spec.underlying]:g} already breaches the {spec.direction} knock-out "
                                 f"barrier {Trigger.level(spec, v):g}; the trade would terminate today",
                           "check barrier level / barrier_mode")
        if spec.observation != CONTINUOUS and not [d for d in obs_dates if d >= v.valuation_date]:
            v.report.error(path + ".observation", f"schedule '{spec.observation}' has no dates on/after the "
                                                  "valuation date")
        if spec.observation == CONTINUOUS and v.dates.resolve("effective_date") < v.valuation_date:
            v.report.warning(path + ".observation", "continuous monitoring of a seasoned trade only looks forward "
                                                    "from today", "confirm the barrier was not hit before the "
                                                                  "valuation date")
        if spec.rebate and spec.settle_on_event_date and spec.observation != CONTINUOUS:
            redemptions = set()
            for c, s in v.legs.values():
                if c is FixedAmount:
                    with contextlib.suppress(ValueError):
                        redemptions.add(v.dates.resolve(s.payment_date))
            both = sorted(redemptions & set(obs_dates))
            if both:
                v.report.warning(path + ".observation",
                                 f"a fixed_amount leg also pays on observation date(s) {[str(d) for d in both]}: if the "
                                 "event hits there, both the rebate and that amount are paid",
                                 "if the rebate is the redemption, drop that date from the observation schedule")

    @staticmethod
    def validate_past(spec, v: VCtx, path, reads):
        """After the central history check: a hit before the valuation date means the trade is over."""
        if spec.observation == CONTINUOUS:
            return
        hits = Trigger.past_hits(spec, v, v.schedules[spec.observation])
        if hits:
            v.report.error(path, f"the barrier was already hit on {hits[0]} (before the valuation date): the "
                                 "trade terminated then and has no remaining value to simulate",
                           "check trade.history / the observable's history and the barrier level; price only "
                           "what is still owed (e.g. an unpaid rebate) as fixed_amount legs")

    @staticmethod
    def fixing_dates(spec, v: VCtx, path):
        if spec.observation == CONTINUOUS:
            return {}
        with obs.reading(v, path + (".underlying" if spec.underlying else ".observable")):
            return Trigger.requirements(spec, v, v.schedules[spec.observation])

    @staticmethod
    def payment_dates(spec, v: VCtx):
        if spec.rebate == 0:
            return []
        if spec.rebate_payment == "at_maturity":
            return [v.maturity]
        return v.future_obs(spec.observation) if spec.observation != CONTINUOUS else []

    @staticmethod
    def evaluate(spec, p: PCtx, path, legs):
        dates = p.v.future_obs(spec.observation)     # validation rejects a hit on a past date
        hit_date = Trigger.first_hit(spec, p, dates)
        any_hit = ~np.isnat(hit_date)
        date_arr = np.array(dates, dtype="datetime64[D]")
        rebate = None
        if spec.rebate != 0:
            if spec.rebate_payment == "at_maturity":
                rebate = pd.DataFrame([spec.rebate * any_hit], index=[p.v.maturity])
            else:
                m = (date_arr[:, None] == hit_date[None, :]) * spec.rebate
                rebate = pd.DataFrame(m, index=dates)
        diag = {"hit_probability": float(any_hit.mean())}
        if any_hit.any():
            t = (hit_date[any_hit] - np.datetime64(p.v.valuation_date)).astype(float) / 365.0
            diag["expected_years_to_hit_given_hit"] = float(t.mean())
        return EventResult(hit_date, rebate, spec.settle_on_event_date, diag)


@register("event", "target")
class TargetEvent(EventFactory):
    description = ("Target accumulation (TARN): coupons of `legs` accumulate on their payment dates; on the first "
                   "payment date the total reaches `target` the trade terminates: that date's cashflows are paid "
                   "(the crossing coupon full, capped at the remaining target, or none), later ones are cancelled, "
                   "and `rebate` (e.g. the early redemption) is paid on that date if it is before maturity. "
                   "`guaranteed`: if the target is never reached, the shortfall is paid at maturity.")
    leg_dependent = True

    class Spec(SpecModel):
        id: str
        legs: list[str] = Field(min_length=1, description="ids of the coupon legs that accumulate")
        target: float = Field(gt=0, description="cash amount, e.g. 0.10 * notional for a 10% target")
        crossing_coupon: Literal["capped", "full", "none"] = "capped"
        rebate: float = Field(0.0, description="cash paid on an early termination date (not at maturity)")
        guaranteed: bool = False
        accumulated: float | None = Field(
            None, ge=0, description="override: coupons of `legs` already paid on or before the valuation date (cash). "
                                    "Default: computed from the legs' past fixings, which history must then cover")

    @staticmethod
    def paid_legs(spec, v: VCtx):
        """(leg factory, leg spec, leg path, paid periods) of each accumulated leg with coupons paid on or before
        the valuation date."""
        out = []
        for lid in spec.legs:
            if lid not in v.legs or not hasattr(v.legs[lid][0], "paid_periods"):
                continue
            c, s = v.legs[lid]
            paid = c.paid_periods(s, v)
            if paid:
                out.append((c, s, f"trade.legs[{list(v.legs).index(lid)}]", paid))
        return out

    @staticmethod
    def validate(spec, v: VCtx, path):
        for i, lid in enumerate(spec.legs):
            if lid not in v.legs:
                v.report.error(f"{path}.legs[{i}]", f"leg '{lid}' is not defined.{cv.suggest(lid, v.legs)}",
                               f"leg ids: {sorted(v.legs)}")
        if len(set(spec.legs)) != len(spec.legs):
            v.report.error(path + ".legs", "leg ids must be distinct")

    @staticmethod
    def fixing_dates(spec, v, path):
        """The paid coupons are recomputed by their own legs, so their past fixings are read like any other."""
        if spec.accumulated is not None:
            return {}
        reqs = []
        for c, s, lpath, paid in TargetEvent.paid_legs(spec, v):
            with obs.reading(v, f"{path}.legs[{spec.legs.index(s.id)}] (coupons paid before the valuation date)",
                             sticky=True):
                reqs.append(c.fixing_dates(s, v, lpath, pers=paid))
        return merge_requirements(*reqs)

    @staticmethod
    def paid_amount(spec, p: PCtx):
        """Coupons of `legs` paid on or before the valuation date, per path (one value unless a payment on the
        valuation date reads a fixing of that date, which is simulated)."""
        if spec.accumulated is not None:
            return np.full(1, spec.accumulated)
        if spec.id in p.v.target_paid:          # known from history alone: computed once, at validation
            return p.v.target_paid[spec.id]
        total = np.zeros(1)
        for c, s, lpath, paid in TargetEvent.paid_legs(spec, p.v):
            lr = c.cashflows(s, p, lpath, pers=paid)
            total = total + lr.amounts.values.sum(axis=0) * lr.sign
        return total

    @staticmethod
    def validate_past(spec, v: VCtx, path, reads):
        """After the central history check (`reads`: this event's simulation needs from it): a target already
        reached means the trade is over. A paid amount known from history alone is cached for pricing."""
        if spec.accumulated is None:
            if reads:
                return      # a paid coupon reads a fixing of the valuation date: only known after simulation
            if any(cspec.observation == CONTINUOUS for c, s, lpath, _ in TargetEvent.paid_legs(spec, v)
                   for _, cspec, _ in parsed_conditions(s, v.report, lpath)):
                return      # not priceable by simulation; the method selection reports it
            v.target_paid[spec.id] = TargetEvent.paid_amount(spec, PCtx(v, None, 1))
        paid = float(TargetEvent.paid_amount(spec, PCtx(v, None, 1))[0])
        if paid >= spec.target * (1 - 1e-12):
            v.report.error(path, f"coupons paid by the valuation date ({paid:,.2f}) already reach the target "
                                 f"{spec.target:,.2f}: the trade has terminated")

    @staticmethod
    def payment_dates(spec, v: VCtx):
        out = set()
        if spec.rebate:
            for lid in spec.legs:
                if lid in v.legs:
                    c, s = v.legs[lid]
                    out.update(d for d in c.payment_dates(s, v) if d < v.maturity)
        if spec.guaranteed:
            out.add(v.maturity)
        return sorted(out)

    @staticmethod
    def evaluate(spec, p: PCtx, path, legs):
        n = p.n_paths
        rows = [(d, lid, i) for lid in spec.legs for i, d in enumerate(legs[lid].amounts.index)]
        dates = sorted({d for d, _, _ in rows})
        pos = {d: k for k, d in enumerate(dates)}
        gross = np.zeros((len(dates), n))
        for d, lid, i in rows:
            lr = legs[lid]
            gross[pos[d]] += lr.amounts.iloc[i].values * lr.sign
        paid = TargetEvent.paid_amount(spec, p)
        cum = paid + np.cumsum(gross, axis=0)
        reach = cum >= spec.target * (1 - 1e-12)
        any_hit = reach.any(axis=0)
        first = reach.argmax(axis=0)
        date_arr = np.array(dates, dtype="datetime64[D]")
        hit_date = np.where(any_hit, date_arr[first], NAT)
        cols = np.arange(n)
        crossing = gross[first, cols]
        before = np.where(first > 0, cum[np.maximum(first - 1, 0), cols], paid)
        if spec.crossing_coupon == "full":
            scale = np.ones(n)
        elif spec.crossing_coupon == "none":
            scale = np.zeros(n)
        else:
            scale = np.where(crossing > 0, (spec.target - before) / np.where(crossing > 0, crossing, 1.0), 0.0)
        rebate = None
        if spec.rebate:
            early = [d for d in dates if d < p.v.maturity]
            m = (np.array(early, dtype="datetime64[D]")[:, None] == hit_date[None, :]) * spec.rebate
            rebate = pd.DataFrame(m, index=early)
        conditional = None
        if spec.guaranteed:
            conditional = pd.DataFrame([np.maximum(spec.target - cum[-1], 0.0) * ~any_hit], index=[p.v.maturity])
        diag = {"hit_probability": float(any_hit.mean()),
                "mean_accumulated_if_not_hit": float(cum[-1][~any_hit].mean()) if (~any_hit).any() else None}
        if any_hit.any():
            t = (hit_date[any_hit] - np.datetime64(p.v.valuation_date)).astype(float) / 365.0
            diag["expected_years_to_hit_given_hit"] = float(t.mean())
        return EventResult(hit_date, rebate, True, diag, {lid: scale for lid in spec.legs}, conditional)
