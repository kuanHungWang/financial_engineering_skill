"""Assembler: input document -> validated plan (a dependency graph of components) -> price.

    plan = build_plan(raw_dict, base_dir)     # never raises on bad input; see plan.report
    result = run(plan)                        # only when plan.report.ok

Graph (Monte Carlo):
    curve:<c> -> underlying:<u> -> model -> fixings (Scenario) -> observable:<o> -> leg:<id>, event:<id> -> price
                                                            \\-> discount_factors ----------------------/
    schedule:<s> feeds legs / events. A target event also reads the legs it accumulates.
Graph (analytic):  curve -> underlying -> model -> price
"""
from __future__ import annotations

import bisect
import contextlib
import datetime as dt
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import NormalDist

import numpy as np
import QuantLib as ql
import yaml
from pydantic import ValidationError

from . import analytic as _analytic  # noqa: F401  (registers analytic pricers)
from . import bermudan as _bermudan  # noqa: F401  (registers bermudan_call)
from . import conventions as cv
from . import coupons as _coupons  # noqa: F401  (registers coupon legs)
from . import market as _market  # noqa: F401  (registers curves, vols)
from . import observables as obs
from . import rates as _rates  # noqa: F401  (registers hull_white)
from . import registry
from .issues import Report
from .exercise import longstaff_schwartz
from .market import simulation_curve
from .models import build_underlying
from .observables import RATES
from .payoffs import CONTINUOUS, PCtx, VCtx, merge_requirements, parsed_conditions
from .schedules import DateContext
from .spec import FixingsData, MarketData, PricingInput


# ------------------------------------------------------------------ data holders
@dataclass
class ModelInfo:
    cls: type
    spec: object
    european_engine_available: bool
    barrier_engine_available: bool


@dataclass
class ParsedTrade:
    legs: list                    # [(cls, spec, path)]
    conditions: dict              # leg path -> [(cls, spec, path)]
    events: list                  # [(cls, spec, path)]
    model: ModelInfo


@dataclass
class Node:
    key: str
    deps: list
    desc: str
    fn: object = None


@dataclass
class Plan:
    report: Report
    input: PricingInput | None = None
    market: MarketData | None = None
    trade: ParsedTrade | None = None
    v: VCtx | None = None
    curve_specs: dict = field(default_factory=dict)       # name -> (cls, spec)
    swaption_vol_specs: dict = field(default_factory=dict)  # name -> (cls, spec)
    method: str | None = None
    method_reason: str = ""
    analytic_cls: type | None = None
    analytic_matches: list = field(default_factory=list)   # [(name, ok, reason)]
    cross_check: bool = False
    sim_dates: dict = field(default_factory=dict)          # state (underlying | 'rates') -> [date]
    pay_dates: list = field(default_factory=list)
    memory_mb: float = 0.0
    nodes: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)            # informational messages produced while running

    def summary(self) -> dict:
        out = {"validation": self.report.to_dict()}
        if self.method:
            out["method"] = self.method
            out["method_reason"] = self.method_reason
            out["analytic_candidates"] = [{"pricer": n, "matches": ok, "reason": r}
                                          for n, ok, r in self.analytic_matches]
            out["cross_check"] = self.cross_check
        if self.method == "mc":
            out["simulation"] = {
                "fixing_dates": {u: len(d) for u, d in self.sim_dates.items()},
                "past_fixings_from_history": {n: len(set().union(*r.values()))
                                              for n, r in sorted(self.v.past_reads.items())} if self.v else {},
                "payment_dates": len(self.pay_dates),
                "estimated_memory_mb": round(self.memory_mb, 1),
            }
        if self.nodes:
            out["graph"] = [{"node": n.key, "needs": n.deps, "does": n.desc} for n in self.nodes.values()]
        return out


# ------------------------------------------------------------------ loading
def load_document(source: str) -> tuple[dict, Path]:
    """YAML or JSON from a file path, '-' (stdin), or a literal string."""
    if source == "-":
        text, base = sys.stdin.read(), Path.cwd()
    elif Path(source).exists():
        text, base = Path(source).read_text(), Path(source).resolve().parent
    else:
        text, base = source, Path.cwd()
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError("input must be a YAML/JSON mapping (or a path to one)")
    return doc, base


def apply_overrides(doc: dict, sets: list[str]) -> dict:
    """--set pricing.num_paths=50000  (value parsed as YAML)."""
    for s in sets or []:
        key, _, val = s.partition("=")
        node = doc
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = yaml.safe_load(val)
    return doc


