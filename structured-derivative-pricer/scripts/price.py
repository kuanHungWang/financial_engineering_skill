#!/usr/bin/env python
"""Structured derivative pricer CLI.

    price.py catalog [KIND]                  list component types and their fields (JSON)
    price.py validate INPUT [--set k=v ...]  check the input; print every error at once
    price.py plan INPUT [--set k=v ...]      validate + show pricing method and component graph
    price.py price INPUT [--set k=v ...] [--out result.json]
    price.py inspect INPUT [--set k=v ...] [--node KEY ...] [--out-dir DIR] [--max-paths N]
                                             price, then write intermediate results (paths, cashflows, curves,
                                             DCF, model parameters, ...) as CSV/JSON to DIR; prints only a
                                             small manifest, never the data

INPUT is a YAML/JSON file, '-' for stdin, or a literal YAML/JSON string.
--set PATH=VALUE: PATH is dot-separated; numeric segments index lists (trade.legs.0.rate,
or trade.legs[0].rate). VALUE is parsed as YAML.
Exit codes: 0 ok, 2 invalid input (see errors), 1 runtime failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from pricer import OverrideError, apply_overrides, build_plan, catalog, load_document, run  # noqa: E402
from pricer.inspection import InspectError, inspect, safe_name, select  # noqa: E402


def _dump(obj):
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("catalog")
    c.add_argument("kind", nargs="?", choices=["curve", "vol", "swaption_vol", "schedule", "model", "observable", "leg",
                                               "condition", "event", "analytic"])
    for name in ("validate", "plan", "price", "inspect"):
        p = sub.add_parser(name)
        p.add_argument("input")
        p.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                       help="override a field, e.g. --set pricing.num_paths=50000; "
                            "numeric segments index lists: --set trade.legs.0.rate=0.09")
        if name == "price":
            p.add_argument("--out", help="also write the result JSON to this file")
        if name == "inspect":
            p.add_argument("--node", action="append", default=[], metavar="KEY",
                           help="graph node to write (repeatable): an exact key from `plan` (leg:coupon, curve:USD) "
                                "or a kind (leg, curve, observable, fixings, price, ...); default: every node")
            p.add_argument("--out-dir", help="directory for the files (default: ./diagnostics/inspect_<trade id>); must be new, "
                                             "empty, or an earlier inspect output, whose listed files are replaced")
            p.add_argument("--max-paths", type=int, metavar="N",
                           help="write only the first N paths of path matrices, rounded up to keep antithetic "
                                "pairs whole (statistics still use all paths)")
    a = ap.parse_args(argv)

    if a.cmd == "catalog":
        _dump(catalog(a.kind))
        return 0

    try:
        doc, base = load_document(a.input)
    except Exception as e:  # unreadable YAML is an input error, not a crash
        _dump({"ok": False, "errors": [{"severity": "error", "path": "", "message": f"cannot parse input: {e}"}]})
        return 2
    try:
        apply_overrides(doc, a.set)
    except OverrideError as e:
        _dump({"ok": False, "errors": [{"severity": "error", "path": e.path, "message": str(e)}]})
        return 2
    plan = build_plan(doc, base)

    if a.cmd == "validate":
        _dump(plan.report.to_dict())
        return 0 if plan.report.ok else 2
    if a.cmd == "plan" or not plan.report.ok:
        _dump(plan.summary())
        return 0 if plan.report.ok else 2

    if a.cmd == "inspect":
        if a.max_paths is not None and a.max_paths < 1:
            _dump({"ok": False, "errors": [{"severity": "error", "path": "--max-paths",
                                            "message": f"must be a positive integer, got {a.max_paths}"}]})
            return 2
        try:
            select(plan, a.node)
        except InspectError as e:
            _dump({"ok": False, "errors": [{"severity": "error", "path": "--node", "message": str(e)}]})
            return 2
        out_dir = a.out_dir or f"diagnostics/inspect_{safe_name(plan.input.trade.id)}"
        try:
            _, manifest = inspect(plan, out_dir, a.node, a.max_paths)
        except InspectError as e:          # e.g. --out-dir holds files inspect did not write
            _dump({"ok": False, "errors": [{"severity": "error", "path": "--out-dir", "message": str(e)}]})
            return 2
        except Exception as e:
            _dump({"ok": False, "stage": "inspect", "error": f"{type(e).__name__}: {e}",
                   "traceback": traceback.format_exc().splitlines()[-6:]})
            return 1
        _dump(manifest)
        return 0

    try:
        result = run(plan)
    except Exception as e:
        _dump({"ok": False, "stage": "run", "error": f"{type(e).__name__}: {e}",
               "traceback": traceback.format_exc().splitlines()[-6:]})
        return 1
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=2, default=str))
    _dump(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
