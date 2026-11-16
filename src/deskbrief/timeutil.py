"""Time helpers.

Rule: everything stored or passed between modules is a timezone-aware UTC ``datetime``.
Eastern Time (reports, schedules) and Central European Time (ECB fix) appear only at the
edges, through the helpers below.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CET = ZoneInfo("Europe/Berlin")  # ECB publishes in Frankfurt local time
_HHMM = re.compile(r"[0-9]{2}:[0-9]{2}")  # ASCII digits only

Clock = Callable[[], datetime]
"""A zero-arg callable returning aware UTC now. Inject a fake in tests."""


def utc_now() -> datetime:
    """The real clock."""
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` converted to UTC. Naive datetimes are rejected, never guessed."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"naive datetime not allowed: {dt!r}")
    return dt.astimezone(UTC)


def to_et(dt: datetime) -> datetime:
    """Convert an aware datetime to America/New_York."""
    return ensure_utc(dt).astimezone(ET)


def et_date(dt: datetime) -> date:
    """The Eastern Time calendar date of an aware datetime (report dates are ET dates)."""
    return to_et(dt).date()


def parse_hhmm(value: str) -> time:
    """Parse ``"HH:MM"`` (24h) into a ``time``. Raises ``ValueError`` on bad input."""
    if not _HHMM.fullmatch(value):
        raise ValueError(f"expected HH:MM, got {value!r}")
    hh, mm = int(value[:2]), int(value[3:])
    return time(hh, mm)  # time() validates ranges


def local_to_utc(d: date, hhmm: str, tz: ZoneInfo) -> datetime:
    """UTC instant of wall-clock ``hhmm`` on date ``d`` in zone ``tz`` (DST-aware)."""
    return datetime.combine(d, parse_hhmm(hhmm), tzinfo=tz).astimezone(UTC)


def et_to_utc(d: date, hhmm: str) -> datetime:
    """UTC instant of an Eastern Time wall-clock time on an ET date."""
    return local_to_utc(d, hhmm, ET)
