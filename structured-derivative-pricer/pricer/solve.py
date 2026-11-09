"""Solve one term-sheet parameter so the price hits a target: the coupon that prices a note at 98%, the barrier that
makes an autocall worth par, the strike for a given premium. Not a sensitivity: the answer is a parameter value.

    out = solve(doc, base_dir, "trade.legs.0.rate", target=980000, bracket=(0.0, 0.2))

A Monte Carlo price has no derivative to hand and no inverse, so the search is bracketed (Brent). Three things make
it cheap:

1. Common random numbers. Every evaluation of one stage uses the same seed and path count, so the price is a
   deterministic, smooth function f_N(x) of the parameter (for a coupon rate under a fixed exercise rule it is even
   affine). Brent then converges superlinearly, as on any smooth function; no noise reaches the root search. With a
   new seed per evaluation the function would be noisy and no bracketed method would converge.
2. A cheap probe stage, then the full path count. The MC error of the root is
   sigma_x = std_error(price) / |dprice/dx|. Stage 1 runs few paths (final/64) over the whole bracket, where Brent
   may need 6-12 evaluations. It returns a root and the slope dprice/dx. The final stage starts at that root and
   takes secant steps with that slope (a Newton step with a stale derivative), each overshooting by half the
   tolerance so the sign flips: a bracket in 2 evaluations, the root in 3-4. Each stage stops once its bracket is a
   small fraction of its own sigma_x: solving finer than the MC noise buys nothing.
3. Reuse of the simulation. Within a stage only the nodes the parameter feeds (its leg or event, then the price) are
   recomputed; curves, model calibration and the simulated paths come from the stage's first run, so a further
   evaluation costs only the payoff (plus the exercise-rule refit of a Bermudan).

Measured at 200,000 paths (examples/, tests/test_solve.py): the autocallable's coupon (price affine in the coupon,
cheap re-evaluation) takes 3.6 s solved directly at full paths and 3.7 s with the probe; the callable bond's coupon
(each evaluation refits Longstaff-Schwartz) takes 19.4 s directly (11 full evaluations) and 7.0 s with the probe
(4 full evaluations). A ladder of intermediate stages (growth=4) only adds simulations: each stage still needs 3-4
evaluations, and the secant start already lands the final stage near its root.

Bisection is not used on its own: on f_N it gains one bit per evaluation where Brent gains several, and Brent already
falls back to bisection steps where f_N is not smooth (a barrier level under discrete observation makes f_N a step
function; the root is then a jump, found to within the bracket).

The final stage prices at `pricing.num_paths`, or, with `x_tol`, at as many paths as the probe predicts for the
root's confidence half-width to reach x_tol (capped by max_paths; another stage follows if the prediction falls
short).
"""
from __future__ import annotations

import copy
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import NormalDist

from .engine import FIT_FIXINGS, OverrideError, _split_path, build_plan, get_path, run, set_path

MAX_NUM_PATHS = 2_000_000


class SolveError(ValueError):
    """The search cannot proceed; `path` is the input path to blame (or '')."""

    def __init__(self, message: str, path: str = "", details: dict | None = None):
        super().__init__(message)
        self.path = path
        self.details = details or {}


# ------------------------------------------------------------------ Brent's method
class BrentError(ValueError):
    pass


