"""Structured derivative pricer: declarative trade spec -> validated component graph -> price.

Importing the package registers every built-in component (see registry.py)."""
from . import market, schedules, models, rates, observables, payoffs, coupons, analytic  # noqa: F401  (registration)
from .engine import build_plan, run, load_document, apply_overrides  # noqa: F401
from .registry import catalog  # noqa: F401
