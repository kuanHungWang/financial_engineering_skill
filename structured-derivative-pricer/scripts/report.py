#!/usr/bin/env python
"""Model documentation and validation report. Default layout: SR 11-7 (governance, policies and controls excluded);
a user's own format is given as a template.

    report.py evidence INPUT --out-dir DIR [--run-tests]   run the validation tests, write DIR/evidence.json
    report.py narrative DIR [--template FILE]               write DIR/narrative.yaml, the sections a person writes;
                                                            a user template is saved as DIR/template.md
    report.py render DIR [--template FILE] [--narrative FILE] [--out FILE]
                                                            merge template + evidence + narrative into DIR/report.md
    report.py blocks                                        the tags and evidence blocks a template can use

INPUT is a trade YAML (every test at its default) or a report config YAML that names the trade (`trade: FILE`)
and sets the tests; see reference/model_report.md. Every number in the report comes from evidence.json, which only
this script writes, by pricing the trade and variants of it with the engine (bump-and-revalue on the same seed,
stress scenarios, other seeds and path counts, benchmark trades, limiting cases). The narrative holds the text a
person must write (purpose, methodology, limitations, conclusions); it quotes numbers with {{path|format}} tokens
that `render` fills from evidence.json, e.g. {{key.price|,.2f}}. A template (default templates/sr11-7.md) is Markdown
that places evidence blocks and narrative slots with tags; see the tag list above BLOCKS below.

stdout is a small JSON summary; progress goes to stderr.
Exit codes: 0 ok, 1 a test FAILED or a run crashed (evidence) / narrative incomplete (render), 2 invalid input.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.metadata as md
import json
import math
import platform
import re
import subprocess
import sys
import time
import warnings
from pathlib import Path
from statistics import NormalDist

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

from pricer import OverrideError, apply_overrides, build_plan, load_document, run  # noqa: E402
from pricer.engine import INSPECT_FLOWS  # noqa: E402

SCHEMA = 1
Z_TOL = 4.0                 # statistical tolerance, in combined standard errors
CALIBRATION_TOLERANCE = 0.05
DEFAULT_BUMPS = {"spot": 0.01, "vol": 0.01, "rate": 0.0001, "dividend": 0.0001, "swaption_vol": None,
                 "model_volatility": 0.0001}
EQUITY_STRESS = [
    {"label": "Spot -30%", "spot": -0.30}, {"label": "Spot -20%", "spot": -0.20},
    {"label": "Spot -10%", "spot": -0.10}, {"label": "Spot +10%", "spot": 0.10},
    {"label": "Spot +20%", "spot": 0.20}, {"label": "Vol -5pt", "vol": -0.05},
    {"label": "Vol +5pt", "vol": 0.05}, {"label": "Vol +10pt", "vol": 0.10},
    {"label": "Rates -100bp", "rate": -0.01}, {"label": "Rates +100bp", "rate": 0.01},
    {"label": "Crash: spot -30%, vol +10pt", "spot": -0.30, "vol": 0.10},
]
RATES_STRESS = [
    {"label": "Rates -200bp", "rate": -0.02}, {"label": "Rates -100bp", "rate": -0.01},
    {"label": "Rates +100bp", "rate": 0.01}, {"label": "Rates +200bp", "rate": 0.02},
    {"label": "Swaption vols x0.75", "swaption_vol_scale": 0.75},
    {"label": "Swaption vols x1.25", "swaption_vol_scale": 1.25},
    {"label": "Rates +100bp, swaption vols x1.25", "rate": 0.01, "swaption_vol_scale": 1.25},
]
CONFIG_KEYS = {"trade", "set", "convergence", "seeds", "precision", "bumps", "custom_bumps", "stress", "cases",
               "benchmarks", "references"}


class InputError(ValueError):
    pass


def _dump(obj):
    print(json.dumps(obj, indent=2, default=str))


def _log(msg):
    print(f"[report] {msg}", file=sys.stderr, flush=True)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plain(x):
    """JSON-safe copy: numpy scalars to Python, NaN/inf to None, dates to ISO."""
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    return x


# ------------------------------------------------------------------ loading
def load_config(path: str) -> tuple[dict, dict, Path, dict]:
    """(config, trade doc, trade base dir, input files). The doc has config `set` applied and its market inlined."""
    p = Path(path)
    if not p.is_file():
        raise InputError(f"input file not found: {path}")
    raw = yaml.safe_load(p.read_text())
    if not isinstance(raw, dict):
        raise InputError("input must be a YAML mapping")
    if isinstance(raw.get("trade"), str):           # a report config naming its trade
        unknown = set(raw) - CONFIG_KEYS
        if unknown:
            raise InputError(f"unknown config key(s) {sorted(unknown)}; allowed: {sorted(CONFIG_KEYS)}")
        cfg, trade = raw, (p.resolve().parent / raw["trade"]).resolve()
    else:
        cfg, trade = {"trade": p.name}, p.resolve()
    cfg_dir = p.resolve().parent
    if not trade.is_file():
        raise InputError(f"trade file not found: {trade}")
    doc, base = load_document(str(trade))
    files = {"trade": trade}
    if trade != p.resolve():
        files["report_config"] = p.resolve()
    try:
        apply_overrides(doc, cfg.get("set", []))
    except OverrideError as e:
        raise InputError(f"set {e.path}: {e}") from None
    for key in ("market_data", "fixings"):
        if isinstance(doc.get(key), str):
            f = (base / doc[key]).resolve()
            if not f.is_file():
                raise InputError(f"{key} file not found: {f}")
            files[key] = f
    if "market_data" in files:
        doc["market_data"] = yaml.safe_load(files["market_data"].read_text())
    for kind in ("benchmarks", "cases"):
        for item in cfg.get(kind) or []:
            if not isinstance(item, dict) or "label" not in item:
                raise InputError(f"every entry of {kind} needs a label")
            if item.get("trade"):
                item["_trade_path"] = (cfg_dir / item["trade"]).resolve()
                if not item["_trade_path"].is_file():
                    raise InputError(f"{kind} {item['label']!r}: trade file not found: {item['_trade_path']}")
                files[f"{kind[:-1]}:{item['label']}"] = item["_trade_path"]
    return cfg, doc, base, files


# ------------------------------------------------------------------ pricing a variant
def price(doc: dict, base: Path, sets=(), keep_pv=False) -> dict:
    """{'result', 'pv' (per path, MC only), 'error', 'seconds'} for the doc with `sets` applied."""
    d = copy.deepcopy(doc)
    t = time.perf_counter()
    try:
        apply_overrides(d, list(sets))
    except OverrideError as e:
        return {"result": None, "pv": None, "error": f"set {e.path}: {e}", "seconds": 0.0}
    plan = build_plan(d, base)
    if not plan.report.ok:
        errs = "; ".join(f"{e['path']}: {e['message']}" for e in plan.report.to_dict()["errors"])
        return {"result": None, "pv": None, "error": f"invalid: {errs}", "seconds": 0.0}
    keep = {} if keep_pv else None
    try:
        res = run(plan, keep)
    except Exception as e:  # a variant that crashes is evidence, not a crash of the report
        return {"result": None, "pv": None, "error": f"{type(e).__name__}: {e}", "seconds": 0.0}
    pv = keep["results"].get(INSPECT_FLOWS, {}).get("pv") if keep else None
    return {"result": res, "pv": pv, "error": None, "seconds": round(time.perf_counter() - t, 2)}


def _pair_se(x: np.ndarray, antithetic: bool) -> float:
    s = (x[0::2] + x[1::2]) / 2 if antithetic else x
    return float(s.std(ddof=1) / math.sqrt(len(s))) if len(s) > 1 else float("nan")


# ------------------------------------------------------------------ market bumps
def _bump_curve(spec: dict, h: float) -> None:
    t = spec.get("type")
    if t == "flat":
        spec["rate"] += h
    elif t == "zero":
        spec["rates"] = [r + h for r in spec["rates"]]
    elif t == "bootstrap":
        for k in ("deposit", "swap"):
            for q in spec.get(k) or []:
                q["rate"] += h
    else:
        raise InputError(f"cannot bump a curve of type {t!r}")


def _bump_vol(spec: dict, h: float) -> None:
    t = spec.get("type")
    if t == "constant":
        spec["value"] += h
    elif t == "term":
        spec["vols"] = [v + h for v in spec["vols"]]
    elif t == "surface":
        spec["vols"] = [[v + h for v in row] for row in spec["vols"]]
    else:
        raise InputError(f"cannot bump a vol of type {t!r}")
    vals = [spec["value"]] if t == "constant" else np.ravel(spec["vols"]).tolist()
    if min(vals) <= 0:
        raise InputError(f"vol bump {h:+g} makes a vol non-positive")


def bump_market(market: dict, underlyings: list, spot=0.0, vol=0.0, rate=0.0, dividend=0.0, swaption_vol=0.0,
                swaption_vol_scale=1.0) -> dict:
    """A bumped copy: spot relative, vol / rate / dividend / swaption_vol absolute, swaption_vol_scale a factor.
    rate shifts every discount/forecast curve in parallel (quotes of a bootstrap curve), not dividend curves."""
    m = copy.deepcopy(market)
    for u in underlyings:
        eq = m["equities"][u]
        eq["spot"] *= 1 + spot
        if vol:
            _bump_vol(eq["vol"], vol)
        if dividend:
            eq.setdefault("dividend", {"type": "flat", "rate": 0.0})
            _bump_curve(eq["dividend"], dividend)
    if rate:
        for spec in (m.get("curves") or {}).values():
            _bump_curve(spec, rate)
    if swaption_vol or swaption_vol_scale != 1.0:
        for spec in (m.get("swaption_vols") or {}).values():
            spec["vols"] = [[(v + swaption_vol) * swaption_vol_scale for v in row] for row in spec["vols"]]
    return m


def _underlyings(doc: dict) -> list:
    mdl = doc.get("model") or {}
    if mdl.get("underlyings"):
        return list(mdl["underlyings"])
    return [mdl["underlying"]] if mdl.get("underlying") else []


def _with_market(doc: dict, market: dict) -> dict:
    d = copy.copy(doc)
    d["market_data"] = market
    return d


# ------------------------------------------------------------------ expectations
def _rhs(spec, ctx: dict):
    """(value, std_error) of an expectation's right-hand side: a number, 'base', 'case:LABEL' or 'benchmark:LABEL'."""
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        return float(spec), 0.0
    if spec == "base":
        return ctx["base"]
    if isinstance(spec, str) and spec.partition(":")[0] in ("case", "benchmark"):
        kind, _, label = spec.partition(":")
        if label not in ctx[kind]:
            raise InputError(f"expect refers to unknown {kind} {label!r} (only earlier entries can be named)")
        return ctx[kind][label]
    raise InputError(f"expect value must be a number, 'base', 'case:LABEL' or 'benchmark:LABEL', got {spec!r}")


