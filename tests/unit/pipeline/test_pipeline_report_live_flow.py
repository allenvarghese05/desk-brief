"""T25: the morning report must see the observations this very run ingested.

SYNTHETIC FIXTURE: one fake source with made-up prices (``pipeline_fakes``); a local store.
Regression for the first live run: ``ctx.now_utc`` is the run *start*, observations get
``ingested_at = responded_at`` (later), so a point-in-time cut at ``ctx.now_utc`` hid all of them
and every instrument read "data unavailable".
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pipeline_fakes import FakeSource, make_env
from structlog.testing import capture_logs

from deskbrief.models import JobKind, RunStatus, Trigger
from deskbrief.pipeline.runner import RunEnv, run_job
from deskbrief.report.model import BriefModel
from deskbrief.store.backend import StorageBackend
from deskbrief.timeutil import utc_now

MONDAY = date(2026, 11, 16)


def _env(tmp_path: Path) -> RunEnv:
    # the fake source stamps responded_at with the real time, so the run clock must be real too
    return make_env(
        tmp_path, [FakeSource("stooq", instrument="SPY")], real_steps=True, clock=utc_now
    )


def _model(store: StorageBackend) -> BriefModel:
    (key,) = [k for k in store.list("reports/") if k.endswith("model.json")]
    return BriefModel.from_json(store.get_bytes(key).decode())


def test_empty_store_one_morning_run_gives_available_rows(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)
    assert result.status in (RunStatus.OK, RunStatus.DEGRADED)
    model = _model(env.store)
    spy = next(r for r in model.rows() if r.instrument_id == "SPY")
    assert spy.available, "the run's own observation was invisible to the report"
    assert utc_now() >= model.report_time  # the cut is a real instant, not in the future


def test_report_diagnostic_counts_are_logged(tmp_path: Path) -> None:
    env = _env(tmp_path)
    with capture_logs() as logs:
        run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)
    (diag,) = [e for e in logs if e["event"] == "report.diagnostic"]
    model = _model(env.store)
    assert diag["clean_version"] == "v1"
    assert diag["obs_files"] == 1
    assert diag["obs_rows_visible"] == 1
    assert diag["rows_available"] == sum(r.available for r in model.rows()) >= 1
    assert diag["rows_unavailable"] == sum(not r.available for r in model.rows())
    assert all(isinstance(v, int | str) for v in diag.values() if v is not diag["event"])
    assert "450" not in str({k: v for k, v in diag.items() if k != "timestamp"})  # counts only


def test_manifest_groups_check_failures_by_check_name_with_counts(tmp_path: Path) -> None:
    env = _env(tmp_path)
    manifest = run_job(env, JobKind.MORNING, target_date=MONDAY, trigger=Trigger.MANUAL)
    issues = manifest.check_issues
    assert issues, "a run with failing checks must list them"
    fails = [i for i in issues if i.status == "fail"]
    assert sum(i.count for i in fails if i.kind == "dq") == manifest.n_dq_fail
    assert all(i.count > 0 and i.check for i in issues)
    keys = [(i.kind, i.status, i.check, i.source) for i in issues]
    assert len(keys) == len(set(keys))  # one line per (kind, status, check, source)
    text = manifest.to_markdown()
    top = fails[0]
    assert f"`{top.check}`" in text
    assert f": {top.count}" in text


def test_manifest_markdown_check_table_has_counts_and_names_only() -> None:
    from datetime import UTC, datetime

    from deskbrief.models import RunStatus
    from deskbrief.pipeline.manifest import CheckIssue, RunManifest

    m = RunManifest(
        "R1", JobKind.MORNING, Trigger.MANUAL, MONDAY, datetime(2026, 11, 16, tzinfo=UTC)
    )
    m.status = RunStatus.DEGRADED
    m.check_issues = [
        CheckIssue("dq", "fail", "completeness", "stooq", 12),
        CheckIssue("dq", "warn", "fresh", None, 2),
        CheckIssue("crosscheck", "fail", "proxy_vs_exact", None, 3),
    ]
    md = m.to_markdown()
    assert "- dq fail `completeness` (source stooq): 12" in md
    assert "- dq warn `fresh`: 2" in md
    assert "- crosscheck fail `proxy_vs_exact`: 3" in md
