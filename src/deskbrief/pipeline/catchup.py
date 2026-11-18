"""Catch-up of missed target dates (spec §2.4 item 6).

A scheduled ``morning`` or ``eod`` run looks at the ``runs`` table for the last
:data:`CATCHUP_BUSINESS_DAYS` XNYS business days before its own target date. A date is
*covered* when a ``runs`` row for it has status ``ok`` or ``degraded`` and was written by the
same job or by a ``backfill`` (which is what a catch-up run is: ``job=backfill``,
``trigger=catchup``). Anything else (no row, only ``failed`` rows) is missed and gets
backfilled, oldest first, before the main run.

An entirely empty ``runs`` table means "first ever run": there is no history to be behind
on, so nothing is caught up (otherwise the first run would silently backfill a week).

Bounded per run (T25c): run 44219067732 was cancelled at the 30-minute job timeout while
backfilling five days (~5 minutes each) and never produced its morning report. A run therefore
catches up at most :data:`MAX_CATCHUP_DAYS_PER_RUN` dates, oldest first, and starts no further
date once :data:`CATCHUP_TIME_BUDGET` has passed since the run started; the rest stay "missed"
and the next morning or eod run picks them up (both are catch-up parents).
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from deskbrief.calendars import prev_business_day
from deskbrief.models import JobKind, RunStatus

CATCHUP_BUSINESS_DAYS = 5
CATCHUP_PARENT_JOBS = frozenset({JobKind.MORNING, JobKind.EOD})
_GOOD = {RunStatus.OK.value, RunStatus.DEGRADED.value}
#: dates backfilled by one run; the rest wait for the next run (oldest first)
MAX_CATCHUP_DAYS_PER_RUN = 2
#: no catch-up date is started once this much of the run has passed, so the main run (and the
#: morning report) still fits the workflow's 30-minute timeout
CATCHUP_TIME_BUDGET = timedelta(minutes=12)


def window(target_date: date, n: int = CATCHUP_BUSINESS_DAYS) -> list[date]:
    """The ``n`` XNYS business days strictly before ``target_date``, ascending."""
    days: list[date] = []
    d = target_date
    for _ in range(n):
        d = prev_business_day("XNYS", d)
        days.append(d)
    return sorted(days)


def missed_dates(runs: pd.DataFrame, job: JobKind, target_date: date) -> list[date]:
    """Dates in the catch-up window that no good run covers (empty for non-parent jobs)."""
    if job not in CATCHUP_PARENT_JOBS or runs.empty:
        return []
    good = runs[runs["status"].isin(_GOOD)]
    covering = good[good["job"].isin([job.value, JobKind.BACKFILL.value])]
    covered = set(covering["target_date"])
    return [d for d in window(target_date) if d not in covered]


def split_catchup(
    missed: list[date], limit: int = MAX_CATCHUP_DAYS_PER_RUN
) -> tuple[list[date], list[date]]:
    """``(this run, later runs)``: the oldest ``limit`` missed dates first."""
    ordered = sorted(missed)
    return ordered[: max(limit, 0)], ordered[max(limit, 0) :]
