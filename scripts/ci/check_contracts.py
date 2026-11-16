"""CI `contracts` check.

Modifying an existing contract file requires the PR to be a labeled contract PR:
label ``contract`` AND title prefix ``contract:``. Creating a contract file for the first
time (e.g. card T01) is allowed without the label.

Usage (in CI): python scripts/ci/check_contracts.py
Reads GITHUB_EVENT_PATH / GITHUB_EVENT_NAME / GITHUB_BASE_REF. On non-PR events it passes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence

CONTRACT_FILES: frozenset[str] = frozenset(
    {
        "src/deskbrief/models.py",
        "src/deskbrief/schemas.py",
        "src/deskbrief/config.py",
        "src/deskbrief/sources/base.py",
        "src/deskbrief/store/backend.py",
    }
)


def violations(changes: Iterable[tuple[str, str]], title: str, labels: Sequence[str]) -> list[str]:
    """Return contract files modified without contract approval.

    ``changes`` are (git status letter, path) pairs from ``git diff --name-status``.
    Status ``A`` (added) is allowed; anything else touching a contract file needs approval.
    """
    approved = "contract" in labels and title.lower().startswith("contract:")
    if approved:
        return []
    return sorted(
        path for status, path in changes if path in CONTRACT_FILES and not status.startswith("A")
    )


def _changed_files(base_ref: str) -> list[tuple[str, str]]:
    out = subprocess.run(
        ["git", "diff", "--name-status", f"origin/{base_ref}...HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    pairs: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            pairs.append((parts[0], parts[-1]))
    return pairs


def main() -> int:
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        print("contracts: not a pull_request event, skipping")
        return 0
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        event = json.load(fh)
    pr = event["pull_request"]
    labels = [lbl["name"] for lbl in pr.get("labels", [])]
    bad = violations(_changed_files(os.environ["GITHUB_BASE_REF"]), pr["title"], labels)
    if bad:
        print("contracts: these contract files changed without a labeled contract PR:")
        for path in bad:
            print(f"  - {path}")
        print("Add label 'contract' and prefix the title with 'contract:' (planner approval).")
        return 1
    print("contracts: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
