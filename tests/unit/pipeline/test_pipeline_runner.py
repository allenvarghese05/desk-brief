"""Runner behaviour: happy path, isolation, flush semantics, hooks, dry run, catch-up.

SYNTHETIC FIXTURE: sources and payloads come from ``pipeline_fakes``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pipeline_fakes import FakeClock, FakeSource, make_env

from deskbrief import schemas
from deskbrief.models import JobKind, RunContext, RunStatus, Trigger
from deskbrief.pipeline.jobs import JobSteps
from deskbrief.pipeline.runner import RunEnv, execute_run, make_context, run_job
from deskbrief.store import duck
from deskbrief.store.backend import KeyExistsError, StorageBackend
from deskbrief.store.datasets import Datasets

MONDAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, 11, 20, tzinfo=UTC)
FRI_CLOSE = (datetime(2026, 11, 13, 5, 0, tzinfo=UTC), 450.0)  # 00:00 ET Fri


def run(env: RunEnv, job: JobKind = JobKind.MORNING, **kw: Any) -> Any:
    return run_job(env, job, target_date=MONDAY, trigger=Trigger.MANUAL, **kw)


def table(env: RunEnv, name: str) -> Any:
    return Datasets(env.store).read(name)


def test_happy_path_writes_every_table_and_a_runs_row(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq"), FakeSource("ecb", instrument="EURUSD")])
    m = run(env)
    assert m.status is RunStatus.OK
    assert m.exit_code == 0
    obs = table(env, "observations")
    assert sorted(obs["source"]) == ["ecb", "stooq"]
    assert set(obs["run_id"]) == {m.run_id}
    assert (obs["parser_version"] == "fake-1").all()
    assert not obs["fetch_id"].isna().any()
    log = table(env, "source_log")
    assert len(log) == 2
    assert set(log["status"]) == {"ok"}
    assert (log["rows_parsed"] == 1).all()
    assert log["raw_key"].str.startswith("raw/").all()
    assert len(log["sha256"].iloc[0]) == 64
    runs = table(env, "runs")
    assert len(runs) == 1
    row = runs.iloc[0]
    assert (row["run_id"], row["status"], row["job"], row["trigger"]) == (
        m.run_id,
        "ok",
        "morning",
        "manual",
    )
    assert row["target_date"] == MONDAY
    assert (row["n_fetch"], row["n_fetch_failed"], row["n_obs"]) == (2, 0, 2)
    assert row["git_sha"] == "deadbeefcafe"
    assert len(row["config_hash"]) == 64
    raw_keys = env.store.list("raw/")
    assert len([k for k in raw_keys if k.endswith(".meta.json")]) == 2


def test_ingested_at_is_the_responded_at_of_the_fetch(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    run(env)
    obs = table(env, "observations").iloc[0]
    log = table(env, "source_log").iloc[0]
    assert obs["ingested_at"] == log["responded_at"]
    assert obs["fetch_id"] == log["fetch_id"]


def test_observations_are_split_by_et_date_of_as_of(tmp_path: Path) -> None:
    pts = [
        (datetime(2026, 11, 13, 5, 0, tzinfo=UTC), 450.0),  # Fri 00:00 ET
        (datetime(2026, 11, 14, 2, 0, tzinfo=UTC), 451.0),  # Fri 21:00 ET: UTC date is Saturday
        (datetime(2026, 11, 14, 4, 59, tzinfo=UTC), 452.0),  # Fri 23:59 ET
        (datetime(2026, 11, 14, 5, 0, tzinfo=UTC), 453.0),  # Sat 00:00 ET
    ]
    env = make_env(tmp_path, [FakeSource("stooq", points=pts)])
    run(env)
    ds = Datasets(env.store)
    fri = ds.read("observations", part_date=date(2026, 11, 13))
    sat = ds.read("observations", part_date=date(2026, 11, 14))
    assert sorted(fri["value"]) == [450.0, 451.0, 452.0]  # a UTC split would put two in Saturday
    assert sorted(sat["value"]) == [453.0]
    keys = [k for k in env.store.list("clean/v1/observations/") if k.endswith(".parquet")]
    assert len(keys) == 2  # one file per (table, ET date)


def test_empty_response_is_not_a_failure(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq", mode="empty")])
    m = run(env)
    assert m.status is RunStatus.OK
    assert table(env, "observations").empty
    log = table(env, "source_log").iloc[0]
    assert log["status"] == "empty"
    assert log["raw_key"] is None or log["raw_key"] != log["raw_key"]  # no raw for empty body


def test_two_requests_for_one_source_flush_into_one_file(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq", requests=3)])
    m = run(env)
    assert m.n_fetch == 3
    keys = [k for k in env.store.list("clean/v1/observations/") if k.endswith(".parquet")]
    assert len(keys) == 1
    assert len(table(env, "observations")) == 3


def test_rerun_gets_new_run_id_and_no_key_exists_error(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    first = run(env, JobKind.BACKFILL)  # backfill: no catch-up, so only these two runs write
    second = run(env, JobKind.BACKFILL)
    assert first.run_id != second.run_id
    assert second.status is RunStatus.OK
    assert len(table(env, "observations")) == 2  # two versions kept on disk (append-only)
    conn = duck.connect(env.store, env.config)
    latest = conn.execute("SELECT count(*) FROM obs_latest").fetchone()
    assert latest == (1,)  # the view dedupes on the natural key
    assert len(table(env, "runs")) == 2


def test_double_append_in_one_run_is_still_loud(tmp_path: Path) -> None:
    """P-008: one append per (table, date) per run; a second one raises by design."""
    env = make_env(tmp_path, [FakeSource("stooq")])
    ctx = make_context(env, JobKind.MORNING, Trigger.MANUAL, MONDAY, NOW)
    execute_run(env, ctx)
    with pytest.raises(KeyExistsError):
        Datasets(env.store).append("runs", table(env, "runs"), ctx, MONDAY)


# --- isolation -----------------------------------------------------------------------------


def test_source_raising_in_fetch_degrades_and_keeps_the_others(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("ecb", mode="raise_fetch"), FakeSource("stooq")])
    m = run(env)
    assert m.status is RunStatus.DEGRADED
    assert m.exit_code == 0
    assert list(table(env, "observations")["source"]) == ["stooq"]
    log = table(env, "source_log").set_index("source")
    assert log.loc["ecb", "status"] == "error"
    assert "socket on fire" in log.loc["ecb", "error"]
    dq = table(env, "dq_results")
    fail = dq[(dq["status"] == "fail") & (dq["source"] == "ecb")]
    assert list(fail["check_name"]) == ["runner.fetch"]
    assert m.n_dq_fail == 1


def test_source_raising_in_plan_is_isolated(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("ecb", mode="raise_plan"), FakeSource("stooq")])
    m = run(env)
    assert m.status is RunStatus.DEGRADED
    assert list(table(env, "observations")["source"]) == ["stooq"]
    dq = table(env, "dq_results")
    assert list(dq["check_name"]) == ["runner.plan"]


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("corrupt", "parse_error"),
        ("bad_schema", "schema_invalid"),
        ("parser_bug", "parser_exception"),
    ],
)
def test_unparseable_fetch_is_quarantined_with_a_dq_fail(
    tmp_path: Path, mode: str, reason: str
) -> None:
    env = make_env(tmp_path, [FakeSource("ecb", mode=mode), FakeSource("stooq")])
    m = run(env)
    assert m.status is RunStatus.DEGRADED
    assert list(table(env, "observations")["source"]) == ["stooq"]
    q = table(env, "quarantine")
    assert len(q) == 1
    assert (q.iloc[0]["source"], q.iloc[0]["reason"]) == ("ecb", reason)
    assert q.iloc[0]["raw_key"].startswith("raw/")  # the evidence is kept
    dq = table(env, "dq_results")
    assert list(dq[dq["status"] == "fail"]["check_name"]) == ["runner.parse"]
    log = table(env, "source_log").set_index("source")
    assert log.loc["ecb", "rows_parsed"] == 0


def test_429_storm_degrades_and_other_data_is_present(tmp_path: Path) -> None:
    env = make_env(
        tmp_path, [FakeSource("twelvedata", mode="http_429", requests=3), FakeSource("stooq")]
    )
    m = run(env)
    assert m.status is RunStatus.DEGRADED
    assert m.n_fetch_failed == 3
    log = table(env, "source_log")
    assert list(log[log["source"] == "twelvedata"]["status"]) == ["rate_limited"] * 3
    assert len(table(env, "observations")) == 1
    dq = table(env, "dq_results")
    assert len(dq) == 1  # identical failures collapse to one row per (source, reason)
    assert json.loads(dq.iloc[0]["details_json"])["count"] == 3


def test_http_500_keeps_the_error_body_as_raw_evidence(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("ecb", mode="http_500"), FakeSource("stooq")])
    m = run(env)
    assert m.status is RunStatus.DEGRADED
    assert (table(env, "source_log").set_index("source").loc["ecb", "raw_key"]).startswith("raw/")


def test_budget_exhausted_is_a_failed_fetch(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("twelvedata", mode="budget"), FakeSource("stooq")])
    m = run(env)
    assert m.status is RunStatus.DEGRADED
    log = table(env, "source_log").set_index("source")
    assert log.loc["twelvedata", "status"] == "budget_exhausted"


def test_every_source_failing_is_failed_with_exit_code_1(tmp_path: Path) -> None:
    env = make_env(
        tmp_path, [FakeSource("ecb", mode="raise_fetch"), FakeSource("stooq", mode="corrupt")]
    )
    m = run(env)
    assert m.status is RunStatus.FAILED
    assert m.exit_code == 1
    assert table(env, "runs").iloc[0]["status"] == "failed"  # a failed run is still recorded


def test_no_sources_is_failed(tmp_path: Path) -> None:
    env = make_env(tmp_path, [])
    m = run(env)
    assert m.status is RunStatus.FAILED
    assert any("no sources" in n for n in m.notes)


class _Boom(BaseException):
    """Escapes every ``except Exception``: stands in for a crash inside the loop."""


class _ExplodingSource(FakeSource):
    def fetch(self, req: Any, http: Any) -> Any:
        raise _Boom


def test_exception_escaping_the_loop_still_flushes_other_sources(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq"), _ExplodingSource("ecb"), FakeSource("fred")])
    with pytest.raises(_Boom):
        run(env)
    assert list(table(env, "observations")["source"]) == ["stooq"]  # fred never ran
    assert len(table(env, "source_log")) == 1
    runs = table(env, "runs")
    assert list(runs["status"]) == ["failed"]  # the aborted run is recorded as failed


# --- storage failures ----------------------------------------------------------------------


class _DeadStore:
    """INJECTED FAULT: every operation fails."""

    def _dead(self, *a: object, **k: object) -> Any:
        raise OSError("disk on fire")

    put_bytes = get_bytes = exists = list = delete = _dead


def test_unreadable_storage_fails_before_fetching(tmp_path: Path) -> None:
    src = FakeSource("stooq")
    env = make_env(tmp_path, [src])
    env.store = _DeadStore()  # type: ignore[assignment]
    m = run(env)
    assert m.status is RunStatus.FAILED
    assert m.exit_code == 1
    assert src.plans == []  # nothing fetched, no quota burned
    assert any("storage unavailable" in n for n in m.notes)


class _WriteOnlyBroken:
    """INJECTED FAULT: reads work, every write fails."""

    def __init__(self, inner: StorageBackend) -> None:
        self._inner = inner

    def put_bytes(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
        raise OSError("read-only filesystem")

    def get_bytes(self, key: str) -> bytes:
        return self._inner.get_bytes(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def list(self, prefix: str) -> list[str]:
        return self._inner.list(prefix)

    def delete(self, key: str) -> None:
        self._inner.delete(key)


def test_nothing_writable_is_failed(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    env.store = _WriteOnlyBroken(env.store)  # type: ignore[assignment]
    m = run(env)
    assert m.status is RunStatus.FAILED
    assert m.storage_failed > 0
    assert any("storage unavailable" in n for n in m.notes)


def test_one_partition_failing_to_flush_only_degrades(tmp_path: Path) -> None:
    class FlakyOnce(_WriteOnlyBroken):
        def put_bytes(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
            if "dq_results" in key:
                raise OSError("one bad partition")
            self._inner.put_bytes(key, data, overwrite=overwrite)

    env = make_env(tmp_path, [FakeSource("stooq"), FakeSource("ecb", mode="raise_fetch")])
    env.store = FlakyOnce(env.store)  # type: ignore[assignment]
    m = run(env)
    assert m.status is RunStatus.DEGRADED
    assert any("flush failed" in n for n in m.notes)
    assert len(table(env, "observations")) == 1  # the other partitions were still written


# --- hooks ---------------------------------------------------------------------------------


def test_hooks_run_in_order_after_the_flush_and_report_key_lands_on_the_run(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    seen: list[str] = []

    def checks(ctx: RunContext) -> None:
        # the clean rows are already flushed when checks run
        seen.append(f"checks:{len(Datasets(ctx.store).read('observations'))}")

    def report(ctx: RunContext) -> str:
        seen.append("report")
        return "reports/date=2026-11-16/v1/brief.html"

    env.steps_for = lambda job: JobSteps(
        run_checks=checks,
        build_serving=lambda ctx: seen.append("serving"),
        build_report=report,
        notify=lambda ctx: seen.append("notify"),
    )
    m = run(env)
    assert seen == ["checks:1", "serving", "report", "notify"]
    assert m.status is RunStatus.OK
    assert table(env, "runs").iloc[0]["report_key"] == "reports/date=2026-11-16/v1/brief.html"


def test_failing_hook_degrades_but_does_not_stop_later_hooks(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    ran: list[str] = []

    def bad(ctx: RunContext) -> None:
        raise RuntimeError("checks blew up")

    env.steps_for = lambda job: JobSteps(run_checks=bad, notify=lambda c: ran.append("notify"))
    m = run(env)
    assert m.status is RunStatus.DEGRADED
    assert ran == ["notify"]
    assert any("checks blew up" in n for n in m.notes)


def test_default_hooks_are_noops(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    assert run(env).status is RunStatus.OK


# --- dry run and summary -------------------------------------------------------------------


def test_dry_run_plans_only(tmp_path: Path) -> None:
    src = FakeSource("stooq", requests=2)
    env = make_env(tmp_path, [src, FakeSource("ecb", mode="raise_plan")])
    m = run(env, dry_run=True)
    assert m.planned == ["stooq/daily (SPY)", "stooq/daily (SPY)"]
    assert m.status is RunStatus.DEGRADED  # a plan that raises is reported even in a dry run
    assert env.store.list("") == []  # nothing written at all


def test_step_summary_is_appended_when_github_sets_it(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    env = make_env(
        tmp_path,
        [FakeSource("stooq"), FakeSource("ecb", mode="corrupt")],
        env={"GITHUB_STEP_SUMMARY": str(summary)},
    )
    run(env)
    text = summary.read_text()
    assert "**degraded**" in text
    assert "| stooq | 1 | 0 | 1 | 0 |" in text
    assert "parse_error" in text  # the problem is listed, not just counted


def test_no_summary_without_the_env_var(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    run(env)  # must not raise or create files outside the store
    assert not (tmp_path / "summary.md").exists()


# --- gates, delay ---------------------------------------------------------------------------


def _sched(env: RunEnv, job: JobKind, now: datetime, **kw: Any) -> Any:
    env.clock = FakeClock(now)
    return run_job(env, job, trigger=Trigger.SCHEDULE, **kw)


@pytest.mark.parametrize(
    ("now", "proceeds"),
    [
        (datetime(2026, 11, 16, 11, 15, tzinfo=UTC), True),  # EST: 06:15 ET
        (datetime(2026, 11, 16, 10, 15, tzinfo=UTC), False),  # EST: 05:15 ET, too early
        (datetime(2026, 11, 16, 12, 15, tzinfo=UTC), False),  # EST: 07:15 ET, too late
        (datetime(2026, 7, 15, 10, 15, tzinfo=UTC), True),  # EDT: 06:15 ET
        (datetime(2026, 7, 15, 11, 15, tzinfo=UTC), False),  # EDT: 07:15 ET
        (datetime(2026, 11, 14, 11, 15, tzinfo=UTC), False),  # Saturday
        (datetime(2026, 11, 26, 11, 15, tzinfo=UTC), False),  # Thanksgiving
    ],
)
def test_morning_gate_is_dst_safe_and_skips_without_a_runs_row(
    tmp_path: Path, now: datetime, proceeds: bool
) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    m = _sched(env, JobKind.MORNING, now)
    if proceeds:
        assert m.status is RunStatus.OK
        assert len(table(env, "runs")) == 1
    else:
        assert m.status is None
        assert m.skipped_reason
        assert m.exit_code == 0
        assert env.store.list("") == []  # no fetch, no runs row
        assert env.sources[0].plans == []  # type: ignore[attr-defined]


def test_manual_trigger_bypasses_the_gate(tmp_path: Path) -> None:
    env = make_env(
        tmp_path, [FakeSource("stooq")], clock=FakeClock(datetime(2026, 11, 14, 3, 0, tzinfo=UTC))
    )
    m = run_job(env, JobKind.MORNING, trigger=Trigger.MANUAL)
    assert m.status is RunStatus.OK


def test_trigger_defaults_from_github_event_name(tmp_path: Path) -> None:
    env = make_env(
        tmp_path,
        [FakeSource("stooq")],
        env={"GITHUB_EVENT_NAME": "schedule"},
        clock=FakeClock(datetime(2026, 11, 14, 3, 0, tzinfo=UTC)),  # Saturday night
    )
    assert run_job(env, JobKind.MORNING).status is None  # treated as scheduled, so gated


def test_intraday_gate_follows_the_nyse_session(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    assert (
        _sched(env, JobKind.INTRADAY, datetime(2026, 11, 16, 15, 0, tzinfo=UTC)).status
        is RunStatus.OK
    )
    assert _sched(env, JobKind.INTRADAY, datetime(2026, 11, 16, 22, 0, tzinfo=UTC)).status is None
    assert _sched(env, JobKind.INTRADAY, datetime(2026, 11, 14, 15, 0, tzinfo=UTC)).status is None


def test_eod_gate_skips_weekends(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    assert (
        _sched(env, JobKind.EOD, datetime(2026, 11, 16, 22, 30, tzinfo=UTC)).status is RunStatus.OK
    )
    assert _sched(env, JobKind.EOD, datetime(2026, 11, 15, 22, 30, tzinfo=UTC)).status is None


def test_schedule_delay_comes_from_github_event_schedule(tmp_path: Path) -> None:
    env = make_env(
        tmp_path,
        [FakeSource("stooq")],
        env={"GITHUB_EVENT_SCHEDULE": "15 10,11 * * 1-5"},
        clock=FakeClock(datetime(2026, 11, 16, 11, 21, 40, tzinfo=UTC)),
    )
    m = run_job(env, JobKind.MORNING, trigger=Trigger.SCHEDULE)
    # FakeClock advances 1 s per call: started at 11:21:41, scheduled 11:15:00
    assert m.scheduled_for == datetime(2026, 11, 16, 11, 15, tzinfo=UTC)
    assert m.schedule_delay_s == pytest.approx(401, abs=3)
    row = table(env, "runs").iloc[0]
    assert row["schedule_delay_s"] == pytest.approx(m.schedule_delay_s)
    assert row["scheduled_for"] == m.scheduled_for


def test_manual_runs_have_no_schedule_delay(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")], env={"GITHUB_EVENT_SCHEDULE": "15 11 * * *"})
    m = run(env)
    assert m.schedule_delay_s is None
    assert (
        table(env, "runs").iloc[0]["schedule_delay_s"]
        != table(env, "runs").iloc[0]["schedule_delay_s"]
    )  # NaN


# --- request redaction in source_log ----------------------------------------------------------


def test_source_log_request_is_json_without_secrets(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    run(env)
    req = json.loads(table(env, "source_log").iloc[0]["request_redacted"])
    assert req == {
        "url": "https://example.invalid/stooq",
        "params": {"n": "0"},
        "target_date": "2026-11-16",
        "window": None,
    }


def test_runs_row_validates_against_the_schema(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    run(env)
    schemas.validate("runs", table(env, "runs"))


def test_flush_failure_of_observations_makes_the_run_failed(tmp_path: Path) -> None:
    class FailObs(_WriteOnlyBroken):
        def put_bytes(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
            if "clean/v1/observations/" in key:
                raise OSError("disk full")
            self._inner.put_bytes(key, data, overwrite=overwrite)

    env = make_env(tmp_path, [FakeSource("stooq")])
    env.store = FailObs(env.store)  # type: ignore[assignment]
    m = run(env)
    assert m.status is RunStatus.FAILED
    assert m.exit_code == 1
    assert table(env, "runs").iloc[0]["status"] == "failed"
    from deskbrief.pipeline.catchup import missed_dates

    # the date is not covered, so the next scheduled run retries it
    next_day = date(2026, 11, 17)
    assert MONDAY in missed_dates(table(env, "runs"), JobKind.MORNING, next_day)


def test_dry_run_with_no_sources_is_failed_like_a_real_run(tmp_path: Path) -> None:
    env = make_env(tmp_path, [])
    assert run(env, dry_run=True).status is RunStatus.FAILED


def test_unwritable_step_summary_never_fails_the_run(tmp_path: Path) -> None:
    env = make_env(
        tmp_path,
        [FakeSource("stooq")],
        env={"GITHUB_STEP_SUMMARY": str(tmp_path / "no_such_dir" / "summary.md")},
    )
    assert run(env).status is RunStatus.OK


def test_morning_gate_uses_the_scheduled_time_so_a_delayed_cron_still_runs(
    tmp_path: Path,
) -> None:
    env = make_env(
        tmp_path,
        [FakeSource("stooq")],
        env={"GITHUB_EVENT_SCHEDULE": "15 10,11 * * 1-5"},
    )
    # EST: the 11:15 UTC fire (06:15 ET) starts 2 h late, at 08:20 ET; the gate looks at the
    # fire time (06:15 ET), not the wall clock, so the run proceeds
    m = _sched(env, JobKind.MORNING, datetime(2026, 11, 16, 13, 20, tzinfo=UTC))
    assert m.status is RunStatus.OK
    # T43 (P-018): the same fire again on the same store is a duplicate, not a second report
    env.clock = FakeClock(datetime(2026, 11, 16, 11, 50, tzinfo=UTC))
    assert (
        run_job(env, JobKind.MORNING, trigger=Trigger.SCHEDULE).skipped_reason == "duplicate fire"
    )
    fresh = make_env(
        tmp_path / "fresh",
        [FakeSource("stooq")],
        env={"GITHUB_EVENT_SCHEDULE": "15 10,11 * * 1-5"},
        clock=FakeClock(datetime(2026, 11, 16, 11, 50, tzinfo=UTC)),  # 35 min late
    )
    assert run_job(fresh, JobKind.MORNING, trigger=Trigger.SCHEDULE).status is RunStatus.OK


def test_eod_runs_on_an_xnyse_holiday_when_target_is_open(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    thanksgiving = datetime(2026, 11, 26, 22, 30, tzinfo=UTC)
    assert _sched(env, JobKind.EOD, thanksgiving).status is RunStatus.OK  # TARGET is open
    christmas = datetime(2026, 12, 25, 22, 30, tzinfo=UTC)
    assert _sched(env, JobKind.EOD, christmas).status is None  # every calendar is closed
