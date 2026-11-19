"""T43 (P-018): cron-Worker dispatches are scheduled runs, and a second run of a fire is skipped.

SYNTHETIC FIXTURE: every source and value is made up; the gate and the runner are the real ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from pipeline_fakes import FakeClock, FakeSource, make_env
from structlog.testing import capture_logs

from deskbrief.models import JobKind, RunStatus, Trigger
from deskbrief.pipeline.jobs import (
    dispatcher_from_env,
    evaluate_gate,
    scheduled_for_from_env,
    trigger_from_env,
)
from deskbrief.pipeline.manifest import RunManifest
from deskbrief.pipeline.runner import DUPLICATE_FIRE, RunEnv, run_job
from deskbrief.store.datasets import Datasets

ECB_FIX = "15 12 * * 1-5"
QUARTERLY = "*/15 13-21 * * 1-5"


def utc(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")  # the Worker's slot format


def schedule_env(cron: str) -> dict[str, str]:
    return {"GITHUB_EVENT_NAME": "schedule", "GITHUB_EVENT_SCHEDULE": cron}


def worker_env(cron: str, slot: datetime) -> dict[str, str]:
    """What the workflow sets for a Worker dispatch: fire -> GITHUB_EVENT_SCHEDULE, slot."""
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_EVENT_SCHEDULE": cron,
        "DESKBRIEF_SLOT": iso(slot),
    }


def human_env() -> dict[str, str]:
    """A human dispatch: the inputs default to empty strings, so both variables are empty."""
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_EVENT_SCHEDULE": "",
        "DESKBRIEF_SLOT": "",
    }


def run_at(
    env: RunEnv, job: JobKind, now: datetime, vars_: dict[str, str], *, dry_run: bool = False
) -> RunManifest:
    env.clock = FakeClock(now - timedelta(seconds=1))
    env.env = vars_
    return run_job(env, job, dry_run=dry_run)


def runs_rows(env: RunEnv) -> list[tuple[str, str, str | None]]:
    runs = Datasets(env.store).read("runs")
    return [
        (r.job, r.status, None if pd.isna(r.scheduled_for) else iso(r.scheduled_for))
        for r in runs.itertuples()
    ]


def sources() -> list[FakeSource]:
    return [FakeSource("yahoo", dataset="quote", instrument="ES_F")]


# --- trigger and scheduled_for ----------------------------------------------------------------


def test_worker_dispatch_is_a_schedule_trigger_with_scheduled_for_the_slot() -> None:
    slot = utc(2026, 7, 15, 12, 15)
    env = worker_env(ECB_FIX, slot)
    assert trigger_from_env(env) is Trigger.SCHEDULE
    assert scheduled_for_from_env(env, slot + timedelta(minutes=25)) == slot
    assert dispatcher_from_env(env) == "cron-worker"


def test_slot_wins_over_the_cron_and_the_wall_clock() -> None:
    """A Worker dispatch for the 13:15 */15 fire that starts at 13:47 still belongs to 13:15."""
    slot = utc(2026, 11, 17, 13, 15)
    assert scheduled_for_from_env(worker_env(QUARTERLY, slot), utc(2026, 11, 17, 13, 47)) == slot
    # the GitHub event for the same cron, at the same wall clock, resolves to the last fire
    late = scheduled_for_from_env(schedule_env(QUARTERLY), utc(2026, 11, 17, 13, 47))
    assert late == utc(2026, 11, 17, 13, 45)


@pytest.mark.parametrize(
    "vars_",
    [
        human_env(),
        {"GITHUB_EVENT_NAME": "workflow_dispatch"},
        {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_EVENT_SCHEDULE": ECB_FIX},  # no slot
        {"GITHUB_EVENT_NAME": "workflow_dispatch", "DESKBRIEF_SLOT": "2026-07-15T12:15:00Z"},
        {},
    ],
)
def test_human_dispatch_stays_manual(vars_: dict[str, str]) -> None:
    assert trigger_from_env(vars_) is Trigger.MANUAL
    assert dispatcher_from_env(vars_) != "cron-worker"


def test_github_schedule_event_is_still_a_schedule_trigger() -> None:
    env = schedule_env(ECB_FIX)
    assert trigger_from_env(env) is Trigger.SCHEDULE
    assert dispatcher_from_env(env) == "github-schedule"


def test_unparseable_slot_falls_back_to_the_cron() -> None:
    env = {**worker_env(ECB_FIX, utc(2026, 7, 15, 12, 15)), "DESKBRIEF_SLOT": "not-a-time"}
    assert scheduled_for_from_env(env, utc(2026, 7, 15, 12, 40)) == utc(2026, 7, 15, 12, 15)


def test_slot_that_is_not_a_fire_of_the_cron_is_logged_and_still_used() -> None:
    env = worker_env(ECB_FIX, utc(2026, 7, 15, 12, 16))
    with capture_logs() as logs:
        assert scheduled_for_from_env(env, utc(2026, 7, 15, 12, 20)) == utc(2026, 7, 15, 12, 16)
    assert [e["event"] for e in logs] == ["slot.not_a_fire_of_cron"]


# --- same gate as a schedule event --------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "cron", "slot", "late"),
    [
        ("summer ECB fix, 25 min late", ECB_FIX, utc(2026, 7, 15, 12, 15), timedelta(minutes=25)),
        ("winter fix, */15, 9 min", QUARTERLY, utc(2026, 11, 17, 13, 15), timedelta(minutes=9)),
        ("winter 12:15 is not the fix", ECB_FIX, utc(2026, 11, 17, 12, 15), timedelta(minutes=15)),
    ],
)  # fmt: skip
def test_dispatched_run_gets_the_gate_of_the_schedule_event(
    label: str, cron: str, slot: datetime, late: timedelta
) -> None:
    started = slot + late
    dispatched = worker_env(cron, slot)
    sched_for = scheduled_for_from_env(dispatched, started)
    assert sched_for == slot, label
    by_dispatch = evaluate_gate(JobKind.INTRADAY, trigger_from_env(dispatched), started, sched_for)
    # the GitHub event for the same fire, starting equally late
    event_env = schedule_env(cron)
    event_for = scheduled_for_from_env(event_env, started)
    assert event_for == slot
    by_event = evaluate_gate(JobKind.INTRADAY, trigger_from_env(event_env), started, event_for)
    assert by_dispatch == by_event, label
    # and the decision (not the wording, which quotes the clock) of the on-time event
    on_time = evaluate_gate(JobKind.INTRADAY, Trigger.SCHEDULE, slot, slot)
    assert (by_dispatch.proceed, by_dispatch.sources) == (on_time.proceed, on_time.sources)


def test_late_dispatched_ecb_fix_run_fetches_yahoo_only(tmp_path: Path) -> None:
    alpaca = FakeSource("alpaca", dataset="iex_latest", instrument="SPY")
    yahoo = FakeSource("yahoo", dataset="quote", instrument="ES_F")
    env = make_env(tmp_path, [alpaca, yahoo])
    slot = utc(2026, 7, 15, 12, 15)
    with capture_logs() as logs:
        m = run_at(env, JobKind.INTRADAY, utc(2026, 7, 15, 12, 40), worker_env(ECB_FIX, slot))
    assert m.trigger is Trigger.SCHEDULE
    assert m.scheduled_for == slot
    assert m.schedule_delay_s is not None
    assert m.schedule_delay_s >= 25 * 60 - 1
    assert m.status is RunStatus.OK
    assert alpaca.plans == []
    assert len(yahoo.plans) == 1
    assert runs_rows(env) == [("intraday", "ok", iso(slot))]
    assert Datasets(env.store).read("runs")["trigger"].tolist() == ["schedule"]
    (trig,) = [e for e in logs if e["event"] == "run.trigger"]
    assert trig["dispatcher"] == "cron-worker"
    assert trig["trigger"] == "schedule"


def test_human_dispatch_is_ungated_and_not_deduplicated(tmp_path: Path) -> None:
    env = make_env(tmp_path, sources())
    saturday = utc(2026, 11, 21, 3, 0)
    first = run_at(env, JobKind.INTRADAY, saturday, human_env())
    second = run_at(env, JobKind.INTRADAY, saturday + timedelta(minutes=1), human_env())
    assert first.trigger is Trigger.MANUAL
    assert first.scheduled_for is None
    assert first.status is RunStatus.OK
    assert second.status is RunStatus.OK
    assert [r[0] for r in runs_rows(env)] == ["intraday", "intraday"]


# --- duplicates ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("job", "cron", "slot", "fallback_at"),
    [
        # morning: 11:15 UTC is the winter fire (06:15 ET); GitHub's event lands 5.5 h late
        (JobKind.MORNING, "15 11 * * 1-5", utc(2026, 11, 17, 11, 15), utc(2026, 11, 17, 16, 45)),
        (JobKind.EOD, "30 22 * * 1-5", utc(2026, 11, 17, 22, 30), utc(2026, 11, 17, 23, 55)),
        # the summer ECB-fix fire; the fallback lands in the NYSE session, gate open to all
        (JobKind.INTRADAY, ECB_FIX, utc(2026, 7, 15, 12, 15), utc(2026, 7, 15, 15, 0)),
    ],
)
def test_late_github_fallback_for_a_once_a_day_fire_is_a_duplicate(
    tmp_path: Path, job: JobKind, cron: str, slot: datetime, fallback_at: datetime
) -> None:
    env = make_env(tmp_path, sources())
    first = run_at(env, job, slot + timedelta(seconds=20), worker_env(cron, slot))
    assert first.status is RunStatus.OK
    before = runs_rows(env)
    assert (job.value, "ok", iso(slot)) in before

    src = sources()
    env.sources = src
    with capture_logs() as logs:
        late = run_at(env, job, fallback_at, schedule_env(cron))
    assert late.scheduled_for == slot
    assert late.skipped_reason == DUPLICATE_FIRE
    assert late.status is None
    assert late.exit_code == 0
    assert src[0].plans == []  # nothing fetched, no catch-up either
    assert runs_rows(env) == before  # no runs row for a skipped run
    assert any(e["event"] == "run.skipped" and e["reason"] == DUPLICATE_FIRE for e in logs)
    assert "**skipped**" in late.to_markdown()  # the workflows' skip test (no healthcheck ping)


def test_worker_dispatch_after_a_github_run_of_the_same_fire_is_skipped_too(tmp_path: Path) -> None:
    env = make_env(tmp_path, sources())
    slot = utc(2026, 11, 17, 22, 30)
    assert run_at(env, JobKind.EOD, slot, schedule_env("30 22 * * 1-5")).status is RunStatus.OK
    m = run_at(env, JobKind.EOD, slot + timedelta(minutes=3), worker_env("30 22 * * 1-5", slot))
    assert m.skipped_reason == DUPLICATE_FIRE


def test_late_quarterly_fallback_resolves_to_a_later_slot_and_runs(tmp_path: Path) -> None:
    env = make_env(tmp_path, sources())
    s15 = utc(2026, 11, 17, 15, 0)  # 10:00 ET, in session
    assert run_at(env, JobKind.INTRADAY, s15, worker_env(QUARTERLY, s15)).status is RunStatus.OK
    # GitHub's 15:00 event arrives at 15:20: its last fire is 15:15, which no run has taken
    late = run_at(env, JobKind.INTRADAY, utc(2026, 11, 17, 15, 20), schedule_env(QUARTERLY))
    assert late.scheduled_for == utc(2026, 11, 17, 15, 15)
    assert late.skipped_reason is None
    assert late.status is RunStatus.OK
    assert [r[2] for r in runs_rows(env)] == [iso(s15), iso(utc(2026, 11, 17, 15, 15))]


def test_late_quarterly_fallback_on_a_slot_the_worker_took_is_a_duplicate(tmp_path: Path) -> None:
    env = make_env(tmp_path, sources())
    s = utc(2026, 11, 17, 15, 15)
    assert run_at(env, JobKind.INTRADAY, s, worker_env(QUARTERLY, s)).status is RunStatus.OK
    late = run_at(env, JobKind.INTRADAY, utc(2026, 11, 17, 15, 22), schedule_env(QUARTERLY))
    assert late.skipped_reason == DUPLICATE_FIRE


def test_a_failed_run_does_not_block_the_fallback(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("yahoo", mode="raise_fetch")])
    slot = utc(2026, 11, 17, 22, 30)
    assert (
        run_at(env, JobKind.EOD, slot, worker_env("30 22 * * 1-5", slot)).status is RunStatus.FAILED
    )
    env.sources = sources()
    late = run_at(env, JobKind.EOD, utc(2026, 11, 17, 23, 40), schedule_env("30 22 * * 1-5"))
    assert late.skipped_reason is None
    assert late.status is RunStatus.OK


def test_degraded_counts_as_done(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("yahoo"), FakeSource("fred", mode="http_500")])
    slot = utc(2026, 11, 17, 22, 30)
    assert (
        run_at(env, JobKind.EOD, slot, worker_env("30 22 * * 1-5", slot)).status
        is RunStatus.DEGRADED
    )
    late = run_at(env, JobKind.EOD, utc(2026, 11, 17, 23, 40), schedule_env("30 22 * * 1-5"))
    assert late.skipped_reason == DUPLICATE_FIRE


def test_other_job_or_other_slot_is_not_a_duplicate(tmp_path: Path) -> None:
    env = make_env(tmp_path, sources())
    slot = utc(2026, 11, 17, 22, 30)
    assert run_at(env, JobKind.EOD, slot, worker_env("30 22 * * 1-5", slot)).status is RunStatus.OK
    next_day = utc(2026, 11, 18, 22, 30)
    m = run_at(env, JobKind.EOD, next_day, worker_env("30 22 * * 1-5", next_day))
    assert m.skipped_reason is None
    # the same instant for another job (a dispatched intraday with a made-up slot) still runs
    other = run_at(env, JobKind.INTRADAY, slot, {**worker_env(QUARTERLY, slot)})
    assert other.skipped_reason != DUPLICATE_FIRE


def test_dry_run_does_not_check_for_duplicates(tmp_path: Path) -> None:
    env = make_env(tmp_path, sources())
    slot = utc(2026, 11, 17, 22, 30)
    run_at(env, JobKind.EOD, slot, worker_env("30 22 * * 1-5", slot))
    m = run_at(env, JobKind.EOD, slot, schedule_env("30 22 * * 1-5"), dry_run=True)
    assert m.skipped_reason is None
    assert m.planned
