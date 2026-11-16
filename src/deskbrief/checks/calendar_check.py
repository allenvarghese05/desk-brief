"""Calendar: is the target date an expected day on each calendar in use? (spec §8.1)

Weekends and holidays produce ``skip`` (never ``fail``); other checks reuse the same
calendars to skip. Early closes pass but are flagged in ``details``.
"""

from __future__ import annotations

import duckdb

from deskbrief.calendars import is_early_close
from deskbrief.checks.base import closed_reason, make_result, register
from deskbrief.config import CalendarName
from deskbrief.models import CheckType, DQResult, DQStatus, RunContext


class CalendarCheck:
    name = "calendar"
    check_type = CheckType.CALENDAR

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        del duck  # calendars only
        cals: set[CalendarName] = {s.calendar for s in ctx.config.datasets} | {"XNYS"}
        out: list[DQResult] = []
        for cal in sorted(cals):
            reason = closed_reason(cal, ctx.target_date)
            if reason:
                out.append(make_result(ctx, self, DQStatus.SKIP, calendar=cal, reason=reason))
            else:
                early = is_early_close(ctx.target_date, cal=cal)
                out.append(make_result(ctx, self, DQStatus.PASS, calendar=cal, early_close=early))
        return out


register(CalendarCheck())