def brent(f, a, b, fa=None, fb=None, xtol=1e-12, rtol=4 * sys.float_info.epsilon, maxiter=100):
    """Root of f in [a, b] (f(a), f(b) of opposite sign) by Brent's method, as scipy.optimize.brentq.
    fa, fb: already known values, saving two evaluations. Returns (root, f(root), (lo, hi)): the last bracket,
    which holds the sign change and contains the root."""
    xpre, xcur = float(a), float(b)
    fpre = f(xpre) if fa is None else fa
    fcur = f(xcur) if fb is None else fb
    if fpre == 0:
        return xpre, fpre, (xpre, xpre)
    if fcur == 0:
        return xcur, fcur, (xcur, xcur)
    if (fpre > 0) == (fcur > 0):
        raise BrentError(f"f(a) = {fpre:g} and f(b) = {fcur:g} have the same sign")
    xblk = fblk = spre = scur = 0.0
    for _ in range(maxiter):
        if fpre != 0 and fcur != 0 and (fpre > 0) != (fcur > 0):
            xblk, fblk = xpre, fpre
            spre = scur = xcur - xpre
        if abs(fblk) < abs(fcur):
            xpre, xcur, xblk = xcur, xblk, xcur
            fpre, fcur, fblk = fcur, fblk, fcur
        delta = (xtol + rtol * abs(xcur)) / 2
        sbis = (xblk - xcur) / 2
        if fcur == 0 or abs(sbis) < delta:
            return xcur, fcur, tuple(sorted((xcur, xblk)))
        if abs(spre) > delta and abs(fcur) < abs(fpre):
            if xpre == xblk:                                   # secant
                stry = -fcur * (xcur - xpre) / (fcur - fpre)
            else:                                              # inverse quadratic interpolation
                dpre = (fpre - fcur) / (xpre - xcur)
                dblk = (fblk - fcur) / (xblk - xcur)
                stry = -fcur * (fblk * dblk - fpre * dpre) / (dblk * dpre * (fblk - fpre))
            if 2 * abs(stry) < min(abs(spre), 3 * abs(sbis) - delta):
                spre, scur = scur, stry
            else:
                spre = scur = sbis
        else:
            spre = scur = sbis
        xpre, fpre = xcur, fcur
        xcur += scur if abs(scur) > delta else (delta if sbis > 0 else -delta)
        fcur = f(xcur)
    raise BrentError(f"no convergence in {maxiter} iterations")


# ------------------------------------------------------------------ evaluation with reuse
@dataclass
class Evaluation:
    x: float
    num_paths: int
    price: float
    std_error: float
    seconds: float
    reused: bool
    result: dict = field(repr=False, default_factory=dict)


class _Pricer:
    """Prices the document with the parameter set to x at N paths; reuses the parameter-free nodes of the first
    run at the same N."""

    def __init__(self, doc, base, param, log, base_plan):
        self.doc, self.base, self.param, self.log = doc, base, param, log
        self.cache = {}            # num_paths -> (signature, {node: result})
        self.evaluations = []
        self.direct = _direct_nodes(base_plan, param)

    def plan(self, x, n):
        d = copy.deepcopy(self.doc)
        set_path(d, self.param, float(x), create=False)
        if n is not None:
            set_path(d, "pricing.num_paths", int(n))
        p = build_plan(d, self.base)
        if not p.report.ok:
            raise SolveError(f"the input is invalid with {self.param} = {x:.10g} (the search went there while "
                             "widening its bracket, or the bracket holds it): give a --bracket of valid values whose "
                             "two ends price on either side of the target",
                             self.param, {"x": x, "errors": p.report.to_dict()["errors"]})
        return p

    def __call__(self, x, n) -> Evaluation:
        t = time.perf_counter()
        plan = self.plan(x, n)
        sig = _signature(plan)
        key = n if plan.method == "mc" else None
        preset, reused = {}, False
        cached = self.cache.get(key)
        if self.direct is not None and cached and cached[0] == sig:
            preset, reused = cached[1], True
        out = {} if self.direct is not None and not reused else None
        res = run(plan, preset=preset, node_results=out)
        if out is not None:
            self.cache = {key: (sig, _reusable(plan, out["results"], self.direct))}   # one N at a time
        e = Evaluation(float(x), plan.input.pricing.num_paths if plan.method == "mc" else 0, res["price"],
                       res.get("std_error") or 0.0, time.perf_counter() - t, reused, res)
        self.evaluations.append(e)
        self.log(f"  N={e.num_paths:>8d}  x={e.x:.10g}  price={e.price:.6f}  se={e.std_error:.4g}  "
                 f"{e.seconds:.2f}s{' (reused paths)' if reused else ''}")
        return e


