"""Intermediate results of a pricing run, for manual UAT and offline analysis.

    result, manifest = inspect(plan, out_dir, nodes=["leg", "curve:USD"], max_paths=None)

Runs the same graph as `run` (same seed, same paths, same price) while keeping every node result, then writes each
selected node's result to `out_dir`. Nothing is recomputed: the writer walks whatever object the node produced and
serializes it by type, so a new component's output is exported without code here.

    DataFrame (dates x paths)         -> <node>.<field>.csv, one row per path, one column per date, + _stats.csv
    array (dates x paths [x k])       -> the same, its dates taken from a sibling `dates` / `row` field when present
    array (paths,)                    -> a column of <node>.per_path.csv
    list of dates                     -> <node>.<field>.csv
    scalars, dicts, pydantic specs    -> <node>.json
    QuantLib yield curve (node itself)-> <node>.csv: discount factor and zero rate on the trade's dates
    other QuantLib objects, callables -> listed under `not_exported` in the manifest

Two things are not node results and are exported from where the run kept them: observable values (computed lazily
by the legs and events that read them; see PCtx.reads) and the price node's own per-path cashflows and PV.

Full data goes to disk only. The manifest (what the CLI prints) is small, so it never floods a console or an LLM
context whatever num_paths is.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd
import QuantLib as ql
from pydantic import BaseModel

from . import conventions as cv
from .engine import INSPECT_FLOWS, INSPECT_READS, Plan, run

PERCENTILES = (5, 25, 50, 75, 95)
FLOAT = "%.15g"


class InspectError(ValueError):
    pass


def node_keys(plan: Plan) -> list:
    """What `inspect` can write, in graph order: the graph nodes, plus each underlying read as an observable by its
    own name (an observable without a node of its own)."""
    declared = [k for k in plan.nodes if k.startswith("observable:")]
    by_name = [f"observable:{u}" for u in (plan.v.underlyings if plan.method == "mc" else [])
               if f"observable:{u}" not in declared]
    return list(plan.nodes) + by_name


def select(plan: Plan, wanted) -> list:
    """Exact node keys, or a kind ('leg', 'curve', 'observable', ...) for every node of that kind."""
    keys = node_keys(plan)
    if not wanted:
        return keys
    out, unknown = [], []
    for w in wanted:
        hit = [k for k in keys if k == w or k.split(":", 1)[0] == w]
        if not hit:
            unknown.append(w)
        out += [k for k in hit if k not in out]
    if unknown:
        raise InspectError(f"unknown node(s) {unknown}; available: {keys}")
    return out


def inspect(plan: Plan, out_dir, nodes=None, max_paths: int | None = None) -> tuple[dict, dict]:
    """Price, then write the selected nodes. Returns (pricing result, manifest).

    out_dir must be new, empty, or an earlier inspect output: the files its manifest lists are removed first, so no
    stale file from an earlier run sits next to the new manifest."""
    chosen = select(plan, nodes)
    if max_paths is not None and (isinstance(max_paths, bool) or not isinstance(max_paths, int) or max_paths < 1):
        raise InspectError(f"--max-paths must be a positive integer, got {max_paths!r}")
    out = Path(out_dir)
    _clear(out)
    keep = {}
    result = run(plan, keep)
    r = keep["results"]
    out.mkdir(parents=True, exist_ok=True)
    n = r["fixings"].n_paths if "fixings" in r else 1
    k = n if max_paths is None else min(max_paths, n)
    if plan.input.pricing.antithetic and k % 2:      # keep antithetic pairs (rows 2i, 2i+1) whole
        k = min(k + 1, n)
    w = _Writer(out, k, n, plan)
    (out / "result.json").write_text(json.dumps(_plain(result), indent=2, default=str))
    w.files.append({"file": "result.json", "node": "price", "content": "the pricing result (as `price --out`)"})
    (out / "input.json").write_text(json.dumps(_plain(plan.input.model_dump(mode="json")), indent=2, default=str))
    w.files.append({"file": "input.json", "node": "input", "content": "the trade input as priced (after --set)"})
    reads = r.get(INSPECT_READS) or {}
    for key in chosen:
        kind, _, name = key.partition(":")
        if kind == "observable":           # computed lazily by the legs and events that read it
            w.observable(key, reads.get(name, {}))
        elif key == "price" and r.get(INSPECT_FLOWS):
            w.node(key, {"result": r[key]})
            w.price_flows(key, r[INSPECT_FLOWS])
        elif key == "discount_factors":
            w.node(key, {"dates": plan.pay_dates, "values": r[key]})
        else:
            w.node(key, r[key])
    manifest = {
        "ok": True, "trade_id": result["trade_id"], "method": result["method"],
        "price": result["price"], "std_error": result.get("std_error"),
        "num_paths": result.get("num_paths"), "seed": result.get("seed"),
        "out_dir": str(out.resolve()),
        "paths_written": k if plan.method == "mc" else None,
        "layout": "path matrices: rows = paths (antithetic pairs are adjacent rows), columns = dates; a deterministic "
                  "quantity has one row with path 'all'; each path matrix has a _stats.csv computed on every path",
        "files": w.files,
        "not_exported": w.skipped,
        "warnings": result.get("warnings", []),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return result, manifest


# ------------------------------------------------------------------ generic serialization
class _Writer:
    def __init__(self, out: Path, k_paths, n_paths, plan):
        self.out, self.k, self.n, self.plan = out, k_paths, n_paths, plan
        self.files, self.skipped = [], []
        self.curves = {}            # tabulated curve -> file already written

    # one node: split its result into JSON-able parts and tables
    def node(self, key, value):
        base = safe_name(key)
        meta, per_path = {}, {}
        self._walk(key, base, "", value, meta, per_path)
        if per_path:
            df = pd.DataFrame({c: v[:self.k] for c, v in per_path.items()})
            df.insert(0, "path", range(self.k))
            self._csv(f"{base}.per_path.csv", key, df, content="one value per path: " + ", ".join(per_path))
        if meta:
            file = safe_name(f"{base}.json")
            (self.out / file).write_text(json.dumps(_plain(meta), indent=2, default=str))
            self.files.append({"file": file, "node": key, "content": "scalars, settings and diagnostics"})

    def _walk(self, key, base, field, x, meta, per_path, dates=None):
        name = field or "value"
        if isinstance(x, ql.YieldTermStructureHandle):
            self._curve(key, f"{base}.{field}" if field else base, x)
            return
        if isinstance(x, type):
            _put(meta, field, getattr(x, "type_name", x.__name__))
            return
        if x is None or callable(x) and not isinstance(x, (np.ndarray, pd.DataFrame)):
            if x is not None:
                self.skipped.append({"node": key, "field": name, "type": type(x).__name__})
            return
        if isinstance(x, pd.DataFrame):
            self._matrix(key, f"{base}.{name}.csv", list(x.index), x.values)
        elif isinstance(x, np.ndarray):
            self._array(key, base, name, x, meta, per_path, dates)
        elif isinstance(x, BaseModel):
            meta[name] = x.model_dump(mode="json")
        elif _is_date_list(x):
            self._csv(f"{base}.{name}.csv", key, pd.DataFrame({"date": [_iso(d) for d in x]}), content="dates")
        elif isinstance(x, dict):
            sub = {}
            row_dates = x.get("dates") if _is_date_list(x.get("dates")) else dates
            for k, v in x.items():
                if _jsonable(v):
                    sub[str(k)] = v
                else:
                    self._walk(key, base, f"{field}.{k}" if field else str(k), v, meta, per_path, row_dates)
            if sub and field:
                _put(meta, field, sub)
            elif sub:
                meta.update(sub)
        elif dataclasses.is_dataclass(x) or (hasattr(x, "__dict__") and type(x).__module__.startswith("pricer")):
            items = vars(x) if not dataclasses.is_dataclass(x) else {f.name: getattr(x, f.name)
                                                                     for f in dataclasses.fields(x)}
            row_dates = items.get("dates") if _is_date_list(items.get("dates")) else _row_dates(items)
            for k, v in items.items():
                if k.startswith("_"):
                    continue
                if _jsonable(v):
                    _put(meta, f"{field}.{k}" if field else k, v)
                else:
                    self._walk(key, base, f"{field}.{k}" if field else k, v, meta, per_path, row_dates)
        elif _jsonable(x):
            _put(meta, field, x)
        elif isinstance(x, (list, tuple)) and all(isinstance(v, (np.ndarray, np.generic)) or _jsonable(v) for v in x):
            _put(meta, field, _plain(x))
        else:
            self.skipped.append({"node": key, "field": name, "type": type(x).__name__})

    def _array(self, key, base, name, x, meta, per_path, dates):
        if x.ndim == 0 or (x.ndim == 1 and x.size <= 1):
            _put(meta, name, x.item() if x.size else None)
        elif x.ndim == 1 and len(x) == self.n and self.n > 1:
            per_path[name] = [None if isinstance(v, np.datetime64) and np.isnat(v) else v for v in x] \
                if x.dtype.kind == "M" else x
        elif x.ndim == 1:
            _put(meta, name, x.tolist())
        elif x.ndim == 2 and x.shape[1] in (self.n, 1):
            labels = dates if dates is not None and len(dates) == x.shape[0] else list(range(x.shape[0]))
            self._matrix(key, f"{base}.{name}.csv", labels, x)
        elif x.ndim == 3 and x.shape[1] in (self.n, 1):
            for j in range(x.shape[2]):
                self._walk(key, base, f"{name}.{j}", x[:, :, j], meta, per_path, dates)
        else:
            _put(meta, name, x.tolist())

    # ------------------------------------------------------------------ formats
    def _csv(self, file, key, df, content, **extra):
        file = safe_name(file)
        df.to_csv(self.out / file, index=False, float_format=FLOAT)
        self.files.append({"file": file, "node": key, "content": content, "rows": len(df),
                           "columns": len(df.columns), **extra})

    def _matrix(self, key, file, labels, values):
        """(dates x paths) or (dates x 1), written transposed: rows = paths, columns = dates, plus per-date stats."""
        x = np.asarray(values, dtype=float)
        det = x.shape[1] == 1
        shown = x if det else x[:, :self.k]
        df = pd.DataFrame(shown.T, columns=_labels(labels))
        df.insert(0, "path", ["all"] if det else range(shown.shape[1]))
        stats = _stats(labels, x)
        sfile = safe_name(file[:-len(".csv")] + "_stats.csv")
        stats.to_csv(self.out / sfile, index=False, float_format=FLOAT)
        summary = {"dates": len(labels), "first": _iso(labels[0]), "last": _iso(labels[-1])} if len(labels) else {}
        if len(labels):
            last = stats.iloc[-1]
            summary["at_last"] = {k: float(last[k]) for k in ("mean", "std", "p5", "p50", "p95")}
        self._csv(file, key, df, content="path matrix" if not det else "deterministic row", deterministic=det,
                  stats_file=sfile, summary=summary)

    def _curve(self, key, base, h):
        """A yield curve has no stored table to dump: tabulate it on the dates the trade uses."""
        v = self.plan.v
        ds = sorted({v.valuation_date, v.maturity} | set(self.plan.pay_dates)
                    | {d for s in self.plan.sim_dates.values() for d in s if d >= v.valuation_date})
        rows = []
        for d in ds:
            q = cv.to_ql(d)
            t = h.timeFromReference(q)
            rows.append({"date": d.isoformat(), "time": t, "discount_factor": h.discount(q),
                         "zero_rate_cc": h.zeroRate(q, h.dayCounter(), ql.Continuous).rate() if t > 0 else math.nan})
        df = pd.DataFrame(rows)
        sig = (h.dayCounter().name(), tuple(df.discount_factor.round(14)))
        if sig in self.curves:                   # the same curve seen through another node
            ref = {"file": self.curves[sig], "node": key, "content": "same curve as written for an earlier node"}
            if ref not in self.files:
                self.files.append(ref)
            return
        self.curves[sig] = safe_name(f"{base}.csv")
        self._csv(f"{base}.csv", key, df,
                  content=f"discount factor and continuously-compounded zero rate ({h.dayCounter().name()}) on the "
                          "valuation date, every simulation and payment date, and maturity")

    def observable(self, key, values: dict):
        if not values:
            return
        ds = sorted(values)
        rows = [np.atleast_1d(values[d]) for d in ds]
        m = max(len(v) for v in rows)
        x = np.vstack([np.broadcast_to(v, (m,)) for v in rows])
        self._matrix(key, f"{safe_name(key)}.values.csv", ds, x)

    def price_flows(self, key, fl):
        """The price node's per-path cashflows (after termination and exercise) and PV, as it aggregated them."""
        base = key
        n = len(fl["pv"])
        k = self.k
        cols = {}
        for kind, oid, d, cf in fl["rows"]:
            label, i = flow_label(kind, oid, d), 2
            while label in cols:
                label, i = flow_label(kind, oid, d, i), i + 1
            cols[label] = np.broadcast_to(cf, (n,))[:k]
        flows = pd.DataFrame(cols)
        flows.insert(0, "path", range(k))
        self._csv(f"{base}.cashflows.csv", key, flows,
                  content="cashflow paid on each path after termination and exercise, undiscounted; "
                          "columns kind:owner:date (discount with discount_factors.values.csv)")
        events = fl["events"]
        df = pd.DataFrame({"path": range(k), "pv": fl["pv"][:k],
                           "termination_date": [None if np.isnat(x) else str(x) for x in fl["first"][:k]],
                           "terminated_by": [events[i] if i >= 0 else None for i in fl["owner"][:k]],
                           "exercise_date": [None if np.isnat(x) else str(x) for x in fl["exercise_date"][:k]]})
        self._csv(f"{base}.per_path.csv", key, df, content="PV of each path (the price is their mean) and what ended it")


