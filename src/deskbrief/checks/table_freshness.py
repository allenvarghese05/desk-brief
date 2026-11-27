"""Freshness of the ``events`` and ``filings`` datasets (card T65, P-024).

These tables are not observations, so the ``freshness`` check (``obs_pit``, ``config.datasets``)
cannot see them, and ``config.datasets`` only accepts datasets an instrument feed uses. Their
sources re-fetch the whole scope in every ``morning`` run, so freshness here means: the newest
*complete* fetch of the dataset is recent enough.

* Datasets judged: :func:`deskbrief.sources.tables.planned_datasets` (enabled source, and the
  config section asks for something). A switched-off source is expected to deliver nothing.
* A run's fetch of a dataset is complete when every fetch of that dataset in that run is
  ``ok`` or ``empty`` with nothing quarantined: the same condition under which the writer
  stores the scope (``pipeline/runner.py``). Read from ``source_log`` up to ``now``.
* Due: the morning run of the latest XNYS session whose :data:`MORNING_DUE_ET` has passed (the
  morning window ends 07:00 ET). Fresh when the newest complete fetch is on or after that
  session's ET date. Otherwise the ladder of ``freshness.late_status`` from the due time with
  :data:`GRACE_MINUTES`: within grace ``skip`` ("waiting"), then ``warn``, beyond twice the
  grace ``fail``. Never fetched at all counts as late.
* Runs in the ``morning`` and ``eod`` jobs only (catch-up, rebuild, history and intraday runs
  would only repeat the same answer).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import duckdb

from deskbrief.calendars import is_business_day, prev_business_day
from deskbrief.checks.base import make_result, query, register
from deskbrief.checks.freshness import late_status
from deskbrief.models import CheckType, DQResult, DQStatus, JobKind, RunContext
from deskbrief.sources.tables import TableDataset, planned_datasets
from deskbrief.timeutil import ensure_utc, et_date, et_to_utc

#: End of the morning window (``pipeline/jobs.MORNING_WINDOW_ET``): the morning fetch is due.
MORNING_DUE_ET = "07:00"
GRACE_MINUTES = 120
_JOBS = frozenset({JobKind.MORNING, JobKind.EOD})


def due_session(now: datetime) -> date:
    """The latest XNYS session whose morning fetch is due at ``now``."""
    day = et_date(now)
    if not is_business_day("XNYS", day) or now < et_to_utc(day, MORNING_DUE_ET):
        day = prev_business_day("XNYS", day)
    return day


def latest_complete_fetch(
    duck: duckdb.DuckDBPyConnection, ds: TableDataset, now: datetime
) -> datetime | None:
    """``responded_at`` of the newest run whose every fetch of ``ds`` was complete."""
    df = query(
        duck,
        "SELECT max(responded_at) AS at FROM ("
        "  SELECT run_id, max(responded_at) AS responded_at,"
        "         bool_and(status IN ('ok', 'empty') AND rows_quarantined = 0) AS complete"
        "  FROM source_log WHERE source = ? AND dataset = ? AND responded_at <= ?"
        "  GROUP BY run_id) WHERE complete",
        [ds.source, ds.dataset, now],
    )
    value = df["at"].iloc[0]
    if value is None or value != value:
        return None
    return ensure_utc(value.to_pydatetime())


class TableFreshnessCheck:
    name = "freshness_tables"
    check_type = CheckType.FRESHNESS

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        if ctx.job not in _JOBS:
            return []
        out: list[DQResult] = []
        session = due_session(ctx.now_utc)
        due = et_to_utc(session, MORNING_DUE_ET)
        for ds in planned_datasets(ctx.config):
            latest = latest_complete_fetch(duck, ds, ctx.now_utc)
            info: dict[str, Any] = {
                "dataset": ds.dataset,
                "table": ds.table,
                "due_session": session.isoformat(),
                "latest_complete_fetch": latest.isoformat() if latest else None,
            }
            if latest is not None and et_date(latest) >= session:
                out.append(
                    make_result(
                        ctx, self, DQStatus.PASS, source=ds.source, observed=0.0,
                        threshold=float(GRACE_MINUTES), state="fresh", **info,
                    )
                )  # fmt: skip
                continue
            late = max(0.0, (ctx.now_utc - due).total_seconds() / 60.0)
            if late <= GRACE_MINUTES:
                status, extra = DQStatus.SKIP, {"reason": "waiting"}
            else:
                status, extra = late_status(late, GRACE_MINUTES), {"state": "late"}
            out.append(
                make_result(
                    ctx, self, status, source=ds.source, observed=late,
                    threshold=float(GRACE_MINUTES), **extra, **info,
                )
            )  # fmt: skip
        return out


register(TableFreshnessCheck())
