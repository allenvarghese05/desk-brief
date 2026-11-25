"""Business-day calendars for the markets the pipeline cares about.

* ``XNYS``     - NYSE equities, via ``pandas_market_calendars`` (early closes included).
* ``SIFMA_US`` - US bond market, via ``pandas_market_calendars`` ``SIFMA_US`` (verified present in
  5.6.1). A day is a business day if SIFMA recommends any trading, so recommended early closes
  (e.g. 2026-04-03) are open days; only full-closure holidays are excluded.
* ``TARGET``   - ECB payment system. ``pandas_market_calendars`` has no such calendar, so the fixed
  rules are implemented here: weekends, Jan 1, Good Friday, Easter Monday, May 1, Dec 25, Dec 26.
  TARGET has no early closes.
* ``XTKS``, ``XLON``, ``XETR`` - the Tokyo, London and Frankfurt (Xetra) exchanges, via
  ``pandas_market_calendars`` (T61): a foreign index's change is measured against its own
  exchange's previous session, not the NYSE's (P-024).

All datetimes returned are timezone-aware UTC. Callers pass plain ``date`` objects; anything else
(``str``, ``None``, ``datetime``, ``pandas.Timestamp``) is rejected with ``TypeError`` because
silently truncating or parsing it hides timezone bugs. The calendar name is validated first in
every function, before any shortcut. Lookups are cached per (calendar, year) and the cached
objects are read-only (``MappingProxyType``, tuples, frozensets).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from functools import cache
from types import MappingProxyType
from typing import get_args

import pandas_market_calendars as mcal

from deskbrief.config import CalendarName
from deskbrief.timeutil import ensure_utc

_MAX_SCAN_DAYS = 400  # no calendar has a closure streak anywhere near this long

Session = tuple[datetime, datetime]


def _check_date(d: date) -> date:
    # datetime and pandas.Timestamp are date subclasses, so test for them explicitly
    if not isinstance(d, date) or isinstance(d, datetime):
        raise TypeError(f"expected datetime.date, got {type(d).__name__}: {d!r}")
    return d


def _check_cal(cal: str) -> None:
    if cal not in get_args(CalendarName):
        raise ValueError(f"unknown calendar {cal!r}")


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Meeus/Jones/Butcher algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


@cache
def _target_holidays(year: int) -> frozenset[date]:
    easter = _easter(year)
    return frozenset(
        {
            date(year, 1, 1),
            easter - timedelta(days=2),  # Good Friday
            easter + timedelta(days=1),  # Easter Monday
            date(year, 5, 1),
            date(year, 12, 25),
            date(year, 12, 26),
        }
    )


@cache
def _schedule(cal: str, year: int) -> Mapping[date, Session]:
    """Sessions for one year of a library-backed calendar: date -> (open, close) UTC.

    Read-only: the cached mapping is shared by every caller.
    """
    sched = mcal.get_calendar(cal).schedule(
        start_date=date(year, 1, 1), end_date=date(year, 12, 31)
    )
    return MappingProxyType(
        {
            ts.date(): (
                ensure_utc(row.market_open.to_pydatetime()),
                ensure_utc(row.market_close.to_pydatetime()),
            )
            for ts, row in sched.iterrows()
        }
    )


@cache
def _early_closes(cal: str, year: int) -> frozenset[date]:
    lib = mcal.get_calendar(cal)
    sched = lib.schedule(start_date=date(year, 1, 1), end_date=date(year, 12, 31))
    return frozenset(ts.date() for ts in lib.early_closes(sched).index)


def is_business_day(cal: CalendarName, d: date) -> bool:
    """True if ``cal`` is open (or has any recommended trading) on ``d``."""
    _check_cal(cal)
    _check_date(d)
    if d.weekday() >= 5:
        return False
    if cal == "TARGET":
        return d not in _target_holidays(d.year)
    return d in _schedule(cal, d.year)


def prev_business_day(cal: CalendarName, d: date) -> date:
    """Latest business day strictly before ``d``."""
    _check_cal(cal)
    _check_date(d)
    for _ in range(_MAX_SCAN_DAYS):
        d -= timedelta(days=1)
        if is_business_day(cal, d):
            return d
    raise RuntimeError(f"no business day found before {d} for {cal}")  # pragma: no cover


def next_business_day(cal: CalendarName, d: date) -> date:
    """Earliest business day strictly after ``d``."""
    _check_cal(cal)
    _check_date(d)
    for _ in range(_MAX_SCAN_DAYS):
        d += timedelta(days=1)
        if is_business_day(cal, d):
            return d
    raise RuntimeError(f"no business day found after {d} for {cal}")  # pragma: no cover


def business_days(cal: CalendarName, start: date, end: date) -> list[date]:
    """Business days in ``[start, end]`` inclusive, ascending. Empty if ``end < start``."""
    _check_cal(cal)  # before the empty-range shortcut below
    _check_date(start)
    _check_date(end)
    out: list[date] = []
    d = start
    while d <= end:
        if is_business_day(cal, d):
            out.append(d)
        d += timedelta(days=1)
    return out


def session_bounds_utc(d: date) -> Session | None:
    """XNYS (open, close) as UTC-aware datetimes, early closes honoured; ``None`` if closed."""
    _check_date(d)
    return _schedule("XNYS", d.year).get(d)


def add_business_days(cal: CalendarName, d: date, n: int) -> date:
    """``d`` moved ``n`` business days: forward for ``n > 0``, back for ``n < 0``.

    ``n == 0`` returns ``d`` unchanged (it is not rolled to a business day), so
    ``add_business_days(cal, d, 1)`` equals ``next_business_day(cal, d)`` even when ``d`` is a
    weekend or holiday.
    """
    _check_cal(cal)
    _check_date(d)
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError(f"n must be an int, got {type(n).__name__}")
    for _ in range(abs(n)):
        d = next_business_day(cal, d) if n > 0 else prev_business_day(cal, d)
    return d


def is_early_close(d: date, *, cal: CalendarName = "XNYS") -> bool:
    """True if ``cal`` has a shortened session on ``d`` (XNYS by default). TARGET never does."""
    _check_cal(cal)
    _check_date(d)
    if cal == "TARGET":
        return False
    return d in _early_closes(cal, d.year)


__all__ = [
    "add_business_days",
    "business_days",
    "is_business_day",
    "is_early_close",
    "next_business_day",
    "prev_business_day",
    "session_bounds_utc",
]