def judge(expect: dict | None, p: float, se: float, ctx: dict) -> dict:
    """PASS/FAIL of `p` against expect {approx|le|ge|lt|gt: rhs, tol: abs | tol_se: k}; INFO without expect."""
    if not expect:
        return {"status": "INFO"}
    ops = [k for k in ("approx", "le", "ge", "lt", "gt") if k in expect]
    if len(ops) != 1:
        raise InputError(f"expect needs exactly one of approx/le/ge/lt/gt, got {sorted(expect)}")
    op = ops[0]
    rv, rse = _rhs(expect[op], ctx)
    comb = math.hypot(se or 0.0, rse or 0.0)
    tol = float(expect["tol"]) if "tol" in expect else float(expect.get("tol_se", Z_TOL)) * comb
    diff = p - rv
    ok = {"approx": abs(diff) <= tol, "le": diff <= tol, "lt": diff < tol,
          "ge": diff >= -tol, "gt": diff > -tol}[op]
    return {"status": "PASS" if ok else "FAIL", "expected": f"{op} {expect[op]}", "reference_value": rv,
            "difference": diff, "tolerance": tol}


# ------------------------------------------------------------------ the tests
class Evidence:
    def __init__(self, cfg, doc, base, files, run_tests: bool):
        self.cfg, self.doc, self.base, self.files, self.run_tests = cfg, doc, base, files, run_tests
        self.tests, self.summary = {}, []
        self.underlyings = _underlyings(doc)
        self.model_type = (doc.get("model") or {}).get("type")

    def add(self, test, section, status, detail):
        self.summary.append({"test": test, "section": section, "status": status, "detail": detail})

    def build(self) -> dict:
        _log("base price")
        b = price(self.doc, self.base, keep_pv=True)
        if b["result"] is None:
            raise InputError(f"the trade does not price: {b['error']}")
        self.b, r = b, b["result"]
        self.mc = r["method"] == "mc"
        self.anti = bool(r.get("antithetic"))
        plan = build_plan(copy.deepcopy(self.doc), self.base)
        # pin the initial fixings: a relative strike or barrier must not move with a spot bump (by default it is
        # today's spot); the zero-bump check below confirms the pinned trade prices as the original
        refs = dict(getattr(plan.v, "reference_levels", None) or {})
        if refs:
            self.doc["trade"]["reference_levels"] = {**refs, **(self.doc["trade"].get("reference_levels") or {})}
        self.pinned_reference_levels = refs
        self.process(plan)
        deterministic = self.mc and (r.get("std_error") or 0.0) <= 1e-9 * max(1.0, abs(r["price"]))
        if deterministic:
            self.add("Convergence", "convergence", "SKIPPED",
                     "zero sampling error: the price does not depend on the simulated paths under this model")
        elif self.mc:
            self.convergence()
            self.seed_stability()
        else:
            self.add("Convergence", "convergence", "SKIPPED", "analytic method: no sampling error")
        self.precision()
        self.sensitivities()
        self.stress()
        ctx = {"base": (r["price"], r.get("std_error") or 0.0), "case": {}, "benchmark": {}}
        self.benchmarks(ctx)
        self.cases(ctx)
        self.references()
        self.unit_tests()
        return self.assemble(plan)

    # -- implementation and process verification
    def process(self, plan):
        r = self.b["result"]
        out = {}
        warn = plan.report.to_dict()["warnings"]
        out["validation"] = {"errors": 0, "warnings": warn}
        self.add("Input validation", "implementation", "PASS" if not warn else "WARN",
                 "no warnings" if not warn else f"{len(warn)} validation warning(s)")
        again = price(self.doc, self.base)
        same = again["result"] is not None and again["result"]["price"] == r["price"]
        out["reproducibility"] = {"first": r["price"], "second": again["result"] and again["result"]["price"],
                                  "identical": same}
        self.add("Reproducibility (same seed)", "implementation", "PASS" if same else "FAIL",
                 "two runs give the identical price" if same else "two runs with the same seed differ")
        if self.mc:
            mc = r.get("martingale_check", {})
            ok = all(c["ok"] for c in mc.values())
            out["martingale_check"] = mc
            worst = max((abs(c["z_score"]) for c in mc.values()), default=0.0)
            self.add("Martingale (forward) check", "implementation", "PASS" if ok else "FAIL",
                     f"max |z| = {worst:.2f} (limit 4)")
        if r.get("cross_check"):
            cc = r["cross_check"]
            out["cross_check"] = cc
            self.add("MC vs closed form", "benchmarking", "PASS" if cc["agrees"] else "FAIL",
                     f"gap {cc['mc_minus_analytic']:.6g} = {cc['gap_in_std_errors']:.2f} std errors")
        for eid, ev in (r.get("events") or {}).items():
            pol = ev.get("policy") if isinstance(ev, dict) else None
            if pol and "in_sample_price" in pol and not pol.get("in_sample"):
                gap = (pol["in_sample_price"] - r["price"]) / r["std_error"] if r.get("std_error") else 0.0
                out.setdefault("exercise_policy", {})[eid] = {"in_sample_price": pol["in_sample_price"],
                                                             "out_of_sample_price": r["price"], "gap_in_se": gap}
                self.add(f"Exercise policy in/out of sample ({eid})", "implementation",
                         "PASS" if abs(gap) <= Z_TOL else "WARN", f"gap {gap:.2f} std errors")
        cal = (r.get("model_diagnostics") or {}).get("calibration")
        if cal and cal.get("instruments"):
            misses = [abs(f["vol_error"] / f["market_vol"]) for f in cal["instruments"]
                      if f.get("vol_error") is not None and f.get("market_vol")]
            worst = max(misses, default=0.0)
            out["calibration"] = {"worst_relative_vol_miss": worst, "instruments": len(cal["instruments"])}
            self.add("Calibration fit", "methodology", "PASS" if worst <= CALIBRATION_TOLERANCE else "WARN",
                     f"worst instrument misses its market vol by {worst:.1%} (limit {CALIBRATION_TOLERANCE:.0%})")
        z = price(_with_market(self.doc, bump_market(self.doc["market_data"], self.underlyings)), self.base)
        same = z["result"] is not None and abs(z["result"]["price"] - r["price"]) <= 1e-9 * max(1.0, abs(r["price"]))
        out["zero_bump"] = {"identical": same, "pinned_reference_levels": self.pinned_reference_levels}
        self.add("Zero bump reproduces base", "implementation", "PASS" if same else "FAIL",
                 "unbumped market inlined, reference levels pinned: same price as base" if same else
                 "a zero bump changes the price: sensitivities are not trustworthy")
        self.tests["implementation"] = out

    # -- convergence in the number of paths, and dispersion across seeds
    def convergence(self):
        r = self.b["result"]
        n = r["num_paths"]
        conv = (self.cfg.get("convergence") or {}).get("paths")
        paths = sorted({int(x) for x in conv}) if conv else \
            sorted({max(min(n, 500), n // k) for k in (16, 8, 4, 2)} | {n})
        if self.anti:
            paths = sorted({p + p % 2 for p in paths})
        rows = []
        for p in paths:
            _log(f"convergence: {p} paths")
            v = r if p == n else (price(self.doc, self.base, [f"pricing.num_paths={p}"])["result"] or {})
            rows.append({"num_paths": p, "price": v.get("price"), "std_error": v.get("std_error"),
                         "confidence_interval": v.get("confidence_interval")})
        ref = rows[-1]
        ok_rows = [x for x in rows if x["price"] is not None and x["std_error"]]
        for x in ok_rows:
            comb = math.hypot(x["std_error"], ref["std_error"]) if x is not ref else 0.0
            x["diff_vs_largest"] = x["price"] - ref["price"]
            x["within_tolerance"] = x is ref or abs(x["diff_vs_largest"]) <= Z_TOL * comb
        slope = None
        if len(ok_rows) >= 3:
            slope = float(np.polyfit(np.log([x["num_paths"] for x in ok_rows]),
                                     np.log([x["std_error"] for x in ok_rows]), 1)[0])
        ok = len(ok_rows) == len(rows) and all(x["within_tolerance"] for x in ok_rows) and \
            (slope is None or -0.65 <= slope <= -0.35)
        self.tests["convergence"] = {"rows": rows, "se_slope": slope, "expected_slope": -0.5}
        shown = "n/a (fewer than 3 levels)" if slope is None else f"{slope:.2f}"
        self.add("Convergence in paths", "convergence", "PASS" if ok else "WARN",
                 f"std-error slope {shown} (expect -0.5); "
                 + (f"every level within {Z_TOL:g} SE of the largest" if ok else
                    f"slope outside -0.65..-0.35 or a level more than {Z_TOL:g} SE from the largest"))

    def seed_stability(self):
        r = self.b["result"]
        k = int((self.cfg.get("seeds") or {}).get("count", 8))
        if k < 3:
            return
        rows = []
        for i in range(k):
            s = int(r["seed"]) + 1 + i
            _log(f"seed {s}")
            v = price(self.doc, self.base, [f"pricing.seed={s}"])["result"] or {}
            rows.append({"seed": s, "price": v.get("price"), "std_error": v.get("std_error")})
        ps = [x["price"] for x in rows if x["price"] is not None]
        sd = float(np.std(ps, ddof=1)) if len(ps) > 1 else float("nan")
        mse = float(np.mean([x["std_error"] for x in rows if x["std_error"]]))
        ratio = sd / mse if mse else float("nan")
        lo, hi = _chi_ratio_bounds(len(ps) - 1)
        ok = len(ps) == k and lo <= ratio <= hi
        self.tests["seed_stability"] = {"rows": rows, "mean_price": float(np.mean(ps)), "std_across_seeds": sd,
                                        "mean_reported_se": mse, "ratio": ratio, "ratio_99pct_bounds": [lo, hi]}
        self.add("Seed stability", "convergence", "PASS" if ok else "WARN",
                 f"std across {k} seeds / reported SE = {ratio:.2f} (99% band {lo:.2f}-{hi:.2f})")

    def precision(self):
        r = self.b["result"]
        lim = (self.cfg.get("precision") or {}).get("max_std_error")
        se = r.get("std_error")
        self.tests["precision"] = {"price": r["price"], "std_error": se, "max_std_error": lim,
                                   "relative_std_error": se / abs(r["price"]) if se and r["price"] else None}
        if lim is not None and se is not None:
            self.add("Pricing precision", "convergence", "PASS" if se <= lim else "FAIL",
                     f"std error {se:.6g} vs limit {lim:g}")

    # -- sensitivities: bump and revalue on the same seed (common random numbers)
    def _sens_items(self):
        bumps = {**DEFAULT_BUMPS, **(self.cfg.get("bumps") or {})}
        mkt = self.doc["market_data"]
        items = []
        for u in self.underlyings:
            s0 = mkt["equities"][u]["spot"]
            items.append({"label": f"Delta / gamma {u}", "factor": "spot", "underlying": u, "bump": bumps["spot"],
                          "unit": "relative spot move", "scale": s0 * bumps["spot"],
                          "mk": lambda h, u=u: bump_market(mkt, [u], spot=h)})
            items.append({"label": f"Vega {u}", "factor": "vol", "underlying": u, "bump": bumps["vol"],
                          "unit": "absolute vol (all points)", "mk": lambda h, u=u: bump_market(mkt, [u], vol=h)})
            items.append({"label": f"Dividend {u}", "factor": "dividend", "underlying": u, "bump": bumps["dividend"],
                          "unit": "absolute dividend yield", "mk": lambda h, u=u: bump_market(mkt, [u], dividend=h)})
        if mkt.get("curves"):
            items.append({"label": "Rates (all curves, parallel)", "factor": "rate", "bump": bumps["rate"],
                          "unit": "absolute rate on every curve quote", "mk": lambda h: bump_market(mkt, [], rate=h)})
        mdl = self.doc.get("model") or {}
        if self.model_type == "hull_white":
            if mdl.get("calibration") and mkt.get("swaption_vols"):
                name = mdl["calibration"].get("swaption_vol")
                vt = (mkt["swaption_vols"].get(name) or {}).get("vol_type", "normal")
                h = bumps["swaption_vol"] or (0.0001 if vt == "normal" else 0.01)
                items.append({"label": "Swaption vega (all points)", "factor": "swaption_vol", "bump": h,
                              "unit": f"absolute {vt} swaption vol", "mk": lambda h: bump_market(mkt, [], swaption_vol=h)})
            elif mdl.get("volatility") is not None:
                h, v0 = bumps["model_volatility"], mdl["volatility"]
                items.append({"label": "Hull-White sigma", "factor": "model_volatility", "bump": h,
                              "unit": "absolute model volatility",
                              "sets": lambda s, v0=v0, h=h: [f"model.volatility={v0 + s * h}"]})
        for c in self.cfg.get("custom_bumps") or []:
            if not {"label", "up", "down"} <= set(c):
                raise InputError("custom_bumps entries need label, up and down (lists of PATH=VALUE)")
            items.append({"label": c["label"], "factor": "custom", "bump": c.get("size", 1.0),
                          "unit": c.get("unit", "as defined by the up/down overrides"),
                          "sets": lambda s, c=c: c["up"] if s > 0 else c["down"]})
        return items

    def sensitivities(self):
        r, base_pv = self.b["result"], self.b["pv"]
        rows = []
        for it in self._sens_items():
            _log(f"sensitivity: {it['label']}")
            row = {k: it[k] for k in ("label", "factor", "bump", "unit") if k in it}
            try:
                if "sets" in it:
                    up, dn = (price(self.doc, self.base, it["sets"](s), keep_pv=True) for s in (1, -1))
                else:
                    up, dn = (price(_with_market(self.doc, it["mk"](s * it["bump"])), self.base, keep_pv=True)
                              for s in (1, -1))
            except InputError as e:
                rows.append({**row, "error": str(e)})
                continue
            if up["result"] is None or dn["result"] is None:
                rows.append({**row, "error": up["error"] or dn["error"]})
                continue
            pu, pd, p0 = up["result"]["price"], dn["result"]["price"], r["price"]
            row.update(price_up=pu, price_down=pd, first_order=(pu - pd) / 2, second_order=pu - 2 * p0 + pd)
            if self.mc and up["pv"] is not None and dn["pv"] is not None:
                row["first_order_se"] = _pair_se((up["pv"] - dn["pv"]) / 2, self.anti)
                row["second_order_se"] = _pair_se(up["pv"] - 2 * base_pv + dn["pv"], self.anti)
                row["significant"] = bool(abs(row["first_order"]) > 2 * row["first_order_se"]) \
                    if row["first_order_se"] > 0 else bool(row["first_order"] != 0)
            if "scale" in it:            # spot: per unit of the underlying
                row["delta"] = row["first_order"] / it["scale"]
                row["gamma"] = row["second_order"] / it["scale"] ** 2
            rows.append(row)
        self.tests["sensitivities"] = {"method": "central bump-and-revalue, same seed and paths (common random "
                                                 "numbers); first_order = (P+ - P-)/2 per bump, second_order = "
                                                 "P+ - 2P0 + P-", "rows": rows}
        bad = [x["label"] for x in rows if "error" in x]
        self.add("Sensitivities", "sensitivity", "FAIL" if bad else "INFO",
                 f"{len(rows) - len(bad)} computed" + (f"; failed: {bad}" if bad else ""))

    # -- stress scenarios
    def stress(self):
        r = self.b["result"]
        scen = self.cfg.get("stress")
        if scen is None:
            scen = EQUITY_STRESS if self.underlyings else RATES_STRESS if self.model_type == "hull_white" else []
            if not self.underlyings:
                scen = [s for s in scen if "swaption_vol_scale" not in s or self._calibrated()]
        dims = ("spot", "vol", "rate", "dividend", "swaption_vol", "swaption_vol_scale")
        rows = []
        for s in scen:
            extra = set(s) - set(dims) - {"label", "set"}
            if extra or "label" not in s:
                raise InputError(f"stress scenario needs a label and only {dims} or set; got {sorted(s)}")
            _log(f"stress: {s['label']}")
            row = {"label": s["label"], "shocks": {k: s[k] for k in dims if k in s}, "set": s.get("set", [])}
            try:
                m = bump_market(self.doc["market_data"], self.underlyings, **row["shocks"])
            except InputError as e:
                rows.append({**row, "error": str(e)})
                continue
            v = price(_with_market(self.doc, m), self.base, row["set"])
            if v["result"] is None:
                rows.append({**row, "error": v["error"]})
                continue
            p = v["result"]["price"]
            row.update(price=p, std_error=v["result"].get("std_error"), change=p - r["price"],
                       change_pct=(p - r["price"]) / abs(r["price"]) if r["price"] else None,
                       warnings=len(v["result"].get("warnings", [])))
            rows.append(row)
        self.tests["stress"] = {"rows": rows, "base_price": r["price"],
                                "note": "spot shocks are relative; vol, rate and dividend shocks absolute; "
                                        "swaption_vol_scale multiplies every swaption vol. Same seed as base. "
                                        "Initial fixings (reference levels) stay at their base values."}
        bad = [x["label"] for x in rows if "error" in x]
        self.add("Stress scenarios", "stress", "FAIL" if bad else "INFO",
                 f"{len(rows) - len(bad)} scenario(s) priced" + (f"; failed: {bad}" if bad else ""))

    def _calibrated(self):
        return bool((self.doc.get("model") or {}).get("calibration"))

    # -- benchmarking, limiting cases, outcomes
    def _variant(self, item):
        if item.get("_trade_path"):
            d, b = load_document(str(item["_trade_path"]))
            return price(d, b, item.get("set", []))
        if not item.get("set"):
            raise InputError(f"{item['label']!r} needs a trade file or a set list")
        return price(self.doc, self.base, item["set"])

    def benchmarks(self, ctx):
        rows = []
        r = self.b["result"]
        for item in self.cfg.get("benchmarks") or []:
            _log(f"benchmark: {item['label']}")
            v = self._variant(item)
            row = {"label": item["label"], "description": item.get("description"), "trade": item.get("trade"),
                   "set": item.get("set", [])}
            if v["result"] is None:
                rows.append({**row, "status": "FAIL", "error": v["error"]})
                continue
            p, se = v["result"]["price"], v["result"].get("std_error") or 0.0
            comb = math.hypot(se, r.get("std_error") or 0.0)
            row.update(price=p, std_error=se, method=v["result"]["method"], diff_vs_base=p - r["price"],
                       diff_in_se=(p - r["price"]) / comb if comb else None,
                       **judge(item.get("expect"), p, se, ctx))
            ctx["benchmark"][item["label"]] = (p, se)
            rows.append(row)
        self.tests["benchmarks"] = {"rows": rows}
        for x in rows:
            self.add(f"Benchmark: {x['label']}", "benchmarking", x["status"],
                     x.get("error") or f"diff vs base {x['diff_vs_base']:.6g}")

    def cases(self, ctx):
        rows = []
        for item in self.cfg.get("cases") or []:
            _log(f"case: {item['label']}")
            v = self._variant(item)
            row = {"label": item["label"], "rationale": item.get("rationale"), "trade": item.get("trade"),
                   "set": item.get("set", [])}
            if v["result"] is None:
                rows.append({**row, "status": "FAIL", "error": v["error"]})
                continue
            p, se = v["result"]["price"], v["result"].get("std_error") or 0.0
            row.update(price=p, std_error=se, **judge(item.get("expect"), p, se, ctx))
            ctx["case"][item["label"]] = (p, se)
            rows.append(row)
        self.tests["cases"] = {"rows": rows}
        for x in rows:
            self.add(f"Case: {x['label']}", "cases", x["status"],
                     x.get("error") or (f"{x['expected']}: diff {x['difference']:.6g}, tol {x['tolerance']:.6g}"
                                        if "expected" in x else f"price {x['price']:.6g}"))

    def references(self):
        r = self.b["result"]
        rows = []
        for ref in self.cfg.get("references") or []:
            if not {"label", "price"} <= set(ref):
                raise InputError("references entries need label and price")
            diff = r["price"] - float(ref["price"])
            row = {**{k: ref.get(k) for k in ("label", "price", "source", "date")}, "model_price": r["price"],
                   "difference": diff, "difference_pct": diff / abs(ref["price"]) if ref["price"] else None}
            tol = ref.get("tolerance")
            if tol is None and ref.get("tolerance_pct") is not None:
                tol = abs(float(ref["price"])) * float(ref["tolerance_pct"])
            row["tolerance"] = tol
            row["status"] = "INFO" if tol is None else "PASS" if abs(diff) <= tol else "FAIL"
            rows.append(row)
            self.add(f"Reference: {row['label']}", "outcomes", row["status"], f"model - reference = {diff:.6g}")
        self.tests["references"] = {"rows": rows}

    def unit_tests(self):
        if not self.run_tests:
            self.tests["unit_tests"] = {"run": False}
            return
        _log("engine test suite (about a minute)")
        t = time.perf_counter()
        p = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q", "-p", "no:cacheprovider"],
                           capture_output=True, text=True, cwd=ROOT)
        last = (p.stdout.strip().splitlines() or [""])[-1]
        counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|errors?|skipped)", last)}
        self.tests["unit_tests"] = {"run": True, "returncode": p.returncode, "summary": last, "counts": counts,
                                    "seconds": round(time.perf_counter() - t, 1)}
        self.add("Engine test suite", "implementation", "PASS" if p.returncode == 0 else "FAIL", last)

    # -- assembly
    def assemble(self, plan) -> dict:
        r = self.b["result"]
        mkt = self.doc["market_data"]
        text = self.files["market_data"].read_text() if "market_data" in self.files else json.dumps(mkt)
        key = {"trade_id": r["trade_id"], "valuation_date": r["valuation_date"], "currency": r["currency"],
               "method": r["method"], "model": self.model_type, "price": r["price"],
               "std_error": r.get("std_error"), "confidence": r.get("confidence"),
               "ci_low": (r.get("confidence_interval") or [None, None])[0],
               "ci_high": (r.get("confidence_interval") or [None, None])[1],
               "num_paths": r.get("num_paths"), "seed": r.get("seed"),
               "mock_market_data": bool(re.search(r"\bmock\b", text, re.I))}
        counts = {s: sum(1 for x in self.summary if x["status"] == s)
                  for s in ("PASS", "WARN", "FAIL", "INFO", "SKIPPED")}
        return _plain({
            "schema": SCHEMA,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "key": key,
            "summary": self.summary,
            "status_counts": counts,
            "base": r,
            "plan": plan.summary(),
            "input": self.doc_as_priced(),
            "market": market_summary(mkt, self.underlyings),
            "known_limitations": known_limitations(self.doc, r),
            "tests": self.tests,
            "config": _public(self.cfg),
            "files": {k: {"path": str(p), "sha256": _sha256(p)} for k, p in self.files.items()},
            "environment": environment(),
        })

    def doc_as_priced(self):
        d = copy.deepcopy(self.doc)
        if "market_data" in self.files:
            d["market_data"] = str(self.files["market_data"])
        return d


def _public(x):
    """The config as written: without the keys this script adds (resolved paths)."""
    if isinstance(x, dict):
        return {k: _public(v) for k, v in x.items() if not str(k).startswith("_")}
    return [_public(v) for v in x] if isinstance(x, list) else x


def _chi_ratio_bounds(df: int, p: float = 0.99) -> tuple[float, float]:
    """Bounds of sample std / true std at probability p (Wilson-Hilferty chi-square quantiles)."""
    def q(a):
        z = NormalDist().inv_cdf(a)
        return df * (1 - 2 / (9 * df) + z * math.sqrt(2 / (9 * df))) ** 3
    return math.sqrt(max(q((1 - p) / 2), 0) / df), math.sqrt(q((1 + p) / 2) / df)


def market_summary(m: dict, underlyings: list) -> dict:
    out = {"as_of": m.get("as_of"), "curves": {}, "equities": {}, "swaption_vols": {}}
    for n, c in (m.get("curves") or {}).items():
        quotes = len(c.get("deposit") or []) + len(c.get("swap") or []) if c.get("type") == "bootstrap" else \
            len(c.get("rates") or []) if c.get("type") == "zero" else 1
        out["curves"][n] = {"type": c.get("type"), "quotes": quotes}
    for u, e in (m.get("equities") or {}).items():
        v = e.get("vol") or {}
        out["equities"][u] = {"spot": e.get("spot"), "vol_type": v.get("type"), "used_by_model": u in underlyings,
                              "strike_mode": v.get("strike_mode"), "dividend": (e.get("dividend") or {}).get("type")}
    for n, s in (m.get("swaption_vols") or {}).items():
        out["swaption_vols"][n] = {"vol_type": s.get("vol_type"), "expiries": len(s.get("expiries") or []),
                                   "tenors": len(s.get("tenors") or [])}
    return out


def known_limitations(doc: dict, r: dict) -> list:
    """Limitations that follow from the engine's model and method choices; the narrative adds product-specific ones."""
    mdl = doc.get("model") or {}
    t, out = mdl.get("type"), []
    mkt = doc.get("market_data") or {}
    trade = doc.get("trade") or {}
    events = [e.get("type") for e in trade.get("termination_events") or []]
    legs = [l.get("type") for l in trade.get("legs") or []]
    if t == "black_scholes":
        vols = {(mkt.get("equities", {}).get(u) or {}).get("vol", {}).get("type") for u in _underlyings(doc)}
        out.append("Interest rates and dividends are deterministic; no rate-equity correlation is modelled.")
        if "surface" in vols:
            out.append("Dupire local volatility fits today's smile exactly but implies a forward smile that flattens; "
                       "products sensitive to forward skew (cliquets, forward-starting, autocall chains) may be "
                       "mispriced relative to stochastic-volatility models.")
        if vols & {"constant", "term"}:
            out.append("A constant or term-structure volatility ignores the smile: barrier and digital features are "
                       "priced without skew.")
        if len(_underlyings(doc)) > 1:
            out.append("Correlation between underlyings is constant and deterministic; no correlation skew.")
        out.append("No jumps: gap risk through barriers is not captured beyond what the volatility input implies.")
    elif t == "heston":
        out.append("Single-asset Heston with deterministic rates and dividends; no jumps.")
        out.append("Heston parameters are " + ("given, not fitted to today's surface." if mdl.get("params") else
                   "calibrated to the whole surface; a five-parameter model cannot fit every quote, so short-dated "
                   "wings may be off."))
    elif t == "hull_white":
        out.append("One-factor Hull-White: all rates along the curve are perfectly correlated, so spread "
                   "(e.g. CMS spread) and curve-shape optionality is not captured.")
        out.append("Gaussian short rate: rates can become negative; constant mean reversion and volatility "
                   "cannot fit the whole swaption surface.")
        out.append("Basis between forecast curves and the discount curve is deterministic.")
    if r.get("method") == "mc":
        out.append("Monte Carlo: the price carries sampling error (see std error); barriers and ranges are observed "
                   "only on the dates of their schedules.")
    if "bermudan_call" in events:
        out.append("Bermudan exercise by Longstaff-Schwartz regression on an independent path set: the policy is "
                   "sub-optimal by construction, so the holder's-option value is a lower bound; the quality depends "
                   "on the regressors.")
    if "path_dependent" in legs:
        out.append("Path-dependent coupons are given as formulas in the trade specification; their correctness "
                   "rests on matching the term sheet wording.")
    if r.get("method") == "analytic":
        out.append("Closed-form pricing assumes the model's dynamics exactly (e.g. continuous barrier monitoring "
                   "for barrier formulas).")
    return out


def environment() -> dict:
    def ver(p):
        try:
            return md.version(p)
        except md.PackageNotFoundError:
            return None
    h = hashlib.sha256()
    for f in sorted((ROOT / "pricer").rglob("*.py")):
        h.update(f.relative_to(ROOT).as_posix().encode() + b"\0" + f.read_bytes())
    engine = {"code_sha256": h.hexdigest()[:16]}
    try:
        engine["export_version"] = json.loads((ROOT / "ENGINE.json").read_text()).get("version")
    except (OSError, ValueError):
        pass
    try:
        engine["git_commit"] = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return {"python": platform.python_version(), "platform": platform.platform(),
            "packages": {p: ver(p) for p in ("QuantLib", "numpy", "pandas", "pydantic", "PyYAML")},
            "engine": engine}




# ------------------------------------------------------------------ templates, narrative and rendering
# A template is Markdown with tags. The default (templates/sr11-7.md) follows SR 11-7; a user's own format is
# written as a template with the same tags, in the user's headings and order (see reference/model_report.md).
#   {{block:NAME}}                    a table / list generated from the evidence (BLOCKS)
#   {{narrative:KEY "guidance"}}      text a person writes in narrative.yaml under KEY (required)
#   {{commentary:SECTION "guidance"}} comment on an evidence section (required when a test there is WARN / FAIL)
#   {{manual:KEY "guidance"}}         content only the organisation can supply (governance, sign-off, ...); filled
#                                     from narrative.yaml `manual:` if given, else a visible placeholder
#   {{path.in.evidence|format}}       one number or value from evidence.json
DEFAULT_TEMPLATE = ROOT / "templates" / "sr11-7.md"
GUIDANCE = {  # guidance for the default narrative keys; a template's own "guidance" wins
    "executive_summary": "What was priced, the price with its confidence interval, the method and model, whether "
                         "the data is mock, and the main findings and limitations in a few sentences.",
    "purpose_and_use": "Business purpose (valuation / MTM / risk), users, the product scope the model covers, and "
                       "uses it is NOT intended for.",
    "product_description": "The term sheet in plain words: underlyings, dates, coupons, barriers and their "
                           "observation, early termination, who holds which right; then how each feature maps to an "
                           "engine component.",
    "methodology": "Model dynamics and why they suit this product's risk drivers; numerical method and why; "
                   "calibration choices; alternatives considered and why rejected.",
    "data": "Source and date of each market input, fixings and their source, transformations, data quality checks, "
            "and whether any data is mock.",
    "assumptions_limitations": "Product-specific assumptions and limitations beyond the engine-derived list; how "
                               "each could affect the price and any compensating measures.",
    "ongoing_monitoring": "What to re-run and when, which results trigger a review, market conditions that stress "
                          "the model's assumptions.",
    "conclusion": "Whether the model is fit for the stated purpose, with which conditions. No approval or rating.",
}
SECTIONS = list(GUIDANCE)
COMMENTARY = ["implementation", "convergence", "sensitivity", "stress", "benchmarking", "outcomes", "cases",
              "methodology"]   # evidence sections (summary `section` values) that can carry commentary
TAG = re.compile(r'\{\{\s*(block|narrative|commentary|manual):([A-Za-z0-9_]+)\s*(?:"([^"]*)"\s*)?\}\}')
TOKEN = re.compile(r"\{\{\s*([^}|:\"]+?)\s*(?:\|\s*([^}]*?)\s*)?\}\}")


def load_template(path: Path | None) -> tuple[str, Path]:
    p = path or DEFAULT_TEMPLATE
    if not p.is_file():
        raise InputError(f"template not found: {p}")
    text = p.read_text()
    unknown = sorted({m.group(2) for m in TAG.finditer(text) if m.group(1) == "block"} - set(BLOCKS))
    if unknown:
        raise InputError(f"template {p.name}: unknown block(s) {unknown}; available: {sorted(BLOCKS)}")
    bad = sorted({m.group(2) for m in TAG.finditer(text) if m.group(1) == "commentary"} - set(COMMENTARY))
    if bad:
        raise InputError(f"template {p.name}: unknown commentary section(s) {bad}; available: {COMMENTARY}")
    return text, p


def template_slots(text: str) -> dict:
    """Tags in template order: {kind: {key: guidance}}; guidance defaults to the heading above the tag."""
    slots = {"block": {}, "narrative": {}, "commentary": {}, "manual": {}}
    for m in TAG.finditer(text):
        kind, key, guide = m.groups()
        if key in slots[kind]:
            continue
        heading = re.findall(r"^#+\s*(.+)$", text[:m.start()], re.M)
        slots[kind][key] = guide or GUIDANCE.get(key) or (heading[-1].strip() if heading else key)
    return slots


def _flagged(ev) -> set:
    return {x["section"] for x in ev["summary"] if x["status"] in ("WARN", "FAIL")}


def narrative_template(ev: dict, template: str) -> str:
    slots = template_slots(template)
    flagged = _flagged(ev)
    lines = [f"# Narrative for the model report of {ev['key']['trade_id']} (valuation {ev['key']['valuation_date']}).",
             "# Write every narrative block. Quote numbers with tokens from evidence.json, e.g.",
             "#   {{key.price|,.2f}}  {{key.ci_low|,.2f}}  {{tests.seed_stability.ratio|.2f}}  {{key.std_error|.3g}}",
             "# Markdown is allowed inside each block.", ""]
    for k, guide in slots["narrative"].items():
        lines += [f"# {guide}", f"{k}: |", "  ", ""]
    if "findings" in slots["block"]:
        lines += ["# Findings: one entry per issue found (empty list if none).", "findings: []",
                  "#  - {id: F1, title: ..., description: ..., impact: ..., recommendation: ...}", ""]
    if slots["commentary"]:
        lines.append("# Commentary on evidence sections; REQUIRED where a test is WARN or FAIL.")
        lines.append("commentary:")
        for k, guide in slots["commentary"].items():
            lines.append(f"  {k}: ''   # {guide}{'  <- REQUIRED (WARN/FAIL)' if k in flagged else ''}")
        lines.append("")
    if slots["manual"]:
        lines.append("# Organisation-supplied content (governance, sign-off, ...): fill ONLY with what the user gave;")
        lines.append("# left empty, the report shows a visible placeholder for the organisation to complete.")
        lines.append("manual:")
        lines += [f"  {k}: ''   # {guide}" for k, guide in slots["manual"].items()]
    return "\n".join(lines) + "\n"


def _lookup(ev, path):
    x = ev
    for seg in path.split("."):
        if isinstance(x, list) and seg.lstrip("-").isdigit():
            x = x[int(seg)]
        elif isinstance(x, dict) and seg in x:
            x = x[seg]
        else:
            raise KeyError(path)
    return x


def fill_tokens(text: str, ev: dict, bad: list) -> str:
    def sub(m):
        path, fmt = m.group(1), m.group(2)
        try:
            v = _lookup(ev, path)
            return format(v, fmt) if fmt else _fmt(v)
        except (KeyError, IndexError, ValueError, TypeError) as e:
            bad.append({"token": m.group(0), "error": f"{type(e).__name__}: {e}"})
            return f"**[UNRESOLVED {m.group(0)}]**"
    return TOKEN.sub(sub, text)


def _fmt(v, kind=None):
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, np.integer)):
        return f"{v:,}"
    if isinstance(v, float):
        if not math.isfinite(v):
            return "-"
        if kind == "pct":
            return f"{v:.2%}"
        return f"{v:,.2f}" if abs(v) >= 1000 else f"{v:.6g}"
    if isinstance(v, (list, dict)):
        return json.dumps(v, default=str)
    return str(v)


