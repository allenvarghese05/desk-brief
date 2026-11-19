"""Parse ``INCIDENTS.md`` for the Runs page: one entry per ``## YYYY-MM-DD — title`` heading.

Headings inside a fenced code block (the template at the top of the file) are not incidents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*[—-]\s*(.+?)\s*$")


@dataclass(frozen=True)
class Incident:
    date: str
    title: str
    body: str


def parse(markdown: str) -> list[Incident]:
    """Incidents in file order (newest first by convention)."""
    out: list[tuple[str, str, list[str]]] = []
    fenced = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else _HEADING.match(line)
        if m:
            out.append((m.group(1), m.group(2), []))
        elif (out and not fenced) or (out and fenced):
            out[-1][2].append(line)
    return [Incident(d, t, "\n".join(b).strip()) for d, t, b in out]
