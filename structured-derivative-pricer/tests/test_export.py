"""Stand-alone export: the copied engine runs without the skill, reproduces the skill's price, and its baselines
detect changed inputs; a foreign folder or another engine version is not overwritten silently."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples"


def _run(args, cwd):
    p = subprocess.run([sys.executable, "-I", *map(str, args)], capture_output=True, text=True, cwd=cwd)
    return p.returncode, json.loads(p.stdout)


def _project(tmp_path):
    shutil.copy(EX / "vanilla_call.yaml", tmp_path / "trade.yaml")
    (tmp_path / "market").mkdir()
    shutil.copy(EX / "market" / "mock_market.yaml", tmp_path / "market")
    return tmp_path


def test_export_records_and_verifies(tmp_path):
    proj = _project(tmp_path)
    code, out = _run([ROOT / "scripts" / "export_engine.py", "--trade", "trade.yaml"], proj)
    assert code == 0 and out["ok"] and out["mode"] == "new" and out["standalone_check"] == "ok"
    (rec,) = out["baselines_recorded"]
    assert rec["matches_skill"] and rec["price"] == rec["skill_price"]

    dest = proj / "pricing_engine"
    meta = json.loads((dest / "ENGINE.json").read_text())
    assert meta["version"] == out["version"]
    assert set(meta["requirements"]) >= {"QuantLib", "numpy", "pydantic"}
    assert (dest / "requirements.lock.txt").read_text().count("==") == len(meta["requirements"])
    base = json.loads(next((dest / "baselines").glob("*.json")).read_text())
    assert base["trade"] == "../trade.yaml" and set(base["files"]) == {"trade", "market_data"}

    verify = [dest / "scripts" / "baseline.py", "verify"]
    code, v = _run(verify, proj)
    assert code == 0 and [b["status"] for b in v["baselines"]] == ["reproduced"]

    with open(proj / "market" / "mock_market.yaml", "a") as f:
        f.write("# edited\n")
    code, v = _run(verify, proj)
    assert code == 0 and v["baselines"][0]["status"] == "inputs_changed" and v["baselines"][0]["changed"] == ["market_data"]


def test_export_refuses_foreign_folder_and_other_version(tmp_path):
    proj = _project(tmp_path)
    (proj / "notes").mkdir()
    (proj / "notes" / "x.txt").write_text("mine")
    code, out = _run([ROOT / "scripts" / "export_engine.py", "notes"], proj)
    assert code == 2 and "ENGINE.json" in out["errors"][0]["message"]

    code, out = _run([ROOT / "scripts" / "export_engine.py"], proj)
    assert code == 0 and out["mode"] == "new"
    meta = proj / "pricing_engine" / "ENGINE.json"
    meta.write_text(json.dumps({**json.loads(meta.read_text()), "version": "older"}))
    code, out = _run([ROOT / "scripts" / "export_engine.py"], proj)
    assert code == 2 and out["errors"][0]["path"] == "--upgrade"
    code, out = _run([ROOT / "scripts" / "export_engine.py", "--upgrade"], proj)
    assert code == 0 and out["mode"] == "upgrade" and (proj / "pricing_engine" / "archive" / "older" / "pricer").is_dir()