# ------------------------------------------------------------------ planning
def build_plan(raw: dict, base_dir: Path | str = ".") -> Plan:
    rep = Report()
    plan = Plan(rep)
    base_dir = Path(base_dir)

    # 1. document shape
    try:
        inp = PricingInput.model_validate(raw)
    except ValidationError as e:
        rep.extend_pydantic("", e)
        return plan
    plan.input = inp
    val = inp.valuation_date
    ql.Settings.instance().evaluationDate = cv.to_ql(val)

    # 2. market data
    mraw = inp.market_data
    if isinstance(mraw, str):
        mpath = (base_dir / mraw)
        if not mpath.exists():
            rep.error("market_data", f"market data file not found: {mpath}",
                      "put the file next to the input, pass an absolute path, or inline the document")
            return plan
        mraw = yaml.safe_load(mpath.read_text())
    try:
        market = MarketData.model_validate(mraw)
    except ValidationError as e:
        rep.extend_pydantic("market_data", e)
        return plan
    plan.market = market
    if market.as_of != val:
        rep.warning("market_data.as_of", f"market data as_of {market.as_of} differs from valuation_date {val}")

    # 2b. past fixings shared by trades (a file appended over the trade's life)
    fixings = FixingsData()
    fraw = inp.fixings
    if isinstance(fraw, str):
        fpath = base_dir / fraw
        if not fpath.exists():
            rep.error("fixings", f"fixings file not found: {fpath}",
                      "put the file next to the input, pass an absolute path, or inline the document")
            return plan
        fraw = yaml.safe_load(fpath.read_text()) or {}
    if fraw is not None:
        try:
            fixings = FixingsData.model_validate(fraw)
        except ValidationError as e:
            rep.extend_pydantic("fixings", e)
            return plan

    # 3. trade dates
    cal_conv = (inp.trade.calendar, inp.trade.business_day_convention)
    for f, x, parse in (("calendar", cal_conv[0], cv.calendar), ("business_day_convention", cal_conv[1], cv.bdc)):
        try:
            parse(x)
        except ValueError as e:
            rep.error(f"trade.{f}", str(e))
            return plan
    try:
        eff0 = DateContext(val, val, val, *cal_conv).resolve(inp.trade.effective_date or "valuation_date")
        maturity = DateContext(val, eff0, val, *cal_conv).resolve(inp.trade.maturity, anchor=eff0)
        maturity_unadj = DateContext(val, eff0, val, *cal_conv).resolve(inp.trade.maturity, anchor=eff0,
                                                                        unadjusted=True)
    except ValueError as e:
        rep.error("trade.maturity", str(e))
        return plan
    if maturity <= val:
        rep.error("trade.maturity", f"maturity {maturity} is not after valuation_date {val}")
        return plan
    dates = DateContext(val, eff0, maturity, *cal_conv, unadjusted={"maturity": maturity_unadj})

    # 4. model and the market data it needs
    mcls, mspec = registry.parse("model", inp.model, "model", rep)
    if mcls is None:
        return plan
    unds = mcls.underlyings(mspec)
    for u in unds:
        if u not in market.equities:
            rep.error("model.underlying", f"underlying '{u}' not in market data.{cv.suggest(u, market.equities)}",
                      f"market equities: {sorted(market.equities)}")

    def need_curve(c, path):
        if c not in market.curves:
            rep.error(path, f"curve '{c}' not in market data.{cv.suggest(c, market.curves)}",
                      f"market curves: {sorted(market.curves)}")
        elif c not in plan.curve_specs:
            ccls, cspec = registry.parse("curve", market.curves[c], f"market_data.curves.{c}", rep)
            if ccls:
                plan.curve_specs[c] = (ccls, cspec)

    for c in mcls.curves(mspec):
        need_curve(c, "model.discount_curve")
    for u in unds:
        eq = market.equities.get(u)
        if eq:
            registry.parse("curve", eq.dividend, f"market_data.equities.{u}.dividend", rep)
            registry.parse("vol", eq.vol, f"market_data.equities.{u}.vol", rep)
            try:
                cv.calendar(eq.calendar)
            except ValueError as e:
                rep.error(f"market_data.equities.{u}.calendar", str(e))
    for name in mcls.swaption_vols(mspec):
        if name not in market.swaption_vols:
            rep.error("model.calibration.swaption_vol",
                      f"swaption vol '{name}' not in market data.{cv.suggest(name, market.swaption_vols)}",
                      f"market swaption_vols: {sorted(market.swaption_vols)}")
            continue
        scls, sspec = registry.parse("swaption_vol", market.swaption_vols[name],
                                     f"market_data.swaption_vols.{name}", rep)
        if scls:
            plan.swaption_vol_specs[name] = (scls, sspec)
    mcls.validate(mspec, market, rep, "model")
    minfo = ModelInfo(mcls, mspec, getattr(mcls, "has_european_engine", False),
                      getattr(mcls, "has_barrier_engine", False))

    # 4b. observables (and the forecast curves they read)
    parsed_obs = {}
    for name, oraw in inp.observables.items():
        ocls, ospec = registry.parse("observable", oraw, f"observables.{name}", rep)
        if ocls:
            parsed_obs[name] = (ocls, ospec)
            for c in ocls.curves(ospec):
                if c in market.curves:
                    need_curve(c, f"observables.{name}.curve")

    # rate observables take the fixings file's entry; their own history wins date by date
    for name, (ocls, ospec) in parsed_obs.items():
        if hasattr(ospec, "fixings_key"):
            shared = fixings.rates.get(ospec.fixings_key or name, {})
            if ospec.fixings_key and ospec.fixings_key not in fixings.rates:
                rep.warning(f"observables.{name}.fixings_key", f"no '{ospec.fixings_key}' entry in the fixings "
                                                               f"file.{cv.suggest(ospec.fixings_key, fixings.rates)}")
            ospec.history = {**{d: x for d, x in shared.items() if d < val}, **ospec.history}

    # 5. reference levels and past underlying fixings
    for u in inp.trade.reference_levels:
        if u not in unds:
            rep.error(f"trade.reference_levels.{u}", f"'{u}' is not a model underlying ({unds})")
    history = {u: {d: x for d, x in h.items() if d < val} for u, h in fixings.equities.items() if u in unds}
    for u, h in history.items():
        bad = sorted(d for d, x in h.items() if not x > 0)
        if bad:
            rep.error(f"fixings.equities.{u}", f"closing levels must be > 0 ({len(bad)} bad, first {bad[0]})")
    for u, h in inp.trade.history.items():
        if u not in unds:
            rep.error(f"trade.history.{u}", f"'{u}' is not a model underlying.{cv.suggest(u, unds)}",
                      f"model underlyings: {unds}; give rate fixings in the observable's own history")
            continue
        bad = sorted(d for d, x in h.items() if not x > 0)
        if bad:
            rep.error(f"trade.history.{u}", f"closing levels must be > 0 ({len(bad)} bad, first {bad[0]})")
        late = sorted(d for d in h if d >= val)
        if late:
            rep.warning(f"trade.history.{u}", f"{len(late)} date(s) on or after the valuation date are ignored "
                                              f"(first {late[0]}); those are simulated from today's spot")
        history[u] = {**history.get(u, {}), **{d: x for d, x in h.items() if d < val}}
    ref_levels = {}
    for u in unds:
        if u not in market.equities:
            continue
        if u in inp.trade.reference_levels:
            ref_levels[u] = inp.trade.reference_levels[u]
        elif eff0 < val and eff0 in history.get(u, {}):
            ref_levels[u] = history[u][eff0]
        else:
            ref_levels[u] = market.equities[u].spot
            if eff0 < val:
                rep.warning(f"trade.reference_levels.{u}",
                            f"trade started {eff0} (before valuation) but no initial fixing given; using spot",
                            "set trade.reference_levels (or trade.history on effective_date) for relative "
                            "strikes/barriers of a seasoned trade")

    # 6. schedules (resolved in dependency order)
    sched_parsed = {}
    for name, sraw in inp.schedules.items():
        if name == CONTINUOUS:
            rep.error(f"schedules.{name}", "'continuous' is reserved")
            continue
        scls, sspec = registry.parse("schedule", sraw, f"schedules.{name}", rep)
        if scls:
            sched_parsed[name] = (scls, sspec)
    resolved: dict[str, list] = {}

    def resolve(name, stack=()):
        if name in resolved:
            return True
        if name in stack:
            rep.error(f"schedules.{name}", f"circular schedule reference: {' -> '.join(stack + (name,))}")
            return False
        scls, sspec = sched_parsed[name]
        for dep in scls.dependencies(sspec):
            if dep not in sched_parsed:
                rep.error(f"schedules.{name}.base", f"schedule '{dep}' is not defined."
                          f"{cv.suggest(dep, sched_parsed)}")
                return False
            if not resolve(dep, stack + (name,)):
                return False
        try:
            resolved[name] = scls.resolve(sspec, dates, resolved)
        except ValueError as e:
            rep.error(f"schedules.{name}", str(e))
            return False
        if not resolved[name]:
            rep.error(f"schedules.{name}", "schedule resolved to no dates")
            return False
        return True

    for name in sched_parsed:
        resolve(name)

    # 7. observables and trade components. Semantic checks run on everything that parsed, and
    #    references are scanned on the raw dicts too, so one validation round reports as much as possible.
    if not all(u in market.equities for u in unds):
        return plan
    v = VCtx(dates, val, maturity, resolved, unds, {u: market.equities[u].spot for u in unds},
             ref_levels, rep, broken_schedules=set(sched_parsed) - set(resolved) | set(inp.schedules) - set(sched_parsed),
             observables=parsed_obs, stochastic_rates=mcls.stochastic_rates, model_name=mcls.type_name,
             curve_names=set(market.curves), history=history)
    plan.v = v
    obs.validate_all(v)
    broken_obs = set(inp.observables) - set(parsed_obs)
    _scan_references(inp.trade, v, broken_obs)
    legs, conds, events, ids = [], {}, [], {}
    for i, lraw in enumerate(inp.trade.legs):
        path = f"trade.legs[{i}]"
        lcls, lspec = registry.parse("leg", lraw, path, rep)
        if lcls:
            legs.append((lcls, lspec, path))
            conds[path] = parsed_conditions(lspec, rep, path)
    for i, eraw in enumerate(inp.trade.termination_events):
        path = f"trade.termination_events[{i}]"
        ecls, espec = registry.parse("event", eraw, path, rep)
        if ecls:
            events.append((ecls, espec, path))
    for _, s, path in legs + events:
        if s.id in ids:
            rep.error(path + ".id", f"duplicate id '{s.id}' (also used at {ids[s.id]})")
        ids[s.id] = path
    v.legs = {s.id: (c, s) for c, s, _ in legs}
    if broken_obs:          # references to observables that failed to parse were already reported
        return plan
    for cls, spec, path in legs + events:
        cls.validate(spec, v, path)
    exercise = [path for cls, _, path in events if getattr(cls, "exercise", False)]
    for path in exercise[1:]:
        rep.error(path, "only one Bermudan exercise event per trade is supported",
                  f"first one at {exercise[0]}")
    if not rep.ok:
        return plan

    # 7b. what every component reads, declared through obs.requirements. Dates before the valuation date are
    #     served from history for any payoff, so they are checked here, once, for the whole trade.
    fixing_reqs = []
    for cls, spec, path in legs + events:
        with obs.reading(v, path):
            fixing_reqs.append(cls.fixing_dates(spec, v, path))
    obs.check_history(v)
    if rep.ok:
        for (cls, spec, path), reads in zip(events, fixing_reqs[len(legs):]):
            if hasattr(cls, "validate_past"):
                cls.validate_past(spec, v, path, reads)
    if not rep.ok:
        return plan
    trade = ParsedTrade(legs, conds, events, minfo)
    plan.trade = trade

    # 8. method selection
    settings = inp.pricing
    plan.analytic_matches = [(n, *c.match(trade)) for n, c in registry.types("analytic").items()]
    # Closed forms start from today's spot: they cannot use known fixings, and QuantLib treats an exercise on its
    # reference date as already expired (NPV 0), so a fixing read ON the valuation date also needs Monte Carlo.
    blocker = ("the trade reads fixings before the valuation date" if v.past_reads else
               _fixed_today(legs, v))
    if blocker:
        plan.analytic_matches = [(n, False, blocker) if ok else (n, ok, r) for n, ok, r in plan.analytic_matches]
    matching = [(n, r) for n, ok, r in plan.analytic_matches if ok]
    uses_continuous = any(s.observation == CONTINUOUS for c in conds.values() for _, s, _ in c) or \
        any(getattr(s, "observation", None) == CONTINUOUS for _, s, _ in events)
    reasons = "; ".join(f"{n}: {r}" for n, ok, r in plan.analytic_matches if not ok)
    if settings.method == "analytic" or (settings.method == "auto" and matching):
        if not matching:
            rep.error("pricing.method", f"no analytic pricer matches this trade ({reasons})",
                      "use method: auto or mc")
            return plan
        plan.method, plan.method_reason = "analytic", f"{matching[0][0]}: {matching[0][1]}"
        plan.analytic_cls = registry.get("analytic", matching[0][0])
    else:
        if uses_continuous:
            rep.error("pricing.method", "continuous barrier monitoring can only be priced analytically, "
                                        f"and no analytic pricer matches ({reasons})",
                      "observe the barrier on a schedule instead, e.g. a generated schedule with frequency 1D")
            return plan
        plan.method = "mc"
        plan.method_reason = ("requested" if settings.method == "mc"
                              else f"no analytic pricer matches ({reasons})")
        plan.cross_check = bool(settings.cross_check and matching)
        if plan.cross_check:
            plan.analytic_cls = registry.get("analytic", matching[0][0])

    # 9. simulation needs
    if plan.method == "mc":
        pays = set()
        for cls, spec, path in legs + events:
            pays.update(cls.payment_dates(spec, v))
        plan.sim_dates = merge_requirements(*fixing_reqs)
        plan.pay_dates = sorted(pays)
        eq = [ds for k, ds in plan.sim_dates.items() if k != RATES]
        n_eq = _grid_size(set().union(*eq) | {val}, settings.steps_per_year) if eq else 0
        n_rates = len(set(plan.sim_dates.get(RATES, [])) | pays) + 1 if mcls.stochastic_rates else 0
        held = settings.num_paths + (_fit_paths(settings) if _exercise_event(trade) else 0)   # + fit paths
        plan.memory_mb = (2.2 * n_eq * len(unds) + 2.2 * n_rates) * held * 8 / 2**20
        if plan.memory_mb > settings.max_memory_mb:
            rep.error("pricing.num_paths",
                      f"path storage ~{plan.memory_mb:,.0f} MB exceeds max_memory_mb {settings.max_memory_mb}",
                      f"reduce num_paths (e.g. {int(settings.num_paths * settings.max_memory_mb / plan.memory_mb) // 2 * 2}) "
                      "or raise pricing.max_memory_mb")

    # 10. horizon warnings
    for u in unds:
        eq = market.equities[u]
        vcls = registry.get("vol", eq.vol["type"])
        vspec = vcls.Spec.model_validate({k: x for k, x in eq.vol.items() if k != "type"})
        if hasattr(vspec, "tenors"):
            last = max(dates.resolve(t, anchor=val) for t in vspec.tenors)   # business-day adjusted
            if last < maturity:
                rep.warning(f"market_data.equities.{u}.vol", f"vol data ends {last}, before maturity {maturity}; "
                                                             "flat extrapolation is used")
    for name, (_, sspec) in plan.swaption_vol_specs.items():
        last = max(dates.resolve(t, anchor=dates.resolve(e, anchor=val)) for e in sspec.expiries for t in sspec.tenors)
        if last < maturity:
            rep.warning(f"market_data.swaption_vols.{name}", f"swaption vols cover swaps ending by {last}, before "
                                                             f"maturity {maturity}; flat extrapolation is used")
    for c, (ccls, cspec) in plan.curve_specs.items():
        tenors = getattr(cspec, "tenors", None) or [q.tenor for q in getattr(cspec, "swap", [])] or \
            [q.tenor for q in getattr(cspec, "deposit", [])]
        if tenors:
            last = max(dates.resolve(t, anchor=val) for t in tenors)
            if last < maturity:
                rep.warning(f"market_data.curves.{c}", f"curve data ends {last}, before maturity {maturity}")
    if rep.ok:
        _build_graph(plan)
    return plan


