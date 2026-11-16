"""Pipeline runner (spec §5.5): plan -> fetch -> raw -> parse -> buffer -> one flush -> hooks.

Failure isolation (spec §2.4)
-----------------------------
* Every source and every request is isolated: a source that raises (in ``plan``, ``fetch`` or
  ``parse``), returns a corrupted body, or is rate limited costs only its own rows.
* A parse or validation failure quarantines that fetch (``clean/quarantine``) and writes a DQ
  ``fail``; a failed fetch (error, 429, budget exhausted) writes a DQ ``fail`` too. The run
  continues and ends ``degraded``.
* The buffer is flushed once per (table, ET date) in a ``finally`` (P-008), so even an
  unexpected exception that escapes the loop still flushes what the other sources produced,
  and the ``runs`` row is written last, also on that path (status ``failed``).

Status: ``failed`` when no source succeeded, nothing could be stored, or no sources were
selected; ``degraded`` when anything failed along the way; otherwise ``ok``. Exit code 1 only
for ``failed``.

Dry run is plan-only: it prints the requests each source would make; nothing is fetched,
nothing is written and no budget is spent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from deskbrief.config import Config, config_hash
from deskbrief.ids import new_id
from deskbrief.logging import get_logger, redact
from deskbrief.models import (
    CheckType,
    FetchRequest,
    FetchStatus,
    JobKind,
    RawResponse,
    RunContext,
    RunStatus,
    Trigger,
)
from deskbrief.pipeline.catchup import missed_dates
from deskbrief.pipeline.clean import (
    RunBuffer,
    clean_response,
    dq_row,
    next_clean_version,
    quarantine_row,
    rebuild,
    runner_dq,
)
from deskbrief.pipeline.jobs import (
    JobSteps,
    evaluate_gate,
    schedule_delay_s,
    scheduled_for_from_env,
    steps_for,
    trigger_from_env,
)
from deskbrief.pipeline.manifest import RunManifest, SourceStats, write_step_summary
from deskbrief.schemas import to_frame
from deskbrief.sources.base import HttpClient, Source
from deskbrief.store.backend import StorageBackend
from deskbrief.store.datasets import Datasets
from deskbrief.store.raw import RawWriter
from deskbrief.timeutil import Clock, et_date, utc_now

#: A failed flush of one of these loses data (raw may still hold it): the run is ``failed`` so
#: catch-up treats the date as missed (spec §2.4 item 5). A lost dq_results partition degrades.
_DATA_TABLES = frozenset({"observations", "source_log", "quarantine"})
_FAILED_FETCH = frozenset(
    {FetchStatus.ERROR, FetchStatus.RATE_LIMITED, FetchStatus.BUDGET_EXHAUSTED}
)


@dataclass
class RunEnv:
    """Everything a run needs from the outside; tests inject fakes for each field."""

    config: Config
    store: StorageBackend
    http: HttpClient
    sources: Sequence[Source]
    clock: Clock = utc_now
    git_sha: str = "local"
    env: Mapping[str, str] = field(default_factory=dict)
    steps_for: Callable[[JobKind], JobSteps] = steps_for


def make_context(
    env: RunEnv,
    job: JobKind,
    trigger: Trigger,
    target_date: date,
    now: datetime,
    scheduled_for: datetime | None = None,
) -> RunContext:
    return RunContext(
        run_id=new_id(),
        job=job,
        trigger=trigger,
        target_date=target_date,
        now_utc=now,
        git_sha=env.git_sha,
        config_hash=config_hash(env.config),
        config=env.config,
        store=env.store,
        scheduled_for=scheduled_for,
    )


def _manifest(ctx: RunContext) -> RunManifest:
    return RunManifest(
        run_id=ctx.run_id,
        job=ctx.job,
        trigger=ctx.trigger,
        target_date=ctx.target_date,
        started_at=ctx.now_utc,
        git_sha=ctx.git_sha,
        config_hash=ctx.config_hash,
        scheduled_for=ctx.scheduled_for,
        schedule_delay_s=schedule_delay_s(ctx.scheduled_for, ctx.now_utc),
    )


# ---------------------------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------------------------


def execute_run(env: RunEnv, ctx: RunContext) -> RunManifest:
    """Fetch, clean, flush, run hooks and record the ``runs`` row for one ``RunContext``."""
    log = get_logger(run_id=ctx.run_id, job=ctx.job.value)
    manifest = _manifest(ctx)
    manifest.status = RunStatus.FAILED  # until the run proves otherwise
    buf = RunBuffer()
    datasets = Datasets(env.store)
    degraded: list[str] = []
    try:
        try:
            _fetch_all(env, ctx, buf, manifest)
        finally:
            _flush(buf, datasets, ctx, manifest)
        degraded += _run_steps(env, ctx, manifest)
        manifest.status = _decide(env, manifest, degraded)
    finally:
        manifest.finished_at = env.clock()
        _write_runs_row(datasets, ctx, manifest)
        log.info(
            "run.finished",
            status=manifest.status.value if manifest.status else None,
            n_fetch=manifest.n_fetch,
            n_fetch_failed=manifest.n_fetch_failed,
            n_obs=manifest.n_obs,
        )
    return manifest


def _fetch_all(env: RunEnv, ctx: RunContext, buf: RunBuffer, manifest: RunManifest) -> None:
    log = get_logger(run_id=ctx.run_id)
    writer = RawWriter(env.store)
    for source in env.sources:
        stats = manifest.stats(source.name)
        try:
            requests = source.plan(ctx.job, ctx)
        except Exception as exc:  # one source failing to plan must not stop the others
            stats.fetches += 1
            stats.failed += 1
            stats.errors.append(f"plan failed: {type(exc).__name__}: {redact(str(exc))}")
            log.error("source.plan_failed", source=source.name, error=redact(str(exc)))
            buf.add(
                "dq_results",
                dq_row(
                    runner_dq(
                        ctx,
                        name="runner.plan",
                        check_type=CheckType.COMPLETENESS,
                        source=source.name,
                        at=env.clock(),
                        details={"error": redact(f"{type(exc).__name__}: {exc}")},
                    )
                ),
            )
            continue
        for req in requests:
            _process_request(env, ctx, source, req, buf, manifest, stats, writer)


def _crash_response(req: FetchRequest, exc: Exception, at: datetime) -> RawResponse:
    """Stand-in for a ``fetch`` that raised (the contract says it should return an error)."""
    return RawResponse(
        request=req,
        fetch_id=new_id(),
        requested_at=at,
        responded_at=at,
        http_status=None,
        content_type="",
        body=b"",
        error=f"{type(exc).__name__}: {exc}",
        status=FetchStatus.ERROR,
    )


def _process_request(
    env: RunEnv,
    ctx: RunContext,
    source: Source,
    req: FetchRequest,
    buf: RunBuffer,
    manifest: RunManifest,
    stats: SourceStats,
    writer: RawWriter,
) -> None:
    log = get_logger(run_id=ctx.run_id, source=source.name)
    stats.fetches += 1
    try:
        raw = source.fetch(req, env.http)
    except Exception as exc:  # a source bug is a failed fetch, not a failed run
        raw = _crash_response(req, exc, env.clock())
        log.error("source.fetch_raised", dataset=req.dataset, error=raw.error)
    status = raw.fetch_status

    raw_key: str | None = None
    if raw.body:  # empty bodies (budget exhausted, calendar skip, transport error) have no evidence
        try:
            raw_key = writer.write(raw, ctx)
            manifest.storage_ok += 1
        except Exception as exc:  # storage trouble: keep going, the status reflects it
            manifest.storage_failed += 1
            stats.errors.append(f"raw write failed: {type(exc).__name__}: {redact(str(exc))}")
            log.error("raw.write_failed", fetch_id=raw.fetch_id, error=redact(str(exc)))

    rows = quarantined = 0
    as_of_min = as_of_max = None
    failure: str | None = None
    if status is FetchStatus.OK:
        cleaned = clean_response(source, raw, ctx.run_id)
        if cleaned.ok:
            buf.add_observations(cleaned.observations)
            rows = len(cleaned.observations)
            if rows:
                as_of_min = cleaned.observations["as_of"].min().to_pydatetime()
                as_of_max = cleaned.observations["as_of"].max().to_pydatetime()
        else:
            failure = f"{cleaned.reason}: {redact(cleaned.detail)[:300]}"
            quarantined = cleaned.rows_quarantined
            buf.add(
                "quarantine",
                quarantine_row(
                    ctx, raw, cleaned.reason or "unknown", cleaned.detail, raw_key, env.clock()
                ),
            )
            _dq_fail(env, ctx, buf, "runner.parse", CheckType.VALIDITY, raw, reason=cleaned.reason)
    elif status in _FAILED_FETCH:
        failure = f"fetch {status.value}: {raw.error or raw.http_status}"
        _dq_fail(env, ctx, buf, "runner.fetch", CheckType.COMPLETENESS, raw, status=status.value)

    if failure is None:
        stats.succeeded += 1
    else:
        stats.failed += 1
        stats.errors.append(f"{req.dataset}: {failure}")
    stats.rows += rows
    stats.quarantined += quarantined
    buf.add(
        "source_log", _source_log_row(ctx, raw, raw_key, rows, quarantined, as_of_min, as_of_max)
    )


def _dq_fail(
    env: RunEnv,
    ctx: RunContext,
    buf: RunBuffer,
    name: str,
    check_type: CheckType,
    raw: RawResponse,
    **details: object,
) -> None:
    """One DQ ``fail`` row per (check, source, reason); repeats only bump ``count``.

    A source with many requests (one per series) and a missing key would otherwise write N
    identical rows; the first row keeps its fetch_id and gains ``count`` and ``datasets``.
    """
    key = (name, raw.request.source, json.dumps(details, sort_keys=True, default=str))
    prior = buf.dq_index.get(key)
    if prior is not None:
        row, info = prior
        info["count"] += 1
        if raw.request.dataset not in info["datasets"]:
            info["datasets"].append(raw.request.dataset)
        row["details_json"] = json.dumps(info, sort_keys=True, default=str, separators=(",", ":"))
        return
    info = {
        "dataset": raw.request.dataset,
        "datasets": [raw.request.dataset],
        "count": 1,
        "fetch_id": raw.fetch_id,
        "http_status": raw.http_status,
        "error": raw.error,
        **details,
    }
    result = runner_dq(
        ctx,
        name=name,
        check_type=check_type,
        source=raw.request.source,
        at=env.clock(),
        details=info,
    )
    row = dq_row(result)
    buf.add("dq_results", row)
    buf.dq_index[key] = (row, info)


def _source_log_row(
    ctx: RunContext,
    raw: RawResponse,
    raw_key: str | None,
    rows: int,
    quarantined: int,
    as_of_min: datetime | None,
    as_of_max: datetime | None,
) -> dict[str, object]:
    req = raw.request
    request = {
        "url": raw.url_redacted,
        "params": dict(req.params),
        "target_date": req.target_date.isoformat() if req.target_date else None,
        "window": [t.isoformat() for t in req.window] if req.window else None,
    }
    return {
        "fetch_id": raw.fetch_id,
        "run_id": ctx.run_id,
        "source": req.source,
        "dataset": req.dataset,
        "symbols": ",".join(req.symbols),
        "request_redacted": redact(json.dumps(request, sort_keys=True)),
        "requested_at": raw.requested_at,
        "responded_at": raw.responded_at,
        "latency_ms": raw.latency_ms,
        "http_status": raw.http_status,
        "status": raw.fetch_status.value,
        "error": raw.error,
        "bytes": len(raw.body),
        "sha256": hashlib.sha256(raw.body).hexdigest() if raw.body else None,
        "raw_key": raw_key,
        "rows_parsed": rows,
        "rows_quarantined": quarantined,
        "data_min_as_of": as_of_min,
        "data_max_as_of": as_of_max,
        "attempt": raw.attempts,
    }


def _flush(buf: RunBuffer, datasets: Datasets, ctx: RunContext, manifest: RunManifest) -> None:
    """Write the run's buffered partitions once (P-008). Never raises."""
    report = buf.flush(datasets, ctx)
    manifest.storage_ok += report.written
    manifest.storage_failed += len(report.errors)
    manifest.flush_data_lost = bool(report.failed_tables & _DATA_TABLES)
    manifest.n_obs = report.rows_by_table.get("observations", 0)
    dq = buf.rows("dq_results")
    manifest.n_dq_fail = sum(1 for r in dq if r["status"] == "fail")
    manifest.n_dq_warn = sum(1 for r in dq if r["status"] == "warn")
    manifest.notes.extend(f"flush failed: {e}" for e in report.errors)


