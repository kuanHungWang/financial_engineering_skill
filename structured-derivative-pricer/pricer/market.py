"""Market data components: yield curves and Black volatility term structures.

Built on fastFE (bootstrap_curve, create_black_vol_curve, create_black_vol_surface).
"""
from __future__ import annotations

from typing import Literal

import pandas as pd
import QuantLib as ql
from fastFE import bootstrap_curve, create_black_vol_curve, create_black_vol_surface
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
    description = "Bootstrapped from deposit / swap par quotes with fastFE currency conventions."

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
        # fastFE column names differ per instrument: deposits use 'rates', swaps use 'rate'
        dep = pd.DataFrame({"tenor": [q.tenor for q in spec.deposit],
                            "rates": [q.rate for q in spec.deposit]}) if spec.deposit else None
        swp = pd.DataFrame({"tenor": [q.tenor for q in spec.swap],
                            "rate": [q.rate for q in spec.swap]}) if spec.swap else None
        h = bootstrap_curve(ref, deposit=dep, swap=swp, method=spec.method, currency=spec.currency)
        if isinstance(h, ql.YieldTermStructureHandle):
            return h
        return ql.YieldTermStructureHandle(h)


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
        return create_black_vol_curve(pd.Series(spec.vols, index=spec.tenors), ref, dc)

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
        if spec.interpolation == "bilinear":
            # fastFE layout: index = strikes, columns = tenors
            df = pd.DataFrame([list(col) for col in zip(*spec.vols)],
                              index=cls.absolute_strikes(spec, spot), columns=spec.tenors)
            return create_black_vol_surface(df, ref, dc, cal)
        # same construction as fastFE.create_black_vol_surface, which does not expose the
        # interpolation choice
        expiries = [ref + cv.period(t) for t in spec.tenors]
        strikes = cls.absolute_strikes(spec, spot)
        m = ql.Matrix(len(strikes), len(expiries))
        for i in range(len(strikes)):
            for j in range(len(expiries)):
                m[i][j] = spec.vols[j][i]
        # constant extrapolation outside the quoted strikes: bicubic extrapolation of the
        # wings otherwise produces negative Dupire local variance
        surf = ql.BlackVarianceSurface(ref, cal, expiries, strikes, m, dc,
                                       ql.BlackVarianceSurface.ConstantExtrapolation,
                                       ql.BlackVarianceSurface.ConstantExtrapolation)
        surf.setInterpolation("bicubic")
        surf.enableExtrapolation()
        return ql.BlackVolTermStructureHandle(surf)

    @classmethod
    def calibration_quotes(cls, spec, spot) -> pd.DataFrame:
        """Long-format quotes in the layout fastFE HestonModel.calibrate expects."""
        rows = []
        for i, t in enumerate(spec.tenors):
            for j, k in enumerate(cls.absolute_strikes(spec, spot)):
                rows.append({"expiration": t, "strike": k, "vol": spec.vols[i][j]})
        return pd.DataFrame(rows)

    @staticmethod
    def max_tenor(spec):
        return cv.period(spec.tenors[-1])