# ------------------------------------------------------------------ helpers
def flow_label(kind, owner, date, repeat=1) -> str:
    """Column label of price.cashflows.csv: kind:owner:date, with #2, #3 ... for a repeated (kind, owner, date)."""
    label = f"{kind}:{owner}:{date.isoformat()}"
    return label if repeat == 1 else f"{label}#{repeat}"


def flow_date(label) -> str:
    """The payment date (YYYY-MM-DD) of a price.cashflows.csv column label."""
    return label.rsplit(":", 1)[1].split("#")[0]


def safe_name(name) -> str:
    """A file name that stays inside the output directory whatever the trade, leg or observable ids contain."""
    out = re.sub(r"[^A-Za-z0-9._-]", "_", str(name)).lstrip(".")
    return out or "_"


def _clear(out: Path):
    """Remove the files an earlier inspect run listed in its manifest; refuse a directory inspect did not write."""
    if not out.exists():
        return
    if not out.is_dir():
        raise InspectError(f"--out-dir {out} exists and is not a directory")
    entries = list(out.iterdir())
    if not entries:
        return
    manifest = out / "manifest.json"
    try:
        old = json.loads(manifest.read_text())
    except (OSError, ValueError):
        raise InspectError(f"--out-dir {out} is not empty and holds no inspect manifest.json; "
                           "use a new or empty directory") from None
    names = {"manifest.json"} | {f.get(k) for f in old.get("files", []) for k in ("file", "stats_file")}
    for name in names:
        if isinstance(name, str) and name == safe_name(name) and (out / name).is_file():
            (out / name).unlink()