def _run_steps(env: RunEnv, ctx: RunContext, manifest: RunManifest) -> list[str]:
    """Checks, serving, report, notify. Each is isolated; failures degrade the run."""
    steps = env.steps_for(ctx.job)
    failures: list[str] = []
    log = get_logger(run_id=ctx.run_id)

    def call(name: str, fn: Callable[[RunContext], object]) -> object:
        try:
            return fn(ctx)
        except Exception as exc:  # the run (and the report) continues without this step
            failures.append(f"{name}: {type(exc).__name__}: {redact(str(exc))}")
            log.error("step.failed", step=name, error=redact(str(exc)))
            return None

    call("run_checks", steps.run_checks)
    call("build_serving", steps.build_serving)
    key = call("build_report", steps.build_report)
    if isinstance(key, str):
        manifest.report_key = key
    call("notify", steps.notify)
    manifest.notes.extend(f"step failed: {f}" for f in failures)
    return failures


def _decide(env: RunEnv, manifest: RunManifest, degraded: list[str]) -> RunStatus:
    if manifest.storage_failed and not manifest.storage_ok:
        manifest.notes.append("storage unavailable: nothing could be written")
        return RunStatus.FAILED
    if manifest.flush_data_lost:
        manifest.notes.append("clean rows were not stored: run is failed so catch-up retries it")
        return RunStatus.FAILED
    if not env.sources:
        manifest.notes.append("no sources selected or registered")
        return RunStatus.FAILED
    attempted = [s for s in manifest.sources.values() if s.fetches]
    if attempted and not any(s.succeeded for s in attempted):
        manifest.notes.append("no source succeeded")
        return RunStatus.FAILED
    if manifest.n_fetch_failed or manifest.storage_failed or degraded:
        return RunStatus.DEGRADED
    return RunStatus.OK


