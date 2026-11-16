"""Uniqueness: no duplicate natural key within a single fetch (spec §8.1).

Reads ``obs_all`` (``obs_latest``/``obs_pit`` would hide duplicates) for this run's rows with
``ingested_at <= now``, grouped by ``fetch_id``. The same key in two different fetches is a
revision, not a duplicate. One result per (source, dataset): ``fail`` if any key repeats within
a fetch (``observed`` = number of duplicated keys), else ``pass``.
"""

from __future__ import annotations

import duckdb

from deskbrief.checks.base import make_result, query, records, register
from deskbrief.models import CheckType, DQResult, DQStatus, RunContext

_SQL = """
WITH k AS (
  SELECT source, dataset, fetch_id, instrument_id, field, interval, as_of, count(*) AS c
  FROM obs_all WHERE run_id = ? AND ingested_at <= ? GROUP BY ALL
)
SELECT source, dataset, count(*) AS n_keys, count(*) FILTER (WHERE c > 1) AS dupe_keys,
       count(DISTINCT fetch_id) FILTER (WHERE c > 1) AS dupe_fetches
FROM k GROUP BY source, dataset ORDER BY source, dataset
"""


class UniquenessCheck:
    name = "uniqueness"
    check_type = CheckType.UNIQUENESS

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        df = query(duck, _SQL, [ctx.run_id, ctx.now_utc])
        if df.empty:
            return [make_result(ctx, self, DQStatus.SKIP, reason="no observations in this run")]
        return [
            make_result(
                ctx,
                self,
                DQStatus.FAIL if r["dupe_keys"] else DQStatus.PASS,
                source=str(r["source"]),
                observed=float(r["dupe_keys"]),
                threshold=0.0,
                dataset=str(r["dataset"]),
                keys=int(r["n_keys"]),
                fetches_with_duplicates=int(r["dupe_fetches"]),
            )
            for r in records(df)
        ]


register(UniquenessCheck())
