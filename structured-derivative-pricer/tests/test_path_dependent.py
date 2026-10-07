"""path_dependent leg and its formula language.

Each test checks the leg against an existing leg on the same paths (exact), against a recursion recomputed by
hand from the fixings the run itself read (exact, pathwise), or against an identity between two ways of giving
the same information (history vs known_state). Run:  python -m pytest tests -q
"""
import copy
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pricer import build_plan, run  # noqa: E402
from pricer.engine import INSPECT_READS  # noqa: E402
from pricer.expressions import Expr, ExprError  # noqa: E402
from pricer.path_dependent import PathDependentLeg  # noqa: E402
from pricer.payoffs import LegResult  # noqa: E402

EX = ROOT / "examples"
VAL = dt.date(2026, 9, 28)
N = 1_000_000
BS = {"type": "black_scholes", "underlying": "AAA", "discount_curve": "USD-FLAT"}
HW = {"type": "hull_white", "discount_curve": "USD-FLAT", "mean_reversion": 0.05, "volatility": 0.012}
L3M = {"type": "term_rate", "curve": "USD-FLAT", "tenor": "3M", "spot_lag": 0, "calendar": "NullCalendar"}
SOFR = {"type": "overnight_rate", "curve": "USD-FLAT", "calendar": "WeekendsOnly"}
Q = {"q": {"type": "generated", "frequency": "3M", "calendar": "NullCalendar"}}


def market():
    m = yaml.safe_load((EX / "market" / "mock_market.yaml").read_text())
    m["equities"]["AAA"] = {"spot": 100.0, "calendar": "NullCalendar", "dividend": {"type": "flat", "rate": 0.01},
                            "vol": {"type": "constant", "value": 0.25}}
    return m


def doc(model, legs, events=(), observables=None, schedules=None, maturity="2028-09-28", **trade):
    return {"valuation_date": VAL.isoformat(), "market_data": market(), "model": model,
            "observables": observables or {}, "schedules": schedules or Q,
            "trade": {"id": "t", "maturity": maturity, "legs": list(legs), "termination_events": list(events),
                      **trade},
            "pricing": {"num_paths": 4000, "seed": 3}}


def price(d):
    plan = build_plan(copy.deepcopy(d), EX)
    assert plan.report.ok, [str(i) for i in plan.report.errors]
    return run(plan)


def inspect(d):
    """(result, every node's output, observable reads) of one run."""
    plan = build_plan(copy.deepcopy(d), EX)
    assert plan.report.ok, [str(i) for i in plan.report.errors]
    keep = {}
    r = run(plan, keep)
    return r, keep["results"], keep["results"][INSPECT_READS]


def errors(d):
    return {i.path: i.message for i in build_plan(copy.deepcopy(d), EX).report.errors}


def pd_leg(**kw):
    leg = {"id": "pd", "type": "path_dependent", "notional": N, "schedule": "q", "day_count": "Actual360"}
    leg.update(kw)
    return leg


def snowball(**kw):
    return pd_leg(inputs={"L": {"observable": "L3M", "fixing": "in_advance", "fixing_lag": 0}},
                  parameters={"K": 0.045}, state={"C": {"initial": 0.0, "evolve": "max(C + K - L, 0)"}},
                  payoff="C * tau", **kw)


# ---------------------------------------------------------------- formulas
def test_formula_language():
    x, y = np.array([1.0, 2.0, 3.0]), np.array([3.0, 2.0, 1.0])
    env = {"x": x, "y": y}
    assert Expr("x ** 2 - 2 * x * y + y ** 2")(env) == pytest.approx((x - y) ** 2)
    assert Expr("1 < x <= 2")(env).tolist() == [0.0, 1.0, 0.0]
    assert Expr("(x >= 2) and not (y < 2)")(env).tolist() == [0.0, 1.0, 0.0]
    assert Expr("where(x > y, x, y) - max(x, y)")(env).tolist() == [0.0, 0.0, 0.0]
    assert Expr("x if x > 1 else -1")(env).tolist() == [-1.0, 2.0, 3.0]
    assert Expr("clip(x, 1.5, 2.5) + abs(-y) + min(x, y, 2)")(env).tolist() == [1.5 + 3 + 1, 2 + 2 + 2, 2.5 + 1 + 1]
    assert Expr("prev(x) + x")(env, prev={"x": 10.0}).tolist() == [11.0, 12.0, 13.0]
    e = Expr("max(x - 1, 0) + where(y > 0, x, 0) * 2")
    assert e.names == {"x", "y"} and {"x"} in e.nonlinear and {"y"} in e.nonlinear


