"""Market data components: yield curves and Black volatility term structures, built from
QuantLib rate helpers, piecewise curves and Black variance term structures.
"""
from __future__ import annotations

from typing import Literal

import QuantLib as ql
from pydantic import Field, model_validator

from . import conventions as cv
from .registry import SpecModel, register


# ---------------------------------------------------------------- curves
class CurveFactory:
    """build(spec, ref_date: ql.Date) -> ql.YieldTermStructureHandle"""


class _Quote(SpecModel):
    tenor: str
    rate: float


@register("curve", "flat")
class FlatCurve(CurveFactory):
    description = "Flat continuously-compounded zero rate."

    class Spec(SpecModel):
        rate: float
        day_count: str = "Actual365Fixed"

    @staticmethod
    def build(spec, ref):
        ts = ql.FlatForward(ref, spec.rate, cv.day_count(spec.day_count), ql.Continuous)
        ts.enableExtrapolation()
        return ql.YieldTermStructureHandle(ts)


@register("curve", "zero")
class ZeroCurve(CurveFactory):
    description = "Continuously-compounded zero rates at tenors, linear interpolation, flat extrapolation."

    class Spec(SpecModel):
        tenors: list[str] = Field(min_length=1)
        rates: list[float] = Field(min_length=1)
        day_count: str = "Actual365Fixed"

        @model_validator(mode="after")
        def _same_len(self):
            if len(self.tenors) != len(self.rates):
                raise ValueError("tenors and rates must have the same length")
            return self

    @staticmethod
    def build(spec, ref):
        dates = [ref] + [ref + cv.period(t) for t in spec.tenors]
        rates = [spec.rates[0]] + list(spec.rates)
        ts = ql.ZeroCurve(dates, rates, cv.day_count(spec.day_count))
        ts.enableExtrapolation()
        return ql.YieldTermStructureHandle(ts)


@register("curve", "bootstrap")
class BootstrapCurve(CurveFactory):
    description = ("Bootstrapped from deposit / par swap quotes with the currency's conventions "
                   "(annual 30/360 fixed vs 6M floating swaps). Curve day count Actual365Fixed.")

    class Spec(SpecModel):
        currency: Literal["USD", "EUR", "JPY", "GBP", "TWD"] = "USD"
        deposit: list[_Quote] = Field(default_factory=list)
        swap: list[_Quote] = Field(default_factory=list)
        method: Literal["logLinearDiscount", "logCubicDiscount", "linearZero", "cubicZero",
                        "linearForward", "splineCubicDiscount"] = "linearZero"

        @model_validator(mode="after")
        def _non_empty(self):
            if not self.deposit and not self.swap:
                raise ValueError("bootstrap needs at least one deposit or swap quote")
            return self

    @staticmethod
    def build(spec, ref):
        conv = cv.curve_conventions(spec.currency)
        helpers = [ql.DepositRateHelper(ql.QuoteHandle(ql.SimpleQuote(q.rate)), cv.period(q.tenor),
                                        conv.settlement_days, conv.calendar, conv.roll,
                                        conv.end_of_month, conv.money_market_day_count)
                   for q in spec.deposit]
        index = conv.ibor_index()
        helpers += [ql.SwapRateHelper(ql.QuoteHandle(ql.SimpleQuote(q.rate)), cv.period(q.tenor),
                                      conv.calendar, conv.fixed_frequency, conv.roll,
                                      conv.fixed_day_count, index)
                    for q in spec.swap]
        curve = _PIECEWISE[spec.method](ref, helpers, ql.Actual365Fixed())
        curve.enableExtrapolation()
        return ql.YieldTermStructureHandle(curve)


_PIECEWISE = {
    "logLinearDiscount": ql.PiecewiseLogLinearDiscount,
    "logCubicDiscount": ql.PiecewiseLogCubicDiscount,
    "linearZero": ql.PiecewiseLinearZero,
    "cubicZero": ql.PiecewiseCubicZero,
    "linearForward": ql.PiecewiseLinearForward,
    "splineCubicDiscount": ql.PiecewiseSplineCubicDiscount,
}


def simulation_curve(h: ql.YieldTermStructureHandle, ref: ql.Date, horizon: ql.Date):
    """Re-express a curve on Actual365Fixed so it can serve as the simulation time axis.

    QuantLib processes measure time with the discount curve's day counter; 30/360 counters
    map distinct dates to equal times (e.g. 30th -> 31st), which breaks path generation.
    Discount factors are sampled on every calendar day to `horizon` and interpolated
    log-linearly, so DF(date) is unchanged on every date. Returns (handle, resampled?)."""
    if h.dayCounter() == ql.Actual365Fixed():
        return h, False
    dates, d = [], ref
    while d <= horizon:
        dates.append(d)
        d += 1
    curve = ql.DiscountCurve(dates, [h.discount(x) for x in dates], ql.Actual365Fixed())
    curve.enableExtrapolation()
    return ql.YieldTermStructureHandle(curve), True


