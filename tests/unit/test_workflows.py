"""T24B: the intraday, eod, sla, backfill and rebuild workflows (spec §5.4, §11, P-012, P-013).

The workflows cannot be dispatched from a branch, so these tests parse the YAML and *execute*
its shell and Python steps against stubs: ``uv`` is a recording script, the store is local.

SYNTHETIC FIXTURE: the report keys and dates below are made up.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from deskbrief import cli
from deskbrief.config import ENV_OVERRIDES
from deskbrief.pipeline.jobs import last_cron_fire
from deskbrief.sources import alpaca, fred, twelvedata
from deskbrief.store.backend import report_key
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_date

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
NAMES = ("intraday", "eod", "sla", "backfill", "rebuild")

#: Names of the GitHub Actions secrets the owner created.
SECRETS = {
    "ALPACA_KEY_ID", "ALPACA_SECRET_KEY", "TWELVEDATA_API_KEY", "FRED_API_KEY",
    "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
    "R2_READER_ACCESS_KEY_ID", "R2_READER_SECRET_ACCESS_KEY",
    "HEALTHCHECK_URL_MORNING", "HEALTHCHECK_URL_EOD",
    "SMTP_USER", "SMTP_APP_PASSWORD", "SMTP_HOST", "SMTP_PORT", "REPORT_RECIPIENTS",
    "DESKBRIEF_DISPLAY_MODE", "DESKBRIEF_STORAGE_BACKEND",
}  # fmt: skip

#: Env var names the code reads (from the code's own constants where it has them).
CODE_ENV = {
    alpaca.KEY_ID_ENV, alpaca.SECRET_KEY_ENV, twelvedata.API_KEY_ENV, fred.API_KEY_ENV,
    *ENV_OVERRIDES,
    "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_READER_ACCESS_KEY_ID", "R2_READER_SECRET_ACCESS_KEY",
    "SMTP_USER", "SMTP_APP_PASSWORD", "SMTP_HOST", "SMTP_PORT", "REPORT_RECIPIENTS",
    "GITHUB_EVENT_SCHEDULE", "PYTHONUNBUFFERED", "SLA_DATE",
    "IN_START", "IN_END", "IN_SOURCES", "IN_FROM_DATE", "HC_URL",
}  # fmt: skip


def load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text())
    assert isinstance(data, dict)
    data["on"] = data.pop(True)  # YAML 1.1 reads the key ``on`` as a boolean
    return data


def job(name: str) -> dict[str, Any]:
    jobs = load(name)["jobs"]
    assert list(jobs) == [name]
    return jobs[name]  # type: ignore[no-any-return]


def run_of(name: str, step_name: str) -> str:
    steps = [s for s in job(name)["steps"] if s.get("name") == step_name]
    assert len(steps) == 1, step_name
    return steps[0]["run"]  # type: ignore[no-any-return]


def crons(name: str) -> list[str]:
    return [t["cron"] for t in load(name)["on"]["schedule"]]


# --- every workflow ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_shared_conventions(name: str) -> None:
    wf = load(name)
    assert wf["concurrency"] == {"group": "deskbrief-pipeline", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "read"}
    assert "workflow_dispatch" in wf["on"]
    j = job(name)
    assert j["timeout-minutes"] <= 360
    assert j["runs-on"] == "ubuntu-latest"
    assert j["env"]["DESKBRIEF_STORAGE_BACKEND"] == "r2"  # literal, not the secret
    assert j["env"]["DESKBRIEF_STORAGE_BUCKET"] == "${{ secrets.R2_BUCKET }}"


@pytest.mark.parametrize("name", NAMES)
def test_actions_are_pinned_and_do_not_persist_credentials(name: str) -> None:
    uses = [s for s in job(name)["steps"] if "uses" in s]
    names = {u["uses"].split("@")[0] for u in uses}
    assert {"actions/checkout", "astral-sh/setup-uv"} <= names
    assert names <= {"actions/checkout", "astral-sh/setup-uv", "actions/upload-artifact"}
    for step in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    checkout = next(s for s in uses if s["uses"].startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False


@pytest.mark.parametrize("name", NAMES)
def test_secrets_exist_and_land_in_names_the_code_reads(name: str) -> None:
    raw = (WORKFLOWS / f"{name}.yml").read_text()
    used = set(re.findall(r"secrets\.([A-Z0-9_]+)", raw))
    assert used <= SECRETS, used - SECRETS
    env = job(name)["env"]
    assert set(env) <= CODE_ENV, set(env) - CODE_ENV
    for step in job(name)["steps"]:
        assert set(step.get("env", {})) <= CODE_ENV
    # R2_BUCKET is not an env var the code reads; the bucket override is DESKBRIEF_STORAGE_BUCKET
    assert "R2_BUCKET" not in env
    assert "DESKBRIEF_STORAGE_BUCKET" in ENV_OVERRIDES


@pytest.mark.parametrize("name", NAMES)
def test_no_untrusted_expression_is_interpolated_into_a_script(name: str) -> None:
    for step in job(name)["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")


def test_only_the_writer_workflows_hold_writer_keys_and_sla_is_read_only() -> None:
    sla_env = job("sla")["env"]
    assert "R2_ACCESS_KEY_ID" not in sla_env
    assert "R2_SECRET_ACCESS_KEY" not in sla_env
    assert "R2_READER_ACCESS_KEY_ID" in sla_env
    for name in ("intraday", "eod", "backfill", "rebuild"):
        assert "R2_READER_ACCESS_KEY_ID" not in job(name)["env"]
        assert "R2_ACCESS_KEY_ID" in job(name)["env"]


# --- schedules ----------------------------------------------------------------------------------


def fires(expr: str, at: datetime) -> bool:
    return last_cron_fire(expr, at) == at


def test_intraday_crons_cover_the_session_and_the_ecb_fix() -> None:
    assert crons("intraday") == ["*/15 13-21 * * 1-5", "15 12 * * 1-5"]
    summer_fix = datetime(2026, 7, 15, 12, 15, tzinfo=UTC)  # 14:15 CEST
    winter_fix = datetime(2026, 11, 17, 13, 15, tzinfo=UTC)  # 14:15 CET
    assert any(fires(c, summer_fix) for c in crons("intraday"))
    assert any(fires(c, winter_fix) for c in crons("intraday"))
    saturday = datetime(2026, 7, 18, 12, 15, tzinfo=UTC)
    assert not any(fires(c, saturday) for c in crons("intraday"))


def test_eod_and_sla_crons() -> None:
    assert crons("eod") == ["30 22 * * 1-5"]
    assert crons("sla") == ["20 12 * * 1-5"]
    assert "schedule" not in load("backfill")["on"]
    assert "schedule" not in load("rebuild")["on"]


# --- scripts, executed against a recording ``uv`` stub ---------------------------------------


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$UV_ARGS_OUT"\n')
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def bash(
    script: str, tmp_path: Path, stub_bin: Path, env: Mapping[str, str]
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    out = tmp_path / "uv-args.txt"
    full_env = {
        "PATH": f"{stub_bin}:/usr/bin:/bin",
        "UV_ARGS_OUT": str(out),
        **env,
    }
    proc = subprocess.run(
        ["/bin/bash", "-eo", "pipefail", "-c", script],
        env=full_env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    return proc, out.read_text().split() if out.exists() else []


def test_intraday_passes_yahoo_only_on_the_12_15_trigger(tmp_path: Path, stub_bin: Path) -> None:
    script = run_of("intraday", "Run intraday")
    _, argv = bash(script, tmp_path, stub_bin, {"GITHUB_EVENT_SCHEDULE": "15 12 * * 1-5"})
    assert argv == ["run", "deskbrief", "run", "intraday", "--sources", "yahoo"]
    for sched in ("*/15 13-21 * * 1-5", ""):  # the other cron, and a manual dispatch
        proc, argv = bash(script, tmp_path, stub_bin, {"GITHUB_EVENT_SCHEDULE": sched})
        assert proc.returncode == 0, proc.stderr
        assert argv == ["run", "deskbrief", "run", "intraday"]


def test_intraday_maps_the_schedule_for_the_gate() -> None:
    assert job("intraday")["env"]["GITHUB_EVENT_SCHEDULE"] == "${{ github.event.schedule }}"
    assert job("eod")["env"]["GITHUB_EVENT_SCHEDULE"] == "${{ github.event.schedule }}"


def test_backfill_accepts_start_end_and_sources(tmp_path: Path, stub_bin: Path) -> None:
    inputs = load("backfill")["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"start", "end", "sources"}
    assert inputs["start"]["required"] is True
    env = {"IN_START": "2026-10-01", "IN_END": "2026-10-31", "IN_SOURCES": "fred,ecb"}
    assert job("backfill")["env"]["IN_START"] == "${{ inputs.start }}"
    proc, argv = bash(run_of("backfill", "Run backfill"), tmp_path, stub_bin, env)
    assert proc.returncode == 0, proc.stderr
    assert argv == [
        *["run", "deskbrief", "run", "backfill"],
        *["--date", "2026-10-01", "--end-date", "2026-10-31", "--sources", "fred,ecb"],
    ]
    ns = cli._parser().parse_args(argv[2:])  # the CLI accepts exactly what the workflow passes
    assert (ns.job, ns.date, ns.end_date, ns.sources) == (
        "backfill",
        date(2026, 10, 1),
        date(2026, 10, 31),
        "fred,ecb",
    )


def test_backfill_with_only_a_start_date(tmp_path: Path, stub_bin: Path) -> None:
    env = {"IN_START": "2026-10-01", "IN_END": "", "IN_SOURCES": ""}
    _, argv = bash(run_of("backfill", "Run backfill"), tmp_path, stub_bin, env)
    assert argv == ["run", "deskbrief", "run", "backfill", "--date", "2026-10-01"]


@pytest.mark.parametrize(
    "env",
    [
        {"IN_START": "yesterday", "IN_END": "", "IN_SOURCES": ""},
        {"IN_START": "2026-10-01; touch pwned", "IN_END": "", "IN_SOURCES": ""},
        {"IN_START": "2026-10-01", "IN_END": "$(touch pwned)", "IN_SOURCES": ""},
        {"IN_START": "2026-10-01", "IN_END": "", "IN_SOURCES": "fred; touch pwned"},
        {"IN_START": "2026-10-01", "IN_END": "", "IN_SOURCES": "Fred,"},
    ],
)
def test_backfill_rejects_malformed_inputs(env: dict[str, str], tmp_path: Path) -> None:
    proc, _ = bash(run_of("backfill", "Validate inputs"), tmp_path, tmp_path, env)
    assert proc.returncode == 2
    assert "::error::" in proc.stdout
    assert not (tmp_path / "pwned").exists()


def test_backfill_accepts_valid_inputs_in_validation(tmp_path: Path) -> None:
    ok = {"IN_START": "2026-10-01", "IN_END": "2026-10-31", "IN_SOURCES": "fred,twelve_data"}
    assert bash(run_of("backfill", "Validate inputs"), tmp_path, tmp_path, ok)[0].returncode == 0


def test_rebuild_input_and_command(tmp_path: Path, stub_bin: Path) -> None:
    inputs = load("rebuild")["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"from_date"}
    run = run_of("rebuild", "Run rebuild")
    _, argv = bash(run, tmp_path, stub_bin, {"IN_FROM_DATE": "2026-09-01"})
    assert argv == ["run", "deskbrief", "run", "rebuild", "--date", "2026-09-01"]
    ns = cli._parser().parse_args(argv[2:])
    assert (ns.job, ns.date) == ("rebuild", date(2026, 9, 1))
    _, argv = bash(run, tmp_path, stub_bin, {"IN_FROM_DATE": ""})
    assert argv == ["run", "deskbrief", "run", "rebuild"]
    bad = bash(run_of("rebuild", "Validate inputs"), tmp_path, tmp_path, {"IN_FROM_DATE": "x"})[0]
    assert bad.returncode == 2


def test_eod_runs_the_job_then_pings_like_morning(tmp_path: Path, stub_bin: Path) -> None:
    steps = {s.get("name"): s for s in job("eod")["steps"]}
    order = [s.get("name") for s in job("eod")["steps"]]
    assert order.index("Run eod") < order.index("Ping healthcheck (success)")
    for name in ("Ping healthcheck (success)", "Ping healthcheck (fail)"):
        assert steps[name]["env"] == {"HC_URL": "${{ secrets.HEALTHCHECK_URL_EOD }}"}
    assert "skipped" in steps["Ping healthcheck (success)"]["if"]  # a skipped run does not ping
    assert steps["Ping healthcheck (fail)"]["if"] == "failure()"
    # the job's own exit status decides: stdout tee'd, status kept
    script = run_of("eod", "Run eod")
    proc, argv = bash(script, tmp_path, stub_bin, {"GITHUB_OUTPUT": str(tmp_path / "out")})
    assert proc.returncode == 0, proc.stderr
    assert argv == ["run", "deskbrief", "run", "eod"]


def test_eod_marks_a_skipped_run_and_keeps_a_failing_exit_code(
    tmp_path: Path, stub_bin: Path
) -> None:
    uv = stub_bin / "uv"
    uv.write_text("#!/bin/sh\necho '## eod: **skipped** (holiday)'\nexit 1\n")
    out = tmp_path / "out"
    proc, _ = bash(run_of("eod", "Run eod"), tmp_path, stub_bin, {"GITHUB_OUTPUT": str(out)})
    assert proc.returncode == 1
    assert out.read_text() == "skipped=true\n"


@pytest.mark.parametrize("name", ["Ping healthcheck (success)", "Ping healthcheck (fail)"])
def test_eod_pings_warn_instead_of_failing_when_the_url_is_unset(
    name: str, tmp_path: Path, stub_bin: Path
) -> None:
    proc, _ = bash(run_of("eod", name), tmp_path, stub_bin, {"HC_URL": ""})
    assert proc.returncode == 0
    assert "::warning::" in proc.stdout


# --- sla.yml: the embedded script, run for real against a local store ---------------------------


def sla_script() -> str:
    run = run_of("sla", "Check today's report")
    match = re.search(r"<<'PY'\n(.*?)\nPY\s*$", run, re.S)
    assert match, "heredoc not found"
    return match.group(1)


@pytest.fixture
def local_store(tmp_path: Path) -> tuple[LocalBackend, dict[str, str]]:
    cfg = (ROOT / "config.yaml").read_text()
    root = tmp_path / "data"
    cfg2 = re.sub(r"local_root:.*", f"local_root: {root}", cfg)
    assert cfg2 != cfg
    path = tmp_path / "config.yaml"
    path.write_text(cfg2)
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(ROOT / "src"),
        "DESKBRIEF_CONFIG": str(path),
        "SSL_CERT_FILE": os.environ.get("SSL_CERT_FILE", "/etc/ssl/cert.pem"),
    }
    return LocalBackend(root), env


def sla(env: Mapping[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-"],
        input=sla_script(),
        env={**env, **extra},
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


MONDAY = date(2026, 11, 16)


def test_sla_fails_when_the_report_is_missing(local_store: tuple[LocalBackend, dict[str, str]]):
    _, env = local_store
    proc = sla(env, SLA_DATE=MONDAY.isoformat())
    assert proc.returncode == 1
    assert "missed SLA" in proc.stdout
    assert "alert email not sent" in proc.stdout  # no SMTP settings in this environment


def test_sla_fails_when_only_another_dates_report_exists(
    local_store: tuple[LocalBackend, dict[str, str]],
) -> None:
    store, env = local_store
    store.put_bytes(report_key(date(2026, 11, 13), 1, "brief.html"), b"<html/>")
    assert sla(env, SLA_DATE=MONDAY.isoformat()).returncode == 1


def test_sla_fails_when_a_report_has_no_html_body(
    local_store: tuple[LocalBackend, dict[str, str]],
) -> None:
    store, env = local_store
    store.put_bytes(report_key(MONDAY, 1, "brief.xlsx"), b"x")
    assert sla(env, SLA_DATE=MONDAY.isoformat()).returncode == 1


def test_sla_passes_when_the_report_exists(
    local_store: tuple[LocalBackend, dict[str, str]],
) -> None:
    store, env = local_store
    store.put_bytes(report_key(MONDAY, 2, "brief.html"), b"<html/>")  # any version counts
    proc = sla(env, SLA_DATE=MONDAY.isoformat())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "present" in proc.stdout


@pytest.mark.parametrize("day", [date(2026, 11, 14), date(2026, 11, 26)])  # Saturday, Thanksgiving
def test_sla_expects_no_report_on_a_closed_day(
    local_store: tuple[LocalBackend, dict[str, str]], day: date
) -> None:
    _, env = local_store
    assert sla(env, SLA_DATE=day.isoformat()).returncode == 0


def test_sla_defaults_to_todays_et_date(local_store: tuple[LocalBackend, dict[str, str]]) -> None:
    _, env = local_store
    proc = sla(env, SLA_DATE="")
    today = et_date(datetime.now(UTC))
    assert str(today) in proc.stdout or "not an XNYS business day" in proc.stdout
    assert proc.returncode in (0, 1)


def test_sla_rejects_a_bad_date(local_store: tuple[LocalBackend, dict[str, str]]) -> None:
    _, env = local_store
    assert sla(env, SLA_DATE="soon").returncode == 2


def test_sla_fails_when_the_store_cannot_be_read(
    local_store: tuple[LocalBackend, dict[str, str]],
) -> None:
    """No R2 credentials: cannot verify, which must not look like a pass."""
    _, env = local_store
    proc = sla(env, SLA_DATE=MONDAY.isoformat(), DESKBRIEF_STORAGE_BACKEND="r2")
    assert proc.returncode == 1
    assert "cannot verify" in proc.stdout


def test_sla_fails_even_when_the_alert_email_fails(
    local_store: tuple[LocalBackend, dict[str, str]],
) -> None:
    _, env = local_store
    smtp = {
        "SMTP_USER": "u@example.invalid",
        "SMTP_APP_PASSWORD": "synthetic-password",
        "REPORT_RECIPIENTS": "r@example.invalid",
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": "1",  # nothing listens here
    }
    proc = sla(env, SLA_DATE=MONDAY.isoformat(), **smtp)
    assert proc.returncode == 1
    assert "alert email not sent" in proc.stdout
    assert "synthetic-password" not in proc.stdout + proc.stderr
