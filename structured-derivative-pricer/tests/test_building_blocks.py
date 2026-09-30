"""Regression tests for the payoff building blocks: observables, fixed / floating / range-accrual legs,
masks (kick-in, knock-out, target), multi-asset Black-Scholes and Hull-White.

Each block is checked against an independent closed form, a QuantLib engine, or an exact pathwise
identity. Run:  python -m pytest tests -q
"""
import copy
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import pytest
import QuantLib as ql
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pricer import build_plan, run  # noqa: E402

EX = ROOT / "examples"
VAL = dt.date(2026, 9, 28)
N = 1_000_000
Phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))  # noqa: E731


def market():
    m = yaml.safe_load((EX / "market" / "mock_market.yaml").read_text())
    for name, spot, q in (("AAA", 100.0, 0.01), ("BBB", 50.0, 0.02)):
        m["equities"][name] = {"spot": spot, "calendar": "NullCalendar", "dividend": {"type": "flat", "rate": q},
                               "vol": {"type": "constant", "value": 0.25}}
    return m


def doc(model, legs, events=(), observables=None, schedules=None, paths=20000, maturity="2027-09-28", **pricing):
    return {"valuation_date": VAL.isoformat(), "market_data": market(), "model": model,
            "observables": observables or {},
            "schedules": schedules or {"q": {"type": "generated", "frequency": "3M", "calendar": "NullCalendar"}},
            "trade": {"id": "t", "maturity": maturity, "legs": list(legs), "termination_events": list(events)},
            "pricing": {"num_paths": paths, "seed": 11, **pricing}}


def price(d):
    plan = build_plan(copy.deepcopy(d), EX)
    assert plan.report.ok, [str(i) for i in plan.report.errors]
    return run(plan)


def errors(d):
    return {i.path: i.message for i in build_plan(copy.deepcopy(d), EX).report.errors}


def within(r, target, k=4.0, abs_tol=0.0):
    return abs(r["price"] - target) <= k * r["std_error"] + abs_tol


BS_FLAT = {"type": "black_scholes", "underlying": "SPX-FLATVOL", "discount_curve": "USD-FLAT"}
HW = {"type": "hull_white", "discount_curve": "USD-FLAT", "mean_reversion": 0.05, "volatility": 0.012}
L3M = {"type": "term_rate", "curve": "USD-FLAT", "tenor": "3M", "spot_lag": 0, "calendar": "NullCalendar"}
SOFR = {"type": "overnight_rate", "curve": "USD", "calendar": "WeekendsOnly"}


def floater(**kw):
    leg = {"id": "flt", "type": "floating_leg", "index": "L3M", "notional": N, "schedule": "q",
           "fixing_lag": 0, "fixing_calendar": "NullCalendar"}
    leg.update(kw)
    return leg


PRINCIPAL = {"id": "principal", "type": "fixed_amount", "amount": N}


def df(t_date, r=0.04):
    return math.exp(-r * (t_date - VAL).days / 365.0)


# ---------------------------------------------------------------- floating legs
def test_par_floater_is_exact_under_deterministic_rates():
    """Index fixed in advance over each period + par at maturity = par, with zero MC noise."""
    r = price(doc(BS_FLAT, [floater(), PRINCIPAL], observables={"L3M": L3M}))
    assert r["std_error"] == pytest.approx(0, abs=1e-9)
    assert r["price"] == pytest.approx(N, rel=1e-12)


def test_par_floater_under_hull_white():
    r = price(doc(HW, [floater(), PRINCIPAL], observables={"L3M": L3M}, paths=40000))
    assert within(r, N) and r["martingale_check"]["discount_factor"]["ok"]


def _ql_cap(strike, a, sigma, schedule_dates):
    ql.Settings.instance().evaluationDate = ql.Date(28, 9, 2026)
    h = ql.YieldTermStructureHandle(ql.FlatForward(ql.Date(28, 9, 2026), 0.04, ql.Actual365Fixed(), ql.Continuous))
    idx = ql.IborIndex("L3M", ql.Period("3M"), 0, ql.USDCurrency(), ql.NullCalendar(), ql.ModifiedFollowing,
                       False, ql.Actual360(), h)
    sch = ql.Schedule(schedule_dates, ql.NullCalendar(), ql.Unadjusted)
    leg = ql.IborLeg([N], sch, idx, ql.Actual360(), ql.Unadjusted, [0])
    cap = ql.Cap(leg, [strike])
    cap.setPricingEngine(ql.AnalyticCapFloorEngine(ql.HullWhite(h, a, sigma), h))
    return cap.NPV()