@pytest.mark.parametrize("text, msg", [
    ("__import__('os')", "unknown function"),
    ("x.real", "attribute access"),
    ("x[0]", "indexing"),
    ("lambda: 1", "lambda"),
    ("'a' + x", "numeric constants"),
    ("max(x)", "at least 2"),
    ("clip(x, 0)", "takes 3"),
    ("max(x, y=1)", "positional"),
    ("max", "is a function"),
    ("prev(x + 1)", "state name"),
    ("x +", "cannot parse"),
])
def test_formula_language_rejects(text, msg):
    with pytest.raises(ExprError, match=msg):
        Expr(text)


# ---------------------------------------------------------------- equals existing legs on the same paths
def test_stateless_payoff_equals_fixed_leg():
    fixed = price(doc(BS, [{"id": "f", "type": "fixed_leg", "rate": {"effective_date": 0.03, "1Y": 0.05},
                            "notional": N, "schedule": "q", "day_count": "Actual360"}]))
    pdl = price(doc(BS, [pd_leg(parameters={"r": {"effective_date": 0.03, "1Y": 0.05}}, payoff="r * tau")]))
    assert pdl["price"] == pytest.approx(fixed["price"], rel=1e-12)


def test_rate_input_equals_floating_leg_under_hull_white():
    flt = {"id": "f", "type": "floating_leg", "index": "L3M", "notional": N, "schedule": "q", "fixing_lag": 0,
           "fixing_calendar": "NullCalendar", "gearing": -2.0, "spread": 0.09, "floor": 0.0}
    a = price(doc(HW, [flt], observables={"L3M": L3M}))
    b = price(doc(HW, [pd_leg(inputs={"L": {"observable": "L3M", "fixing": "in_advance", "fixing_lag": 0}},
                              payoff="max(0.09 - 2 * L, 0) * tau")], observables={"L3M": L3M}))
    assert b["price"] == pytest.approx(a["price"], rel=1e-12)


def test_compounded_input_equals_compounded_floating_leg():
    flt = {"id": "f", "type": "floating_leg", "index": "SOFR", "fixing": "compounded_in_arrears", "lockout_days": 2,
           "notional": N, "schedule": "q", "spread": 0.001}
    a = price(doc(HW, [flt], observables={"SOFR": SOFR}))
    b = price(doc(HW, [pd_leg(inputs={"R": {"observable": "SOFR", "fixing": "compounded_in_arrears",
                                            "lockout_days": 2}}, payoff="(R + 0.001) * tau")],
                  observables={"SOFR": SOFR}))
    assert b["price"] == pytest.approx(a["price"], rel=1e-12)


def test_range_input_equals_range_accrual():
    ranges = [{"observable": "AAA", "lower": 90.0, "upper": {"effective_date": 115.0, "1Y": 125.0}}]
    ra = {"id": "ra", "type": "range_accrual", "rate": 0.06, "notional": N, "schedule": "q",
          "day_count": "Actual360", "observation_calendar": "NullCalendar", "range": ranges}
    a = price(doc(BS, [ra], maturity="2027-09-28"))
    b = price(doc(BS, [pd_leg(inputs={"F": {"range": ranges, "observation_calendar": "NullCalendar"}},
                              payoff="0.06 * tau * F")], maturity="2027-09-28"))
    assert b["price"] == pytest.approx(a["price"], rel=1e-12)


