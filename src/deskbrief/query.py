"""Read-only query API over a ``store.duck.connect`` connection (spec §7.3, §9, §10).

One small, stable surface shared by the morning report (``report/model.py``) and the dashboard.
Nothing here writes, and nothing opens a socket: callers pass the DuckDB connection.

Functions
---------
``latest_snapshot(duck, at)``                  latest value per instrument as of ``at``
``overnight_changes(duck, report_date, t)``    last value vs its baseline, labelled per P-009
``health_summary(duck, target_date)``          DQ / cross-check counts, issues, source status
``series(duck, instrument_id, days, at)``      one official daily value per ET date
``series_all(duck, days, at)``                 the same for every instrument in one query
``lineage(duck, instrument_id, at)``           provenance of the latest value
``lineages(duck, rows)``                       provenance for many value rows in one query

Every value row (:class:`ValueRow`) carries ``source``, ``tier``, ``field``, ``unit``,
``as_of`` and ``is_proxy``. All values come from ``obs_pit(at)`` (point-in-time), so a report
is reproducible. ``interval = '1d'`` rows dated on the current ET date are excluded: that day
is unfinished (P-009). Instruments missing from ``dim_instrument`` (config) are ignored.

Overnight change (P-009)
------------------------
* price family (``field`` close/last; indices, futures, ETFs, FX ticks): change in **%** (VIX in
  index points) versus the **previous NYSE close** (``field = close``, 1d, on the previous XNYS
  business day: Monday -> Friday, holidays skipped).
* yields: change in **bp** versus the **previous official value**.
* reference rates (``field = rate``, e.g. the ECB fix) and macro series: change versus the
  **previous official value** (% for FX rates, percentage points / points otherwise).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import duckdb

from deskbrief.calendars import prev_business_day
from deskbrief.store.duck import DAILY_CLOSE_FIELDS
from deskbrief.timeutil import ensure_utc, et_date

#: fields that can be "the value" of an instrument (never open/high/low/bid/ask/volume)
VALUE_FIELDS: tuple[str, ...] = ("close", "last", "yield", "rate")
VALUE_INTERVALS: tuple[str, ...] = ("tick", "1m", "1d", "1mo")
OFFICIAL_INTERVALS: tuple[str, ...] = ("1d", "1mo")
#: how far back a baseline is searched (monthly macro needs two releases)
BASELINE_LOOKBACK_DAYS = 100

BASIS_NYSE_CLOSE = "previous NYSE close"
BASIS_OFFICIAL = "previous official value"

_ET_DATE = "CAST(timezone('America/New_York', o.as_of) AS DATE)"
_FIELD_RANK = "CASE o.field WHEN 'close' THEN 0 WHEN 'yield' THEN 1 WHEN 'rate' THEN 2 ELSE 3 END"
_VALUE_FIELDS_SQL = ", ".join(f"'{f}'" for f in VALUE_FIELDS)
_VALUE_INTERVALS_SQL = ", ".join(f"'{i}'" for i in VALUE_INTERVALS)
_OFFICIAL_INTERVALS_SQL = ", ".join(f"'{i}'" for i in OFFICIAL_INTERVALS)
_DAILY_FIELDS_SQL = ", ".join(f"'{f}'" for f in DAILY_CLOSE_FIELDS)

_SELECT = (
    "SELECT o.instrument_id, d.name, d.asset_class, d.kind, d.is_proxy, d.label, o.value, "
    "o.unit, o.field, o.interval, o.source, o.dataset, o.symbol, o.tier, o.as_of, "
    "o.is_fallback, o.is_derived, o.fetch_id, o.run_id, o.ingested_at, o.parser_version "
    "FROM obs_pit(?) o JOIN dim_instrument d ON d.instrument_id = o.instrument_id"
)
_ORDER = f"o.tier DESC, {_FIELD_RANK}, o.source, o.dataset, o.ingested_at DESC, o.fetch_id DESC"


@dataclass(frozen=True)
class ValueRow:
    """One observed value with everything needed to label and trace it."""

    instrument_id: str
    name: str
    asset_class: str
    kind: str
    is_proxy: bool
    proxy_label: str | None
    value: float
    unit: str
    field: str
    interval: str
    source: str
    dataset: str
    symbol: str
    tier: int
    as_of: datetime
    is_fallback: bool
    is_derived: bool
    fetch_id: str
    run_id: str
    ingested_at: datetime
    parser_version: str


@dataclass(frozen=True)
class ChangeRow:
    """``last`` against its baseline. ``change`` is None unless ``status == "ok"``."""

    last: ValueRow
    previous: ValueRow | None
    family: str  # price | yield | rate | macro
    basis: str  # BASIS_NYSE_CLOSE | BASIS_OFFICIAL
    change: float | None
    change_unit: str  # pct | bp | pts | pp
    status: str  # ok | no_baseline | no_move_yet
    note: str | None = None


@dataclass(frozen=True)
class Lineage:
    """Where a value came from: observation lineage joined to its ``source_log`` fetch."""

    value: ValueRow
    requested_at: datetime | None
    responded_at: datetime | None
    latency_ms: int | None
    http_status: int | None
    fetch_status: str | None
    raw_key: str | None
    sha256: str | None
    rows_parsed: int | None


@dataclass(frozen=True)
class CheckItem:
    kind: str  # dq | crosscheck
    name: str
    status: str  # warn | fail
    check_type: str | None
    source: str | None
    instrument_id: str | None
    group_id: str | None
    observed: float | None
    threshold: float | None


@dataclass(frozen=True)
class SourceHealth:
    source: str
    status: str  # ok | failed | skipped | missing
    n_fetches: int
    n_failed: int


@dataclass(frozen=True)
class HealthSummary:
    target_date: date
    run_id: str | None
    run_status: str | None
    n_pass: int
    n_warn: int
    n_fail: int
    n_skip: int
    n_xcheck_pass: int
    n_xcheck_warn: int
    n_xcheck_fail: int
    issues: tuple[CheckItem, ...]
    sources: tuple[SourceHealth, ...]

    @property
    def n_checks(self) -> int:
        """DQ results plus cross-checks recorded for ``target_date``."""
        return (
            self.n_pass
            + self.n_warn
            + self.n_fail
            + self.n_skip
            + self.n_xcheck_pass
            + self.n_xcheck_warn
            + self.n_xcheck_fail
        )


# ---------------------------------------------------------------------------
# value rows
# ---------------------------------------------------------------------------


def _rows(duck: duckdb.DuckDBPyConnection, sql: str, params: list[Any]) -> list[ValueRow]:
    # sql is assembled from module constants only; user values are bound parameters
    return [ValueRow(*r) for r in duck.execute(sql, params).fetchall()]


def _current_day_filter() -> str:
    return f"NOT (o.interval = '1d' AND {_ET_DATE} >= ?)"


def _snapshot(
    duck: duckdb.DuckDBPyConnection,
    at: datetime,
    cutoff: date,
    instrument_id: str | None = None,
) -> list[ValueRow]:
    at = ensure_utc(at)
    where = (
        f"o.as_of <= ? AND o.field IN ({_VALUE_FIELDS_SQL}) "
        f"AND o.interval IN ({_VALUE_INTERVALS_SQL}) AND {_current_day_filter()}"
    )
    params: list[Any] = [at, at, cutoff]
    if instrument_id is not None:
        where += " AND o.instrument_id = ?"
        params.append(instrument_id)
    sql = (
        f"{_SELECT} WHERE {where} "
        f"QUALIFY row_number() OVER (PARTITION BY o.instrument_id "
        f"ORDER BY o.as_of DESC, {_ORDER}) = 1 ORDER BY o.instrument_id"
    )
    return _rows(duck, sql, params)


def latest_snapshot(duck: duckdb.DuckDBPyConnection, at: datetime) -> tuple[ValueRow, ...]:
    """Latest value per instrument with ``as_of <= at``, point-in-time at ``at``.

    Ordered by ``instrument_id``. Instruments with no value are simply absent.
    """
    return tuple(_snapshot(duck, at, et_date(at)))


def _official(duck: duckdb.DuckDBPyConnection, at: datetime, cutoff: date) -> list[ValueRow]:
    at = ensure_utc(at)
    sql = (
        f"{_SELECT} WHERE o.as_of <= ? AND o.as_of > ? "
        f"AND o.field IN ({_VALUE_FIELDS_SQL}) AND o.interval IN ({_OFFICIAL_INTERVALS_SQL}) "
        f"AND {_current_day_filter()} ORDER BY o.instrument_id, o.as_of DESC, {_ORDER}"
    )
    since = at - timedelta(days=BASELINE_LOOKBACK_DAYS)
    return _rows(duck, sql, [at, at, since, cutoff])


# ---------------------------------------------------------------------------
# overnight change
# ---------------------------------------------------------------------------


def family_of(row: ValueRow) -> str:
    """``macro`` | ``yield`` | ``rate`` | ``price``; decides the unit and basis of a change."""
    if row.kind == "macro":
        return "macro"
    if row.field == "yield" or row.kind == "yield":
        return "yield"
    if row.field == "rate":
        return "rate"
    return "price"


def measure(row: ValueRow, previous: float, last: float) -> tuple[float, str]:
    """The change from ``previous`` to ``last`` in the unit used to display ``row``."""
    family = family_of(row)
    if family == "yield":
        return (last - previous) * 100.0, "bp"
    if row.asset_class == "vol":
        return last - previous, "pts"
    if family in ("price", "rate") and (row.unit != "pct" or family == "price"):
        return ((last / previous - 1.0) * 100.0 if previous else 0.0), "pct"
    return last - previous, "pp" if row.unit == "pct" else "pts"


def _change(last: ValueRow, previous: ValueRow | None, basis: str, status: str) -> ChangeRow:
    family = family_of(last)
    if previous is None or status != "ok":
        unit = measure(last, 1.0, 1.0)[1]
        return ChangeRow(last, previous, family, basis, None, unit, status)
    change, unit = measure(last, previous.value, last.value)
    return ChangeRow(last, previous, family, basis, change, unit, "ok")


def _et(row: ValueRow) -> date:
    return et_date(row.as_of)


def overnight_changes(
    duck: duckdb.DuckDBPyConnection, report_date: date, report_time: datetime
) -> tuple[ChangeRow, ...]:
    """One :class:`ChangeRow` per instrument that has a value, ordered by ``instrument_id``.

    ``report_time`` is the point-in-time cut (what was known when the brief was written);
    ``report_date`` is the ET date being reported. Price-family baselines are the ``close`` of
    the previous XNYS business day; everything else uses the previous official value.
    """
    last_rows = _snapshot(duck, report_time, report_date)
    by_inst: dict[str, list[ValueRow]] = {}
    for r in _official(duck, report_time, report_date):
        by_inst.setdefault(r.instrument_id, []).append(r)
    nyse_prev = prev_business_day("XNYS", report_date)
    out: list[ChangeRow] = []
    for last in last_rows:
        history = by_inst.get(last.instrument_id, [])
        if family_of(last) == "price":
            out.append(_price_change(last, history, nyse_prev))
        else:
            out.append(_official_change(last, history))
    return tuple(out)


def _price_change(last: ValueRow, history: list[ValueRow], nyse_prev: date) -> ChangeRow:
    if last.interval == "1d" and _et(last) >= nyse_prev:
        return _change(last, None, BASIS_NYSE_CLOSE, "no_move_yet")
    closes = [
        r for r in history if r.field == "close" and r.interval == "1d" and _et(r) == nyse_prev
    ]
    return _change(
        last, closes[0] if closes else None, BASIS_NYSE_CLOSE, "ok" if closes else "no_baseline"
    )


def _official_change(last: ValueRow, history: list[ValueRow]) -> ChangeRow:
    earlier = [r for r in history if r.as_of < last.as_of]
    if last.interval in OFFICIAL_INTERVALS:
        same = [
            r
            for r in earlier
            if (r.source, r.dataset, r.field) == (last.source, last.dataset, last.field)
        ]
        earlier = same or earlier
    previous = earlier[0] if earlier else None  # history is newest first, best tier first
    return _change(last, previous, BASIS_OFFICIAL, "ok" if previous else "no_baseline")


# ---------------------------------------------------------------------------
# series
# ---------------------------------------------------------------------------


def series_all(
    duck: duckdb.DuckDBPyConnection, days: int, at: datetime
) -> dict[str, tuple[ValueRow, ...]]:
    """Official daily values of the last ``days`` calendar days, oldest first, per instrument.

    One value per instrument and ET date (tier 3, then 2, then 1; field close, yield, rate),
    point-in-time at ``at``; the current ET date is excluded.
    """
    return _series(duck, days, at, None)


def series(
    duck: duckdb.DuckDBPyConnection, instrument_id: str, days: int, at: datetime
) -> tuple[ValueRow, ...]:
    """:func:`series_all` for one instrument (empty tuple if it has no data)."""
    return _series(duck, days, at, instrument_id).get(instrument_id, ())


def _series(
    duck: duckdb.DuckDBPyConnection, days: int, at: datetime, instrument_id: str | None
) -> dict[str, tuple[ValueRow, ...]]:
    at = ensure_utc(at)
    where = (
        f"o.as_of <= ? AND o.as_of > ? AND o.interval = '1d' AND o.field IN ({_DAILY_FIELDS_SQL}) "
        f"AND {_current_day_filter()}"
    )
    params: list[Any] = [at, at, at - timedelta(days=days), et_date(at)]
    if instrument_id is not None:
        where += " AND o.instrument_id = ?"
        params.append(instrument_id)
    sql = (
        f"{_SELECT} WHERE {where} "
        f"QUALIFY row_number() OVER (PARTITION BY o.instrument_id, {_ET_DATE} "
        f"ORDER BY {_ORDER}) = 1 ORDER BY o.instrument_id, o.as_of"
    )
    out: dict[str, list[ValueRow]] = {}
    for r in _rows(duck, sql, params):
        out.setdefault(r.instrument_id, []).append(r)
    return {k: tuple(v) for k, v in out.items()}


# ---------------------------------------------------------------------------
# lineage
# ---------------------------------------------------------------------------


def lineages(duck: duckdb.DuckDBPyConnection, rows: Sequence[ValueRow]) -> tuple[Lineage, ...]:
    """Provenance of ``rows`` (same order), joined to ``source_log`` by ``fetch_id``."""
    ids = sorted({r.fetch_id for r in rows})
    logs: dict[str, tuple[Any, ...]] = {}
    if ids:
        marks = ", ".join("?" for _ in ids)
        sql = (
            "SELECT fetch_id, requested_at, responded_at, latency_ms, http_status, status, "  # noqa: S608
            f"raw_key, sha256, rows_parsed FROM source_log WHERE fetch_id IN ({marks}) "
            "QUALIFY row_number() OVER (PARTITION BY fetch_id ORDER BY attempt DESC) = 1"
        )
        logs = {r[0]: r[1:] for r in duck.execute(sql, ids).fetchall()}
    empty: tuple[Any, ...] = (None,) * 8
    return tuple(Lineage(r, *logs.get(r.fetch_id, empty)) for r in rows)


def lineage(duck: duckdb.DuckDBPyConnection, instrument_id: str, at: datetime) -> Lineage | None:
    """Provenance of the latest value of ``instrument_id`` at ``at`` (None if no value)."""
    rows = _snapshot(duck, at, et_date(at), instrument_id)
    return lineages(duck, rows)[0] if rows else None


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

_STATUS_RANK = {"fail": 0, "warn": 1}
_FETCH_OK = ("ok", "empty")


def health_summary(
    duck: duckdb.DuckDBPyConnection,
    target_date: date,
    *,
    expected_sources: Collection[str] | None = None,
) -> HealthSummary:
    """Quality-check and source status for ``target_date`` (empty tables give zero counts).

    Re-evaluated checks count once (the latest evaluation). ``expected_sources`` defaults to
    every source with a feed in the config; a source with no fetch that day is ``missing``.
    """
    dq = duck.execute(
        "SELECT check_name, check_type, source, instrument_id, status, observed, threshold "
        "FROM dq WHERE target_date = ? QUALIFY row_number() OVER (PARTITION BY check_name, "
        "coalesce(source, ''), coalesce(instrument_id, '') ORDER BY evaluated_at DESC, "
        "result_id DESC) = 1",
        [target_date],
    ).fetchall()
    xc = duck.execute(
        "SELECT check_name, group_id, left_source, right_source, status, diff_bps, "
        "warn_bps, fail_bps, left_instrument FROM xchecks WHERE target_date = ? "
        "QUALIFY row_number() OVER (PARTITION BY check_name, coalesce(group_id, ''), "
        "left_instrument, right_instrument, left_source, right_source "
        "ORDER BY at_time DESC, result_id DESC) = 1",
        [target_date],
    ).fetchall()
    issues: list[CheckItem] = [
        CheckItem("dq", n, st, ct, src, inst, None, obs, thr)
        for n, ct, src, inst, st, obs, thr in dq
        if st in _STATUS_RANK
    ]
    issues += [
        CheckItem(
            "crosscheck", n, st, None, f"{ls}/{rs}", li, grp, diff, warn if st == "warn" else fail
        )
        for n, grp, ls, rs, st, diff, warn, fail, li in xc
        if st in _STATUS_RANK
    ]
    issues.sort(
        key=lambda i: (
            _STATUS_RANK[i.status],
            i.kind,
            i.name,
            i.instrument_id or "",
            i.source or "",
        )
    )

    def count(rows: list[tuple[Any, ...]], idx: int, status: str) -> int:
        return sum(1 for r in rows if r[idx] == status)

    run = duck.execute(
        "SELECT run_id, status FROM runs WHERE target_date = ? "
        "ORDER BY started_at DESC, run_id DESC LIMIT 1",
        [target_date],
    ).fetchone()
    return HealthSummary(
        target_date=target_date,
        run_id=run[0] if run else None,
        run_status=run[1] if run else None,
        n_pass=count(dq, 4, "pass"),
        n_warn=count(dq, 4, "warn"),
        n_fail=count(dq, 4, "fail"),
        n_skip=count(dq, 4, "skip"),
        n_xcheck_pass=count(xc, 4, "pass"),
        n_xcheck_warn=count(xc, 4, "warn"),
        n_xcheck_fail=count(xc, 4, "fail"),
        issues=tuple(issues),
        sources=_source_health(duck, target_date, expected_sources),
    )


def _source_health(
    duck: duckdb.DuckDBPyConnection, target_date: date, expected: Collection[str] | None
) -> tuple[SourceHealth, ...]:
    if expected is None:
        expected = [r[0] for r in duck.execute("SELECT DISTINCT source FROM dim_feed").fetchall()]
    rows = duck.execute(
        "SELECT source, status, count(*) FROM source_log WHERE "
        "CAST(timezone('America/New_York', requested_at) AS DATE) = ? GROUP BY source, status",
        [target_date],
    ).fetchall()
    stats: dict[str, dict[str, int]] = {}
    for source, status, n in rows:
        stats.setdefault(source, {})[status] = n
    out: list[SourceHealth] = []
    for source in sorted(set(expected) | set(stats)):
        by_status = stats.get(source, {})
        total = sum(by_status.values())
        ok = sum(by_status.get(s, 0) for s in _FETCH_OK)
        skipped = by_status.get("skipped_calendar", 0)
        if total == 0:
            status = "missing"
        elif ok:
            status = "ok"
        elif skipped == total:
            status = "skipped"
        else:
            status = "failed"
        out.append(SourceHealth(source, status, total, total - ok - skipped))
    return tuple(out)


# ---------------------------------------------------------------------------
# labels shared by report and dashboard (P-009)
# ---------------------------------------------------------------------------


def field_label(row: ValueRow) -> str:
    """What the value *is*: never calls a yield or an ECB fix a close."""
    if row.field == "rate":
        return "ECB reference rate" if row.source == "ecb" else "rate"
    if row.field == "yield" or row.kind == "yield":
        return "yield"
    return row.field  # close | last


def value_label(row: ValueRow) -> str:
    """Instrument name plus its field label, e.g. ``Euro / US dollar (ECB reference rate)``."""
    label = field_label(row)
    return row.name if label.lower() in row.name.lower() else f"{row.name} ({label})"
