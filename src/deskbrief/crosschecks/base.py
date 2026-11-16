"""Cross-check framework (spec §8.2): protocol, registry, bps math, runner.

A *cross-check* compares two observations of the same quantity from different sources and
emits one :class:`~deskbrief.models.XResult` per comparison. :func:`run_crosschecks` runs every
registered check and returns the two frames the runner flushes **once** per run (P-008)::

    xcheck_df, dq_df = run_crosschecks(ctx, duck)
    datasets.append("crosscheck_results", xcheck_df, ctx, ctx.target_date)   # skip if empty
    datasets.append("dq_results", dq_df, ctx, ctx.target_date)  # merge with T20's rows first

``dq_df`` holds one ``consistency`` row per cross-check row (spec §7.2), so the T19 hook must
concatenate it with the single-source rows from ``run_checks`` *before* the one ``dq_results``
append for the run's ``target_date`` (a second append raises ``KeyExistsError`` by design).
Both frames are valid against their schemas, and may be empty.

Reading rules (P-009)
---------------------
* Daily comparisons read ``obs_pit(ctx.now_utc)`` (what was known at run time), never
  ``daily_close``, and compare only ET dates strictly before the current ET date
  (:func:`reference_date`): the current day is unfinished. An eod run therefore evaluates
  today's close the next morning, like the freshness checks.
* Every result's details carry both sides' ``source``/``dataset``/``field``/``unit``/``as_of``
  so consumers label by field and unit (an ECB fix or a Treasury yield is never a "close").

bps conventions (``Tolerance`` docstring in ``config.py``)
----------------------------------------------------------
* yield levels (unit ``pct``): absolute, ``1 bp = 0.01`` percentage point -> :func:`abs_level_bps`
* prices, FX, indices: relative to the right-hand (reference) side, ``1 bp = 0.01%``
  -> :func:`rel_bps`
* returns: difference of decimal returns, ``1 bp = 0.01%`` return -> :func:`return_diff_bps`

Status: ``|diff_bps| <= warn_bps`` pass, ``<= fail_bps`` warn, above that fail (a value exactly
on a threshold does not breach it). A missing side, a unit mismatch or a zero reference give
``skip`` with a ``reason`` in the details, never a silent pass.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

import duckdb
import pandas as pd

from deskbrief.calendars import is_business_day, prev_business_day
from deskbrief.config import CalendarName, Config, Tolerance
from deskbrief.logging import get_logger
from deskbrief.models import CheckType, DQResult, DQStatus, RunContext, XResult
from deskbrief.schemas import to_frame
from deskbrief.timeutil import ensure_utc, et_date, et_to_utc

_log = get_logger(component="crosschecks")

# ---------------------------------------------------------------------------
# bps math
# ---------------------------------------------------------------------------


def abs_level_bps(left: float, right: float) -> float:
    """Absolute level difference of two yields in percent: ``left - right`` in bp.

    1 bp = 0.01 percentage point, so 4.26 vs 4.25 is +1.0 bp.
    """
    return (left - right) * 100.0


def rel_bps(left: float, right: float) -> float:
    """Relative difference of ``left`` against reference ``right`` in bp (1 bp = 0.01%)."""
    if right == 0:
        raise ZeroDivisionError("reference value is zero")
    return (left - right) / abs(right) * 1e4


def level_diff_bps(left: float, right: float, unit: str) -> tuple[float, str]:
    """Level difference in bp and its basis: absolute for yields (``pct``), else relative."""
    if unit == "pct":
        return abs_level_bps(left, right), "absolute_pct_points"
    return rel_bps(left, right), "relative"


def simple_return(prev: float, cur: float) -> float:
    """Decimal return ``cur / prev - 1`` (``prev`` must be non-zero)."""
    if prev == 0:
        raise ZeroDivisionError("previous value is zero")
    return cur / prev - 1.0


def return_diff_bps(ret_a: float, ret_b: float) -> float:
    """Difference of two *decimal* returns in bp (0.0010 vs 0.0004 is +6.0 bp)."""
    return (ret_a - ret_b) * 1e4


def status_for(diff_bps: float, warn_bps: float, fail_bps: float) -> DQStatus:
    """pass / warn / fail from ``|diff_bps|``; equality with a threshold does not breach it."""
    mag = round(abs(diff_bps), 9)  # kill float noise so exact-boundary cases are stable
    if mag > fail_bps:
        return DQStatus.FAIL
    if mag > warn_bps:
        return DQStatus.WARN
    return DQStatus.PASS


# ---------------------------------------------------------------------------
# reading observations (point-in-time)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Obs:
    """One observation row as seen at ``ctx.now_utc``."""

    instrument_id: str
    source: str
    dataset: str
    symbol: str
    field: str
    unit: str
    as_of: datetime
    ingested_at: datetime
    value: float


def et_day_bounds(d: date) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` of the ET calendar date ``d`` (DST-aware)."""
    return et_to_utc(d, "00:00"), et_to_utc(d + timedelta(days=1), "00:00")


