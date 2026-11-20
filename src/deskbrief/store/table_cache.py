"""In-process cache of the append-only tables behind the DuckDB views (T25c, T56).

Why: every check, cross-check, alert, serving and report query used to run against
``read_parquet('r2://.../date=*/run=*.parquet')``, so DuckDB re-listed and re-read every small
file on R2 (``obs_files 1620`` in live run 3) for *each* query. That was most of the ~9-minute
run and of the ~5 minutes per catch-up day. Now :func:`load_table` reads a table's files once,
in parallel through the :class:`~deskbrief.store.backend.StorageBackend`, and keeps the Arrow
table for the life of the process; ``duck.connect`` copies it into the connection.

Incremental (T56): the entry keeps each file's rows, so a changed listing costs only the change:

* a key not seen before is downloaded and appended (one new file = one GET);
* when any key of an ET-date partition (``.../date=YYYY-MM-DD/``) disappears, the whole
  partition is evicted and every key it still lists is read again;
* whenever the listing changed at all, every ``run=compacted-*.parquet`` key of the last
  ``COMPACTED_WINDOW_DAYS`` partition dates (counted back from the newest partition in the
  listing) is read again, whether or not its partition's key set changed;
* loads are single-flight per table: a second caller waits for the first and then downloads
  only what the first did not.

Why the compacted re-read: eod compaction (P-017, ``compact_recent``) rewrites
``run=compacted-{date}.parquet`` in place for today and the six days before it. On a normal day
the morning job writes official closes into D-1, which eod D-1 already compacted, and eod D
merges them into the same ``compacted-(D-1)`` key and deletes the morning file. A dashboard
that took no listing in between (nobody opened it from about 10:15 to 22:30 UTC) sees the same
key set for D-1 before and after, so eviction on a disappearing key alone would keep the old
rows for good (PR #86 review round 1, finding 1). Every pipeline job also writes a new ``runs``
file, so a job that rewrites a compacted file always changes the listing and triggers this
re-read. It costs at most ``COMPACTED_WINDOW_DAYS`` GETs per compacted table per changed
listing (the dashboard caches ``observations``; ``quarantine`` is not a view).

Assumptions:

* Run tables are append-only: a new file has a new key (``run=<run_id>``), and the only
  writer that rewrites a key in place is compaction (``store.compaction``).
* Scheduled compaction only rewrites partitions within ``compaction.RECENT_DAYS`` of its own
  ET date, which is no later than the newest partition in the listing.
  ``COMPACTED_WINDOW_DAYS`` equals ``RECENT_DAYS`` (pinned by a test; ``compaction`` imports
  this module, so the constant lives here).
* A *manual* ``compact_partition`` / ``compact_day`` of an older partition that merges a file
  this process never listed is not seen until that partition loses another key or the process
  restarts. Restart the dashboard after a manual compaction of partitions older than a week.
* Compaction in this process also calls :func:`invalidate`, which drops every entry.

The result is one contiguous Arrow table per listing (``combine_chunks``): DuckDB copies it
about 8x faster than the 2,000-chunk concatenation of the per-file tables. That costs a second
in-memory copy of the table.

``scorecard_daily`` (a ``DAILY_TABLES`` overwrite table) is never cached. Files are read
independently and conformed to the declared schema (missing columns become nulls, extra columns
are dropped), the equivalent of ``read_parquet(..., union_by_name=true)``.
"""

from __future__ import annotations

import io
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from deskbrief.store.backend import StorageBackend
from deskbrief.store.datasets import arrow_schema

#: parallel GETs when reading a table's files (R2 latency, not bandwidth, is the limit)
READ_WORKERS = 16
#: partition dates whose compacted file is re-read on every listing change; equals
#: ``compaction.RECENT_DAYS`` (that module imports this one, so the value is pinned by a test)
COMPACTED_WINDOW_DAYS = 7

