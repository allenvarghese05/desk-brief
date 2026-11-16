"""Catch-up of missed target dates (spec §2.4 item 6).

A scheduled ``morning`` or ``eod`` run looks at the ``runs`` table for the last
:data:`CATCHUP_BUSINESS_DAYS` XNYS business days before its own target date. A date is
*covered* when a ``runs`` row for it has status ``ok`` or ``degraded`` and was written by the
same job or by a ``backfill`` (which is what a catch-up run is: ``job=backfill``,
``trigger=catchup``). Anything else (no row, only ``failed`` rows) is missed and gets
backfilled, oldest first, before the main run.

An entirely empty ``runs`` table means "first ever run": there is no history to be behind
on, so nothing is caught up (otherwise the first run would silently backfill a week).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from deskbrief.calendars import prev_business_day
from deskbrief.models import JobKind, RunStatus

CATCHUP_BUSINESS_DAYS = 5
CATCHUP_PARENT_JOBS = frozenset({JobKind.MORNING, JobKind.EOD})
_GOOD = {RunStatus.OK.value, RunStatus.DEGRADED.value}


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