def _table(headers, rows) -> str:
    esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ evidence blocks
def _b_header(ev, nar, bad):
    k, eng = ev["key"], ev["environment"]["engine"]
    engine = eng.get("export_version") or f"code {eng['code_sha256']}" + \
        (f", commit {eng['git_commit']}" if eng.get("git_commit") else "")
    return _table(["Item", "Value"], [
        ["Trade", k["trade_id"]], ["Valuation date", k["valuation_date"]], ["Currency", k["currency"]],
        ["Model / method", f"{k['model']} / {k['method']}"], ["Engine", engine],
        ["Evidence generated", ev["generated_at"]],
        ["Market data", "**MOCK / synthetic**" if k["mock_market_data"] else "as supplied"]])


def _b_mock_warning(ev, nar, bad):
    return ("> **Warning:** the market data is mock / synthetic. Prices and sensitivities in this report are "
            "illustrative only.\n") if ev["key"]["mock_market_data"] else ""


def _b_key_results(ev, nar, bad):
    k, r = ev["key"], ev["base"]
    ci = f"[{_fmt(k['ci_low'])}, {_fmt(k['ci_high'])}]" if k["ci_low"] is not None else "exact (closed form)"
    return _table(["Quantity", "Value"], [
        ["Price", _fmt(k["price"])], ["Std error", _fmt(k["std_error"])],
        [f"{_fmt(k['confidence'], 'pct') if k['confidence'] else ''} confidence interval", ci],
        ["Paths / seed", f"{_fmt(k['num_paths'])} / {_fmt(k['seed'])}"]] +
        [[f"PV leg {lid}", _fmt(v)] for lid, v in (r.get("legs") or {}).items()])


