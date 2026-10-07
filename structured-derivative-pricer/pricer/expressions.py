"""Restricted arithmetic expressions: the payoff and state-update functions of a path_dependent leg.

An expression is a formula over named per-path values, evaluated vectorised over the paths. It is parsed
with Python's `ast` and walked node by node: nothing is passed to eval, and only the constructs below
are accepted, so an expression can compute but cannot call, import or reach anything else.

    numbers, names                    3, 0.05, C, L, tau
    + - * / **                        polynomials and ratios (x ** 2, S_end / S_start - 1)
    < <= > >= == !=                   indicators: 1.0 where true, 0.0 where false; chains (a <= x < b) allowed
    and  or  not                      on indicators (any nonzero value is true)
    a if cond else b                  same as where(cond, a, b)
    min(a, b, ...)  max(a, b, ...)    piecewise linear: floors, caps, ratchets, lookbacks
    clip(x, lo, hi)  abs(x)
    where(cond, a, b)                 a where cond is nonzero, else b
    exp(x)  log(x)  sqrt(x)
    prev(S)                           a state's value before this period's update (payoff only)

So the function class is piecewise polynomial with digital (indicator) switches, closed under composition:
linear and polynomial maps, caps and floors, digital and memory coupons, and their combinations.
"""
from __future__ import annotations

import ast

import numpy as np

FUNCTIONS = {"min": (2, None), "max": (2, None), "clip": (3, 3), "abs": (1, 1), "where": (3, 3),
             "exp": (1, 1), "log": (1, 1), "sqrt": (1, 1), "prev": (1, 1)}
_NONLINEAR = {"min", "max", "clip", "abs"}      # optionality: their arguments must not be deterministic only
_BIN = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide, ast.Pow: np.power}
_CMP = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater, ast.GtE: np.greater_equal,
        ast.Eq: np.equal, ast.NotEq: np.not_equal}


class ExprError(ValueError):
    pass


def _what(node) -> str:
    return {ast.Attribute: "attribute access", ast.Subscript: "indexing", ast.Lambda: "lambda",
            ast.Constant: "a non-numeric constant", ast.Call: "this call"}.get(type(node), type(node).__name__)