_COMPACTED = re.compile(r"/date=(\d{4}-\d{2}-\d{2})/run=compacted-[^/]*\.parquet$")
_PART_DATE = re.compile(r"/date=(\d{4}-\d{2}-\d{2})/")


@dataclass
class _Entry:
    """One table of one store: per-file rows and the combined table of the last listing."""

    lock: threading.Lock = field(default_factory=threading.Lock)  # single flight per table
    parts: dict[str, pa.Table] = field(default_factory=dict)
    keys: tuple[str, ...] | None = None
    table: pa.Table | None = None


_lock = threading.Lock()  # guards _cache itself, never held while reading
_cache: dict[tuple[str, str, str, str], _Entry] = {}


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


def _read_each(store: StorageBackend, schema: pa.Schema, keys: tuple[str, ...]) -> list[pa.Table]:
    if not keys:
        return []
    with ThreadPoolExecutor(max_workers=min(READ_WORKERS, len(keys))) as pool:
        return list(pool.map(lambda k: _conform(store.get_bytes(k), schema), keys))


def read_files(store: StorageBackend, table: str, keys: tuple[str, ...]) -> pa.Table:
    """Every file in ``keys`` as one table in the declared schema (empty table for no keys)."""
    schema = cached_schema(table)
    if not keys:
        return schema.empty_table()
    return pa.concat_tables(_read_each(store, schema, keys))


def partition_of(key: str) -> str:
    """The ET-date partition a file belongs to: its key up to and including ``date=.../``."""
    return key.rpartition("/")[0] + "/"


def recent_compacted(keys: tuple[str, ...]) -> set[str]:
    """The ``run=compacted-*`` keys of the last ``COMPACTED_WINDOW_DAYS`` partition dates,
    counted back from the newest partition date in ``keys`` (the ones eod may rewrite)."""
    dates = [date.fromisoformat(m.group(1)) for k in keys if (m := _PART_DATE.search(k))]
    if not dates:
        return set()
    cutoff = max(dates) - timedelta(days=COMPACTED_WINDOW_DAYS - 1)
    return {
        k for k in keys if (m := _COMPACTED.search(k)) and date.fromisoformat(m.group(1)) >= cutoff
    }


def load_table(
    store: StorageBackend, table: str, clean_version: str, keys: tuple[str, ...]
) -> pa.Table:
    """``table``'s rows from ``keys``; only keys not cached yet (or of a compacted partition)
    are read from the store. Rows come in ``keys`` order, file by file, like a fresh read."""
    ident = (*_store_id(store), table, clean_version)
    with _lock:
        for other in [i for i in _cache if i[:3] == ident[:3] and i != ident]:
            del _cache[other]  # an older clean version is never read again
        entry = _cache.setdefault(ident, _Entry())
    with entry.lock:
        if entry.table is not None and entry.keys == keys:
            return entry.table
        wanted = set(keys)
        evicted = {partition_of(k) for k in entry.parts if k not in wanted}
        reread = recent_compacted(keys)  # rewritten in place by eod: never trusted across listings
        kept = {
            k: t
            for k, t in entry.parts.items()
            if k in wanted and k not in reread and partition_of(k) not in evicted
        }
        schema = cached_schema(table)
        missing = tuple(k for k in keys if k not in kept)
        kept.update(zip(missing, _read_each(store, schema, missing), strict=True))
        combined = (
            pa.concat_tables([kept[k] for k in keys]).combine_chunks()
            if keys
            else schema.empty_table()
        )
        entry.parts, entry.keys, entry.table = kept, keys, combined
        return combined


def invalidate() -> None:
    """Forget every cached table (compaction rewrites keys in place)."""
    with _lock:
        _cache.clear()


__all__ = [
    "COMPACTED_WINDOW_DAYS",
    "READ_WORKERS",
    "cached_schema",
    "invalidate",
    "load_table",
    "partition_of",
    "read_files",
    "recent_compacted",
]
