"""Read-only query API over a ``store.duck.connect`` connection (spec §7.3, §9, §10).

One small, stable surface shared by the morning report (``report/model.py``) and the dashboard.
Nothing here writes, and nothing opens a socket: callers pass the DuckDB connection.

Functions
---------
``latest_snapshot(duck, at)``                  latest value per instrument as of ``at``
``overnight_changes(duck, report_date, t)``    last value vs its baseline, labelled per P-009
``health_summary(duck, target_date)``          DQ / cross-check counts, issues, source status
                                               (mirrored ``consistency`` rows count once)
``instrument_flags(duck, target_date)``        worst check / cross-check status per instrument
``latest_check_run(duck, now)``                the latest run with check rows known at ``now``
``series(duck, instrument_id, days, at)``      one official daily value per ET date
``series_all(duck, days, at)``                 the same for every instrument in one query
``lineage(duck, instrument_id, at)``           provenance of the latest value
``lineages(duck, rows)``                       provenance for many value rows in one query

Every value row (:class:`ValueRow`) carries ``source``, ``tier``, ``field``, ``unit``,
``as_of`` and ``is_proxy``. All values come from ``obs_pit(at)`` (point-in-time), so a report
is reproducible. ``interval = '1d'`` rows dated on the current ET date are excluded: that day
is unfinished (P-009). Instruments missing from ``dim_instrument`` (config) are ignored.

``exclude_tiers`` (``latest_snapshot``, ``overnight_changes``) drops those tiers before anything
is picked, so a hosted deployment that may not show tier 1 falls back to the best remaining
value (a tier 2 or 3 row, even an older one) instead of hiding the instrument. An instrument whose
every row is excluded is absent, as if it had no data. Baselines honour the same exclusion.

Health (``health_summary``, ``instrument_flags``, the cross-check items) reads one run for the
date: the newest run that wrote any row to ``dq_results`` or ``crosscheck_results`` (one run id
for both tables). Re-runs keep every run's rows (P-008), but a check that failed in a superseded
run does not count. ``health_summary`` and ``instrument_flags`` take an optional ``as_of``: when
given, check rows evaluated after it (a cross-check row from its run's ``started_at``, since its
``at_time`` is market time), ``runs`` rows started after it and ``source_log`` rows requested
after it are ignored, so a re-run stored later cannot change a brief written earlier.
``None`` (the default) reads everything, as before. :func:`latest_check_run` finds the date to
read on a day without a run of its own (a weekend or holiday).

Overnight change (P-009)
------------------------
* price family (``field`` close/last; indices, futures, ETFs, FX ticks): change in **%** (VIX in
  index points) versus the **previous NYSE close** (``field = close``, 1d, on the previous XNYS
  business day: Monday -> Friday, holidays skipped).
* price family when ``report_date`` is **not an XNYS session** (a weekend or holiday; T60 bug 4),
  for every price interval (tick, 1m, 1d). Let L be the last XNYS session before ``report_date``
  and S the ET date of the last value:

  - S on or before L (Saturday, including Friday ticks stored after the close): the baseline is
    the 1d close of the session before S, and the basis names that session's weekday
    (``"Thu close"``). Such a row is never "no move yet".
  - S after L (a market that traded on an XNYS holiday: ES futures on MLK Monday, FX on
    Thanksgiving, N225 on Good Friday seen on Saturday): the baseline stays L's own close and the
    basis is ``"<L's weekday> close"`` (``"Fri close"``).

  On XNYS sessions nothing changes, including "no move yet" before the morning run. Scheduled
  morning runs only happen on XNYS sessions (``pipeline/jobs.py``), so the scheduled email is
  unchanged; a manual ``--date`` run for a day that is not a session does use this rule.
* yields: change in **bp** versus the **previous official value**.
  A tick compared with an official baseline carries a note saying so (different sources).
* reference rates (``field = rate``, e.g. the ECB fix) and macro series: change versus the
  **previous official value** (% for FX rates, percentage points / points otherwise).
* index levels, positions and open interest (``field`` level / net_position / open_interest,
  P-021) are macro-family: points against the previous official value, never a % of a level
  that may be zero or negative (NFCI).
* weekly series (``interval = 1w``): change versus the **previous weekly value** of the same
  source, dataset and field (week-on-week). Like ``1d`` rows, a ``1w`` row dated on the current
  ET date is excluded.

Own-market bases and ranking (P-024, T61)
-----------------------------------------
* FX pairs (``kind = fx``, price family) compare with their **own previous New York close**: the
  stored quote of the same source, dataset and field nearest 17:00 ET (within two hours) on the
  latest weekday whose 17:00 ET is before the last value, walking back up to
  ``FX_CLOSE_LOOKBACK`` weekdays if none was stored. Yahoo's daily FX bars are not used: their
  close sits at the London-day open (live probe 2026-11-24, recorded in the T61 PR).
* Foreign indices (:data:`HOME_EXCHANGES`: N225, FTSE, DAX) compare with the close of their own
  exchange's previous session before the last value's exchange-local date (``"previous Tokyo
  close"``), whatever the NYSE did that day.
* A price baseline is like-for-like with the value when it has the same source, dataset and field,
  or the pair is in :data:`LIKE_FOR_LIKE` (a quote or trade against the same security's daily
  close). Like-for-like closes are preferred; a **futures** price never falls back to another
  series (Brent's ``BZ=F`` is never compared with FRED's spot ``DCOILBRENTEU``).
* A yield quote (a Yahoo ``^IRX`` tick) compares with its own like-for-like close before the
  quote's date when one is stored (``"previous Yahoo close"``); otherwise with the previous
  official value, noted, as before.
* A move over more than one period is shown with a note but never ranked: an FX baseline that
  had to walk back past a missing 17:00 close, a yield quote whose own close is older than the
  previous SIFMA session.
* ``ChangeRow.rankable`` is True only for an ``ok`` change whose baseline is like-for-like and
  whose value is new since the previous scheduled brief (the previous XNYS session at the
  report's ET clock time). "New" compares with the value that brief showed, point-in-time on
  ``ingested_at``: a print first stored after it is new even if dated earlier (FRED's day-late
  series), once; it must belong to a later trading day, or the same day at a later instant.
  ``unranked_reason`` says why not. The brief scores sigma only for rankable changes.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

from deskbrief.calendars import Calendar, ExchangeCalendar, is_business_day, prev_business_day
from deskbrief.schemas import current_rows
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import DAILY_CLOSE_FIELDS
from deskbrief.timeutil import ensure_utc, et_date, et_to_utc, to_et

#: fields that can be "the value" of an instrument (never open/high/low/bid/ask/volume)
VALUE_FIELDS: tuple[str, ...] = (
    "close", "last", "yield", "rate", "level", "net_position", "open_interest",
)  # fmt: skip
#: value fields that are neither a price nor a rate: changes are in points (P-021)
LEVEL_FIELDS: tuple[str, ...] = ("level", "net_position", "open_interest")
VALUE_INTERVALS: tuple[str, ...] = ("tick", "1m", "1d", "1w", "1mo")
OFFICIAL_INTERVALS: tuple[str, ...] = ("1d", "1w", "1mo")
#: intervals whose rows are dated (``as_of`` = 00:00 ET of the date): today's is unfinished
DATED_INTERVALS: tuple[str, ...] = ("1d", "1w")
#: how far back a baseline is searched (monthly macro needs two releases)
BASELINE_LOOKBACK_DAYS = 100

#: decimals kept in a computed change (see :func:`measure`)
CHANGE_DECIMALS = 9

BASIS_NYSE_CLOSE = "previous NYSE close"
BASIS_OFFICIAL = "previous official value"
BASIS_WEEKLY = "previous weekly value"
BASIS_NY_CLOSE = "previous NY close"

#: how a provider is named in basis text ("previous Yahoo close"); unknown ids are shown as is
SOURCE_NAMES: Mapping[str, str] = {
    "yahoo": "Yahoo", "stooq": "Stooq", "cboe": "Cboe", "alpaca": "Alpaca",
    "twelvedata": "Twelve Data", "treasury": "Treasury", "fred": "FRED", "ecb": "ECB",
    "cftc": "CFTC", "fiscaldata": "Treasury", "edgar": "SEC EDGAR",
}  # fmt: skip


def source_name(source: str) -> str:
    return SOURCE_NAMES.get(source, source)


#: FX: the New York close is 17:00 ET; a stored quote within two hours of it stands in for it
FX_CLOSE_HHMM = "17:00"
FX_CLOSE_WINDOW = timedelta(hours=2)
#: how many weekday closes back an FX baseline is searched (a US holiday stores no quote)
FX_CLOSE_LOOKBACK = 4


#: the business-day calendar of each official daily series (``config.datasets``): a daily value
#: whose previous stored value is older than the previous business day spans a gap (T61 review)
_OFFICIAL_CALENDARS: Mapping[tuple[str, str], Calendar] = {
    ("treasury", "par_curve"): "SIFMA_US",
    ("fred", "series"): "SIFMA_US",
    ("ecb", "eurofxref"): "TARGET",
}


@dataclass(frozen=True)
class HomeExchange:
    """A foreign index's own exchange: its calendar, time zone and basis text."""

    calendar: ExchangeCalendar
    tz: str
    basis: str


