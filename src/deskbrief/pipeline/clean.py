"""Clean step: parse -> lineage -> validate, the run buffer with its single flush, and rebuild.

Flush semantics (P-008, spec §2.4 item 5)
-----------------------------------------
A run buffers every table's rows in memory and :meth:`RunBuffer.flush` writes each
(table, partition date) exactly once, in the runner's ``finally``. A failure on one partition
never blocks the others. If the process dies before the flush, nothing clean is written, but
``raw/`` already holds every fetched response and :func:`rebuild` re-creates the clean layer.

Rebuild writes a complete new ``clean/v{N+1}/`` from ``raw/`` (sidecars + bodies) and only then
flips ``clean/_CURRENT``; readers therefore never see a half-built version. The sidecar does
not record ``target_date``, ``window`` or the content type, so a rebuilt ``RawResponse`` has
``target_date=None`` and a content type derived from the file extension. Parsers must derive
``as_of`` from the payload, not from the request (see "Limits" in the PR).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

import pandas as pd
from pandera.errors import SchemaErrors

from deskbrief.logging import get_logger, redact
from deskbrief.models import (
    CheckType,
    DQResult,
    DQStatus,
    FetchRequest,
    JobKind,
    RawResponse,
    RunContext,
)
from deskbrief.pipeline.manifest import CheckIssue
from deskbrief.schemas import empty_frame, to_frame, validate
from deskbrief.sources.base import ParseError, Source
from deskbrief.store.backend import (
    CLEAN_CURRENT_KEY,
    CLEAN_PREFIX,
    RAW_PREFIX,
    KeyExistsError,
    StorageBackend,
    StorageError,
)
from deskbrief.store.datasets import PARTITION_RULES, Datasets, current_clean_version
from deskbrief.timeutil import ET, ensure_utc, et_date

_DETAIL_MAX = 2000
_VERSION = re.compile(r"^v(\d+)$")
_CONTENT_TYPES = {
    "json": "application/json",
    "csv": "text/csv",
    "xml": "application/xml",
    "txt": "text/plain",
    "bin": "application/octet-stream",
}

# ---------------------------------------------------------------------------------------------
# row builders
# ---------------------------------------------------------------------------------------------


def dq_row(res: DQResult) -> dict[str, Any]:
    return {
        "result_id": res.result_id,
        "run_id": res.run_id,
        "check_name": res.check_name,
        "check_type": res.check_type.value,
        "source": res.source,
        "instrument_id": res.instrument_id,
        "target_date": res.target_date,
        "evaluated_at": res.evaluated_at,
        "status": res.status.value,
        "observed": res.observed,
        "threshold": res.threshold,
        "details_json": res.details_json(),
    }


def runner_dq(
    ctx: RunContext,
    *,
    name: str,
    check_type: CheckType,
    source: str,
    at: datetime,
    details: dict[str, Any],
    status: DQStatus = DQStatus.FAIL,
) -> DQResult:
    """A runner-originated DQ row: a ``fail`` (fetch/parse/plan failure) or an empty ``warn``."""
    return DQResult(
        run_id=ctx.run_id,
        check_name=name,
        check_type=check_type,
        target_date=ctx.target_date,
        evaluated_at=at,
        status=status,
        source=source,
        details=details,
    )


def quarantine_row(
    ctx: RunContext, raw: RawResponse, reason: str, detail: str, raw_key: str | None, at: datetime
) -> dict[str, Any]:
    return {
        "fetch_id": raw.fetch_id,
        "run_id": ctx.run_id,
        "source": raw.request.source,
        "dataset": raw.request.dataset,
        "reason": reason,
        "detail": redact(detail)[:_DETAIL_MAX],
        "raw_key": raw_key,
        "detected_at": at,
    }


# ---------------------------------------------------------------------------------------------
# parse -> validate -> lineage
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanResult:
    observations: pd.DataFrame
    reason: str | None = None  # quarantine reason; None means the parse was clean
    detail: str = ""
    rows_quarantined: int = 0

    @property
    def ok(self) -> bool:
        return self.reason is None


def clean_response(source: Source, raw: RawResponse, run_id: str) -> CleanResult:
    """Parse ``raw``, validate, and add the lineage columns. Never raises.

    ``ParseError`` -> ``parse_error``; a frame that violates the schema -> ``schema_invalid``;
    any other exception from the parser -> ``parser_exception``. All three mean the fetch is
    quarantined and none of its rows are kept ("never return partial garbage").
    """
    n_rows = 0
    try:
        parsed = source.parse(raw)
        if not isinstance(parsed, pd.DataFrame):
            raise ParseError(f"parse() returned {type(parsed).__name__}, expected DataFrame")
        n_rows = len(parsed)
        validate("parsed_observations", parsed)
        if parsed.empty:
            return CleanResult(empty_frame("observations"))
        obs = parsed.copy()
        obs["fetch_id"] = raw.fetch_id
        obs["run_id"] = run_id
        obs["ingested_at"] = pd.Timestamp(raw.responded_at)
        obs["parser_version"] = source.parser_version
        validate("observations", obs)
    except ParseError as exc:
        return CleanResult(empty_frame("observations"), "parse_error", str(exc))
    except SchemaErrors as exc:
        return CleanResult(
            empty_frame("observations"),
            "schema_invalid",
            str(exc.failure_cases)[:_DETAIL_MAX],
            n_rows,
        )
    except Exception as exc:  # a buggy parser must not take the run down
        return CleanResult(
            empty_frame("observations"), "parser_exception", f"{type(exc).__name__}: {exc}"
        )
    return CleanResult(obs)


# ---------------------------------------------------------------------------------------------
# buffer + single flush
# ---------------------------------------------------------------------------------------------

#: tables the runner buffers row by row; partition dates come from ``PARTITION_RULES`` (P-006)
_ROW_TABLES = ("quarantine", "source_log", "dq_results")
#: tables a check hook hands over as frames (dq_results also takes the runner's own rows)
_FRAME_TABLES = ("dq_results", "revisions", "crosscheck_results")


@dataclass
class FlushReport:
    written: int = 0
    rows_by_table: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    failed_tables: set[str] = field(default_factory=set)


class RunBuffer:
    """In-memory rows of one run, flushed once per (table, partition date)."""

    def __init__(self) -> None:
        self._obs: list[pd.DataFrame] = []
        self._rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._frames: dict[str, list[pd.DataFrame]] = defaultdict(list)
        #: collapse key -> (buffered dq row, its details); see ``runner._dq_fail``
        self.dq_index: dict[tuple[str, str, str], tuple[dict[str, Any], dict[str, Any]]] = {}

    def add_observations(self, df: pd.DataFrame) -> None:
        if not df.empty:
            self._obs.append(df)

    def add(self, table: str, row: dict[str, Any]) -> None:
        if table not in _ROW_TABLES:
            raise ValueError(f"buffer has no partition rule for table {table!r}")
        self._rows[table].append(row)

    def add_frame(self, table: str, df: pd.DataFrame) -> None:
        """Buffer a whole validated frame (check output); split by the P-006 partition rule."""
        if table not in _FRAME_TABLES:
            raise ValueError(f"buffer takes no frames for table {table!r}")
        if not df.empty:
            self._frames[table].append(df)

    @property
    def n_obs(self) -> int:
        return sum(len(f) for f in self._obs)

    def rows(self, table: str) -> list[dict[str, Any]]:
        return list(self._rows[table])

    def dq_status_counts(self) -> dict[str, int]:
        """``status -> n`` over every buffered ``dq_results`` row: runner rows and check frames."""
        counts: dict[str, int] = defaultdict(int)
        for row in self._rows["dq_results"]:
            counts[str(row["status"])] += 1
        for frame in self._frames["dq_results"]:
            for status, n in frame["status"].value_counts().items():
                counts[str(status)] += int(n)
        return dict(counts)

    def check_issues(self) -> list[CheckIssue]:
        """Fail/warn results of ``dq_results`` and ``crosscheck_results`` grouped by check.

        One entry per (kind, status, check name, source); failures first, then by count.
        Cross-check results carry no single source, so theirs is ``None``.
        """
        counts: dict[tuple[str, str, str, str | None], int] = defaultdict(int)
        for kind, table in (("dq", "dq_results"), ("crosscheck", "crosscheck_results")):
            records: list[dict[str, Any]] = list(self._rows[table])
            for frame in self._frames[table]:
                records.extend({str(k): v for k, v in r.items()} for r in frame.to_dict("records"))
            for rec in records:
                status = str(rec["status"])
                if status not in ("fail", "warn"):
                    continue
                source = rec.get("source") if kind == "dq" else None
                source = None if source is None or pd.isna(source) else str(source)
                counts[(kind, status, str(rec["check_name"]), source)] += 1
        ordered = sorted(counts.items(), key=lambda kv: (kv[0][1] != "fail", -kv[1], kv[0][0:3]))
        return [CheckIssue(k, st, name, src, n) for (k, st, name, src), n in ordered]

    def flush(
        self, datasets: Datasets, ctx: RunContext, *, only: Collection[str] | None = None
    ) -> FlushReport:
        """Write every buffered partition once. Per-partition failures are collected, not raised.

        ``only`` limits the flush to those tables: the runner flushes the data tables right away
        and the check tables after the checks hook, so each (table, date) is still written once.
        """
        report = FlushReport()
        log = get_logger(run_id=ctx.run_id)
        for table, part_date, frame in self._partitions(only):
            try:
                datasets.append(table, frame, ctx, part_date)
            except (
                Exception
            ) as exc:  # storage or schema trouble in one partition must not stop the rest
                report.failed_tables.add(table)
                report.errors.append(f"{table} {part_date}: {type(exc).__name__}: {exc}")
                log.error("flush.failed", table=table, part_date=str(part_date), error=str(exc))
            else:
                report.written += 1
                report.rows_by_table[table] = report.rows_by_table.get(table, 0) + len(frame)
        return report

    def _partitions(
        self, only: Collection[str] | None = None
    ) -> list[tuple[str, date, pd.DataFrame]]:
        parts: list[tuple[str, date, pd.DataFrame]] = []
        if self._obs and (only is None or "observations" in only):
            obs = pd.concat(self._obs, ignore_index=True)
            days = obs["as_of"].dt.tz_convert(ET).dt.date
            for day, grp in obs.groupby(days, sort=True):
                parts.append(("observations", day, grp.reset_index(drop=True)))
        for table in dict.fromkeys([*_ROW_TABLES, *_FRAME_TABLES]):
            if only is not None and table not in only:
                continue
            parts.extend(self._table_partitions(table))
        return parts

    def _table_partitions(self, table: str) -> list[tuple[str, date, pd.DataFrame]]:
        """Runner rows and hook frames of ``table``, grouped by its P-006 partition date."""
        pieces = [f for f in self._frames[table] if not f.empty]
        if self._rows[table]:
            pieces.insert(0, to_frame(table, self._rows[table]))
        if not pieces:
            return []
        frame = pd.concat(pieces, ignore_index=True) if len(pieces) > 1 else pieces[0]
        column, to_date = PARTITION_RULES[table]
        days = pd.Series([to_date(v) for v in frame[column]], index=frame.index)
        return [
            (table, day, grp.reset_index(drop=True)) for day, grp in frame.groupby(days, sort=True)
        ]


# ---------------------------------------------------------------------------------------------
# rebuild: raw/ -> clean/v{N+1}/ -> flip _CURRENT
# ---------------------------------------------------------------------------------------------


class _VersionedDatasets(Datasets):
    """``Datasets`` that writes to a fixed clean version instead of ``clean/_CURRENT``."""

    def __init__(self, store: StorageBackend, version: str, previous: str) -> None:
        super().__init__(store)
        self._version = version
        self._previous = previous

    def clean_version(self) -> str:
        return self._version

    def _check_version_not_stale(self) -> None:
        """The stale-``_CURRENT`` guard, adapted: we write to ``new`` on purpose, but the
        pointer must still be where we found it (otherwise another rebuild flipped it)."""
        now = current_clean_version(self._store)
        if now != self._previous:
            raise StorageError(
                f"clean/_CURRENT moved from {self._previous!r} to {now!r} during rebuild"
            )


@dataclass
class RebuildResult:
    new_version: str
    previous_version: str
    fetches: int = 0
    skipped: int = 0
    rows: int = 0
    quarantined: int = 0
    no_target_date: int = 0  # raw whose request context could not be recovered
    flip_done: bool = False
    partitions: int = 0  # clean partition files written or carried over into the new version
    abort_reason: str | None = None  # why the flip was refused (nothing written, pointer moved...)
    errors: list[str] = field(default_factory=list)


def next_clean_version(store: StorageBackend) -> str:
    """``v{N+1}`` where N is the highest of ``_CURRENT`` and any version directory present."""
    highest = 0
    current = _VERSION.match(current_clean_version(store))
    if current:
        highest = int(current.group(1))
    for key in store.list(f"{CLEAN_PREFIX}/v"):
        m = _VERSION.match(key.split("/")[1])
        if m:
            highest = max(highest, int(m.group(1)))
    return f"v{highest + 1}"


@dataclass(frozen=True)
class RequestInfo:
    """What the raw sidecar lacks and parsers may need: ``target_date`` and ``window``."""

    target_date: date | None = None
    window: tuple[datetime, datetime] | None = None


def _info_from_request_json(text: str) -> RequestInfo:
    try:
        data = json.loads(text)
        td = data.get("target_date")
        win = data.get("window")
        return RequestInfo(
            date.fromisoformat(td) if td else None,
            (datetime.fromisoformat(win[0]), datetime.fromisoformat(win[1])) if win else None,
        )
    except (ValueError, TypeError, AttributeError, IndexError):
        return RequestInfo()


def recover_request_info(store: StorageBackend) -> tuple[dict[str, RequestInfo], dict[str, date]]:
    """``({fetch_id: info}, {run_id: target_date})`` from ``source_log`` and ``runs``.

    The runner writes ``target_date`` and ``window`` into ``source_log.request_redacted``; the
    ``runs`` row is the fallback (its target date) for fetches whose source_log row is lost.
    """
    datasets = Datasets(store)
    by_fetch: dict[str, RequestInfo] = {}
    log = datasets.read("source_log")
    for fid, req in zip(log["fetch_id"], log["request_redacted"], strict=True):
        by_fetch[str(fid)] = _info_from_request_json(str(req))
    runs = datasets.read("runs")
    by_run = {str(r): d for r, d in zip(runs["run_id"], runs["target_date"], strict=True)}
    return by_fetch, by_run


def _raw_from_sidecar(
    meta: dict[str, Any], key: str, body: bytes, info: RequestInfo | None = None
) -> RawResponse:
    ext = key.rpartition(".")[2]
    info = info or RequestInfo()
    req = FetchRequest(
        source=meta["source"],
        dataset=meta["dataset"],
        params=meta.get("params") or {},
        symbols=tuple(meta.get("symbols") or ()),
        target_date=info.target_date,
        window=info.window,
    )
    return RawResponse(
        request=req,
        fetch_id=meta["fetch_id"],
        requested_at=datetime.fromisoformat(meta["requested_at"]),
        responded_at=datetime.fromisoformat(meta["responded_at"]),
        http_status=meta.get("http_status"),
        content_type=_CONTENT_TYPES.get(ext, "application/octet-stream"),
        body=body,
        url_redacted=meta.get("url_redacted"),
        attempts=int(meta.get("attempts", 1)),
    )


def _flip_claim_key(previous: str) -> str:
    return f"{CLEAN_PREFIX}/_flips/from-{previous}"


def rebuild(
    store: StorageBackend,
    ctx: RunContext,
    sources: Sequence[Source],
    *,
    from_date: date | None = None,
    now: Callable[[], datetime],
) -> RebuildResult:
    """Re-parse raw responses into a new clean version, then flip ``_CURRENT``.

    With ``from_date``, only raw ingested (ET date) on or after it is re-parsed; clean
    partitions dated before it are copied unchanged from the previous version, so nothing
    older is lost (duplicates from re-parsed raw are removed by the ``obs_latest`` view).

    Raw is processed one UTC month at a time and each month is flushed before the next is read,
    so memory is bounded by a month of raw, not by all of ``raw/``. A month's files are named
    ``run={run_id}-{YYYY-MM}`` because the same (table, date) can occur in two months (P-008
    allows one file per run id; the suffix keeps each file write-once).

    Flip guard: nothing is flipped when a flush failed, when no partition was written, or when
    ``_CURRENT`` is not where the rebuild found it. The version is claimed with an exclusive
    marker (``clean/{new}/_CLAIM``) and the flip itself with another (``clean/_flips/from-{prev}``),
    so two concurrent rebuilds cannot both flip away from the same version. An operator who rolls
    ``_CURRENT`` back by hand must delete that flip marker before rebuilding from it again; the
    abort reason names the key. The marker body is JSON: ``flipped_at`` (UTC ISO 8601),
    ``new_version`` and ``run_id``.
    """
    log = get_logger(run_id=ctx.run_id, job=JobKind.REBUILD.value)
    previous = current_clean_version(store)
    new = next_clean_version(store)
    result = RebuildResult(new_version=new, previous_version=previous)
    try:
        store.put_bytes(f"{CLEAN_PREFIX}/{new}/_CLAIM", ctx.run_id.encode(), overwrite=False)
    except KeyExistsError:
        result.abort_reason = f"clean/{new} is claimed by another rebuild"
        return result
    except Exception as exc:  # cannot even write the marker: the new version is not writable
        result.abort_reason = f"could not claim clean/{new}: {type(exc).__name__}: {exc}"
        return result
    datasets = _VersionedDatasets(store, new, previous)
    by_name = {s.name: s for s in sources}
    by_fetch, by_run = recover_request_info(store)

    months: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for key in store.list(f"{RAW_PREFIX}/"):
        if not key.endswith(".meta.json"):
            continue
        meta = json.loads(store.get_bytes(key))
        responded = ensure_utc(datetime.fromisoformat(meta["responded_at"]))
        if from_date is not None and et_date(responded) < from_date:
            continue
        source = by_name.get(meta["source"])
        if source is None or meta.get("fetch_status") != "ok":
            result.skipped += 1
            continue
        months[f"{responded:%Y-%m}"].append((key, meta))

    for month in sorted(months):
        buf = RunBuffer()
        for key, meta in months[month]:
            raw_key = key.removesuffix(".meta.json")
            info = by_fetch.get(meta["fetch_id"])
            if info is None or info.target_date is None:
                fallback = by_run.get(str(meta.get("run_id")))
                info = RequestInfo(fallback, info.window if info else None)
            if info.target_date is None:
                result.no_target_date += 1
            raw = _raw_from_sidecar(meta, raw_key, store.get_bytes(raw_key), info)
            result.fetches += 1
            cleaned = clean_response(by_name[meta["source"]], raw, ctx.run_id)
            if cleaned.ok:
                buf.add_observations(cleaned.observations)
                result.rows += len(cleaned.observations)
            else:
                result.quarantined += 1
                buf.add(
                    "quarantine",
                    quarantine_row(
                        ctx, raw, cleaned.reason or "unknown", cleaned.detail, raw_key, now()
                    ),
                )
                log.warning("rebuild.quarantined", fetch_id=raw.fetch_id, reason=cleaned.reason)
        report = buf.flush(datasets, replace(ctx, run_id=f"{ctx.run_id}-{month}"))
        result.partitions += report.written
        result.errors.extend(report.errors)
        if report.errors:  # never flip to a partial version
            return result

    result.partitions += _carry_over(store, previous, new, from_date)
    return _flip(store, ctx, result, now())


def _flip(
    store: StorageBackend, ctx: RunContext, result: RebuildResult, flipped_at: datetime
) -> RebuildResult:
    """Point ``_CURRENT`` at the new version if, and only if, it is safe to."""
    previous, new = result.previous_version, result.new_version
    if result.partitions == 0:
        result.abort_reason = (
            f"no partitions were written to clean/{new}; _CURRENT left on {previous}"
        )
        return result
    now_current = current_clean_version(store)
    if now_current != previous:
        result.abort_reason = f"clean/_CURRENT moved from {previous!r} to {now_current!r}"
        return result
    claim = _flip_claim_key(previous)
    try:
        body = {
            "flipped_at": ensure_utc(flipped_at).isoformat(),
            "new_version": new,
            "run_id": ctx.run_id,
        }
        store.put_bytes(claim, json.dumps(body).encode(), overwrite=False)
    except KeyExistsError:
        result.abort_reason = (
            f"another rebuild already flipped away from {previous!r}: marker {claim} exists. "
            f"After rolling back _CURRENT by hand, delete {claim} to rebuild from "
            f"{previous} again; if you did not roll back by hand, check `flipped_at` in the "
            "marker body first (a rebuild killed between the claim and the pointer write leaves "
            "the same marker)"
        )
        return result
    try:
        store.put_bytes(CLEAN_CURRENT_KEY, new.encode(), overwrite=True)
    except Exception:
        store.delete(claim)  # nothing flipped, so release the claim for a retry
        raise
    result.flip_done = True
    return result


_PART_DATE = re.compile(r"/date=(\d{4}-\d{2}-\d{2})/")


def _carry_over(store: StorageBackend, old: str, new: str, from_date: date | None) -> int:
    """Copy older clean partitions (``date < from_date``) from ``old`` into ``new`` unchanged.

    Returns how many partition files were copied."""
    if from_date is None:
        return 0
    copied = 0
    prefix = f"{CLEAN_PREFIX}/{old}/"
    for key in store.list(prefix):
        m = _PART_DATE.search(key)
        if m and date.fromisoformat(m.group(1)) < from_date:
            dest = f"{CLEAN_PREFIX}/{new}/{key[len(prefix) :]}"
            if not store.exists(dest):
                store.put_bytes(dest, store.get_bytes(key))
                copied += 1
    return copied