class Expr:
    """A parsed expression. `names`: free names it reads (prev(S) counts as S); `prev_names`: states read
    through prev(); `nonlinear`: per nonlinear construct (min/max/clip/abs, an indicator, a condition), the
    names it depends on."""

    def __init__(self, text: str):
        if not isinstance(text, str) or not text.strip():
            raise ExprError("expected a non-empty formula string")
        self.text = text
        try:
            self.tree = ast.parse(text.strip(), mode="eval").body
        except SyntaxError as e:
            raise ExprError(f"cannot parse '{text}': {e.msg}") from None
        self.names, self.prev_names, self.nonlinear = set(), set(), []
        self._check(self.tree)

    # ------------------------------------------------------------------ static checks
    def _check(self, n) -> set:
        """Validate node n; return the names it depends on."""
        if isinstance(n, ast.Constant):
            if isinstance(n.value, bool) or not isinstance(n.value, (int, float)):
                raise ExprError(f"'{self.text}': only numeric constants are allowed, not {n.value!r}")
            return set()
        if isinstance(n, ast.Name):
            if n.id in FUNCTIONS:
                raise ExprError(f"'{self.text}': '{n.id}' is a function; call it, e.g. {n.id}(...)")
            self.names.add(n.id)
            return {n.id}
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            return self._check(n.left) | self._check(n.right)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd, ast.Not)):
            d = self._check(n.operand)
            if isinstance(n.op, ast.Not):
                self.nonlinear.append(d)
            return d
        if isinstance(n, ast.Compare) and all(type(o) in _CMP for o in n.ops):
            d = set().union(*(self._check(x) for x in [n.left, *n.comparators]))
            self.nonlinear.append(d)
            return d
        if isinstance(n, ast.BoolOp):
            d = set().union(*(self._check(x) for x in n.values))
            self.nonlinear.append(d)
            return d
        if isinstance(n, ast.IfExp):
            c = self._check(n.test)
            self.nonlinear.append(c)
            return c | self._check(n.body) | self._check(n.orelse)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FUNCTIONS:
            f = n.func.id
            if n.keywords or any(isinstance(a, ast.Starred) for a in n.args):
                raise ExprError(f"'{self.text}': {f}() takes positional arguments only")
            lo, hi = FUNCTIONS[f]
            if len(n.args) < lo or (hi is not None and len(n.args) > hi):
                want = f"{lo}" if lo == hi else f"at least {lo}"
                raise ExprError(f"'{self.text}': {f}() takes {want} argument(s), got {len(n.args)}")
            if f == "prev":
                if not isinstance(n.args[0], ast.Name):
                    raise ExprError(f"'{self.text}': prev() takes a state name, e.g. prev(C)")
                self.prev_names.add(n.args[0].id)
                self.names.add(n.args[0].id)
                return {n.args[0].id}
            deps = [self._check(a) for a in n.args]
            if f in _NONLINEAR:
                self.nonlinear.append(set().union(*deps))
            elif f == "where":
                self.nonlinear.append(deps[0])
            return set().union(*deps)
        if isinstance(n, ast.Call):
            name = n.func.id if isinstance(n.func, ast.Name) else ast.unparse(n.func)
            raise ExprError(f"'{self.text}': unknown function '{name}'. Available: {sorted(FUNCTIONS)}")
        raise ExprError(f"'{self.text}': {_what(n)} is not allowed in a formula")

    # ------------------------------------------------------------------ evaluation
    def __call__(self, env: dict, prev: dict | None = None) -> np.ndarray:
        """Evaluate with `env` (name -> number or per-path array); `prev` serves prev(S)."""
        return np.asarray(self._eval(self.tree, env, prev or {}), dtype=float)

    def _eval(self, n, env, prev):
        if isinstance(n, ast.Constant):
            return float(n.value)
        if isinstance(n, ast.Name):
            return env[n.id]
        if isinstance(n, ast.BinOp):
            with np.errstate(all="ignore"):      # a non-finite result is reported by the caller, with its period
                return _BIN[type(n.op)](self._eval(n.left, env, prev), self._eval(n.right, env, prev))
        if isinstance(n, ast.UnaryOp):
            x = self._eval(n.operand, env, prev)
            if isinstance(n.op, ast.USub):
                return -x
            if isinstance(n.op, ast.Not):
                return (np.asarray(x) == 0).astype(float)
            return x
        if isinstance(n, ast.Compare):
            out, left = True, self._eval(n.left, env, prev)
            for op, c in zip(n.ops, n.comparators):
                right = self._eval(c, env, prev)
                out = out & _CMP[type(op)](left, right)
                left = right
            return np.asarray(out, dtype=float)
        if isinstance(n, ast.BoolOp):
            vals = [np.asarray(self._eval(x, env, prev)) != 0 for x in n.values]
            red = np.logical_and if isinstance(n.op, ast.And) else np.logical_or
            return red.reduce(np.broadcast_arrays(*vals)).astype(float)
        if isinstance(n, ast.IfExp):
            return np.where(np.asarray(self._eval(n.test, env, prev)) != 0,
                            self._eval(n.body, env, prev), self._eval(n.orelse, env, prev))
        f = n.func.id
        if f == "prev":
            return prev[n.args[0].id]
        a = [self._eval(x, env, prev) for x in n.args]
        if f == "min":
            return np.minimum.reduce(np.broadcast_arrays(*a))
        if f == "max":
            return np.maximum.reduce(np.broadcast_arrays(*a))
        if f == "clip":
            return np.minimum(np.maximum(a[0], a[1]), a[2])
        if f == "where":
            return np.where(np.asarray(a[0]) != 0, a[1], a[2])
        with np.errstate(all="ignore"):
            return {"abs": np.abs, "exp": np.exp, "log": np.log, "sqrt": np.sqrt}[f](a[0])
