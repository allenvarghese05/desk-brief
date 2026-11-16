"""Storage contract: backend protocol and the storage key layout (spec §6).

CONTRACT FILE. Changing anything here needs a labeled ``contract:`` PR.

Every object written by the pipeline goes through one of the key builders below, so the
layout lives in exactly one place. Backends (local filesystem, Cloudflare R2) only move bytes.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from deskbrief.timeutil import ensure_utc


class StorageError(Exception):
    """Base class for storage failures."""


class KeyExistsError(StorageError):
    """Raised when writing to an existing key without ``overwrite=True`` (raw is write-once)."""


class KeyNotFoundError(StorageError):
    """Raised when reading a key that does not exist."""


@runtime_checkable
class StorageBackend(Protocol):
    """Byte store addressed by ``/``-separated keys relative to the bucket/root."""

    def put_bytes(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
        """Write ``data``. Must raise ``KeyExistsError`` if the key exists and not overwrite."""
        ...

    def get_bytes(self, key: str) -> bytes:
        """Read a key. Must raise ``KeyNotFoundError`` if missing."""
        ...

    def exists(self, key: str) -> bool: ...

    def list(self, prefix: str) -> list[str]:
        """All keys starting with ``prefix``, sorted ascending."""
        ...

    def delete(self, key: str) -> None:
        """Delete a key. Deleting a missing key is a no-op. Never used on ``raw/``."""
        ...


# ---------------------------------------------------------------------------
# Key layout (spec §6)
# ---------------------------------------------------------------------------

RAW_PREFIX = "raw"
CLEAN_PREFIX = "clean"
LOGS_PREFIX = "logs"
DERIVED_PREFIX = "derived"
SERVING_PREFIX = "serving"
REPORTS_PREFIX = "reports"
CLEAN_CURRENT_KEY = f"{CLEAN_PREFIX}/_CURRENT"

_SEGMENT = re.compile(r"^[A-Za-z0-9_.\-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: Where each table lives. Clean tables are under the current clean version.
TABLE_LAYER: dict[str, str] = {
    "observations": CLEAN_PREFIX,
    "quarantine": CLEAN_PREFIX,
    "source_log": LOGS_PREFIX,
    "dq_results": DERIVED_PREFIX,
    "crosscheck_results": DERIVED_PREFIX,
    "revisions": DERIVED_PREFIX,
    "alerts": DERIVED_PREFIX,
    "runs": DERIVED_PREFIX,
}


def _seg(value: str, what: str) -> str:
    if not _SEGMENT.match(value):
        raise ValueError(f"invalid {what} key segment: {value!r}")
    return value


def raw_key(
    *, source: str, dataset: str, part_date: date, ingested_at: datetime, sha256: str, ext: str
) -> str:
    """Key for an immutable raw provider response.

    ``raw/source={src}/dataset={ds}/date={YYYY-MM-DD}/{ingested_at:%Y%m%dT%H%M%S%fZ}_{sha[:12]}.{ext}``
    """
    if not _SHA256.match(sha256):
        raise ValueError("sha256 must be 64 lowercase hex chars")
    stamp = ensure_utc(ingested_at).strftime("%Y%m%dT%H%M%S%fZ")
    return (
        f"{RAW_PREFIX}/source={_seg(source, 'source')}/dataset={_seg(dataset, 'dataset')}"
        f"/date={part_date.isoformat()}/{stamp}_{sha256[:12]}.{_seg(ext, 'ext')}"
    )


def raw_meta_key(raw: str) -> str:
    """Sidecar metadata key for a raw object key."""
    if not raw.startswith(f"{RAW_PREFIX}/"):
        raise ValueError(f"not a raw key: {raw!r}")
    return f"{raw}.meta.json"


def table_key(*, table: str, part_date: date, run_id: str, clean_version: str = "v1") -> str:
    """Key for one run's Parquet file of a table, partitioned by date.

    Clean tables: ``clean/{version}/{table}/date={d}/run={run_id}.parquet``;
    others: ``{layer}/{table}/date={d}/run={run_id}.parquet``.
    """
    layer = TABLE_LAYER.get(table)
    if layer is None:
        raise ValueError(f"unknown table {table!r}; known: {sorted(TABLE_LAYER)}")
    tail = f"{table}/date={part_date.isoformat()}/run={_seg(run_id, 'run_id')}.parquet"
    if layer == CLEAN_PREFIX:
        return f"{CLEAN_PREFIX}/{_seg(clean_version, 'clean_version')}/{tail}"
    return f"{layer}/{tail}"


def table_prefix(table: str, clean_version: str = "v1") -> str:
    """Prefix under which all partitions of ``table`` live (for globbing / listing)."""
    layer = TABLE_LAYER.get(table)
    if layer is None:
        raise ValueError(f"unknown table {table!r}")
    if layer == CLEAN_PREFIX:
        return f"{CLEAN_PREFIX}/{_seg(clean_version, 'clean_version')}/{table}/"
    return f"{layer}/{table}/"


def scorecard_key(part_date: date) -> str:
    return f"{DERIVED_PREFIX}/scorecard_daily/date={part_date.isoformat()}.parquet"


def budget_key(source: str, part_date: date) -> str:
    return f"{LOGS_PREFIX}/budget/{_seg(source, 'source')}/date={part_date.isoformat()}.json"


def serving_key(name: str) -> str:
    """e.g. ``serving_key("latest_snapshot.parquet")``."""
    return f"{SERVING_PREFIX}/{_seg(name, 'serving name')}"


def report_key(report_date: date, version: int, filename: str) -> str:
    if version < 1:
        raise ValueError("report version starts at 1")
    return f"{REPORTS_PREFIX}/date={report_date.isoformat()}/v{version}/{_seg(filename, 'file')}"
