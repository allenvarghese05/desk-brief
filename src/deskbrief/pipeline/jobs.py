"""Job wiring: run gates, step hooks, schedule delay, and store / HTTP construction (spec §5.4).

Gates (scheduled triggers only; ``workflow_dispatch`` and local runs always proceed)
-----------------------------------------------------------------------------------
GitHub cron is UTC, so ``morning`` is scheduled twice (10:15 and 11:15 UTC) and the code picks
the one that lands in the 06:00-07:00 ET window on an XNYS business day (DST-safe). The other
trigger, a weekend or a holiday is a **skip**, not a failure.

``RunStatus`` has no ``skipped`` member (it is a contract enum that also feeds the ``runs``
schema), so a skipped run is logged (``run.skipped``) and exits 0 **without a ``runs`` row**.
That is deliberate: a skipped morning must not count as "morning ran" for catch-up, the
``runs`` views and the SLA check, and it keeps ``runs`` a record of runs that did work.

Hooks
-----
:class:`JobSteps` are the extension points later cards fill in (checks T20/T21, serving T23,
report T31). The defaults are no-ops; ``steps_for`` is the one place to wire them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd

from deskbrief.calendars import is_business_day, session_bounds_utc
from deskbrief.checks import run_checks
from deskbrief.config import CalendarName, Config
from deskbrief.crosschecks.base import run_crosschecks
from deskbrief.logging import get_logger
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.sources.http import BudgetTracker, CallableBudgetStore, DefaultHttpClient
from deskbrief.store.backend import KeyNotFoundError, StorageBackend, budget_key
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.store.r2 import R2Backend
from deskbrief.timeutil import ensure_utc, to_et

MORNING_WINDOW_ET = (time(6, 0), time(7, 0))
_EOD_CALENDARS: tuple[CalendarName, ...] = ("XNYS", "SIFMA_US", "TARGET")

# ---------------------------------------------------------------------------------------------
# step hooks (no-ops until the owning cards fill them in)
# ---------------------------------------------------------------------------------------------

Hook = Callable[[RunContext], None]


@dataclass(frozen=True)
class CheckOutputs:
    """What the checks hook hands back; the runner flushes each frame once (P-008).

    ``dq_results`` already holds the single-source rows *and* the mirrored ``consistency``
    rows; the runner adds its own rows and makes the one ``dq_results`` append for the run.
    """

    dq_results: pd.DataFrame
    revisions: pd.DataFrame
    crosscheck_results: pd.DataFrame


ChecksHook = Callable[[RunContext], CheckOutputs | None]
ReportHook = Callable[[RunContext], str | None]  # returns the report key, if one was written


def _noop(ctx: RunContext) -> None:
    return None


def _noop_report(ctx: RunContext) -> str | None:
    return None


def _noop_checks(ctx: RunContext) -> CheckOutputs | None:
    return None


def run_checks_step(ctx: RunContext) -> CheckOutputs:
    """Checks hook (T06): single-source checks (T20) plus cross-checks (T21), nothing written.

    One DuckDB connection, opened after the observations flush, serves both. The cross-checks'
    mirrored ``consistency`` rows are concatenated with the check rows here, so the runner makes
    one ``dq_results`` append per run.
    """
    duck = connect(ctx.store, ctx.config)
    try:
        dq, revisions = run_checks(ctx, duck)
        crosscheck, consistency = run_crosschecks(ctx, duck)
    finally:
        duck.close()
    merged = pd.concat([dq, consistency], ignore_index=True) if len(consistency) else dq
    return CheckOutputs(merged, revisions, crosscheck)


@dataclass(frozen=True)
class JobSteps:
    """Steps that run after the fetch/clean flush, in this order. Each is isolated by the runner."""

    run_checks: ChecksHook = _noop_checks  # T06: dq_results, crosscheck_results, revisions
    build_serving: Hook = _noop  # T23: serving snapshot
    build_report: ReportHook = _noop_report  # T31: html/xlsx/xlsm (morning)
    notify: Hook = _noop  # T31: email


def steps_for(job: JobKind) -> JobSteps:
    """Hooks for ``job``. Later cards edit this table (T31 is allowed to edit the morning row)."""
    return JobSteps(run_checks=run_checks_step)


# ---------------------------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    proceed: bool
    reason: str = ""


def evaluate_gate(
    job: JobKind, trigger: Trigger, now_utc: datetime, scheduled_for: datetime | None = None
) -> Gate:
    """Whether a *scheduled* run should do any work right now.

    ``morning`` is gated on the instant the cron was *meant* to fire (``scheduled_for``, from
    ``GITHUB_EVENT_SCHEDULE``) when known, so a delayed GitHub runner still produces the
    report; otherwise on the wall clock.
    """
    if trigger is not Trigger.SCHEDULE:
        return Gate(True)
    et = to_et(scheduled_for if scheduled_for is not None and job is JobKind.MORNING else now_utc)
    day = et.date()
    if job is JobKind.MORNING:
        if not is_business_day("XNYS", day):
            return Gate(False, f"{day} is not an XNYS business day")
        lo, hi = MORNING_WINDOW_ET
        if not lo <= et.time() <= hi:
            return Gate(
                False,
                f"{et:%H:%M} ET is outside the {lo:%H:%M}-{hi:%H:%M} ET morning window "
                "(the other DST cron trigger handles it)",
            )
    elif job is JobKind.INTRADAY:
        bounds = session_bounds_utc(day)
        if bounds is None:
            return Gate(False, f"{day} is not an XNYS business day")
        et = to_et(now_utc)
        opens, closes = bounds
        if not opens <= ensure_utc(now_utc) <= closes:
            return Gate(False, f"{et:%H:%M} ET is outside the XNYS session")
    elif job is JobKind.EOD and not any(is_business_day(c, day) for c in _EOD_CALENDARS):
        return Gate(False, f"{day} is a holiday on every configured calendar")
    return Gate(True)


def trigger_from_env(env: Mapping[str, str]) -> Trigger:
    """``schedule`` when GitHub says the event was a cron; everything else is manual."""
    return Trigger.SCHEDULE if env.get("GITHUB_EVENT_NAME") == "schedule" else Trigger.MANUAL


# ---------------------------------------------------------------------------------------------
# schedule delay
# ---------------------------------------------------------------------------------------------

_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_CRON_LOOKBACK = timedelta(hours=72)


def _cron_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        rng, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if step < 1:
            raise ValueError(f"bad cron step in {spec!r}")
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            start, end = int(a), int(b)
        else:
            start = int(rng)
            end = hi if step_s else start
        if not (lo <= start <= end <= hi) and not (hi == 6 and end == 7):
            raise ValueError(f"cron value out of range in {spec!r}")
        out.update(v % 7 if hi == 6 else v for v in range(start, end + 1, step))
    return out


def last_cron_fire(expr: str, now_utc: datetime) -> datetime | None:
    """Latest minute at or before ``now_utc`` matching the 5-field cron, or ``None``.

    Looks back 72 h so a Monday run still finds Friday's fire.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"expected 5 cron fields, got {expr!r}")
    minute, hour, dom, month, dow = (
        _cron_field(f, lo, hi) for f, (lo, hi) in zip(fields, _CRON_RANGES, strict=True)
    )
    dom_any, dow_any = fields[2] == "*", fields[4] == "*"
    t = ensure_utc(now_utc).replace(second=0, microsecond=0)
    floor = t - _CRON_LOOKBACK
    while t >= floor:
        cron_dow = (t.weekday() + 1) % 7
        if dom_any or dow_any:
            day_ok = t.day in dom and cron_dow in dow
        else:  # standard cron: both restricted means either may match
            day_ok = t.day in dom or cron_dow in dow
        if t.minute in minute and t.hour in hour and t.month in month and day_ok:
            return t
        t -= timedelta(minutes=1)
    return None