def _direct_nodes(plan, param):
    """Graph nodes the parameter feeds directly; None if it may feed the simulation (then nothing is reused).
    Leg and event ids come from the validated plan, whose input paths are trade.legs[i] / trade.termination_events[i]."""
    parts = _split_path(param)
    if len(parts) > 3 and parts[:2] in (["trade", "legs"], ["trade", "termination_events"]) and parts[2].isdigit():
        owner = f"trade.{parts[1]}[{parts[2]}]"
        kind = "leg" if parts[1] == "legs" else "event"
        for _, spec, path in plan.trade.legs + plan.trade.events:
            if path == owner:
                return {f"{kind}:{spec.id}"}
        return None
    if len(parts) > 2 and parts[0] == "observables" and f"observable:{parts[1]}" in plan.nodes:
        return {f"observable:{parts[1]}"}
    return None


def _signature(plan):
    """What the simulation depends on besides the reused nodes' own specs: same signature, same paths."""
    return (plan.method, plan.input.pricing.num_paths, plan.input.pricing.seed,
            {k: tuple(v) for k, v in plan.sim_dates.items()}, tuple(plan.pay_dates),
            tuple((n.key, tuple(n.deps)) for n in plan.nodes.values()))


def _reusable(plan, results, direct):
    dependents = {}
    for n in plan.nodes.values():
        for d in n.deps:
            dependents.setdefault(d, set()).add(n.key)
    stale, todo = set(), [k for k in direct if k in plan.nodes] + ["price", "analytic_check", "exercise_policy"]
    while todo:
        k = todo.pop()
        if k not in stale:
            stale.add(k)
            todo.extend(dependents.get(k, ()))
    keep = {k: v for k, v in results.items() if k in plan.nodes and k not in stale}
    if FIT_FIXINGS in results and "model" not in stale:      # the exercise rule's fit paths depend on the model only
        keep[FIT_FIXINGS] = results[FIT_FIXINGS]
    return keep


# ------------------------------------------------------------------ the search
def _even(n, antithetic):
    n = int(n)
    return n + (n % 2) if antithetic else n


