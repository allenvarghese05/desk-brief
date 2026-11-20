"""T25c: the append-only tables behind the DuckDB views are read once per process.

T56: loads are incremental (one new file = one read), a compacted partition is evicted and read
again, and loads are single-flight per table.

SYNTHETIC FIXTURE: every observation is made up (``store_fakes.obs_row``).
"""

from __future__ import annotations

import io
import threading
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import sleep
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from store_fakes import make_ctx, obs_row

from deskbrief.config import Config
from deskbrief.schemas import to_frame
from deskbrief.store import table_cache
from deskbrief.store.backend import table_key
from deskbrief.store.compaction import compact_day, compact_partition
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


# ---------------------------------------------------------------------------
# T56: incremental, compaction-safe, single-flight
# ---------------------------------------------------------------------------

D2 = date(2026, 11, 12)


def load(store: LocalBackend) -> pa.Table:
    return table_cache.load_table(
        store, "observations", "v1", table_keys(store, "observations", "v1")
    )


def fresh_load(store: LocalBackend) -> pa.Table:
    """What a process with an empty cache reads (the reference for every incremental load)."""
    return table_cache.read_files(store, "observations", table_keys(store, "observations", "v1"))


def write_on(store: LocalBackend, config: Config, run_id: str, part: date, value: float) -> None:
    as_of = datetime.combine(part, time(5), tzinfo=UTC)  # 00:00 ET, so the row fits `part`
    rows = [obs_row(run_id=run_id, fetch_id=f"F-{run_id}", value=value, as_of=as_of,
                    ingested_at=as_of + timedelta(hours=15))]  # fmt: skip
    ctx = make_ctx(config, store, run_id=run_id)
    Datasets(store).append("observations", to_frame("observations", rows), ctx, part)


def sorted_rows(t: pa.Table) -> list[tuple[Any, ...]]:
    return sorted(tuple(r.values()) for r in t.to_pylist())


def test_one_new_file_downloads_exactly_that_file(store: CountingBackend, config: Config) -> None:
    for i in range(4):
        write(store, config, f"R{i}", value=float(i))
    load(store)
    store.reads.clear()
    write(store, config, "R9", value=9.0)
    new_key = table_key(table="observations", part_date=D, run_id="R9")
    got = load(store)
    assert obs_reads(store) == [new_key]
    assert got.equals(fresh_load(store))  # same rows, same order as a fresh read


def test_unchanged_listing_downloads_nothing_and_returns_the_same_table(
    store: CountingBackend, config: Config
) -> None:
    write(store, config, "R1", value=1.0)
    first = load(store)
    store.reads.clear()
    assert load(store) is first
    assert obs_reads(store) == []


def test_a_compacted_partition_is_evicted_and_read_again(
    store: CountingBackend, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard is another process: compaction's own ``invalidate()`` never reaches it.
    Only the partition whose keys disappeared is read again; other partitions stay cached."""
    import deskbrief.store.compaction as compaction

    monkeypatch.setattr(compaction, "invalidate", lambda: None)  # as seen from another process
    write_on(store, config, "R1", D, 1.0)
    write_on(store, config, "R2", D, 2.0)
    write_on(store, config, "R1", D2, 10.0)
    write_on(store, config, "R2", D2, 20.0)
    load(store)
    compact_partition(store, "observations", D)  # D: R1 + R2 -> run=compacted-2026-11-13
    store.reads.clear()
    got = load(store)
    compacted = table_key(table="observations", part_date=D, run_id=f"compacted-{D}")
    assert obs_reads(store) == [compacted]  # D2's files are not read again
    assert got.num_rows == fresh_load(store).num_rows == 4
    assert sorted_rows(got) == sorted_rows(fresh_load(store))  # no duplicate, no stale row


def test_a_late_file_merged_into_an_existing_compacted_file_is_reread(
    store: CountingBackend, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``compacted-D`` is rewritten in place; the merged late file disappearing evicts D."""
    import deskbrief.store.compaction as compaction

    monkeypatch.setattr(compaction, "invalidate", lambda: None)
    write_on(store, config, "R1", D, 1.0)
    write_on(store, config, "R2", D, 2.0)
    compact_day(store, D, tables=("observations",))
    write_on(store, config, "R3", D, 3.0)
    assert sorted(r["value"] for r in load(store).to_pylist()) == [1.0, 2.0, 3.0]
    compact_day(store, D, tables=("observations",))  # R3 merged into compacted-D, R3 deleted
    got = load(store)
    assert sorted(r["value"] for r in got.to_pylist()) == [1.0, 2.0, 3.0]
    assert sorted_rows(got) == sorted_rows(fresh_load(store))


def test_compaction_in_this_process_still_invalidates_everything(
    store: CountingBackend, config: Config
) -> None:
    write_on(store, config, "R1", D, 1.0)
    write_on(store, config, "R2", D, 2.0)
    write_on(store, config, "R1", D2, 10.0)
    load(store)
    compact_partition(store, "observations", D)
    assert table_cache._cache == {}


class SlowBackend(CountingBackend):
    """Reads take a while, so two loads started together overlap."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._reads_lock = threading.Lock()

    def get_bytes(self, key: str) -> bytes:
        with self._reads_lock:
            self.reads.append(key)
        sleep(0.02)
        return LocalBackend.get_bytes(self, key)


def test_two_concurrent_loads_download_each_key_once(tmp_path: Path, config: Config) -> None:
    store = SlowBackend(tmp_path / "data")
    for i in range(6):
        write(store, config, f"R{i}", value=float(i))
    store.reads.clear()
    start = threading.Barrier(2)
    results: list[pa.Table] = []

    def worker() -> None:
        start.wait()
        results.append(load(store))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    reads = obs_reads(store)
    assert len(reads) == 6
    assert len(set(reads)) == 6  # every key once, not once per caller
    assert results[0] is results[1]


def test_existing_signatures_are_kept() -> None:
    """``store.compaction`` and ``duck.connect`` (outside T56's Owns) call these."""
    import inspect

    assert list(inspect.signature(table_cache.invalidate).parameters) == []
    assert list(inspect.signature(table_cache.load_table).parameters) == [
        "store", "table", "clean_version", "keys",
    ]  # fmt: skip
    assert list(inspect.signature(table_cache.read_files).parameters) == ["store", "table", "keys"]
