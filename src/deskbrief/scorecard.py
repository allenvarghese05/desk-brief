"""``scorecard_daily``: measured per-source numbers for one ET date (spec §7.2, §10.3 page 5).

Built from ``source_log`` and the cross-check results, on a DuckDB connection from
``store.duck.connect``. Re-runs of a date keep every run's rows (P-008), so the scorecard reads
**only the latest run for each ``target_date``**: counting all of them would score a re-run as
extra, mostly redundant, fetches. The date scored is that ``target_date`` (P-006).

Per source, over that run's fetch attempts:

* ``skipped_calendar`` rows are not attempts and are left out; a source with no attempt gets no row.
* ``fetches`` attempts; ``ok_rate`` share with status ``ok`` or ``empty`` (the ``query`` rule);
  ``error_rate`` share with ``error``, ``rate_limited`` or ``budget_exhausted``.
* ``median_latency_ms`` and ``p95_latency_ms`` (linear interpolation) of ``latency_ms``.
* ``median_data_age_s``: ``responded_at - data_max_as_of``, floored at 0, of attempts that
  returned data; null when none did.
* ``n_crosschecks`` / ``disagreement_rate``: cross-check rows of that run where the source is the
  left or right side (counted once if both), skipped checks excluded; ``disagreement_rate`` is the
  share that ended ``warn`` or ``fail`` and is null (unknown, not 0 %) when nothing was compared.
  Cross-check rows carry the run's ``target_date``; the date actually compared is
  ``details.ref_date`` and is exposed in the serving file, not used for scoring.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

from deskbrief.models import RunContext
from deskbrief.schemas import to_frame
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect

_FETCH_SQL = """
SELECT source, count(*),
       avg(CASE WHEN status IN ('ok', 'empty') THEN 1.0 ELSE 0.0 END),
       median(latency_ms), quantile_cont(latency_ms, 0.95),
       median(greatest(0, epoch(responded_at) - epoch(data_max_as_of))),
       avg(CASE WHEN status IN ('error', 'rate_limited', 'budget_exhausted') THEN 1.0 ELSE 0.0 END)
FROM source_log WHERE run_id = ? AND status <> 'skipped_calendar' GROUP BY source
"""
_XCHECK_SQL = """
SELECT s, count(*), count(*) FILTER (WHERE status IN ('warn', 'fail')) FROM (
  SELECT left_source AS s, status FROM xchecks WHERE run_id = $1 AND target_date = $2
  UNION ALL
  SELECT right_source, status FROM xchecks
    WHERE run_id = $1 AND target_date = $2 AND right_source <> left_source
) WHERE status <> 'skip' GROUP BY s
"""


def latest_run_id(duck: duckdb.DuckDBPyConnection, target_date: date) -> str | None:
    """The run that last started for ``target_date`` (from ``runs``), or ``None``."""
    row = duck.execute(
        "SELECT run_id FROM runs WHERE target_date = ? ORDER BY started_at DESC, run_id DESC "
        "LIMIT 1",
        [target_date],
    ).fetchone()
    return None if row is None else str(row[0])


def compute_scorecard(
    duck: duckdb.DuckDBPyConnection, target_date: date, *, run_id: str | None = None
) -> pd.DataFrame:
    """One row per source for ``target_date``, typed as ``scorecard_daily``.

    ``run_id`` names the latest run when the caller knows it (the run in progress has no ``runs``
    row yet); otherwise it is looked up. No run, or no fetch attempts, gives an empty frame.
    """
    run_id = run_id or latest_run_id(duck, target_date)
    if run_id is None:
        return to_frame("scorecard_daily", [])
    xc = {s: (n, bad) for s, n, bad in duck.execute(_XCHECK_SQL, [run_id, target_date]).fetchall()}
    rows = []
    for src, n, ok, med, p95, age, err in duck.execute(_FETCH_SQL, [run_id]).fetchall():
        n_x, bad = xc.get(src, (0, 0))
        rows.append(
            {
                "source": src,
                "date": target_date,
                "fetches": int(n),
                "ok_rate": float(ok),
                "median_latency_ms": med,
                "p95_latency_ms": p95,
                "median_data_age_s": age,
                "error_rate": float(err),
                "disagreement_rate": bad / n_x if n_x else None,
                "n_crosschecks": int(n_x),
            }
        )
    return to_frame("scorecard_daily", sorted(rows, key=lambda r: str(r["source"])))


def write_scorecard(ctx: RunContext) -> str | None:
    """Score ``ctx.target_date`` for this run and overwrite that day's file (spec §6).

    Returns the key, or ``None`` when there was nothing to score (an earlier file is kept).
    """
    duck = connect(ctx.store, ctx.config)
    try:
        df = compute_scorecard(duck, ctx.target_date, run_id=ctx.run_id)
    finally:
        duck.close()
    if df.empty:
        return None
    return Datasets(ctx.store).append("scorecard_daily", df, ctx, ctx.target_date, overwrite=True)