def _write_runs_row(datasets: Datasets, ctx: RunContext, manifest: RunManifest) -> None:
    try:
        datasets.append("runs", to_frame("runs", [manifest.runs_row()]), ctx, ctx.target_date)
    except Exception as exc:  # cannot record the run: say so, and fail if nothing was stored
        get_logger(run_id=ctx.run_id).error("runs.write_failed", error=redact(str(exc)))
        manifest.notes.append(f"runs row not written: {type(exc).__name__}")
        manifest.storage_failed += 1
        if not manifest.storage_ok:
            manifest.status = RunStatus.FAILED


# ---------------------------------------------------------------------------------------------
# job level: gate, preflight, catch-up, dry run, rebuild
# ---------------------------------------------------------------------------------------------


def run_job(
    env: RunEnv,
    job: JobKind,
    *,
    target_date: date | None = None,
    trigger: Trigger | None = None,
    dry_run: bool = False,
) -> RunManifest:
    """Gate, catch-up, then the main run for ``job``. Writes the GHA step summary."""
    now = env.clock()
    trig = trigger or trigger_from_env(env.env)
    target = target_date or et_date(now)
    scheduled_for = scheduled_for_from_env(env.env, now) if trig is Trigger.SCHEDULE else None
    ctx = make_context(env, job, trig, target, now, scheduled_for)
    log = get_logger(run_id=ctx.run_id, job=job.value)

    gate = evaluate_gate(job, trig, now, scheduled_for)
    if not gate.proceed:
        manifest = _manifest(ctx)
        manifest.skipped_reason = gate.reason
        log.info("run.skipped", reason=gate.reason)  # no runs row: RunStatus has no 'skipped'
    elif dry_run:
        manifest = _plan_only(env, ctx)
    else:
        manifest = _run_with_catchup(env, ctx)
    write_step_summary(env.env, manifest)
    return manifest


