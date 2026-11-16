"""T05 hardening: partition membership (P-006), overwrite rules, raw immutability on R2,
R2 checksum config, per-instance ``_CURRENT`` caching, DuckDB glob escaping.

SYNTHETIC FIXTURE: all rows are made up.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from store_fakes import FakeS3, make_ctx, obs_row

from deskbrief.config import Config
from deskbrief.schemas import to_frame
from deskbrief.store.backend import StorageBackend, StorageError
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect, escape_glob
from deskbrief.store.local import LocalBackend
from deskbrief.store.r2 import R2Backend, r2_boto_config

D = date(2026, 11, 13)
# 2026-11-14 02:00 UTC is still 2026-11-13 in ET (EST); 2026-11-14 06:00 UTC is 2026-11-14 in ET.
LATE_ON_D = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
NEXT_DAY = datetime(2026, 11, 14, 6, 0, tzinfo=UTC)
ON_D = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)


def _quarantine(at: datetime) -> pd.DataFrame:
    return to_frame(
        "quarantine",
        [
            {
                "fetch_id": "F1",
                "run_id": "R1",
                "source": "yahoo",
                "dataset": "quote",
                "reason": "bad json",
                "detected_at": at,
            }
        ],
    )


def _revisions(at: datetime) -> pd.DataFrame:
    return to_frame(
        "revisions",
        [
            {
                "instrument_id": "SPY",
                "source": "stooq",
                "dataset": "daily",
                "field": "close",
                "interval": "1d",
                "as_of": datetime(2026, 11, 13, 5, tzinfo=UTC),
                "old_value": 1.0,
                "new_value": 2.0,
                "old_fetch_id": "F1",
                "new_fetch_id": "F2",
                "detected_at": at,
                "run_id": "R1",
            }
        ],
    )


def _source_log(at: datetime) -> pd.DataFrame:
    return to_frame(
        "source_log",
        [
            {
                "fetch_id": "F1",
                "run_id": "R1",
                "source": "yahoo",
                "dataset": "quote",
                "symbols": "SPY",
                "request_redacted": "GET q",
                "requested_at": at,
                "responded_at": at,
                "latency_ms": 5,
                "status": "ok",
                "bytes": 10,
                "rows_parsed": 1,
                "rows_quarantined": 0,
                "attempt": 1,
            }
        ],
    )


def _dq(target: date) -> pd.DataFrame:
    return to_frame(
        "dq_results",
        [
            {
                "result_id": "Q1",
                "run_id": "R1",
                "check_name": "fresh",
                "check_type": "freshness",
                "target_date": target,
                "evaluated_at": ON_D,
                "status": "pass",
                "details_json": "{}",
            }
        ],
    )


def _xcheck(target: date) -> pd.DataFrame:
    return to_frame(
        "crosscheck_results",
        [
            {
                "result_id": "X1",
                "run_id": "R1",
                "check_name": "spy_vs_es",
                "target_date": target,
                "at_time": ON_D,
                "relation": "ratio",
                "left_instrument": "SPY",
                "left_source": "yahoo",
                "right_instrument": "ES_F",
                "right_source": "yahoo",
                "warn_bps": 5.0,
                "fail_bps": 10.0,
                "status": "pass",
                "details_json": "{}",
            }
        ],
    )


def _runs(target: date) -> pd.DataFrame:
    return to_frame(
        "runs",
        [
            {
                "run_id": "R1",
                "job": "morning",
                "trigger": "manual",
                "target_date": target,
                "started_at": ON_D,
                "status": "ok",
                "git_sha": "abc",
                "config_hash": "def",
                "n_fetch": 1,
                "n_fetch_failed": 0,
                "n_obs": 1,
                "n_dq_fail": 0,
                "n_dq_warn": 0,
            }
        ],
    )


def _obs(as_of: datetime) -> pd.DataFrame:
    return to_frame("observations", [obs_row(as_of=as_of)])


CASES: list[tuple[str, Any, Any]] = [
    ("observations", lambda: _obs(datetime(2026, 11, 13, 5, tzinfo=UTC)), lambda: _obs(NEXT_DAY)),
    ("quarantine", lambda: _quarantine(LATE_ON_D), lambda: _quarantine(NEXT_DAY)),
    ("revisions", lambda: _revisions(LATE_ON_D), lambda: _revisions(NEXT_DAY)),
    ("source_log", lambda: _source_log(LATE_ON_D), lambda: _source_log(NEXT_DAY)),
    ("dq_results", lambda: _dq(D), lambda: _dq(date(2026, 11, 12))),
    ("crosscheck_results", lambda: _xcheck(D), lambda: _xcheck(date(2026, 11, 12))),
    ("runs", lambda: _runs(D), lambda: _runs(date(2026, 11, 12))),
]


@pytest.mark.parametrize(("table", "good", "bad"), CASES, ids=[c[0] for c in CASES])
def test_partition_membership_per_p006(
    local: LocalBackend, config: Config, table: str, good: Any, bad: Any
) -> None:
    ctx = make_ctx(config, local, run_id="R1")
    ds = Datasets(local)
    with pytest.raises(ValueError, match="partition date"):
        ds.append(table, bad(), ctx, D)
    assert local.list("") == []  # nothing written for the rejected frame
    ds.append(table, good(), ctx, D)  # ET date, not UTC date, decides (late evening ET row)


def test_partition_check_names_the_offending_dates(local: LocalBackend, config: Config) -> None:
    ctx = make_ctx(config, local, run_id="R1")
    mixed = to_frame(
        "observations",
        [obs_row(), obs_row(symbol="x", as_of=NEXT_DAY)],
    )
    with pytest.raises(ValueError, match=r"as_of falls on 2026-11-14, not partition date"):
        Datasets(local).append("observations", mixed, ctx, D)


def test_overwrite_only_for_daily_tables(local: LocalBackend, config: Config) -> None:
    ctx = make_ctx(config, local, run_id="R1")
    ds = Datasets(local)
    ds.append("runs", _runs(D), ctx, D)
    with pytest.raises(ValueError, match="daily tables"):
        ds.append("runs", _runs(D), ctx, D, overwrite=True)
    card = to_frame(
        "scorecard_daily",
        [
            {
                "source": "yahoo",
                "date": D,
                "fetches": 1,
                "ok_rate": 1.0,
                "error_rate": 0.0,
                "n_crosschecks": 0,
            }
        ],
    )
    ds.append("scorecard_daily", card, ctx, D, overwrite=True)  # allowed


# --- raw immutability on both backends ---------------------------------------------------------
RAW = "raw/source=yahoo/dataset=quote/date=2026-11-16/x.json"


@pytest.fixture(params=["local", "r2"])
def backend(request: pytest.FixtureRequest, tmp_path: Path, fake_s3: FakeS3) -> StorageBackend:
    if request.param == "local":
        return LocalBackend(tmp_path / "data")
    return R2Backend("bkt", fake_s3)


def test_raw_overwrite_and_delete_refused(backend: StorageBackend) -> None:
    backend.put_bytes(RAW, b"evidence")
    with pytest.raises(StorageError, match="immutable"):
        backend.put_bytes(RAW, b"replaced", overwrite=True)
    with pytest.raises(StorageError, match="immutable"):
        backend.delete(RAW)
    with pytest.raises(StorageError, match="immutable"):
        backend.delete("raw/never/existed")  # refused even when absent
    assert backend.get_bytes(RAW) == b"evidence"


def test_raw_first_write_and_non_raw_mutation_still_work(backend: StorageBackend) -> None:
    backend.put_bytes(RAW, b"evidence")  # write-once is the normal path
    backend.put_bytes("clean/_CURRENT", b"v1")
    backend.put_bytes("clean/_CURRENT", b"v2", overwrite=True)
    backend.delete("clean/_CURRENT")
    # only the exact "raw/" layer is protected, not names that merely start with "raw"
    backend.put_bytes("rawish/k", b"1")
    backend.put_bytes("rawish/k", b"2", overwrite=True)
    backend.delete("rawish/k")


def test_r2_raw_refusal_makes_no_client_call(fake_s3: FakeS3) -> None:
    b = R2Backend("bkt", fake_s3)
    with pytest.raises(StorageError):
        b.put_bytes(RAW, b"x", overwrite=True)
    with pytest.raises(StorageError):
        b.delete(RAW)
    assert fake_s3.calls == []


# --- R2 boto config -----------------------------------------------------------------------------
def test_r2_boto_config_checksums_only_when_required() -> None:
    cfg = r2_boto_config()
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"


def test_from_credentials_passes_that_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from deskbrief.store import r2

    seen: dict[str, Any] = {}

    def fake_client(service: str, **kw: Any) -> object:
        seen.update(kw)
        return object()

    monkeypatch.setattr(r2.boto3, "client", fake_client)
    R2Backend.from_credentials("bkt", r2.R2Credentials("acct", "AKID", "SECRET"))
    assert seen["config"].request_checksum_calculation == "when_required"
    assert seen["config"].response_checksum_validation == "when_required"


# --- _CURRENT cached per Datasets instance -----------------------------------------------------
class _CountingBackend(LocalBackend):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.pointer_reads = 0

    def get_bytes(self, key: str) -> bytes:
        if key == "clean/_CURRENT":
            self.pointer_reads += 1
        return super().get_bytes(key)


def test_current_pointer_resolved_once_per_instance(tmp_path: Path, config: Config) -> None:
    store = _CountingBackend(tmp_path / "data")
    store.put_bytes("clean/_CURRENT", b"v3")
    ds = Datasets(store)
    for _ in range(3):  # key_for / read never re-read the pointer
        assert ds.key_for("observations", D, "R9").startswith("clean/v3/")
        ds.read("observations")
    assert store.pointer_reads == 1
    # a clean-table append makes exactly one extra cheap read as the staleness guard
    ds.append(
        "observations", _obs(datetime(2026, 11, 13, 5, tzinfo=UTC)), make_ctx(config, store), D
    )
    assert store.pointer_reads == 2


def test_append_refuses_to_write_after_current_was_flipped(
    local: LocalBackend, config: Config
) -> None:
    ds = Datasets(local)
    obs = _obs(datetime(2026, 11, 13, 5, tzinfo=UTC))
    ds.append("observations", obs, make_ctx(config, local, run_id="A"), D)  # resolves v1
    local.put_bytes("clean/_CURRENT", b"v2")  # rebuild flips the pointer
    with pytest.raises(StorageError, match="refresh_clean_version"):
        ds.append("observations", obs, make_ctx(config, local, run_id="B"), D)
    assert local.list("clean/v2/") == []  # nothing written to either version by the refusal
    assert len(local.list("clean/v1/")) == 1
    assert ds.refresh_clean_version() == "v2"
    key = ds.append("observations", obs, make_ctx(config, local, run_id="B"), D)
    assert key.startswith("clean/v2/")
    # non-clean tables do not depend on the pointer
    Datasets(local).append("runs", _runs(D), make_ctx(config, local, run_id="C"), D)


def test_scorecard_rows_must_match_the_partition_date(local: LocalBackend, config: Config) -> None:
    def card(day: date) -> pd.DataFrame:
        return to_frame(
            "scorecard_daily",
            [
                {
                    "source": "yahoo",
                    "date": day,
                    "fetches": 1,
                    "ok_rate": 1.0,
                    "error_rate": 0.0,
                    "n_crosschecks": 0,
                }
            ],
        )

    ds = Datasets(local)
    with pytest.raises(ValueError, match="scorecard_daily: date falls on 2020-01-01"):
        ds.append("scorecard_daily", card(date(2020, 1, 1)), make_ctx(config, local), D)
    assert local.list("") == []
    ds.append("scorecard_daily", card(D), make_ctx(config, local), D)


# --- DuckDB glob escaping -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "escaped"),
    [("/a/plain", "/a/plain"), ("/a/b[1]", "/a/b[[]1]"), ("/a/*", "/a/[*]"), ("/a/?x", "/a/[?]x")],
)
def test_escape_glob(raw: str, escaped: str) -> None:
    assert escape_glob(raw) == escaped


@pytest.mark.parametrize("name", ["run[1]", "star*root", "what?root", "a[b]*?c"])
def test_root_with_glob_metacharacters_is_read_literally(
    tmp_path: Path, config: Config, name: str
) -> None:
    def frame(value: float) -> pd.DataFrame:
        return to_frame("observations", [obs_row(value=value)])

    store = LocalBackend(tmp_path / name)
    Datasets(store).append("observations", frame(1.0), make_ctx(config, store, run_id="R1"), D)
    # a sibling that the unescaped pattern would also match must not leak in
    other = LocalBackend(tmp_path / "run1")
    Datasets(other).append("observations", frame(2.0), make_ctx(config, other, run_id="R2"), D)
    conn = connect(store, config)
    assert conn.execute("SELECT value FROM obs_all").fetchall() == [(1.0,)]
