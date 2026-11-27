"""Pipeline runner (spec §5.5): plan -> fetch -> raw -> parse -> buffer -> one flush -> hooks.

Failure isolation (spec §2.4)
-----------------------------
* Every source and every request is isolated: a source that raises (in ``plan``, ``fetch`` or
  ``parse``), returns a corrupted body, or is rate limited costs only its own rows.
* A parse or validation failure quarantines that fetch (``clean/quarantine``) and writes a DQ
  ``fail``; a failed fetch (error, 429, budget exhausted) writes a DQ ``fail`` too. The run
  continues and ends ``degraded``.
* Table sources (``sources/tables.py``, card T65) feed ``events``/``filings`` instead of
  observations. Their rows are validated per fetch (a failure quarantines the fetch exactly as
  above), buffered, and written per replace scope, all or nothing: a scope that any failed or
  quarantined fetch of this run feeds is withheld whole, so the previous run's rows stay current
  (``schemas.current_rows``). Only a parsed, well-formed response with zero rows is logged
  ``empty`` (the provider answered and nothing is scheduled, which is what lets
  ``current_rows`` empty the scope): a 200 with an empty body goes through ``parse_table`` like
  any other, and a table fetch that quarantines is logged ``error`` (PR #102 review B1).
* A run cut short (an exception escaping the loop, or ``KeyboardInterrupt`` when the job is
  cancelled or times out) withholds every ``events`` scope that still had unprocessed requests
  and logs each of them ``error`` ("not fetched: run aborted"), so the partial run is neither
  half written nor read as "nothing scheduled" (review B2).
* ``events``/``filings`` describe the calendar as of today: a run whose target date is not
  today (ET), such as a manual ``morning`` run for a past date, does not fetch them, so it
  cannot replace the current calendar with an old window.
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
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

import pandas as pd

from deskbrief.checks.base import expected_for
from deskbrief.config import Config, config_hash
from deskbrief.ids import new_id
from deskbrief.logging import get_logger, redact
from deskbrief.models import (
    CheckType,
    DQStatus,
    FetchRequest,
    FetchStatus,
    JobKind,
    RawResponse,
    RunContext,
    RunStatus,
    Trigger,
)
from deskbrief.pipeline.catchup import CATCHUP_TIME_BUDGET, missed_dates, split_catchup
from deskbrief.pipeline.clean import (
    SCOPED_TABLES,
    RunBuffer,
    clean_response,
    clean_table_response,
    dq_row,
    next_clean_version,
    quarantine_row,
    rebuild,
    runner_dq,
)
from deskbrief.pipeline.jobs import (
    CheckOutputs,
    JobSteps,
    dispatcher_from_env,
    evaluate_gate,
    schedule_delay_s,
    scheduled_for_from_env,
    steps_for,
    trigger_from_env,
)
from deskbrief.pipeline.manifest import RunManifest, SourceStats, write_step_summary
from deskbrief.schemas import to_frame
from deskbrief.sources.base import HttpClient, Source
from deskbrief.sources.tables import TableName, is_table_source, table_of
from deskbrief.store.backend import StorageBackend
from deskbrief.store.datasets import Datasets
from deskbrief.store.raw import RawWriter
from deskbrief.timeutil import Clock, et_date, utc_now

#: A failed flush of one of these loses data (raw may still hold it): the run is ``failed`` so
#: catch-up treats the date as missed (spec §2.4 item 5). A lost dq_results partition degrades.
_DATA_TABLES = frozenset({"observations", "source_log", "quarantine"})
#: flushed with the data tables; a lost ``events``/``filings`` partition degrades the run (raw
#: still holds the response and the next run fetches the scope again), it does not fail it
_FIRST_FLUSH = _DATA_TABLES | frozenset(SCOPED_TABLES)
#: flushed right after the checks hook (before serving/report/notify), so the runner's own DQ
#: rows and the hook's rows share one file and later hooks can read them
_CHECK_FLUSH = frozenset({"dq_results", "revisions", "crosscheck_results"})
_FAILED_FETCH = frozenset(
    {FetchStatus.ERROR, FetchStatus.RATE_LIMITED, FetchStatus.BUDGET_EXHAUSTED}
)
#: statuses of a fetch the provider answered: a table fetch parses both (review B1)
_ANSWERED = frozenset({FetchStatus.OK, FetchStatus.EMPTY})


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
    check_flush = _CheckFlush()
    try:
        try:
            try:
                _fetch_all(env, ctx, buf, manifest)
            finally:
                _flush(buf, datasets, ctx, manifest, _FIRST_FLUSH)
            degraded += _run_steps(env, ctx, manifest, buf, datasets, check_flush)
        finally:  # the one dq_results/revisions/crosscheck_results append of the run (P-008)
            if not check_flush.done:  # not yet written: _run_steps raised before its flush
                _flush(buf, datasets, ctx, manifest, _CHECK_FLUSH)
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
        pending = [(req, _table_or_none(source, req, log)) for req in requests]
        pending = _drop_stale_table_requests(ctx, source, pending, manifest)
        done = 0
        try:
            for req, _table in pending:
                _process_request(env, ctx, source, req, buf, manifest, stats, writer)
                done += 1
        except BaseException as exc:  # incl. KeyboardInterrupt: never half a scope (review B2)
            _abort_pending(env, ctx, buf, stats, pending[done:], exc)
            raise


def _table_or_none(source: Source, req: FetchRequest, log: Any) -> TableName | None:
    """The scoped table ``req`` feeds; a ``table_for`` that raises is logged and means none."""
    try:
        return table_of(source, req)
    except Exception as exc:  # a broken table_for is a source bug: treat it as no table
        log.error("source.table_for_raised", dataset=req.dataset, error=redact(str(exc)))
        return None


def _drop_stale_table_requests(
    ctx: RunContext,
    source: Source,
    pending: list[tuple[FetchRequest, TableName | None]],
    manifest: RunManifest,
) -> list[tuple[FetchRequest, TableName | None]]:
    """Leave out ``events``/``filings`` requests when the target date is not today (ET).

    Those tables hold the calendar and filings as of today and each run replaces their scopes,
    so a manual run for a past date would replace today's rows with an old window. Nothing is
    fetched or logged for them, so the stored rows stay current.
    """
    today = et_date(ctx.now_utc)
    if ctx.target_date == today:
        return pending
    kept: list[tuple[FetchRequest, TableName | None]] = [
        (req, table) for req, table in pending if table is None
    ]
    if len(kept) < len(pending):
        note = (
            f"{source.name}: events/filings not fetched, target date "
            f"{ctx.target_date.isoformat()} is not today (ET {today.isoformat()})"
        )
        manifest.notes.append(note)
        get_logger(run_id=ctx.run_id).info(
            "source.table_requests_skipped",
            source=source.name,
            n=len(pending) - len(kept),
            target_date=ctx.target_date.isoformat(),
        )
    return kept


def _abort_pending(
    env: RunEnv,
    ctx: RunContext,
    buf: RunBuffer,
    stats: SourceStats,
    pending: Sequence[tuple[FetchRequest, TableName | None]],
    exc: BaseException,
) -> None:
    """The loop stopped before ``pending`` were processed (the first may be half done).

    Every ``events`` scope they feed is withheld (rows of the requests that did finish
    included), and each table request gets an ``error`` source_log row, so ``current_rows``
    and ``freshness_tables`` do not read the partial run as complete or as "nothing
    scheduled". ``filings`` needs no hold: its scope is an accession, and one fetch's rows are
    buffered whole or not at all. Observation requests are left as before (no row: nothing
    reads their absence as an answer). Never raises (the original exception must propagate).
    """
    tables = [(req, table) for req, table in pending if table is not None]
    for req, table in tables:
        _taint(buf, table, req)
    why = f"not fetched: run aborted ({type(exc).__name__})"
    try:
        at = env.clock()
        for req, _table in tables:
            stand_in = RawResponse(
                request=req,
                fetch_id=new_id(),
                requested_at=at,
                responded_at=at,
                http_status=None,
                content_type="",
                body=b"",
                error=why,
                status=FetchStatus.ERROR,
            )
            buf.add("source_log", _source_log_row(ctx, stand_in, None, 0, 0, None, None))
            stats.failed += 1
            stats.errors.append(f"{req.dataset}: {why}")
    except Exception as inner:  # recording the abort must not mask why the run stopped
        get_logger(run_id=ctx.run_id).error("run.abort_record_failed", error=redact(str(inner)))


def _unexpected_empty_day(ctx: RunContext, req: FetchRequest) -> str | None:
    """Data day (ISO) the dataset should have published when an empty fetch is a surprise.

    Uses the per-dataset schedules of T20 (``config.datasets``) and their calendar rules: an
    empty answer on a weekend, a holiday or before the publication time is expected. A dataset
    with no schedule entry has no expectation, so it never warns (gap noted in the PR).
    """
    for sched in ctx.config.datasets:
        if (sched.source, sched.dataset) == (req.source, req.dataset):
            exp = expected_for(ctx, sched)
            return None if exp.skip_reason else exp.data_day.isoformat()
    return None


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
    table = _table_or_none(source, req, log)
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
    logged = status
    logged_error: str | None = None
    if status in _ANSWERED and table is not None and is_table_source(source):
        # EMPTY too: for a table only a parsed, well-formed answer may say "nothing scheduled";
        # a 200 with an empty body is not JSON, so parse_table quarantines it (review B1)
        tcleaned = clean_table_response(source, raw, ctx.run_id, table)
        if tcleaned.ok:
            buf.add_table_rows(table, tcleaned.rows)
            rows = len(tcleaned.rows)
            for note in _held_back_notes(source.name, tcleaned.rows):
                if note not in manifest.notes:
                    manifest.notes.append(note)
            logged = FetchStatus.OK if rows else FetchStatus.EMPTY  # empty: nothing scheduled
        else:
            failure = f"{tcleaned.reason}: {redact(tcleaned.detail)[:300]}"
            quarantined = tcleaned.rows_quarantined
            # logged "error" (with the reason), never "ok"/"empty" with 0 quarantined rows,
            # which current_rows and freshness_tables would read as a complete fetch
            logged, logged_error = FetchStatus.ERROR, failure
            _taint(buf, table, req)
            buf.add(
                "quarantine",
                quarantine_row(
                    ctx, raw, tcleaned.reason or "unknown", tcleaned.detail, raw_key, env.clock()
                ),
            )
            _dq_fail(env, ctx, buf, "runner.parse", CheckType.VALIDITY, raw, reason=tcleaned.reason)
    elif status is FetchStatus.OK:
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
        if table is not None:
            _taint(buf, table, req)
        _dq_fail(env, ctx, buf, "runner.fetch", CheckType.COMPLETENESS, raw, status=status.value)
    elif status is FetchStatus.EMPTY and (day := _unexpected_empty_day(ctx, req)):
        why = "empty response on an expected publication day"
        _dq_fail(
            env,
            ctx,
            buf,
            "runner.empty",
            CheckType.COMPLETENESS,
            raw,
            level=DQStatus.WARN,
            data_day=day,
            reason=why,
        )

    if failure is None:
        stats.succeeded += 1
    else:
        stats.failed += 1
        stats.errors.append(f"{req.dataset}: {failure}")
    stats.rows += rows
    stats.quarantined += quarantined
    buf.add(
        "source_log",
        _source_log_row(
            ctx, raw, raw_key, rows, quarantined, as_of_min, as_of_max, logged, logged_error
        ),
    )


def _held_back_notes(source_name: str, rows: pd.DataFrame) -> list[str]:
    """A run note per instrument whose filings fetch held a Form 4/A back (PR #102 review round
    4, N1): a possible original was unreadable, so the stored rows stay as they were. The run is
    still ``ok``; the note says what was not refreshed (the Company section also says "check")."""
    if rows.empty or "details_json" not in rows.columns:
        return []
    held: dict[str, set[str]] = {}
    for inst, details in zip(rows["instrument_id"], rows["details_json"], strict=True):
        try:
            info = json.loads(details) if isinstance(details, str) and details else {}
        except ValueError:
            continue
        items = info.get("held_back_amendments") if isinstance(info, dict) else None
        for item in items or ():
            if isinstance(item, dict) and item.get("accession"):
                held.setdefault(str(inst), set()).add(str(item["accession"]))
    return [
        f"{source_name}: {inst}: {len(accs)} Form 4/A held back (a possible original was not "
        f"readable): {', '.join(sorted(accs))}"
        for inst, accs in sorted(held.items())
    ]


def _taint(buf: RunBuffer, table: TableName, req: FetchRequest) -> None:
    """A fetch feeding ``table`` failed or was quarantined: hold back the scopes it feeds.

    ``events`` scopes are ``(source, dataset)``, known from the request even when nothing came
    back, so every other fetch of that scope in this run is withheld too (FRED makes one fetch
    per release under one dataset). A ``filings`` scope is ``(instrument_id, accession)``: the
    accessions of a failed fetch are unknown, and one EDGAR fetch is the only feed of each
    accession it yields, so a failed fetch already contributes no rows and nothing else is held.
    """
    if table == "events":
        buf.taint_scope("events", (req.source, req.dataset))


def _dq_fail(
    env: RunEnv,
    ctx: RunContext,
    buf: RunBuffer,
    name: str,
    check_type: CheckType,
    raw: RawResponse,
    *,
    level: DQStatus = DQStatus.FAIL,
    **details: object,
) -> None:
    """One DQ row (``fail`` unless ``level``) per (check, source, reason); repeats bump ``count``.

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
        status=level,
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
    status: FetchStatus | None = None,
    error: str | None = None,
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
        "status": (status or raw.fetch_status).value,
        "error": redact(error) if error is not None else raw.error,
        "bytes": len(raw.body),
        "sha256": hashlib.sha256(raw.body).hexdigest() if raw.body else None,
        "raw_key": raw_key,
        "rows_parsed": rows,
        "rows_quarantined": quarantined,
        "data_min_as_of": as_of_min,
        "data_max_as_of": as_of_max,
        "attempt": raw.attempts,
    }