def _scan_references(trade, v: VCtx, broken_obs):
    """Check underlying / schedule / observable references in raw leg, condition and event dicts."""
    from .payoffs import _check_ref, _check_schedule
    items = [(f"trade.legs[{i}]", x) for i, x in enumerate(trade.legs)]
    items += [(f"trade.termination_events[{i}]", x) for i, x in enumerate(trade.termination_events)]
    for path, raw in list(items):
        if not isinstance(raw, dict):
            continue
        for j, c in enumerate(raw.get("conditions") or []):
            items.append((f"{path}.conditions[{j}]", c))
        for j, c in enumerate(raw.get("range") or []):
            items.append((f"{path}.range[{j}]", c))
    for path, raw in items:
        if not isinstance(raw, dict):
            continue
        if isinstance(raw.get("underlying"), str):
            _check_ref(v, raw["underlying"], path + ".underlying")
        if isinstance(raw.get("observation"), str):
            _check_schedule(v, raw["observation"], path + ".observation", allow_continuous=True)
        if isinstance(raw.get("schedule"), str):
            _check_schedule(v, raw["schedule"], path + ".schedule")
        for key in ("observable", "index"):
            if isinstance(raw.get(key), str) and raw[key] not in broken_obs:
                obs.known(v, raw[key], f"{path}.{key}")


