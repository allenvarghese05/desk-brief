"""Check protocol, registry and the helpers shared by the single-source checks (spec §8.1).

Every check implements ``evaluate(ctx, duck) -> list[DQResult]`` and registers itself with
:func:`register`, so :func:`deskbrief.checks.run_checks` can run them all.

Which view each check reads (P-009: reproducible values come from ``obs_pit``, never from
``daily_close``, and the current unfinished ET date is excluded where it matters):

=============  =======================================================================
freshness      ``obs_pit(now)``; gated by the dataset's due time instead of the date rule
completeness   ``obs_pit(now)``; daily feeds gated by due time + grace, minute bars, Yahoo and
               Twelve Data feeds use the last finished session (``feed_completeness``)
calendar       calendars only, no data read
validity       ``obs_pit(now)`` restricted to this run's rows
uniqueness     ``obs_all`` (the dedupe views would hide duplicates) for this run's fetches
anomaly        ``obs_pit(now)``, ET dates strictly before today and not after the target;
               skipped when the newest day is older than the expected data day
revision       ``obs_all`` up to ``now`` (needs every version of a key)
=============  =======================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

import duckdb
import pandas as pd

from deskbrief.calendars import (
    add_business_days,
    is_business_day,
    is_early_close,
    prev_business_day,
)
from deskbrief.config import CalendarName, DatasetSchedule
from deskbrief.models import CheckType, DQResult, DQStatus, JobKind, RunContext
from deskbrief.timeutil import et_date, et_to_utc

#: SQL for the ET calendar date of an observation's ``as_of`` (P-006).
ET_DATE_SQL = "CAST(timezone('America/New_York', as_of) AS DATE)"


class Check(Protocol):
    """One single-source data-quality check."""

    name: str
    check_type: CheckType

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]: ...


_REGISTRY: dict[str, Check] = {}


def register(check: Check) -> Check:
    """Add ``check`` to the registry (names are unique)."""
    if check.name in _REGISTRY:
        raise ValueError(f"check {check.name!r} already registered")
    _REGISTRY[check.name] = check
    return check


def registered_checks() -> tuple[Check, ...]:
    return tuple(_REGISTRY.values())


def make_result(
    ctx: RunContext,
    check: Check,
    status: DQStatus,
    *,
    source: str | None = None,
    instrument_id: str | None = None,
    observed: float | None = None,
    threshold: float | None = None,
    **details: Any,
) -> DQResult:
    return DQResult(
        run_id=ctx.run_id,
        check_name=check.name,
        check_type=check.check_type,
        target_date=ctx.target_date,
        evaluated_at=ctx.now_utc,
        status=status,
        source=source,
        instrument_id=instrument_id,
        observed=observed,
        threshold=threshold,
        details=details,
    )


def records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Rows of ``df`` as plain dicts (keeps mypy and itertuples quirks out of the checks)."""
    return [{str(k): v for k, v in r.items()} for r in df.to_dict("records")]


def query(duck: duckdb.DuckDBPyConnection, sql: str, params: list[Any]) -> pd.DataFrame:
    return duck.execute(sql, params).df()


@dataclass(frozen=True)
class Expected:
    """What a scheduled dataset should have delivered by now, or why it is not evaluated."""

    publication_day: date  # the ET day whose ``expected_by_et`` we judge
    data_day: date  # the observation date that publication should contain
    due_utc: datetime
    grace_minutes: int
    early_close: bool  # SIFMA recommended early close: Treasury/FRED may skip the value
    skip_reason: str | None = None


def closed_reason(cal: CalendarName, d: date) -> str | None:
    """``None`` on a business day, else ``"weekend"`` or ``"holiday"``."""
    if is_business_day(cal, d):
        return None
    return "weekend" if d.weekday() >= 5 else "holiday"


#: Calendars that must all be open for a FRED oil series to have a value. SIFMA is open on days
#: the oil market is shut (Good Friday), so the dataset calendar alone over-expects. XNYS stands
#: in for NYMEX (WTI); Brent also needs TARGET (Easter Monday, 1 May, 26 Dec). UK bank holidays
#: beyond those are not modelled (approximation; see docs/agent-log/T07.md).
OIL_CALENDARS: dict[str, tuple[CalendarName, ...]] = {
    "DCOILWTICO": ("XNYS",),
    "DCOILBRENTEU": ("XNYS", "TARGET"),
}


def market_closed(symbol: str, d: date) -> bool:
    """True when the oil market behind a FRED oil series is shut on ``d`` (else False)."""
    return any(closed_reason(cal, d) for cal in OIL_CALENDARS.get(symbol, ()))


def expected_for(ctx: RunContext, sched: DatasetSchedule) -> Expected:
    """Publication day, data day and due time of ``sched`` for this run.

    The morning job runs before today's publications, so it judges the previous business day's
    (the same rule the Treasury source plans with). Other jobs judge ``target_date``.
    ``data_day = publication_day - lag_bdays`` business days on the dataset's calendar. A
    closed publication day, or ``now`` before ``expected_by_et``, gives a skip reason.
    """
    cal = sched.calendar
    pub = prev_business_day(cal, ctx.target_date) if ctx.job is JobKind.MORNING else ctx.target_date
    data_day = add_business_days(cal, pub, -sched.lag_bdays)
    due = et_to_utc(pub, sched.expected_by_et)
    reason = closed_reason(cal, pub)
    if reason is None and ctx.now_utc < due:
        reason = "not yet due"
    early = cal == "SIFMA_US" and is_early_close(data_day, cal=cal)
    return Expected(pub, data_day, due, sched.grace_minutes, early, reason)


def minutes_late(ctx: RunContext, exp: Expected) -> float:
    return max(0.0, (ctx.now_utc - exp.due_utc).total_seconds() / 60.0)


def within_grace(ctx: RunContext, exp: Expected) -> bool:
    """True while a due dataset is still inside its grace period.

    One convention for freshness and completeness: the grace is inclusive, so a dataset exactly
    ``grace_minutes`` late is still "waiting"; the first instant after is late (warn/fail). This
    matches the freshness ladder (warn only when ``late > grace``).
    """
    return minutes_late(ctx, exp) <= exp.grace_minutes


def finished_session_date(ctx: RunContext, cal: CalendarName = "XNYS") -> date:
    """Latest ET date whose data is complete (P-009): the target, or the business day before
    it when the target is still today (or later) in ET."""
    if ctx.target_date < et_date(ctx.now_utc):
        return ctx.target_date
    return prev_business_day(cal, ctx.target_date)
