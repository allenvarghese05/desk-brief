"""T06 part 2: rebuild flip guard, month-chunked rebuild and the EMPTY warn row.

SYNTHETIC FIXTURE: every source, payload and price here is made up.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pipeline_fakes import FakeSource, make_env

from deskbrief.models import FetchRequest, JobKind, RawResponse, RunStatus, Trigger
from deskbrief.pipeline import clean
from deskbrief.pipeline.runner import RunEnv, run_job, run_rebuild
from deskbrief.sources.base import HttpClient
from deskbrief.store.backend import CLEAN_CURRENT_KEY
from deskbrief.store.datasets import Datasets, current_clean_version

MONDAY = date(2026, 11, 16)


def first_run(env: RunEnv) -> None:
    run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)


def move_pointer_before_flip(monkeypatch: pytest.MonkeyPatch, env: RunEnv, to: bytes) -> None:
    """``_carry_over`` runs after the last clean write and just before the flip."""

    def moved(store: Any, old: str, new: str, from_date: date | None) -> int:
        env.store.put_bytes(CLEAN_CURRENT_KEY, to, overwrite=True)
        return 0

    monkeypatch.setattr(clean, "_carry_over", moved)


# --- flip guard ----------------------------------------------------------------------------


def test_rebuild_with_zero_partitions_leaves_current_unchanged(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    first_run(env)
    env.sources = [FakeSource("ecb")]  # stooq's raw is now an unknown source: all skipped
    m = run_rebuild(env)
    assert m.status is RunStatus.FAILED
    assert current_clean_version(env.store) == "v1"
    assert any("no partitions" in n for n in m.notes)
    assert len(Datasets(env.store).read("observations")) == 1  # readers still see v1


def test_pointer_move_between_write_and_flip_aborts_the_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    first_run(env)
    move_pointer_before_flip(monkeypatch, env, b"v9")
    m = run_rebuild(env)
    assert m.status is RunStatus.FAILED
    assert env.store.get_bytes(CLEAN_CURRENT_KEY) == b"v9"  # the rebuild did not overwrite it
    assert any("moved" in n for n in m.notes)


def test_a_claimed_version_is_not_rebuilt_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    first_run(env)
    env.store.put_bytes("clean/v2/_CLAIM", b"other-rebuild")
    monkeypatch.setattr(clean, "next_clean_version", lambda store: "v2")
    m = run_rebuild(env)
    assert m.status is RunStatus.FAILED
    assert any("claimed" in n for n in m.notes)
    assert current_clean_version(env.store) == "v1"


def test_a_second_flip_away_from_the_same_version_is_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    first_run(env)
    env.store.put_bytes("clean/_flips/from-v1", b"v7 other-rebuild")  # someone flipped first
    m = run_rebuild(env)
    assert m.status is RunStatus.FAILED
    assert any("already flipped" in n for n in m.notes)
    assert current_clean_version(env.store) == "v1"


def test_a_failed_pointer_write_releases_the_flip_claim(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    first_run(env)
    real_put = env.store.put_bytes

    def no_pointer(key: str, data: bytes, *, overwrite: bool = False) -> None:
        if key == CLEAN_CURRENT_KEY:
            raise OSError("pointer write failed")
        real_put(key, data, overwrite=overwrite)

    env.store.put_bytes = no_pointer  # type: ignore[method-assign]
    assert run_rebuild(env).status is RunStatus.FAILED
    assert not env.store.exists("clean/_flips/from-v1")
    env.store.put_bytes = real_put  # type: ignore[method-assign]
    assert run_rebuild(env).status is RunStatus.OK  # a retry is not blocked


# --- month chunking ------------------------------------------------------------------------


class _DatedSource(FakeSource):
    """SYNTHETIC FIXTURE: stamps every response with a fixed fetch time."""

    def __init__(self, name: str, when: datetime, **kw: Any) -> None:
        super().__init__(name, **kw)
        self.when = when

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        raw = super().fetch(req, http)
        return replace(raw, requested_at=self.when, responded_at=self.when)


def test_rebuild_flushes_one_month_at_a_time_and_flips_once_at_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    points = [(datetime(2026, 10, 29, 4, 0, tzinfo=UTC), 100.0)]
    env = make_env(
        tmp_path,
        [
            _DatedSource("stooq", datetime(2026, 10, 30, 21, tzinfo=UTC), points=points),
            _DatedSource(
                "ecb", datetime(2026, 11, 2, 15, tzinfo=UTC), points=points, instrument="EURUSD"
            ),
        ],
    )
    first_run(env)
    events: list[str] = []
    real_flush = clean.RunBuffer.flush
    real_put = env.store.put_bytes

    def flush(self: clean.RunBuffer, datasets: Datasets, ctx: Any, **kw: Any) -> Any:
        events.append(f"flush:{ctx.run_id.rpartition('-')[2]}")
        return real_flush(self, datasets, ctx, **kw)

    def put(key: str, data: bytes, *, overwrite: bool = False) -> None:
        if key == CLEAN_CURRENT_KEY:
            events.append("flip")
        real_put(key, data, overwrite=overwrite)

    monkeypatch.setattr(clean.RunBuffer, "flush", flush)
    env.store.put_bytes = put  # type: ignore[method-assign]
    m = run_rebuild(env)
    assert m.status is RunStatus.OK
    assert events == ["flush:10", "flush:11", "flip"]  # chunk per UTC month, single final flip
    v2 = [k for k in env.store.list("clean/v2/observations/") if k.endswith(".parquet")]
    assert len(v2) == 2  # same ET date in two months: two write-once files, not a KeyExistsError
    assert sorted(Datasets(env.store).read("observations")["source"]) == ["ecb", "stooq"]


# --- EMPTY warn ----------------------------------------------------------------------------


def empty_run(tmp_path: Path, job: JobKind, target: date, **kw: Any) -> tuple[Any, RunEnv]:
    env = make_env(tmp_path, [FakeSource("stooq", mode="empty", **kw)])
    return run_job(env, job, target_date=target, trigger=Trigger.MANUAL), env


def warns(env: RunEnv) -> Any:
    dq = Datasets(env.store).read("dq_results")
    return dq[dq["check_name"] == "runner.empty"]


def test_empty_fetch_on_an_expected_day_writes_a_warn_row(tmp_path: Path) -> None:
    m, env = empty_run(tmp_path, JobKind.MORNING, MONDAY)  # judges Friday's 20:00 ET publication
    w = warns(env)
    assert list(w["status"]) == ["warn"]
    assert list(w["source"]) == ["stooq"]
    assert '"data_day":"2026-11-13"' in w.iloc[0]["details_json"]
    assert m.status is RunStatus.OK  # a warn is not a failure
    assert (m.n_dq_warn, m.n_dq_fail) == (1, 0)


@pytest.mark.parametrize(
    ("job", "target", "dataset", "why"),
    [
        (JobKind.EOD, date(2026, 11, 14), "daily", "weekend"),
        (JobKind.EOD, date(2026, 11, 26), "daily", "holiday (Thanksgiving)"),
        (JobKind.EOD, MONDAY, "daily", "not yet due at 06:20 ET"),
        (JobKind.MORNING, MONDAY, "mystery", "no schedule entry: no expectation"),
    ],
)
def test_empty_fetch_with_a_calendar_reason_or_no_schedule_is_silent(
    tmp_path: Path, job: JobKind, target: date, dataset: str, why: str
) -> None:
    _, env = empty_run(tmp_path, job, target, dataset=dataset)
    assert warns(env).empty, why