def _plan_only(env: RunEnv, ctx: RunContext) -> RunManifest:
    manifest = _manifest(ctx)
    manifest.status = RunStatus.OK
    if not env.sources:
        manifest.status = RunStatus.FAILED
        manifest.notes.append("no sources selected or registered")
    for source in env.sources:
        try:
            for req in source.plan(ctx.job, ctx):
                manifest.planned.append(f"{req.source}/{req.dataset} ({', '.join(req.symbols)})")
        except Exception as exc:
            manifest.notes.append(f"{source.name}: plan failed: {type(exc).__name__}: {exc}")
            manifest.status = RunStatus.DEGRADED
    return manifest


def _preflight(env: RunEnv, ctx: RunContext) -> pd.DataFrame | str:
    """Read ``runs`` once: proves storage is reachable and feeds catch-up (str = the error)."""
    try:
        return Datasets(env.store).read("runs")
    except Exception as exc:
        get_logger(run_id=ctx.run_id).error("storage.unavailable", error=redact(str(exc)))
        return f"{type(exc).__name__}: {redact(str(exc))}"


def _run_with_catchup(env: RunEnv, ctx: RunContext) -> RunManifest:
    log = get_logger(run_id=ctx.run_id, job=ctx.job.value)
    runs = _preflight(env, ctx)
    if isinstance(runs, str):
        manifest = _manifest(ctx)
        manifest.status = RunStatus.FAILED
        manifest.finished_at = env.clock()
        manifest.notes.append(f"storage unavailable: {runs}")
        return manifest
    catchups: list[RunManifest] = []
    for day in missed_dates(runs, ctx.job, ctx.target_date):
        log.info("catchup.start", target_date=day.isoformat())
        cctx = make_context(env, JobKind.BACKFILL, Trigger.CATCHUP, day, env.clock())
        try:
            catchups.append(execute_run(env, cctx))
        except Exception as exc:  # catch-up must never prevent the main run
            log.error("catchup.failed", target_date=day.isoformat(), error=redact(str(exc)))
    manifest = execute_run(env, ctx)
    manifest.catchup_runs = catchups
    return manifest


