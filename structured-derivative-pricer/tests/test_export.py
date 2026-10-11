"""Stand-alone export: the copied engine runs without the skill, reproduces the skill's price, and its baselines
detect changed inputs; a foreign folder or another engine version is not overwritten silently, and a failed check
leaves DEST untouched."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples"
EXPORT = ROOT / "scripts" / "export_engine.py"


def _run(args, cwd):
    p = subprocess.run([sys.executable, "-I", *map(str, args)], capture_output=True, text=True, cwd=cwd)
    return p.returncode, json.loads(p.stdout)


def _project(tmp_path):
    shutil.copy(EX / "vanilla_call.yaml", tmp_path / "trade.yaml")
    (tmp_path / "market").mkdir()
    shutil.copy(EX / "market" / "mock_market.yaml", tmp_path / "market")
    return tmp_path


def _older(dest: Path):
    meta = dest / "ENGINE.json"
    meta.write_text(json.dumps({**json.loads(meta.read_text()), "version": "older"}))


def test_export_records_and_verifies(tmp_path):
    proj = _project(tmp_path)
    code, out = _run([EXPORT, "--trade", "trade.yaml"], proj)
    assert code == 0 and out["ok"] and out["mode"] == "new" and out["standalone_check"] == "ok"
    (rec,) = out["baselines_recorded"]
    assert rec["matches_skill"] and rec["price"] == rec["skill_price"]

    dest = proj / "pricing_engine"
    assert not any(p.name.endswith(".staging") for p in proj.iterdir())
    meta = json.loads((dest / "ENGINE.json").read_text())
    assert meta["version"] == out["version"]
    lock = (dest / "requirements.lock.txt").read_text()
    assert {"QuantLib", "numpy", "pydantic", "pydantic_core"} <= set(meta["requirements"])  # dependencies pinned too
    assert lock.count("==") == len(meta["requirements"]) and "requirements.lock.txt" in meta["files"]
    base = json.loads(next((dest / "baselines").glob("*.json")).read_text())
    assert base["trade"] == "../trade.yaml" and set(base["files"]) == {"trade", "market_data"}

    verify = [dest / "scripts" / "baseline.py", "verify"]
    code, v = _run(verify, proj)
    assert code == 0 and [b["status"] for b in v["baselines"]] == ["reproduced"]
    assert v["environment"] == {"engine_files_modified": [], "library_drift": {}}

    # an edited engine file is named, so a MISMATCH can be told apart from library drift
    with open(dest / "pricer" / "analytic.py", "a") as f:
        f.write("\n")
    assert _run(verify, proj)[1]["environment"]["engine_files_modified"] == ["pricer/analytic.py"]

    # changed inputs are not judged, even when they no longer price
    mkt = proj / "market" / "mock_market.yaml"
    mkt.write_text(mkt.read_text().replace("as_of:", "as_of_typo:"))
    code, v = _run(verify, proj)
    b = v["baselines"][0]
    assert code == 0 and b["status"] == "inputs_changed" and b["changed"] == ["market_data"] and "error" in b


def test_baselines_do_not_collide(tmp_path):
    proj = _project(tmp_path)
    shutil.copy(proj / "trade.yaml", proj / "trade_copy.yaml")       # same trade id, another file
    code, out = _run([EXPORT, "--trade", "trade.yaml", "--trade", "trade_copy.yaml"], proj)
    assert code == 0
    assert len(list((proj / "pricing_engine" / "baselines").glob("*.json"))) == 2
    code, out = _run([proj / "pricing_engine" / "scripts" / "baseline.py", "record", "trade.yaml",
                      "--set", "pricing.seed=7"], proj)                # other overrides: a third baseline
    assert code == 0 and len(list((proj / "pricing_engine" / "baselines").glob("*.json"))) == 3


def test_export_refuses_bad_destinations(tmp_path):
    proj = _project(tmp_path)
    (proj / "notes").mkdir()
    (proj / "notes" / "x.txt").write_text("mine")
    code, out = _run([EXPORT, "notes"], proj)
    assert code == 2 and "ENGINE.json" in out["errors"][0]["message"]
    (proj / "afile").write_text("x")
    code, out = _run([EXPORT, "afile"], proj)
    assert code == 2 and "is a file" in out["errors"][0]["message"]
    (proj / "broken").mkdir()
    (proj / "broken" / "ENGINE.json").write_text("{not json")
    code, out = _run([EXPORT, "broken"], proj)
    assert code == 2 and out["errors"][0]["path"] == "DEST/ENGINE.json"


def test_upgrade_archives_and_failed_check_leaves_dest(tmp_path):
    proj = _project(tmp_path)
    code, out = _run([EXPORT, "--trade", "trade.yaml"], proj)
    assert code == 0
    dest = proj / "pricing_engine"
    _older(dest)
    code, out = _run([EXPORT], proj)
    assert code == 2 and out["errors"][0]["path"] == "--upgrade"

    # a trade that does not price fails the check: nothing is archived or replaced
    (proj / "bad.yaml").write_text("valuation_date: 2026-09-28\n")
    before = (dest / "ENGINE.json").read_text()
    code, out = _run([EXPORT, "--upgrade", "--trade", "bad.yaml"], proj)
    assert code == 1 and out["dest_unchanged"] and (dest / "ENGINE.json").read_text() == before
    assert not (dest / "archive").exists() and not any(p.name.endswith(".staging") for p in proj.iterdir())

    code, out = _run([EXPORT, "--upgrade"], proj)
    assert code == 0 and out["mode"] == "upgrade"
    assert [b["status"] for b in out["baseline_verification"]["baselines"]] == ["reproduced"]
    arch = dest / "archive" / "older"
    assert (arch / "pricer").is_dir() and (arch / "baseline.sh").is_file()
    assert (arch / "report.sh").is_file() and (arch / "templates" / "sr11-7.md").is_file()   # report runs too
    # the archived engine still verifies the shared baselines
    code, v = _run([arch / "scripts" / "baseline.py", "verify", "--baselines", dest / "baselines"], proj)
    assert code == 0 and [b["status"] for b in v["baselines"]] == ["reproduced"]


def test_readme_names_nested_dest(tmp_path):
    proj = _project(tmp_path)
    code, out = _run([EXPORT, "vendor/engine"], proj)
    assert code == 0 and out["next"]["verify"] == "vendor/engine/baseline.sh verify"
    readme = (proj / "vendor" / "engine" / "README.md").read_text()
    assert "vendor/engine/price.sh price trade.yaml" in readme and 'sys.path.insert(0, "vendor/engine")' in readme
