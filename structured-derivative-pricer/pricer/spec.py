"""Top-level shape of a pricing request and of a market-data document.

Component bodies (curves, vols, schedules, model, observables, legs, conditions, events) are kept
as raw dicts here and validated against their registered factory Spec later, so an
unknown `type` or a wrong field is reported with its exact input path.
"""
from __future__ import annotations

import datetime as dt
from typing import Literal, Union

from pydantic import Field, model_validator

from .registry import SpecModel

DateLike = Union[dt.date, str]   # ISO date, a symbol (valuation_date, maturity, ...) or a tenor


class PricingSettings(SpecModel):
    method: Literal["auto", "mc", "analytic"] = Field(
        "auto", description="auto: analytic when a QuantLib closed form matches the trade exactly, else Monte Carlo")
    num_paths: int = Field(20000, ge=100, le=2_000_000)
    seed: int = Field(42, description="RNG seed; same seed + same input => same price")
    antithetic: bool = True
    confidence: float = Field(0.95, gt=0.5, lt=1.0)
    cross_check: bool = Field(
        True, description="when MC is used and an analytic pricer also matches, price both and report the gap")
    steps_per_year: int = Field(
        52, ge=0, description="minimum simulation time steps per year (grid refinement between fixing dates)")
    max_memory_mb: int = Field(2000, description="abort before simulation if path storage would exceed this")
    exercise_fit_paths: int | None = Field(
        None, ge=0, le=2_000_000,
        description="Bermudan exercise: paths of the independent simulation the Longstaff-Schwartz rule is fitted "
                    "on (default num_paths); 0 fits on the pricing paths themselves (in sample, biased up)")

    @model_validator(mode="after")
    def _even_paths(self):
        if self.antithetic and self.num_paths % 2:
            raise ValueError("num_paths must be even when antithetic is true")
        if self.antithetic and self.exercise_fit_paths and self.exercise_fit_paths % 2:
            raise ValueError("exercise_fit_paths must be even when antithetic is true")
        return self


class TradeSpec(SpecModel):
    id: str = "trade"
    currency: str = "USD"
    effective_date: DateLike | None = Field(
        None, description="accrual start of the trade; default valuation_date")
    maturity: DateLike = Field(description="ISO date, or tenor from effective_date (e.g. '1Y')")
    calendar: str = Field("WeekendsOnly", description="adjusts dates given as tenors (maturity: 5Y, 1Y keys, ...)")
    business_day_convention: str = Field("ModifiedFollowing", description="for dates given as tenors")
    reference_levels: dict[str, float] = Field(
        default_factory=dict,
        description="initial fixing per underlying for relative strikes/barriers; default is today's spot")
    legs: list[dict] = Field(min_length=1)
    termination_events: list[dict] = Field(default_factory=list)


class PricingInput(SpecModel):
    valuation_date: dt.date
    market_data: Union[str, dict] = Field(
        description="path to a market-data YAML/JSON (relative to this file) or the document inline")
    model: dict
    schedules: dict[str, dict] = Field(default_factory=dict)
    observables: dict[str, dict] = Field(
        default_factory=dict, description="named observables (rates, spreads, baskets, worst-of) read by legs, "
                                          "ranges and barriers; model underlyings are usable by name without one")
    trade: TradeSpec
    pricing: PricingSettings = Field(default_factory=PricingSettings)


class EquityData(SpecModel):
    spot: float = Field(gt=0)
    dividend: dict = Field(default_factory=lambda: {"type": "flat", "rate": 0.0},
                           description="a curve spec (flat/zero) for the continuous dividend yield")
    vol: dict = Field(description="a vol spec: constant / term / surface")
    calendar: str = "WeekendsOnly"


class MarketData(SpecModel):
    as_of: dt.date
    curves: dict[str, dict] = Field(default_factory=dict)
    equities: dict[str, EquityData] = Field(default_factory=dict)
    swaption_vols: dict[str, dict] = Field(
        default_factory=dict, description="swaption vol matrices by name (Hull-White calibration)")
