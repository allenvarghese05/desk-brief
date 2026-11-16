"""Datasets: validated Parquet append/read on a backend (spec §6, §7.2).

SYNTHETIC FIXTURE: all rows are made up.
"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime

import pandas as pd
import pyarrow.parquet as pq
import pytest
from pandera.errors import SchemaErrors
from store_fakes import make_ctx, obs_row

from deskbrief.config import Config
from deskbrief.schemas import empty_frame, to_frame, validate
from deskbrief.store.backend import KeyExistsError
from deskbrief.store.datasets import Datasets, arrow_schema, current_clean_version
from deskbrief.store.local import LocalBackend

D = date(2026, 11, 13)


def test_append_then_read_round_trip(local: LocalBackend, config: Config) -> None:
    ctx = make_ctx(config, local, run_id="RUN1")
    ds = Datasets(local)
    df = to_frame(
        "observations",
        [obs_row(value=100.5), obs_row(instrument_id="QQQ", symbol="qqq.us", value=200.25)],
    )
    key = ds.append("observations", df, ctx, D)
    assert key == "clean/v1/observations/date=2026-11-13/run=RUN1.parquet"
    back = ds.read("observations")
    validate("observations", back)  # round trip still satisfies the contract
    assert sorted(back["value"]) == [100.5, 200.25]
    assert back["as_of"].iloc[0] == datetime(2026, 11, 13, 5, 0, tzinfo=UTC)
    assert list(back.columns) == list(df.columns)


def test_parquet_types_come_from_declared_schema_even_when_empty(
    local: LocalBackend, config: Config
) -> None:
    ctx = make_ctx(config, local, run_id="RUN1")
    key = Datasets(local).append("source_log", empty_frame("source_log"), ctx, D)
    written = pq.read_table(io.BytesIO(local.get_bytes(key))).schema
    assert written.equals(arrow_schema("source_log"))
    assert str(written.field("http_status").type) == "int64"  # not "null"
    assert str(written.field("requested_at").type) == "timestamp[us, tz=UTC]"


def test_nullable_columns_round_trip(local: LocalBackend, config: Config) -> None:
    ctx = make_ctx(config, local, run_id="RUN1")
    df = to_frame(
        "quarantine",
        [
            {
                "fetch_id": "F1",
                "run_id": "RUN1",
                "source": "yahoo",
                "dataset": "quote",
                "reason": "bad json",
                "detected_at": datetime(2026, 11, 13, 15, 0, tzinfo=UTC),
            }
        ],
    )
    ds = Datasets(local)
    ds.append("quarantine", df, ctx, D)
    back = ds.read("quarantine")
    validate("quarantine", back)
    assert back["detail"].isna().all()


def test_invalid_frame_is_rejected_and_nothing_written(local: LocalBackend, config: Config) -> None:
    ctx = make_ctx(config, local, run_id="RUN1")
    bad = to_frame("observations", [obs_row()])
    bad["tier"] = 9  # INJECTED FAULT: tier must be 1, 2 or 3
    with pytest.raises(SchemaErrors):
        Datasets(local).append("observations", bad, ctx, D)
    assert local.list("") == []


def test_naive_timestamps_rejected(local: LocalBackend, config: Config) -> None:
    ctx = make_ctx(config, local, run_id="RUN1")
    bad = to_frame("observations", [obs_row()])
    bad["ingested_at"] = bad["ingested_at"].dt.tz_localize(None)  # INJECTED FAULT
    with pytest.raises(SchemaErrors):
        Datasets(local).append("observations", bad, ctx, D)


def test_write_once_per_run_table_and_date(local: LocalBackend, config: Config) -> None:
    ctx = make_ctx(config, local, run_id="RUN1")
    ds = Datasets(local)
    df = to_frame("observations", [obs_row()])
    ds.append("observations", df, ctx, D)
    with pytest.raises(KeyExistsError):
        ds.append("observations", df, ctx, D)
    with pytest.raises(ValueError, match="overwrite is only allowed for daily tables"):
        ds.append("observations", df, ctx, D, overwrite=True)
    ds.append("observations", df, make_ctx(config, local, run_id="RUN2"), D)  # new run is fine
    assert len(ds.read("observations")) == 2


def test_unknown_and_unstored_tables_rejected(local: LocalBackend, config: Config) -> None:
    ctx = make_ctx(config, local)
    ds = Datasets(local)
    with pytest.raises(ValueError, match="unknown table"):
        ds.append("parsed_observations", empty_frame("parsed_observations"), ctx, D)
    with pytest.raises(ValueError, match="unknown table"):
        ds.read("nope")


def test_read_filters_by_partition_date_and_spans_runs(local: LocalBackend, config: Config) -> None:
    ds = Datasets(local)
    d1, d2 = date(2026, 11, 12), date(2026, 11, 13)
    ds.append(
        "observations",
        to_frame("observations", [obs_row(value=1.0, as_of=datetime(2026, 11, 12, 5, tzinfo=UTC))]),
        make_ctx(config, local, run_id="A"),
        d1,
    )
    ds.append(
        "observations",
        to_frame("observations", [obs_row(value=2.0)]),
        make_ctx(config, local, run_id="B"),
        d2,
    )
    ds.append(
        "observations",
        to_frame("observations", [obs_row(value=3.0)]),
        make_ctx(config, local, run_id="C"),
        d2,
    )
    assert sorted(ds.read("observations")["value"]) == [1.0, 2.0, 3.0]
    assert list(ds.read("observations", part_date=d1)["value"]) == [1.0]
    assert sorted(ds.read("observations", part_date=d2)["value"]) == [2.0, 3.0]
    assert ds.read("observations", part_date=date(2030, 1, 1)).empty


def test_read_empty_table_returns_typed_empty_frame(local: LocalBackend) -> None:
    out = Datasets(local).read("runs")
    assert out.empty
    assert list(out.columns) == list(empty_frame("runs").columns)
    validate("runs", out)


def test_non_clean_tables_ignore_clean_version(local: LocalBackend, config: Config) -> None:
    local.put_bytes("clean/_CURRENT", b"v2\n")
    ctx = make_ctx(config, local, run_id="R9")
    ds = Datasets(local)
    runs = to_frame(
        "runs",
        [
            {
                "run_id": "R9",
                "job": "morning",
                "trigger": "manual",
                "target_date": D,
                "started_at": datetime(2026, 11, 13, 11, 0, tzinfo=UTC),
                "status": "ok",
                "git_sha": "abc",
                "config_hash": "def",
                "n_fetch": 1,
                "n_fetch_failed": 0,
                "n_obs": 10,
                "n_dq_fail": 0,
                "n_dq_warn": 0,
            }
        ],
    )
    assert ds.append("runs", runs, ctx, D) == "derived/runs/date=2026-11-13/run=R9.parquet"
    obs_key = ds.append("observations", to_frame("observations", [obs_row()]), ctx, D)
    assert obs_key == "clean/v2/observations/date=2026-11-13/run=R9.parquet"


def test_clean_current_pointer(local: LocalBackend) -> None:
    assert current_clean_version(local) == "v1"  # no pointer yet
    local.put_bytes("clean/_CURRENT", b"v7\n")
    assert current_clean_version(local) == "v7"
    local.put_bytes("clean/_CURRENT", b"  \n", overwrite=True)
    assert current_clean_version(local) == "v1"  # blank pointer falls back


def test_read_follows_current_pointer(local: LocalBackend, config: Config) -> None:
    ds = Datasets(local)
    ds.append(
        "observations",
        to_frame("observations", [obs_row(value=1.0)]),
        make_ctx(config, local, run_id="A"),
        D,
    )
    local.put_bytes("clean/_CURRENT", b"v2")
    assert ds.clean_version() == "v1"  # resolved once per instance, not re-read on every call
    assert ds.refresh_clean_version() == "v2"
    assert ds.read("observations").empty  # v2 has no data yet
    ds.append(
        "observations",
        to_frame("observations", [obs_row(value=9.0)]),
        make_ctx(config, local, run_id="B"),
        D,
    )
    assert list(ds.read("observations")["value"]) == [9.0]


def test_scorecard_is_one_overwritten_file_per_date(local: LocalBackend, config: Config) -> None:
    ds = Datasets(local)

    def card(fetches: int) -> pd.DataFrame:
        return to_frame(
            "scorecard_daily",
            [
                {
                    "source": "yahoo",
                    "date": D,
                    "fetches": fetches,
                    "ok_rate": 1.0,
                    "error_rate": 0.0,
                    "n_crosschecks": 0,
                }
            ],
        )

    k1 = ds.append("scorecard_daily", card(5), make_ctx(config, local, run_id="A"), D)
    k2 = ds.append("scorecard_daily", card(8), make_ctx(config, local, run_id="B"), D)
    assert k1 == k2 == "derived/scorecard_daily/date=2026-11-13.parquet"
    out = ds.read("scorecard_daily")
    assert list(out["fetches"]) == [8]
    assert ds.read("scorecard_daily", part_date=D).shape[0] == 1
    assert ds.read("scorecard_daily", part_date=date(2026, 11, 12)).empty