def _b_test_summary(ev, nar, bad):
    c = ev["status_counts"]
    return ", ".join(f"{n} {s}" for s, n in c.items() if n) + "\n\n" + _table(
        ["Test", "Section", "Status", "Detail"],
        [[x["test"], x["section"], f"**{x['status']}**" if x["status"] in ("FAIL", "WARN") else x["status"],
          x["detail"]] for x in ev["summary"]])


def _b_components(ev, nar, bad):
    tr = ev["input"]["trade"]
    rows = [["leg", l.get("id"), l.get("type"), l.get("position", "long"),
             ", ".join(c.get("type") for c in l.get("conditions") or []) or "-"] for l in tr.get("legs", [])]
    rows += [["event", e.get("id"), e.get("type"), e.get("exerciser", "-"), e.get("observation", "-")]
             for e in tr.get("termination_events", [])]
    return _table(["Kind", "Id", "Type", "Position / exerciser", "Conditions / observation"], rows) + \
        f"\nEffective {tr.get('effective_date', ev['key']['valuation_date'])}, maturity {tr.get('maturity')}.\n"


def _b_engine_method(ev, nar, bad):
    r = ev["base"]
    out = [f"- **Method:** {r['method']}. {r.get('method_reason', '')}\n",
           f"- **Model specification:** `{json.dumps(ev['input'].get('model'), default=str)}`\n"]
    sim = ev["plan"].get("simulation")
    if sim:
        out.append(f"- **Simulation:** {_fmt(r.get('num_paths'))} paths, seed {r.get('seed')}, antithetic "
                   f"{r.get('antithetic')}; fixing dates {sim['fixing_dates']}, payment dates {sim['payment_dates']}\n")
    out.append("- **Computation graph:** " + " -> ".join(n["node"] for n in ev["plan"].get("graph", [])) + "\n")
    return "".join(out)


