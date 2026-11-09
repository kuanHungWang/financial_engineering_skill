"""Solving a term-sheet parameter for a target price (pricer/solve.py, `price.py solve`).

Run:  python -m pytest tests -q
"""
import copy
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pricer import build_plan, run  # noqa: E402
from pricer.engine import set_path  # noqa: E402
from pricer.solve import BrentError, SolveError, brent, solve  # noqa: E402

EX = ROOT / "examples"
QUIET = lambda m: None  # noqa: E731


def load(name):
    return yaml.safe_load((EX / name).read_text())


def reprice(doc, param, x, n=None):
    """A fresh price (no reuse) at the solution, as `price.py price --set ...` would give."""
    d = copy.deepcopy(doc)
    set_path(d, param, x)
    if n:
        set_path(d, "pricing.num_paths", n)
    plan = build_plan(d, EX)
    assert plan.report.ok
    return run(plan)


# ------------------------------------------------------------------ Brent
def test_brent_finds_cubic_root_and_keeps_bracket():
    calls = []

    def f(x):
        calls.append(x)
        return x ** 3 - 2 * x - 5
    root, fr, (lo, hi) = brent(f, 2.0, 3.0, xtol=1e-12)
    assert root == pytest.approx(2.0945514815423265, abs=1e-11)
    assert lo <= root <= hi and hi - lo < 1e-11
    assert len(calls) < 15


def test_brent_rejects_same_sign():
    with pytest.raises(BrentError):
        brent(lambda x: x * x + 1, -1.0, 1.0)


# ------------------------------------------------------------------ Monte Carlo
def test_coupon_for_target_price_reprices_exactly():
    doc = load("autocallable_note.yaml")
    doc["pricing"]["num_paths"] = 8000
    p = "trade.legs.0.rate"
    out = solve(doc, EX, p, 980000.0, bracket=(0.0, 0.2), log=QUIET)
    assert out["ok"] and out["method"] == "mc" and out["num_paths"] == 8000
    # the price the solver reports (computed with reused paths) is what a fresh run gives at the solution
    fresh = reprice(doc, p, out["solution"])
    assert fresh["price"] == pytest.approx(out["price_at_solution"], rel=1e-12)
    assert fresh["price"] == pytest.approx(980000.0, abs=0.05 * out["price_std_error"])
    # one cheap probe, then the full count started from the probe's root and slope
    assert [s["num_paths"] for s in out["stages"]] == [2000, 8000]
    assert out["stages"][1]["start"] == "secant from the previous stage"
    assert out["stages"][1]["evaluations"] <= 4
    lo, hi = out["solution_confidence_interval"]
    assert lo < out["solution"] < hi
    assert out["solution_std_error"] == pytest.approx(out["price_std_error"] / abs(out["slope"]))
    # only one simulation per stage: later evaluations reuse the paths
    assert out["paths_simulated"] == 2000 + 8000


def test_ladder_matches_two_level_solution():
    doc = load("autocallable_note.yaml")
    doc["pricing"]["num_paths"] = 8000
    a = solve(doc, EX, "trade.legs.0.rate", 980000.0, bracket=(0.0, 0.2), log=QUIET)
    b = solve(doc, EX, "trade.legs.0.rate", 980000.0, bracket=(0.0, 0.2), growth=2, log=QUIET)
    assert [s["num_paths"] for s in b["stages"]] == [2000, 4000, 8000]
    # same final paths (same seed and count): the same function, so the same root up to the tolerance
    assert b["solution"] == pytest.approx(a["solution"], abs=0.05 * a["solution_std_error"])


def test_barrier_of_an_event_is_solvable():
    doc = load("autocallable_note.yaml")
    doc["pricing"]["num_paths"] = 4000
    p = "trade.termination_events.0.barrier"
    out = solve(doc, EX, p, 1020000.0, bracket=(1.05, 1.3), log=QUIET)
    assert 1.1 < out["solution"] < 1.2
    fresh = reprice(doc, p, out["solution"])
    assert fresh["price"] == pytest.approx(out["price_at_solution"], rel=1e-12)


def test_x_tol_raises_paths_until_the_interval_is_tight_enough():
    doc = load("autocallable_note.yaml")
    doc["pricing"]["num_paths"] = 4000
    out = solve(doc, EX, "trade.legs.0.rate", 980000.0, bracket=(0.0, 0.2), x_tol=0.002, log=QUIET)
    lo, hi = out["solution_confidence_interval"]
    assert out["stop_reason"] == "x_tol reached"
    assert (hi - lo) / 2 <= 0.002
    assert out["num_paths"] > 2000


def test_callable_bond_coupon_at_par():
    doc = load("callable_fixed_rate_bond.yaml")
    doc["pricing"]["num_paths"] = 4000
    p = "trade.legs.0.rate"
    out = solve(doc, EX, p, 1000000.0, bracket=(0.01, 0.1), log=QUIET)
    fresh = reprice(doc, p, out["solution"])         # the exercise rule is refitted, not reused
    assert fresh["price"] == pytest.approx(out["price_at_solution"], rel=1e-12)
    assert 0.03 < out["solution"] < 0.06


# ------------------------------------------------------------------ closed form
def test_analytic_strike_for_premium():
    doc = load("vanilla_call.yaml")
    p = "trade.legs.0.strike"
    base = reprice(doc, p, 1.0)["price"]
    out = solve(doc, EX, p, 0.8 * base, log=QUIET)       # default bracket: strike +/- 10%, widened if needed
    assert out["method"] == "analytic" and out["num_paths"] is None and len(out["stages"]) == 1
    assert out["solution"] > 1.0
    assert reprice(doc, p, out["solution"])["price"] == pytest.approx(0.8 * base, rel=1e-9)
    assert out["solution_std_error"] == 0


# ------------------------------------------------------------------ errors
@pytest.mark.parametrize("param, fragment", [
    ("trade.legs.0.schedule", "only a number can be solved for"),
    ("trade.legs.0.no_such_field", "has no field"),
    ("trade.legs.7.rate", "is a list of 3 item(s)"),
])
def test_bad_parameter(param, fragment):
    with pytest.raises(SolveError) as e:
        solve(load("autocallable_note.yaml"), EX, param, 1.0, log=QUIET)
    assert fragment in str(e.value)


def test_unreachable_target_names_the_bracket():
    doc = load("vanilla_call.yaml")
    with pytest.raises(SolveError) as e:
        solve(doc, EX, "trade.legs.0.strike", -5.0, bracket=(0.9, 1.1), max_expansions=3, log=QUIET)
    assert e.value.path == "bracket" and e.value.details["evaluated"]


# ------------------------------------------------------------------ CLI
def cli(*args):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "price.py"), *args],
                       capture_output=True, text=True, cwd=ROOT)
    return r.returncode, json.loads(r.stdout)


def test_cli_solve_and_errors():
    note = str(EX / "autocallable_note.yaml")
    code, out = cli("solve", note, "--param", "trade.legs.0.rate", "--target", "980000", "--bracket", "0", "0.2",
                    "--set", "pricing.num_paths=4000")
    assert code == 0 and out["ok"] and math.isfinite(out["solution"])
    assert out["reprice"].startswith("--set trade.legs.0.rate=")
    code, out = cli("solve", note, "--param", "trade.legs.0.schedule", "--target", "1")
    assert code == 2 and out["errors"][0]["path"] == "trade.legs.0.schedule"
