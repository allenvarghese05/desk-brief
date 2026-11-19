"""One-time history load (P-020, card T53): ``deskbrief history``.

Loads a date range (default: the two years up to yesterday, ET) for every daily and weekly feed
whose source can plan a range, in one run. It is not the per-day ``backfill`` path: each source's
``plan_range(start, end, ctx)`` covers the whole range in as few requests as the provider allows.
``plan`` is never called here (FRED's ``plan`` is the 10-observation per-run request).

What one run does
-----------------
* **Plans** every selected source. A source without range support (Alpaca, Twelve Data) is
  listed and skipped; so is each configured dataset that ``plan_range`` does not cover (Yahoo
  ``quote``, Twelve Data ``fx_series_1m``), and each disabled source (Stooq).
* **Fetches** each request through the runner's per-request path, so raw evidence, cleaning,
  quarantine and ``source_log`` lineage are the same as a scheduled run's, and the configured HTTP
  client applies (per-source pacing, retries, User-Agent, budget). One log line per request and
  per source reports progress.
* **Skips keys already stored**: rows whose natural key (``OBS_NATURAL_KEY``) is in the store
  before the run, or that an earlier request of this run already produced, are dropped, as are
  rows outside ``[start, end]`` (Treasury returns whole months). An interrupted load can be
  re-run; it writes only what is missing.
* **Writes once per partition** (P-008): ``observations`` one file per touched ET date, then
  ``source_log``, ``quarantine`` and the ``runs`` row (job ``history``). Rows keep their real
  ``ingested_at``, so ``obs_pit(t)`` for a ``t`` before the load does not see them.
* Runs **no checks, scorecard, serving, report or notify**, and writes no ``dq_results``: a
  failed request shows in ``source_log``, ``quarantine``, the manifest and the exit code.

Dry run prints the plan (sources, requests, date spans, estimated rows and partitions) and
fetches and writes nothing.

The per-request helpers are the runner's own (``pipeline/runner.py``), imported rather than
copied so the two paths cannot drift.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from deskbrief.calendars import business_days
from deskbrief.config import CalendarName, Config
from deskbrief.logging import get_logger, redact
from deskbrief.models import FetchRequest, JobKind, RunContext, RunStatus, Trigger
from deskbrief.pipeline.clean import RunBuffer
from deskbrief.pipeline.manifest import RunManifest
from deskbrief.pipeline.runner import (
    _DATA_TABLES,
    RunEnv,
    _decide,
    _flush,
    _process_request,
    _write_runs_row,
    make_context,
)
from deskbrief.schemas import OBS_NATURAL_KEY
from deskbrief.sources.base import Source, supports_range
from deskbrief.sources.fred import SERIES_SPECS
from deskbrief.store.backend import StorageBackend, table_prefix
from deskbrief.store.datasets import Datasets
from deskbrief.store.raw import RawWriter
from deskbrief.store.table_cache import read_files
from deskbrief.timeutil import et_date

#: The owner's choice (P-020): two years of history.
DEFAULT_YEARS = 2
#: Calendar for a dataset with no ``datasets:`` schedule entry (e.g. Yahoo ``history``).
DEFAULT_CALENDAR: CalendarName = "XNYS"

_PERIOD_WORD = {"1d": "business days", "1w": "weeks", "1mo": "months"}
_DATE_IN_KEY = re.compile(r"/date=(\d{4}-\d{2}-\d{2})/")


def default_range(today: date, years: int = DEFAULT_YEARS) -> tuple[date, date]:
    """``years`` years up to yesterday: today's ET date is left to the scheduled runs."""
    end = today - timedelta(days=1)
    try:
        start = end.replace(year=end.year - years)
    except ValueError:  # 29 February
        start = end.replace(year=end.year - years, day=28)
    return start, end


# ---------------------------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Skipped:
    """Configured feeds the history job does not load, and why."""

    source: str
    datasets: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class DatasetEstimate:
    """Rows one dataset should yield: per interval, instruments x periods in the range."""

    source: str
    dataset: str
    #: interval -> (instruments, periods); periods are the calendar's business days for ``1d``
    by_interval: tuple[tuple[str, int, int], ...]

    @property
    def rows(self) -> int:
        return sum(n * periods for _, n, periods in self.by_interval)

    def describe(self) -> str:
        parts = [
            f"{n} x {periods} {_PERIOD_WORD.get(iv, iv)}" for iv, n, periods in self.by_interval
        ]
        return f"{self.source}/{self.dataset}: {' + '.join(parts)} = {self.rows:,}"


