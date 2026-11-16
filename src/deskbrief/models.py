"""Core value types shared by every module (spec §5.1, §7.2).

CONTRACT FILE. Changing anything here needs a labeled ``contract:`` PR.
All datetimes are timezone-aware UTC; constructors reject naive values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from deskbrief.ids import new_id
from deskbrief.timeutil import ensure_utc

if TYPE_CHECKING:
    from deskbrief.config import Config
    from deskbrief.store.backend import StorageBackend

Tier = Literal[1, 2, 3]


class JobKind(StrEnum):
    MORNING = "morning"
    INTRADAY = "intraday"
    EOD = "eod"
    BACKFILL = "backfill"
    REBUILD = "rebuild"


class Trigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    CATCHUP = "catchup"


class RunStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"  # some sources/checks failed; outputs still produced
    FAILED = "failed"  # nothing usable produced (no source succeeded, or storage down)


class FetchStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SKIPPED_CALENDAR = "skipped_calendar"


class DQStatus(StrEnum):
    PASS = "pass"  # noqa: S105 - enum value, not a credential
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


class CheckType(StrEnum):
    FRESHNESS = "freshness"
    COMPLETENESS = "completeness"
    CALENDAR = "calendar"
    VALIDITY = "validity"
    ANOMALY = "anomaly"
    UNIQUENESS = "uniqueness"
    CONSISTENCY = "consistency"
    REVISION = "revision"
    RECONCILIATION = "reconciliation"


_SECRET_PARAM = re.compile(
    r"(api[_-]?key|apikey|token|secret|password|passwd|auth|key[_-]?id|access[_-]?key)", re.I
)


def _frozen_map(m: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(m))


def _json(details: Mapping[str, Any]) -> str:
    return json.dumps(dict(details), sort_keys=True, default=str, separators=(",", ":"))


@dataclass(frozen=True)
class FetchRequest:
    """What to fetch. Never contains secrets: keys are passed to the HttpClient separately."""

    source: str
    dataset: str
    params: Mapping[str, str] = field(default_factory=dict)
    symbols: tuple[str, ...] = ()
    target_date: date | None = None
    window: tuple[datetime, datetime] | None = None  # UTC, [start, end)

    def __post_init__(self) -> None:
        bad = [k for k in self.params if _SECRET_PARAM.search(k)]
        if bad:
            raise ValueError(
                f"FetchRequest.params must not carry secrets ({bad}); "
                "pass them to HttpClient.get(secret_params=...)"
            )
        if isinstance(self.target_date, datetime):
            raise TypeError("target_date must be a date, not a datetime")
        object.__setattr__(self, "params", _frozen_map(self.params))
        if self.window is not None:
            start, end = (ensure_utc(self.window[0]), ensure_utc(self.window[1]))
            if end <= start:
                raise ValueError("window end must be after start")
            object.__setattr__(self, "window", (start, end))


@dataclass(frozen=True)
class RawResponse:
    """Exactly what came back (or why nothing did). ``body`` is stored byte-for-byte in raw/.

    ``status`` is set explicitly only when no request was made or the outcome isn't visible in
    the HTTP status (``budget_exhausted``, ``skipped_calendar``, provider error inside a 200);
    otherwise :attr:`fetch_status` derives it. ``responded_at`` becomes the ``ingested_at`` of
    every observation parsed from this response.
    """

    request: FetchRequest
    fetch_id: str
    requested_at: datetime
    responded_at: datetime
    http_status: int | None
    content_type: str
    body: bytes
    error: str | None = None
    attempts: int = 1
    url_redacted: str | None = None
    status: FetchStatus | None = None

    def __post_init__(self) -> None:
        req_at = ensure_utc(self.requested_at)
        resp_at = ensure_utc(self.responded_at)
        object.__setattr__(self, "requested_at", req_at)
        # clock skew must not crash a fetch; clamp so latency is never negative
        object.__setattr__(self, "responded_at", max(resp_at, req_at))
        if self.attempts < 1:
            raise ValueError("attempts must be >= 1")

    @property
    def ok(self) -> bool:
        return (
            self.status in (None, FetchStatus.OK, FetchStatus.EMPTY)
            and self.error is None
            and self.http_status is not None
            and 200 <= self.http_status < 300
        )

    @property
    def fetch_status(self) -> FetchStatus:
        """Status recorded in ``source_log``."""
        if self.status is not None:
            return self.status
        if self.http_status == 429:
            return FetchStatus.RATE_LIMITED
        if not self.ok:
            return FetchStatus.ERROR
        return FetchStatus.EMPTY if not self.body else FetchStatus.OK

    @property
    def latency_ms(self) -> int:
        return int((self.responded_at - self.requested_at).total_seconds() * 1000)


@dataclass(frozen=True)
class RunContext:
    """Everything a job step needs to know about the current run."""

    run_id: str
    job: JobKind
    trigger: Trigger
    target_date: date  # an ET calendar date
    now_utc: datetime
    git_sha: str
    config_hash: str
    config: Config
    store: StorageBackend
    scheduled_for: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "now_utc", ensure_utc(self.now_utc))
        if self.scheduled_for is not None:
            object.__setattr__(self, "scheduled_for", ensure_utc(self.scheduled_for))


@dataclass(frozen=True)
class DQResult:
    """One single-source data-quality check outcome (table ``dq_results``)."""

    run_id: str
    check_name: str
    check_type: CheckType
    target_date: date
    evaluated_at: datetime
    status: DQStatus
    source: str | None = None
    instrument_id: str | None = None
    observed: float | None = None
    threshold: float | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    result_id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evaluated_at", ensure_utc(self.evaluated_at))
        if isinstance(self.target_date, datetime):
            raise TypeError("target_date must be a date, not a datetime")

    def details_json(self) -> str:
        return _json(self.details)


@dataclass(frozen=True)
class XResult:
    """One cross-check outcome comparing two observations (table ``crosscheck_results``)."""

    run_id: str
    check_name: str
    group_id: str | None
    target_date: date
    at_time: datetime
    relation: str
    left_instrument: str
    left_source: str
    left_value: float | None
    right_instrument: str
    right_source: str
    right_value: float | None
    expected_value: float | None
    diff_bps: float | None
    warn_bps: float
    fail_bps: float
    status: DQStatus
    details: Mapping[str, Any] = field(default_factory=dict)
    result_id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        object.__setattr__(self, "at_time", ensure_utc(self.at_time))
        if self.fail_bps < self.warn_bps:
            raise ValueError("fail_bps must be >= warn_bps")
        if isinstance(self.target_date, datetime):
            raise TypeError("target_date must be a date, not a datetime")

    def details_json(self) -> str:
        return _json(self.details)
