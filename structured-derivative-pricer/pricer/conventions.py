"""String -> QuantLib object parsing for dates, calendars, day counts and rolling conventions.

Every name accepted by the input spec is listed here, so the catalog can show them
and validation can reject unknown names with a suggestion instead of a QuantLib crash.
"""
from __future__ import annotations

import datetime as dt
import difflib
import re

import QuantLib as ql

CALENDARS = {
    "NullCalendar": lambda: ql.NullCalendar(),
    "WeekendsOnly": lambda: ql.WeekendsOnly(),
    "TARGET": lambda: ql.TARGET(),
    "UnitedStates.NYSE": lambda: ql.UnitedStates(ql.UnitedStates.NYSE),
    "UnitedStates.Settlement": lambda: ql.UnitedStates(ql.UnitedStates.Settlement),
    "UnitedStates.GovernmentBond": lambda: ql.UnitedStates(ql.UnitedStates.GovernmentBond),
    "UnitedStates.SOFR": lambda: ql.UnitedStates(ql.UnitedStates.SOFR),
    "UnitedKingdom": lambda: ql.UnitedKingdom(),
    "UnitedKingdom.Exchange": lambda: ql.UnitedKingdom(ql.UnitedKingdom.Exchange),
    "Japan": lambda: ql.Japan(),
    "Taiwan": lambda: ql.Taiwan(),
    "HongKong": lambda: ql.HongKong(),
    "China.SSE": lambda: ql.China(ql.China.SSE),
    "China.IB": lambda: ql.China(ql.China.IB),
    "SouthKorea": lambda: ql.SouthKorea(),
    "Singapore": lambda: ql.Singapore(),
    "Australia": lambda: ql.Australia(),
    "Switzerland": lambda: ql.Switzerland(),
    "Germany.Eurex": lambda: ql.Germany(ql.Germany.Eurex),
}

DAY_COUNTS = {
    "Actual365Fixed": lambda: ql.Actual365Fixed(),
    "Actual360": lambda: ql.Actual360(),
    "Thirty360.USA": lambda: ql.Thirty360(ql.Thirty360.USA),
    "Thirty360.BondBasis": lambda: ql.Thirty360(ql.Thirty360.BondBasis),
    "Thirty360.European": lambda: ql.Thirty360(ql.Thirty360.European),
    "ActualActual.ISDA": lambda: ql.ActualActual(ql.ActualActual.ISDA),
    "ActualActual.Bond": lambda: ql.ActualActual(ql.ActualActual.Bond),
}

BUSINESS_DAY_CONVENTIONS = {
    "Following": ql.Following,
    "ModifiedFollowing": ql.ModifiedFollowing,
    "Preceding": ql.Preceding,
    "ModifiedPreceding": ql.ModifiedPreceding,
    "Unadjusted": ql.Unadjusted,
}

DATE_GENERATION = {
    "Forward": ql.DateGeneration.Forward,
    "Backward": ql.DateGeneration.Backward,
}

_TENOR_RE = re.compile(r"^-?\d+[DWMY]$", re.IGNORECASE)


def suggest(name: str, choices) -> str:
    close = difflib.get_close_matches(str(name), list(choices), n=1)
    return f" Did you mean '{close[0]}'?" if close else ""


def _lookup(table: dict, name: str, what: str):
    if name not in table:
        raise ValueError(f"unknown {what} '{name}'.{suggest(name, table)} Allowed: {sorted(table)}")
    v = table[name]
    return v() if callable(v) else v


def calendar(name: str) -> ql.Calendar:
    """Single calendar name, or several joined with '+' (e.g. 'UnitedStates.NYSE+TARGET')."""
    parts = [p.strip() for p in name.split("+")]
    cals = [_lookup(CALENDARS, p, "calendar") for p in parts]
    if len(cals) == 1:
        return cals[0]
    joint = ql.JointCalendar(cals[0], cals[1])
    for c in cals[2:]:
        joint = ql.JointCalendar(joint, c)
    return joint


def day_count(name: str) -> ql.DayCounter:
    return _lookup(DAY_COUNTS, name, "day count")


def bdc(name: str) -> int:
    return _lookup(BUSINESS_DAY_CONVENTIONS, name, "business day convention")


def is_tenor(s) -> bool:
    return isinstance(s, str) and bool(_TENOR_RE.match(s.strip()))


def period(s: str) -> ql.Period:
    if not is_tenor(s):
        raise ValueError(f"'{s}' is not a tenor like '1D', '3M', '1Y'")
    return ql.Period(s.strip().upper())


def to_ql(d: dt.date) -> ql.Date:
    return ql.Date(d.day, d.month, d.year)


def from_ql(d: ql.Date) -> dt.date:
    return dt.date(d.year(), d.month(), d.dayOfMonth())


def parse_iso(x) -> dt.date:
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    if isinstance(x, str):
        try:
            return dt.date.fromisoformat(x.strip())
        except ValueError:
            pass
    raise ValueError(f"'{x}' is not an ISO date (YYYY-MM-DD)")