def _fixed_today(legs, v: VCtx):
    """Reason closed forms cannot price an option leg fixed on the valuation date, or None."""
    for cls, spec, path in legs:
        if hasattr(cls, "dates") and cls.dates(spec, v)[0] <= v.valuation_date:
            return f"{path} expires on the valuation date (its value is today's intrinsic)"
    return None


def _grid_size(dates, steps_per_year):
    pts = sorted(dates)
    n = len(pts)
    if steps_per_year > 0 and n > 1:
        gap = max(1, int(365 / steps_per_year))
        n += sum(max(0, math.ceil((b - a).days / gap) - 1) for a, b in zip(pts[:-1], pts[1:]))
    return n


def _obs_refs(spec) -> set:
    """Observable names a leg or event reads (for the plan graph)."""
    out = set()
    for key in ("observable", "index"):
        if isinstance(getattr(spec, key, None), str):
            out.add(getattr(spec, key))
    for r in getattr(spec, "range", None) or []:
        out.add(r.observable)
    for c in getattr(spec, "conditions", None) or []:
        if isinstance(c, dict) and isinstance(c.get("observable"), str):
            out.add(c["observable"])
    return out


# ------------------------------------------------------------------ graph
def _build_graph(plan: Plan):
    inp, market, trade, v = plan.input, plan.market, plan.trade, plan.v
    ref = cv.to_ql(inp.valuation_date)
    mi = trade.model
    disc = mi.cls.curves(mi.spec)[0]
    unds = mi.cls.underlyings(mi.spec)
    nodes = plan.nodes

    def add(key, deps, desc, fn):
        nodes[key] = Node(key, deps, desc, fn)

    for c, (ccls, cspec) in plan.curve_specs.items():
        add(f"curve:{c}", [], f"{ccls.type_name} curve",
            lambda r, ccls=ccls, cspec=cspec: ccls.build(cspec, ref))
    horizon = cv.to_ql(v.maturity) + 31
    add(f"discount:{disc}", [f"curve:{disc}"], "discount curve on an Actual365Fixed time axis "
        "(resampled daily if the source day count differs; DFs unchanged)",
        lambda r: _sim_curve(plan, r[f"curve:{disc}"], ref, horizon))
    for u in unds:
        add(f"underlying:{u}", [f"discount:{disc}"],
            f"spot/dividend/{market.equities[u].vol['type']} vol on the model time axis",
            lambda r, u=u: build_underlying(u, market.equities[u], ref, r[f"discount:{disc}"]))
    years = (v.maturity - v.valuation_date).days / 365.0
    curve_nodes = [f"curve:{c}" for c in plan.curve_specs]

    for name, (scls, sspec) in plan.swaption_vol_specs.items():
        add(f"swaption_vol:{name}", [], f"{scls.type_name} swaption vols ({sspec.vol_type})",
            lambda r, scls=scls, sspec=sspec: scls.build(sspec))
    svol_nodes = [f"swaption_vol:{x}" for x in plan.swaption_vol_specs]

    def build_model(r):
        extra = {"swaption_vols": {x: r[f"swaption_vol:{x}"] for x in plan.swaption_vol_specs}} if svol_nodes else {}
        m = mi.cls.build(mi.spec, inp.valuation_date, r[f"discount:{disc}"],
                         {u: r[f"underlying:{u}"] for u in unds}, horizon_years=years, **extra)
        m.curves = {c: r[f"curve:{c}"] for c in plan.curve_specs}
        return m
    calibrated = " calibrated to " + ", ".join(plan.swaption_vol_specs) + " swaptions" if svol_nodes else ""
    add("model", [f"discount:{disc}"] + [f"underlying:{u}" for u in unds] + curve_nodes + svol_nodes,
        f"{mi.cls.type_name} model{calibrated}", build_model)

    if plan.method == "mc":
        s = inp.pricing
        sched_refs = sorted({x for _, sp, _ in trade.legs + trade.events
                             for x in [getattr(sp, "schedule", None), getattr(sp, "observation", None)]
                             if x and x != CONTINUOUS} |
                            {c.observation for cl in trade.conditions.values() for _, c, _ in cl
                             if c.observation != CONTINUOUS})
        for name in sched_refs:
            add(f"schedule:{name}", [], f"{len(v.schedules[name])} dates", lambda r, name=name: v.schedules[name])
        states = ", ".join(f"{k}: {len(d)} dates" for k, d in plan.sim_dates.items()) or "nothing stochastic"
        add("fixings", ["model"],
            f"simulate {s.num_paths} paths ({'antithetic, ' if s.antithetic else ''}seed {s.seed}); {states}",
            lambda r: r["model"].simulate(plan.sim_dates, plan.pay_dates, s.num_paths, s.seed, s.antithetic,
                                          s.steps_per_year))

        def obs_node(name):
            key = f"observable:{name}"
            if key not in nodes:
                cls, spec = obs.lookup(v, name)
                deps = ["fixings"] + [obs_node(d) for d in cls.deps(spec)]
                add(key, deps, f"{cls.type_name} observable", lambda r: None)
            return key

        pctx = lambda r: PCtx(v, r["fixings"], r["fixings"].n_paths)  # noqa: E731
        for cls, spec, path in trade.legs:
            used = {getattr(spec, "schedule", None)} | {c.observation for _, c, _ in trade.conditions[path]}
            deps = ["fixings"] + [f"schedule:{x}" for x in sched_refs if x in used] + \
                [obs_node(o) for o in sorted(_obs_refs(spec))]
            add(f"leg:{spec.id}", deps, f"{cls.type_name} cashflow paths",
                lambda r, cls=cls, spec=spec, path=path: cls.cashflows(spec, pctx(r), path))
        for cls, spec, path in trade.events:
            lids = list(spec.legs) if getattr(cls, "leg_dependent", False) else []
            deps = ["fixings"] + ([f"schedule:{spec.observation}"] if getattr(spec, "observation", None) in sched_refs
                                  else []) + [obs_node(o) for o in sorted(_obs_refs(spec))] + [f"leg:{x}" for x in lids]
            add(f"event:{spec.id}", deps, f"{cls.type_name} " + ("exercise amounts and regression state"
                                                                  if getattr(cls, "exercise", False)
                                                                  else "termination dates"),
                lambda r, cls=cls, spec=spec, path=path, lids=lids:
                cls.evaluate(spec, pctx(r), path, {x: r[f"leg:{x}"] for x in lids}))
        add("discount_factors", ["fixings"],
            f"{'path-wise' if mi.cls.stochastic_rates else 'deterministic'} DFs on {len(plan.pay_dates)} payment dates",
            lambda r: r["fixings"].discount(plan.pay_dates))
        price_deps = [f"leg:{sp.id}" for _, sp, _ in trade.legs] + \
            [f"event:{sp.id}" for _, sp, _ in trade.events] + ["discount_factors"]
        if _exercise_event(trade):
            n_fit = _fit_paths(s)
            add("exercise_policy", ["model"] + price_deps,
                f"fit Longstaff-Schwartz exercise regressions on {n_fit} independent paths (seed {_fit_seed(s)})"
                if n_fit else "fit Longstaff-Schwartz exercise regressions on the pricing paths (in sample)",
                lambda r: _fit_exercise(plan, r))
            price_deps.append("exercise_policy")
        add("price", price_deps, "apply terminations and exercise, discount, average paths",
            lambda r: _aggregate_mc(plan, r))
        if plan.cross_check:
            add("analytic_check", ["model"], f"{plan.analytic_cls.type_name} closed form for cross-check",
                lambda r: plan.analytic_cls.price(trade, r["model"], v))
    else:
        add("price", ["model"], f"{plan.analytic_cls.type_name} closed form",
            lambda r: plan.analytic_cls.price(trade, r["model"], v))


