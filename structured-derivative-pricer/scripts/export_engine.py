#!/usr/bin/env python
"""Copy this pricing engine into a project folder, so its trades can be re-priced without the skill or an AI.

    export_engine.py [DEST] [--trade TRADE ...] [--set k=v ...] [--upgrade]

DEST (default ./pricing_engine) receives a pinned, self-contained copy:
    pricer/ scripts/ reference/ examples/ tests/   the engine, its docs, examples and test suite
    requirements.lock.txt  .python-version         the exact library versions that produced the prices
    ENGINE.json                                    engine version (content hash), source, file checksums
    price.sh  baseline.sh  README.md               entry points and a guide written for people
    baselines/                                     recorded prices that `baseline.sh verify` re-checks
Each --trade is priced by the COPY in a fresh interpreter that cannot see the skill (proving the copy runs on
its own), compared to the skill's own price, and recorded as a baseline. The trades, market and fixings files
stay where they are; baselines refer to them by relative path.

DEST must be new, empty, or an earlier export. Re-exporting the same engine version refreshes the copy and keeps
baselines. A different version needs --upgrade: the old engine moves to DEST/archive/<version>/ (still runnable),
and every existing baseline is re-verified with the new engine.
Exit codes: 0 ok, 1 a copied-engine run failed or disagreed, 2 invalid input.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata as md
import json
import platform
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))
warnings.filterwarnings("ignore")

from pricer import apply_overrides, build_plan, load_document, run  # noqa: E402

TREES = ["pricer", "reference", "examples", "tests"]           # copied whole; replaced on every export
SCRIPTS = ["price.py", "baseline.py", "make_mock_market.py", "setup.sh"]
VERSIONED = ["pricer", "scripts/price.py", "requirements.txt"]  # the code that computes prices: the version hash
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "inspect_*", "test_export.py")  # the copy has no exporter


def _dump(obj):
    print(json.dumps(obj, indent=2, default=str))


def _files(root: Path, names: list[str]) -> list[Path]:
    out = []
    for n in names:
        p = root / n
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out += [f for f in sorted(p.rglob("*")) if f.is_file() and "__pycache__" not in f.parts
                    and f.suffix != ".pyc"]
    return out


def engine_version(root: Path = SKILL) -> str:
    """Content hash of the code that computes prices; the same code gives the same version on any machine."""
    h = hashlib.sha256()
    for f in _files(root, VERSIONED):
        h.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:12]


def _git() -> dict | None:
    try:
        sha = subprocess.run(["git", "-C", str(SKILL), "rev-parse", "HEAD"], capture_output=True, text=True,
                             check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(SKILL), "status", "--porcelain", "--", "."], capture_output=True,
                               text=True, check=True).stdout.strip()
        return {"commit": sha, "uncommitted_changes": bool(dirty)}
    except (OSError, subprocess.CalledProcessError):
        return None


def _locked_requirements() -> dict[str, str]:
    """Exact installed versions of the requirements, as this interpreter runs them."""
    pins = {}
    for line in (SKILL / "requirements.txt").read_text().splitlines():
        name = re.split(r"[<>=!~\[; ]", line.strip(), maxsplit=1)[0]
        if name and not name.startswith("#"):
            pins[name] = md.version(name)
    return pins


def _skill_price(trade: Path, sets: list[str]) -> float | None:
    doc, base = load_document(str(trade))
    try:
        apply_overrides(doc, sets)
        plan = build_plan(doc, base)
        return run(plan)["price"] if plan.report.ok else None
    except Exception:                  # reported as a missing skill price
        return None


def _run_copy(dest: Path, args: list[str]) -> tuple[int, dict]:
    """Run a script of the copy in an isolated interpreter (-I: no PYTHONPATH, no script-dir magic beyond its own)."""
    p = subprocess.run([sys.executable, "-I", *args], capture_output=True, text=True, cwd=Path.cwd())
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode or 1, {"ok": False, "stdout": p.stdout[-2000:], "stderr": p.stderr[-2000:]}


def _write(path: Path, text: str, executable: bool = False):
    path.write_text(text)
    if executable:
        path.chmod(0o755)


def _check_dest(dest: Path, version: str, upgrade: bool) -> tuple[str, str | None, dict | None]:
    """('new' | 'refresh' | 'upgrade', previous version, error)."""
    if dest.resolve() == SKILL or SKILL in dest.resolve().parents:
        return "", None, {"path": "DEST", "message": f"{dest} is inside the skill itself; export to the project folder"}
    if not dest.exists() or not any(dest.iterdir()):
        return "new", None, None
    meta = dest / "ENGINE.json"
    if not meta.exists():
        return "", None, {"path": "DEST", "message": f"{dest} is not empty and holds no ENGINE.json, so it is not an "
                                                     "earlier export; choose another folder"}
    old = json.loads(meta.read_text()).get("version")
    if old == version:
        return "refresh", old, None
    if not upgrade:
        return "", old, {"path": "--upgrade", "message": f"{dest} holds engine {old}; this skill is {version}. Pass "
                                                         "--upgrade to archive the old engine and verify every "
                                                         "baseline with the new one"}
    return "upgrade", old, None


def _archive(dest: Path, old: str) -> Path:
    arch = dest / "archive" / old
    if arch.exists():
        shutil.rmtree(arch)
    arch.mkdir(parents=True)
    for n in ["pricer", "scripts", "requirements.txt", "requirements.lock.txt", ".python-version", "ENGINE.json",
              "price.sh", "baseline.sh"]:
        if (dest / n).exists():
            shutil.move(str(dest / n), str(arch / n))
    return arch


def export(dest: Path, version: str) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    for n in TREES + ["scripts"]:
        if (dest / n).exists():
            shutil.rmtree(dest / n)
    for n in TREES:
        shutil.copytree(SKILL / n, dest / n, ignore=IGNORE)
    (dest / "scripts").mkdir()
    for s in SCRIPTS:
        shutil.copy2(SKILL / "scripts" / s, dest / "scripts" / s)
    shutil.copy2(SKILL / "requirements.txt", dest / "requirements.txt")

    pins = _locked_requirements()
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    _write(dest / "requirements.lock.txt",
           "# exact versions that produced the recorded prices; installed by scripts/setup.sh\n" +
           "".join(f"{k}=={v}\n" for k, v in pins.items()))
    _write(dest / ".python-version", pyver + "\n")
    _write(dest / "price.sh", WRAPPER.format(name="price", example="price ../trade.yaml"), executable=True)
    _write(dest / "baseline.sh", WRAPPER.format(name="baseline", example="verify"), executable=True)
    _write(dest / ".gitignore", ".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\n")
    _write(dest / "README.md", README.format(name=dest.name, version=version, date=dt.date.today().isoformat(), python=pyver,
                                             pins="\n".join(f"| {k} | {v} |" for k, v in pins.items())))
    meta = {
        "version": version,
        "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": {"skill": "structured-derivative-pricer", "path": str(SKILL), "git": _git()},
        "python": platform.python_version(),
        "platform": platform.platform(),
        "requirements": pins,
        "files": {f.relative_to(dest).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
                  for f in _files(dest, VERSIONED)},
    }
    _write(dest / "ENGINE.json", json.dumps(meta, indent=2) + "\n")
    return meta


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest", nargs="?", default="pricing_engine")
    ap.add_argument("--trade", action="append", default=[], help="trade to price with the copy and record as a "
                                                                 "baseline (repeatable)")
    ap.add_argument("--set", action="append", default=[], metavar="PATH=VALUE", help="override applied to every "
                                                                                     "--trade, as in price.py")
    ap.add_argument("--upgrade", action="store_true", help="replace an export of another engine version")
    a = ap.parse_args(argv)

    dest, version = Path(a.dest), engine_version()
    missing = [t for t in a.trade if not Path(t).is_file()]
    if missing:
        _dump({"ok": False, "errors": [{"path": "--trade", "message": f"trade file not found: {m}"} for m in missing]})
        return 2
    mode, old, err = _check_dest(dest, version, a.upgrade)
    if err:
        _dump({"ok": False, "errors": [{"severity": "error", **err}]})
        return 2
    archived = _archive(dest, old) if mode == "upgrade" else None
    export(dest, version)

    out = {"ok": True, "dest": str(dest.resolve()), "mode": mode, "version": version, "previous_version": old}
    if archived:
        out["archived_engine"] = str(archived)

    # the copy runs on its own: an isolated interpreter that imports only DEST's pricer
    code, smoke = _run_copy(dest, [str(dest / "scripts" / "price.py"), "validate",
                                   str(dest / "examples" / "vanilla_call.yaml")])
    out["standalone_check"] = "ok" if code == 0 and smoke.get("ok") else smoke
    ok = code == 0

    recorded = []
    for t in a.trade:
        sets = [x for s in a.set for x in ("--set", s)]
        code, res = _run_copy(dest, [str(dest / "scripts" / "baseline.py"), "record", t, *sets])
        row = {"trade": t, **res}
        if code == 0:
            skill = _skill_price(Path(t), a.set)
            row["skill_price"] = skill
            row["matches_skill"] = skill is not None and abs(res["price"] - skill) <= 1e-9 * max(1.0, abs(skill))
            ok &= row["matches_skill"]
        else:
            ok = False
        recorded.append(row)
    if recorded:
        out["baselines_recorded"] = recorded
    if mode == "upgrade":
        code, ver = _run_copy(dest, [str(dest / "scripts" / "baseline.py"), "verify"])
        out["baseline_verification"] = ver
        ok &= code == 0
    out["ok"] = ok
    out["next"] = {"reprice": f"{dest}/price.sh price <trade.yaml> [--set valuation_date=YYYY-MM-DD]",
                   "verify": f"{dest}/baseline.sh verify", "guide": f"{dest}/README.md"}
    _dump(out)
    return 0 if ok else 1


WRAPPER = """#!/usr/bin/env bash
# Runs scripts/{name}.py of this stand-alone engine; creates .venv with the locked versions on first use.
#   e.g. ./{name}.sh {example}
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$("$DIR/scripts/setup.sh")"
exec "$PY" "$DIR/scripts/{name}.py" "$@"
"""

README = """# Stand-alone pricing engine

