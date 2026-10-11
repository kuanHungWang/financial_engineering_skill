"""Validation report material: the evidence is produced by engine runs only (bumps, stresses, benchmarks, cases),
every status follows its rule, and materials.md shows the same numbers as evidence.json."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples"
_spec = importlib.util.spec_from_file_location("report", ROOT / "scripts" / "report.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


def _main(args, capsys):
    code = report.main([str(a) for a in args])
    return code, json.loads(capsys.readouterr().out)


def _config(tmp_path, trade, **cfg):
    path = tmp_path / "report_config.yaml"
    path.write_text(yaml.safe_dump({"trade": str(trade), **cfg}))
    return path


# ------------------------------------------------------------------ building blocks
def test_bump_market_moves_only_what_it_names():
    m = {"curves": {"A": {"type": "flat", "rate": 0.04},
                    "B": {"type": "zero", "tenors": ["1Y"], "rates": [0.03]},
                    "C": {"type": "bootstrap", "deposit": [{"tenor": "3M", "rate": 0.04}],
                          "swap": [{"tenor": "2Y", "rate": 0.035}]}},
         "equities": {"X": {"spot": 100.0, "dividend": {"type": "flat", "rate": 0.01},
                            "vol": {"type": "surface", "vols": [[0.2, 0.3]]}},
                      "Y": {"spot": 50.0, "vol": {"type": "constant", "value": 0.25}}},
         "swaption_vols": {"USD": {"vols": [[0.01, 0.02]]}}}
    b = report.bump_market(m, ["X"], spot=0.1, vol=0.01, rate=0.001, dividend=0.002, swaption_vol_scale=2.0)
    assert b["equities"]["X"]["spot"] == pytest.approx(110.0) and b["equities"]["Y"]["spot"] == 50.0
    assert b["equities"]["X"]["vol"]["vols"] == [[pytest.approx(0.21), pytest.approx(0.31)]]
    assert b["equities"]["Y"]["vol"]["value"] == 0.25
    assert b["equities"]["X"]["dividend"]["rate"] == pytest.approx(0.012)        # rate bump leaves dividends alone
    assert b["curves"]["A"]["rate"] == pytest.approx(0.041)
    assert b["curves"]["B"]["rates"] == [pytest.approx(0.031)]
    assert [q["rate"] for q in b["curves"]["C"]["deposit"] + b["curves"]["C"]["swap"]] == \
        [pytest.approx(0.041), pytest.approx(0.036)]
    assert b["swaption_vols"]["USD"]["vols"] == [[0.02, 0.04]]
    assert m["equities"]["X"]["spot"] == 100.0                                     # the input is not changed
    with pytest.raises(report.InputError):
        report.bump_market(m, ["Y"], vol=-0.3)                                     # non-positive vol


def test_judge_uses_statistical_tolerance_and_named_results():
    ctx = {"base": (100.0, 1.0), "case": {"c": (90.0, 0.0)}, "benchmark": {}}
    assert report.judge(None, 1.0, 0.0, ctx) == {"status": "INFO"}
    assert report.judge({"approx": "base"}, 105.0, 1.0, ctx)["status"] == "PASS"  # 5 < 4 * sqrt(2)
    assert report.judge({"approx": "base"}, 106.0, 1.0, ctx)["status"] == "FAIL"
    assert report.judge({"approx": "base", "tol": 0.5}, 100.4, 1.0, ctx)["status"] == "PASS"
    assert report.judge({"lt": "case:c"}, 89.0, 0.0, ctx)["status"] == "PASS"
    assert report.judge({"ge": 95}, 96.0, 0.0, ctx)["status"] == "PASS"
    with pytest.raises(report.InputError):
        report.judge({"approx": "case:unknown"}, 1.0, 0.0, ctx)
    with pytest.raises(report.InputError):
        report.judge({"approx": 1, "le": 2}, 1.0, 0.0, ctx)


def test_seed_band_contains_one():
    lo, hi = report._chi_ratio_bounds(7)
    assert 0.3 < lo < 1 < hi < 2


# ------------------------------------------------------------------ evidence
def test_evidence_closed_form_with_config(tmp_path, capsys):
    cfg = _config(tmp_path, EX / "vanilla_call.yaml",
                  benchmarks=[{"label": "MC", "set": ["pricing.method=mc", "pricing.num_paths=20000"],
                               "expect": {"approx": "base"}}],
                  cases=[{"label": "higher strike", "set": ["trade.legs.0.strike=1.1"], "expect": {"lt": "base"}},
                         {"label": "wrong", "set": ["trade.legs.0.strike=1.1"], "expect": {"approx": "case:higher strike",
                                                                                         "tol": 0}},
                         {"label": "near-deterministic", "rationale": "vol near zero",
                          "set": ["market_data.equities.SPX.vol={type: constant, value: 0.0001}"],
                          "expect": {"lt": "base"}}],
                  references=[{"label": "quote", "price": 84000, "tolerance_pct": 0.001}],
                  stress=[{"label": "down", "spot": -0.2}])
    code, out = _main(["evidence", cfg, "--out-dir", tmp_path / "ev"], capsys)
    ev = json.loads((tmp_path / "ev" / "evidence.json").read_text())
    status = {x["test"]: x["status"] for x in ev["summary"]}
    assert status["Benchmark: MC"] == "PASS" and status["Case: higher strike"] == "PASS"
    assert status["Case: near-deterministic"] == "PASS" and status["Case: wrong"] == "PASS"
    assert status["Reference: quote"] == "FAIL" and code == 1 and not out["ok"]   # 84654 vs 84000 +- 84
    assert status["Zero bump reproduces base"] == "PASS" and status["Convergence"] == "SKIPPED"
    sens = {x["factor"]: x for x in ev["tests"]["sensitivities"]["rows"]}
    assert sens["spot"]["first_order"] > 0 and sens["vol"]["first_order"] > 0 and sens["spot"]["gamma"] > 0
    assert ev["tests"]["stress"]["rows"][0]["change"] < 0
    assert set(ev["files"]) == {"trade", "market_data", "report_config"}
    assert ev["key"]["mock_market_data"] and ev["key"]["method"] == "analytic"
    assert "_trade_path" not in json.dumps(ev["config"])


def test_evidence_monte_carlo_pins_reference_levels(tmp_path, capsys):
    cfg = _config(tmp_path, EX / "autocallable_note.yaml", set=["pricing.num_paths=4000"],
                  convergence={"paths": [1000, 2000, 4000]}, seeds={"count": 3},
                  stress=[{"label": "crash", "spot": -0.3, "vol": 0.1}])
    code, out = _main(["evidence", cfg, "--out-dir", tmp_path / "ev"], capsys)
    assert code == 0 and out["ok"]
    ev = json.loads((tmp_path / "ev" / "evidence.json").read_text())
    status = {x["test"]: x["status"] for x in ev["summary"]}
    for t in ("Reproducibility (same seed)", "Martingale (forward) check", "Zero bump reproduces base"):
        assert status[t] == "PASS"
    assert ev["input"]["trade"]["reference_levels"] == {"SPX": 5000.0}
    assert [r["num_paths"] for r in ev["tests"]["convergence"]["rows"]] == [1000, 2000, 4000]
    assert len(ev["tests"]["seed_stability"]["rows"]) == 3
    spot = next(x for x in ev["tests"]["sensitivities"]["rows"] if x["factor"] == "spot")
    assert spot["price_up"] != spot["price_down"] and spot["first_order_se"] > 0   # strikes stay fixed
    assert ev["tests"]["stress"]["rows"][0]["change"] < 0                          # the KI put bites


def test_evidence_rejects_bad_config(tmp_path, capsys):
    cfg = _config(tmp_path, EX / "vanilla_call.yaml", stres=[])
    code, out = _main(["evidence", cfg, "--out-dir", tmp_path / "ev"], capsys)
    assert code == 2 and "stres" in out["error"]
    cfg = _config(tmp_path, tmp_path / "missing.yaml")
    assert _main(["evidence", cfg, "--out-dir", tmp_path / "ev"], capsys)[0] == 2


# ------------------------------------------------------------------ review fixes
def test_failed_variants_are_recorded_not_raised(tmp_path, capsys):
    """The largest path level, most seeds and a default stress may fail to price: evidence still completes."""
    cfg = _config(tmp_path, EX / "autocallable_note.yaml", set=["pricing.num_paths=2000"],
                  convergence={"paths": [1000, 2000, 10 ** 9]}, seeds={"count": 3},
                  stress=[{"label": "fine", "spot": -0.1}])
    real = report.price

    def flaky(doc, base, sets=(), keep_pv=False):
        if any(s.startswith("pricing.seed=") and not s.endswith("=8") for s in sets):
            return {"result": None, "pv": None, "error": "boom", "seconds": 0.0}
        return real(doc, base, sets, keep_pv)
    report.price = flaky
    try:
        code, out = _main(["evidence", cfg, "--out-dir", tmp_path / "ev"], capsys)
    finally:
        report.price = real
    ev = json.loads((tmp_path / "ev" / "evidence.json").read_text())
    status = {x["test"]: x for x in ev["summary"]}
    conv = ev["tests"]["convergence"]["rows"]
    assert conv[-1]["price"] is None and conv[1]["diff_vs_largest"] == 0        # the reference is 2000 paths
    assert status["Convergence in paths"]["status"] == "WARN" and "1000000000" in status["Convergence in paths"]["detail"]
    assert status["Seed stability"]["status"] == "WARN" and "only 1 of 3" in status["Seed stability"]["detail"]


def test_base_is_priced_with_pinned_levels(tmp_path, capsys):
    cfg = _config(tmp_path, EX / "autocallable_note.yaml", set=["pricing.num_paths=2000"],
                  convergence={"paths": [1000, 2000]}, seeds={"count": 0}, stress=[])
    _main(["evidence", cfg, "--out-dir", tmp_path / "ev"], capsys)
    ev = json.loads((tmp_path / "ev" / "evidence.json").read_text())
    z = ev["tests"]["implementation"]["zero_bump"]
    assert z["zero_bump_identical"] and z["unpinned_identical"] and z["unpinned_price"] == ev["key"]["price"]
    assert ev["environment"]["engine"]["report_sha256"]


def test_default_stress_not_applicable_is_skipped(tmp_path, capsys):
    trade = yaml.safe_load((EX / "vanilla_call.yaml").read_text())
    market = yaml.safe_load((EX / "market" / "mock_market.yaml").read_text())
    market["equities"]["SPX"]["vol"] = {"type": "constant", "value": 0.04}         # vol -5pt cannot apply
    trade["market_data"] = market
    (tmp_path / "t.yaml").write_text(yaml.safe_dump(trade))
    code, out = _main(["evidence", tmp_path / "t.yaml", "--out-dir", tmp_path / "ev"], capsys)
    ev = json.loads((tmp_path / "ev" / "evidence.json").read_text())
    row = next(x for x in ev["tests"]["stress"]["rows"] if x["label"] == "Vol -5pt")
    assert "skipped" in row and code == 0
    assert next(x for x in ev["summary"] if x["test"] == "Stress scenarios")["status"] == "INFO"
    # a user scenario that cannot apply is still a failure
    cfg = _config(tmp_path, tmp_path / "t.yaml", stress=[{"label": "too low", "vol": -0.05}])
    assert _main(["evidence", cfg, "--out-dir", tmp_path / "ev2"], capsys)[0] == 1


def test_config_set_applies_to_variant_trade_files(tmp_path, capsys):
    shutil_copy = (EX / "vanilla_call.yaml").read_text().replace("market/mock_market.yaml",
                                                                 str(EX / "market" / "mock_market.yaml"))
    (tmp_path / "bench.yaml").write_text(shutil_copy)
    cfg = _config(tmp_path, EX / "vanilla_call.yaml", set=["pricing.method=mc", "pricing.num_paths=3000"],
                  seeds={"count": 0}, stress=[], benchmarks=[{"label": "file", "trade": "bench.yaml"}])
    _main(["evidence", cfg, "--out-dir", tmp_path / "ev"], capsys)
    ev = json.loads((tmp_path / "ev" / "evidence.json").read_text())
    b = ev["tests"]["benchmarks"]["rows"][0]
    assert b["method"] == "mc" and b["price"] == ev["key"]["price"]             # same settings, same price


def test_materials_show_the_evidence(tmp_path, capsys):
    cfg = _config(tmp_path, EX / "callable_fixed_rate_bond_calibrated.yaml", set=["pricing.num_paths=2000"],
                  convergence={"paths": [1000, 2000]}, seeds={"count": 0},
                  stress=[{"label": "Rates +100bp", "rate": 0.01}])
    code, out = _main(["evidence", cfg, "--out-dir", tmp_path / "ev"], capsys)
    assert code == 0 and out["materials"].endswith("materials.md")
    ev = json.loads((tmp_path / "ev" / "evidence.json").read_text())
    text = (tmp_path / "ev" / "materials.md").read_text()
    assert text.startswith(f"# Report materials: {ev['key']['trade_id']}")
    assert "> **Warning:** the market data is mock" in text
    for title, _ in report.MATERIALS:
        assert f"\n## {title}\n" in text
    assert report._fmt(ev["key"]["price"]) in text                       # same rounding as the tables
    assert report._fmt(ev["tests"]["stress"]["rows"][0]["change"]) in text
    assert "| Calibration fit | methodology | **WARN** |" in text
    assert (ROOT / "templates" / "sr11-7.md").is_file()