def _sim_curve(plan, h, ref, horizon):
    out, resampled = simulation_curve(h, ref, horizon)
    if resampled:
        plan.notes.append(f"discount curve day count '{h.dayCounter().name()}' re-expressed on "
                          "Actual365Fixed for simulation (discount factors preserved on every date)")
    return out


# ------------------------------------------------------------------ execution
def run(plan: Plan) -> dict:
    """Execute the graph. Library chatter goes to stderr so stdout stays JSON."""
    if not plan.report.ok:
        raise RuntimeError("plan has validation errors; fix them before running")
    with contextlib.redirect_stdout(sys.stderr):
        return _run(plan)


def _run(plan: Plan) -> dict:
    results, timings = {}, {}

    def ev(key):
        if key not in results:
            n = plan.nodes[key]
            for d in n.deps:
                ev(d)
            t = time.perf_counter()
            results[key] = n.fn(results)
            timings[key] = round(time.perf_counter() - t, 3)
        return results[key]

    for key in plan.nodes:
        ev(key)
    inp = plan.input
    price = results["price"]
    out = {
        "trade_id": inp.trade.id,
        "valuation_date": inp.valuation_date.isoformat(),
        "currency": inp.trade.currency,
        "method": plan.method,
        "method_reason": plan.method_reason,
        **price,
    }
    model = results["model"]
    if model.diagnostics:
        out["model_diagnostics"] = model.diagnostics
    if "analytic_check" in results:
        a = results["analytic_check"]["price"]
        gap = price["price"] - a
        se = price.get("std_error") or float("nan")
        out["cross_check"] = {"analytic_price": a, "mc_minus_analytic": gap,
                              "gap_in_std_errors": gap / se if se else None,
                              "agrees": bool(se and abs(gap) <= 4 * se)}
    if plan.method == "mc":
        out["martingale_check"] = _martingale_check(plan, results)
    out["warnings"] = [str(i) for i in plan.report.warnings]
    if plan.notes:
        out["notes"] = plan.notes
    for chk in out.get("martingale_check", {}).values():
        if not chk["ok"]:
            out["warnings"].append(f"[WARNING] martingale check failed: {chk}")
    for u, lv in model.diagnostics.get("local_vol", {}).items():
        if lv["illegal_fraction"] > 0:
            out["warnings"].append(
                f"[WARNING] model: {u} Dupire local vol is illegal on {lv['illegal_fraction']:.1%} of the "
                f"(0..T, 50%..200% spot) grid; those points are floored at {lv['floor']:.4f}. Check the vol surface "
                "for butterfly/calendar arbitrage (steep wings) if paths spend time there.")
    cal = model.diagnostics.get("calibration")
    if cal and cal.get("instruments"):
        worst = max(cal["instruments"], key=lambda f: abs(f["vol_error"] / f["market_vol"])
                    if np.isfinite(f["vol_error"]) else np.inf)
        rel = abs(worst["vol_error"] / worst["market_vol"])
        if not rel <= CALIBRATION_TOLERANCE:
            out["warnings"].append(
                f"[WARNING] model: calibration misses the {worst['expiry']}x{worst['tenor']} swaption by {rel:.1%} of "
                f"its vol (market {worst['market_vol']:.5f}, model {worst['model_vol']:.5f}). Constant-parameter "
                "Hull-White 1F cannot fit every swaption: check that the calibration instruments are the ones that "
                "drive the trade (coterminal for a Bermudan call), or let mean_reversion float.")
    if out.get("cross_check") and not out["cross_check"]["agrees"]:
        out["warnings"].append("[WARNING] Monte Carlo and analytic prices differ by more than 4 std errors")
    out["timings_sec"] = timings
    return out