def _b_calibration(ev, nar, bad):
    cal = (ev["base"].get("model_diagnostics") or {}).get("calibration")
    if not cal:
        return "The model parameters are given, not calibrated.\n"
    out = _table(["Parameter", "Value"], [[p, _fmt(v)] for p, v in cal.items() if not isinstance(v, (list, dict))])
    if cal.get("instruments"):
        out += "\n" + _table(["Expiry", "Tenor", "Market vol", "Model vol", "Error"], [
            [f.get("expiry"), f.get("tenor"), _fmt(f.get("market_vol")), _fmt(f.get("model_vol")),
             _fmt(f.get("vol_error"))] for f in cal["instruments"]])
    return out


def _b_model_diagnostics(ev, nar, bad):
    other = {n: v for n, v in (ev["base"].get("model_diagnostics") or {}).items() if n != "calibration"}
    return "".join(f"- **{n}:** `{json.dumps(v, default=str)}`\n" for n, v in other.items()) or "None.\n"


def _b_market_data(ev, nar, bad):
    m = ev["market"]
    rows = [[f"curve {n}", c["type"], f"{c['quotes']} quote(s)"] for n, c in m["curves"].items()]
    rows += [[f"equity {u}", f"spot {_fmt(e['spot'])}", f"vol {e['vol_type']}"
              + (f" ({e['strike_mode']} strikes)" if e.get("strike_mode") else "") + f", dividend {e['dividend']}"
              + ("" if e["used_by_model"] else " (not used by the model)")] for u, e in m["equities"].items()]
    rows += [[f"swaption vols {n}", s["vol_type"], f"{s['expiries']} expiries x {s['tenors']} tenors"]
             for n, s in m["swaption_vols"].items()]
    return f"Market data as of {m['as_of']}.\n\n" + _table(["Input", "Type", "Detail"], rows)