Engine version **{version}**, exported {date} from the `structured-derivative-pricer` skill.
This folder holds everything needed to re-price your trades with no AI and no skill installed: the engine source,
the exact library versions, a guide to the trade YAML, and recorded prices to check the copy against.
Python {python}.

| package | version |
|---|---|
{pins}

## Set up (once per machine)

macOS / Linux: nothing to do. `./price.sh` creates `.venv/` with the locked versions on first use
(with [uv](https://docs.astral.sh/uv/) if installed, otherwise `python3 -m venv` + pip).

Windows (PowerShell):

```powershell
py -{python} -m venv .venv
.venv\\Scripts\\python -m pip install -r requirements.lock.txt
.venv\\Scripts\\python scripts\\price.py price ..\\trade.yaml
```

## Re-price

Run from anywhere; paths are relative to your current directory.

```bash
{name}/price.sh validate trade.yaml        # every input error at once, with hints
{name}/price.sh plan trade.yaml            # pricing method, component graph, fixing dates
{name}/price.sh price trade.yaml --out result.json
{name}/price.sh price trade.yaml --set pricing.num_paths=200000     # tighter Monte Carlo error
{name}/price.sh inspect trade.yaml --out-dir inspect_out            # every intermediate result as CSV
{name}/price.sh catalog leg                # every component, field, default and allowed value
```

**A new valuation date** (mark-to-market over the trade's life) needs no change to the trade itself:

1. write the new market data (curves, spot, vols) into a market file, e.g. `market/market_2026-12-31.yaml`
   (format: `reference/market_data.md`); `scripts/make_mock_market.py` shows the shape, with MOCK numbers;
2. append the closes / rate fixings since the last run to the fixings file the trade names;
3. `price.sh price trade.yaml --set valuation_date=2026-12-31 --set market_data=market/market_2026-12-31.yaml`
   (the `market_data` path is relative to the trade file), or edit those two lines in the trade.

`validate` lists every past observation date that has no fixing.

## Reading the result

| field | what to check |
|---|---|
| `method`, `method_reason` | analytic (closed form) or mc (Monte Carlo), and why |
| `price`, `std_error`, `confidence_interval` | if `std_error` is large relative to price, add paths |
| `legs` | PV per leg, signed from the holder's view |
| `leg_diagnostics`, `events` | knock-in / knock-out / autocall / exercise probabilities |
| `events.<id>.policy` | Bermudan exercise: `in_sample_price` within a few `std_error` of `price` |
| `cross_check` | when a closed form also applies, `agrees` must be true |
| `martingale_check` | `ok` must be true |
| `warnings`, `notes` | read every one |

Prices are exactly reproducible for a given `pricing.seed`, input and engine version.

## Check this copy: baselines

`baselines/` holds prices recorded when the engine was exported (or later): the trade, the hashes of its trade,
market and fixings files, and the result.

```bash
{name}/baseline.sh verify                  # re-price every baseline
{name}/baseline.sh record trade.yaml       # record a new one (e.g. a month-end mark)
```

`reproduced`: same inputs, same price. `inputs_changed`: a file was edited since recording, so the new price is
shown but not judged. `MISMATCH`: same inputs, different price, which means the engine or a library version
changed; check `requirements.lock.txt` against `.venv`.

## Writing or changing a trade

- `reference/components.md`: how term-sheet features map to components, sign conventions, product recipes.
- `reference/market_data.md`, `reference/exercise.md`: market data format, early exercise.
- `examples/`: one spec per product type; start from the closest one.
- `price.sh catalog [kind]`: the authoritative list of fields.

## Inside

```
pricer/          engine source (do not edit if you want baselines to keep reproducing)
scripts/         price.py (CLI), baseline.py, make_mock_market.py, setup.sh
tests/           the engine's regression suite: .venv/bin/python -m pytest tests -q
ENGINE.json      version = hash of pricer/, scripts/price.py, requirements.txt; source commit; file checksums
archive/<ver>/   earlier engine versions, kept runnable after an upgrade
```

From Python:

```python
import sys; sys.path.insert(0, "{name}")
from pricer import load_document, build_plan, run
doc, base = load_document("trade.yaml")
plan = build_plan(doc, base)
assert plan.report.ok, plan.report.to_dict()
print(run(plan)["price"])
```
"""

if __name__ == "__main__":
    sys.exit(main())