CALIBRATION_TOLERANCE = 0.05   # worst relative vol miss of a calibration instrument before warning
FIT_SEED_OFFSET = 1_000_003      # seed of the independent exercise-fit paths: pricing.seed + this


def _exercise_event(trade):
    for cls, spec, _ in trade.events:
        if getattr(cls, "exercise", False):
            return spec
    return None


def _fit_paths(s) -> int:
    return s.num_paths if s.exercise_fit_paths is None else s.exercise_fit_paths


def _fit_seed(s) -> int:
    return s.seed + FIT_SEED_OFFSET


@dataclass
class _Flows:
    """Every cashflow of the trade after the (non-exercise) termination events, path by path."""
    rows: list            # [(kind, owner id, date, amounts (n,))]; kind: leg | rebate | conditional | exercise
    first: np.ndarray     # first termination date per path (NaT if none)
    settle: np.ndarray    # that termination pays the cashflows on its date
    owner: np.ndarray     # index into events of the terminating event, -1 if none
    events: list          # [(spec, EventResult)] for the non-exercise events
    n: int

    def alive(self, d):
        d64 = np.datetime64(d, "D")
        return np.isnat(self.first) | (self.first > d64) | ((self.first == d64) & self.settle)


def _flows(plan: Plan, r) -> _Flows:
    trade = plan.trade
    n = r["fixings"].n_paths
    first = np.full(n, np.datetime64("NaT"), dtype="datetime64[D]")
    settle = np.zeros(n, dtype=bool)
    owner = np.full(n, -1)
    ev_res = []
    for _, spec, _ in [e for e in trade.events if not getattr(e[0], "exercise", False)]:
        er = r[f"event:{spec.id}"]
        i = len(ev_res)
        ev_res.append((spec, er))
        better = ~np.isnat(er.hit_date) & (np.isnat(first) | (er.hit_date < first))
        first = np.where(better, er.hit_date, first)
        settle = np.where(better, er.settle_on_event_date, settle)
        owner = np.where(better, i, owner)
    f = _Flows([], first, settle, owner, ev_res, n)
    for _, spec, _ in trade.legs:
        lr = r[f"leg:{spec.id}"]
        scales = [(i, er) for i, (_, er) in enumerate(ev_res) if spec.id in er.leg_scale]
        for row, d in enumerate(lr.amounts.index):
            cf = lr.amounts.iloc[row].values * f.alive(d)
            d64 = np.datetime64(d, "D")
            for i, er in scales:
                cf = np.where((owner == i) & (er.hit_date == d64), cf * er.leg_scale[spec.id], cf)
            f.rows.append(("leg", spec.id, d, cf))
    for i, (spec, er) in enumerate(ev_res):
        if er.rebate is not None:
            for row, d in enumerate(er.rebate.index):
                f.rows.append(("rebate", spec.id, d, er.rebate.iloc[row].values * (owner == i)))
        if er.conditional is not None:
            for row, d in enumerate(er.conditional.index):
                f.rows.append(("conditional", spec.id, d, er.conditional.iloc[row].values * f.alive(d)))
    return f


