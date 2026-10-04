"""Regression tests for the bermudan_call event (Longstaff-Schwartz exercise inside the engine).

Checked against QuantLib's Hull-White tree for a callable bond and its finite-difference engine for a Bermudan put,
plus limiting cases (an exercise price nobody takes, termination by another event first) and validation.
Run:  python -m pytest tests -q
"""
import copy
import math
import sys
from pathlib import Path

import pytest
import QuantLib as ql
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pricer import build_plan, catalog, run  # noqa: E402

EX = ROOT / "examples"
TODAY = ql.Date(28, 9, 2026)
A, SIG, CPN = 0.03, 0.01, 0.045


def price(d):
    plan = build_plan(copy.deepcopy(d), EX)
    assert plan.report.ok, [str(i) for i in plan.report.errors]
    return run(plan)


def report(d):
    return build_plan(copy.deepcopy(d), EX).report


def errors(d):
    return {i.path: i.message for i in report(d).errors}


def callable_bond(event=None, paths=40000, **pricing):
    ev = {"id": "call", "type": "bermudan_call", "observation": "calls", "exerciser": "issuer", "exercise_price": 100}
    ev.update(event or {})
    return {"valuation_date": "2026-09-28", "market_data": "market/mock_market.yaml",
            "model": {"type": "hull_white", "discount_curve": "USD-FLAT", "mean_reversion": A, "volatility": SIG},
            "schedules": {"annual": {"type": "generated", "frequency": "1Y", "calendar": "NullCalendar"},
                          "calls": {"type": "generated", "frequency": "1Y", "end": "4Y", "calendar": "NullCalendar"}},
            "trade": {"id": "cb", "maturity": "5Y", "calendar": "NullCalendar", "legs": [
                {"id": "cpn", "type": "fixed_leg", "rate": CPN, "notional": 100, "schedule": "annual",
                 "day_count": "Actual365Fixed"},
                {"id": "principal", "type": "fixed_amount", "amount": 100}],
                "termination_events": [ev]},
            "pricing": {"num_paths": paths, "seed": 3, **pricing}}


def ql_flat():
    ql.Settings.instance().evaluationDate = TODAY
    return ql.YieldTermStructureHandle(ql.FlatForward(TODAY, 0.04, ql.Actual365Fixed(), ql.Continuous))


def ql_schedule():
    return ql.Schedule(TODAY, TODAY + ql.Period(5, ql.Years), ql.Period(1, ql.Years), ql.NullCalendar(),
                       ql.Unadjusted, ql.Unadjusted, ql.DateGeneration.Forward, False)


def ql_callable(call_price=100.0):
    ts, sch = ql_flat(), ql_schedule()
    calls = ql.CallabilitySchedule()
    for i in range(1, 5):
        calls.append(ql.Callability(ql.BondPrice(call_price, ql.BondPrice.Clean), ql.Callability.Call, sch[i]))
    bond = ql.CallableFixedRateBond(0, 100.0, sch, [CPN], ql.Actual365Fixed(), ql.Unadjusted, 100.0, TODAY, calls)
    bond.setPricingEngine(ql.TreeCallableFixedRateBondEngine(ql.HullWhite(ts, A, SIG), 1000))
    return bond.NPV()


def ql_straight():
    ts, sch = ql_flat(), ql_schedule()
    bond = ql.FixedRateBond(0, 100.0, sch, [CPN], ql.Actual365Fixed())
    bond.setPricingEngine(ql.DiscountingBondEngine(ts))
    return bond.NPV()


