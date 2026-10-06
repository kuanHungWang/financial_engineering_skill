"""Seasoned trades: priced after inception, between two payment dates, with part of the fixings known.

Past fixings come from `history` (rate observables) and `trade.history` (closing levels of model
underlyings). Each test checks a known-past / simulated-future split against an exact value, a closed
form, or QuantLib with the same fixings. Run:  python -m pytest tests -q
"""
import copy
import datetime as dt
import math
import sys
from pathlib import Path

import pytest
import QuantLib as ql
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pricer import build_plan, run  # noqa: E402

EX = ROOT / "examples"
VAL = dt.date(2026, 9, 28)
START = dt.date(2026, 6, 29)          # the trade was done 3 months ago
N = 1_000_000
BS = {"type": "black_scholes", "underlying": "AAA", "discount_curve": "USD-FLAT"}


def market():
    m = yaml.safe_load((EX / "market" / "mock_market.yaml").read_text())
    m["equities"]["AAA"] = {"spot": 100.0, "calendar": "NullCalendar", "dividend": {"type": "flat", "rate": 0.01},
                            "vol": {"type": "constant", "value": 0.25}}
    return m


def doc(legs, events=(), observables=None, schedules=None, history=None, model=BS, maturity="2027-06-28",
        paths=20000, **trade):
    return {"valuation_date": VAL.isoformat(), "market_data": market(), "model": model,
            "observables": observables or {},
            "schedules": schedules or {"q": {"type": "generated", "frequency": "3M", "calendar": "NullCalendar"},
                                       "daily": {"type": "generated", "frequency": "1D", "calendar": "NullCalendar"}},
            "trade": {"id": "t", "effective_date": START.isoformat(), "maturity": maturity, "legs": list(legs),
                      "termination_events": list(events), "history": history or {}, **trade},
            "pricing": {"num_paths": paths, "seed": 7}}


def price(d):
    plan = build_plan(copy.deepcopy(d), EX)
    assert plan.report.ok, [str(i) for i in plan.report.errors]
    return run(plan)


def errors(d):
    return {i.path: i.message for i in build_plan(copy.deepcopy(d), EX).report.errors}


def past_days(start=START, end=VAL):
    """Calendar days in [start, end) (NullCalendar: every day is a business day)."""
    return [start + dt.timedelta(k) for k in range((end - start).days)]


def flat_history(overrides=None, level=100.0):
    h = {d: level for d in past_days()}
    h.update(overrides or {})
    return {"AAA": h}


def df(d, r=0.04):
    return math.exp(-r * (d - VAL).days / 365.0)


# ---------------------------------------------------------------- range accrual in the middle of a period
RA_END = dt.date(2026, 10, 29)
RA_SCHED = {"ra": {"type": "dates", "dates": ["2026-10-29", "2027-01-29"]}}


def _ra(**kw):
    leg = {"id": "ra", "type": "range_accrual", "rate": 0.06, "notional": N, "schedule": "ra",
           "day_count": "Actual360", "observation_calendar": "NullCalendar",
           "range": [{"observable": "AAA", "upper": 1000.0}]}
    leg.update(kw)
    return leg


def test_range_accrual_mid_period_counts_known_days_from_history():
    """Future days are in range on every path (upper = 10 x spot), so the first period's fraction is
    exactly the share of known in-range days plus all remaining days."""
    out = past_days()[:20]
    h = flat_history({d: 2000.0 for d in out})
    r = price(doc([_ra()], history=h, schedules=RA_SCHED, maturity="2027-01-29"))
    first = r["expected_cashflows"][0]
    n_days = (RA_END - START).days         # first period START .. RA_END, 91 days known, 31 simulated
    assert first["date"] == RA_END.isoformat()
    assert first["expected_amount"] == pytest.approx(N * 0.06 * n_days / 360 * (n_days - 20) / n_days, abs=1e-5)
    assert r["leg_diagnostics"]["ra"]["mean_accrual_fraction_by_period"][0] == pytest.approx((n_days - 20) / n_days,
                                                                                               abs=1e-6)


