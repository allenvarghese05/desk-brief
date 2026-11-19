"""Freshness: did each scheduled dataset deliver its data day on time? (spec §8.1)

Status ladder, in minutes past ``expected_by_et``: data present -> pass; missing and within
``grace_minutes`` -> ``skip`` with reason "waiting" (completeness says the same, so the two
checks agree); missing beyond grace -> warn; beyond 2x grace -> fail. Before the due time, or on
a closed publication day, the result is ``skip``. On a SIFMA recommended early close a missing
Treasury/FRED value is capped at ``warn`` (P-011).

Weekly datasets (``DatasetSchedule.release_weekday`` set, ``1w`` rows, P-021) are judged on
their latest release whose due time has passed (:func:`weekly_expected`): the release must hold
a reference date newer than last week's. A holiday in the release week delays the release by one
business day per holiday; until that delayed due time a missing release is ``skip`` ("release
delayed by holiday"), never ``fail``. The same ladder applies after it.

:class:`IntradayFreshnessCheck` (below) covers the ``Config.intraday`` entries (T07B).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import duckdb

from deskbrief.calendars import add_business_days, is_early_close, session_bounds_utc
from deskbrief.checks.base import (
    ET_DATE_SQL,
    Expected,
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
from deskbrief.timeutil import ensure_utc, et_date, et_to_utc

DELAYED = "release delayed by holiday"


def late_status(late_minutes: float, grace_minutes: float) -> DQStatus:
    if late_minutes > 2 * grace_minutes:
        return DQStatus.FAIL
    if late_minutes > grace_minutes:
        return DQStatus.WARN
    return DQStatus.PASS


def interval_of(sched: DatasetSchedule) -> str:
    """``1w`` for a weekly dataset (it has a ``release_weekday``), else ``1d``."""
    return "1d" if sched.release_weekday is None else "1w"


def latest_data_day(
    duck: duckdb.DuckDBPyConnection, ctx: RunContext, sched: DatasetSchedule
) -> date | None:
    df = query(
        duck,
        f"SELECT max({ET_DATE_SQL}) AS d FROM obs_pit(?) "  # noqa: S608
        "WHERE source = ? AND dataset = ? AND interval = ?",
        [ctx.now_utc, sched.source, sched.dataset, interval_of(sched)],
    )
    value = df["d"].iloc[0]
    return None if value is None or value != value else date.fromisoformat(str(value)[:10])


def _weekdays_back(d: date, n: int) -> date:
    """``d`` moved back ``n`` weekdays, ignoring holidays: a weekly release's nominal lag."""
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


@dataclass(frozen=True)
class WeeklyExpected:
    """The latest due release of a ``1w`` dataset (P-021).

    ``exp.publication_day`` is the release day after any holiday delay and ``exp.data_day`` the
    nominal reference date (``lag_bdays`` weekdays before the scheduled release day). A stored
    ``1w`` row belongs to this release when its ET date is in ``[data_from, publication_day]``:
    reference dates are a week apart, so last week's is older than ``data_from`` even when a
    holiday moves this week's by a few days. ``skip_reason`` is :data:`DELAYED` while a release
    week with a holiday has not reached its delayed due time.
    """

    exp: Expected
    data_from: date
    scheduled_day: date
    holidays: int


def weekly_expected(ctx: RunContext, sched: DatasetSchedule) -> WeeklyExpected:
    """The release to judge: the latest ``release_weekday`` on or before ``target_date`` whose
    ``expected_by_et`` has passed (else the one a week earlier). Each holiday on the dataset's
    calendar in the seven days ending on the scheduled day delays it one business day."""
    if sched.release_weekday is None:
        raise ValueError(f"{sched.source}/{sched.dataset} is not weekly (no release_weekday)")
    cal = sched.calendar
    t = ctx.target_date
    scheduled = t - timedelta(days=(t.weekday() - sched.release_weekday) % 7)
    if ctx.now_utc < et_to_utc(scheduled, sched.expected_by_et):
        scheduled -= timedelta(days=7)
    week = [scheduled - timedelta(days=i) for i in range(7)]
    holidays = sum(closed_reason(cal, d) == "holiday" for d in week)
    pub = add_business_days(cal, scheduled, holidays)
    due = et_to_utc(pub, sched.expected_by_et)
    data_day = _weekdays_back(scheduled, sched.lag_bdays)
    reason = DELAYED if holidays and ctx.now_utc < due else None
    exp = Expected(pub, data_day, due, sched.grace_minutes, False, reason)
    return WeeklyExpected(exp, data_day - timedelta(days=6), scheduled, holidays)


def weekly_info(w: WeeklyExpected) -> dict[str, Any]:
    """``details`` shared by the weekly freshness and completeness rows."""
    return {
        "interval": "1w",
        "release_day": w.exp.publication_day.isoformat(),
        "scheduled_release_day": w.scheduled_day.isoformat(),
        "data_day": w.exp.data_day.isoformat(),
        "data_from": w.data_from.isoformat(),
    }


class FreshnessCheck:
    name = "freshness"
    check_type = CheckType.FRESHNESS

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        out: list[DQResult] = []
        for sched in ctx.config.datasets:
            if not source_enabled(ctx, sched.source):
                continue  # switched off in config.yaml: nothing is expected from it
            if sched.release_weekday is not None:
                out.append(self._weekly(ctx, duck, sched))
                continue
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

    def _weekly(
        self, ctx: RunContext, duck: duckdb.DuckDBPyConnection, sched: DatasetSchedule
    ) -> DQResult:
        w = weekly_expected(ctx, sched)
        exp = w.exp
        info: dict[str, Any] = {"dataset": sched.dataset, **weekly_info(w)}
        latest = latest_data_day(duck, ctx, sched)
        info["latest_data_day"] = latest.isoformat() if latest else None
        grace = float(exp.grace_minutes)
        if latest is not None and latest >= w.data_from:
            return make_result(
                ctx, self, DQStatus.PASS, source=sched.source, observed=0.0, threshold=grace,
                state="fresh", **info,
            )  # fmt: skip
        late = minutes_late(ctx, exp)
        if exp.skip_reason or within_grace(ctx, exp):
            return make_result(
                ctx, self, DQStatus.SKIP, source=sched.source, observed=late, threshold=grace,
                reason=exp.skip_reason or "waiting", **info,
            )  # fmt: skip
        return make_result(
            ctx, self, late_status(late, exp.grace_minutes), source=sched.source,
            observed=late, threshold=grace, state="late", **info,
        )  # fmt: skip


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