def _flush(
    buf: RunBuffer,
    datasets: Datasets,
    ctx: RunContext,
    manifest: RunManifest,
    tables: frozenset[str],
) -> None:
    """Write the run's buffered partitions of ``tables`` once (P-008). Never raises."""
    report = buf.flush(datasets, ctx, only=tables)
    manifest.storage_ok += report.written
    manifest.storage_failed += len(report.errors)
    manifest.flush_data_lost = manifest.flush_data_lost or bool(report.failed_tables & _DATA_TABLES)
    manifest.n_obs += report.rows_by_table.get("observations", 0)
    if "dq_results" in tables:  # counts cover the runner's rows and the checks' rows
        dq = buf.dq_status_counts()
        manifest.n_dq_fail, manifest.n_dq_warn = dq.get("fail", 0), dq.get("warn", 0)
        manifest.check_issues = buf.check_issues()
    manifest.notes.extend(f"flush failed: {e}" for e in report.errors)
    for note in buf.withheld:  # scoped tables: complete scopes only (card T65)
        if note not in manifest.notes:
            manifest.notes.append(note)


@dataclass
class _CheckFlush:
    """Whether the check tables were written already: they are flushed once, right after the
    checks hook, so serving/report/notify can read this run's results (P-008: never twice)."""

    done: bool = False