def query_obs(
    ctx: RunContext,
    duck: duckdb.DuckDBPyConnection,
    *,
    source: str,
    dataset: str,
    interval: str,
    start: datetime,
    end: datetime,
    fields: Sequence[str],
) -> list[Obs]:
    """Rows known at ``ctx.now_utc`` (``obs_pit``) with ``as_of`` in ``[start, end)``."""
    marks = ", ".join("?" for _ in fields)
    sql = (
        "SELECT instrument_id, source, dataset, symbol, field, unit, as_of, ingested_at, value "  # noqa: S608
        "FROM obs_pit(?::TIMESTAMPTZ) "
        "WHERE source = ? AND dataset = ? AND interval = ? "
        f"AND as_of >= ?::TIMESTAMPTZ AND as_of < ?::TIMESTAMPTZ AND field IN ({marks}) "
        "ORDER BY as_of, instrument_id, field"
    )
    params: list[Any] = [ctx.now_utc, source, dataset, interval, start, end, *fields]
    rows = duck.execute(sql, params).fetchall()
    return [
        Obs(r[0], r[1], r[2], r[3], r[4], r[5], ensure_utc(r[6]), ensure_utc(r[7]), float(r[8]))
        for r in rows
    ]


def pick_by_field(rows: Iterable[Obs], fields: Sequence[str]) -> Obs | None:
    """The row with the highest-priority field in ``fields``; latest ``as_of`` breaks ties."""
    best: Obs | None = None
    for r in rows:
        if r.field not in fields:
            continue
        if best is None:
            best = r
            continue
        key = (fields.index(r.field), -r.as_of.timestamp())
        if key < (fields.index(best.field), -best.as_of.timestamp()):
            best = r
    return best


def reference_date(ctx: RunContext, cal: CalendarName | None) -> date:
    """Latest completed ET date to compare: ``<= target_date`` and ``< today (ET)``.

    ``cal`` rolls back to a business day of that calendar; ``None`` rolls back over weekends
    only (FX trades on US holidays). The current, unfinished ET date is never returned (P-009).
    """
    limit = min(ctx.target_date, et_date(ctx.now_utc) - timedelta(days=1))
    if cal is None:
        while limit.weekday() >= 5:
            limit -= timedelta(days=1)
        return limit
    return limit if is_business_day(cal, limit) else prev_business_day(cal, limit)


# ---------------------------------------------------------------------------
# one side of a comparison, and building an XResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Side:
    """One side of a comparison; ``value`` is ``None`` when nothing was found."""

    instrument: str
    source: str
    dataset: str
    unit: str | None
    field: str | None = None
    value: float | None = None
    as_of: datetime | None = None
    ingested_at: datetime | None = None

    @classmethod
    def from_obs(cls, o: Obs) -> Side:
        return cls(
            o.instrument_id, o.source, o.dataset, o.unit, o.field, o.value, o.as_of, o.ingested_at
        )

    def detail(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "source": self.source,
            "dataset": self.dataset,
            "field": self.field,
            "unit": self.unit,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "ingested_at": self.ingested_at.isoformat() if self.ingested_at else None,
        }


def tolerance_for(config: Config, name: str) -> Tolerance:
    """Thresholds of check ``name`` from ``crosschecks.<name>``."""
    tol = getattr(config.crosschecks, name)
    assert isinstance(tol, Tolerance)
    return tol


