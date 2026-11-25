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
  quote's date when one is stored (``"previous yahoo close"``); otherwise with the previous
  official value, noted, as before.
* ``ChangeRow.rankable`` is True only for an ``ok`` change whose baseline is like-for-like and
  whose value is new since the previous scheduled brief (the previous XNYS session at the
  report's ET clock time: a dated row must be dated on or after that session, an intraday row
  must be later than that instant). ``unranked_reason`` says why not. The brief scores sigma
  only for rankable changes.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from deskbrief.calendars import is_business_day, prev_business_day
from deskbrief.config import CalendarName
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

#: FX: the New York close is 17:00 ET; a stored quote within two hours of it stands in for it
FX_CLOSE_HHMM = "17:00"
FX_CLOSE_WINDOW = timedelta(hours=2)
#: how many weekday closes back an FX baseline is searched (a US holiday stores no quote)
FX_CLOSE_LOOKBACK = 4


@dataclass(frozen=True)
class HomeExchange:
    """A foreign index's own exchange: its calendar, time zone and basis text."""

    calendar: CalendarName
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
    # the previous scheduled brief: the previous XNYS session at this report's ET clock time
    since = et_to_utc(nyse_prev, f"{to_et(report_time):%H:%M}")
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
        out.append(_ranked(ch, nyse_prev, since))
    return tuple(out)


def like_for_like(value: ValueRow, baseline: ValueRow) -> bool:
    """True when ``baseline`` measures the same thing as ``value`` (P-024).

    Same source, dataset and field, or a pair listed in :data:`LIKE_FOR_LIKE`.
    """
    v = (value.source, value.dataset, value.field)
    b = (baseline.source, baseline.dataset, baseline.field)
    return v == b or (v, b) in LIKE_FOR_LIKE


def _is_new_since(row: ValueRow, since_date: date, since: datetime) -> bool:
    """New since the previous brief: dated on or after its date, or stamped after its cut."""
    if row.interval in OFFICIAL_INTERVALS:
        return _et(row) >= since_date
    return ensure_utc(row.as_of) > ensure_utc(since)


def _ranked(ch: ChangeRow, since_date: date, since: datetime) -> ChangeRow:
    if ch.status != "ok" or ch.previous is None:
        return ch
    if not like_for_like(ch.last, ch.previous):
        prev = ch.previous
        return replace(
            ch,
            unranked_reason=f"baseline is another series ({prev.source} {prev.dataset} "
            f"{prev.field}), not {ch.last.source} {ch.last.dataset} {ch.last.field}",
        )
    if not _is_new_since(ch.last, since_date, since):
        when = to_et(ch.last.as_of)
        return replace(
            ch,
            unranked_reason=f"value dated {when.day} {when:%b}, not new since the previous "
            f"brief ({since_date.day} {since_date:%b})",
        )
    return replace(ch, rankable=True)


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


def _close_change(last: ValueRow, closes: list[ValueRow], basis: str) -> ChangeRow:
    prev, note = _pick_close(last, closes)
    return _change(last, prev, basis, "ok" if prev else "no_baseline", note)


def _daily_closes(history: list[ValueRow], on: date) -> list[ValueRow]:
    return [r for r in history if r.field == "close" and r.interval == "1d" and _et(r) == on]


def _price_change(last: ValueRow, history: list[ValueRow], nyse_prev: date) -> ChangeRow:
    if last.interval == "1d" and _et(last) >= nyse_prev:
        return _change(last, None, BASIS_NYSE_CLOSE, "no_move_yet")
    return _close_change(last, _daily_closes(history, nyse_prev), BASIS_NYSE_CLOSE)


def _off_session_price_change(last: ValueRow, history: list[ValueRow]) -> ChangeRow:
    """Price change on a day that is not an XNYS session (module docstring, T60 bug 4)."""
    # S <= L: the session before S. S > L (a holiday trade): L itself, which is also the
    # session before S, since no session lies between L and the report date.
    base = prev_business_day("XNYS", _et(last))
    return _close_change(last, _daily_closes(history, base), session_close_basis(base))


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
    return _close_change(last, _daily_closes(history, base), home.basis)


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
    for close in _ny_closes_before(last.as_of):
        near = [q for q in series if abs(ensure_utc(q.as_of) - close) <= FX_CLOSE_WINDOW]
        if near:
            prev = min(near, key=lambda q: (abs(ensure_utc(q.as_of) - close), -q.as_of.timestamp()))
            basis = f"{BASIS_NY_CLOSE}, {to_et(prev.as_of):%H:%M} ET quote"
            return _change(last, prev, basis, "ok")
    return _change(last, None, BASIS_NY_CLOSE, "no_baseline")


def _official_change(last: ValueRow, history: list[ValueRow]) -> ChangeRow:
    if last.interval not in OFFICIAL_INTERVALS:
        # a quoted yield (Yahoo ^IRX) against its own previous close when one is stored: the
        # official par yield measures something else (P-024, T61)
        own = [r for r in history if like_for_like(last, r) and _et(r) < _et(last)]
        if own:
            return _change(last, own[0], f"previous {own[0].source} close", "ok")
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
    return _change(last, previous, basis, "ok" if previous else "no_baseline", note)


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