#: foreign indices measured against their own exchange's previous session (P-024)
HOME_EXCHANGES: Mapping[str, HomeExchange] = {
    "N225": HomeExchange("XTKS", "Asia/Tokyo", "previous Tokyo close"),
    "FTSE": HomeExchange("XLON", "Europe/London", "previous London close"),
    "DAX": HomeExchange("XETR", "Europe/Berlin", "previous Frankfurt close"),
}

#: (source, dataset, field) of a quote or trade on the listed security itself
_QUOTE_SERIES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("yahoo", "quote", "last"),
        ("alpaca", "iex_latest", "last"),
        ("alpaca", "iex_bars_1m", "close"),
        ("alpaca", "sip_bars_1m", "close"),
    }
)
#: (source, dataset, field) of that same security's (or index's) own daily close
_OWN_CLOSE_SERIES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("yahoo", "history", "close"),
        ("stooq", "daily", "close"),
        ("cboe", "vix_history", "close"),  # Cboe's own close of the index Yahoo quotes
    }
)
#: (value series, baseline series) pairs that measure the same thing (P-024 like-for-like).
#: Never listed: a spot series (FRED ``DCOIL*``) for a future, an official par yield (Treasury)
#: for a quoted rate (``^IRX`` is a discount rate), a reference fix (ECB) for a quote.
LIKE_FOR_LIKE: frozenset[tuple[tuple[str, str, str], tuple[str, str, str]]] = frozenset(
    (value, base) for value in _QUOTE_SERIES | _OWN_CLOSE_SERIES for base in _OWN_CLOSE_SERIES
)


def session_close_basis(session: date) -> str:
    """Basis of a price change off an XNYS session: ``"Thu close"`` for a Thursday baseline."""
    return f"{session:%a} close"


_ET_DATE = "CAST(timezone('America/New_York', o.as_of) AS DATE)"
_FIELD_RANK = (
    "CASE o.field WHEN 'close' THEN 0 WHEN 'yield' THEN 1 WHEN 'rate' THEN 2 WHEN 'level' THEN 3 "
    "WHEN 'net_position' THEN 4 WHEN 'open_interest' THEN 5 ELSE 6 END"
)
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
    """``last`` against its baseline. ``change`` is None unless ``status == "ok"``.

    ``rankable`` (P-024): the change may be scored and ranked (like-for-like baseline, value new
    since the previous scheduled brief). Otherwise ``unranked_reason`` says why, for an ``ok``
    change; it is None for a change that has no number.
    """

    last: ValueRow
    previous: ValueRow | None
    family: str  # price | yield | rate | macro
    basis: str  # BASIS_* | "<weekday> close" | HomeExchange.basis | "previous <source> close"
    change: float | None
    change_unit: str  # pct | bp | pts | pp
    status: str  # ok | no_baseline | no_move_yet
    note: str | None = None
    rankable: bool = False
    unranked_reason: str | None = None


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
    role: str = "value"  # value | baseline: which side of a change this fetch backs


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
    ref_date: date | None = None  # cross-checks: the data date compared (details.ref_date)


@dataclass(frozen=True)
class SourceHealth:
    source: str
    status: str  # ok | failed | skipped | missing
    n_fetches: int
    n_failed: int


@dataclass(frozen=True)
class RunRef:
    """A run that wrote check rows: its target date, id and (if recorded) start time."""

    target_date: date
    run_id: str
    started_at: datetime | None


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
    dated = ", ".join(f"'{i}'" for i in DATED_INTERVALS)
    return f"NOT (o.interval IN ({dated}) AND {_ET_DATE} >= ?)"


def _tier_filter(exclude_tiers: Collection[int]) -> tuple[str, list[Any]]:
    """`` AND o.tier NOT IN (?, ...)`` and its bound values (empty when nothing is excluded)."""
    tiers = sorted(set(exclude_tiers))
    if not tiers:
        return "", []
    return f" AND o.tier NOT IN ({', '.join('?' for _ in tiers)})", [int(t) for t in tiers]