def test_capped_floater_matches_quantlib_hull_white_cap():
    """floater capped at K = floater - cap(K); QuantLib's analytic Hull-White cap is the reference."""
    k = 0.045
    r = price(doc(HW, [floater(cap=k), PRINCIPAL], observables={"L3M": L3M}, paths=60000))
    dates = [ql.Date(28, 9, 2026), ql.Date(28, 12, 2026), ql.Date(28, 3, 2027), ql.Date(28, 6, 2027),
             ql.Date(28, 9, 2027)]
    target = N - _ql_cap(k, HW["mean_reversion"], HW["volatility"], dates)
    assert r["leg_diagnostics"]["flt"]["cap_binding_probability"] > 0.1
    assert within(r, target), (r["price"], target, r["std_error"])


def test_inverse_floater_equals_fixed_minus_capped_floater_pathwise():
    """max(K - L, 0) = K - min(L, K): same seed => identical price to rounding."""
    k = 0.05
    obsv = {"L3M": L3M}
    inv = price(doc(HW, [floater(gearing=-1.0, spread=k, floor=0.0)], observables=obsv))
    fixed = {"id": "fix", "type": "fixed_leg", "rate": k, "notional": N, "schedule": "q", "day_count": "Actual360"}
    cap = price(doc(HW, [fixed, floater(cap=k, position="short")], observables=obsv))
    assert inv["price"] == pytest.approx(cap["price"], rel=1e-10)


def _ql_ois_amount(start, end, lockout):
    from pricer.market import BootstrapCurve
    ql.Settings.instance().evaluationDate = ql.Date(28, 9, 2026)
    h = BootstrapCurve.build(BootstrapCurve.Spec.model_validate(
        {k: v for k, v in market()["curves"]["USD"].items() if k != "type"}), ql.Date(28, 9, 2026))
    idx = ql.OvernightIndex("SOFR", 0, ql.USDCurrency(), ql.WeekendsOnly(), ql.Actual360(), h)
    s, e = (ql.Date(d.day, d.month, d.year) for d in (start, end))
    c = ql.OvernightIndexedCoupon(e, N, s, e, idx, 1.0, 0.0, ql.Date(), ql.Date(), ql.Actual360(), False,
                                  ql.RateAveraging.Compound, ql.nullInt(), lockout)
    return c.amount()


@pytest.mark.parametrize("lockout", [0, 2])
def test_compounded_in_arrears_matches_quantlib_overnight_coupon(lockout):
    leg = {"id": "ois", "type": "floating_leg", "index": "SOFR", "fixing": "compounded_in_arrears",
           "lockout_days": lockout, "notional": N, "schedule": "one"}
    d = doc(BS_FLAT, [leg], observables={"SOFR": SOFR},
            schedules={"one": {"type": "dates", "dates": ["2026-12-28"]}}, maturity="2026-12-28")
    r = price(d)
    amount = r["expected_cashflows"][0]["expected_amount"]
    assert amount == pytest.approx(_ql_ois_amount(VAL, dt.date(2026, 12, 28), lockout), rel=1e-9)


def test_compounded_in_arrears_with_principal_is_par_pathwise_under_hull_white():
    """D(0,e) * growth(s,e) = D(0,s) on every path, so coupons + par redemption = par exactly."""
    sofr = dict(SOFR, curve="USD-FLAT")
    leg = {"id": "ois", "type": "floating_leg", "index": "SOFR", "fixing": "compounded_in_arrears",
           "notional": N, "schedule": "q"}
    r = price(doc(HW, [leg, PRINCIPAL], observables={"SOFR": sofr}))
    assert r["price"] == pytest.approx(N, rel=1e-10) and r["std_error"] < 1e-4


def test_past_fixing_needs_history_and_uses_it():
    leg = floater(fixing_lag=2, fixing_calendar="WeekendsOnly")
    d = doc(BS_FLAT, [leg], observables={"L3M": L3M})
    assert "needs 1 past fixing" in errors(d)["trade.legs[0].index"]
    d["observables"]["L3M"] = dict(L3M, history={"2026-09-24": 0.05})
    r = price(d)
    first = r["expected_cashflows"][0]
    assert first["expected_amount"] == pytest.approx(N * 0.05 * 91 / 360, abs=1e-6)


