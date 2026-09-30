"""Analytic pricers: QuantLib closed forms used when a trade matches one exactly.

Each pricer implements
    match(trade) -> (bool, reason)       trade = engine.ParsedTrade
    price(trade, model, v) -> dict        {price, legs: {id: pv}, engine}
`match` must be conservative: a partial match silently mispricing a feature is
worse than falling back to Monte Carlo.
"""
from __future__ import annotations

import QuantLib as ql

from . import conventions as cv
from .payoffs import CONTINUOUS, KnockOutEvent, OptionPayoff, PCtx, Trigger
from .registry import SpecModel, register


def _deterministic_pv(trade, model, v):
    """PV of fixed legs by direct discounting."""
    out = {}
    for cls, spec, path in trade.legs:
        if cls.is_deterministic(spec):
            p = PCtx(v, None, 1)
            amounts = cls.cashflows(spec, p, path).amounts
            out[spec.id] = float(sum(amounts.iloc[i, 0] * model.discount.discount(cv.to_ql(d))
                                     for i, d in enumerate(amounts.index)))
    return out


def _vanilla_parts(trade):
    opts = [(c, s, p) for c, s, p in trade.legs if c is OptionPayoff]
    others = [(c, s, p) for c, s, p in trade.legs if c is not OptionPayoff]
    return opts, others


def _ql_payoff(spec, v):
    units, k = OptionPayoff.unit_scale(spec, v)
    t = ql.Option.Call if spec.option_type == "call" else ql.Option.Put
    return units, ql.PlainVanillaPayoff(t, k)


@register("analytic", "european_vanilla")
class EuropeanVanilla:
    description = ("One unconditioned option_payoff leg (plus any fixed legs), no termination events. "
                   "Engine: model's analytic European engine (BSM or Heston).")

    class Spec(SpecModel):
        pass

    @staticmethod
    def match(trade):
        if trade.model.european_engine_available is False:
            return False, "model has no analytic European engine"
        opts, others = _vanilla_parts(trade)
        if len(opts) != 1:
            return False, f"needs exactly one option_payoff leg, found {len(opts)}"
        if opts[0][1].underlying is None:
            return False, f"leg '{opts[0][1].id}' is an option on an observable, not on an underlying"
        if opts[0][1].conditions:
            return False, f"leg '{opts[0][1].id}' has barrier conditions"
        if trade.events:
            return False, "trade has termination events"
        if any(not c.is_deterministic(s) for c, s, _ in others):
            return False, "other legs are path-dependent or rate-dependent"
        return True, "single European option (+ deterministic legs)"

    @staticmethod
    def price(trade, model, v):
        (cls, spec, path), = _vanilla_parts(trade)[0]
        exp, pay = OptionPayoff.dates(spec, v)
        units, payoff = _ql_payoff(spec, v)
        opt = ql.VanillaOption(payoff, ql.EuropeanExercise(cv.to_ql(exp)))
        opt.setPricingEngine(model.european_engine(spec.underlying))
        lag = model.discount.discount(cv.to_ql(pay)) / model.discount.discount(cv.to_ql(exp))
        sign = 1.0 if spec.position == "long" else -1.0
        legs = {spec.id: sign * units * opt.NPV() * lag}
        legs.update(_deterministic_pv(trade, model, v))
        return {"price": sum(legs.values()), "legs": legs,
                "engine": type(model.european_engine(spec.underlying)).__name__}


@register("analytic", "single_barrier")
class SingleBarrier:
    description = ("One option_payoff leg with EITHER one continuous knock_in/knock_out condition and no events, "
                   "OR no conditions and one continuous knock_out event (rebate paid at_hit). "
                   "Engine: QuantLib AnalyticBarrierEngine (Black-Scholes only).")

    class Spec(SpecModel):
        pass

    @staticmethod
    def _barrier(trade):
        """-> (leg spec, barrier spec, is_knock_in, rebate cash) or reason string."""
        if not trade.model.barrier_engine_available:
            return "model has no analytic barrier engine (Black-Scholes only)"
        if len(trade.legs) != 1 or trade.legs[0][0] is not OptionPayoff:
            return "needs exactly one leg, of type option_payoff"
        _, leg, path = trade.legs[0]
        if leg.underlying is None:
            return "option is on an observable, not on an underlying"
        conds = trade.conditions[path]
        if len(conds) + len(trade.events) != 1:
            return "needs exactly one barrier (one condition or one event)"
        if conds:
            ccls, b, _ = conds[0]
            knock_in, rebate = ccls.knock_in, 0.0
        else:
            ecls, b, _ = trade.events[0]
            if ecls is not KnockOutEvent:
                return f"event type '{ecls.type_name}' has no closed form"
            knock_in, rebate = False, b.rebate
            if b.rebate != 0 and b.rebate_payment != "at_hit":
                return "analytic knock-out rebate is paid at hit; rebate_payment must be at_hit"
            if b.settle_on_event_date:
                return "settle_on_event_date is not representable analytically"
        if b.underlying is None:
            return "barrier reads an observable, not the option's underlying"
        if b.observation != CONTINUOUS:
            return f"barrier is discretely monitored (schedule '{b.observation}'); closed form assumes continuous"
        if b.underlying != leg.underlying:
            return "barrier underlying differs from option underlying"
        if leg.payment_date is not None and leg.payment_date != leg.expiry:
            return "payment_date differs from expiry"
        return leg, b, knock_in, rebate

    @classmethod
    def match(cls, trade):
        r = cls._barrier(trade)
        return (False, r) if isinstance(r, str) else (True, "single continuously-monitored barrier option")

    @classmethod
    def price(cls, trade, model, v):
        leg, b, knock_in, rebate = cls._barrier(trade)
        exp, _ = OptionPayoff.dates(leg, v)
        units, payoff = _ql_payoff(leg, v)
        level = Trigger.level(b, v)
        bt = {(True, "up"): ql.Barrier.UpIn, (True, "down"): ql.Barrier.DownIn,
              (False, "up"): ql.Barrier.UpOut, (False, "down"): ql.Barrier.DownOut}[(knock_in, b.direction)]

        def npv(r):
            opt = ql.BarrierOption(bt, level, r, payoff, ql.EuropeanExercise(cv.to_ql(exp)))
            opt.setPricingEngine(model.barrier_engine(leg.underlying))
            return opt.NPV()

        # the rebate is cash received by the trade holder regardless of the option's position
        sign = 1.0 if leg.position == "long" else -1.0
        option_pv = sign * units * npv(0.0)
        out = {"price": option_pv, "legs": {leg.id: option_pv}, "engine": "AnalyticBarrierEngine"}
        if rebate:
            rebate_pv = units * (npv(rebate / units) - npv(0.0))
            out["price"] += rebate_pv
            out["events"] = {trade.events[0][1].id: {"rebate_pv": rebate_pv}}
        return out
