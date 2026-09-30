"""Schedule components: named date lists referenced by legs, conditions and events.

A schedule resolves to a sorted, de-duplicated list of datetime.date. Symbols
`valuation_date`, `effective_date`, `maturity` may be used wherever a date is expected.
"""
from __future__ import annotations

import datetime as dt
from typing import Literal

import QuantLib as ql
from pydantic import Field

from . import conventions as cv
from .registry import SpecModel, register
from .spec import DateLike

SYMBOLS = ("valuation_date", "effective_date", "maturity")


class ResolvedDates(list):
    """Sorted schedule dates; `ql_schedule` keeps the QuantLib schedule they came from (with its
    tenor and regular/stub flags) when there is one, so coupon accruals can use it."""
    ql_schedule = None


class DateContext:
    """Resolves DateLike values. Tenors are added to `anchor` (default effective_date) and the result is
    adjusted with the trade's calendar and business-day convention (QuantLib Calendar.advance)."""

    def __init__(self, valuation_date: dt.date, effective_date: dt.date, maturity: dt.date,
                 calendar: str = "NullCalendar", convention: str = "Unadjusted", unadjusted: dict | None = None):
        self.symbols = {"valuation_date": valuation_date,
                        "effective_date": effective_date, "maturity": maturity}
        self.unadjusted_symbols = {**self.symbols, **(unadjusted or {})}
        self.calendar, self.convention = cv.calendar(calendar), cv.bdc(convention)

    def resolve(self, x, anchor: dt.date | None = None, unadjusted: bool = False) -> dt.date:
        """unadjusted=True gives the date before business-day adjustment, which schedule generation
        needs (QuantLib generates from unadjusted dates and adjusts them itself)."""
        symbols = self.unadjusted_symbols if unadjusted else self.symbols
        if isinstance(x, str) and x.strip() in symbols:
            return symbols[x.strip()]
        if cv.is_tenor(x):
            base = anchor or symbols["effective_date"]
            if unadjusted:
                return cv.from_ql(cv.to_ql(base) + cv.period(x)) if cv.period(x).units() != ql.Days else \
                    cv.from_ql(self.calendar.advance(cv.to_ql(base), cv.period(x), self.convention))
            return cv.from_ql(self.calendar.advance(cv.to_ql(base), cv.period(x), self.convention))
        try:
            return cv.parse_iso(x)
        except ValueError:
            raise ValueError(f"'{x}' is not an ISO date, a tenor, or one of {list(SYMBOLS)}")


class ScheduleFactory:
    """dependencies(spec) -> [schedule names]; resolve(spec, ctx, resolved) -> [date]"""

    @staticmethod
    def dependencies(spec):
        return []


def _adjust(dates, cal_name, conv_name):
    if cal_name is None:
        return dates
    cal, conv = cv.calendar(cal_name), cv.bdc(conv_name)
    return [cv.from_ql(cal.adjust(cv.to_ql(d), conv)) for d in dates]


@register("schedule", "dates")
class ExplicitDates(ScheduleFactory):
    description = "Explicit list of dates (optionally business-day adjusted)."

    class Spec(SpecModel):
        dates: list[DateLike] = Field(min_length=1)
        calendar: str | None = None
        convention: str = "Following"

    @staticmethod
    def resolve(spec, ctx, resolved):
        return sorted(set(_adjust([ctx.resolve(d) for d in spec.dates], spec.calendar, spec.convention)))


@register("schedule", "generated")
class GeneratedSchedule(ScheduleFactory):
    description = ("Regular schedule from start to end. frequency '1D' means every business day of the "
                   "calendar (daily observation / American-style monitoring). The start date is excluded "
                   "unless include_start is true.")

    class Spec(SpecModel):
        start: DateLike = "effective_date"
        end: DateLike = "maturity"
        frequency: str = Field(description="tenor such as 1D, 1W, 1M, 3M, 6M, 1Y")
        calendar: str = "WeekendsOnly"
        convention: str = "ModifiedFollowing"
        rule: Literal["Forward", "Backward"] = "Backward"
        end_of_month: bool = False
        include_start: bool = False

    @staticmethod
    def resolve(spec, ctx, resolved):
        start = ctx.resolve(spec.start, unadjusted=True)
        end = ctx.resolve(spec.end, anchor=start, unadjusted=True)
        if end <= start:
            raise ValueError(f"end {end} must be after start {start}")
        cal = cv.calendar(spec.calendar)
        p = cv.period(spec.frequency)
        s = None
        if p == ql.Period("1D"):
            out = [cv.from_ql(d) for d in cal.businessDayList(cv.to_ql(start), cv.to_ql(end))]
        else:
            s = ql.Schedule(cv.to_ql(start), cv.to_ql(end), p, cal, cv.bdc(spec.convention),
                            cv.bdc(spec.convention), cv.DATE_GENERATION[spec.rule], spec.end_of_month)
            out = [cv.from_ql(d) for d in s]
        out = sorted(set(out))
        if not spec.include_start:
            adj_start = cv.from_ql(cal.adjust(cv.to_ql(start), cv.bdc(spec.convention)))
            out = [d for d in out if d not in (start, adj_start)]
        res = ResolvedDates(out)
        res.ql_schedule = s
        return res


@register("schedule", "offset")
class OffsetSchedule(ScheduleFactory):
    description = ("Shift every date of another schedule, e.g. fixing dates 2 business days before "
                   "payment dates: {type: offset, base: payment, offset: -2D, calendar: ...}.")

    class Spec(SpecModel):
        base: str = Field(description="name of another schedule")
        offset: str = Field(description="signed tenor, e.g. -2D, 1W")
        business_days: bool = Field(True, description="count D offsets in business days of `calendar`")
        calendar: str = "WeekendsOnly"
        convention: str = "Following"

    @staticmethod
    def dependencies(spec):
        return [spec.base]

    @staticmethod
    def resolve(spec, ctx, resolved):
        cal, p = cv.calendar(spec.calendar), cv.period(spec.offset)
        out = []
        for d in resolved[spec.base]:
            q = cv.to_ql(d)
            if spec.business_days and p.units() == ql.Days:
                q = cal.advance(q, p.length(), ql.Days)
            else:
                q = cal.adjust(q + p, cv.bdc(spec.convention))
            out.append(cv.from_ql(q))
        return sorted(set(out))
