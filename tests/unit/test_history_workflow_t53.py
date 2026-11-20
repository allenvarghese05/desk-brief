"""T53: ``.github/workflows/history.yml`` (P-020). The YAML is parsed and its shell steps are
executed against a recording ``uv`` stub; nothing is dispatched.

SYNTHETIC FIXTURE: the dates below are made up.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from deskbrief import cli

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "history.yml"


def load() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(data, dict)
    data["on"] = data.pop(True)  # YAML 1.1 reads the key ``on`` as a boolean
    return data


def job() -> dict[str, Any]:
    jobs = load()["jobs"]
    assert list(jobs) == ["history"]
    return jobs["history"]  # type: ignore[no-any-return]


def run_of(step_name: str) -> str:
    steps = [s for s in job()["steps"] if s.get("name") == step_name]
    assert len(steps) == 1, step_name
    return steps[0]["run"]  # type: ignore[no-any-return]


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$UV_ARGS_OUT"\n')
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def bash(
    script: str, tmp_path: Path, stub_bin: Path, env: dict[str, str]
) -> tuple[int, str, list[str]]:
    out = tmp_path / "uv-args.txt"
    proc = subprocess.run(
        ["/bin/bash", "-eo", "pipefail", "-c", script],
        env={"PATH": f"{stub_bin}:/usr/bin:/bin", "UV_ARGS_OUT": str(out), **env},
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    return proc.returncode, proc.stdout, out.read_text().split() if out.exists() else []


def test_owner_dispatch_only_with_its_own_concurrency_group() -> None:
    wf = load()
    assert set(wf["on"]) == {"workflow_dispatch"}  # no schedule, no push
    assert wf["concurrency"] == {"group": "deskbrief-history", "cancel-in-progress": False}
    assert wf["concurrency"]["group"] != "deskbrief-pipeline"
    assert wf["permissions"] == {"contents": "read"}
    inputs = wf["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"start", "end", "sources", "dry_run"}
    assert not any(i["required"] for i in inputs.values())  # default: two years
    assert inputs["dry_run"]["type"] == "boolean"


def test_conventions_secrets_and_pins() -> None:
    j = job()
    assert j["runs-on"] == "ubuntu-latest"
    assert j["timeout-minutes"] <= 360
    # job-level env: the validated inputs only, never a secret (T55)
    assert set(j["env"]) == {"PYTHONUNBUFFERED", "IN_START", "IN_END", "IN_SOURCES", "IN_DRY_RUN"}
    assert "secrets." not in repr(j["env"])
    # secrets live on the "Run history" step only; no other step (or the job) sees them
    (run,) = [s for s in j["steps"] if s.get("name") == "Run history"]
    env = run["env"]
    assert env["DESKBRIEF_STORAGE_BACKEND"] == "r2"
    assert env["DESKBRIEF_STORAGE_BUCKET"] == "${{ secrets.R2_BUCKET }}"
    for step in j["steps"]:
        if step is not run:
            assert "secrets." not in repr(step), step.get("name") or step.get("uses")
    assert "secrets." not in repr({k: v for k, v in j.items() if k != "steps"})
    secrets = set(re.findall(r"secrets\.([A-Z0-9_]+)", WORKFLOW.read_text()))
    assert secrets == set(re.findall(r"secrets\.([A-Z0-9_]+)", repr(env)))
    # writer keys and FRED only: the range sources need no other key
    assert secrets == {
        "R2_BUCKET", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "FRED_API_KEY",
    }  # fmt: skip
    for step in j["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")
        if "uses" in step:
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    checkout = next(s for s in j["steps"] if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False


def test_no_checks_serving_or_report_step() -> None:
    scripts = "\n".join(s.get("run", "") for s in job()["steps"])
    assert "deskbrief history" in scripts
    for word in ("deskbrief run", "scorecard", "serving", "notify"):
        assert word not in scripts
    assert all("upload-artifact" not in s.get("uses", "") for s in job()["steps"])


def test_defaults_run_the_history_subcommand(tmp_path: Path, stub_bin: Path) -> None:
    env = {"IN_START": "", "IN_END": "", "IN_SOURCES": "", "IN_DRY_RUN": "false"}
    code, _, argv = bash(run_of("Run history"), tmp_path, stub_bin, env)
    assert code == 0
    assert argv == ["run", "deskbrief", "history"]


def test_all_inputs_reach_the_cli(tmp_path: Path, stub_bin: Path) -> None:
    env = {
        "IN_START": "2024-11-18", "IN_END": "2026-11-18", "IN_SOURCES": "fred,ecb",
        "IN_DRY_RUN": "true",
    }  # fmt: skip
    code, _, argv = bash(run_of("Run history"), tmp_path, stub_bin, env)
    assert code == 0
    assert argv == [
        *["run", "deskbrief", "history", "--start", "2024-11-18", "--end", "2026-11-18"],
        *["--sources", "fred,ecb", "--dry-run"],
    ]
    ns = cli._parser().parse_args(argv[2:])  # the CLI accepts exactly what the workflow passes
    assert (ns.command, ns.sources, ns.dry_run) == ("history", "fred,ecb", True)


@pytest.mark.parametrize(
    "env",
    [
        {"IN_START": "yesterday", "IN_END": "", "IN_SOURCES": "", "IN_DRY_RUN": "false"},
        {"IN_START": "", "IN_END": "$(touch pwned)", "IN_SOURCES": "", "IN_DRY_RUN": "false"},
        {"IN_START": "", "IN_END": "", "IN_SOURCES": "fred; touch pwned", "IN_DRY_RUN": "false"},
        {"IN_START": "", "IN_END": "", "IN_SOURCES": "", "IN_DRY_RUN": "yes"},
    ],
)
def test_malformed_inputs_are_rejected(env: dict[str, str], tmp_path: Path) -> None:
    code, out, _ = bash(run_of("Validate inputs"), tmp_path, tmp_path, env)
    assert code == 2
    assert "::error::" in out
    assert not (tmp_path / "pwned").exists()


def test_valid_inputs_pass_validation(tmp_path: Path) -> None:
    env = {"IN_START": "", "IN_END": "2026-11-18", "IN_SOURCES": "cboe", "IN_DRY_RUN": "true"}
    assert bash(run_of("Validate inputs"), tmp_path, tmp_path, env)[0] == 0
