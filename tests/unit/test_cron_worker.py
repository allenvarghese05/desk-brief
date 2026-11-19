"""T43 (P-018): the cron Worker's schedule table, its config and the workflows' fire/slot inputs.

The drift test parses the four workflows' ``schedule:`` crons and the table in
``infra/cron-worker/src/index.js``; they must be the same set, per workflow.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from deskbrief.pipeline.jobs import FIRE_ENV, SLOT_ENV

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKER = ROOT / "infra" / "cron-worker"
SCHEDULED = ("morning", "intraday", "eod", "sla")


def load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text())
    assert isinstance(data, dict)
    data["on"] = data.pop(True)  # YAML 1.1 reads the key ``on`` as a boolean
    return data


def worker_table() -> list[dict[str, str]]:
    src = (WORKER / "src" / "index.js").read_text()
    m = re.search(r"export const SCHEDULE = (\[.*?\]);", src, re.DOTALL)
    assert m, "SCHEDULE table not found in index.js"
    table = json.loads(m.group(1))
    assert isinstance(table, list)
    return table


def workflow_crons() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for name in SCHEDULED:
        for entry in load(name)["on"]["schedule"]:
            out.add((f"{name}.yml", entry["cron"]))
    return out


def test_worker_table_matches_the_workflow_crons() -> None:
    table = worker_table()
    pairs = [(e["workflow"], e["cron"]) for e in table]
    assert len(pairs) == len(set(pairs)), "duplicate entry in the Worker table"
    assert set(pairs) == workflow_crons()


def test_every_scheduled_workflow_has_a_cron_and_only_those_are_dispatched() -> None:
    names = {e["workflow"] for e in worker_table()}
    assert names == {f"{n}.yml" for n in SCHEDULED}
    for wf in WORKFLOWS.glob("*.yml"):
        if wf.stem not in SCHEDULED:
            assert "schedule" not in load(wf.stem)["on"], f"{wf.name} has a cron the Worker lacks"


def test_wrangler_has_one_every_minute_trigger_and_no_secret() -> None:
    cfg = tomllib.loads((WORKER / "wrangler.toml").read_text())
    assert cfg["triggers"]["crons"] == ["* * * * *"]
    assert cfg["main"] == "src/index.js"
    assert cfg["vars"]["GITHUB_REPO"] == "allenvarghese05/desk-brief"
    assert "GITHUB_TOKEN" not in cfg.get("vars", {})
    raw = (WORKER / "wrangler.toml").read_text()
    assert not re.search(r"(ghp_|github_pat_)\w+", raw)


def test_worker_never_logs_the_token() -> None:
    src = (WORKER / "src" / "index.js").read_text()
    for line in src.splitlines():
        if re.search(r"\blog(\.log|\.error)?\(", line):
            assert "GITHUB_TOKEN" not in line, line
            assert "headers" not in line, line


@pytest.mark.parametrize("name", SCHEDULED)
def test_workflows_accept_fire_and_slot_and_map_them_through_env(name: str) -> None:
    wf = load(name)
    inputs = wf["on"]["workflow_dispatch"]["inputs"]
    for key in ("fire", "slot"):
        assert inputs[key]["required"] is False
        assert inputs[key]["default"] == ""
        assert inputs[key]["type"] == "string"
    (job,) = wf["jobs"].values()
    env = job["env"]
    assert env[FIRE_ENV] == "${{ github.event.schedule || inputs.fire }}"
    assert env[SLOT_ENV] == "${{ inputs.slot }}"
    for step in job["steps"]:  # read through env only, never ${{ }} inside a script
        assert "inputs." not in step.get("run", "")
        assert "${{" not in step.get("run", "")


def test_worker_node_tests_pass() -> None:
    """Runs the Worker's node suite. CI's ubuntu runner ships node, so there it must run: a
    missing node fails under ``CI`` instead of skipping silently; locally it is a skip."""
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH in CI; the Worker's tests would not run")
        pytest.skip("node is not installed")
    proc = subprocess.run(
        ["node", "--test", "test/index.test.mjs"],
        cwd=WORKER,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
