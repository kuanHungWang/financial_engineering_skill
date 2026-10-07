"""`inspect`: node results written as they are reproduce the price, and stdout stays a small manifest.

Run:  python -m pytest tests -q
"""
import copy
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pricer import build_plan, run  # noqa: E402
from pricer.inspection import InspectError, inspect, select  # noqa: E402

EX = ROOT / "examples"


def plan_of(name, n=2000):
    doc = yaml.safe_load((EX / name).read_text())
    doc["pricing"] = {**doc.get("pricing", {}), "num_paths": n, "seed": 11}
    return build_plan(copy.deepcopy(doc), EX)


def matrix(path):
    return pd.read_csv(path).drop(columns="path")


@pytest.mark.parametrize("name", ["autocallable_note.yaml", "bermudan_put.yaml", "cms_spread_range_accrual.yaml",
                                  "inverse_floater_tarn.yaml", "callable_fixed_rate_bond_calibrated.yaml"])
def test_files_reproduce_the_price(tmp_path, name):
    result, manifest = inspect(plan_of(name), tmp_path)
    assert result["price"] == run(plan_of(name))["price"]           # inspecting does not change the run
    pv = pd.read_csv(tmp_path / "price.per_path.csv")
    assert len(pv) == result["num_paths"]
    assert pv.pv.mean() == pytest.approx(result["price"], rel=1e-12)
    flows = matrix(tmp_path / "price.cashflows.csv")
    dfs = matrix(tmp_path / "discount_factors.values.csv")
    disc = np.column_stack([np.broadcast_to(dfs[c.split(":", 2)[2].split("#")[0]].values, (len(flows),))
                            for c in flows.columns])
    assert (flows.values * disc).sum(axis=1) == pytest.approx(pv.pv.values, rel=1e-12, abs=1e-6)
    assert {"result.json", "input.json", "model.json"} <= {f["file"] for f in manifest["files"]}


def test_leg_and_observable_files_are_what_the_legs_computed(tmp_path):
    result, _ = inspect(plan_of("autocallable_note.yaml"), tmp_path, nodes=["fixings", "leg", "observable"])
    paths = matrix(tmp_path / "fixings.equity.SPX.csv")
    chk = result["martingale_check"]["SPX"]
    assert paths[chk["date"]].mean() == pytest.approx(chk["simulated_mean"], rel=1e-12)
    seen = matrix(tmp_path / "observable_SPX.values.csv")         # SPX as the legs and events read it
    common = [d for d in seen.columns if d in paths.columns]
    assert common and np.allclose(seen[common].values, paths[common].values, rtol=1e-14)


def test_bermudan_exercise_inputs_and_decisions(tmp_path):
    result, _ = inspect(plan_of("bermudan_put.yaml"), tmp_path, nodes=["event", "exercise_policy", "price"])
    amount = matrix(tmp_path / "event_early_exercise.amount.csv")
    state = matrix(tmp_path / "event_early_exercise.state.0.csv")
    assert list(amount.columns) == list(state.columns)            # labelled by the exercise dates
    per_path = pd.read_csv(tmp_path / "price.per_path.csv")
    ex = per_path.exercise_date.dropna()
    assert len(ex) / len(per_path) == pytest.approx(result["events"]["early_exercise"]["exercise_probability"])
    assert all((amount.loc[i, d] > 0) for i, d in ex.items())      # a put is exercised in the money only
    pol = json.loads((tmp_path / "exercise_policy.json").read_text())
    assert pol["policy.degree"] == 2
    assert len([k for k in pol if k.startswith("policy.coefficients.")]) == len(amount.columns)   # one regression per exercise date


def test_node_selection_and_max_paths(tmp_path):
    plan = plan_of("dual_equity_range_accrual.yaml")
    assert select(plan, ["leg"]) == [k for k in plan.nodes if k.startswith("leg:")]
    with pytest.raises(InspectError):
        select(plan, ["leg:nope"])
    _, manifest = inspect(plan, tmp_path, nodes=["observable:SPX_PERF", "curve:USD"], max_paths=7)
    files = {f["file"]: f for f in manifest["files"]}
    assert set(files) == {"result.json", "input.json", "curve_USD.csv", "observable_SPX_PERF.values.csv"}
    obs = pd.read_csv(tmp_path / "observable_SPX_PERF.values.csv")
    assert len(obs) == 7
    full = files["observable_SPX_PERF.values.csv"]["summary"]["at_last"]["mean"]      # statistics use every path
    assert pd.read_csv(tmp_path / "observable_SPX_PERF.values_stats.csv")["mean"].iloc[-1] == pytest.approx(full)
    assert obs.iloc[:, -1].mean() != pytest.approx(full, rel=1e-12)


def test_analytic_trade(tmp_path):
    result, manifest = inspect(plan_of("vanilla_call.yaml"), tmp_path)
    assert result["method"] == "analytic"
    assert json.loads((tmp_path / "price.json").read_text())["price"] == result["price"]
    assert "underlying_SPX.json" in {f["file"] for f in manifest["files"]}


def test_cli_prints_a_small_manifest_and_rejects_unknown_nodes(tmp_path):
    cli = [sys.executable, str(ROOT / "scripts" / "price.py"), "inspect", str(EX / "bermudan_put.yaml"),
           "--set", "pricing.num_paths=4000", "--out-dir", str(tmp_path)]
    p = subprocess.run(cli, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert len(p.stdout) < 20_000                     # the data is on disk, not on stdout
    assert json.loads(p.stdout)["files"]
    assert (tmp_path / "fixings.equity.SPX.csv").stat().st_size > 10 * len(p.stdout)
    bad = subprocess.run(cli + ["--node", "leg:nope"], capture_output=True, text=True)
    assert bad.returncode == 2 and "unknown node" in bad.stdout
