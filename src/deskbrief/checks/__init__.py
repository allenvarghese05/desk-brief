"""Single-source data-quality checks (spec §8.1) and the runner hook.

Runner wiring (follow-up after T19 merges; P-008 one append per table/date per run)::

    from deskbrief.checks import run_checks

    duck = connect(ctx.store, ctx.config)          # connect AFTER the observations flush
    dq, revisions = run_checks(ctx, duck)
    datasets = Datasets(ctx.store)
    datasets.append("dq_results", dq, ctx, ctx.target_date)
    if not revisions.empty:                        # revisions partition by ET date of detected_at
        datasets.append("revisions", revisions, ctx, et_date(ctx.now_utc))

``consistency`` rows come from the cross-checks (T21) and are appended to the same frame by
the runner before the single ``dq_results`` flush.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from deskbrief.checks import (  # noqa: F401  (importing registers each check)
    anomaly,
    calendar_check,
    completeness,
    freshness,
    revision,
    uniqueness,
    validity,
)
from deskbrief.checks.base import Check, make_result, register, registered_checks
from deskbrief.checks.revision import RevisionCheck
from deskbrief.models import DQResult, DQStatus, RunContext
from deskbrief.schemas import empty_frame, to_frame


def _row(r: DQResult) -> dict[str, object]:
    return {
        "result_id": r.result_id,
        "run_id": r.run_id,
        "check_name": r.check_name,
        "check_type": r.check_type.value,
        "source": r.source,
        "instrument_id": r.instrument_id,
        "target_date": r.target_date,
        "evaluated_at": r.evaluated_at,
        "status": r.status.value,
        "observed": r.observed,
        "threshold": r.threshold,
        "details_json": r.details_json(),
    }


def run_checks(
    ctx: RunContext,
    duck: duckdb.DuckDBPyConnection,
    checks: tuple[Check, ...] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run every registered check; return ``(dq_results, revisions)`` frames.

    A check that raises does not stop the others: it yields one ``fail`` result carrying the
    error (spec §2.4). Nothing is written here; the runner flushes each frame once.
    """
    results: list[DQResult] = []
    revisions = empty_frame("revisions")
    for check in checks if checks is not None else registered_checks():
        try:
            if isinstance(check, RevisionCheck):
                found, revisions = check.scan(ctx, duck)
            else:
                found = check.evaluate(ctx, duck)
        except Exception as exc:  # isolate one broken check and record it
            found = [
                make_result(
                    ctx,
                    check,
                    DQStatus.FAIL,
                    reason="check raised",
                    error=f"{type(exc).__name__}: {exc}",
                )
            ]
        results.extend(found)
    return to_frame("dq_results", [_row(r) for r in results]), revisions


__all__ = ["Check", "make_result", "register", "registered_checks", "run_checks"]