def scheduled_for_from_env(env: Mapping[str, str], now_utc: datetime) -> datetime | None:
    """The instant GitHub meant to start this run, from ``GITHUB_EVENT_SCHEDULE``.

    The workflow maps ``github.event.schedule`` (a cron expression) into that variable; an ISO
    8601 UTC timestamp is also accepted. Unusable values give ``None`` (logged), never an error.
    """
    value = env.get("GITHUB_EVENT_SCHEDULE", "").strip()
    if not value:
        return None
    try:
        if len(value.split()) == 5:
            return last_cron_fire(value, now_utc)
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError as exc:
        get_logger().warning("schedule.unparseable", value=value, error=str(exc))
        return None


def schedule_delay_s(scheduled_for: datetime | None, started_at: datetime) -> float | None:
    if scheduled_for is None:
        return None
    return max(0.0, (started_at - scheduled_for).total_seconds())


# ---------------------------------------------------------------------------------------------
# construction helpers
# ---------------------------------------------------------------------------------------------


def build_store(config: Config) -> StorageBackend:
    """The configured backend (``DESKBRIEF_STORAGE_BACKEND`` overrides ``config.storage``)."""
    if config.storage.backend == "r2":
        return R2Backend.from_env(config.storage.bucket)
    return LocalBackend(config.storage.local_root)


def budget_store(store: StorageBackend) -> CallableBudgetStore:
    """Persist the per-source daily request counter in ``logs/budget/...`` (see ``http.py``)."""

    def load(source: str, day: date) -> int:
        try:
            return int(json.loads(store.get_bytes(budget_key(source, day)))["used"])
        except KeyNotFoundError:
            return 0

    def save(source: str, day: date, used: int) -> None:
        data = json.dumps({"used": used}).encode()
        store.put_bytes(budget_key(source, day), data, overwrite=True)

    return CallableBudgetStore(load, save)


def build_http(config: Config, store: StorageBackend) -> DefaultHttpClient:
    tracker = BudgetTracker.from_settings(budget_store(store), config.sources)
    return DefaultHttpClient(config.sources, budget=tracker)


def parse_sources_arg(value: str | None) -> frozenset[str] | None:
    """``"fred,ecb"`` -> ``{"fred", "ecb"}``; ``None`` / empty means all."""
    if not value:
        return None
    names = [n for n in re.split(r"[,\s]+", value) if n]
    return frozenset(names) or None


__all__ = [
    "CheckOutputs",
    "Gate",
    "JobSteps",
    "budget_store",
    "build_http",
    "build_store",
    "evaluate_gate",
    "last_cron_fire",
    "parse_sources_arg",
    "run_checks_step",
    "schedule_delay_s",
    "scheduled_for_from_env",
    "steps_for",
    "trigger_from_env",
]
