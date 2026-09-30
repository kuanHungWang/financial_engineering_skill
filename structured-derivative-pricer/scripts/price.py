#!/usr/bin/env python
"""Structured derivative pricer CLI.

    price.py catalog [KIND]                  list component types and their fields (JSON)
    price.py validate INPUT [--set k=v ...]  check the input; print every error at once
    price.py plan INPUT [--set k=v ...]      validate + show pricing method and component graph
    price.py price INPUT [--set k=v ...] [--out result.json]

INPUT is a YAML/JSON file, '-' for stdin, or a literal YAML/JSON string.
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

from pricer import apply_overrides, build_plan, catalog, load_document, run  # noqa: E402


def _dump(obj):
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("catalog")
    c.add_argument("kind", nargs="?", choices=["curve", "vol", "schedule", "model", "observable", "leg",
                                               "condition", "event", "analytic"])
    for name in ("validate", "plan", "price"):
        p = sub.add_parser(name)
        p.add_argument("input")
        p.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                       help="override a field, e.g. --set pricing.num_paths=50000")
        if name == "price":
            p.add_argument("--out", help="also write the result JSON to this file")
    a = ap.parse_args(argv)

    if a.cmd == "catalog":
        _dump(catalog(a.kind))
        return 0

    try:
        doc, base = load_document(a.input)
    except Exception as e:  # unreadable YAML is an input error, not a crash
        _dump({"ok": False, "errors": [{"severity": "error", "path": "", "message": f"cannot parse input: {e}"}]})
        return 2
    apply_overrides(doc, a.set)
    plan = build_plan(doc, base)

    if a.cmd == "validate":
        _dump(plan.report.to_dict())
        return 0 if plan.report.ok else 2
    if a.cmd == "plan" or not plan.report.ok:
        _dump(plan.summary())
        return 0 if plan.report.ok else 2

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