def test_range_accrual_needs_underlying_history():
    e = errors(doc([_ra()], schedules=RA_SCHED, maturity="2027-01-29"))
    assert "past closing level" in e["trade.legs[0].range[0].observable"]
    d = doc([_ra()], history={"AAA": {past_days()[0]: 100.0}}, schedules=RA_SCHED, maturity="2027-01-29")
    assert "trade.history.AAA" in str(build_plan(copy.deepcopy(d), EX).report.errors)


def test_equity_history_is_levels_in_performance_mode():
    """A performance observable divides past levels by the reference level, as it does simulated ones."""
    perf = {"type": "equity", "underlying": "AAA", "mode": "performance"}
    out = past_days()[:10]
    h = flat_history({d: 115.0 for d in out})                # 115 / 100 = 1.15 > 1.1: out of range
    leg = _ra(range=[{"observable": "PERF", "upper": 10.0}, {"observable": "PERF", "upper": 1.1}],
              schedule="one")
    sched = {"one": {"type": "dates", "dates": ["2026-09-29"]}}
    r = price(doc([leg], observables={"PERF": perf}, schedules=sched, history=h, maturity="2026-09-29",
                  reference_levels={"AAA": 100.0}))
    n_days = (dt.date(2026, 9, 29) - START).days
    # the one future day (2026-09-28) is in range with some probability p; the past is exact
    frac = r["leg_diagnostics"]["ra"]["mean_accrual_fraction_by_period"][0]
    assert (n_days - 11) / n_days - 1e-9 <= frac <= (n_days - 10) / n_days + 1e-9


def test_reference_level_defaults_to_history_on_effective_date():
    put = {"id": "p", "type": "option_payoff", "underlying": "AAA", "option_type": "put", "strike": 1.0,
           "notional": N}
    plan = build_plan(doc([put], history=flat_history({START: 120.0})), EX)
    assert plan.report.ok and plan.v.reference_levels["AAA"] == 120.0
    assert not [w for w in plan.report.warnings if "reference_levels" in w.path]


# ---------------------------------------------------------------- barriers observed before the valuation date
def _put(**kw):
    leg = {"id": "p", "type": "option_payoff", "underlying": "AAA", "option_type": "put", "strike": 1.0,
           "notional": N}
    leg.update(kw)
    return leg


KI = {"type": "knock_in", "underlying": "AAA", "direction": "down", "barrier": 0.7, "observation": "daily"}
KO = {"type": "knock_out", "underlying": "AAA", "direction": "down", "barrier": 0.7, "observation": "daily"}


def test_knock_in_hit_before_valuation_is_a_vanilla():
    hit = flat_history({past_days()[30]: 65.0})
    vanilla = price(doc([_put()], history=hit, reference_levels={"AAA": 100.0}))
    assert vanilla["method"] == "analytic"
    r = price(doc([_put(conditions=[KI])], history=hit, reference_levels={"AAA": 100.0}, paths=40000))
    assert r["leg_diagnostics"]["p"]["conditions[0].knock_in.barrier_hit_probability"] == 1.0
    assert abs(r["price"] - vanilla["price"]) < 4 * r["std_error"]


def test_knock_out_condition_hit_before_valuation_is_worthless():
    r = price(doc([_put(conditions=[KO])], history=flat_history({past_days()[5]: 69.0}),
                  reference_levels={"AAA": 100.0}))
    assert r["price"] == 0.0


def test_knock_in_not_hit_before_valuation_is_only_future_monitoring():
    """With a clean history the seasoned knock-in equals a fresh one observed on the remaining dates."""
    seasoned = price(doc([_put(conditions=[KI])], history=flat_history(), reference_levels={"AAA": 100.0}))
    fresh_sched = {"daily": {"type": "generated", "frequency": "1D", "calendar": "NullCalendar",
                             "start": "valuation_date", "include_start": True}}
    d = doc([_put(conditions=[KI])], reference_levels={"AAA": 100.0}, schedules=fresh_sched)
    fresh = price(d)
    assert seasoned["price"] == pytest.approx(fresh["price"], rel=1e-12)


