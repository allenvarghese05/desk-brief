"""Catch-up, rebuild, cron/delay helpers, catalog, budget wiring and the CLI.

SYNTHETIC FIXTURE: sources and payloads come from ``pipeline_fakes``.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import types
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pipeline_fakes import REPO_ROOT, FakeSource, make_env

from deskbrief import cli
from deskbrief.models import JobKind, RunStatus, Trigger
from deskbrief.pipeline import jobs
from deskbrief.pipeline.catchup import (
    CATCHUP_TIME_BUDGET,
    MAX_CATCHUP_DAYS_PER_RUN,
    missed_dates,
    split_catchup,
    window,
)
from deskbrief.pipeline.clean import next_clean_version
from deskbrief.pipeline.runner import RunEnv, make_context, run_job, run_rebuild
from deskbrief.schemas import to_frame
from deskbrief.sources import catalog, registry
from deskbrief.store import duck
from deskbrief.store.backend import CLEAN_CURRENT_KEY, KeyNotFoundError
from deskbrief.store.datasets import Datasets, current_clean_version

MONDAY = date(2026, 11, 16)
TUESDAY = date(2026, 11, 17)
NOW = datetime(2026, 11, 17, 11, 20, tzinfo=UTC)


def seed_run(
    env: RunEnv, day: date, *, job: str = "morning", status: str = "ok", trigger: str = "schedule"
) -> None:
    """INJECTED HISTORY: a ``runs`` row as if a previous run had happened."""
    ctx = make_context(env, JobKind.MORNING, Trigger.MANUAL, day, NOW)
    row = {
        "run_id": f"seed-{job}-{day}-{status}",
        "job": job,
        "trigger": trigger,
        "target_date": day,
        "started_at": NOW,
        "status": status,
        "git_sha": "x",
        "config_hash": "y",
        "n_fetch": 1,
        "n_fetch_failed": 0,
        "n_obs": 1,
        "n_dq_fail": 0,
        "n_dq_warn": 0,
    }
    Datasets(env.store).append("runs", to_frame("runs", [row]), ctx, day)


# --- catch-up ---------------------------------------------------------------------------------


def test_window_is_the_five_xnys_business_days_before_the_target() -> None:
    assert window(MONDAY) == [
        date(2026, 11, 9),
        date(2026, 11, 10),
        date(2026, 11, 11),
        date(2026, 11, 12),
        date(2026, 11, 13),
    ]
    # Thanksgiving (Thu Nov 26) is not a business day
    assert date(2026, 11, 26) not in window(date(2026, 11, 30))


def test_missed_dates_ignores_covered_failed_and_other_jobs() -> None:
    def runs(*rows: tuple[str, date, str]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "job": [r[0] for r in rows],
                "target_date": [r[1] for r in rows],
                "status": [r[2] for r in rows],
            }
        )

    history = runs(
        ("morning", date(2026, 11, 9), "ok"),
        ("morning", date(2026, 11, 10), "degraded"),
        ("morning", date(2026, 11, 11), "failed"),  # failed does not cover
        ("eod", date(2026, 11, 12), "ok"),  # another job does not cover a morning
        ("backfill", date(2026, 11, 13), "ok"),  # a backfill does
    )
    assert missed_dates(history, JobKind.MORNING, MONDAY) == [
        date(2026, 11, 11),
        date(2026, 11, 12),
    ]
    assert missed_dates(history, JobKind.INTRADAY, MONDAY) == []
    assert (
        missed_dates(runs(), JobKind.MORNING, MONDAY) == []
    )  # first ever run: nothing to catch up


def test_catch_up_backfills_two_missed_days_before_the_main_run(tmp_path: Path) -> None:
    src = FakeSource("stooq")
    env = make_env(tmp_path, [src])
    for d in (date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 13), date(2026, 11, 16)):
        seed_run(env, d)
    # missed: Wed 11, Thu 12
    m = run_job(env, JobKind.MORNING, target_date=TUESDAY, trigger=Trigger.MANUAL)
    assert [c.target_date for c in m.catchup_runs] == [date(2026, 11, 11), date(2026, 11, 12)]
    assert src.plans == [
        (JobKind.BACKFILL, date(2026, 11, 11)),
        (JobKind.BACKFILL, date(2026, 11, 12)),
        (JobKind.MORNING, TUESDAY),
    ]
    runs = Datasets(env.store).read("runs")
    caught = runs[runs["trigger"] == "catchup"]
    assert sorted(caught["target_date"]) == [date(2026, 11, 11), date(2026, 11, 12)]
    assert set(caught["job"]) == {"backfill"}
    assert len(set(caught["run_id"])) == 2
    assert m.run_id not in set(caught["run_id"])
    assert "Catch-up runs" in m.to_markdown()
    # the next run finds nothing left to do
    again = run_job(env, JobKind.MORNING, target_date=TUESDAY, trigger=Trigger.MANUAL)
    assert again.catchup_runs == []


def test_catch_up_is_bounded_per_run_oldest_first(tmp_path: Path) -> None:
    """T25c: five missed days at ~5 min each hit the 30-minute timeout (run 44219067732) before
    the morning report. Each run now backfills at most two, oldest first; later runs finish."""
    assert MAX_CATCHUP_DAYS_PER_RUN == 2
    src = FakeSource("stooq")
    env = make_env(tmp_path, [src])
    seed_run(env, date(2026, 11, 2))  # history exists, but the whole window was missed
    missed = window(TUESDAY)
    first = run_job(env, JobKind.MORNING, target_date=TUESDAY, trigger=Trigger.MANUAL)
    assert [c.target_date for c in first.catchup_runs] == missed[:2]
    assert src.plans[-1] == (JobKind.MORNING, TUESDAY)  # the main run still happens
    assert any(
        n == "catch-up deferred to the next run: " + ", ".join(d.isoformat() for d in missed[2:])
        for n in first.notes
    )
    second = run_job(env, JobKind.MORNING, target_date=TUESDAY, trigger=Trigger.MANUAL)
    assert [c.target_date for c in second.catchup_runs] == missed[2:4]
    third = run_job(env, JobKind.MORNING, target_date=TUESDAY, trigger=Trigger.MANUAL)
    assert [c.target_date for c in third.catchup_runs] == missed[4:]
    assert not any("deferred" in n for n in third.notes)
    assert run_job(env, JobKind.MORNING, target_date=TUESDAY).catchup_runs == []


def test_catch_up_stops_starting_days_after_the_time_budget(tmp_path: Path) -> None:
    """A slow catch-up day (the clock jumps past the budget) defers the rest to the next run."""
    start = datetime(2026, 11, 17, 11, 0, tzinfo=UTC)

    class SlowClock:
        def __init__(self) -> None:
            self.now = start

        def __call__(self) -> datetime:
            self.now += timedelta(seconds=1)
            return self.now

    clock = SlowClock()

    class SlowSource(FakeSource):
        def plan(self, job: JobKind, ctx: Any) -> list[Any]:
            if job is JobKind.BACKFILL:  # one backfill day eats the whole budget
                clock.now += CATCHUP_TIME_BUDGET
            return super().plan(job, ctx)

    src = SlowSource("stooq")
    env = make_env(tmp_path, [src], clock=clock)
    seed_run(env, date(2026, 11, 2))
    m = run_job(env, JobKind.MORNING, target_date=TUESDAY, trigger=Trigger.MANUAL)
    missed = window(TUESDAY)
    assert [c.target_date for c in m.catchup_runs] == missed[:1]
    assert [p[0] for p in src.plans] == [JobKind.BACKFILL, JobKind.MORNING]
    deferred = ", ".join(d.isoformat() for d in missed[1:])
    assert f"catch-up deferred to the next run: {deferred}" in m.notes


def test_split_catchup_orders_and_limits() -> None:
    days = [date(2026, 11, 13), date(2026, 11, 9), date(2026, 11, 11)]
    assert split_catchup(days, 2) == ([date(2026, 11, 9), date(2026, 11, 11)], [date(2026, 11, 13)])
    assert split_catchup(days, 0) == ([], sorted(days))
    assert split_catchup([], 2) == ([], [])


def test_catch_up_failure_never_blocks_the_main_run(tmp_path: Path) -> None:
    class BoomOnPast(FakeSource):
        def plan(self, job: JobKind, ctx: Any) -> list[Any]:
            if job is JobKind.BACKFILL:
                raise _StopError
            return super().plan(job, ctx)

    class _StopError(Exception):
        pass

    env = make_env(tmp_path, [BoomOnPast("stooq")])
    seed_run(env, date(2026, 11, 13))
    m = run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)
    assert m.status is RunStatus.OK
    # the failed catch-up runs are recorded (as failed), so they will be retried next time
    runs = Datasets(env.store).read("runs")
    assert set(runs[runs["trigger"] == "catchup"]["status"]) == {"failed"}


def test_first_ever_run_does_not_backfill(tmp_path: Path) -> None:
    src = FakeSource("stooq")
    env = make_env(tmp_path, [src])
    run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)
    assert src.plans == [(JobKind.MORNING, MONDAY)]


def test_backfill_and_intraday_jobs_do_not_catch_up(tmp_path: Path) -> None:
    src = FakeSource("stooq")
    env = make_env(tmp_path, [src])
    seed_run(env, date(2026, 11, 9))
    run_job(env, JobKind.INTRADAY, target_date=MONDAY, trigger=Trigger.MANUAL)
    run_job(env, JobKind.BACKFILL, target_date=MONDAY, trigger=Trigger.MANUAL)
    assert [p[0] for p in src.plans] == [JobKind.INTRADAY, JobKind.BACKFILL]


# --- rebuild ------------------------------------------------------------------------------------


def obs_keys(env: RunEnv, version: str) -> list[str]:
    return [k for k in env.store.list(f"clean/{version}/observations/") if k.endswith(".parquet")]


def test_rebuild_writes_v2_from_raw_then_flips_current(tmp_path: Path) -> None:
    pts = [
        (datetime(2026, 11, 12, 5, 0, tzinfo=UTC), 449.0),
        (datetime(2026, 11, 13, 5, 0, tzinfo=UTC), 450.0),
    ]
    env = make_env(tmp_path, [FakeSource("stooq", points=pts)])
    run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)
    before = Datasets(env.store).read("observations").sort_values("as_of")
    assert current_clean_version(env.store) == "v1"

    m = run_rebuild(env)
    assert m.status is RunStatus.OK
    assert current_clean_version(env.store) == "v2"
    assert env.store.get_bytes(CLEAN_CURRENT_KEY) == b"v2"
    assert len(obs_keys(env, "v1")) == 2  # the old version is left in place
    after = Datasets(env.store).read("observations").sort_values("as_of")
    assert list(after["value"]) == list(before["value"])
    assert list(after["fetch_id"]) == list(before["fetch_id"])  # lineage points at the same fetches
    assert list(after["ingested_at"]) == list(before["ingested_at"])
    assert set(after["run_id"]) == {m.run_id}  # produced by the rebuild run
    runs = Datasets(env.store).read("runs")
    assert "rebuild" in set(runs["job"])
    conn = duck.connect(env.store, env.config)  # views follow the flipped pointer
    assert conn.execute("SELECT count(*) FROM obs_latest").fetchone() == (2,)


def test_crash_before_flush_leaves_raw_and_rebuild_recovers_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, [FakeSource("stooq"), FakeSource("ecb", instrument="EURUSD")])
    # the process "dies" before the flush: nothing clean is written
    monkeypatch.setattr("deskbrief.pipeline.clean.RunBuffer.flush", lambda *a, **k: _empty_report())
    run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)
    monkeypatch.undo()
    assert Datasets(env.store).read("observations").empty
    raw = [k for k in env.store.list("raw/") if k.endswith(".meta.json")]
    assert len(raw) == 2  # raw is intact

    m = run_rebuild(env)
    assert m.status is RunStatus.OK
    obs = Datasets(env.store).read("observations")
    assert sorted(obs["source"]) == ["ecb", "stooq"]
    # a retried run after the crash gets a new run_id and also works
    assert (
        run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL).status
        is RunStatus.OK
    )


def _empty_report() -> Any:
    from deskbrief.pipeline.clean import FlushReport

    return FlushReport()


def test_rebuild_quarantines_raw_the_current_parser_rejects(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)
    env.sources = [FakeSource("stooq", mode="parser_bug")]  # a parser regression
    m = run_rebuild(env)
    assert m.status is RunStatus.DEGRADED
    assert Datasets(env.store).read("observations").empty
    q = Datasets(env.store).read("quarantine")
    assert list(q["reason"]) == ["parser_exception"]
    assert current_clean_version(env.store) == "v2"


def test_rebuild_skips_raw_of_unknown_sources_and_failed_fetches(tmp_path: Path) -> None:
    env = make_env(
        tmp_path,
        [FakeSource("stooq"), FakeSource("ecb", mode="http_500")],
    )
    run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)
    env.sources = [env.sources[0]]  # ecb no longer registered
    m = run_rebuild(env)
    assert m.status is RunStatus.OK
    assert "raw skipped: 1" in " ".join(m.notes)
    assert list(Datasets(env.store).read("observations")["source"]) == ["stooq"]


def test_rebuild_from_date_carries_older_partitions_over(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)
    m = run_rebuild(env, from_date=date(2026, 12, 1))  # later than all raw: nothing re-parsed
    assert m.status is RunStatus.OK
    assert current_clean_version(env.store) == "v2"
    assert len(obs_keys(env, "v2")) == 1  # carried over byte-for-byte
    assert len(Datasets(env.store).read("observations")) == 1


def test_failed_rebuild_flush_never_flips_current(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)
    real_put = env.store.put_bytes

    def flaky(key: str, data: bytes, *, overwrite: bool = False) -> None:
        if key.startswith("clean/v2/"):
            raise OSError("boom")
        real_put(key, data, overwrite=overwrite)

    env.store.put_bytes = flaky  # type: ignore[method-assign]
    m = run_rebuild(env)
    assert m.status is RunStatus.FAILED
    assert m.exit_code == 1
    assert current_clean_version(env.store) == "v1"
    assert any("NOT flipped" in n for n in m.notes)


def test_rebuild_with_unreadable_storage_is_failed(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])

    def dead(*a: Any, **k: Any) -> Any:
        raise OSError("gone")

    env.store.list = dead  # type: ignore[method-assign]
    m = run_rebuild(env)
    assert m.status is RunStatus.FAILED


def test_rebuild_dry_run_writes_nothing(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    m = run_rebuild(env, dry_run=True)
    assert m.planned == ["rebuild raw -> clean/v2"]
    assert env.store.list("") == []


def test_next_clean_version_skips_abandoned_versions(tmp_path: Path) -> None:
    env = make_env(tmp_path, [])
    assert next_clean_version(env.store) == "v2"
    env.store.put_bytes("clean/v4/observations/date=2026-01-02/run=x.parquet", b"x")
    assert next_clean_version(env.store) == "v5"
    with pytest.raises(KeyNotFoundError):
        env.store.get_bytes(CLEAN_CURRENT_KEY)


# --- cron / schedule delay ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "now", "expected"),
    [
        (
            "15 10,11 * * 1-5",
            datetime(2026, 11, 16, 11, 21, tzinfo=UTC),
            datetime(2026, 11, 16, 11, 15, tzinfo=UTC),
        ),
        (
            "15 10,11 * * 1-5",
            datetime(2026, 11, 16, 10, 59, tzinfo=UTC),
            datetime(2026, 11, 16, 10, 15, tzinfo=UTC),
        ),
        (
            "*/15 13-21 * * 1-5",
            datetime(2026, 11, 16, 14, 7, tzinfo=UTC),
            datetime(2026, 11, 16, 14, 0, tzinfo=UTC),
        ),
        (
            "30 22 * * 1-5",
            datetime(2026, 11, 16, 2, 0, tzinfo=UTC),
            datetime(2026, 11, 13, 22, 30, tzinfo=UTC),
        ),
        ("0 0 1 1 *", datetime(2026, 11, 16, 2, 0, tzinfo=UTC), None),
        ("0 12 * * 3", datetime(2026, 11, 16, 13, 0, tzinfo=UTC), None),  # Wednesdays: > 72 h away
        (
            "0 12 * * 7",
            datetime(2026, 11, 15, 13, 0, tzinfo=UTC),
            datetime(2026, 11, 15, 12, 0, tzinfo=UTC),
        ),
        (
            "0 12 15 * 1",
            datetime(2026, 11, 16, 13, 0, tzinfo=UTC),
            datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
        ),  # dom OR dow
    ],
)
def test_last_cron_fire(expr: str, now: datetime, expected: datetime | None) -> None:
    assert jobs.last_cron_fire(expr, now) == expected


@pytest.mark.parametrize("bad", ["* * *", "61 * * * *", "*/0 * * * *", "a b c d e"])
def test_bad_cron_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match=r"cron|invalid literal"):
        jobs.last_cron_fire(bad, NOW)


def test_scheduled_for_accepts_cron_iso_and_ignores_garbage() -> None:
    now = datetime(2026, 11, 16, 11, 20, tzinfo=UTC)
    assert jobs.scheduled_for_from_env({}, now) is None
    assert jobs.scheduled_for_from_env({"GITHUB_EVENT_SCHEDULE": "15 11 * * *"}, now) == datetime(
        2026, 11, 16, 11, 15, tzinfo=UTC
    )
    iso = jobs.scheduled_for_from_env({"GITHUB_EVENT_SCHEDULE": "2026-11-16T11:15:00+00:00"}, now)
    assert iso == datetime(2026, 11, 16, 11, 15, tzinfo=UTC)
    assert jobs.scheduled_for_from_env({"GITHUB_EVENT_SCHEDULE": "not a schedule"}, now) is None
    assert jobs.schedule_delay_s(None, now) is None
    assert (
        jobs.schedule_delay_s(datetime(2026, 11, 16, 11, 25, tzinfo=UTC), now) == 0.0
    )  # never negative


def test_parse_sources_arg() -> None:
    assert jobs.parse_sources_arg(None) is None
    assert jobs.parse_sources_arg("") is None
    assert jobs.parse_sources_arg("fred, ecb") == frozenset({"fred", "ecb"})


# --- budget wiring and store construction -------------------------------------------------------


def test_budget_store_persists_under_the_budget_key(tmp_path: Path) -> None:
    env = make_env(tmp_path, [])
    store = jobs.budget_store(env.store)
    day = date(2026, 11, 16)
    assert store.load("twelvedata", day) == 0
    store.save("twelvedata", day, 7)
    assert store.load("twelvedata", day) == 7
    assert env.store.get_bytes("logs/budget/twelvedata/date=2026-11-16.json") == b'{"used": 7}'


def test_build_http_enforces_the_configured_daily_budget(tmp_path: Path) -> None:
    import httpx
    import respx

    env = make_env(tmp_path, [])
    cfg = env.config
    assert cfg.sources["twelvedata"].daily_budget == 750
    client = jobs.build_http(cfg, env.store)
    day = datetime.now(UTC).date()
    jobs.budget_store(env.store).save("twelvedata", day, 750)  # budget already spent
    from deskbrief.models import FetchRequest

    with respx.mock(assert_all_called=False) as router:
        route = router.get("https://example.invalid/x").mock(return_value=httpx.Response(200))
        raw = client.get(FetchRequest("twelvedata", "fx_quote"), "https://example.invalid/x")
        assert not route.called  # no request made
    assert raw.fetch_status.value == "budget_exhausted"


def test_build_store_local_and_r2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deskbrief.store.local import LocalBackend

    env = make_env(tmp_path, [])
    cfg_local = env.config.model_copy(
        update={
            "storage": env.config.storage.model_copy(update={"local_root": str(tmp_path / "x")})
        }
    )
    assert isinstance(jobs.build_store(cfg_local), LocalBackend)
    cfg_r2 = env.config.model_copy(
        update={"storage": env.config.storage.model_copy(update={"backend": "r2"})}
    )
    seen: dict[str, str] = {}

    class FakeR2:
        @staticmethod
        def from_env(bucket: str) -> str:
            seen["bucket"] = bucket
            return "r2-backend"

    monkeypatch.setattr(jobs, "R2Backend", FakeR2)
    assert jobs.build_store(cfg_r2) == "r2-backend"
    assert seen["bucket"] == "deskbrief-data"


# --- catalog ------------------------------------------------------------------------------------


def test_catalog_lists_the_nine_known_modules() -> None:
    assert [m.rsplit(".", 1)[1] for m in catalog.KNOWN_SOURCE_MODULES] == [
        "treasury", "fred", "ecb", "stooq", "cboe", "alpaca", "twelvedata", "yahoo", "cftc",
    ]  # fmt: skip


def test_load_all_skips_missing_modules_and_survives_broken_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = types.ModuleType("t19_good_source")
    broken_dep = "t19_broken_source"
    monkeypatch.setitem(sys.modules, "t19_good_source", good)

    class Raising:
        def find_spec(self, name: str, path: Any = None, target: Any = None) -> Any:
            if name == broken_dep:
                raise RuntimeError("import time failure")
            return None

    finder = Raising()
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    loaded = catalog.load_all(["t19_missing_source", broken_dep, "t19_good_source"])
    assert loaded == ["t19_good_source"]


def test_load_all_reports_a_missing_dependency_of_an_existing_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "t19_needs_dep.py").write_text("import t19_dep_that_is_not_installed\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert catalog.load_all(["t19_needs_dep"]) == []


def test_load_all_with_defaults_does_not_raise() -> None:
    assert isinstance(catalog.load_all(), list)  # works whichever source cards have merged


# --- CLI ----------------------------------------------------------------------------------------


def _drop_log_handlers() -> None:
    root = logging.getLogger()
    for h in [h for h in root.handlers if type(h).__name__ == "_RedactedHandler"]:
        root.removeHandler(h)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)  # config.yaml's ./.data resolves under tmp
    for var in ("GITHUB_EVENT_NAME", "GITHUB_EVENT_SCHEDULE", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("DESKBRIEF_STORAGE_BACKEND", raising=False)
    registry.clear()
    monkeypatch.setattr(catalog, "load_all", lambda *a, **k: [])
    _drop_log_handlers()  # configure_logging() keeps a handler on a stream pytest has closed
    yield tmp_path
    _drop_log_handlers()
    registry.clear()


def _all_sources_config() -> Path:
    """config.yaml with every source enabled (Stooq is off there, but these tests register a
    fake source under that name)."""
    path = Path(tempfile.mkdtemp()) / "config.yaml"
    text = (REPO_ROOT / "config.yaml").read_text().replace(", enabled: false}", "}")
    path.write_text(text)
    return path


CFG = ["--config", str(_all_sources_config())]


def test_cli_version_and_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip()
    assert cli.main([]) == 2


def test_cli_config_validate(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert cli.main(["config", "validate", str(REPO_ROOT / "config.yaml")]) == 0
    assert "config: ok" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 2\n")
    assert cli.main(["config", "validate", str(bad)]) == 1


def test_cli_run_dry_run_lists_plans_and_writes_nothing(cli_env: Path, capsys: Any) -> None:
    registry.register("stooq", lambda cfg: FakeSource("stooq", instrument="SPY"))
    code = cli.main(["run", "morning", "--dry-run", "--date", "2026-11-16", *CFG])
    assert code == 0
    assert "stooq/daily (SPY)" in capsys.readouterr().out
    assert not (cli_env / ".data").exists()


def test_cli_run_end_to_end_exit_codes(cli_env: Path, capsys: Any) -> None:
    registry.register("stooq", lambda cfg: FakeSource("stooq"))
    assert cli.main(["run", "eod", "--date", "2026-11-16", *CFG]) == 0
    out = capsys.readouterr().out
    assert "**ok**" in out
    assert (cli_env / ".data" / "derived" / "runs").exists()
    registry.clear()
    registry.register("stooq", lambda cfg: FakeSource("stooq", mode="raise_fetch"))
    assert cli.main(["run", "eod", "--date", "2026-11-16", *CFG]) == 1  # no source succeeded


def test_cli_sources_filter_and_unknown_source(cli_env: Path, capsys: Any) -> None:
    registry.register("stooq", lambda cfg: FakeSource("stooq"))
    registry.register("ecb", lambda cfg: FakeSource("ecb", instrument="EURUSD"))
    assert cli.main(["run", "eod", "--sources", "ecb", "--date", "2026-11-16", *CFG]) == 0
    out = capsys.readouterr().out
    assert "| ecb |" in out
    assert "| stooq |" not in out
    assert cli.main(["run", "eod", "--sources", "nope", *CFG]) == 2


def test_cli_backfill_range_and_rebuild(cli_env: Path, capsys: Any) -> None:
    registry.register("stooq", lambda cfg: FakeSource("stooq"))
    code = cli.main(["run", "backfill", "--date", "2026-11-13", "--end-date", "2026-11-17", *CFG])
    assert code == 0
    out = capsys.readouterr().out
    assert out.count("### Desk Brief `backfill`") == 3  # Fri, Mon, Tue (weekend skipped)
    code = cli.main(["run", "backfill", "--date", "2026-11-24", "--end-date", "2026-11-27", *CFG])
    assert capsys.readouterr().out.count("### Desk Brief `backfill`") == 3  # Thanksgiving skipped
    assert cli.main(["run", "rebuild", *CFG]) == 0
    assert cli.main(["run", "backfill", *CFG]) == 2  # needs --date
    assert cli.main(["run", "morning", "--end-date", "2026-11-17", *CFG]) == 2


def test_cli_bad_config_and_unavailable_storage(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.main(["run", "morning", "--config", str(cli_env / "missing.yaml")]) == 2

    def no_store(cfg: Any) -> Any:
        raise RuntimeError("R2 credentials not configured")

    monkeypatch.setattr(cli, "build_store", no_store)
    assert cli.main(["run", "morning", *CFG]) == 1


def test_rebuild_recovers_target_date_for_window_filtering_parsers(tmp_path: Path) -> None:
    pts = [
        (datetime(2026, 11, 1, 5, 0, tzinfo=UTC), 400.0),  # outside the 3-day window
        (datetime(2026, 11, 2, 5, 0, tzinfo=UTC), 401.0),
        (datetime(2026, 11, 13, 5, 0, tzinfo=UTC), 450.0),
        (datetime(2026, 11, 16, 5, 0, tzinfo=UTC), 451.0),
    ]
    env = make_env(tmp_path, [FakeSource("cboe", points=pts, windowed=True)])
    run_job(env, JobKind.BACKFILL, target_date=MONDAY, trigger=Trigger.MANUAL)
    before = Datasets(env.store).read("observations").sort_values("as_of")
    assert list(before["value"]) == [450.0, 451.0]  # window applied by the original run
    rebuilt = run_rebuild(env)
    assert rebuilt.status is RunStatus.OK
    after = Datasets(env.store).read("observations").sort_values("as_of")
    assert list(after["value"]) == list(before["value"])
    assert len([k for k in obs_keys(env, "v2")]) == len(obs_keys(env, "v1"))


def test_rebuild_falls_back_to_the_runs_row_for_target_date(tmp_path: Path) -> None:
    pts = [
        (datetime(2026, 11, 2, 5, 0, tzinfo=UTC), 401.0),
        (datetime(2026, 11, 13, 5, 0, tzinfo=UTC), 450.0),
    ]
    env = make_env(tmp_path, [FakeSource("cboe", points=pts, windowed=True)])
    run_job(env, JobKind.BACKFILL, target_date=MONDAY, trigger=Trigger.MANUAL)
    for key in env.store.list("logs/source_log/"):  # the source_log row is lost
        env.store.delete(key)
    run_rebuild(env)
    assert list(Datasets(env.store).read("observations")["value"]) == [450.0]


def test_rebuild_reports_raw_without_recoverable_target_date(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    run_job(env, JobKind.BACKFILL, target_date=MONDAY, trigger=Trigger.MANUAL)
    for key in env.store.list("logs/") + env.store.list("derived/runs/"):
        env.store.delete(key)  # simulates a hard crash before anything but raw was written
    m = run_rebuild(env)
    assert any("no recoverable target_date" in n for n in m.notes)


def test_rebuild_skips_non_ok_raw_even_for_registered_sources(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq"), FakeSource("ecb", mode="http_500")])
    run_job(env, JobKind.BACKFILL, target_date=MONDAY, trigger=Trigger.MANUAL)
    m = run_rebuild(env)  # ecb stays registered: its raw is an error body
    assert "raw skipped: 1" in " ".join(m.notes)
    assert list(Datasets(env.store).read("observations")["source"]) == ["stooq"]
    assert Datasets(env.store).read("quarantine").empty  # skipped, not parsed


def test_rebuild_works_with_the_stale_current_guard(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    run_job(env, JobKind.BACKFILL, target_date=MONDAY, trigger=Trigger.MANUAL)
    old = Datasets(env.store)
    assert old.clean_version() == "v1"
    assert run_rebuild(env).status is RunStatus.OK
    ctx = make_context(env, JobKind.BACKFILL, Trigger.MANUAL, MONDAY, NOW)
    frame = Datasets(env.store).read("observations")
    from deskbrief.store.backend import StorageError

    with pytest.raises(StorageError, match="_CURRENT"):  # the guard still protects stale writers
        old.append("observations", frame, ctx, date(2026, 11, 13))
    fresh = Datasets(env.store)
    assert fresh.clean_version() == "v2"
    # later runs write to v2 through a fresh instance
    run_job(env, JobKind.BACKFILL, target_date=MONDAY, trigger=Trigger.MANUAL)
    assert len(obs_keys(env, "v2")) == 2


def test_concurrent_pointer_move_aborts_rebuild(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    run_job(env, JobKind.BACKFILL, target_date=MONDAY, trigger=Trigger.MANUAL)
    real_get = env.store.get_bytes

    def moved(key: str) -> bytes:
        if key.endswith(".meta.json"):  # mid-rebuild, someone else flips the pointer
            env.store.put_bytes(CLEAN_CURRENT_KEY, b"v9", overwrite=True)
        return real_get(key)

    env.store.get_bytes = moved  # type: ignore[method-assign]
    m = run_rebuild(env)
    assert m.status is RunStatus.FAILED
    assert any("moved" in n for n in m.notes)