# ---------------------------------------------------------------- the recursion, pathwise
def test_snowball_is_the_recursion_on_every_path():
    r, res, reads = inspect(doc(HW, [snowball()], observables={"L3M": L3M}))
    fix = sorted(reads["L3M"])
    L = np.vstack([np.broadcast_to(reads["L3M"][d], (4000,)) for d in fix])
    amounts = res["leg:pd"].amounts
    assert len(fix) == len(amounts) == 8
    c = np.zeros(4000)
    taus = [(e - s).days / 360 for s, e in zip([VAL] + list(amounts.index[:-1]), amounts.index)]
    for k in range(8):
        c = np.maximum(c + 0.045 - L[k], 0.0)
        assert amounts.iloc[k].values == pytest.approx(N * c * taus[k], rel=1e-12, abs=1e-9)
        assert res["leg:pd"].states["C"].iloc[k].values == pytest.approx(c, rel=1e-12, abs=1e-12)
    # the floor binds on some paths, so this is not the linear sum
    assert (np.cumsum(0.045 - L, axis=0) < 0).any()


def test_memory_coupon_pays_missed_coupons_on_the_next_hit():
    obsv = {"PERF": {"type": "equity", "underlying": "AAA", "mode": "performance"}}
    leg = pd_leg(inputs={"X": {"observable": "PERF", "fixing": "in_arrears"}}, parameters={"c": 0.02, "B": 1.0},
                 define={"hit": "X >= B"}, state={"U": {"initial": 0.0, "evolve": "where(hit, 0, U + c)"}},
                 payoff="hit * (prev(U) + c)")
    r, res, reads = inspect(doc(BS, [leg], observables=obsv, maturity="2027-09-28"))
    X = np.vstack([reads["PERF"][d] for d in sorted(reads["PERF"])])
    amounts = res["leg:pd"].amounts.values
    u = np.zeros(4000)
    for k in range(4):
        hit = X[k] >= 1.0
        assert amounts[k] == pytest.approx(N * np.where(hit, u + 0.02, 0.0), abs=1e-9)
        u = np.where(hit, 0.0, u + 0.02)
    # total paid on a path = 2% x (index of the last hit + 1): every coupon up to the last hit is paid once
    last = np.where((X >= 1.0).any(axis=0), 3 - np.argmax((X >= 1.0)[::-1], axis=0), -1)
    assert amounts.sum(axis=0) == pytest.approx(N * 0.02 * (last + 1), abs=1e-9)


def test_cumulative_cap_on_a_leg_through_prev():
    """A = capped cumulative coupon; payoff = A - prev(A): the coupons sum to min(sum of raw coupons, cap)."""
    obsv = {"PERF": {"type": "equity", "underlying": "AAA", "mode": "performance"}}
    leg = pd_leg(inputs={"X": {"observable": "PERF", "fixing": "in_arrears"}},
                 state={"A": {"initial": 0.0, "evolve": "min(A + max(X - 1, 0), 0.15)"}}, payoff="A - prev(A)")
    r, res, reads = inspect(doc(BS, [leg], observables=obsv, maturity="2027-09-28"))
    X = np.vstack([reads["PERF"][d] for d in sorted(reads["PERF"])])
    total = res["leg:pd"].amounts.values.sum(axis=0)
    assert total == pytest.approx(N * np.minimum(np.maximum(X - 1, 0).sum(axis=0), 0.15), abs=1e-6)


# ---------------------------------------------------------------- seasoned: history vs known_state
def test_seasoned_snowball_from_history_equals_known_state():
    start = dt.date(2026, 3, 30)        # two periods paid (2026-06-30, 2026-09-28 ... the second on VAL)
    hist = {"2026-03-30": 0.050, "2026-06-30": 0.030, "2026-09-28": 0.041}
    obsv = {"L3M": dict(L3M, history=hist)}
    sched = {"q": {"type": "dates", "dates": ["2026-06-30", "2026-09-28", "2026-12-28", "2027-03-29",
                                              "2027-06-28"]}}
    d = doc(HW, [snowball()], observables=obsv, schedules=sched, maturity="2027-06-28",
            effective_date=start.isoformat())
    a = price(d)
    c1 = max(0.045 - 0.050, 0.0)
    c2 = max(c1 + 0.045 - 0.030, 0.0)
    known = copy.deepcopy(d)
    known["trade"]["legs"][0]["known_state"] = {"period_end": "2026-09-28", "values": {"C": c2}}
    known["observables"]["L3M"]["history"] = {"2026-09-28": 0.041}    # only the next period's fixing
    b = price(known)
    assert b["price"] == pytest.approx(a["price"], rel=1e-12)
    # without the early fixings, the history check names them
    d["observables"]["L3M"]["history"] = {"2026-09-28": 0.041}
    assert "needs 2 past fixing" in errors(d)["trade.legs[0].inputs.L.observable"]


