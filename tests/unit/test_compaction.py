"""T23: compaction of a day's clean files (spec §6): verified, lossless, re-runnable.

SYNTHETIC FIXTURE: rows come from ``t23_synth`` (made up).
INJECTED FAULT: a backend that stores a truncated compacted file.
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from t23_synth import D, ctx_for, obs_row, put

from deskbrief.store.compaction import (
    CompactionError,
    compact_day,
    compact_partition,
    compact_recent,
)
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
    assert len(mid) == 5  # 2 compacted rows + 3 exact copies in the leftover inputs: documented
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


@pytest.mark.parametrize(
    "change",
    [{"fetch_id": "F2"}, {"ingested_at": AS_OF + timedelta(hours=21)}, {"parser_version": "v2"}],
    ids=["fetch_id", "ingested_at", "parser_version"],
)
def test_rows_differing_in_any_version_column_are_kept(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    store = LocalBackend(tmp_path / "data")
    base = obs_row("SPY", 100.0, AS_OF, "R1", "F1")  # same value throughout
    put(store, "observations", [base], "R1", D)
    put(store, "observations", [base | change | {"run_id": "R2"}], "R2", D)
    (res,) = [r for r in compact_day(store, D) if r.table == "observations"]
    assert (res.rows_in, res.rows_out) == (2, 2)  # two versions of one key, neither is dropped


def test_rows_differing_only_in_run_id_collapse_to_one(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path / "data")
    base = obs_row("SPY", 100.0, AS_OF, "R1", "F1")
    put(store, "observations", [base], "R1", D)
    put(store, "observations", [base | {"run_id": "R2"}], "R2", D)
    (res,) = [r for r in compact_day(store, D) if r.table == "observations"]
    assert (res.rows_in, res.rows_out) == (2, 1)


def quarantine_row(**kw: Any) -> dict[str, Any]:
    return {
        "fetch_id": "F1", "run_id": "R1", "source": "stooq", "dataset": "daily",
        "reason": "synthetic parse error", "detail": None, "raw_key": None,
        "detected_at": et_to_utc(D, "10:00"),
    } | kw  # fmt: skip


def test_quarantine_rows_with_nulls_survive_the_round_trip(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path / "data")
    put(store, "quarantine", [quarantine_row()], "R1", D)
    two = [quarantine_row(run_id="R2"), quarantine_row(fetch_id="F2")]
    put(store, "quarantine", two, "R2", D)
    res = {r.table: r for r in compact_day(store, D)}["quarantine"]
    assert (res.rows_in, res.rows_out) == (3, 2)
    df = Datasets(store).read("quarantine")
    assert sorted(df["fetch_id"]) == ["F1", "F2"]
    assert df["detail"].isna().all()
    assert df["raw_key"].isna().all()


def test_an_input_with_unexpected_columns_is_never_compacted_or_deleted(tmp_path: Path) -> None:
    store = world(tmp_path)
    sink = io.BytesIO()
    pq.write_table(pa.table({"surprise": [1]}), sink)
    store.put_bytes(f"{DIR}/run=ODD.parquet", sink.getvalue())
    before = keys(store)
    with pytest.raises(CompactionError, match="differ from the declared"):
        compact_partition(store, "observations", D)
    assert keys(store) == before


def test_one_failing_table_does_not_stop_the_next_and_is_reported(tmp_path: Path) -> None:
    store = world(tmp_path)
    store.put_bytes(f"{DIR}/run=ODD.parquet", b"not parquet")  # observations cannot be read
    put(store, "quarantine", [quarantine_row()], "R1", D)
    put(store, "quarantine", [quarantine_row(run_id="R2")], "R2", D)
    with pytest.raises(CompactionError, match="observations 2026-11-16"):
        compact_day(store, D)
    qdir = f"clean/v1/quarantine/date={D.isoformat()}/"
    assert store.list(qdir) == [f"{qdir}run=compacted-2026-11-16.parquet"]


def test_recent_days_are_recompacted_when_a_later_run_adds_a_file(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path / "data")
    eve = D - timedelta(days=1)
    as_of = et_to_utc(eve, "00:00")
    put(store, "observations", [obs_row("SPY", 1.0, as_of, "R1", "F1")], "R1", eve)
    put(store, "observations", [obs_row("QQQ", 2.0, as_of, "R2", "F2")], "R2", eve)
    compact_recent(store, D)  # the eod run
    day = f"clean/v1/observations/date={eve.isoformat()}/"
    assert store.list(day) == [f"{day}run=compacted-{eve.isoformat()}.parquet"]
    # the next morning's run writes into D-1 again; the next eod compacts it too
    put(store, "observations", [obs_row("IWM", 3.0, as_of, "RM", "FM")], "RM", eve)
    assert len(store.list(day)) == 2
    compact_recent(store, D)
    assert store.list(day) == [f"{day}run=compacted-{eve.isoformat()}.parquet"]
    assert len(Datasets(store).read("observations")) == 3


def test_recent_window_is_seven_days_and_isolates_each_date(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path / "data")
    for back in (0, 1, 7):  # today, yesterday, and one day outside the window
        day = D - timedelta(days=back)
        for n in (1, 2):
            row = obs_row("SPY", float(n), et_to_utc(day, "00:00"), f"R{back}{n}", f"F{back}{n}")
            put(store, "observations", [row], f"R{back}{n}", day)

    def files(back: int) -> int:
        day = (D - timedelta(days=back)).isoformat()
        return len(store.list(f"clean/v1/observations/date={day}/"))

    with pytest.raises(CompactionError, match=f"observations {D.isoformat()}"):
        compact_recent(Truncating(store.root), D)  # the first compaction (today's) fails
    assert (files(0), files(1), files(7)) == (2, 1, 2)  # yesterday still compacted; day 7 left
