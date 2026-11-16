"""CI `pr-hygiene` check.

Enforces: title ``t<NN>[a-z]: <summary>`` or ``contract: <summary>``; body references a card
``T<NN>``; every PR-template section heading is present and has content; no unfilled
placeholders such as ``T__``.

Usage (in CI): python scripts/ci/check_pr_hygiene.py   (pull_request events only)
"""

from __future__ import annotations

import json
import os
import re
import sys

TITLE_RE = re.compile(r"^(t\d{2}[a-z]?|contract): \S.+$")
CARD_RE = re.compile(r"\bT\d{2}[A-Z]?\b")
REQUIRED_SECTIONS: tuple[str, ...] = (
    "Card",
    "What changed",
    "Spec sections implemented",
    "Acceptance criteria",
    "Tests",
    "Honesty / labeling",
    'Files outside "Owns"',
    "Risks and follow-ups",
)
PLACEHOLDERS: tuple[str, ...] = ("T__", "<title>", "§…", "- …")


def _sections(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                found[current] = "\n".join(lines).strip()
            current, lines = m.group(1), []
        else:
            lines.append(line)
    if current is not None:
        found[current] = "\n".join(lines).strip()
    return found


def problems(title: str, body: str | None) -> list[str]:
    errs: list[str] = []
    if not TITLE_RE.match(title):
        errs.append(f"title {title!r} must look like 't01: summary' or 'contract: summary'")
    text = body or ""
    if not CARD_RE.search(text):
        errs.append("body must reference a card like T01")
    sections = _sections(text)
    for name in REQUIRED_SECTIONS:
        if name not in sections:
            errs.append(f"missing section '## {name}'")
        elif not sections[name]:
            errs.append(f"section '## {name}' is empty")
    for ph in PLACEHOLDERS:
        if ph in text:
            errs.append(f"unfilled template placeholder {ph!r}")
    return errs


def main() -> int:
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        print("pr-hygiene: not a pull_request event, skipping")
        return 0
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        pr = json.load(fh)["pull_request"]
    errs = problems(pr["title"], pr.get("body"))
    for e in errs:
        print(f"pr-hygiene: {e}")
    if errs:
        return 1
    print("pr-hygiene: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
