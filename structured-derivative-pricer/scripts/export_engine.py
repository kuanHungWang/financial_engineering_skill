#!/usr/bin/env python
"""Copy this pricing engine into a project folder, so its trades can be re-priced without the skill or an AI.

    export_engine.py [DEST] [--trade TRADE ...] [--set k=v ...] [--upgrade] [--check-env]

DEST (default ./pricing_engine) receives a pinned, self-contained copy:
    pricer/ scripts/ reference/ examples/ tests/ templates/   the engine, its docs, examples, test suite and
                                                  report templates
    requirements.lock.txt  .python-version         every library (with its dependencies) at its exact version
    ENGINE.json                                    engine version, source, file checksums
    price.sh  baseline.sh  README.md               entry points and a guide written for people
    baselines/                                     recorded prices that `baseline.sh verify` re-checks
The version hashes the pricing code, the lock file and the Python version, so a library upgrade is a new version.

The copy is built in a staging folder beside DEST and checked there, by an interpreter that imports only the
copy's code (with the skill's own libraries, which the lock file pins): each --trade is priced, compared to the
skill's price and recorded as a baseline; on --upgrade every existing baseline is re-verified. DEST is replaced
only when every check passes; otherwise it is left untouched. --check-env then also builds the copy's own
locked .venv through price.sh (as a user will, network needed) and verifies the baselines with it.
The trades, market and fixings files stay where they are; baselines refer to them by relative path.

DEST must be new, empty, or an earlier export. Re-exporting the same version refreshes the copy and keeps
baselines. A different version needs --upgrade: the old engine moves to DEST/archive/<version>/, which can still
price and verify (`archive/<version>/baseline.sh verify --baselines DEST/baselines`).
Exit codes: 0 ok, 1 a check failed (DEST unchanged, except after --check-env), 2 invalid input.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata as md
import json
import os
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

TREES = ["pricer", "reference", "examples", "tests", "templates"]   # copied whole
SCRIPTS = ["price.py", "baseline.py", "report.py", "make_mock_market.py", "setup.sh"]
VERSIONED = ["pricer", "scripts/price.py", "requirements.txt", "requirements.lock.txt"]  # hashed into the version
RUNNABLE = ["pricer", "scripts", "requirements.txt", "requirements.lock.txt", ".python-version", "ENGINE.json",
            "price.sh", "baseline.sh", "report.sh", "templates"]  # what an archived engine keeps
ENGINE_OWNED = list(dict.fromkeys(TREES + RUNNABLE[1:] + ["README.md", ".gitignore"]))
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


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_requirements() -> dict[str, str]:
    """Exact installed version of every requirement and of everything it pulls in, as this interpreter runs them."""
    todo = []
    for line in (SKILL / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):                  # option lines (-r, --index-url, -e) name no package
            todo.append(re.split(r"[<>=!~\[; @]", line, maxsplit=1)[0])
    for name in todo:
        md.distribution(name)                                  # a direct requirement must be installed
    pins: dict[str, str] = {}
    while todo:
        try:
            dist = md.distribution(todo.pop())
        except md.PackageNotFoundError:                        # a dependency for another platform or Python
            continue
        name = dist.metadata["Name"]
        if _canon(name) in map(_canon, pins):
            continue
        pins[name] = dist.version
        todo += [re.split(r"[<>=!~\[; (@]", r, maxsplit=1)[0] for r in dist.requires or []
                 if not re.search(r"extra\s*==", r)]           # optional extras are not installed by the lock
    return dict(sorted(pins.items(), key=lambda kv: _canon(kv[0])))


def lock_text(pins: dict[str, str]) -> str:
    return (f"# python {sys.version_info.major}.{sys.version_info.minor}: every package at the version that produced "
            "the recorded prices; installed by scripts/setup.sh\n" + "".join(f"{k}=={v}\n" for k, v in pins.items()))


def engine_version(lock: str) -> str:
    """Hash of the pricing code and the locked environment: the same pair gives the same version on any machine."""
    h = hashlib.sha256()
    for f in _files(SKILL, VERSIONED[:-1]):
        h.update(f.relative_to(SKILL).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    h.update(b"requirements.lock.txt\0" + lock.encode())
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


def _skill_price(trade: Path, sets: list[str]) -> float | None:
    doc, base = load_document(str(trade))
    try:
        apply_overrides(doc, sets)
        plan = build_plan(doc, base)
        return run(plan)["price"] if plan.report.ok else None
    except Exception:                  # reported as a missing skill price
        return None


def _json_run(cmd: list[str]) -> tuple[int, dict]:
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=Path.cwd())
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode or 1, {"ok": False, "stdout": p.stdout[-2000:], "stderr": p.stderr[-2000:]}


def _run_copy(script: Path, args: list[str]) -> tuple[int, dict]:
    """Run a script of the copy in an isolated interpreter (-I: no PYTHONPATH, no user site); it imports only the
    copy's pricer, with this interpreter's libraries."""
    return _json_run([sys.executable, "-I", str(script), *args])


def _write(path: Path, text: str, executable: bool = False):
    path.write_text(text)
    if executable:
        path.chmod(0o755)


def _check_dest(dest: Path, version: str, upgrade: bool) -> tuple[str, str | None, dict | None]:
    """('new' | 'refresh' | 'upgrade', previous version, error)."""
    if dest.resolve() == SKILL or SKILL in dest.resolve().parents:
        return "", None, {"path": "DEST", "message": f"{dest} is inside the skill itself; export to the project folder"}
    if dest.exists() and not dest.is_dir():
        return "", None, {"path": "DEST", "message": f"{dest} is a file, not a folder"}
    if not dest.exists() or not any(dest.iterdir()):
        return "new", None, None
    meta = dest / "ENGINE.json"
    if not meta.is_file():
        return "", None, {"path": "DEST", "message": f"{dest} is not empty and holds no ENGINE.json, so it is not an "
                                                     "earlier export; choose another folder"}
    try:
        old = json.loads(meta.read_text())["version"]
        if not isinstance(old, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", old):
            raise ValueError(f"bad version {old!r}")
    except (ValueError, KeyError, TypeError) as e:
        return "", None, {"path": "DEST/ENGINE.json", "message": f"unreadable engine metadata ({e}); restore it from "
                                                                 "version control or export to a new folder"}
    if old == version:
        return "refresh", old, None
    if not upgrade:
        return "", old, {"path": "--upgrade", "message": f"{dest} holds engine {old}; this skill is {version}. Pass "
                                                         "--upgrade to archive the old engine and verify every "
                                                         "baseline with the new one"}
    return "upgrade", old, None


def build(stage: Path, version: str, pins: dict[str, str], lock: str, shown: str) -> dict:
    """Write the engine into an empty `stage` folder; `shown` is how the README names the final folder."""
    stage.mkdir(parents=True)
    for n in TREES:
        shutil.copytree(SKILL / n, stage / n, ignore=IGNORE)
    (stage / "scripts").mkdir()
    for s in SCRIPTS:
        shutil.copy2(SKILL / "scripts" / s, stage / "scripts" / s)
    shutil.copy2(SKILL / "requirements.txt", stage / "requirements.txt")
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    _write(stage / "requirements.lock.txt", lock)
    _write(stage / ".python-version", pyver + "\n")
    _write(stage / "price.sh", WRAPPER.format(name="price", example="price ../trade.yaml"), executable=True)
    _write(stage / "baseline.sh", WRAPPER.format(name="baseline", example="verify"), executable=True)
    _write(stage / "report.sh", WRAPPER.format(name="report", example="evidence ../trade.yaml --out-dir ../report"),
           executable=True)
    _write(stage / ".gitignore", ".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\n")
    direct = {_canon(re.split(r"[<>=!~\[; @]", x.split("#", 1)[0].strip(), maxsplit=1)[0])
              for x in (SKILL / "requirements.txt").read_text().splitlines()}
    _write(stage / "README.md", README.format(
        name=shown, version=version, date=dt.date.today().isoformat(), python=pyver, n_pins=len(pins),
        pins="\n".join(f"| {k} | {v} |" for k, v in pins.items() if _canon(k) in direct)))
    meta = {
        "version": version,
        "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": {"skill": "structured-derivative-pricer", "path": str(SKILL), "git": _git()},
        "python": platform.python_version(),
        "platform": platform.platform(),
        "requirements": pins,
        "files": {f.relative_to(stage).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
                  for f in _files(stage, VERSIONED)},
    }
    _write(stage / "ENGINE.json", json.dumps(meta, indent=2) + "\n")
    return meta


def check(stage: Path, mode: str, trades: list[str], sets: list[str]) -> tuple[bool, dict]:
    """Price with the staged copy: smoke test, record each trade, re-verify old baselines on an upgrade."""
    out: dict = {}
    code, smoke = _run_copy(stage / "scripts" / "price.py", ["validate", str(stage / "examples" / "vanilla_call.yaml")])
    out["standalone_check"] = "ok" if code == 0 and smoke.get("ok") else smoke
    ok = out["standalone_check"] == "ok"
    rows = []
    for t in trades:
        code, res = _run_copy(stage / "scripts" / "baseline.py",
                              ["record", t, *[x for s in sets for x in ("--set", s)]])
        row = {"trade": t, **res}
        if code == 0:
            skill = _skill_price(Path(t), sets)
            row["skill_price"] = skill
            row["matches_skill"] = skill is not None and abs(res["price"] - skill) <= 1e-9 * max(1.0, abs(skill))
            ok &= row["matches_skill"]
        else:
            ok = False
        rows.append(row)
    if rows:
        out["baselines_recorded"] = rows
    if mode == "upgrade":
        code, ver = _run_copy(stage / "scripts" / "baseline.py", ["verify"])
        out["baseline_verification"] = ver
        ok &= code == 0
    return ok, out


def swap(stage: Path, dest: Path, mode: str, old: str | None) -> Path | None:
    """Move the checked copy into DEST: the old engine goes to archive/<old>/ on an upgrade, is replaced otherwise."""
    dest.mkdir(parents=True, exist_ok=True)
    arch = None
    if mode == "upgrade":
        arch = dest / "archive" / old
        if arch.exists():
            shutil.rmtree(arch)
        arch.mkdir(parents=True)
    for n in ENGINE_OWNED:
        p = dest / n
        if not p.exists():
            continue
        if arch is not None and n in RUNNABLE:
            shutil.move(str(p), str(arch / n))
        elif p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    if (stage / "baselines").exists():                # the staged baselines hold the old ones plus the new
        if (dest / "baselines").exists():
            shutil.rmtree(dest / "baselines")
    for p in stage.iterdir():
        shutil.move(str(p), str(dest / p.name))
    stage.rmdir()
    return arch


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest", nargs="?", default="pricing_engine")
    ap.add_argument("--trade", action="append", default=[], help="trade to price with the copy and record as a "
                                                                 "baseline (repeatable)")
    ap.add_argument("--set", action="append", default=[], metavar="PATH=VALUE", help="override applied to every "
                                                                                     "--trade, as in price.py")
    ap.add_argument("--upgrade", action="store_true", help="replace an export of another engine version")
    ap.add_argument("--check-env", action="store_true", help="after installing, build DEST's own locked .venv via "
                                                             "price.sh and verify the baselines with it (slow, network)")
    a = ap.parse_args(argv)

    dest = Path(a.dest)
    shown = Path(os.path.relpath(dest.resolve(), Path.cwd())).as_posix()
    missing = [t for t in a.trade if not Path(t).is_file()]
    if missing:
        _dump({"ok": False, "errors": [{"path": "--trade", "message": f"trade file not found: {m}"} for m in missing]})
        return 2
    try:
        pins = locked_requirements()
    except md.PackageNotFoundError as e:
        _dump({"ok": False, "errors": [{"path": "requirements.txt", "message": f"not installed in this Python: {e}; "
                                                                               "run scripts/setup.sh"}]})
        return 2
    lock = lock_text(pins)
    version = engine_version(lock)
    mode, old, err = _check_dest(dest, version, a.upgrade)
    if err:
        _dump({"ok": False, "errors": [{"severity": "error", **err}]})
        return 2

    # build and check beside DEST (same depth, so baselines' relative paths resolve alike), then swap in
    stage = dest.resolve().parent / f".{dest.name}.staging"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        build(stage, version, pins, lock, shown)
        if (dest / "baselines").is_dir():
            shutil.copytree(dest / "baselines", stage / "baselines")
        ok, checks = check(stage, mode, a.trade, a.set)
        out = {"ok": ok, "dest": str(dest.resolve()), "mode": mode, "version": version, "previous_version": old,
               **checks}
        if not ok:
            out["dest_unchanged"] = True
            _dump(out)
            return 1
        archived = swap(stage, dest, mode, old)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if archived:
        out["archived_engine"] = str(archived)

    if a.check_env:                                   # the user's path: price.sh -> setup.sh -> locked .venv
        has_baselines = any((dest / "baselines").glob("*.json"))
        cmd = [str(dest / "baseline.sh"), "verify"] if has_baselines else \
              [str(dest / "price.sh"), "validate", str(dest / "examples" / "vanilla_call.yaml")]
        code, env = _json_run(cmd)
        out["env_check"] = env if code or not env.get("ok") else "ok"
        out["ok"] = ok = code == 0 and env.get("ok", False)
    out["next"] = {"reprice": f"{shown}/price.sh price <trade.yaml> [--set valuation_date=YYYY-MM-DD]",
                   "verify": f"{shown}/baseline.sh verify", "guide": f"{shown}/README.md"}
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
Python {python}. Main packages below; `requirements.lock.txt` pins all {n_pins}, dependencies included.

| package | version |
|---|---|
{pins}

## Set up (once per machine)

macOS / Linux: nothing to do. `./price.sh` creates `.venv/` with the locked versions on first use
(with [uv](https://docs.astral.sh/uv/) if installed, otherwise `python{python}` or `python3` + pip), and rebuilds it
whenever `requirements.lock.txt` or `.python-version` changes.

Windows (PowerShell):

```powershell
py -{python} -m venv .venv                          # after an engine upgrade: delete .venv and repeat
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

`reproduced`: same inputs, same price. `inputs_changed`: a file was edited since recording, so the new price (or the
error, e.g. missing fixings) is shown but not judged. `MISMATCH`: same inputs, different price. The `environment`
part of the output names the cause: engine files edited since export (`engine_files_modified`) or installed
library versions that differ from the lock (`library_drift`).

## Validation report

`report.sh` produces the material for a model documentation and validation report: convergence, seed stability,
sensitivities, stress, benchmarks, limiting cases, reference prices, as tables in `report/materials.md` (and
`report/evidence.json`). Config: `reference/model_report.md`. The report is written from that material, following
your own template or the SR 11-7 outline in `templates/sr11-7.md`.

```bash
{name}/report.sh evidence trade.yaml --out-dir report     # or a report config naming the trade
```

## Writing or changing a trade

- `reference/components.md`: how term-sheet features map to components, sign conventions, product recipes.
- `reference/market_data.md`, `reference/exercise.md`: market data format, early exercise.
- `examples/`: one spec per product type; start from the closest one.
- `price.sh catalog [kind]`: the authoritative list of fields.

## Inside

```
pricer/          engine source (do not edit if you want baselines to keep reproducing)
scripts/         price.py (CLI), baseline.py, report.py, make_mock_market.py, setup.sh
tests/           the engine's regression suite: .venv/bin/python -m pytest tests -q
templates/       report outline (sr11-7.md, the default for validation reports)
ENGINE.json      version = hash of pricer/, scripts/price.py and the lock file; source commit; file checksums
archive/<ver>/   earlier engine versions, kept runnable after an upgrade
```

An archived engine prices and verifies on its own; point it at the shared baselines:

```bash
{name}/archive/<ver>/baseline.sh verify --baselines {name}/baselines
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