def test_known_state_must_be_a_paid_period_end_with_every_state():
    d = doc(HW, [snowball(known_state={"period_end": "2026-12-28", "values": {"C": 0.01}})],
            observables={"L3M": L3M})
    assert "after the valuation date" in errors(d)["trade.legs[0].known_state.period_end"]
    d["trade"]["legs"][0]["known_state"]["period_end"] = "2026-11-28"
    assert "not an accrual end" in errors(d)["trade.legs[0].known_state.period_end"]


# ---------------------------------------------------------------- with trade events
def test_target_event_accumulates_a_path_dependent_leg():
    events = [{"id": "tarn", "type": "target", "legs": ["pd"], "target": 30000, "rebate": N}]
    r = price(doc(HW, [snowball(), {"id": "p", "type": "fixed_amount", "amount": N}], events,
                  observables={"L3M": L3M}))
    assert 0 < r["events"]["tarn"]["hit_probability"] < 1


def test_state_regressor_uses_only_states_known_by_the_exercise_date():
    idx = [dt.date(2027, 1, 1), dt.date(2027, 4, 1)]
    lr = LegResult(pd.DataFrame(), states={"C": pd.DataFrame([[1.0, 2.0], [3.0, 4.0]], index=idx)},
                   initial_states={"C": 0.5})
    got = PathDependentLeg.state_on(lr, "C", [dt.date(2026, 12, 31), dt.date(2027, 1, 1), dt.date(2027, 3, 31),
                                              dt.date(2027, 6, 1)])
    assert got.tolist() == [[0.5, 0.5], [1.0, 2.0], [1.0, 2.0], [3.0, 4.0]]


def test_callable_snowball_regresses_on_the_coupon_state():
    d = yaml.safe_load((EX / "callable_snowball.yaml").read_text())
    d["pricing"]["num_paths"] = 4000
    plan = build_plan(copy.deepcopy(d), EX)
    assert plan.report.ok and "leg:coupons" in plan.nodes["event:issuer_call"].deps
    d["trade"]["termination_events"][0]["regressors"] = ["L3M", "coupons.X"]
    assert "has no state 'X'" in errors(d)["trade.termination_events[0].regressors[1]"]


# ---------------------------------------------------------------- validation
def test_nonlinearity_on_deterministic_inputs_is_rejected_but_linear_use_is_fine():
    d = doc(BS, [snowball()], observables={"L3M": L3M})
    assert "deterministic under model" in errors(d)["trade.legs[0].state.C.evolve"]
    d["trade"]["legs"][0]["state"]["C"]["evolve"] = "C + K - L"
    assert not errors(d)
    # a nonlinearity that also reads a stochastic input is an option, not intrinsic value
    d["trade"]["legs"][0]["inputs"]["S"] = {"observable": "AAA", "fixing": "in_arrears"}
    d["trade"]["legs"][0]["state"]["C"]["evolve"] = "max(C + K - L, S / 100 - 1)"
    assert not errors(d)


@pytest.mark.parametrize("change, path, msg", [
    ({"payoff": "C * tau * Z"}, "payoff", "unknown name(s) ['Z']"),
    ({"state": {"C": {"initial": 0, "evolve": "prev(C) + 1"}}}, "state.C.evolve", "only for the payoff"),
    ({"payoff": "prev(K)"}, "payoff", "prev() takes a state name"),
    ({"define": {"a": "b", "b": "1"}}, "define.a", "only the defines before it"),
    ({"payoff": "exec(C)"}, "payoff", "unknown function"),
    ({"inputs": {"L": {"observable": "L3M"}}}, "inputs.L", "give `fixing`"),
    ({"inputs": {"L": {"observable": "L3M", "fixing": "in_advance", "observation_calendar": "TARGET"}}},
     "inputs.L", "apply to a `range` input"),
    ({"parameters": {"tau": 1.0}}, "", "reserved"),
    ({"parameters": {"C": 1.0}}, "", "both"),
    ({"inputs": {"L": {"observable": "L3M", "fixing": "compounded_in_arrears"}}}, "inputs.L.fixing",
     "needs an overnight_rate"),
])
def test_spec_errors(change, path, msg):
    leg = snowball()
    leg.update(change)
    errs = errors(doc(HW, [leg], observables={"L3M": L3M}))
    key = "trade.legs[0]" + (f".{path}" if path else "")
    assert any(k.startswith(key) and msg in m for k, m in errs.items()), errs