def solve(doc: dict, base_dir: Path | str, param: str, target: float, bracket=None, x_tol: float | None = None,
          start_paths: int | None = None, max_paths: int | None = None, growth: float | None = None,
          width_sigmas: float = 4.0, max_expansions: int = 12, log=None, plan=None) -> dict:
    """Find x with price(param = x) = target. See the module docstring for the method.
    growth: path multiplier between stages; None goes from the probe straight to the final path count.
    plan: the already-built plan of `doc` as given, if the caller has one (saves building it again)."""
    log = log or (lambda m: print(m, file=sys.stderr))
    t0 = time.perf_counter()
    try:
        x0 = get_path(doc, param)
    except OverrideError as e:
        raise SolveError(str(e), param) from None
    if isinstance(x0, bool) or not isinstance(x0, (int, float)):
        raise SolveError(f"{param} is {x0!r}; only a number can be solved for (write the field as a number "
                         "first, e.g. a single rate instead of a per-period list)", param)
    if _split_path(param)[0] == "pricing":
        raise SolveError(f"{param} is a pricing setting, not a term-sheet value: solving for it would change the "
                         "paths the search holds fixed", param)
    for name, v in (("start_paths", start_paths), ("max_paths", max_paths)):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            raise SolveError(f"{name} must be a positive integer, got {v!r}", name)
    if growth is not None and growth <= 1:
        raise SolveError("growth must be > 1", "growth")
    if x_tol is not None and not x_tol > 0:
        raise SolveError("x_tol must be positive", "x_tol")
    if bracket is not None:
        lo, hi = sorted(float(b) for b in bracket)
        if not lo < hi:
            raise SolveError("the bracket needs two different values", "bracket")
    else:
        step = 0.1 * abs(x0) if x0 else 0.01
        lo, hi = x0 - step, x0 + step

    base = plan if plan is not None else build_plan(copy.deepcopy(doc), base_dir)
    if not base.report.ok:
        raise SolveError("the input is invalid; run validate", "", {"errors": base.report.to_dict()["errors"]})
    price = _Pricer(doc, base_dir, param, log, base)
    s = base.input.pricing
    mc = base.method == "mc"
    conf = s.confidence
    z = NormalDist().inv_cdf(0.5 + conf / 2)
    cap = min(max_paths or MAX_NUM_PATHS, MAX_NUM_PATHS)
    final = cap if x_tol is not None else min(s.num_paths, cap)
    floor = min(s.num_paths, cap)       # x_tol may stop the search only at or above pricing.num_paths
    if mc:
        n = _even(min(start_paths or max(2000, min(final, s.num_paths) // 64), final), s.antithetic)
    else:
        n = final = None

    def g_at(n):
        return lambda x: price(x, n).price - target

    def tolerance(sig, last):
        """Bracket width at which a stage stops: a fraction of the MC error of its root."""
        if not mc or sig == 0:
            t = (x_tol or 0) * 0.01 or 1e-12 * max(1.0, abs(lo), abs(hi))
        else:
            t = (0.02 if last else 0.1) * sig
        return min(t, 0.1 * x_tol) if x_tol is not None else t

    def widen(g, a, b, ga, gb, k0):
        """Widen [a, b] towards the end closer to the target until it holds a sign change."""
        expansions = 0
        while (ga > 0) == (gb > 0) and ga != 0 and gb != 0:
            if expansions >= max_expansions:
                pts = sorted((e.x, e.price) for e in price.evaluations[k0:])
                raise SolveError(
                    f"no price equal to the target {target:g} between {param} = {a:.6g} and {b:.6g} after "
                    f"{expansions} widenings (price there: {ga + target:.6g} .. {gb + target:.6g}); give a --bracket "
                    "whose two ends price on either side of the target", "bracket",
                    {"evaluated": [{"x": x, "price": p} for x, p in pts]})
            w = b - a
            if abs(ga) < abs(gb):
                b, gb = a, ga
                a = a - 1.6 * w
                ga = g(a)
            else:
                a, ga = b, gb
                b = b + 1.6 * w
                gb = g(b)
            a, b, ga, gb = (a, b, ga, gb) if a < b else (b, a, gb, ga)
            expansions += 1
        return a, b, ga, gb, expansions

    def secant_bracket(g, x, gx, slope, xtol, A, B, steps=4):
        """From the previous stage's root and slope: secant steps, each overshooting by xtol/2 so the sign flips
        as soon as the step is right, kept inside [A, B]. Returns a bracket, or None after `steps` steps without a
        sign change (or when a step would leave [A, B])."""
        for _ in range(steps):
            step = -gx / slope
            xn = min(max(x + step + math.copysign(xtol / 2, step), A), B)
            if xn == x:
                return None
            gn = g(xn)
            if gn == 0 or (gn > 0) != (gx > 0):
                return (x, xn, gx, gn) if x < xn else (xn, x, gn, gx)
            if gn != gx:
                slope = (gn - gx) / (xn - x)
            x, gx = xn, gn
        return None

    stages, warnings_ = [], []
    sigma_x, root, slope, stop = None, None, None, None
    A = B = None                                    # stage 1's bracket: every later point stays inside it
    while True:
        st = time.perf_counter()
        k0 = len(price.evaluations)
        g = g_at(n)
        last = (not mc) or n >= final
        expansions, start = 0, "bracket"
        if sigma_x is None:                         # first stage: the given (or default) bracket
            a, b = lo, hi
            ga, gb = g(a), g(b)
            a, b, ga, gb, expansions = widen(g, a, b, ga, gb, k0)
            A, B = a, b
            slope = (gb - ga) / (b - a)
            if slope == 0:
                raise SolveError(f"the price does not change with {param} between {a:.6g} and {b:.6g}", param)
            xtol = tolerance(price.evaluations[-1].std_error / abs(slope), last)
        else:                                       # later stages: start at the last root, step with its slope
            g1 = g(root)
            xtol = tolerance(price.evaluations[-1].std_error / abs(slope), last)
            br = secant_bracket(g, root, g1, slope, xtol, A, B)
            if br is not None:
                a, b, ga, gb = br
                start = "secant from the previous stage"
            else:                                   # +/- width_sigmas around the last root, then all of [A, B]
                half = width_sigmas * sigma_x
                a, b = max(A, root - half), min(B, root + half)
                ga, gb = g(a), g(b)
                start = "previous root +/- %g sigma" % width_sigmas
                if (ga > 0) == (gb > 0) and ga != 0 and gb != 0 and (a, b) != (A, B):
                    a, b = A, B
                    ga, gb = g(a), g(b)
                    start = "stage 1 bracket"
                if (ga > 0) == (gb > 0) and ga != 0 and gb != 0:
                    raise SolveError(
                        f"at {n} paths the price no longer crosses the target {target:g} between {param} = {A:.6g} "
                        f"and {B:.6g} (price {ga + target:.6g} .. {gb + target:.6g}): the root at fewer paths was "
                        "within Monte Carlo error of a bracket end; give a wider --bracket", "bracket")
        try:
            root, _, _ = brent(g, a, b, ga, gb, xtol=xtol)
        except BrentError as e:
            raise SolveError(f"root search failed: {e}") from None
        at = next(e for e in reversed(price.evaluations) if e.x == root)
        here = price.evaluations[k0:]               # slope of f_N from this stage's two most distant points
        p, q = min(here, key=lambda e: e.x), max(here, key=lambda e: e.x)
        if q.x > p.x and q.price != p.price:
            slope = (q.price - p.price) / (q.x - p.x)
        sigma_x = at.std_error / abs(slope)
        stages.append({
            "num_paths": n if mc else None, "start": start, "bracket": [a, b], "bracket_widenings": expansions,
            "evaluations": len(price.evaluations) - k0, "solution": root, "price": at.price,
            "std_error": at.std_error, "slope": slope, "solution_std_error": sigma_x, "xtol": xtol,
            "seconds": round(time.perf_counter() - st, 3)})
        log(f"stage {len(stages)}: N={n} root={root:.10g} +/- {z * sigma_x:.3g} ({len(price.evaluations) - k0} evals)")
        if not mc or sigma_x == 0:
            stop = "closed form" if not mc else "price has no Monte Carlo error"
            break
        if x_tol is not None and z * sigma_x <= x_tol and n >= floor:
            stop = "x_tol reached"
            break
        if last:
            stop = "x_tol not reached: path cap hit" if x_tol is not None else "final path count reached"
            break
        n_next = n * growth if growth else final
        if x_tol is not None:                       # paths for the CI to reach x_tol, if fewer than growth x
            need = n * (z * sigma_x / x_tol) ** 2 * 1.1
            n_next = max(min(n_next, max(need, 1.5 * n)), floor)
        n = _even(min(n_next, final), s.antithetic)

    final_eval = next(e for e in reversed(price.evaluations) if e.x == root)
    gap = final_eval.price - target
    if x_tol is not None and stop.startswith("x_tol not"):
        warnings_.append(f"[WARNING] solve: the {conf:.0%} interval of {param} is +/-{z * sigma_x:.3g}, wider than "
                         f"x_tol {x_tol:g}, at the path cap ({final}); raise --max-num-paths or loosen x_tol")
    if mc and final_eval.std_error and abs(gap) > final_eval.std_error:
        warnings_.append(f"[WARNING] solve: the price at the solution misses the target by {gap:.6g} "
                         f"({abs(gap) / final_eval.std_error:.1f} std errors): the price jumps in {param} on these "
                         "paths (e.g. a barrier level under discrete observation). The solution is the jump.")
    res = final_eval.result
    return {
        "ok": True,
        "parameter": param,
        "target": target,
        "initial_value": x0,
        "solution": root,
        "solution_std_error": sigma_x,
        "solution_confidence_interval": [root - z * sigma_x, root + z * sigma_x],
        "confidence": conf,
        "price_at_solution": final_eval.price,
        "price_std_error": final_eval.std_error,
        "price_minus_target": gap,
        "slope": stages[-1]["slope"],
        "method": base.method,
        "num_paths": final_eval.num_paths if mc else None,
        "stop_reason": stop,
        "stages": stages,
        "evaluations": len(price.evaluations),
        "paths_simulated": sum(e.num_paths for e in price.evaluations if not e.reused),
        "seconds": round(time.perf_counter() - t0, 3),
        "warnings": warnings_ + list(res.get("warnings", [])),
        "reprice": f"--set {param}={root!r}" + (f" --set pricing.num_paths={final_eval.num_paths}" if mc else ""),
        "result": res,
    }