# ------------------------------------------------------------------ against QuantLib
def test_callable_bond_matches_quantlib_hull_white_tree():
    r = price(callable_bond())
    ref = ql_callable()
    # out-of-sample LSM: slightly sub-optimal for the issuer, so at most a few cents above the tree
    assert abs(r["price"] - ref) < 4 * r["std_error"] + 0.02, (r["price"], ref, r["std_error"])
    assert r["price"] < ql_straight() - 2.0                       # the call is worth > 2 points here
    ev = r["events"]["call"]
    assert 0.5 < ev["exercise_probability"] < 0.9
    assert sum(ev["exercise_probability_by_date"].values()) == pytest.approx(ev["exercise_probability"], abs=1e-5)
    assert ev["policy"]["in_sample"] is False and ev["policy"]["fit_paths"] == 40000
    # price = legs + exercise payments, and the survival column reflects the exercise
    assert r["price"] == pytest.approx(sum(r["legs"].values()) + ev["exercise_payment_pv"], rel=1e-12)
    surv = {c["date"]: c["survival_probability"] for c in r["expected_cashflows"]}
    assert surv["2031-09-28"] == pytest.approx(1 - ev["exercise_probability"], abs=1e-9)


def test_bermudan_put_matches_quantlib_fd():
    s0, k, q, r_, vol = 5000.0, 5000.0, 0.01, 0.04, 0.2
    d = {"valuation_date": "2026-09-28", "market_data": "market/mock_market.yaml",
         "model": {"type": "black_scholes", "underlying": "SPX-FLATVOL", "discount_curve": "USD-FLAT"},
         "schedules": {"ex": {"type": "generated", "frequency": "1M", "end": "11M", "calendar": "NullCalendar"}},
         "trade": {"id": "bp", "maturity": "1Y", "legs": [
             {"id": "put", "type": "option_payoff", "underlying": "SPX-FLATVOL", "option_type": "put",
              "strike": 1.0, "notional": 100}],
             "termination_events": [{"id": "ex", "type": "bermudan_call", "observation": "ex", "exerciser": "holder",
                                     "exercise_option": {"underlying": "SPX-FLATVOL", "option_type": "put",
                                                         "strike": 1.0, "notional": 100}}]},
         "pricing": {"num_paths": 100000, "seed": 9}}
    res = price(d)
    ql_flat()
    dc = ql.Actual365Fixed()
    dates = [TODAY + ql.Period(m, ql.Months) for m in range(1, 13)]
    opt = ql.VanillaOption(ql.PlainVanillaPayoff(ql.Option.Put, k), ql.BermudanExercise(dates))
    proc = ql.BlackScholesMertonProcess(
        ql.QuoteHandle(ql.SimpleQuote(s0)), ql.YieldTermStructureHandle(ql.FlatForward(TODAY, q, dc)),
        ql.YieldTermStructureHandle(ql.FlatForward(TODAY, r_, dc)),
        ql.BlackVolTermStructureHandle(ql.BlackConstantVol(TODAY, ql.NullCalendar(), vol, dc)))
    opt.setPricingEngine(ql.FdBlackScholesVanillaEngine(proc, 400, 800))
    ref = opt.NPV() / s0 * 100                                   # notional 100 on a relative strike
    euro = ql.VanillaOption(ql.PlainVanillaPayoff(ql.Option.Put, k), ql.EuropeanExercise(dates[-1]))
    euro.setPricingEngine(ql.AnalyticEuropeanEngine(proc))
    assert res["price"] > euro.NPV() / s0 * 100 + 4 * res["std_error"]   # early exercise premium is visible
    assert abs(res["price"] - ref) < 4 * res["std_error"] + 0.01 * ref, (res["price"], ref, res["std_error"])
    assert res["price"] <= ref + 4 * res["std_error"]                    # out of sample: low-biased for the holder


# ------------------------------------------------------------------ limiting cases
def test_unattractive_exercise_is_never_taken():
    r = price(callable_bond({"exercise_price": 1000}))
    assert r["events"]["call"]["exercise_probability"] == 0.0
    assert abs(r["price"] - ql_straight()) < 4 * r["std_error"]
    # a holder put at zero is never worth taking either
    r = price(callable_bond({"exerciser": "holder", "exercise_price": 0}))
    assert r["events"]["call"]["exercise_probability"] == 0.0