def _exercise_problem(plan: Plan, r, f: _Flows):
    """Longstaff-Schwartz tables for the Bermudan event: one row per exercise date e_k plus a final row for what
    is paid after the last one. A cashflow belongs to the first row whose exercise would not cancel it (on e_k
    itself when settled on the event date), valued in that row's money: PV / D(0, e_k). Forward discount factors
    D(0, e_k) / D(0, e_{k-1}) then make every row's value path-wise exact under stochastic rates too."""
    spec = _exercise_event(plan.trade)
    inp = r[f"event:{spec.id}"]
    dfs = r["discount_factors"]
    dfv = {d: np.broadcast_to(dfs[i], (f.n,)) for i, d in enumerate(plan.pay_dates)}
    ex = inp.dates
    k_n = len(ex)
    pick = bisect.bisect_left if spec.settle_on_event_date else bisect.bisect_right
    pv = np.zeros((k_n + 1, f.n))
    last = max([d for _, _, d, _ in f.rows] + [ex[-1]])
    for _, _, d, cf in f.rows:
        pv[pick(ex, d)] += cf * dfv[d]
    disc = np.vstack([dfv[e] for e in ex] + [dfv[last]])
    fdf = disc / np.vstack([np.ones(f.n), disc[:-1]])
    payoff = np.vstack([inp.amount, np.full((1, f.n), np.nan)])
    ex64 = np.array(ex, dtype="datetime64[D]")
    blocked = ~np.isnat(f.first)[None, :] & (f.first[None, :] <= ex64[:, None])   # already terminated
    payoff[:k_n][blocked] = np.nan
    state = np.concatenate([inp.state, inp.state[-1:]], axis=0)
    return spec, inp, (pv / disc, payoff, fdf, state)


def _solve_exercise(spec, tables, policy=None):
    cf, payoff, fdf, state = tables
    return longstaff_schwartz(cf, payoff, fdf, state, exerciser=spec.exerciser, settle_on_event_date=True,
                              degree=spec.degree, itm_only=spec.exercise_option is not None, policy=policy)


def _resimulate(plan: Plan, r, n, seed) -> dict:
    """Legs, events and discount factors again on an independent set of paths."""
    s = plan.input.pricing
    rr = {k: x for k, x in r.items() if k == "model" or k.startswith(("curve:", "discount:", "underlying:",
                                                                         "schedule:"))}
    rr["fixings"] = r["model"].simulate(plan.sim_dates, plan.pay_dates, n, seed, s.antithetic, s.steps_per_year)

    def ev(key):
        if key not in rr:
            node = plan.nodes[key]
            for d in node.deps:
                ev(d)
            rr[key] = node.fn(rr)
    for key in plan.nodes:
        if key.startswith(("observable:", "leg:", "event:")) or key == "discount_factors":
            ev(key)
    return rr


def _fit_exercise(plan: Plan, r) -> dict:
    s = plan.input.pricing
    n_fit = _fit_paths(s)
    rr = _resimulate(plan, r, n_fit, _fit_seed(s)) if n_fit else r
    spec, _, tables = _exercise_problem(plan, rr, _flows(plan, rr))
    res = _solve_exercise(spec, tables)
    return {"policy": res.diagnostics["policy"], "fit_paths": n_fit or s.num_paths,
            "fit_seed": _fit_seed(s) if n_fit else s.seed, "in_sample": not n_fit}


def _apply_exercise(plan: Plan, r, f: _Flows):
    """Run the fitted exercise rule on the pricing paths; cancel what exercise cancels, add exercise payments.
    Returns (exercise date per path, settle flag, diagnostics)."""
    spec, inp, tables = _exercise_problem(plan, r, f)
    fit = r["exercise_policy"]
    res = _solve_exercise(spec, tables, None if fit["in_sample"] else fit["policy"])
    ex64 = np.array(inp.dates, dtype="datetime64[D]")
    hit = res.event[:-1]
    ex_date = np.where(hit.any(axis=0), ex64[hit.argmax(axis=0)], np.datetime64("NaT"))
    settle = spec.settle_on_event_date
    for j, (kind, oid, d, cf) in enumerate(f.rows):
        d64 = np.datetime64(d, "D")
        f.rows[j] = (kind, oid, d, cf * (np.isnat(ex_date) | (d64 < ex_date) | ((d64 == ex_date) & settle)))
    for k, d in enumerate(inp.dates):
        f.rows.append(("exercise", spec.id, d, inp.amount[k] * hit[k]))
    any_ex = ~np.isnat(ex_date)
    diag = {"exerciser": spec.exerciser, "exercise_probability": float(any_ex.mean()),
            "exercise_probability_by_date": {d.isoformat(): round(float(p), 6)
                                             for d, p in zip(inp.dates, hit.mean(axis=1)) if p > 0},
            "first_termination_probability": float(any_ex.mean())}
    if any_ex.any():
        t = (ex_date[any_ex] - np.datetime64(plan.input.valuation_date)).astype(float) / 365.0
        diag["expected_years_to_exercise_given_exercise"] = float(t.mean())
    diag["policy"] = {"fit_paths": fit["fit_paths"], "fit_seed": fit["fit_seed"], "in_sample": fit["in_sample"],
                      "regressors": list(spec.regressors) or "model state", "degree": spec.degree}
    if not fit["in_sample"]:
        # the in-sample price is biased up for the exerciser; a large gap means the regression is too weak
        diag["policy"]["in_sample_price"] = float(_solve_exercise(spec, tables).discounted.sum(axis=0).mean())
    return ex_date, settle, diag