def _snapshot(
    duck: duckdb.DuckDBPyConnection,
    at: datetime,
    cutoff: date,
    instrument_id: str | None = None,
    exclude_tiers: Collection[int] = (),
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
    tier_sql, tier_params = _tier_filter(exclude_tiers)
    where += tier_sql
    params += tier_params
    sql = (
        f"{_SELECT} WHERE {where} "
        f"QUALIFY row_number() OVER (PARTITION BY o.instrument_id "
        f"ORDER BY o.as_of DESC, {_ORDER}) = 1 ORDER BY o.instrument_id"
    )
    return _rows(duck, sql, params)


def latest_snapshot(
    duck: duckdb.DuckDBPyConnection, at: datetime, *, exclude_tiers: Collection[int] = ()
) -> tuple[ValueRow, ...]:
    """Latest value per instrument with ``as_of <= at``, point-in-time at ``at``.

    Ordered by ``instrument_id``. Instruments with no value are simply absent.
    ``exclude_tiers`` removes those tiers first, so the best remaining value is returned.
    """
    return tuple(_snapshot(duck, at, et_date(at), exclude_tiers=exclude_tiers))


def _official(
    duck: duckdb.DuckDBPyConnection,
    at: datetime,
    cutoff: date,
    exclude_tiers: Collection[int] = (),
) -> list[ValueRow]:
    at = ensure_utc(at)
    tier_sql, tier_params = _tier_filter(exclude_tiers)
    sql = (
        f"{_SELECT} WHERE o.as_of <= ? AND o.as_of > ? "
        f"AND o.field IN ({_VALUE_FIELDS_SQL}) AND o.interval IN ({_OFFICIAL_INTERVALS_SQL}) "
        f"AND {_current_day_filter()}{tier_sql} "
        f"ORDER BY o.instrument_id, o.as_of DESC, {_ORDER}"
    )
    since = at - timedelta(days=BASELINE_LOOKBACK_DAYS)
    return _rows(duck, sql, [at, at, since, cutoff, *tier_params])


# ---------------------------------------------------------------------------
# overnight change
# ---------------------------------------------------------------------------


def family_of(row: ValueRow) -> str:
    """``macro`` | ``yield`` | ``rate`` | ``price``; decides the unit and basis of a change."""
    if row.kind == "macro" or row.field in LEVEL_FIELDS:
        return "macro"
    if row.field == "yield" or row.kind == "yield":
        return "yield"
    if row.field == "rate":
        return "rate"
    return "price"


def measure(row: ValueRow, previous: float, last: float) -> tuple[float | None, str]:
    """The change from ``previous`` to ``last`` in the unit used to display ``row``.

    The change is None (missing) when a percentage is asked of a zero baseline. Results are
    rounded to ``CHANGE_DECIMALS`` so float noise (``22.000000000000064``) never reaches the
    serialised model; that is far below any displayed precision.
    """
    family = family_of(row)
    if family == "yield":
        return round((last - previous) * 100.0, CHANGE_DECIMALS), "bp"
    if row.asset_class == "vol":
        return round(last - previous, CHANGE_DECIMALS), "pts"
    if family in ("price", "rate") and (row.unit != "pct" or family == "price"):
        return (
            round((last / previous - 1.0) * 100.0, CHANGE_DECIMALS) if previous else None
        ), "pct"
    return round(last - previous, CHANGE_DECIMALS), "pp" if row.unit == "pct" else "pts"


def _change(
    last: ValueRow,
    previous: ValueRow | None,
    basis: str,
    status: str,
    note: str | None = None,
) -> ChangeRow:
    family = family_of(last)
    if previous is None or status != "ok":
        unit = measure(last, 1.0, 1.0)[1]
        return ChangeRow(last, previous, family, basis, None, unit, status)
    change, unit = measure(last, previous.value, last.value)
    if change is None:
        return ChangeRow(last, previous, family, basis, None, unit, "no_baseline")
    return ChangeRow(last, previous, family, basis, change, unit, "ok", note)


def _et(row: ValueRow) -> date:
    return et_date(row.as_of)


def overnight_changes(
    duck: duckdb.DuckDBPyConnection,
    report_date: date,
    report_time: datetime,
    *,
    exclude_tiers: Collection[int] = (),
) -> tuple[ChangeRow, ...]:
    """One :class:`ChangeRow` per instrument that has a value, ordered by ``instrument_id``.

    ``report_time`` is the point-in-time cut (what was known when the brief was written);
    ``report_date`` is the ET date being reported. Price-family baselines are the ``close`` of
    the previous XNYS business day; everything else uses the previous official value. When
    ``report_date`` is not an XNYS session, price baselines follow the weekend and holiday rule
    of the module docstring. ``exclude_tiers`` applies to the last value and to its baseline alike.
    """
    last_rows = _snapshot(duck, report_time, report_date, exclude_tiers=exclude_tiers)
    by_inst: dict[str, list[ValueRow]] = {}
    for r in _official(duck, report_time, report_date, exclude_tiers):
        by_inst.setdefault(r.instrument_id, []).append(r)
    fx_ids = sorted({r.instrument_id for r in last_rows if _is_fx_price(r)})
    fx_quotes: dict[str, list[ValueRow]] = {}
    for r in _ny_close_quotes(duck, report_time, fx_ids, exclude_tiers):
        fx_quotes.setdefault(r.instrument_id, []).append(r)
    nyse_prev = prev_business_day("XNYS", report_date)
    session = is_business_day("XNYS", report_date)
    since = previous_brief_time(report_date, report_time)
    # what that brief showed, point-in-time on ingested_at (a late print is new once)
    shown = {
        r.instrument_id: r for r in _snapshot(duck, since, nyse_prev, exclude_tiers=exclude_tiers)
    }
    out: list[ChangeRow] = []
    for last in last_rows:
        history = by_inst.get(last.instrument_id, [])
        home = HOME_EXCHANGES.get(last.instrument_id)
        if _is_fx_price(last):
            ch = _fx_change(last, fx_quotes.get(last.instrument_id, []))
        elif family_of(last) == "price" and last.interval != "1w":
            if home is not None:
                ch = _home_session_change(last, history, home)
            elif session:
                ch = _price_change(last, history, nyse_prev)
            else:
                ch = _off_session_price_change(last, history)
        else:
            ch = _official_change(last, history)
        out.append(_ranked(ch, shown.get(last.instrument_id), since))
    return tuple(out)


def previous_brief_time(report_date: date, report_time: datetime) -> datetime:
    """The previous scheduled brief (aware UTC): the previous XNYS session at this report's ET
    clock time. "New since the previous brief" (P-024) compares with what that brief showed."""
    return et_to_utc(prev_business_day("XNYS", report_date), f"{to_et(report_time):%H:%M}")


def like_for_like(value: ValueRow, baseline: ValueRow) -> bool:
    """True when ``baseline`` measures the same thing as ``value`` (P-024).

    Same source, dataset and field, or a pair listed in :data:`LIKE_FOR_LIKE`.
    """
    v = (value.source, value.dataset, value.field)
    b = (baseline.source, baseline.dataset, baseline.field)
    return v == b or (v, b) in LIKE_FOR_LIKE


def _session_day(row: ValueRow) -> date:
    """The trading day a value belongs to: a dated row's own date; an intraday row's date in its
    home exchange's zone for a foreign index (Tokyo's Tuesday quote at 01:45 ET), else in ET."""
    home = HOME_EXCHANGES.get(row.instrument_id)
    if row.interval in OFFICIAL_INTERVALS or home is None:
        return _et(row)
    return ensure_utc(row.as_of).astimezone(ZoneInfo(home.tz)).date()


def _not_new(last: ValueRow, shown: ValueRow | None, since: datetime) -> str | None:
    """Why ``last`` is not new since the previous brief, which showed ``shown``; None if new.

    P-024 and the planner's ruling on T61. ``shown`` is that brief's own value, point-in-time on
    ``ingested_at``: a value first stored after the brief is new even when dated earlier (FRED
    publishes a day late), and a value the brief already had is not. The value must belong to a
    later trading day than ``shown``, or (a US market) the same day at a later instant. A
    foreign index's session is scored once: a later quote of the session the previous brief
    already showed, against the same previous-session close, is not scored again.
    """
    if shown is None:
        return None
    brief = to_et(since)
    when = f"{brief.day} {brief:%b} {brief:%H:%M} ET"
    day, shown_day = _session_day(last), _session_day(shown)
    home = HOME_EXCHANGES.get(last.instrument_id)
    if day == shown_day and home is not None:
        exchange = home.basis.removeprefix("previous ").removesuffix(" close")
        return f"{exchange} session of {day.day} {day:%b} already in the previous brief ({when})"
    if day > shown_day or (
        day == shown_day
        and last.interval not in OFFICIAL_INTERVALS
        and ensure_utc(last.as_of) > ensure_utc(shown.as_of)
    ):
        return None
    dated = to_et(last.as_of)
    text = f"{dated.day} {dated:%b}"
    if last.interval not in OFFICIAL_INTERVALS:
        text += f" {dated:%H:%M} ET"
    return f"value dated {text}, not new since the previous brief ({when})"


def _ranked(ch: ChangeRow, shown: ValueRow | None, since: datetime) -> ChangeRow:
    if ch.status != "ok" or ch.previous is None or ch.unranked_reason is not None:
        return ch  # a builder already said why it may not rank (a move spanning two periods)
    if not like_for_like(ch.last, ch.previous):
        prev = ch.previous
        return replace(
            ch,
            unranked_reason=f"baseline is another series ({prev.source} {prev.dataset} "
            f"{prev.field}), not {ch.last.source} {ch.last.dataset} {ch.last.field}",
        )
    reason = _not_new(ch.last, shown, since) or _not_after_baseline(ch.last, ch.previous)
    if reason is not None:
        return replace(ch, unranked_reason=reason)
    return replace(ch, rankable=True)


def _not_after_baseline(last: ValueRow, prev: ValueRow) -> str | None:
    """Why ``last`` is not later than its baseline close; None when it is (T61 review).

    A stale quote must never be ranked against a later close. A dated baseline (a daily close,
    a weekly or monthly value) stands for its whole day, whose close time differs by market, so
    the value must belong to a later trading day (:func:`_session_day`); an intraday baseline (an
    FX pair's 17:00 ET quote) must be strictly earlier than the value.
    """
    if prev.interval in OFFICIAL_INTERVALS:
        day, base = _session_day(last), _et(prev)
        if day > base:
            return None
        dated = to_et(last.as_of)
        when = f"{dated.day} {dated:%b}"
        if last.interval not in OFFICIAL_INTERVALS:
            when += f" {dated:%H:%M} ET"
        return f"value dated {when}, not after its baseline close ({base.day} {base:%b})"
    if ensure_utc(last.as_of) > ensure_utc(prev.as_of):
        return None
    return "value not later than its baseline quote"


def _pick_close(last: ValueRow, closes: list[ValueRow]) -> tuple[ValueRow | None, str | None]:
    """The baseline among ``closes`` (best first) and its note.

    A like-for-like close wins. Failing that, a future gets none (never a spot series from
    another source); anything else falls back to the best close, with a note.
    """
    for r in closes:
        if like_for_like(last, r):
            return r, None
    if not closes or last.kind == "future":
        return None, None
    prev = closes[0]
    return prev, f"baseline from {prev.source} {prev.dataset}, not {last.source} {last.dataset}"


def _close_change(
    last: ValueRow, closes: list[ValueRow], basis: str, day: date | None = None
) -> ChangeRow:
    prev, note = _pick_close(last, closes)
    ch = _change(last, prev, basis, "ok" if prev else "no_baseline", note)
    if prev is None and last.kind == "future":
        # T61 review: say why a future has no change; it never falls back to another series
        on = f" for {day:%a} {day.day} {day:%b}" if day is not None else ""
        ch = replace(
            ch,
            note=f"no {source_name(last.source)} daily close stored{on}; a future is never "
            "compared with another series",
        )
    return ch


def _daily_closes(history: list[ValueRow], on: date) -> list[ValueRow]:
    return [r for r in history if r.field == "close" and r.interval == "1d" and _et(r) == on]


def _price_change(last: ValueRow, history: list[ValueRow], nyse_prev: date) -> ChangeRow:
    if last.interval == "1d" and _et(last) >= nyse_prev:
        return _change(last, None, BASIS_NYSE_CLOSE, "no_move_yet")
    return _close_change(last, _daily_closes(history, nyse_prev), BASIS_NYSE_CLOSE, nyse_prev)


def _off_session_price_change(last: ValueRow, history: list[ValueRow]) -> ChangeRow:
    """Price change on a day that is not an XNYS session (module docstring, T60 bug 4)."""
    # S <= L: the session before S. S > L (a holiday trade): L itself, which is also the
    # session before S, since no session lies between L and the report date.
    base = prev_business_day("XNYS", _et(last))
    return _close_change(last, _daily_closes(history, base), session_close_basis(base), base)


def _home_session_change(last: ValueRow, history: list[ValueRow], home: HomeExchange) -> ChangeRow:
    """A foreign index against its own exchange's previous session (P-024, T61).

    The session is the one before the last value's exchange-local date: a dated row's own date
    (Yahoo dates a daily bar by the exchange-local day), a quote's date in the exchange's zone.
    """
    if last.interval in DATED_INTERVALS:
        local = _et(last)
    else:
        local = ensure_utc(last.as_of).astimezone(ZoneInfo(home.tz)).date()
    base = prev_business_day(home.calendar, local)
    return _close_change(last, _daily_closes(history, base), home.basis, base)


def _is_fx_price(row: ValueRow) -> bool:
    return row.kind == "fx" and family_of(row) == "price"


def _ny_close_quotes(
    duck: duckdb.DuckDBPyConnection,
    at: datetime,
    instrument_ids: Sequence[str],
    exclude_tiers: Collection[int] = (),
) -> list[ValueRow]:
    """Intraday rows of ``instrument_ids`` stamped within ``FX_CLOSE_WINDOW`` of 17:00 ET."""
    if not instrument_ids:
        return []
    at = ensure_utc(at)
    tier_sql, tier_params = _tier_filter(exclude_tiers)
    ids = ", ".join("?" for _ in instrument_ids)
    hours = FX_CLOSE_WINDOW.total_seconds() / 3600
    close_h = int(FX_CLOSE_HHMM[:2])
    local = "timezone('America/New_York', o.as_of)"
    sql = (
        f"{_SELECT} WHERE o.as_of <= ? AND o.as_of > ? AND o.instrument_id IN ({ids}) "
        f"AND o.field IN ({_VALUE_FIELDS_SQL}) AND o.interval IN ('tick', '1m') "
        f"AND abs(hour({local}) + minute({local}) / 60.0 - {close_h}) <= {hours}{tier_sql} "
        f"ORDER BY o.instrument_id, o.as_of DESC, {_ORDER}"
    )
    since = at - timedelta(days=FX_CLOSE_LOOKBACK + 4)  # weekends on top of the weekdays
    return _rows(duck, sql, [at, at, since, *instrument_ids, *tier_params])


def _ny_closes_before(t: datetime) -> list[datetime]:
    """The ``FX_CLOSE_LOOKBACK`` latest weekday 17:00 ET instants strictly before ``t``."""
    out: list[datetime] = []
    d = et_date(t)
    while len(out) < FX_CLOSE_LOOKBACK:
        close = et_to_utc(d, FX_CLOSE_HHMM)
        if d.weekday() < 5 and close < ensure_utc(t):
            out.append(close)
        d -= timedelta(days=1)
    return out


def _fx_change(last: ValueRow, quotes: list[ValueRow]) -> ChangeRow:
    """An FX pair against its own stored quote at the previous New York close (P-024, T61)."""
    series = [
        q
        for q in quotes
        if (q.source, q.dataset, q.field) == (last.source, last.dataset, last.field)
        and ensure_utc(q.as_of) < ensure_utc(last.as_of)
    ]
    closes = _ny_closes_before(last.as_of)
    for close in closes:
        near = [q for q in series if abs(ensure_utc(q.as_of) - close) <= FX_CLOSE_WINDOW]
        if near:
            prev = min(near, key=lambda q: (abs(ensure_utc(q.as_of) - close), -q.as_of.timestamp()))
            basis = f"{BASIS_NY_CLOSE}, {to_et(prev.as_of):%H:%M} ET quote"
            ch = _change(last, prev, basis, "ok")
            if close == closes[0] or ch.status != "ok":
                return ch
            # walked back past a missing close: a move over more than one FX day, never ranked
            want = to_et(closes[0])
            return replace(
                ch,
                note=f"no {last.source} quote stored near the {want:%a} {want.day} {want:%b} "
                "NY close",
                unranked_reason="spans more than one FX day",
            )
    return _change(last, None, BASIS_NY_CLOSE, "no_baseline")


def _official_change(last: ValueRow, history: list[ValueRow]) -> ChangeRow:
    if last.interval not in OFFICIAL_INTERVALS:
        # a quoted yield (Yahoo ^IRX) against its own previous close when one is stored: the
        # official par yield measures something else (P-024, T61)
        own = [r for r in history if like_for_like(last, r) and _et(r) < _et(last)]
        if own:
            prev = own[0]
            ch = _change(last, prev, f"previous {source_name(prev.source)} close", "ok")
            session = prev_business_day("SIFMA_US", _et(last))
            if ch.status != "ok" or _et(prev) >= session:
                return ch
            # a missing close in between: a move over more than one session, never ranked
            return replace(
                ch,
                note=f"baseline close dated {_et(prev).day} {_et(prev):%b}, not the previous "
                f"session ({session.day} {session:%b})",
                unranked_reason="spans more than one session",
            )
    earlier = [r for r in history if r.as_of < last.as_of]
    if last.interval in OFFICIAL_INTERVALS:
        same = [
            r
            for r in earlier
            if (r.source, r.dataset, r.field) == (last.source, last.dataset, last.field)
        ]
        # a weekly value is only ever compared with the previous week of the same series
        earlier = same if last.interval == "1w" else same or earlier
    previous = earlier[0] if earlier else None  # history is newest first, best tier first
    note = None
    if previous is not None and last.interval not in OFFICIAL_INTERVALS:
        note = f"{last.source} {last.interval} value against an official {previous.source} baseline"
    elif previous is not None and previous.source != last.source:
        note = f"baseline from {previous.source}, not {last.source}"
    basis = BASIS_WEEKLY if last.interval == "1w" else BASIS_OFFICIAL
    ch = _change(last, previous, basis, "ok" if previous else "no_baseline", note)
    cal = _OFFICIAL_CALENDARS.get((last.source, last.dataset))
    if ch.status != "ok" or previous is None or last.interval != "1d" or cal is None:
        return ch
    expected = prev_business_day(cal, _et(last))
    if _et(previous) >= expected:
        return ch
    # T61 review: a missing day in an official series is a two-day move, never ranked as one
    gap = (
        f"baseline dated {_et(previous).day} {_et(previous):%b}, not the previous session "
        f"({expected.day} {expected:%b})"
    )
    return replace(
        ch,
        note=f"{ch.note}; {gap}" if ch.note else gap,
        unranked_reason="spans more than one session",
    )


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


#: one daily series of one instrument: (instrument_id, source, dataset, field)
SeriesKey = tuple[str, str, str, str]


def series_key(row: ValueRow) -> SeriesKey:
    return (row.instrument_id, row.source, row.dataset, row.field)


def series_by_feed(
    duck: duckdb.DuckDBPyConnection, days: int, at: datetime
) -> dict[SeriesKey, tuple[ValueRow, ...]]:
    """Daily (``1d``) values of the last ``days`` calendar days, oldest first, per
    (instrument, source, dataset, field): one value per series and ET date (the latest
    ingested), point-in-time at ``at``, the current ET date excluded.

    Unlike :func:`series_all` it never mixes sources within a series, so a move can be scored
    in sigma against the history of its own baseline series (T61 review: a quoted yield's
    discount basis is not the official par yield's).
    """
    at = ensure_utc(at)
    sql = (
        f"{_SELECT} WHERE o.as_of <= ? AND o.as_of > ? AND o.interval = '1d' "
        f"AND o.field IN ({_DAILY_FIELDS_SQL}) AND {_current_day_filter()} "
        f"QUALIFY row_number() OVER (PARTITION BY o.instrument_id, o.source, o.dataset, "
        f"o.field, {_ET_DATE} ORDER BY o.ingested_at DESC, o.fetch_id DESC) = 1 "
        "ORDER BY o.instrument_id, o.source, o.dataset, o.field, o.as_of"
    )
    out: dict[SeriesKey, list[ValueRow]] = {}
    for r in _rows(duck, sql, [at, at, at - timedelta(days=days), et_date(at)]):
        out.setdefault(series_key(r), []).append(r)
    return {k: tuple(v) for k, v in out.items()}


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


def lineages(
    duck: duckdb.DuckDBPyConnection, rows: Sequence[ValueRow], role: str = "value"
) -> tuple[Lineage, ...]:
    """Provenance of ``rows`` (same order), joined to ``source_log`` by ``fetch_id``.

    ``role`` tags every result: ``value`` for a reported value, ``baseline`` for the value a
    change was measured against.
    """
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
    return tuple(replace(Lineage(r, *logs.get(r.fetch_id, empty)), role=role) for r in rows)


def lineage(duck: duckdb.DuckDBPyConnection, instrument_id: str, at: datetime) -> Lineage | None:
    """Provenance of the latest value of ``instrument_id`` at ``at`` (None if no value)."""
    rows = _snapshot(duck, at, et_date(at), instrument_id)
    return lineages(duck, rows)[0] if rows else None


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

_STATUS_RANK = {"fail": 0, "warn": 1}
_FLAG_RANK = {"pass": 0, "warn": 1, "fail": 2}
_FETCH_OK = ("ok", "empty")


def health_summary(
    duck: duckdb.DuckDBPyConnection,
    target_date: date,
    *,
    expected_sources: Collection[str] | None = None,
    as_of: datetime | None = None,
) -> HealthSummary:
    """Quality-check and source status for ``target_date`` (empty tables give zero counts).

    Only the latest run for ``target_date`` is read, one run id for both tables (see the module
    docstring); within it a re-evaluated check counts once (the latest evaluation). Each
    cross-check is mirrored into ``dq_results`` as a ``consistency`` row (spec §7.2); those rows
    are left out of the DQ counts so a cross-check counts once, from ``crosscheck_results``.
    ``expected_sources`` defaults to every source with a feed in the config; a source with no
    fetch that day is ``missing``.

    A partial re-run (one that evaluated only some checks) is still the latest run, so it
    supersedes both check tables for ``target_date``: checks the earlier, fuller run recorded and
    the re-run did not repeat are not counted, in the DQ and the cross-check counts alike.

    ``as_of`` bounds every read: check rows evaluated after it, ``runs`` rows started after it
    and fetches requested after it are ignored. ``None`` reads everything.
    """
    dq = _latest_dq(duck, target_date, as_of)
    xc = _latest_xchecks(duck, target_date, as_of)
    issues: list[CheckItem] = [
        CheckItem("dq", n, st, ct, src, inst, None, obs, thr)
        for n, ct, src, inst, st, obs, thr in dq
        if st in _STATUS_RANK
    ]
    issues += [
        CheckItem(
            "crosscheck",
            n,
            st,
            None,
            f"{ls}/{rs}",
            li,
            grp,
            diff,
            warn if st == "warn" else fail,
            _ref_date(details),
        )
        for n, grp, ls, rs, st, diff, warn, fail, li, _ri, details in xc
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

    run_sql, run_params = _bound("started_at", as_of)
    run = duck.execute(
        f"SELECT run_id, status FROM runs WHERE target_date = ?{run_sql} "  # noqa: S608
        "ORDER BY started_at DESC, run_id DESC LIMIT 1",
        [target_date, *run_params],
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
        sources=_source_health(duck, target_date, expected_sources, as_of),
    )


#: Cross-check rows with ``eval_at``, when the row was evaluated. ``at_time`` is the market time
#: the check refers to (a fix, a close, midnight of the ref date), not when it ran, and
#: ``crosscheck_results`` has no evaluation timestamp; so a row counts from the start of the run
#: that wrote it (``runs.started_at``). A row whose run has no ``runs`` row falls back to
#: ``at_time``.
_XC_EVAL = (
    "(SELECT x.*, coalesce(r.started_at, x.at_time) AS eval_at FROM xchecks x LEFT JOIN "
    "(SELECT run_id, max(started_at) AS started_at FROM runs GROUP BY run_id) r "
    "ON r.run_id = x.run_id)"
)


def _bound(column: str, as_of: datetime | None) -> tuple[str, list[Any]]:
    """`` AND <column> <= ?`` and its bound value (empty when ``as_of`` is None)."""
    if as_of is None:
        return "", []
    return f" AND {column} <= ?", [ensure_utc(as_of)]


def latest_check_run(duck: duckdb.DuckDBPyConnection, now: datetime) -> RunRef | None:
    """The run whose checks a status line should show at ``now`` (None if there is none).

    The run with the greatest ``target_date`` on or before ``et_date(now)`` that has check rows
    (``dq_results`` or ``crosscheck_results``, skipped checks included) evaluated at or before
    ``now`` (a cross-check counts from its run's ``started_at``; see ``_XC_EVAL``). Among that
    date's rows the newest run wins, as in ``health_summary``. A backfill run started later for
    an earlier date is therefore not chosen. ``started_at`` comes from the ``runs`` table (None
    when the run has no row there).
    """
    now = ensure_utc(now)
    today = et_date(now)
    row = duck.execute(
        "SELECT target_date, run_id FROM ("  # noqa: S608
        "SELECT target_date, run_id, evaluated_at AS ts, result_id FROM dq "
        "WHERE target_date <= ? AND evaluated_at <= ? "
        f"UNION ALL SELECT target_date, run_id, eval_at, result_id FROM {_XC_EVAL} "
        "WHERE target_date <= ? AND eval_at <= ?"
        ") ORDER BY target_date DESC, ts DESC, result_id DESC LIMIT 1",
        [today, now, today, now],
    ).fetchone()
    if row is None:
        return None
    started = duck.execute("SELECT max(started_at) FROM runs WHERE run_id = ?", [row[1]]).fetchone()
    return RunRef(row[0], row[1], started[0] if started else None)


def _latest_run(
    duck: duckdb.DuckDBPyConnection, target_date: date, as_of: datetime | None = None
) -> str | None:
    """The newest run that wrote any check row (``dq_results`` or ``crosscheck_results``).

    One run id serves both tables, so the DQ counts, the cross-check items and the instrument
    flags always describe the same run. A newer run that wrote only one of the two tables still
    wins: the other table then contributes nothing for that date. Rows evaluated after
    ``as_of`` (when given) are ignored; a cross-check counts from its run's ``started_at``
    (see ``_XC_EVAL``).
    """
    dq_sql, dq_params = _bound("evaluated_at", as_of)
    xc_sql, xc_params = _bound("eval_at", as_of)
    row = duck.execute(
        "SELECT run_id FROM ("  # noqa: S608
        f"SELECT run_id, evaluated_at AS ts, result_id FROM dq WHERE target_date = ?{dq_sql} "
        f"UNION ALL SELECT run_id, eval_at, result_id FROM {_XC_EVAL} "
        f"WHERE target_date = ?{xc_sql}"
        ") ORDER BY ts DESC, result_id DESC LIMIT 1",
        [target_date, *dq_params, target_date, *xc_params],
    ).fetchone()
    return row[0] if row else None


def _latest_dq(
    duck: duckdb.DuckDBPyConnection, target_date: date, as_of: datetime | None = None
) -> list[tuple[Any, ...]]:
    """Single-source results of the latest run for ``target_date`` (mirrored rows left out)."""
    bound_sql, bound_params = _bound("evaluated_at", as_of)
    return duck.execute(
        "SELECT check_name, check_type, source, instrument_id, status, observed, threshold "  # noqa: S608
        f"FROM dq WHERE target_date = ? AND check_type <> 'consistency' AND run_id = ?{bound_sql} "
        "QUALIFY row_number() OVER (PARTITION BY check_name, "
        "coalesce(source, ''), coalesce(instrument_id, '') ORDER BY evaluated_at DESC, "
        "result_id DESC) = 1",
        [target_date, _latest_run(duck, target_date, as_of), *bound_params],
    ).fetchall()


def _latest_xchecks(
    duck: duckdb.DuckDBPyConnection, target_date: date, as_of: datetime | None = None
) -> list[tuple[Any, ...]]:
    """Cross-check results of the latest run for ``target_date``."""
    bound_sql, bound_params = _bound("eval_at", as_of)
    return duck.execute(
        "SELECT check_name, group_id, left_source, right_source, status, diff_bps, "  # noqa: S608
        "warn_bps, fail_bps, left_instrument, right_instrument, details_json "
        f"FROM {_XC_EVAL} WHERE target_date = ? AND run_id = ?{bound_sql} "
        "QUALIFY row_number() OVER (PARTITION BY check_name, coalesce(group_id, ''), "
        "left_instrument, right_instrument, left_source, right_source "
        "ORDER BY at_time DESC, result_id DESC) = 1",
        [target_date, _latest_run(duck, target_date, as_of), *bound_params],
    ).fetchall()


def instrument_flags(
    duck: duckdb.DuckDBPyConnection, target_date: date, *, as_of: datetime | None = None
) -> dict[str, str]:
    """Worst check status per instrument for ``target_date``: ``fail`` > ``warn`` > ``pass``.

    Reads single-source results (by ``instrument_id``) and cross-checks (both instruments of
    the pair) of the latest run. ``skip`` counts as no result; an instrument with no
    pass/warn/fail result is absent from the mapping (callers show ``none``). ``as_of`` (when
    given) ignores check rows evaluated after it, as in :func:`health_summary`.
    """
    worst: dict[str, str] = {}

    def note(iid: str | None, status: str) -> None:
        if iid and _FLAG_RANK.get(status, -1) > _FLAG_RANK.get(worst.get(iid, ""), -1):
            worst[iid] = status

    for r in _latest_dq(duck, target_date, as_of):
        note(r[3], r[4])
    for r in _latest_xchecks(duck, target_date, as_of):
        note(r[8], r[4])
        note(r[9], r[4])
    return worst


def _ref_date(details_json: str | None) -> date | None:
    """``details.ref_date`` of a cross-check row (None when absent or malformed)."""
    try:
        ref = json.loads(details_json or "{}").get("ref_date")
        return date.fromisoformat(ref) if isinstance(ref, str) else None
    except (ValueError, AttributeError):
        return None


def _source_health(
    duck: duckdb.DuckDBPyConnection,
    target_date: date,
    expected: Collection[str] | None,
    as_of: datetime | None = None,
) -> tuple[SourceHealth, ...]:
    if expected is None:
        expected = [r[0] for r in duck.execute("SELECT DISTINCT source FROM dim_feed").fetchall()]
    bound_sql, bound_params = _bound("requested_at", as_of)
    rows = duck.execute(
        "SELECT source, status, count(*) FROM source_log WHERE "  # noqa: S608
        f"CAST(timezone('America/New_York', requested_at) AS DATE) = ?{bound_sql} "
        "GROUP BY source, status",
        [target_date, *bound_params],
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
        if row.source != "ecb":
            return "rate"
        return "ECB derived cross rate" if row.is_derived else "ECB reference rate"
    if row.field == "yield" or row.kind == "yield":
        return "yield"
    return _FIELD_LABELS.get(row.field, row.field)  # close | last | level


_FIELD_LABELS = {"net_position": "net speculative position", "open_interest": "open interest"}


def value_label(row: ValueRow) -> str:
    """Instrument name plus its field label, e.g. ``Euro / US dollar (ECB reference rate)``."""
    label = field_label(row)
    return row.name if label.lower() in row.name.lower() else f"{row.name} ({label})"


# ---------------------------------------------------------------------------
# Today's calendar and the Company section (P-024, card T65): ``events`` and ``filings``
# ---------------------------------------------------------------------------
#
# Neither table has a DuckDB view (they are not observations), so these helpers read them with
# ``Datasets`` (or take frames already read) and always go through ``schemas.current_rows``.
# Point in time: rows ingested after ``now`` and ``source_log`` rows that responded after it
# are ignored, so a brief written earlier reads the same rows later. "Newest" in
# ``current_rows`` means the latest ``ingested_at`` (= the fetch's ``responded_at``) per scope,
# ties to the larger ``run_id``.
#
# ``events_today(datasets, now, horizon_days)``         current events, ET today .. +horizon
# ``company_filings(datasets, instrument_id, now, insider_days)``
#                                                       latest 10-Q/10-K, insiders, 13F holders
# ``current_events`` / ``company_filings_from_frame``   the same over frames already read


@dataclass(frozen=True)
class EventItem:
    """One scheduled release or auction (``events``)."""

    event_date: date  # ET calendar date
    scheduled_at: datetime | None  # UTC instant when the time is known
    kind: str  # release | auction
    title: str
    source: str
    dataset: str
    period: str | None
    url: str | None
    details: Mapping[str, Any]


@dataclass(frozen=True)
class FilingRef:
    """A filing (row ``seq = 0``): the latest 10-Q or 10-K."""

    accession: str
    form: str
    filed_date: date
    period: date | None
    url: str


@dataclass(frozen=True)
class InsiderTrade:
    """One Form 4 non-derivative transaction."""

    accession: str
    form: str
    filed_date: date
    trade_date: date
    party: str
    party_role: str | None
    trade_code: str
    side: str  # buy | sell | other
    shares: float
    price: float | None  # USD per share
    value_usd: float | None
    plan_10b5_1: bool
    url: str


@dataclass(frozen=True)
class InsiderSummary:
    """Form 4 transactions filed in ``[since, as-of date]`` and their totals (USD).

    ``net_usd`` = ``buys_usd - sells_usd`` (positive is net buying); trades with no price are
    left out of the sums and counted in ``n_unpriced``. ``n_10b5_1`` counts trades marked as
    under a Rule 10b5-1 plan.
    """

    since: date
    trades: tuple[InsiderTrade, ...]
    n_buys: int
    n_sells: int
    buys_usd: float
    sells_usd: float
    net_usd: float
    n_10b5_1: int
    n_unpriced: int


@dataclass(frozen=True)
class HolderChange:
    """A 13F holder's position at ``period`` end and its change from the previous quarter."""

    party: str
    party_cik: str
    period: date
    filed_date: date
    shares: float
    shares_change: float
    value_usd: float | None
    accession: str
    form: str
    url: str


@dataclass(frozen=True)
class CompanyFilings:
    """Everything the Company section shows for one stock."""

    instrument_id: str
    latest_filing: FilingRef | None
    insiders: InsiderSummary
    holders_period: date | None  # the 13F as-of quarter end (filed up to 45 days after it)
    holders: tuple[HolderChange, ...]


def _known_at(df: pd.DataFrame, column: str, at: datetime) -> pd.DataFrame:
    """Rows of ``df`` whose ``column`` (tz-aware) is not after ``at``."""
    if df.empty:
        return df
    stamps = pd.to_datetime(df[column], utc=True)
    return df[stamps <= pd.Timestamp(ensure_utc(at))].reset_index(drop=True)


def _opt(value: Any) -> Any:
    """``None`` for pandas' missing markers, else the value."""
    if value is None:
        return None
    try:
        return None if bool(pd.isna(value)) else value
    except (TypeError, ValueError):
        return value


def current_events(
    events: pd.DataFrame,
    source_log: pd.DataFrame | None,
    now: datetime,
    horizon_days: int,
) -> list[EventItem]:
    """Current events dated ET ``today .. today + horizon_days``, by date then time then title.

    ``events`` and ``source_log`` are the stored tables (any partitions); rows known after
    ``now`` are ignored, then ``current_rows("events", ...)`` picks each scope's newest run.
    """
    known = _known_at(events, "ingested_at", now)
    log = _known_at(source_log, "responded_at", now) if source_log is not None else None
    cur = current_rows("events", known, log)
    today = et_date(now)
    last = today + timedelta(days=horizon_days)
    out: list[EventItem] = []
    for r in cur.to_dict("records"):
        day = r["event_date"]
        if not today <= day <= last:
            continue
        at = _opt(r["scheduled_at"])
        out.append(
            EventItem(
                event_date=day,
                scheduled_at=ensure_utc(at.to_pydatetime()) if at is not None else None,
                kind=str(r["kind"]),
                title=str(r["title"]),
                source=str(r["source"]),
                dataset=str(r["dataset"]),
                period=_opt(r["period"]),
                url=_opt(r["url"]),
                details=json.loads(r["details_json"]) if r["details_json"] else {},
            )
        )
    far = datetime.max.replace(tzinfo=ensure_utc(now).tzinfo)
    out.sort(key=lambda e: (e.event_date, e.scheduled_at or far, e.title, e.source))
    return out


def events_today(
    datasets: Datasets,
    now: datetime,
    horizon_days: int,
    *,
    source_log: pd.DataFrame | None = None,
) -> list[EventItem]:
    """:func:`current_events` over the stored ``events`` and ``source_log`` tables.

    Pass ``source_log`` when it is already read (the dashboard caches it) to skip that read.
    """
    log = source_log if source_log is not None else datasets.read("source_log")
    return current_events(datasets.read("events"), log, now, horizon_days)


def latest_amendments_only(filings: pd.DataFrame) -> pd.DataFrame:
    """Keep, for each original, only its latest amendment (P-024 review, round 3).

    ``current_rows`` drops a filing that a current amendment names in ``amends_accession``,
    but two amendments of one original would both survive and double-count. Among the filing
    rows (``seq = 0``) naming the same ``(instrument_id, amends_accession)`` the one with the
    latest ``filed_date`` (ties: the larger accession) is kept, with all its rows; the other
    amendments' rows are dropped.
    """
    if filings.empty:
        return filings
    heads = filings[(filings["seq"] == 0) & filings["amends_accession"].notna()]
    drop: set[tuple[str, str]] = set()
    for _, grp in heads.groupby(["instrument_id", "amends_accession"], sort=False):
        ranked = sorted(
            zip(grp["filed_date"], grp["accession"], grp["instrument_id"], strict=True),
            reverse=True,
        )
        drop.update((inst, acc) for _, acc, inst in ranked[1:])
    if not drop:
        return filings
    keep = [
        (i, a) not in drop
        for i, a in zip(filings["instrument_id"], filings["accession"], strict=True)
    ]
    return filings[keep].reset_index(drop=True)


def _insiders(rows: pd.DataFrame, since: date, until: date) -> InsiderSummary:
    trades: list[InsiderTrade] = []
    sel = rows[(rows["row_kind"] == "insider_trade")]
    for r in sel.to_dict("records"):
        if not since <= r["filed_date"] <= until:
            continue
        trades.append(
            InsiderTrade(
                accession=str(r["accession"]),
                form=str(r["form"]),
                filed_date=r["filed_date"],
                trade_date=r["trade_date"],
                party=str(r["party"]),
                party_role=_opt(r["party_role"]),
                trade_code=str(r["trade_code"]),
                side=str(r["side"]),
                shares=float(r["shares"]),
                price=_opt(r["price"]),
                value_usd=_opt(r["value_usd"]),
                plan_10b5_1=bool(r["plan_10b5_1"]),
                url=str(r["url"]),
            )
        )
    trades.sort(key=lambda t: (t.trade_date, t.filed_date, t.accession), reverse=True)
    buys = [t for t in trades if t.side == "buy"]
    sells = [t for t in trades if t.side == "sell"]
    buys_usd = sum(t.value_usd for t in buys if t.value_usd is not None)
    sells_usd = sum(t.value_usd for t in sells if t.value_usd is not None)
    return InsiderSummary(
        since=since,
        trades=tuple(trades),
        n_buys=len(buys),
        n_sells=len(sells),
        buys_usd=float(buys_usd),
        sells_usd=float(sells_usd),
        net_usd=float(buys_usd - sells_usd),
        n_10b5_1=sum(t.plan_10b5_1 for t in trades),
        n_unpriced=sum(t.value_usd is None for t in buys + sells),
    )


def _holders(rows: pd.DataFrame) -> tuple[date | None, tuple[HolderChange, ...]]:
    sel = rows[rows["row_kind"] == "holder_change"]
    if sel.empty:
        return None, ()
    period = max(sel["period"])
    best: dict[str, HolderChange] = {}
    for r in sel[sel["period"] == period].to_dict("records"):
        change = HolderChange(
            party=str(r["party"]),
            party_cik=str(r["party_cik"]),
            period=r["period"],
            filed_date=r["filed_date"],
            shares=float(r["shares"]),
            shares_change=float(r["shares_change"]),
            value_usd=_opt(r["value_usd"]),
            accession=str(r["accession"]),
            form=str(r["form"]),
            url=str(r["url"]),
        )
        prior = best.get(change.party_cik)
        if prior is None or (change.filed_date, change.accession) > (
            prior.filed_date,
            prior.accession,
        ):
            best[change.party_cik] = change
    ordered = sorted(best.values(), key=lambda h: (-abs(h.shares_change), h.party))
    return period, tuple(ordered)


def company_filings_from_frame(
    filings: pd.DataFrame, instrument_id: str, now: datetime, insider_days: int
) -> CompanyFilings:
    """The Company section for one stock from the stored ``filings`` table.

    Rows known after ``now`` are ignored; then ``current_rows`` and
    :func:`latest_amendments_only`. Latest filing: the 10-Q or 10-K with the latest
    ``filed_date`` (ties: the larger accession). Insiders: Form 4 rows filed in the last
    ``insider_days`` up to ET today. Holders: the 13F ``holder_change`` rows of the latest
    ``period``, one per holder (the latest filed), largest change first.
    """
    known = _known_at(filings, "ingested_at", now)
    cur = latest_amendments_only(current_rows("filings", known))
    rows = cur[cur["instrument_id"] == instrument_id] if not cur.empty else cur
    today = et_date(now)
    since = today - timedelta(days=insider_days)
    latest: FilingRef | None = None
    if not rows.empty:
        heads = rows[(rows["seq"] == 0) & rows["form"].isin(["10-K", "10-Q"])]
        if not heads.empty:
            top = max(
                heads.to_dict("records"), key=lambda r: (r["filed_date"], str(r["accession"]))
            )
            latest = FilingRef(
                accession=str(top["accession"]),
                form=str(top["form"]),
                filed_date=top["filed_date"],
                period=_opt(top["period"]),
                url=str(top["url"]),
            )
    if rows.empty:
        insiders = InsiderSummary(since, (), 0, 0, 0.0, 0.0, 0.0, 0, 0)
        return CompanyFilings(instrument_id, latest, insiders, None, ())
    period, holders = _holders(rows)
    return CompanyFilings(instrument_id, latest, _insiders(rows, since, today), period, holders)


def company_filings(
    datasets: Datasets, instrument_id: str, now: datetime, insider_days: int
) -> CompanyFilings:
    """:func:`company_filings_from_frame` over the stored ``filings`` table."""
    return company_filings_from_frame(datasets.read("filings"), instrument_id, now, insider_days)
