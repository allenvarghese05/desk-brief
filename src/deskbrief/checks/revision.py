"""Revision: the same natural key arrives with a new value (spec §8.1).

Reads ``obs_all`` up to ``now`` (every version is needed). A version ingested by this run whose
value differs from the immediately preceding version of the same key is a revision: it becomes a
row of the ``revisions`` table (``detected_at`` = ``now``, so it partitions by the ET date of
``now``, P-006) and a ``warn`` (macro revisions are normal; they are recorded, not failed).
Sources with rows in this run and no revision get one ``pass``.

``evaluate`` returns only the DQ results; :meth:`RevisionCheck.scan` also returns the
``revisions`` frame, which :func:`deskbrief.checks.run_checks` uses.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from deskbrief.checks.base import make_result, query, records, register
from deskbrief.models import CheckType, DQResult, DQStatus, RunContext
from deskbrief.schemas import to_frame

_SQL = """
WITH v AS (
  SELECT *, lag(value) OVER w AS old_value, lag(fetch_id) OVER w AS old_fetch_id
  FROM obs_all WHERE ingested_at <= ?
  WINDOW w AS (PARTITION BY instrument_id, source, dataset, field, interval, as_of
               ORDER BY ingested_at, fetch_id)
)
SELECT instrument_id, source, dataset, field, interval, as_of, old_value,
       value AS new_value, old_fetch_id, fetch_id AS new_fetch_id
FROM v WHERE run_id = ? AND old_value IS NOT NULL AND value <> old_value
ORDER BY source, dataset, instrument_id, as_of
"""


class RevisionCheck:
    name = "revision"
    check_type = CheckType.REVISION

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        return self.scan(ctx, duck)[0]

    def scan(
        self, ctx: RunContext, duck: duckdb.DuckDBPyConnection
    ) -> tuple[list[DQResult], pd.DataFrame]:
        revs = query(duck, _SQL, [ctx.now_utc, ctx.run_id])
        seen = query(
            duck,
            "SELECT DISTINCT source, dataset FROM obs_all WHERE run_id = ? AND ingested_at <= ?",
            [ctx.run_id, ctx.now_utc],
        )
        rev_rows = records(revs)
        rows = [
            {
                "instrument_id": r["instrument_id"],
                "source": r["source"],
                "dataset": r["dataset"],
                "field": r["field"],
                "interval": r["interval"],
                "as_of": r["as_of"],
                "old_value": float(r["old_value"]),
                "new_value": float(r["new_value"]),
                "old_fetch_id": r["old_fetch_id"],
                "new_fetch_id": r["new_fetch_id"],
                "detected_at": ctx.now_utc,
                "run_id": ctx.run_id,
            }
            for r in rev_rows
        ]
        out = [
            make_result(
                ctx,
                self,
                DQStatus.WARN,
                source=str(r["source"]),
                instrument_id=str(r["instrument_id"]),
                observed=float(r["new_value"]),
                threshold=float(r["old_value"]),
                dataset=str(r["dataset"]),
                field=str(r["field"]),
                as_of=str(r["as_of"]),
                old_value=float(r["old_value"]),
                new_value=float(r["new_value"]),
            )
            for r in rev_rows
        ]
        revised = set(zip(revs["source"], revs["dataset"], strict=True))
        out += [
            make_result(
                ctx, self, DQStatus.PASS, source=str(r["source"]), dataset=str(r["dataset"])
            )
            for r in records(seen)
            if (r["source"], r["dataset"]) not in revised
        ]
        return out, to_frame("revisions", rows)


register(RevisionCheck())