def test_non_finite_payoff_is_an_error_not_a_price():
    leg = dict(snowball(), payoff="C / (C - C)")
    plan = build_plan(doc(HW, [leg], observables={"L3M": L3M}), EX)
    with pytest.raises(FloatingPointError, match="trade.legs\\[0\\].payoff: non-finite"):
        run(plan)


# ---------------------------------------------------------------- review regressions (PR #10)
def test_dotted_underlying_is_a_regressor_not_a_leg_state():
    """One classifier: a ticker such as BRK.B is read as the underlying even when a leg 'BRK' exists, and
    observable names may contain dots."""
    m = market()
    m["equities"]["BRK.B"] = dict(m["equities"]["AAA"])
    model = {"type": "black_scholes", "underlying": "BRK.B", "discount_curve": "USD-FLAT"}
    obsv = {"PERF.B": {"type": "equity", "underlying": "BRK.B", "mode": "performance"}}
    leg = pd_leg(id="BRK", inputs={"X": {"observable": "PERF.B", "fixing": "in_arrears"}},
                 state={"B": {"initial": 0.0, "evolve": "B + X"}}, payoff="0.01 * tau")
    call = {"id": "call", "type": "bermudan_call", "observation": "q", "exerciser": "issuer", "exercise_price": 0,
            "regressors": ["BRK.B", "PERF.B"]}
    d = doc(model, [leg], [call], observables=obsv, maturity="2027-09-28")
    d["market_data"] = m
    assert "is both a model underlying or observable and the state 'B'" in \
        errors(d)["trade.termination_events[0].regressors[0]"]
    d["trade"]["legs"][0]["state"] = {"S": {"initial": 0.0, "evolve": "S + X"}}
    plan = build_plan(copy.deepcopy(d), EX)
    assert plan.report.ok, [str(i) for i in plan.report.errors]
    assert "leg:BRK" not in plan.nodes["event:call"].deps
    assert run(plan)["price"] > 0


def test_known_state_keeps_absolute_period_index():
    """is_first / period count from the first period of the schedule, also when evolution starts at known_state."""
    sched = {"q": {"type": "dates", "dates": ["2026-06-30", "2026-09-28", "2026-12-28", "2027-03-29"]}}
    leg = pd_leg(state={"A": {"initial": 0.0, "evolve": "A + 1"}}, payoff="(period + 10 * is_first) * 0.001",
                 known_state={"period_end": "2026-09-28", "values": {"A": 2.0}})
    r = price(doc(BS, [leg], schedules=sched, maturity="2027-03-29", effective_date="2026-03-30"))
    amounts = [c["expected_amount"] for c in r["expected_cashflows"]]
    assert amounts == pytest.approx([N * 0.002, N * 0.003], rel=1e-12)


def test_non_finite_state_names_the_define_that_caused_it():
    leg = dict(snowball(), define={"d": "log(L - 1)"}, state={"C": {"initial": 0.0, "evolve": "C + d"}})
    plan = build_plan(doc(HW, [leg], observables={"L3M": L3M}), EX)
    with pytest.raises(FloatingPointError, match=r"state\.C\.evolve.*\['define\.d'\]"):
        run(plan)
    # a non-finite define masked by where() is fine
    leg["state"]["C"]["evolve"] = "C + where(L > 1, d, 0)"
    assert price(doc(HW, [leg], observables={"L3M": L3M}))["price"] == 0


def test_formula_length_and_nesting_are_bounded():
    with pytest.raises(ExprError, match="at most"):
        Expr("x + " * 600 + "x")
    with pytest.raises(ExprError, match="nested too deeply|cannot parse"):
        Expr("(" * 900 + "x" + ")" * 900)