def _run_steps(
    env: RunEnv,
    ctx: RunContext,
    manifest: RunManifest,
    buf: RunBuffer,
    datasets: Datasets,
    check_flush: _CheckFlush,
) -> list[str]:
    """Checks, serving, report, notify. Each is isolated; failures degrade the run."""
    # ``ctx.now_utc`` is the run's *start*; this run's observations carry ``ingested_at`` =
    # ``responded_at``, later than that. Every hook reads point-in-time (``obs_pit(now)``), so
    # with the start time they would see none of what this run just wrote. Hooks therefore get
    # a context whose ``now_utc`` is "now, after ingestion" (T25: the first live run's report
    # showed every instrument as unavailable and the checks failed on missing data).
    hook_ctx = replace(ctx, now_utc=max(ctx.now_utc, env.clock()))
    steps = env.steps_for(ctx.job)
    failures: list[str] = []
    log = get_logger(run_id=ctx.run_id)

    def call(name: str, fn: Callable[[RunContext], object]) -> object:
        try:
            return fn(hook_ctx)
        except Exception as exc:  # the run (and the report) continues without this step
            failures.append(f"{name}: {type(exc).__name__}: {redact(str(exc))}")
            log.error("step.failed", step=name, error=redact(str(exc)))
            return None

    outputs = call("run_checks", steps.run_checks)
    if isinstance(outputs, CheckOutputs):  # buffered, flushed once right after
        buf.add_frame("dq_results", outputs.dq_results)
        buf.add_frame("revisions", outputs.revisions)
        buf.add_frame("crosscheck_results", outputs.crosscheck_results)
    check_flush.done = True  # set first: a flush never raises and must never run a second time
    _flush(buf, datasets, ctx, manifest, _CHECK_FLUSH)
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
    # P-018: a cron-Worker dispatch maps its ``slot`` to scheduled_for, a GitHub schedule event
    # its cron's last fire; either way the run is gated and de-duplicated on that instant.
    scheduled_for = scheduled_for_from_env(env.env, now) if trig is Trigger.SCHEDULE else None
    ctx = make_context(env, job, trig, target, now, scheduled_for)
    log = get_logger(run_id=ctx.run_id, job=job.value)
    log.info(
        "run.trigger",
        trigger=trig.value,
        dispatcher=dispatcher_from_env(env.env) if trigger is None else "caller",
        scheduled_for=scheduled_for.isoformat() if scheduled_for else None,
    )

    gate = evaluate_gate(job, trig, now, scheduled_for)
    if gate.proceed and gate.sources is not None:  # e.g. the ECB-fix run fetches Yahoo only
        env = replace(env, sources=[s for s in env.sources if s.name in gate.sources])
        log.info("run.sources_restricted", reason=gate.reason, sources=sorted(gate.sources))
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