def _b_input_files(ev, nar, bad):
    return _table(["Role", "File", "SHA-256 (first 16)"],
                  [[role, Path(f["path"]).name, f["sha256"][:16]] for role, f in ev["files"].items()])


def _b_limitations(ev, nar, bad):
    return "".join(f"- {x}\n" for x in ev["known_limitations"])


def _b_run_warnings(ev, nar, bad):
    r = ev["base"]
    w = r.get("warnings", []) + r.get("notes", [])
    return "".join(f"- {x}\n" for x in w) or "None.\n"


def _b_implementation(ev, nar, bad):
    out = _table(["Check", "Status", "Detail"], [[x["test"], x["status"], x["detail"]] for x in ev["summary"]
                                                 if x["section"] == "implementation"])
    if not ev["tests"].get("unit_tests", {}).get("run"):
        out += "\nThe engine test suite was not run for this report (`--run-tests`).\n"
    return out


def _b_convergence(ev, nar, bad):
    t, out = ev["tests"], []
    if "convergence" in t:
        c = t["convergence"]
        out.append(_table(["Paths", "Price", "Std error", "Diff vs largest", "Within tolerance"], [
            [_fmt(x["num_paths"]), _fmt(x["price"]), _fmt(x["std_error"]), _fmt(x.get("diff_vs_largest")),
             _fmt(x.get("within_tolerance"))] for x in c["rows"]]))
        out.append(f"\nFitted std-error slope in log-log: {_fmt(c['se_slope'])} (expected -0.5 for Monte Carlo).\n")
    if "seed_stability" in t:
        s = t["seed_stability"]
        out.append("\n" + _table(["Seed", "Price", "Std error"],
                                 [[x["seed"], _fmt(x["price"]), _fmt(x["std_error"])] for x in s["rows"]]))
        out.append(f"\nStandard deviation across seeds {_fmt(s['std_across_seeds'])} vs mean reported std error "
                   f"{_fmt(s['mean_reported_se'])}: ratio {_fmt(s['ratio'])} (99% band "
                   f"{_fmt(s['ratio_99pct_bounds'][0])}-{_fmt(s['ratio_99pct_bounds'][1])}).\n")
    if not out:
        out.append(next((x["detail"] for x in ev["summary"] if x["test"] == "Convergence"), "Not run.") + "\n")
    return "".join(out)


