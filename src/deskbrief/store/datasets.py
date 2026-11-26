"""Parquet tables on a ``StorageBackend``: validated append and simple read (spec §6, §7.2).

Every write is validated against the table's pandera schema first, so nothing malformed ever
reaches storage. Parquet column types are fixed from the declared table schema (not inferred
from the data), so files written by different runs, including zero-row files, always agree
and DuckDB can union them.

Run-scoped tables are one file per run per partition date
(``…/date={d}/run={run_id}.parquet``) and are write-once like everything else: appending the
same table/date twice in one run raises ``KeyExistsError``; batch rows per partition before
calling :meth:`Datasets.append`. ``scorecard_daily`` is one file per date, overwritten.

Partition membership (P-006) is checked on every append: each row must belong to ``part_date``
(observations by ``et_date(as_of)``, quarantine/revisions by ``et_date(detected_at)``,
source_log by ``et_date(requested_at)``, dq/crosscheck/runs by ``target_date``, events and
filings by ``et_date(ingested_at)``), else
``ValueError``. (``alerts`` has no date column, so it cannot be checked.) ``overwrite=True`` is
only accepted for the daily tables.

``clean/_CURRENT`` is resolved once per ``Datasets`` instance (on first use) so a run writes and
reads one consistent version; call :meth:`Datasets.refresh_clean_version` after a rebuild flips it.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import date, datetime

import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped]  # pyarrow ships no stubs
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from deskbrief.models import RunContext
from deskbrief.schemas import TABLES, empty_frame, validate
from deskbrief.store.backend import (
    CLEAN_CURRENT_KEY,
    CLEAN_PREFIX,
    DAILY_TABLES,
    TABLE_LAYER,
    KeyNotFoundError,
    StorageBackend,
    StorageError,
    scorecard_key,
    table_key,
    table_prefix,
)
from deskbrief.timeutil import et_date

DEFAULT_CLEAN_VERSION = "v1"


def current_clean_version(store: StorageBackend) -> str:
    """Version named by ``clean/_CURRENT``; ``v1`` when the pointer has not been written yet."""
    try:
        text = store.get_bytes(CLEAN_CURRENT_KEY).decode("utf-8").strip()
    except KeyNotFoundError:
        return DEFAULT_CLEAN_VERSION
    return text or DEFAULT_CLEAN_VERSION


def _plain_date(value: object) -> date:
    assert isinstance(value, date)
    return value


def _et_date_of(value: object) -> date:
    assert isinstance(value, datetime)
    return et_date(value)


#: table -> (column, function giving the partition date of that column's value). P-006.
#: ``alerts`` is deliberately absent: P-006 partitions it by the run's ``target_date`` but the
#: ``alerts`` schema has no date column to check, so there is nothing to verify per row.
PARTITION_RULES: dict[str, tuple[str, Callable[[object], date]]] = {
    "observations": ("as_of", _et_date_of),
    "quarantine": ("detected_at", _et_date_of),
    "revisions": ("detected_at", _et_date_of),
    "source_log": ("requested_at", _et_date_of),
    "dq_results": ("target_date", _plain_date),
    "crosscheck_results": ("target_date", _plain_date),
    "runs": ("target_date", _plain_date),
    "scorecard_daily": ("date", _plain_date),  # the ET date scored
    "events": ("ingested_at", _et_date_of),  # P-024 / T64: the fetch date, one file per run
    "filings": ("ingested_at", _et_date_of),  # P-024 / T64: the fetch date, one file per run
}


def check_partition(table: str, df: pd.DataFrame, part_date: date) -> None:
    """Raise ``ValueError`` if any row of ``df`` does not belong to ``part_date`` (P-006)."""
    rule = PARTITION_RULES.get(table)
    if rule is None or df.empty:
        return
    column, to_date = rule
    bad = sorted({d for d in (to_date(v) for v in df[column]) if d != part_date})
    if bad:
        shown = ", ".join(d.isoformat() for d in bad[:5])
        raise ValueError(
            f"{table}: {column} falls on {shown}, not partition date {part_date.isoformat()}"
        )


def _arrow_type(kind: str) -> pa.DataType:
    base = kind.rstrip("?")
    if base == "ts":
        return pa.timestamp("us", tz="UTC")
    if base == "date":
        return pa.date32()
    if base == "int":
        return pa.int64()
    if base == "float":
        return pa.float64()
    if base == "bool":
        return pa.bool_()
    return pa.string()  # str and enum:*


def arrow_schema(table: str) -> pa.Schema:
    """Parquet schema for ``table``, derived from the declared kinds in ``deskbrief.schemas``."""
    return pa.schema(
        [pa.field(c, _arrow_type(k), nullable=k.endswith("?")) for c, k in TABLES[table].items()]
    )


class Datasets:
    """Validated Parquet append/read for the tables in spec §7.2."""

    def __init__(self, store: StorageBackend) -> None:
        self._store = store
        self._clean_version: str | None = None

    def clean_version(self) -> str:
        """``clean/_CURRENT``, read once and then cached for this instance."""
        if self._clean_version is None:
            self._clean_version = current_clean_version(self._store)
        return self._clean_version

    def _check_version_not_stale(self) -> None:
        """One cheap read of ``clean/_CURRENT`` before a clean-table write (never on reads)."""
        cached = self.clean_version()
        now = current_clean_version(self._store)
        if now != cached:
            raise StorageError(
                f"clean/_CURRENT is {now!r} but this Datasets instance resolved {cached!r}; "
                "call refresh_clean_version() or create a new Datasets before writing"
            )

    def refresh_clean_version(self) -> str:
        """Re-read ``clean/_CURRENT`` (e.g. after a rebuild flipped it).

        An instance that forgets to call this after a flip is not silent: the next clean-table
        ``append`` raises ``StorageError`` instead of writing to the old version.
        """
        self._clean_version = None
        return self.clean_version()

    def key_for(self, table: str, part_date: date, run_id: str) -> str:
        if table in DAILY_TABLES:
            return scorecard_key(part_date)
        return table_key(
            table=table, part_date=part_date, run_id=run_id, clean_version=self.clean_version()
        )

    def append(
        self,
        table: str,
        df: pd.DataFrame,
        ctx: RunContext,
        part_date: date,
        *,
        overwrite: bool = False,
    ) -> str:
        """Validate ``df`` and write it as one Parquet file; return its key.

        Raises ``pandera.errors.SchemaErrors`` for invalid frames (nothing is written),
        ``ValueError`` if a row does not belong to ``part_date`` or ``overwrite`` is requested
        for a non-daily table, and ``KeyExistsError`` if this run already wrote this table/date.
        """
        if overwrite and table not in DAILY_TABLES:
            raise ValueError(f"overwrite is only allowed for daily tables, not {table!r}")
        key = self.key_for(table, part_date, ctx.run_id)
        if TABLE_LAYER.get(table) == CLEAN_PREFIX:
            self._check_version_not_stale()
        validate(table, df)
        check_partition(table, df, part_date)
        arrow = pa.Table.from_pandas(df, schema=arrow_schema(table), preserve_index=False)
        sink = io.BytesIO()
        pq.write_table(arrow, sink, compression="zstd")
        # the daily scorecard is "one file per date, overwritten per run" (spec §6)
        self._store.put_bytes(key, sink.getvalue(), overwrite=overwrite or table in DAILY_TABLES)
        return key

    def read(self, table: str, *, part_date: date | None = None) -> pd.DataFrame:
        """All rows of ``table`` (optionally one partition date), typed like ``empty_frame``."""
        prefix = table_prefix(table, self.clean_version())
        keys = [k for k in self._store.list(prefix) if k.endswith(".parquet")]
        if part_date is not None:
            tag = f"date={part_date.isoformat()}"
            keys = [k for k in keys if f"/{tag}/" in k or k.endswith(f"/{tag}.parquet")]
        template = empty_frame(table)
        if not keys:
            return template
        frames: list[pd.DataFrame] = [
            pq.read_table(io.BytesIO(self._store.get_bytes(k))).to_pandas() for k in keys
        ]
        merged = pd.concat(frames, ignore_index=True)
        typed: pd.DataFrame = merged[list(template.columns)].astype(dict(template.dtypes))
        return typed