def test_barrier_needs_history():
    e = errors(doc([_put(conditions=[KI])], reference_levels={"AAA": 100.0}))
    assert "past closing level" in e["trade.legs[0].conditions[0].underlying"]


def test_knock_out_event_hit_before_valuation_is_rejected():
    ev = {"id": "ko", "type": "knock_out", "underlying": "AAA", "direction": "up", "barrier": 1.2,
          "observation": "daily"}
    e = errors(doc([_put()], [ev], history=flat_history({past_days()[40]: 121.0}),
                   reference_levels={"AAA": 100.0}))
    assert "already hit on" in e["trade.termination_events[0]"]
    assert "past closing level" in errors(doc([_put()], [ev], reference_levels={"AAA": 100.0})) \
        ["trade.termination_events[0].underlying"]
    r = price(doc([_put()], [ev], history=flat_history(), reference_levels={"AAA": 100.0}))
    assert 0 < r["events"]["ko"]["hit_probability"] < 1


# ---------------------------------------------------------------- TARN with coupons already paid
def test_target_counts_coupons_paid_before_valuation():
    """Deterministic fixed coupons: with the first coupon already paid, the target is crossed one coupon
    earlier, and the crossing coupon is capped at what is left."""
    model = {"type": "black_scholes", "underlying": "AAA", "discount_curve": "USD-FLAT"}
    fixed = {"id": "cpn", "type": "fixed_leg", "rate": 0.08, "notional": N, "schedule": "q",
             "day_count": "Actual360"}
    ends = [dt.date(2026, 9, 28), dt.date(2026, 12, 28), dt.date(2027, 3, 28), dt.date(2027, 6, 28)]   # backward
    starts = [START] + ends[:-1]
    c = [N * 0.08 * (e - s).days / 360 for s, e in zip(starts, ends)]
    target = c[0] + c[1] + 0.5 * c[2]
    ev = {"id": "tarn", "type": "target", "legs": ["cpn"], "target": target, "rebate": N}
    d = doc([fixed], [ev], model=model, history=flat_history())
    d["valuation_date"] = "2026-10-15"
    d["market_data"]["as_of"] = "2026-10-15"
    assert "count toward the target" in errors(d)["trade.termination_events[0].accumulated"]
    d["trade"]["termination_events"][0]["accumulated"] = c[0]
    r = price(d)
    val = dt.date(2026, 10, 15)
    dfs = [math.exp(-0.04 * (e - val).days / 365.0) for e in ends]
    expected = c[1] * dfs[1] + (target - c[0] - c[1]) * dfs[2] + N * dfs[2]
    assert r["std_error"] == pytest.approx(0, abs=1e-6)
    assert r["price"] == pytest.approx(expected, rel=1e-9)
    d["trade"]["termination_events"][0]["accumulated"] = target
    assert "already reaches the target" in errors(d)["trade.termination_events[0].accumulated"]


