"""Regression tests: Monte Carlo vs QuantLib closed forms, limiting cases, validation behaviour.

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
from pricer import conventions as cv  # noqa: E402
from pricer.market import simulation_curve  # noqa: E402

EX = ROOT / "examples"


def load(name):
    return yaml.safe_load((EX / name).read_text())


def price(doc):
    plan = build_plan(copy.deepcopy(doc), EX)
    assert plan.report.ok, [str(i) for i in plan.report.errors]
    return run(plan)


def errors(doc):
    plan = build_plan(copy.deepcopy(doc), EX)
    return {i.path: i.message for i in plan.report.errors}


def flat(doc, n=40000):
    """Constant-vol market so closed forms apply exactly."""
    doc = copy.deepcopy(doc)
    doc["model"] = {"type": "black_scholes", "underlying": "SPX-FLATVOL", "discount_curve": "USD-FLAT"}
    s = yaml.safe_dump(doc).replace("underlying: SPX\n", "underlying: SPX-FLATVOL\n")
    doc = yaml.safe_load(s)
    doc.setdefault("pricing", {})["num_paths"] = n
    return doc


def within(mc, target, k=4.0):
    return abs(mc["price"] - target) <= k * mc["std_error"]


# ---------------------------------------------------------------- analytic vs MC
def test_vanilla_auto_uses_analytic_and_matches_black_scholes():
    r = price(flat(load("vanilla_call.yaml")))
    assert r["method"] == "analytic"
    # independent Black-Scholes: S=5000, K=5000, r=4% cc, q=1% cc (Act365 on the flat curve), vol 20%
    t = (ql.Date(28, 9, 2027) - ql.Date(28, 9, 2026)) / 365.0
    s, k, rr, q, v = 5000.0, 5000.0, 0.04, 0.01, 0.20
    d1 = (math.log(s / k) + (rr - q + v * v / 2) * t) / (v * math.sqrt(t))
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))  # noqa: E731
    bs = s * math.exp(-q * t) * n(d1) - k * math.exp(-rr * t) * n(d1 - v * math.sqrt(t))
    assert r["price"] == pytest.approx(1_000_000 / 5000 * bs, rel=1e-9)


@pytest.mark.parametrize("vol_market", ["flat", "surface"])
def test_mc_vanilla_matches_analytic(vol_market):
    doc = load("vanilla_call.yaml")
    if vol_market == "flat":
        doc = flat(doc)
    doc["pricing"] = {"method": "mc", "num_paths": 60000}
    r = price(doc)
    assert r["cross_check"]["agrees"], r["cross_check"]
    assert all(c["ok"] for c in r["martingale_check"].values())


def test_local_vol_mc_reproduces_otm_put():
    doc = load("vanilla_call.yaml")
    doc["trade"]["legs"] = [{"id": "p", "type": "option_payoff", "underlying": "SPX",
                             "option_type": "put", "strike": 0.8, "notional": 1_000_000}]
    doc["pricing"] = {"method": "mc", "num_paths": 60000}
    assert price(doc)["cross_check"]["agrees"]


def test_daily_barrier_mc_matches_continuous_with_bgk_correction():
    """Discrete daily up-and-out vs closed form with the Broadie-Glasserman-Kou barrier shift."""
    doc = flat(load("up_and_out_call_continuous.yaml"))
    ev = doc["trade"]["termination_events"][0]
    ev["rebate"] = 0
    doc["schedules"] = {"daily": {"type": "generated", "frequency": "1D", "calendar": "UnitedStates.NYSE"}}
    ev["observation"] = "daily"
    mc = price(doc)
    assert mc["method"] == "mc"
    n_obs = len(build_plan(copy.deepcopy(doc), EX).v.schedules["daily"])
    dt_obs = 1.0 / n_obs
    ev["observation"] = "continuous"
    ev["barrier"] = 1.30 * math.exp(0.5826 * 0.20 * math.sqrt(dt_obs))
    an = price(doc)
    assert an["method"] == "analytic"
    assert abs(mc["price"] - an["price"]) <= 4 * mc["std_error"] + 0.01 * an["price"]


def test_heston_mc_matches_analytic_heston():
    doc = load("vanilla_call.yaml")
    doc["model"] = {"type": "heston", "underlying": "SPX", "discount_curve": "USD",
                    "params": {"v0": 0.04, "kappa": 1.5, "theta": 0.04, "sigma": 0.4, "rho": -0.6}}
    doc["pricing"] = {"method": "mc", "num_paths": 40000}
    assert price(doc)["cross_check"]["agrees"]


# ---------------------------------------------------------------- EKIAKO limits
def test_ekiako_reduces_to_vanilla_put_when_barriers_never_bind():
    doc = flat(load("ekiako_put.yaml"))
    doc["trade"]["legs"][0]["conditions"][0]["barrier"] = 50.0      # down-in at 5000% => always in
    doc["trade"]["termination_events"][0]["barrier"] = 50.0          # up-out at 5000% => never out
    mc = price(doc)
    vanilla = copy.deepcopy(doc)
    vanilla["trade"]["legs"][0]["conditions"] = []
    vanilla["trade"]["termination_events"] = []
    an = price(vanilla)
    assert an["method"] == "analytic"
    assert within(mc, an["price"])


def test_ekiako_is_cheaper_than_vanilla_put_and_positive():
    r = price(load("ekiako_put.yaml"))
    doc = load("ekiako_put.yaml")
    doc["trade"]["legs"][0]["conditions"] = []
    doc["trade"]["termination_events"] = []
    assert 0 < r["price"] < price(doc)["price"]


def test_same_seed_same_price_and_seed_changes_price():
    a = price(load("ekiako_put.yaml"))
    b = price(load("ekiako_put.yaml"))
    assert a["price"] == b["price"]
    doc = load("ekiako_put.yaml")
    doc["pricing"]["seed"] = 43
    assert price(doc)["price"] != a["price"]


# ---------------------------------------------------------------- composition
def test_note_with_unreachable_barriers_equals_discounted_fixed_flows():
    doc = load("autocallable_note.yaml")
    doc["trade"]["termination_events"][0]["barrier"] = 100.0         # never autocalls
    doc["trade"]["legs"][2]["conditions"][0]["barrier"] = 1e-6       # never knocks in
    r = price(doc)
    assert r["std_error"] == pytest.approx(0.0, abs=1e-9)
    plan = build_plan(copy.deepcopy(doc), EX)
    assert r["legs"]["short_dip"] == 0.0
    assert r["price"] == pytest.approx(sum(c["pv"] for c in r["expected_cashflows"]), rel=1e-9)
    assert r["legs"]["principal"] == pytest.approx(
        1_000_000 * next(c["discount_factor"] for c in r["expected_cashflows"] if c["date"] == "2028-09-28"))
    assert len(plan.pay_dates) == 8


def test_autocall_pays_coupon_on_call_date():
    r = price(load("autocallable_note.yaml"))
    first = r["expected_cashflows"][0]
    p_call = r["expected_cashflows"][1]["survival_probability"]
    # first coupon always paid (settle_on_event_date) + par redemption with the autocall probability
    assert first["survival_probability"] == 1.0
    coupon = 1_000_000 * 0.08 * ql.Thirty360(ql.Thirty360.BondBasis).yearFraction(
        ql.Date(28, 9, 2026), ql.Date(28, 12, 2026))
    assert first["expected_amount"] == pytest.approx(coupon + (1 - p_call) * 1_000_000, rel=1e-6)


# ---------------------------------------------------------------- validation
def test_validation_reports_all_problems_in_one_round():
    doc = load("ekiako_put.yaml")
    doc["schedules"]["daily"]["calendar"] = "UnitedStates.NYS"
    doc["trade"]["legs"][0]["option_type"] = "puts"
    doc["trade"]["legs"][0]["conditions"][0]["observation"] = "expiry"
    doc["trade"]["termination_events"][0]["underlying"] = "SXP"
    doc["trade"]["termination_events"][0]["rebate_type"] = "cash"
    errs = errors(doc)
    assert "schedules.daily" in errs and "NYSE" in errs["schedules.daily"]
    assert "trade.legs[0].option_type" in errs
    assert "trade.legs[0].conditions[0].observation" in errs
    assert "trade.termination_events[0].underlying" in errs
    assert "trade.termination_events[0].rebate_type" in errs


def test_unknown_type_suggests_closest():
    doc = load("ekiako_put.yaml")
    doc["trade"]["legs"][0]["type"] = "option_payof"
    assert "Did you mean 'option_payoff'" in errors(doc)["trade.legs[0].type"]


def test_already_knocked_out_is_an_error():
    doc = load("ekiako_put.yaml")
    doc["trade"]["termination_events"][0]["barrier"] = 0.9
    doc["schedules"]["daily"]["include_start"] = True
    assert "already breaches" in errors(doc)["trade.termination_events[0]"]


def test_continuous_monitoring_without_analytic_match_is_rejected():
    doc = load("ekiako_put.yaml")
    doc["trade"]["termination_events"][0]["observation"] = "continuous"
    assert "pricing.method" in errors(doc)


def test_memory_guard():
    doc = load("ekiako_put.yaml")
    doc["pricing"]["num_paths"] = 2_000_000
    assert "pricing.num_paths" in errors(doc)


def test_heston_calibration_needs_surface():
    doc = load("vanilla_call.yaml")
    doc["model"] = {"type": "heston", "underlying": "SPX-FLATVOL", "discount_curve": "USD"}
    doc["trade"]["legs"][0]["underlying"] = "SPX-FLATVOL"
    assert "model.params" in errors(doc)


def test_auto_method_picks_mc_for_ekiako_and_explains_why():
    plan = build_plan(load("ekiako_put.yaml"), EX)
    assert plan.method == "mc" and "barrier conditions" in plan.method_reason


# ---------------------------------------------------------------- plumbing
def test_simulation_curve_preserves_discount_factors():
    ref = ql.Date(28, 9, 2026)
    ql.Settings.instance().evaluationDate = ref
    src = ql.YieldTermStructureHandle(ql.FlatForward(ref, 0.04, ql.Thirty360(ql.Thirty360.European)))
    out, resampled = simulation_curve(src, ref, ref + 800)
    assert resampled and out.dayCounter() == ql.Actual365Fixed()
    for d in [ref + 1, ref + 33, ref + 365, ref + 799]:
        assert out.discount(d) == pytest.approx(src.discount(d), rel=1e-12)


def test_catalog_covers_every_kind():
    c = catalog()
    assert set(c) == {"curve", "vol", "schedule", "model", "leg", "condition", "event", "analytic", "observable"}
    assert "option_payoff" in c["leg"] and "knock_in" in c["condition"]


def test_calendar_parsing():
    assert cv.calendar("UnitedStates.NYSE+TARGET").isBusinessDay(ql.Date(28, 9, 2026))
    with pytest.raises(ValueError, match="Did you mean"):
        cv.calendar("TARGT")
