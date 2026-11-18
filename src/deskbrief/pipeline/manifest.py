"""Per-run manifest: counters, per-source stats, the ``runs`` row and the GHA step summary.

One :class:`RunManifest` is built per executed run (also per catch-up run and per rebuild). It
is the single place that knows how to turn what happened into

* a ``runs`` table row (:meth:`RunManifest.runs_row`), and
* the markdown shown in the GitHub Actions step summary (:meth:`RunManifest.to_markdown`).

Skipped runs (morning gate, intraday outside the session) carry ``status=None`` and a
``skipped_reason``; they produce no ``runs`` row (see ``deskbrief.pipeline.jobs``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from deskbrief.logging import get_logger
from deskbrief.models import JobKind, RunStatus, Trigger


@dataclass
class SourceStats:
    """What happened to one source in one run."""

    source: str
    fetches: int = 0
    failed: int = 0
    succeeded: int = 0
    rows: int = 0
    quarantined: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CheckIssue:
    """``count`` failing or warning results of one check (names and counts only, never values)."""

    kind: str  # dq | crosscheck
    status: str  # fail | warn
    check: str
    source: str | None
    count: int


@dataclass
class RunManifest:
    run_id: str
    job: JobKind
    trigger: Trigger
    target_date: date
    started_at: datetime
    git_sha: str = "unknown"
    config_hash: str = ""
    scheduled_for: datetime | None = None
    schedule_delay_s: float | None = None
    finished_at: datetime | None = None
    status: RunStatus | None = None  # None only for skipped runs
    skipped_reason: str | None = None
    sources: dict[str, SourceStats] = field(default_factory=dict)
    n_obs: int = 0
    n_dq_fail: int = 0  # runner-emitted DQ rows only; rows written by check hooks (T20/T21)
    n_dq_warn: int = 0  # are not included (they belong to dq_results, not to this counter)
    check_issues: list[CheckIssue] = field(default_factory=list)  # DQ + cross-check fail/warn
    storage_ok: int = 0
    storage_failed: int = 0
    flush_data_lost: bool = False  # a data table (observations, source_log, quarantine) failed
    notes: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)  # dry-run: "source/dataset (symbols)"
    report_key: str | None = None
    catchup_runs: list[RunManifest] = field(default_factory=list)

    def stats(self, source: str) -> SourceStats:
        return self.sources.setdefault(source, SourceStats(source))

    @property
    def n_fetch(self) -> int:
        return sum(s.fetches for s in self.sources.values())

    @property
    def n_fetch_failed(self) -> int:
        return sum(s.failed for s in self.sources.values())

    @property
    def exit_code(self) -> int:
        """0 for ok, degraded and skipped; 1 only for ``failed`` (spec §5.5)."""
        return 1 if self.status is RunStatus.FAILED else 0

    def runs_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job": self.job.value,
            "trigger": self.trigger.value,
            "target_date": self.target_date,
            "scheduled_for": self.scheduled_for,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": (self.status or RunStatus.FAILED).value,
            "git_sha": self.git_sha,
            "config_hash": self.config_hash,
            "n_fetch": self.n_fetch,
            "n_fetch_failed": self.n_fetch_failed,
            "n_obs": self.n_obs,
            "n_dq_fail": self.n_dq_fail,
            "n_dq_warn": self.n_dq_warn,
            "report_key": self.report_key,
            "schedule_delay_s": self.schedule_delay_s,
        }

    def to_markdown(self) -> str:
        label = "skipped" if self.status is None else self.status.value
        lines = [
            f"### Desk Brief `{self.job.value}` for {self.target_date}: **{label}**",
            "",
            f"run `{self.run_id}` · trigger `{self.trigger.value}` · git `{self.git_sha[:12]}`",
        ]
        if self.skipped_reason:
            lines += ["", f"Skipped: {self.skipped_reason}"]
        if self.schedule_delay_s is not None:
            lines.append(f"Schedule delay: {self.schedule_delay_s:.0f}s")
        if self.planned:
            lines += ["", "Dry run, planned requests (nothing fetched, nothing written):"]
            lines += [f"- {p}" for p in self.planned]
        if self.sources:
            lines += [
                "",
                "| source | fetches | failed | rows | quarantined |",
                "|---|---:|---:|---:|---:|",
            ]
            lines += [
                f"| {s.source} | {s.fetches} | {s.failed} | {s.rows} | {s.quarantined} |"
                for s in self.sources.values()
            ]
            problems = [f"- `{s.source}`: {e}" for s in self.sources.values() for e in s.errors]
            if problems:
                lines += ["", "Problems:", *problems]
        if self.status is not None and not self.planned:
            lines += [
                "",
                f"Observations written: {self.n_obs} · DQ fail: {self.n_dq_fail} · "
                f"DQ warn: {self.n_dq_warn}",
            ]
        if self.check_issues:
            lines += ["", "Checks that failed or warned (number of results):"]
            lines += [
                f"- {i.kind} {i.status} `{i.check}`"
                + (f" (source {i.source})" if i.source else "")
                + f": {i.count}"
                for i in self.check_issues
            ]
        lines += [f"- {n}" for n in self.notes]
        if self.catchup_runs:
            lines += ["", "Catch-up runs:"]
            lines += [
                f"- {c.target_date}: {c.status.value if c.status else 'skipped'} (`{c.run_id}`)"
                for c in self.catchup_runs
            ]
        return "\n".join(lines) + "\n"


def write_step_summary(env: Mapping[str, str], manifest: RunManifest) -> bool:
    """Append the run's markdown to ``$GITHUB_STEP_SUMMARY`` when set. Returns whether it wrote."""
    path = env.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    try:
        with Path(path).open("a", encoding="utf-8") as fh:
            fh.write(manifest.to_markdown())
    except OSError as exc:  # the run is already recorded; a summary must never fail it
        get_logger().warning("step_summary.unwritable", error=str(exc))
        return False
    return True