# ---------------------------------------------------------------- compounded overnight rate, half fixed
def test_compounded_in_arrears_mid_period_matches_quantlib_with_fixings():
    """SOFR compounded over a period that started 3 months ago: QuantLib's OvernightIndexedCoupon with the
    same past fixings and today's flat curve is the reference."""
    sofr = {"type": "overnight_rate", "curve": "USD-FLAT", "calendar": "WeekendsOnly"}
    end = dt.date(2026, 12, 29)
    cal = ql.WeekendsOnly()
    fixings = {}
    d = cal.adjust(ql.Date(START.day, START.month, START.year))
    while d < ql.Date(VAL.day, VAL.month, VAL.year):
        fixings[dt.date(d.year(), d.month(), d.dayOfMonth())] = 0.05 + 0.0001 * (len(fixings) % 7)
        d = cal.advance(d, 1, ql.Days)
    leg = {"id": "ois", "type": "floating_leg", "index": "SOFR", "fixing": "compounded_in_arrears",
           "notional": N, "schedule": "one"}
    dd = doc([leg], observables={"SOFR": dict(sofr, history=fixings)},
             schedules={"one": {"type": "dates", "dates": [end.isoformat()]}}, maturity=end.isoformat())
    r = price(dd)
    ql.Settings.instance().evaluationDate = ql.Date(VAL.day, VAL.month, VAL.year)
    h = ql.YieldTermStructureHandle(ql.FlatForward(ql.Date(VAL.day, VAL.month, VAL.year), 0.04,
                                                   ql.Actual365Fixed(), ql.Continuous))
    idx = ql.OvernightIndex("SEASONED-SOFR-TEST", 0, ql.USDCurrency(), cal, ql.Actual360(), h)
    idx.clearFixings()
    for day, x in fixings.items():
        idx.addFixing(ql.Date(day.day, day.month, day.year), x)
    s, e = ql.Date(START.day, START.month, START.year), ql.Date(end.day, end.month, end.year)
    target = ql.OvernightIndexedCoupon(e, N, s, e, idx).amount()
    idx.clearFixings()
    assert r["expected_cashflows"][0]["expected_amount"] == pytest.approx(target, rel=1e-9)


# ---------------------------------------------------------------- generic: any payoff reads past fixings the same way
def test_expired_unpaid_option_is_its_known_intrinsic_value():
    """Expiry passed, payment still due: the payoff reads its fixing from history like any other read."""
    exp, pay = dt.date(2026, 9, 21), dt.date(2026, 10, 5)
    put = _put(expiry=exp.isoformat(), payment_date=pay.isoformat())
    r = price(doc([put], history=flat_history({exp: 90.0}), reference_levels={"AAA": 100.0},
                  maturity=pay.isoformat()))
    assert r["std_error"] == pytest.approx(0, abs=1e-9)
    assert r["price"] == pytest.approx(N * 0.10 * df(pay), rel=1e-9)


def test_composite_index_fixed_in_the_past_reads_history_of_its_leaves():
    """An equity-linked coupon (linear on a performance observable) fixed in advance at the start of a
    period that began before the valuation date: no component knows about history, the read does."""
    observables = {"PERF": {"type": "equity", "underlying": "AAA", "mode": "performance"},
                   "CPN": {"type": "linear", "constant": -1.0, "terms": [{"observable": "PERF", "weight": 1.0}]}}
    leg = {"id": "eq", "type": "floating_leg", "index": "CPN", "notional": N, "schedule": "one",
           "fixing": "in_advance", "fixing_lag": 0, "fixing_calendar": "NullCalendar", "day_count": "Actual360"}
    end = dt.date(2026, 12, 29)
    r = price(doc([leg], observables=observables, schedules={"one": {"type": "dates", "dates": [end.isoformat()]}},
                  history=flat_history({START: 80.0}), maturity=end.isoformat(), reference_levels={"AAA": 40.0}))
    assert r["price"] == pytest.approx(N * (80.0 / 40.0 - 1.0) * (end - START).days / 360 * df(end), rel=1e-9)
    plan = build_plan(copy.deepcopy(doc([leg], observables=observables, maturity=end.isoformat(),
                                        schedules={"one": {"type": "dates", "dates": [end.isoformat()]}})), EX)
    assert "'PERF' needs 1 past closing level" in str(plan.report.errors)


def test_components_read_the_market_only_through_observables():
    """The contract that makes history generic: no payoff component reads simulated paths directly."""
    for f in ("payoffs.py", "coupons.py", "bermudan.py", "exercise.py", "analytic.py"):
        src = (ROOT / "pricer" / f).read_text()
        assert "scenario.equity" not in src and ".history[" not in src, f
