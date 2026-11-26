"""T31: the morning report hooks (build, archive per spec §6, notify) and the runner.

SYNTHETIC FIXTURE: observations come from ``brief_synth``/``pipeline_fakes`` (made-up values).
INJECTED FAULT: a notifier that raises, with the (made-up) SMTP password in its error text.
No network and no real sends: SMTP is replaced by ``NullNotifier`` or a failing fake.
"""

from __future__ import annotations

import dataclasses
import io
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pipeline_fakes import FakeSource, make_env

from brief_synth import MON, MON_0700, World, _ctx, monday_rows
from deskbrief.logging import configure_logging
from deskbrief.models import JobKind, RunContext, RunStatus, Trigger
from deskbrief.pipeline.jobs import JobSteps, steps_for
from deskbrief.pipeline.runner import run_job
from deskbrief.report.model import BriefModel
from deskbrief.report.notify import (
    Attachment,
    NotifyConfigError,
    NullNotifier,
    report_steps,
)
from deskbrief.store.backend import StorageBackend

PASSWORD = "hunter2-synthetic-app-password"
PREFIX = "reports/date=2026-11-16"


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path / "w")
    w.observations(monday_rows())
    w.dq(MON, [("fresh", "warn", "yahoo", "ES_F")])
    return w


@pytest.fixture
def ctx(world: World) -> RunContext:
    assert datetime(2026, 11, 16, 12, 0, tzinfo=UTC) == MON_0700  # the context's now_utc
    return _ctx(world.config, world.store, "RUNT31", MON)


def keys(store: StorageBackend) -> list[str]:
    return [k for k in store.list("reports/")]


class FailingNotifier:
    def send(
        self, subject: str, html: str, text: str, attachments: Sequence[Attachment] = ()
    ) -> None:
        raise RuntimeError(f"relay refused login, password was {PASSWORD}")


def test_only_the_morning_row_gets_the_report_hooks() -> None:
    default = JobSteps()
    morning = steps_for(JobKind.MORNING)
    assert morning.build_report is not default.build_report
    assert morning.notify is not default.notify
    for job in set(JobKind) - {JobKind.MORNING}:
        steps = steps_for(job)
        assert (steps.build_report, steps.notify) == (default.build_report, default.notify)


def test_build_archives_html_xlsx_and_model_json_per_spec_6(
    ctx: RunContext, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # T66: never a live call here
    build, _ = report_steps(NullNotifier())
    key = build(ctx)
    assert key == f"{PREFIX}/v1/brief.html"
    assert keys(world.store) == [
        f"{PREFIX}/v1/{n}" for n in ("brief.html", "brief.xlsx", "model.json")
    ]
    model = BriefModel.from_store(world.connect(), world.config, MON, MON_0700)
    archived = BriefModel.from_json(world.store.get_bytes(f"{PREFIX}/v1/model.json").decode())
    # P-024 (T66): the note is written in this step and stored with the brief
    assert archived.note is not None
    assert archived.note.reason == "no API key is configured"
    assert replace(archived, note=None) == model
    assert world.store.get_bytes(f"{PREFIX}/v1/brief.xlsx")[:2] == b"PK"
    assert model.subject in world.store.get_bytes(key).decode()  # T63: the read is the title


def test_a_rerun_archives_a_new_version(ctx: RunContext, world: World) -> None:
    assert report_steps(NullNotifier())[0](ctx) == f"{PREFIX}/v1/brief.html"
    assert report_steps(NullNotifier())[0](ctx) == f"{PREFIX}/v2/brief.html"
    assert len(keys(world.store)) == 6


def test_notify_sends_the_archived_html_with_the_workbook_attached(
    ctx: RunContext, world: World
) -> None:
    null = NullNotifier()
    build, notify = report_steps(null)
    key = build(ctx)
    notify(ctx)
    (msg,) = null.messages
    assert msg.html == world.store.get_bytes(key).decode()
    assert msg.subject == "Quiet · Desk Brief · Mon 16 Nov"  # P-024 (T63): the read first
    assert [a.filename for a in msg.attachments] == ["brief-2026-11-16.xlsx"]
    assert msg.attachments[0].data == world.store.get_bytes(f"{PREFIX}/v1/brief.xlsx")
    assert "Not investment advice" in msg.text


def test_notify_does_not_send_a_stale_report_when_this_run_built_none(
    ctx: RunContext, world: World
) -> None:
    report_steps(NullNotifier())[0](ctx)  # an earlier run's archived report
    null = NullNotifier()
    _, notify = report_steps(null)  # this run's build never happened
    notify(ctx)
    assert null.messages == []


def test_email_still_goes_out_when_archiving_fails(ctx: RunContext, world: World) -> None:
    null = NullNotifier()
    build, notify = report_steps(null)

    def boom(*a: object, **k: object) -> None:
        raise OSError("storage down")

    object.__setattr__(world.store, "put_bytes", boom)
    with pytest.raises(OSError, match="storage down"):
        build(ctx)
    notify(ctx)
    assert len(null.messages) == 1


def test_unconfigured_smtp_is_a_null_send_for_manual_runs_but_an_error_for_cron(
    ctx: RunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("SMTP_USER", "SMTP_APP_PASSWORD", "REPORT_RECIPIENTS"):
        monkeypatch.delenv(name, raising=False)
    build, notify = report_steps()
    build(ctx)
    notify(ctx)  # manual: logged skip, no error
    scheduled = dataclasses.replace(ctx, trigger=Trigger.SCHEDULE)
    build, notify = report_steps()
    build(scheduled)
    with pytest.raises(NotifyConfigError, match="SMTP_APP_PASSWORD"):
        notify(scheduled)


def test_email_failure_degrades_the_run_alerts_and_keeps_the_archived_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_APP_PASSWORD", PASSWORD)  # the repo's redaction masks env secrets
    log = io.StringIO()
    configure_logging(stream=log)
    env = make_env(tmp_path, [FakeSource("stooq")])
    build, notify = report_steps(FailingNotifier())
    env.steps_for = lambda job: JobSteps(build_report=build, notify=notify)
    m = run_job(env, JobKind.MORNING, target_date=date(2026, 11, 16), trigger=Trigger.MANUAL)
    assert m.status is RunStatus.DEGRADED
    assert any(n.startswith("step failed: notify: RuntimeError") for n in m.notes)
    assert PASSWORD not in " ".join(m.notes)
    assert m.report_key == f"{PREFIX}/v1/brief.html"
    assert env.store.exists(f"{PREFIX}/v1/brief.html")
    assert env.store.exists(f"{PREFIX}/v1/brief.xlsx")
    alert = [line for line in log.getvalue().splitlines() if "report.email_failed" in line]
    assert alert
    assert '"level": "error"' in alert[0]
    assert PASSWORD not in log.getvalue()
    configure_logging()  # restore the default stream for later tests