def _b_sensitivity(ev, nar, bad):
    sens = ev["tests"]["sensitivities"]
    out = f"Method: {sens['method']}.\n\n" + _table(
        ["Factor", "Bump", "Unit", "P+", "P-", "First order", "SE", "Second order", "Significant"], [
            [x["label"], _fmt(x["bump"]), x["unit"], _fmt(x.get("price_up")), _fmt(x.get("price_down")),
             _fmt(x.get("first_order")) if "error" not in x else f"error: {x['error']}",
             _fmt(x.get("first_order_se")), _fmt(x.get("second_order")), _fmt(x.get("significant"))]
            for x in sens["rows"]])
    spot = [x for x in sens["rows"] if "delta" in x]
    if spot:
        out += "\n" + _table(["Underlying", "Delta (per unit)", "Gamma (per unit^2)"],
                             [[x["label"].split()[-1], _fmt(x["delta"]), _fmt(x["gamma"])] for x in spot])
    return out


def _b_stress(ev, nar, bad):
    st = ev["tests"]["stress"]
    return f"{st['note']}\n\n" + _table(["Scenario", "Price", "Change", "Change %", "Std error"], [
        [x["label"], _fmt(x.get("price")) if "error" not in x else f"error: {x['error']}", _fmt(x.get("change")),
         _fmt(x.get("change_pct"), "pct"), _fmt(x.get("std_error"))] for x in st["rows"]])


def _b_benchmarking(ev, nar, bad):
    t, rows = ev["tests"], []
    cc = t["implementation"].get("cross_check")
    if cc:
        rows.append(["Closed form (engine cross-check)", _fmt(cc["analytic_price"]), _fmt(-cc["mc_minus_analytic"]),
                     _fmt(-cc["gap_in_std_errors"] if cc["gap_in_std_errors"] is not None else None),
                     "PASS" if cc["agrees"] else "FAIL"])
    rows += [[x["label"], _fmt(x.get("price")) if "error" not in x else f"error: {x['error']}",
              _fmt(x.get("diff_vs_base")), _fmt(x.get("diff_in_se")), x["status"]] for x in t["benchmarks"]["rows"]]
    if not rows:
        return "No benchmark model or closed form was run for this trade.\n"
    return _table(["Benchmark", "Price", "Diff vs model", "Diff (SE)", "Status"], rows) + "".join(
        f"- *{x['label']}:* {x['description']}\n" for x in t["benchmarks"]["rows"] if x.get("description"))


def _b_outcomes(ev, nar, bad):
    refs = ev["tests"]["references"]["rows"]
    if not refs:
        return ("No external reference prices (counterparty marks, vendor or dealer quotes, realised outcomes) were "
                "supplied.\n")
    return _table(["Reference", "Source", "Date", "Reference price", "Model - reference", "%", "Tolerance", "Status"],
                  [[x["label"], x.get("source") or "-", x.get("date") or "-", _fmt(x["price"]), _fmt(x["difference"]),
                    _fmt(x["difference_pct"], "pct"), _fmt(x["tolerance"]), x["status"]] for x in refs])


def _b_cases(ev, nar, bad):
    cs = ev["tests"]["cases"]["rows"]
    if not cs:
        return "No limiting cases were specified.\n"
    return _table(["Case", "Rationale", "Price", "Expectation", "Difference", "Tolerance", "Status"], [
        [x["label"], x.get("rationale") or "-", _fmt(x.get("price")) if "error" not in x else f"error: {x['error']}",
         x.get("expected", "-"), _fmt(x.get("difference")), _fmt(x.get("tolerance")), x["status"]] for x in cs])


def _b_findings(ev, nar, bad):
    f = nar.get("findings")
    if not f:
        return "No findings.\n" if f is not None else "> **[TO BE COMPLETED: findings (an empty list if none)]**\n"
    return _table(["Id", "Title", "Description", "Impact", "Recommendation"], [
        [x.get("id", "-"), x.get("title", "-")] + [fill_tokens(str(x.get(c, "-")), ev, bad)
                                                   for c in ("description", "impact", "recommendation")] for x in f])


def _b_trade_spec(ev, nar, bad):
    return "```yaml\n" + yaml.safe_dump(ev["input"], sort_keys=False, default_flow_style=None, width=110) + "```\n"


def _b_cashflows(ev, nar, bad):
    cf = ev["base"].get("expected_cashflows")
    if not cf:
        return "Not available for a closed-form price.\n"
    return _table(["Date", "Expected amount", "Discount factor", "PV", "Survival probability"], [
        [x["date"], _fmt(x["expected_amount"]), _fmt(x["discount_factor"]), _fmt(x["pv"]),
         _fmt(x["survival_probability"], "pct")] for x in cf])


def _b_environment(ev, nar, bad):
    env = ev["environment"]
    return _table(["Item", "Value"], [["Python", env["python"]], ["Platform", env["platform"]]] +
                  [[p, v or "-"] for p, v in env["packages"].items()] +
                  [[f"engine {a}", b] for a, b in env["engine"].items()]) + \
        ("\nEvery number in this report is in `evidence.json`, produced by `scripts/report.py evidence` from the "
         "input files listed above. Re-running it with the same files and engine reproduces them exactly (fixed "
         "seeds).\n")