@dataclass(frozen=True)
class SourcePlan:
    source: str
    requests: tuple[FetchRequest, ...] = ()
    error: str | None = None  # plan_range raised

    @property
    def calls(self) -> int:
        """Provider calls: Yahoo's one request makes one yfinance call per symbol."""
        return sum(len(r.symbols) if r.source == "yahoo" else 1 for r in self.requests)


@dataclass(frozen=True)
class HistoryPlan:
    start: date
    end: date
    sources: tuple[SourcePlan, ...]
    skipped: tuple[Skipped, ...]
    estimates: tuple[DatasetEstimate, ...]
    partitions: int  # estimated observation partitions (ET dates) touched
    rate_per_min: dict[str, float] = field(default_factory=dict)

    @property
    def n_requests(self) -> int:
        return sum(len(s.requests) for s in self.sources)

    def to_text(self) -> str:
        lines = [
            f"history plan {self.start} .. {self.end}",
            "",
            "| source | datasets | requests | provider calls | min. time | est. rows |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for sp in self.sources:
            ests = [e for e in self.estimates if e.source == sp.source]
            datasets = ", ".join(sorted({r.dataset for r in sp.requests})) or "-"
            if sp.error:
                datasets = f"plan failed: {sp.error}"
            rate = self.rate_per_min.get(sp.source)
            minutes = f"{sp.calls / rate:.1f} min" if rate else "-"
            rows = sum(e.rows for e in ests)
            lines.append(
                f"| {sp.source} | {datasets} | {len(sp.requests)} | {sp.calls} | {minutes} "
                f"| {rows:,} |"
            )
        total_rows = sum(e.rows for e in self.estimates)
        total_calls = sum(sp.calls for sp in self.sources)
        lines.append(f"| **total** | | {self.n_requests} | {total_calls} | | {total_rows:,} |")
        lines += ["", "Estimated rows per dataset (instruments x periods):"]
        lines += [f"- {e.describe()}" for e in self.estimates]
        lines += [
            "",
            f"Estimated observation partitions: {self.partitions} (one file per ET date, P-008), "
            "plus one source_log file and one runs row.",
            "Keys already stored are skipped at run time and are not subtracted here.",
            "",
            "Requests:",
        ]
        for sp in self.sources:
            lines += [f"- {_describe(r)}" for r in sp.requests]
        if self.skipped:
            lines += ["", "Not loaded:"]
            lines += [
                f"- {s.source} ({', '.join(s.datasets) or 'no feeds'}): {s.reason}"
                for s in self.skipped
            ]
        return "\n".join(lines)


def _describe(req: FetchRequest) -> str:
    params = ", ".join(f"{k}={v}" for k, v in sorted(req.params.items()) if not k.startswith("$"))
    syms = req.symbols if len(req.symbols) <= 6 else (*req.symbols[:6], "...")
    return f"{req.source}/{req.dataset} [{len(req.symbols)} symbols: {', '.join(syms)}] {params}"


def _configured(config: Config) -> dict[str, dict[str, int]]:
    """``source -> {dataset: number of instruments}`` over every configured feed."""
    out: dict[str, dict[str, int]] = {}
    for inst in config.instruments:
        for feed in inst.feeds:
            per = out.setdefault(feed.source, {})
            per[feed.dataset] = per.get(feed.dataset, 0) + 1
    return out


def _schedule(config: Config, source: str, dataset: str) -> tuple[CalendarName, bool]:
    """(calendar, weekly) of a dataset, from its ``datasets:`` entry."""
    for sched in config.datasets:
        if (sched.source, sched.dataset) == (source, dataset):
            return sched.calendar, sched.release_weekday is not None
    return DEFAULT_CALENDAR, False


def _symbol_interval(source: str, symbol: str, weekly: bool) -> str:
    """A symbol's interval: FRED's series spec knows it (CPI is monthly); else the dataset's."""
    if source == "fred" and symbol in SERIES_SPECS:
        return SERIES_SPECS[symbol].interval
    return "1w" if weekly else "1d"


def _estimate(
    config: Config, source: str, dataset: str, start: date, end: date, days: set[date]
) -> DatasetEstimate:
    """Estimate one dataset's rows; add the ET dates it touches to ``days`` (partitions)."""
    calendar, weekly = _schedule(config, source, dataset)
    open_days = business_days(calendar, start, end)
    days.update(open_days)
    periods = {
        "1d": len(open_days),
        "1w": (end - start).days // 7 + 1,
        "1mo": (end.year - start.year) * 12 + end.month - start.month + 1,
    }
    counts: dict[str, int] = {}
    for symbol in config.feed_index(source, dataset):
        iv = _symbol_interval(source, symbol, weekly)
        counts[iv] = counts.get(iv, 0) + 1
    return DatasetEstimate(
        source,
        dataset,
        tuple((iv, n, periods.get(iv, len(open_days))) for iv, n in sorted(counts.items())),
    )


def build_plan(
    config: Config, sources: Sequence[Source], start: date, end: date, ctx: RunContext
) -> HistoryPlan:
    """Call ``plan_range`` (never ``plan``) on every source that has it; list the rest."""
    if end < start:
        raise ValueError("end must not be before start")
    configured = _configured(config)
    plans: list[SourcePlan] = []
    skipped: list[Skipped] = []
    estimates: list[DatasetEstimate] = []
    days: set[date] = set()
    for source in sources:
        feeds = configured.get(source.name, {})
        if not supports_range(source):
            skipped.append(Skipped(source.name, tuple(sorted(feeds)), "no range support"))
            continue
        try:
            requests = tuple(source.plan_range(start, end, ctx))
        except Exception as exc:  # one source failing to plan must not stop the others
            plans.append(SourcePlan(source.name, error=redact(f"{type(exc).__name__}: {exc}")))
            continue
        plans.append(SourcePlan(source.name, requests))
        covered = sorted({r.dataset for r in requests})
        rest = tuple(d for d in sorted(feeds) if d not in covered)
        if rest:
            skipped.append(Skipped(source.name, rest, "no range planning for these datasets"))
        for dataset in covered:
            estimates.append(_estimate(config, source.name, dataset, start, end, days))
    selected = {s.name for s in sources}
    for name, settings in config.sources.items():
        if not settings.enabled and name not in selected:
            skipped.append(Skipped(name, tuple(sorted(configured.get(name, {}))), "disabled"))
    rates = {sp.source: config.sources[sp.source].rate_per_min for sp in plans}
    return HistoryPlan(start, end, tuple(plans), tuple(skipped), tuple(estimates), len(days), rates)


# ---------------------------------------------------------------------------------------------
# stored keys and the filtering buffer
# ---------------------------------------------------------------------------------------------


def _key_frame(df: pd.DataFrame) -> pd.Series:
    """One string per row for ``OBS_NATURAL_KEY``; ``as_of`` normalised to UTC microseconds."""
    parts = df[list(OBS_NATURAL_KEY)].copy()
    parts["as_of"] = pd.to_datetime(parts["as_of"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S.%f")
    return parts.astype(str).agg("|".join, axis=1)


def stored_keys(store: StorageBackend, clean_version: str, start: date, end: date) -> set[str]:
    """Natural keys of every stored observation in the partitions ``[start, end]``.

    Only the files of those partitions are read, in parallel (``table_cache.read_files``).
    """
    keys = tuple(
        k
        for k in store.list(table_prefix("observations", clean_version))
        if k.endswith(".parquet")
        and (m := _DATE_IN_KEY.search(k))
        and start <= date.fromisoformat(m.group(1)) <= end
    )
    if not keys:
        return set()
    frame = read_files(store, "observations", keys).to_pandas()
    return set(_key_frame(frame)) if not frame.empty else set()


class _NewRowsBuffer(RunBuffer):
    """A :class:`RunBuffer` that keeps only new rows inside the range (P-020 re-runs)."""

    def __init__(self, stored: set[str], start: date, end: date) -> None:
        super().__init__()
        self._seen = set(stored)
        self._start, self._end = start, end
        self.kept = 0
        self.already_stored = 0
        self.outside_range = 0

    def add_observations(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        days = pd.to_datetime(df["as_of"], utc=True).map(lambda t: et_date(t.to_pydatetime()))
        inside = days.map(lambda d: self._start <= d <= self._end).astype(bool)
        self.outside_range += int((~inside).sum())
        df = df[inside.to_numpy()]
        if df.empty:
            return
        keys = _key_frame(df)
        new = (~keys.isin(self._seen)) & (~keys.duplicated())
        self.already_stored += int((~new).sum())
        self._seen.update(keys[new])
        kept = df[new.to_numpy()]
        self.kept += len(kept)
        super().add_observations(kept)


# ---------------------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------------------


@dataclass
class HistoryResult:
    plan: HistoryPlan
    manifest: RunManifest
    already_stored: int = 0
    outside_range: int = 0

    @property
    def exit_code(self) -> int:
        return self.manifest.exit_code

    def to_markdown(self) -> str:
        out = [self.plan.to_text()]
        if self.manifest.planned:  # dry run
            out += ["", "Dry run: nothing fetched, nothing written."]
            return "\n".join(out)
        out += ["", self.manifest.to_markdown()]
        return "\n".join(out)


def run_history(env: RunEnv, *, start: date, end: date, dry_run: bool = False) -> HistoryResult:
    """Plan, then (unless ``dry_run``) fetch and store the range for every range source."""
    now = env.clock()
    ctx = make_context(env, JobKind.HISTORY, Trigger.MANUAL, et_date(now), now)
    log = get_logger(run_id=ctx.run_id, job=JobKind.HISTORY.value)
    plan = build_plan(env.config, env.sources, start, end, ctx)
    manifest = RunManifest(
        run_id=ctx.run_id,
        job=ctx.job,
        trigger=ctx.trigger,
        target_date=ctx.target_date,
        started_at=now,
        git_sha=ctx.git_sha,
        config_hash=ctx.config_hash,
    )
    log.info(
        "history.plan",
        start=start.isoformat(),
        end=end.isoformat(),
        requests=plan.n_requests,
        sources=[sp.source for sp in plan.sources],
        skipped=[f"{s.source}:{','.join(s.datasets)}" for s in plan.skipped],
        dry_run=dry_run,
    )
    if dry_run:
        manifest.status = RunStatus.OK if env.sources else RunStatus.FAILED
        manifest.planned = [_describe(r) for sp in plan.sources for r in sp.requests]
        for sp in plan.sources:
            if sp.error:
                manifest.status = RunStatus.DEGRADED
                manifest.notes.append(f"{sp.source}: plan failed: {sp.error}")
        result = HistoryResult(plan, manifest)
        _step_summary(env, result)
        return result

    result = HistoryResult(plan, manifest)
    manifest.status = RunStatus.FAILED  # until the run proves otherwise
    datasets = Datasets(env.store)
    try:
        stored = stored_keys(env.store, datasets.clean_version(), start, end)
        buf = _NewRowsBuffer(stored, start, end)
        writer = RawWriter(env.store)
        by_name = {s.name: s for s in env.sources}
        try:
            for sp in plan.sources:
                _load_source(env, ctx, sp, by_name[sp.source], buf, manifest, writer)
        finally:
            _flush(buf, datasets, ctx, manifest, _DATA_TABLES)
            result.already_stored, result.outside_range = buf.already_stored, buf.outside_range
        if result.already_stored or result.outside_range:
            manifest.notes.append(
                f"rows skipped: {result.already_stored} already stored, "
                f"{result.outside_range} outside the range"
            )
        manifest.status = _decide(env, manifest, [])
    except Exception as exc:  # storage unreadable before the first fetch: the run is failed
        log.error("history.failed", error=redact(str(exc)))
        manifest.notes.append(f"history failed: {type(exc).__name__}: {redact(str(exc))}")
    finally:
        manifest.finished_at = env.clock()
        _write_runs_row(datasets, ctx, manifest)
        log.info(
            "history.finished",
            status=manifest.status.value if manifest.status else None,
            n_fetch=manifest.n_fetch,
            n_fetch_failed=manifest.n_fetch_failed,
            n_obs=manifest.n_obs,
            already_stored=result.already_stored,
            outside_range=result.outside_range,
        )
    _step_summary(env, result)
    return result


def _step_summary(env: RunEnv, result: HistoryResult) -> None:
    """The plan and the outcome in ``$GITHUB_STEP_SUMMARY`` when set; never fails the run."""
    path = env.env.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with Path(path).open("a", encoding="utf-8") as fh:
            fh.write(result.to_markdown() + "\n")
    except OSError as exc:
        get_logger().warning("step_summary.unwritable", error=str(exc))


def _load_source(
    env: RunEnv,
    ctx: RunContext,
    sp: SourcePlan,
    source: Source,
    buf: _NewRowsBuffer,
    manifest: RunManifest,
    writer: RawWriter,
) -> None:
    log = get_logger(run_id=ctx.run_id, source=sp.source)
    stats = manifest.stats(sp.source)
    if sp.error:
        stats.fetches += 1
        stats.failed += 1
        stats.errors.append(f"plan_range failed: {sp.error}")
        log.error("history.plan_failed", error=sp.error)
        return
    kept_before = buf.kept
    for i, req in enumerate(sp.requests, 1):
        before = (buf.kept, stats.failed)
        log.info(
            "history.request",
            n=i,
            of=len(sp.requests),
            dataset=req.dataset,
            symbols=len(req.symbols),
            params=redact(", ".join(f"{k}={v}" for k, v in sorted(req.params.items()))),
        )
        _process_request(env, ctx, source, req, buf, manifest, stats, writer)
        log.info(
            "history.request_done",
            n=i,
            of=len(sp.requests),
            new_rows=buf.kept - before[0],
            failed=stats.failed > before[1],
        )
    log.info(
        "history.source_done",
        requests=len(sp.requests),
        failed=stats.failed,
        new_rows=buf.kept - kept_before,
    )
