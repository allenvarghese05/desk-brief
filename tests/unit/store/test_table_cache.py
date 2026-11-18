"""T25c: the append-only tables behind the DuckDB views are read once per process.

SYNTHETIC FIXTURE: every observation is made up (``store_fakes.obs_row``).
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from store_fakes import make_ctx, obs_row

from deskbrief.config import Config
from deskbrief.schemas import to_frame
from deskbrief.store import table_cache
from deskbrief.store.backend import table_key
from deskbrief.store.compaction import compact_partition
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect, table_keys
from deskbrief.store.local import LocalBackend

D = date(2026, 11, 13)


class CountingBackend(LocalBackend):
    """LocalBackend that counts file reads (the R2 GETs the cache is there to avoid)."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.reads: list[str] = []

    def get_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        return super().get_bytes(key)


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    table_cache.invalidate()


@pytest.fixture
def store(tmp_path: Path) -> CountingBackend:
    return CountingBackend(tmp_path / "data")


def write(store: LocalBackend, config: Config, run_id: str, **over: Any) -> None:
    rows = [obs_row(run_id=run_id, fetch_id=f"F-{run_id}", **over)]
    Datasets(store).append(
        "observations", to_frame("observations", rows), make_ctx(config, store, run_id=run_id), D
    )


def obs_reads(store: CountingBackend) -> list[str]:
    return [k for k in store.reads if "/observations/" in k]


def values(store: LocalBackend, config: Config) -> list[float]:
    conn = connect(store, config)
    try:
        return [r[0] for r in conn.execute("SELECT value FROM obs_all ORDER BY value").fetchall()]
    finally:
        conn.close()


def test_a_second_connect_reads_no_file_again(store: CountingBackend, config: Config) -> None:
    for i in range(5):
        write(store, config, f"R{i}", value=float(i))
    assert values(store, config) == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert len(obs_reads(store)) == 5
    store.reads.clear()
    for _ in range(3):  # checks, alerts, serving, report each connect once
        assert values(store, config) == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert obs_reads(store) == []


def test_a_new_file_is_seen_by_the_next_connect(store: CountingBackend, config: Config) -> None:
    write(store, config, "R1", value=1.0)
    conn = connect(store, config)  # bound at connect, like the glob views (module docstring)
    write(store, config, "R2", value=2.0)
    assert [r[0] for r in conn.execute("SELECT value FROM obs_all").fetchall()] == [1.0]
    assert values(store, config) == [1.0, 2.0]


def test_compaction_is_seen_even_when_the_key_set_returns_to_an_old_listing(
    store: CountingBackend, config: Config
) -> None:
    """Listing {C} cached; a file Y is written with no read in between; compaction merges Y into
    C. The listing is {C} again but C changed: compaction must invalidate the cache."""
    write(store, config, "R1", value=1.0)
    write(store, config, "R2", value=2.0)
    compact_partition(store, "observations", D)  # -> only run=compacted-2026-11-13
    assert values(store, config) == [1.0, 2.0]
    keys_before = table_keys(store, "observations", "v1")
    write(store, config, "R3", value=3.0)  # nobody connects before the next compaction
    compact_partition(store, "observations", D)
    assert table_keys(store, "observations", "v1") == keys_before
    assert values(store, config) == [1.0, 2.0, 3.0]


def test_only_run_files_are_read(store: CountingBackend, config: Config) -> None:
    write(store, config, "R1", value=1.0)
    store.put_bytes("clean/v1/observations/date=2026-11-13/notes.txt", b"not parquet")
    store.put_bytes("clean/v1/observations/stray.parquet", b"not under a date partition")
    assert values(store, config) == [1.0]


def test_an_older_file_without_a_column_reads_as_null(
    store: CountingBackend, config: Config
) -> None:
    """Same as the old ``read_parquet(..., union_by_name = true)``."""
    write(store, config, "R1", value=1.0)
    frame = to_frame("observations", [obs_row(run_id="R0", fetch_id="F0", value=0.5)])
    old = pa.Table.from_pandas(frame, preserve_index=False).drop_columns(["parser_version"])
    sink = io.BytesIO()
    pq.write_table(old, sink)
    store.put_bytes(table_key(table="observations", part_date=D, run_id="R0"), sink.getvalue())
    conn = connect(store, config)
    got = conn.execute("SELECT value, parser_version FROM obs_all ORDER BY value").fetchall()
    assert got == [(0.5, None), (1.0, "t1")]


def test_two_stores_never_share_an_entry(tmp_path: Path, config: Config) -> None:
    a, b = LocalBackend(tmp_path / "a"), LocalBackend(tmp_path / "b")
    write(a, config, "R1", value=1.0)
    write(b, config, "R1", value=2.0)
    assert values(a, config) == [1.0]
    assert values(b, config) == [2.0]


def test_scorecard_daily_is_not_cached(store: CountingBackend, config: Config) -> None:
    """Overwritten in place (P-008 DAILY_TABLES): its view still reads the files."""
    write(store, config, "R1", value=1.0)
    connect(store, config).close()
    cached = {ident[2] for ident in table_cache._cache}
    assert "observations" in cached
    assert "scorecard_daily" not in cached