def _aggregate_mc(plan: Plan, r) -> dict:
    s = plan.input.pricing
    trade = plan.trade
    n = s.num_paths
    dfs = r["discount_factors"]                              # (len(pay_dates), n or 1)
    dfv = {d: dfs[i] for i, d in enumerate(plan.pay_dates)}
    f = _flows(plan, r)
    ex_spec = _exercise_event(trade)
    ex_date = np.full(n, np.datetime64("NaT"), dtype="datetime64[D]")
    ex_settle, ex_diag = True, None
    if ex_spec is not None:
        ex_date, ex_settle, ex_diag = _apply_exercise(plan, r, f)

    def alive(d):
        d64 = np.datetime64(d, "D")
        return f.alive(d) & (np.isnat(ex_date) | (d64 < ex_date) | ((d64 == ex_date) & ex_settle))

    table = {}                                               # date -> [sum of mean cf, sum of mean pv]
    pv = np.zeros(n)
    by_owner = {}                                            # (kind, id) -> pv per path
    for kind, oid, d, cf in f.rows:
        p = cf * dfv[d]
        t = table.setdefault(d, [0.0, 0.0])
        t[0] += float(cf.mean())
        t[1] += float(p.mean())
        pv += p
        key = (kind, oid)
        by_owner[key] = by_owner.get(key, 0.0) + p

    def owner_pv(kind, oid):
        return float(np.mean(by_owner.get((kind, oid), 0.0)))

    legs_pv, legs_diag = {}, {}
    for _, spec, _ in trade.legs:
        legs_pv[spec.id] = owner_pv("leg", spec.id)
        lr = r[f"leg:{spec.id}"]
        if lr.diagnostics:
            legs_diag[spec.id] = lr.diagnostics
    events_out = {}
    for i, (spec, er) in enumerate(f.events):
        d = dict(er.diagnostics)
        d["first_termination_probability"] = float(
            ((f.owner == i) & (np.isnat(ex_date) | (f.first < ex_date))).mean())
        if er.rebate is not None:
            d["rebate_pv"] = owner_pv("rebate", spec.id)
        if er.conditional is not None:
            d["conditional_payment_pv"] = owner_pv("conditional", spec.id)
        events_out[spec.id] = d
    if ex_spec is not None:
        ex_diag["exercise_payment_pv"] = owner_pv("exercise", ex_spec.id)
        events_out[ex_spec.id] = ex_diag

    mean = float(pv.mean())
    samples = (pv[0::2] + pv[1::2]) / 2 if s.antithetic else pv
    se = float(samples.std(ddof=1) / math.sqrt(len(samples)))
    z = NormalDist().inv_cdf(0.5 + s.confidence / 2)
    out = {
        "price": mean, "std_error": se,
        "confidence_interval": [mean - z * se, mean + z * se], "confidence": s.confidence,
        "num_paths": n, "seed": s.seed, "antithetic": s.antithetic,
        "legs": legs_pv,
    }
    if legs_diag:
        out["leg_diagnostics"] = legs_diag
    if events_out:
        out["events"] = events_out
    out["expected_cashflows"] = [
        {"date": d.isoformat(), "expected_amount": round(a, 6), "discount_factor": round(float(dfv[d].mean()), 8),
         "pv": round(p, 6), "survival_probability": float(alive(d).mean())}
        for d, (a, p) in sorted(table.items()) if abs(a) > 0 or abs(p) > 0]
    return out


def _martingale_check(plan: Plan, r) -> dict:
    """Equities: E[S_T] vs the forward S0 Dq(T)/Dr(T) at the last simulated date.
    Stochastic rates: E[D(0,T)] vs the curve's P(0,T) at the last payment date."""
    out = {}
    model, sc = r["model"], r["fixings"]
    s = plan.input.pricing

    def z_check(x, target):
        samples = (x[0::2] + x[1::2]) / 2 if s.antithetic else x
        se = samples.std(ddof=1) / math.sqrt(len(samples))
        zval = (x.mean() - target) / se if se > 0 else 0.0
        return float(x.mean()), float(zval), bool(abs(zval) < 4)

    for u, paths in sc.equity.items():
        d = paths.index[-1]
        u_data = model.underlyings[u]
        q = cv.to_ql(d)
        fwd = u_data.spot * u_data.dividend.discount(q) / model.discount.discount(q)
        m, zval, ok = z_check(paths.loc[d].values, fwd)
        out[u] = {"date": d.isoformat(), "simulated_mean": m, "forward": float(fwd), "z_score": zval, "ok": ok}
    if sc.rates is not None and plan.pay_dates:
        d = plan.pay_dates[-1]
        p0 = model.discount.discount(cv.to_ql(d))
        m, zval, ok = z_check(sc.discount([d])[0], p0)
        out["discount_factor"] = {"date": d.isoformat(), "simulated_mean": m, "curve": float(p0),
                                  "z_score": zval, "ok": ok}
    return out
