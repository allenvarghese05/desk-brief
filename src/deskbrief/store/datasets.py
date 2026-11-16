"""Parquet tables on a ``StorageBackend``: validated append and simple read (spec §6, §7.2).

Every write is validated against the table's pandera schema first, so nothing malformed ever
reaches storage. Parquet column types are fixed from the declared table schema (not inferred
from the data), so files written by different runs, including zero-row files, always agree
and DuckDB can union them.

Run-scoped tables are one file per run per partition date
(``…/date={d}/run={run_id}.parquet``) and are write-once like everything else: appending the
same table/date twice in one run raises ``KeyExistsError``; batch rows per partition before
calling :meth:`Datasets.append`. ``scorecard_daily`` is one file per date, overwritten.
"""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped]  # pyarrow ships no stubs
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from deskbrief.models import RunContext
from deskbrief.schemas import TABLES, empty_frame, validate
from deskbrief.store.backend import (
    CLEAN_CURRENT_KEY,
    DAILY_TABLES,
    KeyNotFoundError,
    StorageBackend,
    scorecard_key,
    table_key,
    table_prefix,
)

DEFAULT_CLEAN_VERSION = "v1"


def current_clean_version(store: StorageBackend) -> str:
    """Version named by ``clean/_CURRENT``; ``v1`` when the pointer has not been written yet."""
    try:
        text = store.get_bytes(CLEAN_CURRENT_KEY).decode("utf-8").strip()
    except KeyNotFoundError:
        return DEFAULT_CLEAN_VERSION
    return text or DEFAULT_CLEAN_VERSION


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

    def clean_version(self) -> str:
        return current_clean_version(self._store)

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

        Raises ``pandera.errors.SchemaErrors`` for invalid frames (nothing is written) and
        ``KeyExistsError`` if this run already wrote this table/date (unless ``overwrite``).
        """
        key = self.key_for(table, part_date, ctx.run_id)
        validate(table, df)
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
