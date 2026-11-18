"""Compaction of a day's clean files into ``run=compacted-{date}.parquet`` (spec §6).

Small-files control for the ``eod`` job. For a clean table and an ET partition date with more
than one file, every ``run=*.parquet`` file of that partition (the per-run files, rebuild files
named ``run={run_id}-{YYYY-MM}.parquet``, and an earlier compacted file) is merged into one file.
``compact_recent`` does that for each of the last days (eod, via ``pipeline.serving``), because a
later run (the morning job writing into D-1, a short backfill) adds files to older partitions.
Each (table, date) is independent: a failing one does not stop the others, and the failures are
raised together at the end as one ``CompactionError``.

Safety rules:

* **Nothing distinct is lost.** Only rows identical in every column except ``run_id`` are
  collapsed (the same fetch re-ingested or re-parsed). A restated value, a new ``fetch_id``, a new
  ``ingested_at`` or a new ``parser_version`` is a different version and stays. ``run_id`` is data,
  not the file name: rebuild files hold rows with their real ``run_id``, so it is never checked
  against the key. An input whose columns differ from the declared schema raises before anything
  is written, so no column is silently dropped.
* **Verify, then delete.** The compacted file is written, read back, and its row count must equal
  the distinct-row count of the inputs (which cannot exceed their total). Only then are the inputs
  deleted, and only the keys that were merged (a file written meanwhile survives). On a mismatch
  the destination is restored (or removed) and ``CompactionError`` is raised with every input
  still in place.
* **Safe to re-run.** With nothing but the compacted file left, a run changes nothing; a late
  file is merged into the existing compacted file.
* **Crash window.** The deletes cannot be atomic (no multi-key transaction on R2). If the process
  dies after the compacted file is written and before every input is deleted, nothing is lost,
  but the leftover inputs are exact copies of rows already in the compacted file. The views that
  pick one row per key (``obs_latest``, ``obs_pit``, ``daily_close``) hide them; ``obs_all`` and
  ``Datasets.read`` show the duplicates until the next eod compaction merges and finishes the
  deletes.
* **No concurrent runs.** Compaction must not run alongside itself or a writer of the same
  partition. The workflows' shared concurrency group (spec §5.4, ``deskbrief-pipeline``) enforces
  that, and the workflows must keep it.
* ``raw/`` is never listed, read or deleted (backends also refuse raw deletes).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from deskbrief.store.backend import (
    CLEAN_PREFIX,
    StorageBackend,
    StorageError,
    table_key,
    table_prefix,
)
from deskbrief.store.datasets import arrow_schema, current_clean_version
from deskbrief.store.table_cache import invalidate

#: clean tables written per run and per ET date (``source_log`` etc. live outside ``clean/``)
COMPACTED_TABLES: tuple[str, ...] = ("observations", "quarantine")
RECENT_DAYS = 7


class CompactionError(StorageError):
    """A compaction failed or did not verify; its inputs were left untouched."""


@dataclass(frozen=True)
class CompactionResult:
    table: str
    part_date: date
    inputs: int = 0
    rows_in: int = 0
    rows_out: int = 0
    deleted: int = 0
    skipped: str | None = None


def _read(store: StorageBackend, key: str, schema: pa.Schema) -> pd.DataFrame:
    table = pq.read_table(io.BytesIO(store.get_bytes(key)))
    if set(table.column_names) != set(schema.names):
        raise CompactionError(
            f"{key}: columns {sorted(table.column_names)} differ from the declared "
            f"{sorted(schema.names)}; nothing was compacted"
        )
    frame: pd.DataFrame = table.select(schema.names).cast(schema).to_pandas()
    return frame


def compact_partition(
    store: StorageBackend, table: str, part_date: date, *, clean_version: str | None = None
) -> CompactionResult:
    """Merge the partition's files; returns what was done (``skipped`` says why not)."""
    version = clean_version or current_clean_version(store)
    run_id = f"compacted-{part_date.isoformat()}"
    dest = table_key(table=table, part_date=part_date, run_id=run_id, clean_version=version)
    prefix = f"{table_prefix(table, version)}date={part_date.isoformat()}/"
    keys = [k for k in store.list(prefix) if k.endswith(".parquet")]
    stale = [k for k in keys if k != dest]
    if len(keys) < 2 or not stale or not dest.startswith(f"{CLEAN_PREFIX}/"):
        return CompactionResult(table, part_date, skipped="nothing to compact")
    schema = arrow_schema(table)
    frames = [_read(store, k, schema) for k in keys]  # an old compacted file is just an input
    rows_in = sum(len(f) for f in frames)
    merged = pd.concat(frames, ignore_index=True)
    distinct = merged.drop_duplicates(subset=[c for c in merged.columns if c != "run_id"])
    if len(distinct) > rows_in or (rows_in and not len(distinct)):
        raise CompactionError(f"{table} {part_date}: {len(distinct)} distinct of {rows_in} rows")
    previous = store.get_bytes(dest) if dest in keys else None
    sink = io.BytesIO()
    pq.write_table(
        pa.Table.from_pandas(distinct, schema=schema, preserve_index=False),
        sink,
        compression="zstd",
    )
    store.put_bytes(dest, sink.getvalue(), overwrite=True)
    invalidate()  # a key was rewritten in place: cached tables (store.table_cache) are stale
    written = pq.read_table(io.BytesIO(store.get_bytes(dest))).num_rows
    if written != len(distinct):
        if previous is None:
            store.delete(dest)
        else:
            store.put_bytes(dest, previous, overwrite=True)
        raise CompactionError(
            f"{table} {part_date}: row count {written} != expected {len(distinct)}; "
            f"kept all {len(stale)} input files"
        )
    for key in stale:
        store.delete(key)
    return CompactionResult(table, part_date, len(keys), rows_in, len(distinct), len(stale))


def _compact_all(store: StorageBackend, jobs: list[tuple[str, date]]) -> list[CompactionResult]:
    """Every (table, date) is attempted; failures are raised together at the end."""
    results: list[CompactionResult] = []
    errors: list[str] = []
    version = current_clean_version(store)  # once: every partition compacts the same version
    for table, part_date in jobs:
        try:
            results.append(compact_partition(store, table, part_date, clean_version=version))
        except Exception as exc:  # one failing partition must not stop the others
            errors.append(f"{table} {part_date}: {type(exc).__name__}: {exc}")
    if errors:
        raise CompactionError("; ".join(errors))
    return results


def compact_day(
    store: StorageBackend, part_date: date, tables: tuple[str, ...] = COMPACTED_TABLES
) -> list[CompactionResult]:
    """Compact every clean table of ``part_date`` (all tables are tried before any error)."""
    return _compact_all(store, [(t, part_date) for t in tables])


def compact_recent(
    store: StorageBackend,
    today: date,
    days: int = RECENT_DAYS,
    tables: tuple[str, ...] = COMPACTED_TABLES,
) -> list[CompactionResult]:
    """Compact each partition of ``today`` and the ``days - 1`` days before it."""
    return _compact_all(store, [(t, today - timedelta(d)) for d in range(days) for t in tables])