#: P-018: skip reason when this job already ran ``ok``/``degraded`` for the same scheduled_for.
DUPLICATE_FIRE = "duplicate fire"
_DONE = frozenset({RunStatus.OK.value, RunStatus.DEGRADED.value})


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


def _is_duplicate_fire(runs: pd.DataFrame, ctx: RunContext) -> bool:
    """A ``runs`` row with this job, this ``scheduled_for`` and status ``ok`` or ``degraded``.

    The cron Worker and GitHub's ``schedule:`` fallback can both start the same fire (P-018).
    Whichever runs second finds the first one's row and skips. A ``failed`` row does not count,
    so a fallback can still repair a failed fire. Runs without ``scheduled_for`` never match.
    """
    if ctx.scheduled_for is None or runs.empty:
        return False
    when = pd.Timestamp(ctx.scheduled_for)
    stamps = pd.to_datetime(runs["scheduled_for"], utc=True)
    same = (runs["job"] == ctx.job.value) & (stamps == when) & runs["status"].isin(_DONE)
    return bool(same.any())


def _run_with_catchup(env: RunEnv, ctx: RunContext) -> RunManifest:
    log = get_logger(run_id=ctx.run_id, job=ctx.job.value)
    runs = _preflight(env, ctx)
    if isinstance(runs, str):
        manifest = _manifest(ctx)
        manifest.status = RunStatus.FAILED
        manifest.finished_at = env.clock()
        manifest.notes.append(f"storage unavailable: {runs}")
        return manifest
    if _is_duplicate_fire(runs, ctx):  # before catch-up: the earlier run already did that
        manifest = _manifest(ctx)
        manifest.skipped_reason = DUPLICATE_FIRE
        log.info("run.skipped", reason=DUPLICATE_FIRE, scheduled_for=_iso(ctx.scheduled_for))
        return manifest
    catchups: list[RunManifest] = []
    todo, deferred = split_catchup(missed_dates(runs, ctx.job, ctx.target_date))
    for i, day in enumerate(todo):
        if env.clock() - ctx.now_utc >= CATCHUP_TIME_BUDGET:  # keep time for the main run
            deferred = sorted([*todo[i:], *deferred])
            break
        log.info("catchup.start", target_date=day.isoformat())
        cctx = make_context(env, JobKind.BACKFILL, Trigger.CATCHUP, day, env.clock())
        try:
            catchups.append(execute_run(env, cctx))
        except Exception as exc:  # catch-up must never prevent the main run
            log.error("catchup.failed", target_date=day.isoformat(), error=redact(str(exc)))
    if deferred:
        log.info("catchup.deferred", target_dates=[d.isoformat() for d in deferred])
    manifest = execute_run(env, ctx)
    manifest.catchup_runs = catchups
    if deferred:
        manifest.notes.append(
            "catch-up deferred to the next run: " + ", ".join(d.isoformat() for d in deferred)
        )
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
        if result.abort_reason:
            manifest.notes.append(f"rebuild not flipped: {result.abort_reason}")
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
