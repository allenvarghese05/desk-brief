"""In-process cache of the append-only tables behind the DuckDB views (T25c).

Why: every check, cross-check, alert, serving and report query used to run against
``read_parquet('r2://.../date=*/run=*.parquet')``, so DuckDB re-listed and re-read every small
file on R2 (``obs_files 1620`` in live run 3) for *each* query. That was most of the ~9-minute
run and of the ~5 minutes per catch-up day. Now :func:`load_table` reads a table's files once,
in parallel through the :class:`~deskbrief.store.backend.StorageBackend`, and keeps the Arrow
table for the life of the process; ``duck.connect`` copies it into the connection.

Freshness: the entry is keyed on the exact sorted list of file keys. Run tables are append-only
(a new file has a new key: ``run=<run_id>``), so a changed listing means changed content and
the table is read again. Only the last listing per table is kept, so a key set can never come
back to an older snapshot. The one writer that overwrites a key in these tables, compaction,
calls :func:`invalidate` too. ``scorecard_daily`` (a ``DAILY_TABLES`` overwrite table) is never
cached.

Files are read independently and conformed to the declared schema (missing columns become
nulls, extra columns are dropped), the equivalent of ``read_parquet(..., union_by_name=true)``.
"""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from deskbrief.store.backend import StorageBackend
from deskbrief.store.datasets import arrow_schema

#: parallel GETs when reading a table's files (R2 latency, not bandwidth, is the limit)
READ_WORKERS = 16

_lock = threading.Lock()
_cache: dict[tuple[str, str, str, str], tuple[tuple[str, ...], pa.Table]] = {}


def _store_id(store: StorageBackend) -> tuple[str, str]:
    where = getattr(store, "root", None) or getattr(store, "bucket", None) or id(store)
    return type(store).__name__, str(where)


def cached_schema(table: str) -> pa.Schema:
    """The declared schema with every column nullable (an older file may lack a column)."""
    return pa.schema([f.with_nullable(True) for f in arrow_schema(table)])


def _conform(data: bytes, schema: pa.Schema) -> pa.Table:
    t = pq.read_table(io.BytesIO(data))
    cols = [
        t.column(f.name).cast(f.type) if f.name in t.column_names else pa.nulls(t.num_rows, f.type)
        for f in schema
    ]
    return pa.Table.from_arrays(cols, schema=schema)


def read_files(store: StorageBackend, table: str, keys: tuple[str, ...]) -> pa.Table:
    """Every file in ``keys`` as one table in the declared schema (empty table for no keys)."""
    schema = cached_schema(table)
    if not keys:
        return schema.empty_table()
    with ThreadPoolExecutor(max_workers=min(READ_WORKERS, len(keys))) as pool:
        parts = list(pool.map(lambda k: _conform(store.get_bytes(k), schema), keys))
    return pa.concat_tables(parts)


def load_table(
    store: StorageBackend, table: str, clean_version: str, keys: tuple[str, ...]
) -> pa.Table:
    """``table``'s rows from ``keys``, read from the store only when the listing changed."""
    ident = (*_store_id(store), table, clean_version)
    with _lock:
        hit = _cache.get(ident)
    if hit is not None and hit[0] == keys:
        return hit[1]
    data = read_files(store, table, keys)
    with _lock:
        _cache[ident] = (keys, data)
    return data


def invalidate() -> None:
    """Forget every cached table (compaction rewrites keys in place)."""
    with _lock:
        _cache.clear()


__all__ = ["READ_WORKERS", "cached_schema", "invalidate", "load_table", "read_files"]