def _is_date_list(x):
    return isinstance(x, (list, tuple)) and len(x) > 0 and all(isinstance(d, dt.date) for d in x)


def _row_dates(items):
    """Row labels of an object's arrays: a {date: row} index (e.g. Hull-White paths)."""
    for v in items.values():
        if isinstance(v, dict) and v and all(isinstance(k, dt.date) for k in v) \
                and all(isinstance(i, (int, np.integer)) for i in v.values()):
            return sorted(v, key=v.get)
    return None


def _jsonable(v):
    if isinstance(v, (str, bool, int, float, type(None), np.generic, dt.date)):
        return True
    if isinstance(v, (list, tuple)):
        return all(_jsonable(x) for x in v)
    if isinstance(v, dict):
        return all(_jsonable(x) for x in v.values())
    return False


def _put(meta, path, value):
    meta[path or "value"] = value


def _labels(dates):
    seen, out = {}, []
    for d in dates:
        s = _iso(d)
        seen[s] = seen.get(s, 0) + 1
        out.append(s if seen[s] == 1 else f"{s}#{seen[s]}")
    return out


def _iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def _stats(dates, x):
    q = np.percentile(x, PERCENTILES, axis=1)
    df = pd.DataFrame({"date": _labels(dates), "mean": x.mean(axis=1),
                       "std": x.std(axis=1, ddof=1) if x.shape[1] > 1 else 0.0, "min": x.min(axis=1)})
    for p, row in zip(PERCENTILES, q):
        df[f"p{p}"] = row
    df["max"] = x.max(axis=1)
    return df


def _plain(o):
    """JSON-friendly copy: numpy arrays and scalars to lists and floats, dict keys to strings."""
    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return _plain({f.name: getattr(o, f.name) for f in dataclasses.fields(o)})
    return o
