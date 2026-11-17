"""Serving files (spec §6) and the serving hook, the last data step of every job.

The dashboard (T34/T35) reads only these small files; they are rebuilt from the whole store at the
end of each job (overwritten, never appended), so they always describe the newest state of every
source, whatever job ran last. Every window and summary is anchored on **today's ET date**
(``et_date(ctx.now_utc)``), never on ``ctx.target_date``: a backfill of a date 20 days back must
not move the dashboard's view. Written under ``serving/``:

``latest_snapshot.parquet``  latest value per instrument at ``ctx.now_utc``
                             (``query.latest_snapshot``):
                             ``tier``, ``as_of`` (the observation instant), ``source``, ``field``,
                             ``unit``, ``is_proxy``, lineage ids, plus ``label`` (P-009 wording),
                             ``is_headline`` and ``snapshot_at``
``health.json``              ``query.health_summary`` for today plus ``headline_missing``:
                             headline instruments with no value (named, never silently dropped)
``scorecard_30d.parquet``    ``scorecard_daily`` rows of the last 30 days
``crosschecks_7d.parquet``   cross-check rows of the last 7 days, latest evaluation of
                             each check, with ``ref_date`` (``details.ref_date``): the date compared
``series_30d.parquet``       one official daily value per instrument and ET date (``series_all``)
``runs_14d.parquet``         ``runs`` of the last 14 days (the run in progress is not in it yet:
                             the runner writes its row after the hooks)

:func:`serving_step` is the ``build_serving`` hook. In every job it first (re)writes the
scorecard of ``ctx.target_date`` (an idempotent overwrite of that date's partition; spec §5.4:
"scorecard → compaction → serving", compaction being card T23B), so a morning run rescores the
day it targets including its own fetches. Each part is isolated: a failing one is reported by
raising after the others ran, so the run is ``degraded`` and the dashboard still gets fresh
files.
"""

from __future__ import annotations

import dataclasses
import io
import json
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import duckdb
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from deskbrief.logging import redact
from deskbrief.models import RunContext
from deskbrief.query import ValueRow, health_summary, latest_snapshot, series_all, value_label
from deskbrief.schemas import columns
from deskbrief.scorecard import write_scorecard
from deskbrief.store.backend import StorageBackend, serving_key
from deskbrief.store.datasets import arrow_schema
from deskbrief.store.duck import connect
from deskbrief.timeutil import et_date

SCORECARD_DAYS, CROSSCHECK_DAYS, SERIES_DAYS, RUNS_DAYS = 30, 7, 30, 14
_UTC = pa.timestamp("us", tz="UTC")
_ARROW = {
    "str": pa.string(),
    "str | None": pa.string(),
    "float": pa.float64(),
    "int": pa.int64(),
    "bool": pa.bool_(),
    "datetime": _UTC,
}
_SNAPSHOT = pa.schema(
    [pa.field(f.name, _ARROW[str(f.type)]) for f in dataclasses.fields(ValueRow)]
    + [
        pa.field("label", pa.string()),
        pa.field("is_headline", pa.bool_()),
        pa.field("snapshot_at", _UTC),
    ]
)
_SERIES = pa.schema(
    [
        pa.field("instrument_id", pa.string()),
        pa.field("date", pa.date32()),
        pa.field("value", pa.float64()),
        pa.field("tier", pa.int64()),
        pa.field("unit", pa.string()),
        pa.field("field", pa.string()),
        pa.field("source", pa.string()),
    ]
)
_XCHECK = arrow_schema("crosscheck_results").append(pa.field("ref_date", pa.date32()))


def _put(store: StorageBackend, name: str, data: bytes) -> str:
    key = serving_key(name)
    store.put_bytes(key, data, overwrite=True)
    return key


def _parquet(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="zstd")
    return sink.getvalue()


def _sql_table(
    duck: duckdb.DuckDBPyConnection, sql: str, params: list[Any], schema: pa.Schema
) -> pa.Table:
    table = duck.sql(sql, params=params).to_arrow_table()
    return table.select(schema.names).cast(schema)


def _ref_date(details_json: str) -> date | None:
    try:
        ref = json.loads(details_json).get("ref_date")
        return date.fromisoformat(ref) if isinstance(ref, str) else None
    except (ValueError, AttributeError):
        return None


