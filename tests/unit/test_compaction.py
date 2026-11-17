"""T23: compaction of a day's clean files (spec §6): verified, lossless, re-runnable.

SYNTHETIC FIXTURE: rows come from ``t23_synth`` (made up).
INJECTED FAULT: a backend that stores a truncated compacted file.
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from t23_synth import D, ctx_for, obs_row, put

from deskbrief.store.compaction import CompactionError, compact_day, compact_partition
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_to_utc

AS_OF = et_to_utc(D, "00:00")
DIR = f"clean/v1/observations/date={D.isoformat()}"


def keys(store: LocalBackend) -> list[str]:
    return [k for k in store.list(f"{DIR}/") if k.endswith(".parquet")]


def world(tmp_path: Path) -> LocalBackend:
    store = LocalBackend(tmp_path / "data")
    same = obs_row("SPY", 100.0, AS_OF, "R1", "F1")
    put(store, "observations", [same], "R1", D)
    # a later run re-ingests the same fetch (exact duplicate) and a restated value (a new version)
    put(store, "observations", [same | {"run_id": "R2"}], "R2", D)
    put(store, "observations", [obs_row("SPY", 101.0, AS_OF, "R3", "F3")], "R3", D)
    # a rebuild file: named for the rebuild run and month, rows keep the original run_id
    put(store, "observations", [same], "RB-2026-11", D)
    return store


def test_compacts_dedups_exact_rows_and_keeps_every_distinct_version(tmp_path: Path) -> None:
    store = world(tmp_path)
    (res,) = [r for r in compact_day(store, D) if r.table == "observations"]
    assert (res.inputs, res.rows_in, res.rows_out, res.deleted) == (4, 4, 2, 4)
    assert keys(store) == [f"{DIR}/run=compacted-2026-11-16.parquet"]
    df = Datasets(store).read("observations")
    assert sorted(df["value"]) == [100.0, 101.0]  # both versions of the key survive
    assert set(df["fetch_id"]) == {"F1", "F3"}


def test_raw_is_never_touched_and_a_rerun_changes_nothing(tmp_path: Path) -> None:
    store = world(tmp_path)
    store.put_bytes("raw/source=x/dataset=y/date=2026-11-16/a.json", b"{}")
    compact_day(store, D)
    before = store.get_bytes(keys(store)[0])
    again = [r for r in compact_day(store, D) if r.table == "observations"]
    assert again[0].skipped
    assert again[0].deleted == 0
    assert store.get_bytes(keys(store)[0]) == before
    assert store.exists("raw/source=x/dataset=y/date=2026-11-16/a.json")


def test_late_file_is_merged_into_the_existing_compacted_file(tmp_path: Path) -> None:
    store = world(tmp_path)
    compact_day(store, D)
    put(store, "observations", [obs_row("QQQ", 5.0, AS_OF, "R9", "F9")], "R9", D)
    (res,) = [r for r in compact_day(store, D) if r.table == "observations"]
    assert (res.rows_in, res.rows_out, res.deleted) == (3, 3, 1)  # old compacted file + 1 late
    assert len(keys(store)) == 1


class Truncating(LocalBackend):
    """INJECTED FAULT: the first compacted file lands with its last row missing."""

    fired = False

    def put_bytes(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
        if "compacted" in key and not self.fired:
            self.fired = True
            table = pq.read_table(io.BytesIO(data))
            sink = io.BytesIO()
            pq.write_table(table.slice(0, table.num_rows - 1), sink)
            data = sink.getvalue()
        super().put_bytes(key, data, overwrite=overwrite)


def test_count_mismatch_deletes_nothing_and_removes_the_bad_file(tmp_path: Path) -> None:
    good = world(tmp_path)
    store = Truncating(good.root)
    before = keys(store)
    with pytest.raises(CompactionError, match="row count"):
        compact_partition(store, "observations", D)
    assert keys(store) == before  # every input is still there, no bad compacted file


def test_count_mismatch_restores_an_earlier_compacted_file(tmp_path: Path) -> None:
    store = world(tmp_path)
    compact_day(store, D)
    (compacted,) = keys(store)
    good = store.get_bytes(compacted)
    put(store, "observations", [obs_row("QQQ", 5.0, AS_OF, "R9", "F9")], "R9", D)
    with pytest.raises(CompactionError):
        compact_partition(Truncating(store.root), "observations", D)
    assert store.get_bytes(compacted) == good
    assert len(keys(store)) == 2


class CrashingDeletes(LocalBackend):
    """INJECTED FAULT: the process dies after the first input has been deleted."""

    def delete(self, key: str) -> None:
        if getattr(self, "deleted", 0) >= 1:
            raise OSError("process killed")
        self.deleted = getattr(self, "deleted", 0) + 1
        super().delete(key)


def test_a_crash_between_the_write_and_the_deletes_loses_and_corrupts_nothing(
    tmp_path: Path,
) -> None:
    good = world(tmp_path)
    store = CrashingDeletes(good.root)
    with pytest.raises(OSError, match="process killed"):
        compact_partition(store, "observations", D)
    # the compacted file is complete and verified before any delete, so no distinct row is missing;
    # leftover inputs hold exact copies, which the obs_latest view (one row per key) hides
    mid = Datasets(store).read("observations")
    assert {(r.value, r.fetch_id) for r in mid.itertuples()} == {(100.0, "F1"), (101.0, "F3")}
    conn = connect(store, ctx_for(store, "X").config)
    assert conn.execute("SELECT count(*) FROM obs_latest").fetchone() == (1,)  # newest version
    # the next run merges the compacted file with the leftovers and finishes the deletes
    (res,) = [r for r in compact_day(good, D) if r.table == "observations"]
    assert res.rows_out == 2
    assert keys(good) == [f"{DIR}/run=compacted-2026-11-16.parquet"]
    assert len(Datasets(good).read("observations")) == 2  # not duplicated once recovered


def test_other_dates_are_left_alone(tmp_path: Path) -> None:
    store = world(tmp_path)
    other = D + timedelta(days=1)
    row = obs_row("SPY", 9.0, et_to_utc(other, "00:00"), "R4", "F4")
    put(store, "observations", [row], "R4", other)
    compact_day(store, D)
    assert store.exists(f"clean/v1/observations/date={other.isoformat()}/run=R4.parquet")