# ---------------------------------------------------------------- range accrual
def _ra(ranges, **kw):
    leg = {"id": "ra", "type": "range_accrual", "rate": 0.06, "notional": N, "schedule": "q",
           "day_count": "Actual360", "range": ranges}
    leg.update(kw)
    return leg


def test_range_accrual_with_unbreakable_range_equals_fixed_leg():
    fixed = price(doc(BS_FLAT, [{"id": "f", "type": "fixed_leg", "rate": 0.06, "notional": N, "schedule": "q",
                                 "day_count": "Actual360"}]))
    ra = price(doc(BS_FLAT, [_ra([{"observable": "SPX-FLATVOL", "lower": 0.0}])]))
    assert ra["price"] == pytest.approx(fixed["price"], rel=1e-12)


def _bs_prob_in(lo, hi, t, s0=1.0, r=0.04, q=0.01, vol=0.2):
    def below(k):
        d2 = (math.log(s0 / k) + (r - q - 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
        return Phi(-d2)
    return below(hi) - below(lo)


def test_equity_range_accrual_matches_digital_closed_form():
    obsv = {"perf": {"type": "equity", "underlying": "SPX-FLATVOL", "mode": "performance"}}
    sched = {"one": {"type": "dates", "dates": ["2027-03-29"]}}
    r = price(doc(BS_FLAT, [_ra([{"observable": "perf", "lower": 0.95, "upper": 1.08}], schedule="one",
                                observation_calendar="NullCalendar")],
                  observables=obsv, schedules=sched, maturity="2027-03-29", steps_per_year=0))
    end = dt.date(2027, 3, 29)
    days = [VAL + dt.timedelta(i) for i in range((end - VAL).days)]
    probs = [1.0] + [_bs_prob_in(0.95, 1.08, (d - VAL).days / 365.0) for d in days[1:]]
    target = N * 0.06 * (end - VAL).days / 360 * np.mean(probs) * df(end)
    assert within(r, target), (r["price"], target, r["std_error"])


MULTI = {"type": "black_scholes", "underlyings": ["AAA", "BBB"], "correlation": [[1, 0], [0, 1]],
         "discount_curve": "USD-FLAT"}
PERF = {"pa": {"type": "equity", "underlying": "AAA", "mode": "performance"},
        "pb": {"type": "equity", "underlying": "BBB", "mode": "performance"}}


def test_all_in_range_of_independent_assets_is_product_of_digitals():
    sched = {"one": {"type": "dates", "dates": ["2027-03-29"]}}
    rng = [{"observable": "pa", "lower": 0.9, "upper": 1.1}, {"observable": "pb", "lower": 0.9, "upper": 1.1}]
    r = price(doc(MULTI, [_ra(rng, schedule="one", observation_calendar="NullCalendar")], observables=PERF,
                  schedules=sched, maturity="2027-03-29", steps_per_year=0))
    end = dt.date(2027, 3, 29)
    days = [VAL + dt.timedelta(i) for i in range((end - VAL).days)]
    probs = [1.0] + [_bs_prob_in(0.9, 1.1, t, q=0.01, vol=0.25) * _bs_prob_in(0.9, 1.1, t, q=0.02, vol=0.25)
                     for t in ((d - VAL).days / 365.0 for d in days[1:])]
    target = N * 0.06 * (end - VAL).days / 360 * np.mean(probs) * df(end)
    assert within(r, target), (r["price"], target, r["std_error"])


def test_spread_range_of_perfectly_correlated_twins_always_accrues():
    m = dict(MULTI, underlyings=["AAA", "AAA2"], correlation=[[1, 1], [1, 1]])
    d = doc(m, [_ra([{"observable": "spread", "lower": -1e-9, "upper": 1e-9}])],
            observables={"pa": PERF["pa"], "pa2": {"type": "equity", "underlying": "AAA2", "mode": "performance"},
                         "spread": {"type": "linear", "terms": [{"observable": "pa", "weight": 1},
                                                                {"observable": "pa2", "weight": -1}]}})
    d["market_data"]["equities"]["AAA2"] = copy.deepcopy(d["market_data"]["equities"]["AAA"])
    r = price(d)
    assert r["leg_diagnostics"]["ra"]["mean_accrual_fraction_by_period"] == [1.0] * 4


def _simulate(d):
    """Run the plan's graph up to the simulated Scenario."""
    plan = build_plan(copy.deepcopy(d), EX)
    assert plan.report.ok, [str(i) for i in plan.report.errors]
    r = {}
    for key, node in plan.nodes.items():
        r[key] = node.fn(r)
        if key == "fixings":
            return r[key]


def test_correlated_paths_have_the_requested_correlation():
    sc = _simulate(doc(dict(MULTI, correlation=[[1, 0.6], [0.6, 1]]),
                       [_ra([{"observable": "pa", "lower": 0}]), _ra([{"observable": "pb", "lower": 0}], id="rb")],
                       observables=PERF, paths=20000))
    a, b = (np.diff(np.log(sc.equity[u].values), axis=0).ravel() for u in ("AAA", "BBB"))
    assert np.corrcoef(a, b)[0, 1] == pytest.approx(0.6, abs=0.01)


def test_rate_range_accrual_matches_hull_white_forward_measure_closed_form():
    """P(lo <= L(t) <= hi) under the payment-date forward measure, where x(t) is Gaussian."""
    a, sig, lo, hi = HW["mean_reversion"], HW["volatility"], 0.035, 0.05
    sched = {"one": {"type": "dates", "dates": ["2027-03-29"]}}
    r = price(doc(HW, [_ra([{"observable": "L3M", "lower": lo, "upper": hi}], schedule="one",
                           observation_calendar="NullCalendar")],
                  observables={"L3M": L3M}, schedules=sched, maturity="2027-03-29", paths=20000))
    end = dt.date(2027, 3, 29)
    big_t = (end - VAL).days / 365.0
    V = lambda u: sig ** 2 / a ** 2 * (u + 2 / a * math.exp(-a * u) - 0.5 / a * math.exp(-2 * a * u) - 1.5 / a)  # noqa: E731
    B = lambda u: (1 - math.exp(-a * u)) / a  # noqa: E731
    probs = []
    for i in range((end - VAL).days):
        d = VAL + dt.timedelta(i)
        ql_d = ql.Date(d.day, d.month, d.year)
        s, e = ql_d, ql.NullCalendar().advance(ql_d, ql.Period("3M"), ql.ModifiedFollowing)
        t, ts, te = i / 365.0, (s - ql.Date(28, 9, 2026)) / 365.0, (e - ql.Date(28, 9, 2026)) / 365.0
        tau = (e - s) / 360.0
        c = math.exp(-0.04 * (ts - te)) * math.exp(0.5 * (V(ts - t) - V(ts) - V(te - t) + V(te)))
        db = B(te - t) - B(ts - t)
        x_of = lambda rate: math.log((1 + tau * rate) / c) / db  # noqa: E731
        if i == 0:
            probs.append(float(lo <= (c - 1) / tau <= hi))
            continue
        m = -sig ** 2 / a ** 2 * (1 - math.exp(-a * t)) + sig ** 2 / (2 * a * a) * (
            math.exp(-a * (big_t - t)) - math.exp(-a * (big_t + t)))
        sd = math.sqrt(sig ** 2 * (1 - math.exp(-2 * a * t)) / (2 * a))
        probs.append(Phi((x_of(hi) - m) / sd) - Phi((x_of(lo) - m) / sd))
    target = N * 0.06 * (end - VAL).days / 360 * np.mean(probs) * df(end)
    assert within(r, target), (r["price"], target, r["std_error"])


def test_calendar_day_basis_weights_weekends_with_friday():
    leg = _ra([{"observable": "SPX-FLATVOL", "lower": 0.0}], day_basis="calendar")
    plan = build_plan(doc(BS_FLAT, [leg]), EX)
    from pricer.coupons import Period, RangeAccrual
    fix, w = RangeAccrual.observations(plan.trade.legs[0][1],
                                       Period(dt.date(2026, 10, 2), dt.date(2026, 10, 9), None, 0.0))
    assert fix[0] == dt.date(2026, 10, 2) and list(w) == [3, 1, 1, 1, 1]      # Fri carries Sat and Sun


# ---------------------------------------------------------------- masks
def _fixed(rate=0.08, **kw):
    leg = {"id": "cpn", "type": "fixed_leg", "rate": rate, "notional": N, "schedule": "q"}
    leg.update(kw)
    return leg


QDATES = [dt.date(2026, 12, 28), dt.date(2027, 3, 28), dt.date(2027, 6, 28), dt.date(2027, 9, 28)]


def test_target_redemption_caps_crossing_coupon_and_redeems():
    """8% p.a. quarterly on 30/360 = 20,000 per quarter; target 50,000 hits on the 3rd date, paying 10,000."""
    ev = {"id": "tarn", "type": "target", "legs": ["cpn"], "target": 50000, "rebate": N}
    r = price(doc(BS_FLAT, [_fixed(), PRINCIPAL], [ev]))
    expected = 20000 * df(QDATES[0]) + 20000 * df(QDATES[1]) + (10000 + N) * df(QDATES[2])
    assert r["price"] == pytest.approx(expected, rel=1e-9)
    assert r["events"]["tarn"]["hit_probability"] == 1.0


def test_target_guarantee_pays_shortfall_at_maturity():
    ev = {"id": "tarn", "type": "target", "legs": ["cpn"], "target": 100000, "guaranteed": True}
    r = price(doc(BS_FLAT, [_fixed()], [ev]))
    expected = 20000 * sum(df(d) for d in QDATES) + 20000 * df(QDATES[-1])
    assert r["price"] == pytest.approx(expected, rel=1e-9)


def test_kick_in_coupons_match_digital_probability():
    """Every coupon is paid iff SPX <= 95% on the first coupon date (its cut-off)."""
    cond = {"type": "knock_in", "underlying": "SPX-FLATVOL", "direction": "down", "barrier": 0.95,
            "observation": "first"}
    sched = {"q": {"type": "generated", "frequency": "3M", "calendar": "NullCalendar"},
             "first": {"type": "dates", "dates": ["2026-12-28"]}}
    r = price(doc(BS_FLAT, [_fixed(conditions=[cond])], schedules=sched, paths=40000))
    t1 = (QDATES[0] - VAL).days / 365.0
    prob = 1 - _bs_prob_in(0.95, 1e9, t1)
    assert within(r, prob * 20000 * sum(df(d) for d in QDATES))


def test_kick_in_plus_knock_out_on_same_barrier_is_the_plain_leg():
    sched = {"q": {"type": "generated", "frequency": "3M", "calendar": "NullCalendar"},
             "daily": {"type": "generated", "frequency": "1D", "calendar": "WeekendsOnly"}}
    cond = {"underlying": "SPX-FLATVOL", "direction": "up", "barrier": 1.05, "observation": "daily"}
    legs = [_fixed(id="a", conditions=[dict(cond, type="knock_in")]),
            _fixed(id="b", conditions=[dict(cond, type="knock_out")])]
    r = price(doc(BS_FLAT, legs, schedules=sched))
    assert r["price"] == pytest.approx(20000 * sum(df(d) for d in QDATES), rel=1e-12)
    assert 0 < r["legs"]["a"] < r["legs"]["b"] + r["legs"]["a"]


def test_knock_out_event_on_worst_of_observable():
    obsv = dict(PERF, worst={"type": "min_of", "observables": ["pa", "pb"]})
    ev = {"id": "ko", "type": "knock_out", "observable": "worst", "direction": "down", "barrier": 0.7,
          "observation": "q"}
    r = price(doc(MULTI, [_fixed(), PRINCIPAL], [ev], observables=obsv))
    p_ko = r["events"]["ko"]["hit_probability"]
    assert 0 < p_ko < 0.5 and r["price"] < 20000 * sum(df(d) for d in QDATES) + N * df(QDATES[-1])


def test_worst_of_put_on_observable():
    obsv = dict(PERF, worst={"type": "min_of", "observables": ["pa", "pb"]})
    leg = {"id": "p", "type": "option_payoff", "observable": "worst", "option_type": "put", "strike": 1.0,
           "notional": N}
    r = price(doc(MULTI, [leg], observables=obsv))
    single = price(doc(MULTI, [dict(leg, observable="pa")], observables=obsv))
    assert r["method"] == "mc" and r["price"] > single["price"] > 0


# ---------------------------------------------------------------- validation
def test_cap_on_deterministic_rate_is_rejected():
    e = errors(doc(BS_FLAT, [floater(cap=0.05)], observables={"L3M": L3M}))
    assert "deterministic under model" in e["trade.legs[0].index"]


def test_range_on_deterministic_rate_is_rejected():
    e = errors(doc(BS_FLAT, [_ra([{"observable": "L3M", "upper": 0.05}])], observables={"L3M": L3M}))
    assert "deterministic" in e["trade.legs[0].range[0].observable"]


def test_unknown_observable_and_cycle_and_bad_compounding_index():
    obsv = {"L3M": L3M, "a": {"type": "linear", "terms": [{"observable": "b", "weight": 1}]},
            "b": {"type": "linear", "terms": [{"observable": "a", "weight": 1}]}}
    legs = [floater(index="L3N"), floater(id="c", fixing="compounded_in_arrears")]
    e = errors(doc(HW, legs, observables=obsv))
    assert "Did you mean 'L3M'" in e["trade.legs[0].index"]
    assert any("circular" in m for m in e.values())
    assert "overnight_rate" in e["trade.legs[1].index"]


def test_multi_asset_needs_explicit_correlation():
    m = dict(MULTI)
    del m["correlation"]
    assert any("correlation" in msg for msg in errors(doc(m, [_fixed()])).values())


def test_target_references_known_legs_and_fixed_rate_rejects_float_fields():
    ev = {"id": "tarn", "type": "target", "legs": ["cpm"], "target": 1}
    e = errors(doc(BS_FLAT, [_fixed(), _ra([{"observable": "SPX-FLATVOL", "lower": 0}], id="r2", gearing=2)], [ev]))
    assert "Did you mean 'cpn'" in e["trade.termination_events[0].legs[0]"]
    assert "trade.legs[1]" in e


def test_examples_all_validate():
    for f in EX.glob("*.yaml"):
        d, = [yaml.safe_load(f.read_text())]
        plan = build_plan(d, EX)
        assert plan.report.ok, (f.name, [str(i) for i in plan.report.errors])


# ---------------------------------------------------------------- per-period values, hybrids
Q8 = QDATES + [dt.date(2027, 12, 28), dt.date(2028, 3, 28), dt.date(2028, 6, 28), dt.date(2028, 9, 28)]


def test_step_up_rate_and_amortising_notional_are_exact():
    """{date: value} steps key on period starts; a list gives one value per period."""
    leg = _fixed(rate={"effective_date": 0.03, "1Y": 0.05}, notional=[N] * 4 + [N / 2] * 4)
    r = price(doc(BS_FLAT, [leg], maturity="2Y"))
    expected = sum((0.03 * N if i < 4 else 0.05 * N / 2) * 0.25 * df(d) for i, d in enumerate(Q8))
    assert r["price"] == pytest.approx(expected, rel=1e-12)


def test_hybrid_fixed_then_range_accrual_in_one_leg():
    """Year 1: null bounds = no range, so it accrues every day (a fixed coupon); years 2+: a real range."""
    obsv = {"perf": {"type": "equity", "underlying": "SPX-FLATVOL", "mode": "performance"}}
    rng = [{"observable": "perf", "lower": {"effective_date": None, "1Y": 0.9},
            "upper": {"effective_date": None, "1Y": 1.1}}]
    r = price(doc(BS_FLAT, [_ra(rng, day_count="Thirty360.BondBasis")], observables=obsv, maturity="2Y"))
    frac = r["leg_diagnostics"]["ra"]["mean_accrual_fraction_by_period"]
    assert frac[:4] == [1.0] * 4 and all(0 < f < 1 for f in frac[4:])
    for c, d in zip(r["expected_cashflows"][:4], QDATES):
        assert c["expected_amount"] == pytest.approx(N * 0.06 * 0.25, abs=1e-6)


def test_range_bounds_per_period_as_list():
    obsv = {"perf": {"type": "equity", "underlying": "SPX-FLATVOL", "mode": "performance"}}
    rng = [{"observable": "perf", "lower": [0.0, 10.0, 0.0, 10.0], "upper": [None, 11.0, None, 11.0]}]
    r = price(doc(BS_FLAT, [_ra(rng)], observables=obsv))
    assert r["leg_diagnostics"]["ra"]["mean_accrual_fraction_by_period"] == [1.0, 0.0, 1.0, 0.0]


CMS5Y = {"type": "swap_rate", "curve": "USD-FLAT", "tenor": "5Y", "fixed_frequency": "6M", "calendar": "NullCalendar"}


def test_cms_inverse_floater_in_arrears_formula_forms_agree_and_respect_collar():
    """5% - 0.8 x CMS5Y fixed in arrears, collared 2%..4%: gearing/spread form == linear-index form pathwise."""
    obsv = {"CMS5Y": CMS5Y, "INV": {"type": "linear", "constant": 0.05,
                                    "terms": [{"observable": "CMS5Y", "weight": -0.8}]}}
    a = floater(index="CMS5Y", gearing=-0.8, spread=0.05, cap=0.04, floor=0.02, fixing="in_arrears")
    b = floater(id="b", index="INV", cap=0.04, floor=0.02, fixing="in_arrears", position="short")
    r = price(doc(HW, [a, b], observables=obsv))
    assert abs(r["price"]) < 1e-6 * r["legs"]["flt"]
    lo = price(doc(HW, [floater(index="CMS5Y", gearing=0, spread=0.02)], observables=obsv))["price"]
    hi = price(doc(HW, [floater(index="CMS5Y", gearing=0, spread=0.04)], observables=obsv))["price"]
    diag = r["leg_diagnostics"]["flt"]
    assert lo < r["legs"]["flt"] < hi and diag["cap_binding_probability"] > 0 and diag["floor_binding_probability"] > 0


def test_cap_per_period_with_nulls_equals_uncapped_where_null():
    obsv = {"L3M": L3M}
    capped = price(doc(HW, [floater(cap=[None, None, None, None])], observables=obsv))
    plain = price(doc(HW, [floater()], observables=obsv))
    assert capped["price"] == pytest.approx(plain["price"], rel=1e-12)


def test_per_period_value_errors():
    obsv = {"L3M": L3M}
    e = errors(doc(HW, [floater(cap=[0.05, 0.05], floor={"1Y": 0.0}),
                        _fixed(id="f", rate=[0.03, None, 0.03, 0.03])], observables=obsv))
    assert "has 2 values but the schedule has 4 periods" in e["trade.legs[0].cap"]
    assert "no step on or before" in e["trade.legs[0].floor"]
    assert "null is not allowed" in e["trade.legs[1].rate"]
    e = errors(doc(HW, [floater(cap=0.01, floor=[0.0, 0.02, 0.0, 0.0])], observables=obsv))
    assert "cap is below floor in 1 period" in e["trade.legs[0].cap"]


def test_step_keys_tolerate_business_day_adjusted_period_dates():
    """4Y = Sat 2030-09-28; the schedule's adjusted date is Mon 2030-09-30. The period ending there keeps the old
    value and the one starting there takes the new one."""
    from pricer.coupons import Period, per_period
    plan = build_plan(doc(BS_FLAT, [_fixed()], maturity="5Y"), EX)
    pers = [Period(dt.date(2030, 6, 28), dt.date(2030, 9, 30), None, 0.0),
            Period(dt.date(2030, 9, 30), dt.date(2030, 12, 30), None, 0.0)]
    assert list(per_period(plan.v, {"effective_date": 0.045, "4Y": 0.05}, pers)) == [0.045, 0.05]


# ---------------------------------------------------------------- QuantLib conformance of conventions
def _values_under_deterministic_curve(observables, name, dates):
    """Evaluate an observable in a plan priced on today's curves (deterministic rates)."""
    from pricer.payoffs import PCtx
    from pricer import observables as obs_mod
    d = doc(BS_FLAT, [floater(index=name, fixing_lag=0, fixing_calendar="NullCalendar")], observables=observables)
    plan = build_plan(copy.deepcopy(d), EX)
    assert plan.report.ok, [str(i) for i in plan.report.errors]
    r = {}
    for key, node in plan.nodes.items():
        r[key] = node.fn(r)
        if key == "fixings":
            break
    return obs_mod.evaluate(PCtx(plan.v, r["fixings"], 1), name, dates)[:, 0], r


FIX_DATES = [dt.date(2026, 9, 28), dt.date(2026, 12, 29), dt.date(2027, 6, 28), dt.date(2028, 2, 28)]


@pytest.mark.parametrize("name, spec, make", [
    ("E6M", {"type": "term_rate", "curve": "USD", "quantlib_index": "Euribor", "tenor": "6M"},
     lambda h: ql.Euribor(ql.Period("6M"), h)),
    ("T3M", {"type": "term_rate", "curve": "USD", "tenor": "3M", "calendar": "UnitedStates.GovernmentBond",
             "end_of_month": True},
     lambda h: ql.IborIndex("x", ql.Period("3M"), 2, ql.USDCurrency(), ql.UnitedStates(ql.UnitedStates.GovernmentBond),
                            ql.ModifiedFollowing, True, ql.Actual360(), h)),
    ("SOFR", {"type": "overnight_rate", "curve": "USD", "quantlib_index": "Sofr"}, lambda h: ql.Sofr(h)),
    ("CMS5", {"type": "swap_rate", "curve": "USD", "quantlib_index": "EuriborSwapIsdaFixA", "tenor": "5Y"},
     lambda h: ql.EuriborSwapIsdaFixA(ql.Period("5Y"), h)),
    ("CMS10", {"type": "swap_rate", "curve": "USD", "tenor": "10Y", "fixed_frequency": "6M",
               "calendar": "UnitedStates.GovernmentBond"},
     lambda h: ql.SwapIndex("s", ql.Period("10Y"), 2, ql.USDCurrency(), ql.UnitedStates(ql.UnitedStates.GovernmentBond),
                            ql.Period("6M"), ql.ModifiedFollowing, ql.Thirty360(ql.Thirty360.BondBasis),
                            ql.IborIndex("f", ql.Period("6M"), 2, ql.USDCurrency(),
                                         ql.UnitedStates(ql.UnitedStates.GovernmentBond), ql.ModifiedFollowing,
                                         False, ql.Actual360(), h))),
])
def test_rate_observables_equal_quantlib_forecast_fixings(name, spec, make):
    ours, r = _values_under_deterministic_curve({name: spec}, name, FIX_DATES)
    idx = make(r["curve:USD"])
    theirs = [idx.fixing(idx.fixingCalendar().adjust(ql.Date(d.day, d.month, d.year), ql.Preceding)) for d in FIX_DATES]
    assert ours == pytest.approx(theirs, rel=1e-10, abs=1e-12)


def test_actual_actual_bond_stub_accrual_comes_from_quantlib():
    sched = {"s": {"type": "generated", "start": "2026-11-15", "end": "2029-09-28", "frequency": "6M",
                   "calendar": "WeekendsOnly"}}
    leg = _fixed(rate=0.05, schedule="s", accrual_start="2026-11-15", day_count="ActualActual.Bond")
    r = price(doc(BS_FLAT, [leg], schedules=sched, maturity="2029-09-28"))
    qs = ql.Schedule(ql.Date(15, 11, 2026), ql.Date(28, 9, 2029), ql.Period("6M"), ql.WeekendsOnly(),
                     ql.ModifiedFollowing, ql.ModifiedFollowing, ql.DateGeneration.Backward, False)
    ql_leg = ql.FixedRateLeg(qs, ql.ActualActual(ql.ActualActual.Bond), [N], [0.05])
    assert [c["expected_amount"] for c in r["expected_cashflows"]] == pytest.approx([c.amount() for c in ql_leg],
                                                                                   abs=1e-6)
    assert r["expected_cashflows"][0]["expected_amount"] == pytest.approx(N * 0.05 * 0.3674033149171271, abs=1e-6)


def test_tenor_maturity_is_adjusted_and_schedules_roll_from_the_unadjusted_date():
    plan = build_plan(doc(BS_FLAT, [_fixed(schedule="q")], maturity="5Y",
                          schedules={"q": {"type": "generated", "frequency": "3M", "calendar": "WeekendsOnly"}}), EX)
    assert plan.v.maturity == dt.date(2031, 9, 29)                     # 2031-09-28 is a Sunday
    q = plan.v.schedules["q"]
    assert q[0] == dt.date(2026, 12, 28) and q[-1] == dt.date(2031, 9, 29)
    assert list(q) == [dt.date(d.year(), d.month(), d.dayOfMonth()) for d in list(q.ql_schedule)[1:]]


def test_daily_schedule_is_quantlib_business_day_list():
    plan = build_plan(doc(BS_FLAT, [_fixed()], schedules={
        "q": {"type": "generated", "frequency": "3M", "calendar": "NullCalendar"},
        "d": {"type": "generated", "frequency": "1D", "calendar": "UnitedStates.NYSE"}}), EX)
    nyse = ql.UnitedStates(ql.UnitedStates.NYSE)
    expected = [dt.date(d.year(), d.month(), d.dayOfMonth())
                for d in nyse.businessDayList(ql.Date(28, 9, 2026), ql.Date(28, 9, 2027))][1:]
    assert plan.v.schedules["d"] == expected
