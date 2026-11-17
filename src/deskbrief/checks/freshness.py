"""Freshness: did each scheduled dataset deliver its data day on time? (spec §8.1)

Status ladder, in minutes past ``expected_by_et``: data present -> pass; missing and within
``grace_minutes`` -> ``skip`` with reason "waiting" (completeness says the same, so the two
checks agree); missing beyond grace -> warn; beyond 2x grace -> fail. Before the due time, or on
a closed publication day, the result is ``skip``. On a SIFMA recommended early close a missing
Treasury/FRED value is capped at ``warn`` (P-011).

Intraday tick-age freshness is not implemented: ``config.datasets`` has no per-dataset intraday
schedule (needs a contract change, see the T07 PR).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb

from deskbrief.checks.base import (
    ET_DATE_SQL,
    expected_for,
    make_result,
    minutes_late,
    query,
    register,
    within_grace,
)
from deskbrief.config import DatasetSchedule
from deskbrief.models import CheckType, DQResult, DQStatus, RunContext


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
