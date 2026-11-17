"""T06: checks and cross-checks wired into the runner (P-008 single dq_results append).

SYNTHETIC FIXTURE: every source and value is made up; the checks themselves are the real ones.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pipeline_fakes import FakeSource, make_env

from deskbrief.models import JobKind, RunContext, RunStatus, Trigger
from deskbrief.pipeline.jobs import CheckOutputs, JobSteps, steps_for
from deskbrief.pipeline.runner import RunEnv, run_job
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.store.datasets import Datasets
from deskbrief.store.local import LocalBackend

MONDAY = date(2026, 11, 16)
FRI_CLOSE = (datetime(2026, 11, 13, 5, 0, tzinfo=UTC), 450.0)


def go(env: RunEnv, job: JobKind = JobKind.EOD) -> Any:
    return run_job(env, job, target_date=MONDAY, trigger=Trigger.MANUAL)


def rows(env: RunEnv, table: str) -> pd.DataFrame:
    return Datasets(env.store).read(table)


def files(env: RunEnv, table: str, day: date) -> list[str]:
    tag = f"/{table}/date={day.isoformat()}/"
    return [k for k in env.store.list("") if tag in k and k.endswith(".parquet")]


def real_env(tmp_path: Path, *sources: FakeSource) -> RunEnv:
    return make_env(tmp_path, list(sources) or [FakeSource("stooq")], real_steps=True)


def test_every_job_gets_the_checks_hook() -> None:
    assert all(steps_for(job).run_checks is not JobSteps().run_checks for job in JobKind)


def test_one_dq_results_append_per_date_with_checks_and_crosschecks(tmp_path: Path) -> None:
    env = real_env(tmp_path, FakeSource("stooq"), FakeSource("ecb", mode="raise_fetch"))
    m = go(env)
    assert len(files(env, "dq_results", MONDAY)) == 1  # runner rows + checks + consistency
    dq = rows(env, "dq_results")
    assert set(dq["run_id"]) == {m.run_id}
    assert "runner.fetch" in set(dq["check_name"])  # the runner's own fail row is in the file
    assert {"freshness", "completeness"} & set(dq["check_type"])  # T20 checks ran
    x = rows(env, "crosscheck_results")
    assert len(x) > 0
    assert len(files(env, "crosscheck_results", MONDAY)) == 1
    assert (dq["check_type"] == "consistency").sum() == len(x)  # one mirror row per cross-check


def test_check_written_rows_are_counted_in_the_runs_row(tmp_path: Path) -> None:
    env = real_env(tmp_path)
    m = go(env)
    dq = rows(env, "dq_results")
    run = rows(env, "runs").iloc[0]
    assert m.n_dq_fail == (dq["status"] == "fail").sum() == run["n_dq_fail"]
    assert m.n_dq_warn == (dq["status"] == "warn").sum() == run["n_dq_warn"]
    runner_only = dq[dq["check_name"].str.startswith("runner.")]
    assert m.n_dq_fail + m.n_dq_warn > 0
    assert len(dq) > len(
        runner_only
    )  # check rows are in the file and counted, not only the runner's


def test_rerun_adds_no_duplicate_check_rows(tmp_path: Path) -> None:
    env = real_env(tmp_path)
    first = go(env)
    n_first = int((rows(env, "dq_results")["run_id"] == first.run_id).sum())
    second = go(env)
    dq = rows(env, "dq_results")
    assert first.run_id != second.run_id
    assert int((dq["run_id"] == first.run_id).sum()) == n_first  # the first run is untouched
    assert int((dq["run_id"] == second.run_id).sum()) == n_first  # one set per run, no doubling
    key = ["run_id", "check_name", "source", "instrument_id", "details_json"]
    assert not dq.duplicated(key).any()
    assert rows(env, "revisions").empty  # identical data is not a revision


def test_revisions_partition_by_et_date_of_detected_at(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq")])
    detected = datetime(2026, 11, 16, 11, 21, tzinfo=UTC)
    rev = to_frame(
        "revisions",
        [
            {
                "instrument_id": "SPY", "source": "stooq", "dataset": "daily", "field": "close",
                "interval": "1d", "as_of": FRI_CLOSE[0], "old_value": 1.0, "new_value": 2.0,
                "old_fetch_id": "a", "new_fetch_id": "b", "detected_at": detected,
                "run_id": "r",
            }
        ],
    )  # fmt: skip

    def hook(ctx: RunContext) -> CheckOutputs:
        return CheckOutputs(empty_frame("dq_results"), rev, empty_frame("crosscheck_results"))

    env.steps_for = lambda job: JobSteps(run_checks=hook)
    assert go(env).status is RunStatus.OK
    assert len(files(env, "revisions", MONDAY)) == 1
    assert len(rows(env, "revisions")) == 1


def test_a_crashing_checks_hook_still_flushes_the_runner_rows_once(tmp_path: Path) -> None:
    env = make_env(tmp_path, [FakeSource("stooq"), FakeSource("ecb", mode="raise_fetch")])

    def boom(ctx: RunContext) -> CheckOutputs:
        raise RuntimeError("checks blew up")

    env.steps_for = lambda job: JobSteps(run_checks=boom)
    m = go(env)
    assert m.status is RunStatus.DEGRADED
    assert len(files(env, "dq_results", MONDAY)) == 1
    assert list(rows(env, "dq_results")["check_name"]) == ["runner.fetch"]
    assert m.n_dq_fail == 1


class _FailsOn(LocalBackend):
    """Local store whose ``put_bytes`` raises for keys containing ``needle``."""

    def __init__(self, root: Path, needle: str) -> None:
        super().__init__(root)
        self._needle = needle

    def put_bytes(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
        if self._needle in key:
            raise OSError(f"injected: {self._needle} unwritable")
        super().put_bytes(key, data, overwrite=overwrite)


@pytest.mark.parametrize(
    ("needle", "status"),
    [
        ("/source_log/", RunStatus.FAILED),
        ("/quarantine/", RunStatus.FAILED),
        ("/dq_results/", RunStatus.DEGRADED),
        ("/crosscheck_results/", RunStatus.DEGRADED),
    ],
)
def test_flush_loss_status_by_table(tmp_path: Path, needle: str, status: RunStatus) -> None:
    """INJECTED FAULT: data tables lost -> failed (catch-up retries); check tables -> degraded."""
    env = real_env(tmp_path, FakeSource("stooq"), FakeSource("ecb", mode="corrupt"))
    env.store = _FailsOn(tmp_path / "data", needle)
    m = go(env)
    assert m.status is status
    assert any("flush failed" in n for n in m.notes)
    assert (
        len(Datasets(LocalBackend(tmp_path / "data")).read("observations")) == 1
    )  # unrelated partitions still written
