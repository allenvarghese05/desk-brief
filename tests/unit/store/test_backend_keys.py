from datetime import UTC, date, datetime

import pytest

from deskbrief.schemas import TABLE_SCHEMAS
from deskbrief.store.backend import (
    CLEAN_CURRENT_KEY,
    TABLE_LAYER,
    KeyExistsError,
    KeyNotFoundError,
    StorageBackend,
    StorageError,
    budget_key,
    raw_key,
    raw_meta_key,
    report_key,
    scorecard_key,
    serving_key,
    table_key,
    table_prefix,
)

SHA = "a" * 64
D = date(2026, 11, 16)
T = datetime(2026, 11, 16, 11, 15, 3, 123456, tzinfo=UTC)


def test_raw_key_layout() -> None:
    k = raw_key(source="fred", dataset="series", ingested_at=T, sha256=SHA, ext="json")
    assert (
        k
        == "raw/source=fred/dataset=series/date=2026-11-16/20261116T111503123456Z_aaaaaaaaaaaa.json"
    )
    assert raw_meta_key(k) == k + ".meta.json"


def test_raw_key_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="sha256"):
        raw_key(source="fred", dataset="s", ingested_at=T, sha256="xyz", ext="json")
    with pytest.raises(ValueError, match="source"):
        raw_key(source="../etc", dataset="s", ingested_at=T, sha256=SHA, ext="json")
    with pytest.raises(ValueError, match="naive"):
        raw_key(
            source="fred",
            dataset="s",
            ingested_at=T.replace(tzinfo=None),
            sha256=SHA,
            ext="json",
        )
    with pytest.raises(ValueError, match="raw key"):
        raw_meta_key("clean/x")


def test_table_keys() -> None:
    assert (
        table_key(table="observations", part_date=D, run_id="R1")
        == "clean/v1/observations/date=2026-11-16/run=R1.parquet"
    )
    assert (
        table_key(table="observations", part_date=D, run_id="R1", clean_version="v2")
        == "clean/v2/observations/date=2026-11-16/run=R1.parquet"
    )
    assert table_key(table="source_log", part_date=D, run_id="R1").startswith("logs/source_log/")
    assert table_key(table="dq_results", part_date=D, run_id="R1").startswith("derived/")
    assert table_prefix("observations") == "clean/v1/observations/"
    assert table_prefix("runs") == "derived/runs/"
    with pytest.raises(ValueError, match="unknown table"):
        table_key(table="nope", part_date=D, run_id="R1")
    with pytest.raises(ValueError, match="unknown table"):
        table_prefix("nope")


def test_every_layered_table_has_a_schema() -> None:
    assert set(TABLE_LAYER) <= set(TABLE_SCHEMAS)


def test_misc_keys() -> None:
    assert scorecard_key(D) == "derived/scorecard_daily/date=2026-11-16.parquet"
    assert budget_key("twelvedata", D) == "logs/budget/twelvedata/date=2026-11-16.json"
    assert serving_key("health.json") == "serving/health.json"
    assert report_key(D, 2, "brief.html") == "reports/date=2026-11-16/v2/brief.html"
    assert CLEAN_CURRENT_KEY == "clean/_CURRENT"
    with pytest.raises(ValueError, match="version"):
        report_key(D, 0, "brief.html")


def test_errors_hierarchy() -> None:
    assert issubclass(KeyExistsError, StorageError)
    assert issubclass(KeyNotFoundError, StorageError)


class _Mem:
    """Minimal in-memory backend proving the protocol is implementable (tests only)."""

    def __init__(self) -> None:
        self.d: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
        if key in self.d and not overwrite:
            raise KeyExistsError(key)
        self.d[key] = data

    def get_bytes(self, key: str) -> bytes:
        try:
            return self.d[key]
        except KeyError as exc:
            raise KeyNotFoundError(key) from exc

    def exists(self, key: str) -> bool:
        return key in self.d

    def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self.d if k.startswith(prefix))

    def delete(self, key: str) -> None:
        self.d.pop(key, None)


def test_protocol_is_runtime_checkable() -> None:
    mem = _Mem()
    assert isinstance(mem, StorageBackend)
    mem.put_bytes("raw/a", b"1")
    with pytest.raises(KeyExistsError):
        mem.put_bytes("raw/a", b"2")
    assert mem.get_bytes("raw/a") == b"1"
    with pytest.raises(KeyNotFoundError):
        mem.get_bytes("raw/b")


def test_raw_partition_is_utc_date_of_ingest() -> None:
    late = datetime(2026, 11, 17, 2, 0, tzinfo=UTC)  # still Nov 16 in New York
    k = raw_key(source="ecb", dataset="eurofxref", ingested_at=late, sha256=SHA, ext="csv")
    assert "/date=2026-11-17/" in k


@pytest.mark.parametrize("bad", [".", "..", "...", "a/b", ""])
def test_traversal_segments_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="segment"):
        serving_key(bad)
    with pytest.raises(ValueError, match="segment"):
        report_key(D, 1, bad)


def test_datetime_is_not_a_partition_date() -> None:
    with pytest.raises(TypeError, match="date"):
        table_key(table="runs", part_date=T, run_id="R")
    with pytest.raises(TypeError):
        scorecard_key(T)
    with pytest.raises(TypeError):
        budget_key("x", T)
    with pytest.raises(TypeError):
        report_key(T, 1, "brief.html")


def test_scorecard_prefix() -> None:
    assert table_prefix("scorecard_daily") == "derived/scorecard_daily/"
    assert scorecard_key(D).startswith(table_prefix("scorecard_daily"))
