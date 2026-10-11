#!/usr/bin/env python
"""Record and verify baseline prices of the engine this script belongs to.

    baseline.py record TRADE [--set k=v ...]   price TRADE and save the result with hashes of its input files
    baseline.py verify [NAME ...]              re-price every recorded baseline (or the named ones) and compare

A baseline is a fixed point: the trade file, its market and fixings files, the --set overrides and the
engine version that priced it. The engine is reproducible per seed, so re-pricing an unchanged input with
the same engine must give the same price. `verify` reports per baseline:
    reproduced      inputs unchanged, same price
    MISMATCH        inputs unchanged, different price (engine or library versions changed)
    inputs_changed  a trade / market / fixings file changed since recording; the new price is shown, not judged
    missing         a recorded file no longer exists
Baselines live in <engine>/baselines/, one JSON file per trade and valuation date.
Exit codes: 0 ok, 1 a MISMATCH or a failed run, 2 invalid input.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

from pricer import OverrideError, apply_overrides, build_plan, load_document, run  # noqa: E402

BASELINES = ROOT / "baselines"
REL_TOL = 1e-9


def _dump(obj):
    print(json.dumps(obj, indent=2, default=str))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path) -> str:
    """Path as stored: relative to the engine folder when possible, so the project folder can move as a whole."""
    try:
        return Path(os.path.relpath(path.resolve(), ROOT)).as_posix()
    except ValueError:                     # another drive on Windows
        return str(path.resolve())


def _engine_version() -> str | None:
    f = ROOT / "ENGINE.json"
    return json.loads(f.read_text()).get("version") if f.exists() else None


def _input_files(trade: Path, doc: dict) -> dict[str, Path]:
    """The files a price depends on: the trade, and the market / fixings documents it names by path."""
    files = {"trade": trade.resolve()}
    for key in ("market_data", "fixings"):
        if isinstance(doc.get(key), str):
            files[key] = (trade.resolve().parent / doc[key]).resolve()
    return files


def _price(trade: Path, sets: list[str]) -> tuple[dict | None, dict | None, dict]:
    """(doc, result, error): price the trade exactly as `price.py price` does."""
    doc, base = load_document(str(trade))
    try:
        apply_overrides(doc, sets)
    except OverrideError as e:
        return doc, None, {"stage": "overrides", "path": e.path, "message": str(e)}
    plan = build_plan(doc, base)
    if not plan.report.ok:
        return doc, None, {"stage": "validate", **plan.report.to_dict()}
    try:
        return doc, run(plan), {}
    except Exception as e:
        return doc, None, {"stage": "run", "error": f"{type(e).__name__}: {e}"}


def _summary(result: dict) -> dict:
    keep = ("trade_id", "valuation_date", "currency", "method", "price", "std_error", "confidence_interval", "legs")
    return {k: result[k] for k in keep if k in result}


def record(trade: Path, sets: list[str]) -> int:
    if not trade.exists():
        _dump({"ok": False, "error": f"trade file not found: {trade}"})
        return 2
    doc, result, err = _price(trade, sets)
    if result is None:
        _dump({"ok": False, **err})
        return 2 if err["stage"] != "run" else 1
    files = _input_files(trade, doc)
    missing = [str(p) for p in files.values() if not p.exists()]
    if missing:
        _dump({"ok": False, "error": f"input files not found: {missing}"})
        return 2
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{result['trade_id']}__{result['valuation_date']}")
    entry = {
        "name": name,
        "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
        "engine_version": _engine_version(),
        "trade": _rel(trade),
        "set": sets,
        "files": {k: {"path": _rel(p), "sha256": _sha256(p)} for k, p in files.items()},
        "result": _summary(result),
    }
    BASELINES.mkdir(exist_ok=True)
    (BASELINES / f"{name}.json").write_text(json.dumps(entry, indent=2, default=str) + "\n")
    _dump({"ok": True, "baseline": f"baselines/{name}.json", **entry["result"]})
    return 0


def _check(entry: dict) -> dict:
    out = {"name": entry["name"], "recorded_price": entry["result"]["price"],
           "recorded_engine": entry.get("engine_version"), "engine": _engine_version()}
    paths = {k: (ROOT / f["path"]).resolve() for k, f in entry["files"].items()}
    gone = [f["path"] for k, f in entry["files"].items() if not paths[k].exists()]
    if gone:
        return {**out, "status": "missing", "missing": gone}
    changed = [k for k, f in entry["files"].items() if _sha256(paths[k]) != f["sha256"]]
    _, result, err = _price(paths["trade"], entry.get("set", []))
    if result is None:
        return {**out, "status": "failed", **err}
    new, old = result["price"], entry["result"]["price"]
    out.update(price=new, diff=new - old)
    if changed:
        return {**out, "status": "inputs_changed", "changed": changed}
    same = abs(new - old) <= REL_TOL * max(1.0, abs(old))
    return {**out, "status": "reproduced" if same else "MISMATCH"}


def verify(names: list[str]) -> int:
    files = sorted(BASELINES.glob("*.json")) if BASELINES.exists() else []
    if names:
        wanted = {n.removesuffix(".json") for n in names}
        unknown = wanted - {f.stem for f in files}
        if unknown:
            _dump({"ok": False, "error": f"no baseline named {sorted(unknown)}", "available": [f.stem for f in files]})
            return 2
        files = [f for f in files if f.stem in wanted]
    checks = [_check(json.loads(f.read_text())) for f in files]
    ok = all(c["status"] in ("reproduced", "inputs_changed") for c in checks)
    _dump({"ok": ok, "engine": _engine_version(), "baselines": checks})
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("trade")
    r.add_argument("--set", action="append", default=[], metavar="PATH=VALUE")
    v = sub.add_parser("verify")
    v.add_argument("names", nargs="*")
    a = ap.parse_args(argv)
    return record(Path(a.trade), a.set) if a.cmd == "record" else verify(a.names)


if __name__ == "__main__":
    sys.exit(main())
