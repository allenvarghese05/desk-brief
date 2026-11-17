"""``scorecard_daily``: measured per-source numbers for one ET date (spec §7.2, §10.3 page 5).

Built from ``source_log`` and the cross-check results, on a DuckDB connection from
``store.duck.connect``. Re-runs of a date keep every run's rows (P-008), so counting every run
would score a re-run as extra fetches. The rule, for each ``(target_date, source)``:

* for ``morning``, ``eod`` and ``backfill``, take the **latest run of that job** (newest
  ``started_at``) that fetched that source on that date; the newest run wins, so a re-run replaces
  the earlier one;
* for ``intraday``, take **every run** of that job and date: it polls every 15 minutes and each
  poll is a distinct scheduled run, not a re-run, so all of them are pooled;
* **combine across jobs**: a source fetched by morning, intraday and eod is scored on all of
  those runs, and one fetched only by morning is still scored.

A partial re-run (one source) therefore replaces only that source for its job and date; the other
sources keep the numbers of their earlier run. The date scored is ``target_date`` (P-006).

Per source, over the chosen runs' fetch attempts (``fetches`` and ``n_crosschecks`` are sums across
the chosen runs, intraday polls included; the rates and percentiles are pooled over all of those
attempts, not averaged per job):

* ``skipped_calendar`` rows are not attempts and are left out; a source with no attempt gets no row.
* ``ok_rate`` share with status ``ok`` or ``empty`` (the ``query`` rule); ``error_rate`` share with
  ``error``, ``rate_limited`` or ``budget_exhausted``.
* ``median_latency_ms`` and ``p95_latency_ms`` (linear interpolation) of ``latency_ms``.
* ``median_data_age_s``: ``responded_at - data_max_as_of``, floored at 0, of attempts that returned
  data; null when none did.
* ``n_crosschecks`` / ``disagreement_rate``: cross-check rows (dated ``target_date``) of the chosen
  runs where the source is the left or right side (counted once if both), skipped checks
  excluded; ``disagreement_rate`` is the share ending ``warn`` or ``fail`` and is null (unknown,
  not 0 %) when nothing was compared. The date actually compared is ``details.ref_date``; it is
  exposed in the serving file, not used for scoring.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

from deskbrief.models import RunContext
from deskbrief.schemas import to_frame
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect

# $1 date, $2-$4 the run in progress (no ``runs`` row yet: the runner writes it after the hooks)
_SQL = """
WITH r AS (
  SELECT run_id, job, started_at FROM runs WHERE target_date = $1 AND run_id IS DISTINCT FROM $2
  UNION ALL SELECT $2, $3, $4
), pick AS (
  SELECT l.*, r.job FROM source_log l JOIN r USING (run_id) WHERE l.status <> 'skipped_calendar'
  QUALIFY r.job = 'intraday' OR dense_rank() OVER (
    PARTITION BY r.job, l.source ORDER BY r.started_at DESC, l.run_id DESC) = 1
), f AS (
  SELECT source, count(*) AS n,
         avg(CASE WHEN status IN ('ok', 'empty') THEN 1.0 ELSE 0.0 END) AS ok,
         median(latency_ms) AS med, quantile_cont(latency_ms, 0.95) AS p95,
         median(greatest(0, epoch(responded_at) - epoch(data_max_as_of))) AS age,
         avg(CASE WHEN status IN ('error', 'rate_limited', 'budget_exhausted')
                  THEN 1.0 ELSE 0.0 END) AS err
  FROM pick GROUP BY source
), x AS (
  SELECT p.source, count(*) AS n, count(*) FILTER (WHERE c.status IN ('warn', 'fail')) AS bad
  FROM (SELECT DISTINCT source, run_id FROM pick) p
  JOIN xchecks c ON c.run_id = p.run_id AND c.target_date = $1 AND c.status <> 'skip'
   AND p.source IN (c.left_source, c.right_source)
  GROUP BY p.source
)
SELECT f.source, f.n, f.ok, f.med, f.p95, f.age, f.err, coalesce(x.n, 0), coalesce(x.bad, 0)
FROM f LEFT JOIN x USING (source) ORDER BY f.source
"""


def compute_scorecard(
    duck: duckdb.DuckDBPyConnection, target_date: date, *, current: RunContext | None = None
) -> pd.DataFrame:
    """One row per source for ``target_date``, typed as ``scorecard_daily``.

    ``current`` is the run in progress, when it is for ``target_date``: it has no ``runs`` row yet,
    and it is the newest run of its job. No fetch attempts gives an empty frame.
    """
    run = current if current is not None and current.target_date == target_date else None
    params = [target_date, run and run.run_id, run and run.job.value, run and run.now_utc]
    rows = [
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
        for src, n, ok, med, p95, age, err, n_x, bad in duck.execute(_SQL, params).fetchall()
    ]
    return to_frame("scorecard_daily", rows)


def write_scorecard(ctx: RunContext) -> str | None:
    """Score ``ctx.target_date`` and overwrite that day's file (spec §6).

    Returns the key, or ``None`` when there was nothing to score (an earlier file is kept).
    """
    duck = connect(ctx.store, ctx.config)
    try:
        df = compute_scorecard(duck, ctx.target_date, current=ctx)
    finally:
        duck.close()
    if df.empty:
        return None
    return Datasets(ctx.store).append("scorecard_daily", df, ctx, ctx.target_date, overwrite=True)