def _snapshot_table(duck: duckdb.DuckDBPyConnection, ctx: RunContext) -> tuple[pa.Table, list[str]]:
    heads = set(ctx.config.report.headline_instruments)
    rows = latest_snapshot(duck, ctx.now_utc)
    data = [
        dataclasses.asdict(r)
        | {
            "label": value_label(r),
            "is_headline": r.instrument_id in heads,
            "snapshot_at": ctx.now_utc,
        }
        for r in rows
    ]
    missing = [
        h
        for h in ctx.config.report.headline_instruments
        if h not in {r.instrument_id for r in rows}
    ]
    return pa.Table.from_pylist(data, schema=_SNAPSHOT), missing


def _health(duck: duckdb.DuckDBPyConnection, ctx: RunContext, missing: list[str]) -> bytes:
    summary = health_summary(duck, et_date(ctx.now_utc))
    body = dataclasses.asdict(summary) | {
        "n_checks": summary.n_checks,
        "headline_missing": missing,
        "generated_at": ctx.now_utc.isoformat(),
        "serving_run_id": ctx.run_id,
        "job": ctx.job.value,
    }
    return json.dumps(body, default=str, indent=2, sort_keys=True).encode()


def _crosschecks(duck: duckdb.DuckDBPyConnection, ctx: RunContext) -> pa.Table:
    today = et_date(ctx.now_utc)
    cols = ", ".join(f'"{c}"' for c in columns("crosscheck_results"))
    table = _sql_table(
        duck,
        f"SELECT {cols} FROM xchecks WHERE target_date > ? AND target_date <= ? "  # noqa: S608
        "QUALIFY row_number() OVER (PARTITION BY check_name, coalesce(group_id, ''), "
        "left_instrument, left_source, right_instrument, right_source, target_date "
        "ORDER BY at_time DESC, result_id DESC) = 1 ORDER BY target_date, check_name",
        [today - timedelta(days=CROSSCHECK_DAYS), today],
        arrow_schema("crosscheck_results"),
    )
    refs = pa.array([_ref_date(d) for d in table["details_json"].to_pylist()], pa.date32())
    return table.append_column("ref_date", refs).cast(_XCHECK)


def _windowed(duck: duckdb.DuckDBPyConnection, ctx: RunContext, table: str, view: str, col: str,
              days: int) -> pa.Table:  # fmt: skip
    today = et_date(ctx.now_utc)
    cols = ", ".join(f'"{c}"' for c in columns(table))
    return _sql_table(
        duck,
        f"SELECT {cols} FROM {view} WHERE {col} > ? AND {col} <= ? ORDER BY {col}",  # noqa: S608
        [today - timedelta(days=days), today],
        arrow_schema(table),
    )


def _series(duck: duckdb.DuckDBPyConnection, ctx: RunContext) -> pa.Table:
    rows = [
        {"instrument_id": iid, "date": et_date(r.as_of), "value": r.value, "tier": r.tier,
         "unit": r.unit, "field": r.field, "source": r.source}
        for iid, history in series_all(duck, SERIES_DAYS, ctx.now_utc).items()
        for r in history
    ]  # fmt: skip
    return pa.Table.from_pylist(rows, schema=_SERIES)


def build_serving_files(ctx: RunContext) -> list[str]:
    """(Over)write every serving file from the store; returns the keys written."""
    duck = connect(ctx.store, ctx.config)  # after the scorecard write, so it sees that file
    try:
        snapshot, missing = _snapshot_table(duck, ctx)
        parts = {
            "latest_snapshot.parquet": _parquet(snapshot),
            "health.json": _health(duck, ctx, missing),
            "scorecard_30d.parquet": _parquet(
                _windowed(duck, ctx, "scorecard_daily", "scorecard", "date", SCORECARD_DAYS)
            ),
            "crosschecks_7d.parquet": _parquet(_crosschecks(duck, ctx)),
            "series_30d.parquet": _parquet(_series(duck, ctx)),
            "runs_14d.parquet": _parquet(
                _windowed(duck, ctx, "runs", "runs", "target_date", RUNS_DAYS)
            ),
        }
    finally:
        duck.close()
    return [_put(ctx.store, name, data) for name, data in parts.items()]


def serving_step(ctx: RunContext) -> None:
    """``build_serving`` hook: (eod) the day's scorecard, then every serving file."""
    parts: list[tuple[str, Callable[[], object]]] = []
    parts.append(("scorecard", lambda: write_scorecard(ctx)))
    parts.append(("serving", lambda: build_serving_files(ctx)))
    errors = []
    for name, part in parts:
        try:
            part()
        except Exception as exc:  # one failing part must not stop the others
            errors.append(f"{name}: {type(exc).__name__}: {redact(str(exc))}")
    if errors:
        raise RuntimeError("; ".join(errors))


__all__ = ["build_serving_files", "serving_step"]