BLOCKS = {  # name -> (renderer, what it shows)
    "header": (_b_header, "trade, valuation date, model / method, engine version, mock flag"),
    "mock_warning": (_b_mock_warning, "warning when the market data is mock (added at the top if not placed)"),
    "key_results": (_b_key_results, "price, std error, confidence interval, paths, PV per leg"),
    "test_summary": (_b_test_summary, "every test with section, status and detail"),
    "components": (_b_components, "legs and events of the trade as specified"),
    "engine_method": (_b_engine_method, "pricing method and reason, model spec, simulation, computation graph"),
    "calibration": (_b_calibration, "calibrated parameters and instrument fit"),
    "model_diagnostics": (_b_model_diagnostics, "other model diagnostics (local-vol flooring, Heston fit, ...)"),
    "market_data": (_b_market_data, "curves, equities, swaption vols used"),
    "input_files": (_b_input_files, "input files with SHA-256"),
    "limitations": (_b_limitations, "engine-derived limitations for this model and method"),
    "run_warnings": (_b_run_warnings, "warnings and notes of the base pricing run"),
    "implementation": (_b_implementation, "process verification checks"),
    "convergence": (_b_convergence, "convergence in paths and seed stability"),
    "sensitivity": (_b_sensitivity, "bump-and-revalue sensitivities with their std errors"),
    "stress": (_b_stress, "stress scenarios"),
    "benchmarking": (_b_benchmarking, "closed-form cross-check and benchmark models"),
    "outcomes": (_b_outcomes, "comparison with reference prices"),
    "cases": (_b_cases, "limiting and boundary cases"),
    "findings": (_b_findings, "findings table from narrative.yaml `findings`"),
    "trade_spec": (_b_trade_spec, "the trade specification as priced (YAML)"),
    "cashflows": (_b_cashflows, "expected cashflows per date"),
    "environment": (_b_environment, "Python, libraries, engine version, reproduction note"),
}
# evidence blocks a reader needs; render lists those a template leaves out
CORE_BLOCKS = ["key_results", "test_summary", "engine_method", "market_data", "limitations", "implementation",
               "convergence", "sensitivity", "stress", "benchmarking", "outcomes", "cases"]


def render(ev: dict, nar: dict, template: str) -> tuple[str, dict]:
    bad, missing, manual_open = [], [], []
    slots = template_slots(template)
    com, man = nar.get("commentary") or {}, nar.get("manual") or {}
    flagged = _flagged(ev)

    def sub(m):
        kind, key, guide = m.groups()
        guide = guide or slots[kind].get(key, key)
        if kind == "block":
            return BLOCKS[key][0](ev, nar, bad)
        if kind == "narrative":
            v = str(nar.get(key) or "").strip()
            if not v:
                missing.append(key)
                return f"> **[TO BE COMPLETED: {guide}]**\n"
            return fill_tokens(v, ev, bad) + "\n"
        if kind == "commentary":
            v = str(com.get(key) or "").strip()
            if v:
                return fill_tokens(v, ev, bad) + "\n"
            if key in flagged:
                missing.append(f"commentary.{key}")
                return f"> **[TO BE COMPLETED: commentary on {key} - a test here is WARN or FAIL]**\n"
            return ""
        v = str(man.get(key) or "").strip()            # manual
        if v:
            return fill_tokens(v, ev, bad) + "\n"
        manual_open.append(key)
        return f"> **[To be provided by the organisation: {guide}]**\n"

    # fill the template's own evidence tokens with the tags masked, then expand the tags: a narrative's tokens are
    # filled once, by sub, and nothing a block or narrative produces is parsed again
    tags = {str(m.start()): m for m in TAG.finditer(template)}
    body = fill_tokens(TAG.sub(lambda m: "\0" + str(m.start()) + "\0", template), ev, bad)
    body = re.sub("\0(\\d+)\0", lambda x: sub(tags[x.group(1)]), body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    if "findings" in slots["block"] and "findings" not in nar:
        missing.append("findings")
    # a WARN or FAIL must be discussed somewhere, even if the template has no slot for it
    for s in sorted(flagged - set(slots["commentary"])):
        if s in COMMENTARY:
            missing.append(f"commentary.{s} (template has no slot: add {{{{commentary:{s}}}}})")
    if ev["key"]["mock_market_data"] and "mock_warning" not in slots["block"]:
        body = _b_mock_warning(ev, nar, bad) + "\n" + body
    return body, {"missing_narrative": missing, "unresolved_tokens": bad, "manual_placeholders": manual_open,
                  "evidence_not_in_template": [b for b in CORE_BLOCKS if b not in slots["block"]]}


# ------------------------------------------------------------------ CLI
def _evidence_dir(path: str) -> Path:
    d = Path(path)
    if d.exists() and not d.is_dir():
        raise InputError(f"--out-dir {d} is a file")
    return d


def _template_for(d: Path, given: str | None) -> tuple[str, Path]:
    """--template, else the template saved with the narrative (DIR/template.md), else the SR 11-7 default."""
    if given:
        return load_template(Path(given))
    return load_template(d / "template.md" if (d / "template.md").is_file() else None)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("evidence")
    e.add_argument("input")
    e.add_argument("--out-dir", required=True)
    e.add_argument("--run-tests", action="store_true", help="also run the engine's test suite (about a minute)")
    n = sub.add_parser("narrative")
    n.add_argument("dir")
    n.add_argument("--template", help="the user's report template (Markdown with tags); default: SR 11-7. "
                                      "Saved as DIR/template.md for render")
    n.add_argument("--force", action="store_true", help="overwrite an existing narrative.yaml / template.md")
    r = sub.add_parser("render")
    r.add_argument("dir")
    r.add_argument("--template", help="default: DIR/template.md, else the SR 11-7 template")
    r.add_argument("--narrative", help="default: DIR/narrative.yaml")
    r.add_argument("--out", help="default: DIR/report.md")
    sub.add_parser("blocks", help="list the evidence blocks and tag syntax a template can use")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "blocks":
            _dump({"tags": {"{{block:NAME}}": "evidence table / list", "{{narrative:KEY \"guidance\"}}":
                            "text written in narrative.yaml (required)", "{{commentary:SECTION \"guidance\"}}":
                            f"comment on an evidence section {COMMENTARY} (required on WARN / FAIL)",
                            "{{manual:KEY \"guidance\"}}": "organisation-supplied content; placeholder if not given",
                            "{{path|format}}": "a value from evidence.json"},
                   "blocks": {k: v[1] for k, v in BLOCKS.items()}, "default_template": str(DEFAULT_TEMPLATE)})
            return 0
        if a.cmd == "evidence":
            out = _evidence_dir(a.out_dir)
            cfg, doc, base, files = load_config(a.input)
            ev = Evidence(cfg, doc, base, files, a.run_tests).build()
            out.mkdir(parents=True, exist_ok=True)
            (out / "evidence.json").write_text(json.dumps(ev, indent=2, default=str) + "\n")
            fails = [x for x in ev["summary"] if x["status"] == "FAIL"]
            _dump({"ok": not fails, "evidence": str((out / "evidence.json").resolve()), "key": ev["key"],
                   "status_counts": ev["status_counts"],
                   "attention": [x for x in ev["summary"] if x["status"] in ("FAIL", "WARN")]})
            return 1 if fails else 0
        d = Path(a.dir)
        if not (d / "evidence.json").is_file():
            raise InputError(f"no evidence.json in {d}; run `report.py evidence` first")
        ev = json.loads((d / "evidence.json").read_text())
        if a.cmd == "narrative":
            f = d / "narrative.yaml"
            if f.exists() and not a.force:
                raise InputError(f"{f} exists; edit it, or pass --force to start over")
            text, tpath = load_template(Path(a.template)) if a.template else load_template(None)
            saved = d / "template.md"
            if a.template:
                if saved.exists() and not a.force and saved.read_text() != text:
                    raise InputError(f"{saved} exists with other content; pass --force to replace it")
                saved.write_text(text)
            elif saved.exists() and not a.force:
                text, tpath = load_template(saved)
            f.write_text(narrative_template(ev, text))
            slots = template_slots(text)
            _dump({"ok": True, "narrative": str(f.resolve()), "template": str(tpath),
                   "narrative_keys": list(slots["narrative"]), "manual_keys": list(slots["manual"]),
                   "commentary_required": sorted(_flagged(ev) & set(COMMENTARY)),
                   "evidence_not_in_template": [b for b in CORE_BLOCKS if b not in slots["block"]]})
            return 0
        template, tpath = _template_for(d, a.template)
        nf = Path(a.narrative) if a.narrative else d / "narrative.yaml"
        nar = yaml.safe_load(nf.read_text()) if nf.is_file() else {}
        if not isinstance(nar, dict):
            raise InputError(f"{nf} must be a YAML mapping")
        text, issues = render(ev, nar, template)
        outf = Path(a.out) if a.out else d / "report.md"
        outf.write_text(text)
        complete = not issues["missing_narrative"] and not issues["unresolved_tokens"]
        _dump({"ok": complete, "report": str(outf.resolve()), "template": str(tpath),
               "narrative": str(nf) if nf.is_file() else None, **issues, "status_counts": ev["status_counts"]})
        return 0 if complete else 1
    except InputError as err:
        _dump({"ok": False, "error": str(err)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