def group_id_for(config: Config, *instrument_ids: str) -> str | None:
    """First configured group containing any of the instruments (``None`` if none)."""
    wanted = set(instrument_ids)
    for g in config.groups:
        if wanted & set(g.members()):
            return g.id
    return None


def compare(
    ctx: RunContext,
    *,
    check_name: str,
    relation: str,
    group_id: str | None,
    at_time: datetime,
    left: Side,
    right: Side,
    tol: Tolerance,
    ref_date: date,
    extra: Mapping[str, Any] | None = None,
    skip_reason: str | None = None,
) -> XResult:
    """Build the result for ``left`` vs ``right`` (``right`` is the reference).

    ``expected_value`` is the value ``left`` should equal (``right.value``). The result is
    ``skip`` with a ``reason`` when ``skip_reason`` is given, a side has no value, the units
    differ or the reference is zero.
    """
    details: dict[str, Any] = {
        "ref_date": ref_date.isoformat(),
        "left": left.detail(),
        "right": right.detail(),
        **(extra or {}),
    }
    reason = skip_reason
    if reason is None:
        missing = [
            f"{s.instrument}/{s.source}/{s.dataset}" for s in (left, right) if s.value is None
        ]
        if missing:
            reason = "missing side: " + ", ".join(missing)
    if reason is None and left.unit != right.unit:
        reason = f"unit mismatch: {left.unit} vs {right.unit}"
    diff: float | None = None
    status = DQStatus.SKIP
    if reason is None:
        assert left.value is not None
        assert right.value is not None
        try:
            diff, basis = level_diff_bps(left.value, right.value, left.unit or "")
        except ZeroDivisionError:
            reason = "reference value is zero"
        else:
            details["basis"] = basis
            status = status_for(diff, tol.warn_bps, tol.fail_bps)
    if reason is not None:
        details["reason"] = reason
        diff = None
        status = DQStatus.SKIP
    return XResult(
        run_id=ctx.run_id,
        check_name=check_name,
        group_id=group_id,
        target_date=ctx.target_date,
        at_time=at_time,
        relation=relation,
        left_instrument=left.instrument,
        left_source=left.source,
        left_value=left.value,
        right_instrument=right.instrument,
        right_source=right.source,
        right_value=right.value,
        expected_value=right.value,
        diff_bps=diff,
        warn_bps=tol.warn_bps,
        fail_bps=tol.fail_bps,
        status=status,
        details=details,
    )


# ---------------------------------------------------------------------------
# protocol, registry, runner
# ---------------------------------------------------------------------------


class CrossCheck(Protocol):
    """A named cross-check. ``evaluate`` reads via ``duck`` and never writes."""

    name: str

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]: ...


_REGISTRY: dict[str, CrossCheck] = {}


def register(check: CrossCheck) -> CrossCheck:
    """Add ``check`` to the registry (names are unique); returns it for chaining."""
    if check.name in _REGISTRY:
        raise ValueError(f"cross-check {check.name!r} already registered")
    _REGISTRY[check.name] = check
    return check


def load_builtin_checks() -> None:
    """Import every module of this package so each registers itself (T22's included)."""
    pkg = importlib.import_module("deskbrief.crosschecks")
    for mod in pkgutil.iter_modules(pkg.__path__):
        if not mod.name.startswith("_"):
            importlib.import_module(f"deskbrief.crosschecks.{mod.name}")


def registered() -> dict[str, CrossCheck]:
    """Snapshot of the registry (name -> check) after loading the built-in modules."""
    load_builtin_checks()
    return dict(_REGISTRY)


def to_dq_result(x: XResult, evaluated_at: datetime) -> DQResult:
    """The ``consistency`` ``dq_results`` row mirrored from a cross-check row (spec §7.2).

    ``source`` is ``None`` (two sources are involved; both are in ``details``),
    ``instrument_id`` is the left instrument, ``observed`` the signed ``diff_bps`` and
    ``threshold`` the limit the status was judged against (``fail_bps`` on fail, else
    ``warn_bps``).
    """
    return DQResult(
        run_id=x.run_id,
        check_name=x.check_name,
        check_type=CheckType.CONSISTENCY,
        target_date=x.target_date,
        evaluated_at=evaluated_at,
        status=x.status,
        source=None,
        instrument_id=x.left_instrument,
        observed=x.diff_bps,
        threshold=x.fail_bps if x.status is DQStatus.FAIL else x.warn_bps,
        details={
            **x.details,
            "crosscheck_result_id": x.result_id,
            "relation": x.relation,
            "group_id": x.group_id,
            "left_source": x.left_source,
            "right_source": x.right_source,
            "right_instrument": x.right_instrument,
        },
    )


