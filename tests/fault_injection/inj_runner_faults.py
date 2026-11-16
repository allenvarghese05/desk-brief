"""INJECTED FAULT: runner fault-injection scenarios (card T19, spec §2.4, §13).

Every scenario injects a fault and asserts the run is ``degraded`` (not crashed), the other
sources' data is present, and the right quarantine / DQ rows exist. Nothing here is a real
incident; none of it belongs in INCIDENTS.md.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pipeline_fakes import FakeSource, load_test_config, make_env

from deskbrief.models import FetchRequest, JobKind, RawResponse, RunStatus, Trigger
from deskbrief.pipeline import jobs
from deskbrief.pipeline.runner import RunEnv, run_job, run_rebuild
from deskbrief.sources.http import BudgetTracker, DefaultHttpClient
from deskbrief.store.backend import KeyExistsError
from deskbrief.store.datasets import Datasets, current_clean_version

TARGET = date(2026, 11, 16)


def go(env: RunEnv, job: JobKind = JobKind.EOD) -> Any:
    return run_job(env, job, target_date=TARGET, trigger=Trigger.MANUAL)


def read(env: RunEnv, table: str) -> Any:
    return Datasets(env.store).read(table)


class Http429Source(FakeSource):
    """Calls the real ``DefaultHttpClient`` against a mocked endpoint that answers 429."""

    def fetch(self, req: FetchRequest, http: Any) -> RawResponse:
        return http.get(req, "https://api.example.invalid/quote")  # type: ignore[no-any-return]


def test_inj_one_raises_one_corrupt_one_429_storm_leaves_a_degraded_run(tmp_path: Path) -> None:
    """The card's headline scenario: three different faults, one healthy source."""
    cfg = load_test_config()
    env = make_env(tmp_path, [], config=cfg)
    storm = Http429Source("twelvedata", dataset="fx_quote")
    env.sources = [
        FakeSource("ecb", mode="raise_fetch", instrument="EURUSD"),
        FakeSource("stooq", mode="corrupt"),
        storm,
        FakeSource("treasury", instrument="UST10Y", dataset="curve"),
    ]
    tracker = BudgetTracker.from_settings(jobs.budget_store(env.store), cfg.sources)
    env.http = DefaultHttpClient(cfg.sources, budget=tracker, sleep=lambda s: None)  # no real waits

    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.example.invalid/quote").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "0"})
        )
        m = go(env)

    assert m.status is RunStatus.DEGRADED
    assert m.exit_code == 0
    # the healthy source's data is present
    assert list(read(env, "observations")["source"]) == ["treasury"]
    # the corrupted body is quarantined, with the raw kept as evidence
    q = read(env, "quarantine")
    assert list(q["source"]) == ["stooq"]
    assert q.iloc[0]["raw_key"].startswith("raw/")
    # one DQ fail per faulty source, and the right kind
    dq = read(env, "dq_results")
    fails = dq[dq["status"] == "fail"].set_index("source")["check_name"].to_dict()
    assert fails == {"ecb": "runner.fetch", "stooq": "runner.parse", "twelvedata": "runner.fetch"}
    log = read(env, "source_log").set_index("source")
    assert log.loc["twelvedata", "status"] == "rate_limited"
    assert log.loc["twelvedata", "http_status"] == 429
    assert log.loc["twelvedata", "attempt"] > 1  # the client retried before giving up
    assert log.loc["ecb", "status"] == "error"
    run_row = read(env, "runs").iloc[0]
    assert (run_row["status"], run_row["n_fetch_failed"], run_row["n_dq_fail"]) == (
        "degraded",
        3,
        3,
    )
    # every HTTP attempt of the storm was counted against the daily budget
    used = jobs.budget_store(env.store).load("twelvedata", datetime.now(UTC).date())
    assert used == log.loc["twelvedata", "attempt"]


@pytest.mark.parametrize("mode", ["corrupt", "bad_schema", "parser_bug"])
def test_inj_corrupted_body_variants_never_reach_clean_observations(
    tmp_path: Path, mode: str
) -> None:
    env = make_env(
        tmp_path, [FakeSource("stooq", mode=mode), FakeSource("ecb", instrument="EURUSD")]
    )
    m = go(env)
    assert m.status is RunStatus.DEGRADED
    assert set(read(env, "observations")["source"]) == {"ecb"}
    assert len(read(env, "quarantine")) == 1


class _Crash(BaseException):
    """INJECTED FAULT: not an ``Exception``, so no per-source handler can swallow it."""


class _CrashingSource(FakeSource):
    def fetch(self, req: Any, http: Any) -> Any:
        raise _Crash


def test_inj_mid_run_raise_still_flushes_every_other_source(tmp_path: Path) -> None:
    env = make_env(
        tmp_path,
        [FakeSource("treasury", instrument="UST10Y"), _CrashingSource("fred"), FakeSource("ecb")],
    )
    with pytest.raises(_Crash):
        go(env)
    obs = read(env, "observations")
    assert list(obs["source"]) == ["treasury"]
    assert len(read(env, "source_log")) == 1
    assert list(read(env, "runs")["status"]) == ["failed"]


def test_inj_crash_before_flush_keeps_raw_and_rebuild_recovers_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, [FakeSource("treasury", instrument="UST10Y"), FakeSource("ecb")])

    def die(*a: Any, **k: Any) -> Any:
        raise SystemExit("process killed before flush")  # not caught by `except Exception`

    monkeypatch.setattr("deskbrief.pipeline.clean.RunBuffer._partitions", die)
    with pytest.raises(SystemExit):
        go(env)
    monkeypatch.undo()

    assert read(env, "observations").empty  # nothing clean was written
    sidecars = [k for k in env.store.list("raw/") if k.endswith(".meta.json")]
    assert len(sidecars) == 2  # but raw has every response

    rebuilt = run_rebuild(env)
    assert rebuilt.status is RunStatus.OK
    assert current_clean_version(env.store) == "v2"
    assert sorted(read(env, "observations")["source"]) == ["ecb", "treasury"]


def test_inj_rerun_uses_a_new_run_id_and_never_collides(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("treasury", instrument="UST10Y")])
    runs = [go(env, JobKind.BACKFILL) for _ in range(3)]
    assert len({m.run_id for m in runs}) == 3
    assert all(m.status is RunStatus.OK for m in runs)
    assert len(read(env, "observations")) == 3  # append-only; the view dedupes (see unit test)


def test_inj_second_append_for_the_same_run_is_loud(tmp_path: Path) -> None:
    """P-008: the storage layer still refuses a double write; the runner just never makes one."""
    env = make_env(tmp_path, [FakeSource("treasury", instrument="UST10Y")])
    m = go(env, JobKind.BACKFILL)
    ctx_obs = read(env, "observations")
    from pipeline_fakes import FakeClock

    from deskbrief.pipeline.runner import make_context

    ctx = make_context(env, JobKind.BACKFILL, Trigger.MANUAL, TARGET, FakeClock()())
    object.__setattr__(ctx, "run_id", m.run_id)
    with pytest.raises(KeyExistsError):
        Datasets(env.store).append("observations", ctx_obs, ctx, date(2026, 11, 13))
