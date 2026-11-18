"""Freshness: did each scheduled dataset deliver its data day on time? (spec §8.1)

Status ladder, in minutes past ``expected_by_et``: data present -> pass; missing and within
``grace_minutes`` -> ``skip`` with reason "waiting" (completeness says the same, so the two
checks agree); missing beyond grace -> warn; beyond 2x grace -> fail. Before the due time, or on
a closed publication day, the result is ``skip``. On a SIFMA recommended early close a missing
Treasury/FRED value is capped at ``warn`` (P-011).

:class:`IntradayFreshnessCheck` (below) covers the ``Config.intraday`` entries (T07B).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import duckdb

from deskbrief.calendars import is_early_close, session_bounds_utc
from deskbrief.checks.base import (
    ET_DATE_SQL,
    closed_reason,
    expected_for,
    make_result,
    minutes_late,
    query,
    register,
    source_enabled,
    within_grace,
)
from deskbrief.config import DatasetSchedule, IntradaySchedule
from deskbrief.models import CheckType, DQResult, DQStatus, JobKind, RunContext
from deskbrief.timeutil import ensure_utc, et_date


def late_status(late_minutes: float, grace_minutes: float) -> DQStatus:
    if late_minutes > 2 * grace_minutes:
        return DQStatus.FAIL
    if late_minutes > grace_minutes:
        return DQStatus.WARN
    return DQStatus.PASS


def latest_data_day(
    duck: duckdb.DuckDBPyConnection, ctx: RunContext, sched: DatasetSchedule
) -> date | None:
    df = query(
        duck,
        f"SELECT max({ET_DATE_SQL}) AS d FROM obs_pit(?) "  # noqa: S608
        "WHERE source = ? AND dataset = ? AND interval = '1d'",
        [ctx.now_utc, sched.source, sched.dataset],
    )
    value = df["d"].iloc[0]
    return None if value is None or value != value else date.fromisoformat(str(value)[:10])


class FreshnessCheck:
    name = "freshness"
    check_type = CheckType.FRESHNESS

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        out: list[DQResult] = []
        for sched in ctx.config.datasets:
            if not source_enabled(ctx, sched.source):
                continue  # switched off in config.yaml: nothing is expected from it
            exp = expected_for(ctx, sched)
            base: dict[str, Any] = {"dataset": sched.dataset, "data_day": exp.data_day.isoformat()}
            if exp.skip_reason:
                out.append(
                    make_result(
                        ctx,
                        self,
                        DQStatus.SKIP,
                        source=sched.source,
                        reason=exp.skip_reason,
                        **base,
                    )
                )
                continue
            latest = latest_data_day(duck, ctx, sched)
            if latest is not None and latest >= exp.data_day:
                late, status, state = 0.0, DQStatus.PASS, "fresh"
            else:
                late = minutes_late(ctx, exp)
                status = late_status(late, exp.grace_minutes)
                if within_grace(ctx, exp):  # due but within grace: same answer as completeness
                    out.append(
                        make_result(
                            ctx, self, DQStatus.SKIP, source=sched.source, observed=late,
                            threshold=float(exp.grace_minutes), reason="waiting", **base,
                        )
                    )  # fmt: skip
                    continue
                state = "late"
                if status is DQStatus.FAIL and exp.early_close:
                    status, state = DQStatus.WARN, "late_early_close"
            out.append(
                make_result(
                    ctx,
                    self,
                    status,
                    source=sched.source,
                    observed=late,
                    threshold=float(exp.grace_minutes),
                    state=state,
                    latest_data_day=latest.isoformat() if latest else None,
                    **base,
                )
            )
        return out


register(FreshnessCheck())


def newest_tick(
    duck: duckdb.DuckDBPyConnection, ctx: RunContext, sched: IntradaySchedule
) -> datetime | None:
    """``as_of`` of the newest tick of the feed known at ``now`` (``obs_pit``, P-009)."""
    df = query(
        duck,
        "SELECT max(as_of) AS t FROM obs_pit(?) "
        "WHERE source = ? AND dataset = ? AND interval = 'tick' AND as_of <= ?",
        [ctx.now_utc, sched.source, sched.dataset, ctx.now_utc],
    )
    value = df["t"].iloc[0]
    return None if value is None or value != value else ensure_utc(value.to_pydatetime())


class IntradayFreshnessCheck:
    """Age of the newest tick of each ``Config.intraday`` entry against its ``grace_minutes``.

    Runs in the ``intraday`` job only: that job is the only one that polls ticks, and it already
    refuses to start outside the XNYS session. Other jobs return no rows, so the morning, eod and
    rebuild runs do not log skips for a check that cannot apply to them. ``now`` must be inside
    the entry's session on the ET day of ``now`` (an early close ends it early, P-011) and at
    least one ``grace_minutes`` after the open, otherwise the row is ``skip`` with the reason
    (``pre-open``, ``warm-up``, ``after close``, ``holiday``, ``weekend``). The warm-up stops
    yesterday's last tick from reading as stale right after the open.

    Ladder, same as :func:`late_status` and ``within_grace`` (inclusive at grace): age <= grace
    passes, up to 2x grace warns, beyond fails. An entry with no tick at all while it should be
    flowing fails: after a full grace period of session there is nothing to wait for, and the
    age is unbounded, so it lands at the top of the ladder.

    Only ``XNYS`` sessions are modelled (``calendars.session_bounds_utc``); an entry with another
    calendar is ``skip`` with that reason rather than judged against the wrong hours.
    """

    name = "freshness_intraday"
    check_type = CheckType.FRESHNESS

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        if ctx.job is not JobKind.INTRADAY:
            return []
        out: list[DQResult] = []
        day = et_date(ctx.now_utc)
        for sched in ctx.config.intraday:
            info: dict[str, Any] = {"dataset": sched.dataset, "session_day": day.isoformat()}
            reason = self._skip_reason(ctx, sched, day)
            if reason:
                out.append(
                    make_result(
                        ctx, self, DQStatus.SKIP, source=sched.source,
                        threshold=float(sched.grace_minutes), reason=reason, **info,
                    )
                )  # fmt: skip
                continue
            info["early_close"] = is_early_close(day, cal=sched.calendar)
            latest = newest_tick(duck, ctx, sched)
            if latest is None:
                status, state, age = DQStatus.FAIL, "no_ticks", None
            else:
                age = (ctx.now_utc - latest).total_seconds() / 60.0
                status = late_status(age, sched.grace_minutes)
                state = "fresh" if status is DQStatus.PASS else "stale"
                info["latest_tick_utc"] = latest.isoformat()
            out.append(
                make_result(
                    ctx, self, status, source=sched.source, observed=age,
                    threshold=float(sched.grace_minutes), state=state, **info,
                )
            )  # fmt: skip
        return out

    @staticmethod
    def _skip_reason(ctx: RunContext, sched: IntradaySchedule, day: date) -> str | None:
        if sched.calendar != "XNYS":
            return f"session hours of calendar {sched.calendar} are not modelled"
        closed = closed_reason(sched.calendar, day)
        bounds = session_bounds_utc(day)
        if closed or bounds is None:
            return closed or "holiday"
        opens, closes = bounds
        if ctx.now_utc < opens:
            return "pre-open"
        if ctx.now_utc < opens + timedelta(minutes=sched.grace_minutes):
            return f"warm-up: less than {sched.grace_minutes} min after the open"
        if ctx.now_utc > closes:
            return "after close" + (" (early close)" if is_early_close(day) else "")
        return None


register(IntradayFreshnessCheck())
