"""Validation report: the evidence is produced by engine runs only (bumps, stresses, benchmarks, cases), every status
follows its rule, and the rendered report fills numbers from the evidence and flags what a person still has to write."""
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


# ------------------------------------------------------------------ narrative and render
def test_render_fills_tokens_and_flags_gaps(tmp_path, capsys):
    cfg = _config(tmp_path, EX / "vanilla_call.yaml", stress=[])
    d = tmp_path / "ev"
    assert _main(["evidence", cfg, "--out-dir", d], capsys)[0] == 0
    code, out = _main(["narrative", d], capsys)
    assert code == 0 and (d / "narrative.yaml").exists()
    assert _main(["narrative", d], capsys)[0] == 2                                  # never overwritten

    code, out = _main(["render", d], capsys)                                        # skeleton: all missing
    assert code == 1 and "executive_summary" in out["missing_narrative"]
    assert "[TO BE COMPLETED" in (d / "report.md").read_text()

    ev = json.loads((d / "evidence.json").read_text())
    nar = {k: f"Text for {k}." for k in report.SECTIONS}
    nar["executive_summary"] = "Price {{key.price|,.2f}}, model {{key.model}}, bad {{key.nope}}."
    nar["findings"] = []
    (d / "narrative.yaml").write_text(yaml.safe_dump(nar))
    code, out = _main(["render", d], capsys)
    assert code == 1 and out["missing_narrative"] == [] and out["unresolved_tokens"][0]["token"] == "{{key.nope}}"
    text = (d / "report.md").read_text()
    assert f"Price {ev['key']['price']:,.2f}, model black_scholes" in text and "No findings." in text

    nar["executive_summary"] = "Price {{key.price|,.2f}}."
    (d / "narrative.yaml").write_text(yaml.safe_dump(nar))
    code, out = _main(["render", d, "--out", tmp_path / "r.md"], capsys)
    assert code == 0 and out["ok"]
    text = (tmp_path / "r.md").read_text()
    for heading in ("Executive summary", "Sensitivity analysis", "Stress testing", "Benchmarking",
                    "Outcomes analysis", "Ongoing monitoring", "Appendix A"):
        assert heading in text
    assert "Governance, policies and controls are out of scope" in text


def test_warn_requires_commentary(tmp_path, capsys):
    cfg = _config(tmp_path, EX / "callable_fixed_rate_bond_calibrated.yaml", set=["pricing.num_paths=2000"],
                  convergence={"paths": [1000, 2000]}, seeds={"count": 0}, stress=[])
    d = tmp_path / "ev"
    code, out = _main(["evidence", cfg, "--out-dir", d], capsys)
    assert code == 0 and any(x["test"] == "Calibration fit" and x["status"] == "WARN" for x in out["attention"])
    nar = {k: "x" for k in report.SECTIONS} | {"findings": []}
    (d / "narrative.yaml").write_text(yaml.safe_dump(nar))
    code, out = _main(["render", d], capsys)
    assert code == 1 and out["missing_narrative"] == ["commentary.methodology"]
    nar["commentary"] = {"methodology": "The short-expiry hump cannot be fitted by a constant sigma."}
    (d / "narrative.yaml").write_text(yaml.safe_dump(nar))
    assert _main(["render", d], capsys)[0] == 0


def test_user_template(tmp_path, capsys):
    cfg = _config(tmp_path, EX / "callable_fixed_rate_bond_calibrated.yaml", set=["pricing.num_paths=2000"],
                  convergence={"paths": [1000, 2000]}, seeds={"count": 0}, stress=[])
    d = tmp_path / "ev"
    _main(["evidence", cfg, "--out-dir", d], capsys)
    tpl = tmp_path / "house.md"
    tpl.write_text("# House valuation memo {{key.trade_id}}\n\n## Background\n\n{{narrative:background}}\n\n"
                   "## Price\n\n{{block:key_results}}\n\nPrice is {{key.price|,.0f}}.\n\n"
                   "## Approval\n\n{{manual:approval \"Sign-off per the valuation policy\"}}\n")
    code, out = _main(["narrative", d, "--template", tpl], capsys)
    assert code == 0 and out["narrative_keys"] == ["background"] and out["manual_keys"] == ["approval"]
    assert "stress" in out["evidence_not_in_template"] and (d / "template.md").read_text() == tpl.read_text()
    assert "background:" in (d / "narrative.yaml").read_text()

    (d / "narrative.yaml").write_text(yaml.safe_dump({"background": "Bond priced at {{key.price|,.2f}}."}))
    code, out = _main(["render", d], capsys)                           # uses the saved DIR/template.md
    # the calibration WARN must be discussed, and this template has no slot for it
    assert code == 1 and out["missing_narrative"] == [
        "commentary.methodology (template has no slot: add {{commentary:methodology}})"]
    assert out["manual_placeholders"] == ["approval"] and out["template"].endswith("template.md")
    text = (d / "report.md").read_text()
    ev = json.loads((d / "evidence.json").read_text())
    assert text.startswith("> **Warning:** the market data is mock")    # mock warning forced in
    assert f"# House valuation memo {ev['key']['trade_id']}" in text and f"Price is {ev['key']['price']:,.0f}." in text
    assert f"Bond priced at {ev['key']['price']:,.2f}." in text
    assert "[To be provided by the organisation: Sign-off per the valuation policy]" in text
    assert "SR 11-7" not in text

    bad = tmp_path / "bad.md"
    bad.write_text("{{block:nope}}")
    code, out = _main(["render", d, "--template", bad], capsys)
    assert code == 2 and "nope" in out["error"]


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


def test_findings_and_stale_template(tmp_path, capsys):
    cfg = _config(tmp_path, EX / "vanilla_call.yaml", stress=[])
    d = tmp_path / "ev"
    _main(["evidence", cfg, "--out-dir", d], capsys)
    _main(["narrative", d], capsys)
    nar = yaml.safe_load((d / "narrative.yaml").read_text())
    assert "findings" in nar and nar["findings"] is None                          # an explicit choice is needed
    nar.update({k: "x" for k in report.SECTIONS})
    (d / "narrative.yaml").write_text(yaml.safe_dump(nar))
    code, out = _main(["render", d], capsys)
    assert code == 1 and out["missing_narrative"] == ["findings"]

    tpl = tmp_path / "house.md"
    tpl.write_text("# Memo\n\n{{narrative:background}}\n")
    _main(["narrative", d, "--template", tpl, "--force"], capsys)
    assert (d / "template.md").exists()
    code, out = _main(["narrative", d, "--force"], capsys)                       # back to the default
    assert not (d / "template.md").exists() and out["template"].endswith("sr11-7.md")
    nar = {k: "x" for k in report.SECTIONS} | {"findings": []}
    (d / "narrative.yaml").write_text(yaml.safe_dump(nar))
    code, out = _main(["render", d], capsys)
    assert code == 0 and out["template"].endswith("sr11-7.md")
