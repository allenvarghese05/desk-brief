"""T06 follow-up: check tables are flushed right after the checks hook, before the later hooks.

SYNTHETIC FIXTURE: sources and values come from ``pipeline_fakes``.
INJECTED FAULT: a hook raising a ``BaseException`` that escapes the runner's step isolation.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pipeline_fakes import FakeSource, make_env

from deskbrief.models import JobKind, RunContext, RunStatus, Trigger
from deskbrief.pipeline.jobs import CheckOutputs, JobSteps, run_checks_step
from deskbrief.pipeline.runner import RunEnv, run_job
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect

MONDAY = date(2026, 11, 16)


class Abort(BaseException):  # deliberately not an Exception: escapes `call`
    pass


def env_with(tmp_path: Path) -> RunEnv:
    return make_env(tmp_path, [FakeSource("stooq"), FakeSource("ecb", mode="raise_fetch")])


def go(env: RunEnv) -> object:
    return run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)


def files(env: RunEnv, table: str) -> list[str]:
    return [k for k in env.store.list("") if f"/{table}/" in k and k.endswith(".parquet")]


def test_later_hooks_can_read_this_runs_check_rows_through_duckdb(tmp_path: Path) -> None:
    env = env_with(tmp_path)
    seen: dict[str, tuple[list[str], int]] = {}

    def reader(name: str):  # type: ignore[no-untyped-def]
        def hook(ctx: RunContext) -> None:
            duck = connect(ctx.store, ctx.config)
            try:
                names = [
                    r[0]
                    for r in duck.execute(
                        "SELECT check_name FROM dq WHERE run_id = ?", [ctx.run_id]
                    ).fetchall()
                ]
                n_x = duck.execute(
                    "SELECT count(*) FROM xchecks WHERE run_id = ?", [ctx.run_id]
                ).fetchone()
            finally:
                duck.close()
            seen[name] = (names, int(n_x[0]) if n_x else 0)

        return hook

    env.steps_for = lambda job: JobSteps(
        run_checks=run_checks_step,
        build_serving=reader("serving"),
        build_report=lambda ctx: reader("report")(ctx),
        notify=reader("notify"),
    )
    m = go(env)
    assert m.status is RunStatus.DEGRADED  # type: ignore[attr-defined]
    for name in ("serving", "report", "notify"):
        names, n_x = seen[name]
        assert "runner.fetch" in names  # the runner's own fail row
        assert n_x > 0  # the cross-check rows
    assert len(files(env, "dq_results")) == 1  # still exactly one append
    assert len(files(env, "crosscheck_results")) == 1


def test_a_crashing_checks_hook_rows_are_visible_to_later_hooks_and_written_once(
    tmp_path: Path,
) -> None:
    env = env_with(tmp_path)
    seen: list[list[str]] = []

    def boom(ctx: RunContext) -> CheckOutputs:
        raise RuntimeError("checks blew up")

    def serving(ctx: RunContext) -> None:
        seen.append(list(Datasets(ctx.store).read("dq_results")["check_name"]))

    env.steps_for = lambda job: JobSteps(run_checks=boom, build_serving=serving)
    go(env)
    assert seen == [["runner.fetch"]]
    assert len(files(env, "dq_results")) == 1


def test_an_exception_escaping_after_the_flush_does_not_flush_twice(tmp_path: Path) -> None:
    env = env_with(tmp_path)

    def abort(ctx: RunContext) -> None:
        raise Abort

    env.steps_for = lambda job: JobSteps(run_checks=run_checks_step, build_report=abort)  # type: ignore[arg-type]
    with pytest.raises(Abort):
        go(env)
    assert len(files(env, "dq_results")) == 1  # a second append would raise KeyExistsError
    assert len(files(env, "crosscheck_results")) == 1
    assert len(files(env, "runs")) == 1  # the runs row is still written on the way out
