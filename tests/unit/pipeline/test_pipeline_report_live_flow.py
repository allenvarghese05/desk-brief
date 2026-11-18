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
    rows = model.rows()
    assert set(diag) == {
        "event",
        "log_level",
        "run_id",
        "clean_version",
        "obs_files",
        "obs_rows_visible",
        "rows_available",
        "rows_unavailable",
    }
    assert (diag["clean_version"], diag["obs_files"], diag["obs_rows_visible"]) == ("v1", 1, 1)
    assert diag["rows_available"] == sum(r.available for r in rows) >= 1
    assert diag["rows_unavailable"] == sum(not r.available for r in rows)


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


def test_check_issues_groups_by_kind_status_check_and_source() -> None:
    import pandas as pd

    from deskbrief.pipeline.clean import RunBuffer

    def dq(check: str, status: str, source: str | None) -> dict[str, object]:
        return {"check_name": check, "status": status, "source": source}

    buf = RunBuffer()
    for row in (
        dq("completeness", "fail", "stooq"),
        dq("completeness", "fail", "stooq"),
        dq("completeness", "fail", "fred"),
        dq("freshness", "fail", "stooq"),
        dq("completeness", "pass", "stooq"),  # ignored
        dq("fresh", "warn", None),
    ):
        buf.add("dq_results", row)  # type: ignore[arg-type]
    buf.add_frame(  # the frame path used by the check hooks
        "dq_results",
        pd.DataFrame([dq("completeness", "fail", "stooq"), dq("validity", "skip", "x")]),
    )
    buf.add_frame(
        "crosscheck_results",
        pd.DataFrame(
            [
                {"check_name": "proxy_vs_exact", "status": "fail"},
                {"check_name": "proxy_vs_exact", "status": "fail"},
                {"check_name": "proxy_vs_exact", "status": "pass"},
                {"check_name": "fx_vs_ecb", "status": "warn"},
            ]
        ),
    )
    got = [(i.kind, i.status, i.check, i.source, i.count) for i in buf.check_issues()]
    assert got == [
        ("dq", "fail", "completeness", "stooq", 3),
        ("crosscheck", "fail", "proxy_vs_exact", None, 2),
        (
            "dq",
            "fail",
        )
        + ("completeness", "fred", 1),
        ("dq", "fail", "freshness", "stooq", 1),
        ("crosscheck", "warn", "fx_vs_ecb", None, 1),
        ("dq", "warn", "fresh", None, 1),
    ]