def test_in_sample_fit_and_settlement():
    r_in = price(callable_bond(paths=20000, exercise_fit_paths=0))
    pol = r_in["events"]["call"]["policy"]
    assert pol["in_sample"] is True and "in_sample_price" not in pol
    r_out = price(callable_bond(paths=20000))
    assert r_in["price"] <= r_out["price"] + 4 * r_out["std_error"]       # in sample favours the issuer
    # without the call-date coupon the issuer saves it: the holder is worth less
    r_ns = price(callable_bond({"settle_on_event_date": False}, paths=20000))
    assert r_ns["price"] < r_out["price"] - 1.0


def test_exercise_after_another_termination_is_impossible():
    """An autocall that always triggers on the first quarterly date ends every path before the first call date."""
    d = callable_bond(paths=4000)
    d["model"] = {"type": "black_scholes", "underlying": "SPX-FLATVOL", "discount_curve": "USD-FLAT"}
    d["schedules"]["q1"] = {"type": "dates", "dates": ["2026-12-28"]}
    d["trade"]["termination_events"].append(
        {"id": "ko", "type": "knock_out", "underlying": "SPX-FLATVOL", "direction": "up", "barrier": 1e-6,
         "observation": "q1", "rebate": 100})
    r = price(d)
    assert r["events"]["call"]["exercise_probability"] == 0.0
    assert r["events"]["ko"]["first_termination_probability"] == 1.0
    assert r["price"] == pytest.approx(100 * math.exp(-0.04 * 91 / 365), rel=1e-9)


def test_examples_price():
    for name in ("callable_fixed_rate_bond.yaml", "cancellable_swap.yaml", "bermudan_put.yaml"):
        d = yaml.safe_load((EX / name).read_text())
        d["pricing"]["num_paths"] = 4000
        r = price(d)
        assert not r["warnings"], (name, r["warnings"])
        (ev,) = [e for e in r["events"].values() if "exercise_probability" in e]
        assert 0.0 < ev["exercise_probability"] < 1.0, name


# ------------------------------------------------------------------ validation and plan
def test_validation():
    assert "trade.termination_events[0].observation" in errors(callable_bond({"observation": "nope"}))
    e = errors(callable_bond({"exercise_price": [100, 100]}))
    assert "4 dates" in e["trade.termination_events[0].exercise_price"]
    assert "trade.termination_events[0].regressors[0]" in errors(callable_bond({"regressors": ["L3M"]}))
    d = callable_bond()
    d["trade"]["termination_events"].append(dict(d["trade"]["termination_events"][0], id="call2"))
    assert "only one" in errors(d)["trade.termination_events[1]"]
    d = callable_bond()
    d["schedules"]["calls"]["end"] = "6Y"
    assert "after maturity" in errors(d)["trade.termination_events[0].observation"]
    d = callable_bond()
    d["schedules"]["calls"]["end"] = "5Y"
    w = [str(i) for i in report(d).warnings]
    assert any("fixed_amount leg also pays on exercise date" in x for x in w)
    d = callable_bond(exercise_fit_paths=1001)
    assert errors(d)


def test_per_date_exercise_price_and_plan():
    # call at 102 in year 1 stepping down to par: the first call is less attractive to the issuer
    stepped = price(callable_bond({"exercise_price": {"valuation_date": 102, "2028-09-01": 100}}, paths=20000))
    flat = price(callable_bond(paths=20000))
    p_step = stepped["events"]["call"]["exercise_probability_by_date"].get("2027-09-28", 0.0)
    assert p_step < flat["events"]["call"]["exercise_probability_by_date"]["2027-09-28"]
    # null removes a date
    r = price(callable_bond({"exercise_price": [None, 100, 100, 100]}, paths=4000))
    assert "2027-09-28" not in r["events"]["call"]["exercise_probability_by_date"]
    plan = build_plan(callable_bond(), EX).summary()
    nodes = {g["node"]: g for g in plan["graph"]}
    assert "exercise_policy" in nodes["price"]["needs"]
    assert "independent paths" in nodes["exercise_policy"]["does"]
    assert "bermudan_call" in catalog("event")["event"]