def run_rebuild(
    env: RunEnv, *, from_date: date | None = None, dry_run: bool = False
) -> RunManifest:
    """``deskbrief run rebuild``: raw -> ``clean/v{N+1}`` -> flip ``_CURRENT``."""
    now = env.clock()
    ctx = make_context(env, JobKind.REBUILD, Trigger.MANUAL, et_date(now), now)
    log = get_logger(run_id=ctx.run_id, job="rebuild")
    manifest = _manifest(ctx)
    if dry_run:
        manifest.status = RunStatus.OK
        manifest.planned.append(f"rebuild raw -> clean/{next_clean_version(env.store)}")
        write_step_summary(env.env, manifest)
        return manifest
    manifest.status = RunStatus.FAILED
    try:
        result = rebuild(env.store, ctx, env.sources, from_date=from_date, now=env.clock)
        stats = manifest.stats("rebuild")
        stats.fetches, stats.rows, stats.quarantined = (
            result.fetches,
            result.rows,
            result.quarantined,
        )
        stats.failed = result.quarantined
        stats.succeeded = result.fetches - result.quarantined
        manifest.n_obs = result.rows
        manifest.notes.append(
            f"clean {result.previous_version} -> {result.new_version}"
            f"{'' if result.flip_done else ' (NOT flipped)'}; raw skipped: {result.skipped}"
        )
        if result.no_target_date:
            manifest.notes.append(
                f"{result.no_target_date} raw objects had no recoverable target_date"
            )
        manifest.notes.extend(f"flush failed: {e}" for e in result.errors)
        if result.flip_done:
            manifest.status = RunStatus.DEGRADED if result.quarantined else RunStatus.OK
    except Exception as exc:  # storage unreadable: report failed, exit 1
        log.error("rebuild.failed", error=redact(str(exc)))
        manifest.notes.append(f"rebuild failed: {type(exc).__name__}: {redact(str(exc))}")
    finally:
        manifest.finished_at = env.clock()
        _write_runs_row(Datasets(env.store), ctx, manifest)
    write_step_summary(env.env, manifest)
    return manifest