# ---------------------------------------------------------------- vols
class VolFactory:
    """build(spec, ref, day_count, spot, calendar) -> ql.BlackVolTermStructureHandle
    max_tenor(spec) -> ql.Period | None  (None = no horizon limit)"""


@register("vol", "constant")
class ConstantVol(VolFactory):
    description = "Single flat Black volatility."

    class Spec(SpecModel):
        value: float = Field(gt=0, lt=5)

    @staticmethod
    def build(spec, ref, dc, spot, cal):
        return ql.BlackVolTermStructureHandle(ql.BlackConstantVol(ref, cal, spec.value, dc))

    @staticmethod
    def max_tenor(spec):
        return None


@register("vol", "term")
class TermVol(VolFactory):
    description = "ATM Black vol term structure (no smile)."

    class Spec(SpecModel):
        tenors: list[str] = Field(min_length=1)
        vols: list[float] = Field(min_length=1)

        @model_validator(mode="after")
        def _same_len(self):
            if len(self.tenors) != len(self.vols):
                raise ValueError("tenors and vols must have the same length")
            return self

    @staticmethod
    def build(spec, ref, dc, spot, cal):
        ts = ql.BlackVarianceCurve(ref, [ref + cv.period(t) for t in spec.tenors], list(spec.vols), dc)
        ts.enableExtrapolation()
        return ql.BlackVolTermStructureHandle(ts)

    @staticmethod
    def max_tenor(spec):
        return cv.period(spec.tenors[-1])


@register("vol", "surface")
class SurfaceVol(VolFactory):
    description = ("Black vol surface: vols[i][j] is the vol for tenors[i] and strikes[j]. "
                   "strike_mode 'relative' means strikes are fractions of spot (1.0 = ATM).")

    class Spec(SpecModel):
        tenors: list[str] = Field(min_length=1)
        strikes: list[float] = Field(min_length=2)
        vols: list[list[float]]
        strike_mode: Literal["absolute", "relative"] = "relative"
        interpolation: Literal["bicubic", "bilinear"] = Field(
            "bicubic", description="bicubic gives smoother Dupire local vol")

        @model_validator(mode="after")
        def _shape(self):
            if len(self.vols) != len(self.tenors):
                raise ValueError(f"vols needs {len(self.tenors)} rows (one per tenor), got {len(self.vols)}")
            for i, row in enumerate(self.vols):
                if len(row) != len(self.strikes):
                    raise ValueError(f"vols[{i}] needs {len(self.strikes)} values (one per strike), got {len(row)}")
            if sorted(self.strikes) != list(self.strikes):
                raise ValueError("strikes must be increasing")
            return self

    @staticmethod
    def absolute_strikes(spec, spot):
        return [k * spot for k in spec.strikes] if spec.strike_mode == "relative" else list(spec.strikes)

    @classmethod
    def build(cls, spec, ref, dc, spot, cal):
        expiries = [ref + cv.period(t) for t in spec.tenors]
        strikes = cls.absolute_strikes(spec, spot)
        m = ql.Matrix(len(strikes), len(expiries))
        for i in range(len(strikes)):
            for j in range(len(expiries)):
                m[i][j] = spec.vols[j][i]
        # constant extrapolation outside the quoted strikes: extrapolating the wings otherwise
        # produces negative Dupire local variance
        surf = ql.BlackVarianceSurface(ref, cal, expiries, strikes, m, dc,
                                       ql.BlackVarianceSurface.ConstantExtrapolation,
                                       ql.BlackVarianceSurface.ConstantExtrapolation)
        surf.setInterpolation(spec.interpolation)
        surf.enableExtrapolation()
        return ql.BlackVolTermStructureHandle(surf)

    @classmethod
    def calibration_quotes(cls, spec, spot) -> list[tuple[ql.Period, float, float]]:
        """Every quoted point as (expiry tenor, absolute strike, Black vol)."""
        strikes = cls.absolute_strikes(spec, spot)
        return [(cv.period(t), k, spec.vols[i][j])
                for i, t in enumerate(spec.tenors) for j, k in enumerate(strikes)]

    @staticmethod
    def max_tenor(spec):
        return cv.period(spec.tenors[-1])