def _xrow(x: XResult) -> dict[str, Any]:
    return {
        "result_id": x.result_id,
        "run_id": x.run_id,
        "check_name": x.check_name,
        "group_id": x.group_id,
        "target_date": x.target_date,
        "at_time": x.at_time,
        "relation": x.relation,
        "left_instrument": x.left_instrument,
        "left_source": x.left_source,
        "left_value": x.left_value,
        "right_instrument": x.right_instrument,
        "right_source": x.right_source,
        "right_value": x.right_value,
        "expected_value": x.expected_value,
        "diff_bps": x.diff_bps,
        "warn_bps": x.warn_bps,
        "fail_bps": x.fail_bps,
        "status": x.status.value,
        "details_json": x.details_json(),
    }


def _drow(d: DQResult) -> dict[str, Any]:
    return {
        "result_id": d.result_id,
        "run_id": d.run_id,
        "check_name": d.check_name,
        "check_type": d.check_type.value,
        "source": d.source,
        "instrument_id": d.instrument_id,
        "target_date": d.target_date,
        "evaluated_at": d.evaluated_at,
        "status": d.status.value,
        "observed": d.observed,
        "threshold": d.threshold,
        "details_json": d.details_json(),
    }


def _crashed_check_result(ctx: RunContext, name: str, exc: Exception) -> XResult:
    """A crashed check must not block the others (spec §2.4): record it as one skip row."""
    tol = getattr(ctx.config.crosschecks, name, None)
    warn, fail = (tol.warn_bps, tol.fail_bps) if isinstance(tol, Tolerance) else (0.0, 0.0)
    return XResult(
        run_id=ctx.run_id,
        check_name=name,
        group_id=None,
        target_date=ctx.target_date,
        at_time=ctx.now_utc,
        relation="error",
        left_instrument="*",
        left_source="*",
        left_value=None,
        right_instrument="*",
        right_source="*",
        right_value=None,
        expected_value=None,
        diff_bps=None,
        warn_bps=warn,
        fail_bps=fail,
        status=DQStatus.SKIP,
        details={"reason": f"check raised {type(exc).__name__}: {exc}"},
    )


def run_crosschecks(
    ctx: RunContext,
    duck: duckdb.DuckDBPyConnection,
    *,
    only: Iterable[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the registered checks (all, or the names in ``only``) in name order.

    Returns ``(crosscheck_results, dq_results)`` frames: the §7.2 ``crosscheck_results``
    table and the mirrored ``consistency`` rows for ``dq_results``. Nothing is written; the
    runner flushes them once per run (P-008). A check that raises is logged and recorded as one
    ``skip`` row so the others still run.
    """
    checks = registered()
    names = sorted(checks) if only is None else sorted(set(only))
    unknown = [n for n in names if n not in checks]
    if unknown:
        raise KeyError(f"unknown cross-checks: {unknown}")
    results: list[XResult] = []
    for name in names:
        try:
            results.extend(checks[name].evaluate(ctx, duck))
        except Exception as exc:
            _log.error("crosscheck_failed", check=name, error=str(exc))
            results.append(_crashed_check_result(ctx, name, exc))
    xdf = to_frame("crosscheck_results", [_xrow(x) for x in results])
    ddf = to_frame("dq_results", [_drow(to_dq_result(x, ctx.now_utc)) for x in results])
    return xdf, ddf


__all__ = [
    "CrossCheck",
    "Obs",
    "Side",
    "abs_level_bps",
    "compare",
    "et_day_bounds",
    "group_id_for",
    "level_diff_bps",
    "load_builtin_checks",
    "pick_by_field",
    "query_obs",
    "reference_date",
    "register",
    "registered",
    "rel_bps",
    "return_diff_bps",
    "run_crosschecks",
    "simple_return",
    "status_for",
    "to_dq_result",
    "tolerance_for",
]
