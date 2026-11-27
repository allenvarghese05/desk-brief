"""Dashboard data access: the one place the dashboard touches the store (spec 6, 10.1).

ADAPTER NOTE (T34): the serving files of spec 6 (``serving/latest_snapshot.parquet``,
``serving/health.json``, ...) are produced by T23, which is not merged yet. Until then the
Overview is built from ``deskbrief.query`` and ``BriefModel`` through one function,
:func:`build_snapshot`. When T23 lands, switch only that function to read the serving files;
:class:`Overview` and everything above it stay as they are.

Read-only: the connection uses the ``dashboard-reader`` token (``R2_READER_*`` variables).
Writer keys in the environment are ignored on purpose. Nothing here writes to the store.

Display mode (spec 10.1): ``hosted`` hides tier-1 rows (licence: provider display terms are
unverified until H9); ``local`` shows tiers 1-3. Anything but ``local`` means ``hosted``.
The hidden tiers are passed to ``BriefModel.from_store(exclude_tiers=...)``, so an instrument
whose latest value is a hidden tier falls back to tier 2 or 3, and they are part of the cache
key, so hosted and local snapshots never share an entry. :func:`overview` filters again on the
way out as a second guard.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

import duckdb
import pandas as pd
import streamlit as st
from app.components.values import TIER_NAME

from deskbrief.config import Config
from deskbrief.query import (
    BRIEF_CLOCK_ET,
    CompanyFilings,
    Lineage,
    company_filings_from_frame,
    current_events,
    latest_check_run,
    series_all,
)
from deskbrief.report.model import (
    BriefModel,
    Caveat,
    Event,
    HealthBlock,
    KnownLimit,
    Mover,
    Notable,
    Note,
    Read,
    Row,
    Section,
    today_events,
)
from deskbrief.report.note import note_of, stored_note
from deskbrief.store import duck
from deskbrief.store.backend import REPORTS_PREFIX, StorageError
from deskbrief.store.datasets import current_clean_version
from deskbrief.store.local import LocalBackend
from deskbrief.store.r2 import R2Backend
from deskbrief.timeutil import ensure_utc, et_date, et_to_utc, to_et, utc_now

_log = logging.getLogger(__name__)

HOSTED, LOCAL = "hosted", "local"
#: tiers never shown when hosted, whatever config says, until H9 records the provider terms
ALWAYS_HIDDEN_HOSTED = frozenset({1})
READER_ENV = ("R2_ACCOUNT_ID", "R2_READER_ACCESS_KEY_ID", "R2_READER_SECRET_ACCESS_KEY")
SPARK_DAYS = 30
#: Treasury tenors drawn on the yield curve, shortest first (official values only)
CURVE_TENORS = ("UST_3M", "UST_2Y", "UST_10Y", "UST_20Y", "UST_30Y")
#: TTL of the serving-file caches in ``kit`` (those files are not part of the data version)
CACHE_TTL_SECONDS = 60
#: TTL of this module's caches; they are also keyed on :func:`data_version` (T56)
DATA_CACHE_TTL_SECONDS = 15 * 60
#: the store's listing is checked at most this often; the connection is rebuilt only on change
LISTING_CHECK_SECONDS = 60
#: a background check running longer than this (a LIST or GET that never returns) is
#: abandoned and the next call starts a new one
REFRESH_TIMEOUT_SECONDS = 5 * 60
#: views the dashboard connection never builds (no LIST, no httpfs): the dashboard reads the
#: scorecard from its serving file
DASHBOARD_SKIP_VIEWS = frozenset({"scorecard"})
#: how far back the price chart reads daily bars (calendar days; the store may hold less)
BAR_DAYS = 400
BAR_COLUMNS = (
    "d", "open", "high", "low", "close", "volume", "tier", "source", "field", "unit", "interval",
)  # fmt: skip
#: a bar's main value, best first; level, net_position and open_interest are P-021's fields
_MAIN_RANK = {"close": 0, "yield": 1, "rate": 2, "level": 3, "net_position": 4, "open_interest": 5}
_BAR_FIELDS = ", ".join(f"'{f}'" for f in (*_MAIN_RANK, "open", "high", "low", "volume"))


def current_time() -> datetime:
    """Wall clock (a seam so tests can pin the instant)."""
    return utc_now()


def resolve_mode(value: str | None) -> str:
    """``local`` only when asked for explicitly; unset, empty or unknown fails closed to hosted."""
    return LOCAL if (value or "").strip().lower() == LOCAL else HOSTED


def hidden_tiers(mode: str, config: Config) -> frozenset[int]:
    if mode == LOCAL:
        return frozenset()
    return ALWAYS_HIDDEN_HOSTED | frozenset(config.dashboard.hosted_hidden_tiers)


def reader_env(env: Mapping[str, str]) -> dict[str, str]:
    """Only the read-only R2 variables. Raises ``StorageError`` naming (not showing) gaps."""
    picked = {k: env[k] for k in READER_ENV if env.get(k)}
    missing = [k for k in READER_ENV if k not in picked]
    if missing:
        raise StorageError(
            "R2 read-only credentials not configured; missing: " + ", ".join(missing)
        )
    return picked


def _store(config: Config, env: Mapping[str, str]) -> LocalBackend | R2Backend:
    if config.storage.backend == "r2":
        return R2Backend.from_env(config.storage.bucket, reader_env(env))
    return LocalBackend(config.storage.local_root)


@dataclass(frozen=True)
class Listing:
    """What a dashboard connection is built from: one LIST per cached table (T56)."""

    clean_version: str
    keys: tuple[tuple[str, tuple[str, ...]], ...]  # (table, sorted keys), in TABLE_VIEWS order

    def fingerprint(self) -> str:
        h = hashlib.sha256(self.clean_version.encode())
        for table, keys in self.keys:
            h.update(b"\0" + table.encode())
            for k in keys:
                h.update(b"\n" + k.encode())
        return h.hexdigest()[:16]


def store_listing(store: LocalBackend | R2Backend) -> Listing:
    """The listing of every table the dashboard connection caches (scorecard is skipped)."""
    version = current_clean_version(store)
    tables = [t for v, t in duck.TABLE_VIEWS.items() if v not in DASHBOARD_SKIP_VIEWS]
    return Listing(version, tuple((t, duck.table_keys(store, t, version)) for t in tables))


def _connect(
    store: LocalBackend | R2Backend, config: Config, env: Mapping[str, str], listing: Listing
) -> duckdb.DuckDBPyConnection:
    return duck.connect(
        store,
        config,
        env=reader_env(env) if isinstance(store, R2Backend) else None,
        skip_views=DASHBOARD_SKIP_VIEWS,
        listing=dict(listing.keys),
        clean_version=listing.clean_version,
    )


def open_connection(config: Config, env: Mapping[str, str]) -> duckdb.DuckDBPyConnection:
    """DuckDB views over the configured store: read-only R2, or the local backend for dev.

    A new connection the caller closes. Pages use :func:`shared_connection` instead."""
    store = _store(config, env)
    return _connect(store, config, env, store_listing(store))


@dataclass
class _Shared:
    conn: duckdb.DuckDBPyConnection
    lock: threading.Lock
    built: float
    closed: bool = False
    listing: Listing | None = None
    version: str = ""


@dataclass
class _Slot:
    """The process's connection for one store and the state of its listing check."""

    current: _Shared | None = None
    checked: float = float("-inf")  # time.monotonic() of the last finished listing check
    busy: bool = False  # the first build or a listing check is running
    error: str = ""  # type of the last failed background check (never its message)
    started: float = float("-inf")  # time.monotonic() when the running check started
    gen: int = 0  # id of the newest background check; an abandoned one changes nothing


_SLOTS: dict[tuple[str, str], _Slot] = {}
#: guards _SLOTS and every slot's fields; never held during a query, a LIST or a build
_SHARED_GUARD = threading.Condition()


def _build(
    store: LocalBackend | R2Backend, config: Config, env: Mapping[str, str], listing: Listing
) -> _Shared:
    conn = _connect(store, config, env, listing)
    return _Shared(conn, threading.Lock(), time.monotonic(), listing=listing,
                   version=listing.fingerprint())  # fmt: skip


def _retire(old: _Shared) -> None:
    """Close a replaced connection once the query running on it (if any) has finished."""
    with old.lock:
        old.closed = True
        old.conn.close()


def _refresh(config: Config, env: Mapping[str, str], slot: _Slot, gen: int) -> None:
    """One listing check; a rebuild only if the listing changed (runs in a background thread).

    The old connection keeps serving every query until the new one is built, then is swapped
    in and the old one closed. A check that ran longer than ``REFRESH_TIMEOUT_SECONDS`` has
    been superseded (``slot.gen`` moved on): it then swaps nothing and leaves the slot alone."""
    error = ""
    try:
        cur = slot.current
        store = _store(config, env)
        listing = store_listing(store)
        if cur is None or listing != cur.listing:
            fresh = _build(store, config, env, listing)
            with _SHARED_GUARD:
                current = slot.gen == gen
                if current:
                    slot.current = fresh
            if not current:
                _retire(fresh)
            elif cur is not None:
                _retire(cur)
    except Exception as exc:  # the old connection keeps serving; the next check tries again
        error = type(exc).__name__
        _log.warning("dashboard listing check failed: %s", error)
    finally:
        with _SHARED_GUARD:
            if slot.gen == gen:
                slot.busy, slot.error, slot.checked = False, error, time.monotonic()
            _SHARED_GUARD.notify_all()


def _start_check(config: Config, env: Mapping[str, str], slot: _Slot) -> None:
    """Start a background check (``_SHARED_GUARD`` held). If the thread cannot start, the slot
    is released at once and the next due call tries again."""
    slot.gen += 1
    slot.busy, slot.started = True, time.monotonic()
    try:
        threading.Thread(
            target=_refresh, args=(config, env, slot, slot.gen), name="deskbrief-listing",
            daemon=True,
        ).start()  # fmt: skip
    except Exception as exc:  # e.g. RuntimeError: can't start new thread
        slot.busy, slot.error, slot.checked = False, type(exc).__name__, time.monotonic()
        _log.warning("dashboard listing check not started: %s", slot.error)


def _shared(
    config: Config, backend: str, where: str, env: Mapping[str, str] | None = None
) -> _Shared:
    """The process's connection for (backend, where).

    Built once, synchronously, on first use (callers arriving meanwhile wait for that one
    build). After that the store's listing is checked at most once every
    ``LISTING_CHECK_SECONDS``, in a background thread, and the connection is rebuilt only when
    the listing changed. No caller ever waits for a check or a rebuild."""
    env = os.environ if env is None else env
    with _SHARED_GUARD:
        slot = _SLOTS.setdefault((backend, where), _Slot())
        while slot.current is None and slot.busy:
            _SHARED_GUARD.wait()
        cur = slot.current
        if cur is not None:
            now = time.monotonic()
            due = not slot.busy and now - slot.checked >= LISTING_CHECK_SECONDS
            hung = slot.busy and now - slot.started >= REFRESH_TIMEOUT_SECONDS
            if due or hung:
                _start_check(config, env, slot)
            return cur
        slot.busy = True
    try:
        store = _store(config, env)
        fresh = _build(store, config, env, store_listing(store))
        with _SHARED_GUARD:
            slot.current, slot.checked = fresh, time.monotonic()
        return fresh
    finally:
        with _SHARED_GUARD:
            slot.busy = False
            _SHARED_GUARD.notify_all()


def _where(config: Config) -> str:
    s = config.storage
    return s.bucket if s.backend == "r2" else s.local_root


def wait_for_refresh(config: Config, timeout: float = 60.0) -> bool:
    """Block until no listing check or build is running for ``config``'s store (tests, tools).

    Returns False on timeout."""
    with _SHARED_GUARD:
        slot = _SLOTS.get((config.storage.backend, _where(config)))
        if slot is None:
            return True
        return _SHARED_GUARD.wait_for(lambda: not slot.busy, timeout)


def data_version(config: Config) -> str:
    """Fingerprint of the listing the shared connection was built from (T56).

    It changes when, and only when, the shared connection is rebuilt over a changed listing,
    so caches keyed on it never mix two versions of the store. Calling it also lets the
    background listing check run when it is due."""
    return _shared(config, config.storage.backend, _where(config)).version


@contextmanager
def shared_connection(
    config: Config, env: Mapping[str, str] | None = None
) -> Iterator[duckdb.DuckDBPyConnection]:
    """One read-only connection per process for every dashboard query (T56: one connection).

    Building a connection copies every cached table into a fresh DuckDB, so every page and
    :func:`build_snapshot` share one, rebuilt only when the store's listing changed. The lock
    keeps two browser sessions from using it at the same time (DuckDB connections are not
    thread-safe; cursors would not see its temporary tables).
    """
    while True:  # a connection replaced between lookup and lock is skipped, never used closed
        shared = _shared(config, config.storage.backend, _where(config), env)
        with shared.lock:
            if shared.closed:
                continue
            yield shared.conn
            return


@dataclass(frozen=True)
class Snapshot:
    """Everything the Overview needs, unfiltered (tiers are filtered later, per mode)."""

    model: BriefModel
    #: (tier, value, ET date, source) of official daily values, oldest first
    series: dict[str, tuple[tuple[int, float, date, str], ...]]
    #: the latest run with checks (T60 bug 5): its target date and start time; None when no
    #: run has recorded a check, the only case the status line says "No checks yet"
    last_run: tuple[date, datetime | None] | None = None
    #: the note stored with today's archived brief (T66): the dashboard never calls the API
    note: Note | None = None
    #: the brief the page is anchored to (:func:`brief_time_of`)
    brief_time: datetime | None = None
    #: the latest brief actually archived (its report time), today's or up to
    #: ``ARCHIVE_LOOKBACK_DAYS`` back; None when none is found. The pane heading names it.
    archived: datetime | None = None


def build_snapshot(
    config: Config,
    env: Mapping[str, str],
    now: datetime,
    exclude_tiers: frozenset[int] = frozenset(),
) -> Snapshot:
    """THE adapter: read the store through ``query.py`` (T23 will swap in serving files).

    ``exclude_tiers`` (the mode's hidden tiers) reaches the latest-value read, so a hidden-tier
    latest value falls back to the best remaining tier. ``series_all`` stays unfiltered here;
    :func:`overview` applies the per-mode filter to the history.

    Health describes the latest run with checks (T60 bug 5, ``query.latest_check_run``: the
    greatest target date on or before today with check rows evaluated by ``now``), passed to
    ``BriefModel.from_store(health_date=...)``, so a weekend or a morning before the run shows
    the last run's checks rather than none. With no such run the model reads today's checks,
    still bounded by ``now`` (a ``health_date`` is always passed, so ``from_store`` bounds every
    check read): a run that starts later cannot colour the dot or the flags while the status line
    says "No checks yet".
    """
    note, archived = _read_archive(config, env, now)
    brief_time = brief_time_of(archived, now)
    with shared_connection(config, env) as conn:
        latest = latest_check_run(conn, now)
        model = BriefModel.from_store(
            conn,
            config,
            et_date(now),
            now,
            exclude_tiers=exclude_tiers,
            health_date=latest.target_date if latest is not None else et_date(now),
            brief_time=brief_time,
        )
        history = series_all(conn, SPARK_DAYS, now)
    model = replace(model, today=_today(config, env, now, model))
    return Snapshot(
        model,
        {
            k: tuple((r.tier, r.value, et_date(r.as_of), r.source) for r in v)
            for k, v in history.items()
        },
        (latest.target_date, latest.started_at) if latest is not None else None,
        note,
        brief_time,
        archived,
    )


def brief_time_of(archived: datetime | None, now: datetime) -> datetime:
    """The brief the page is anchored to (PR #102 review): today's archived brief time; before
    today's brief has run, or on a day with none, today at the clock time of the latest
    archived brief (so "new since" is since that brief's time); with no archive, today's
    scheduled fire time (``query.BRIEF_CLOCK_ET``). "New since the previous brief" and the
    ranking then match a brief at any hour, never the reader's clock."""
    if archived is None:
        return et_to_utc(et_date(now), BRIEF_CLOCK_ET)
    if et_date(archived) == et_date(now):
        return archived
    return et_to_utc(et_date(now), f"{to_et(archived):%H:%M}")


#: how many days back the dashboard looks for the latest archived brief (a long weekend plus a
#: holiday); the pane heading names that brief, never a scheduled time that did not run
ARCHIVE_LOOKBACK_DAYS = 5


def _archived_brief_time(config: Config, env: Mapping[str, str], now: datetime) -> datetime | None:
    """The report time of the newest brief archived on or before ``now`` (its ``model.json``),
    searching today and up to ``ARCHIVE_LOOKBACK_DAYS`` days back. Never raises."""
    return _read_archive(config, env, now)[1]


def _read_archive(
    config: Config, env: Mapping[str, str], now: datetime
) -> tuple[Note, datetime | None]:
    """Today's stored note and the report time of the newest brief archived on or before
    ``now`` (today or up to ``ARCHIVE_LOOKBACK_DAYS`` days back), from one read of the archive
    (PR #102 review round 3): one listing per month the window touches, and each day's newest
    ``model.json`` read at most once, today's serving both. Never raises."""
    today = et_date(now)
    note = Note(reason="no brief has been written today yet")
    try:
        store = _store(config, env)
        days = [today - timedelta(days=back) for back in range(ARCHIVE_LOOKBACK_DAYS + 1)]
        newest: dict[date, tuple[int, str]] = {}
        for month in dict.fromkeys(f"{d:%Y-%m}" for d in days):
            prefix = f"{REPORTS_PREFIX}/date={month}"
            for key in store.list(prefix):
                m = re.match(r"-(\d\d)/v(\d+)/model\.json$", key[len(prefix) :])
                if m is None:
                    continue
                day = date.fromisoformat(f"{month}-{m[1]}")
                if day in days and (day not in newest or int(m[2]) > newest[day][0]):
                    newest[day] = (int(m[2]), key)
    except Exception as exc:  # the type only: a message may hold a path or an endpoint
        _log.warning("dashboard archived brief unreadable: %s", type(exc).__name__)
        return Note(reason="the stored note could not be read"), None
    archived: datetime | None = None
    for day in days:
        if day not in newest:
            continue
        try:
            doc = json.loads(store.get_bytes(newest[day][1]))
        except Exception as exc:  # the type only: a message may hold a path or an endpoint
            _log.warning("dashboard archived brief unreadable: %s", type(exc).__name__)
            if day == today:
                note = Note(reason="the stored note could not be read")
            continue
        if day == today:
            note = note_of(doc) or note
        written = doc.get("report_time") if isinstance(doc, dict) else None
        if not isinstance(written, str):
            continue
        at = ensure_utc(datetime.fromisoformat(written))
        if at <= ensure_utc(now):
            archived = at
            break
    return note, archived


#: Today reads the events (and their fetch log) stored in the last this many days: every run
#: that fetches a calendar replaces its scope, so older partitions hold only replaced rows
EVENTS_READ_DAYS = 7


def _today(
    config: Config, env: Mapping[str, str], now: datetime, model: BriefModel
) -> tuple[Event, ...]:
    """Today's stored releases and auctions (T65). Never raises: the page goes on without."""
    try:
        store = _store(config, env)
        events = _recent_table(store, "events", EVENTS_READ_DAYS, now)
        log = _recent_table(store, "source_log", EVENTS_READ_DAYS, now)
        items = current_events(events, log, now, config.events.horizon_days)
    except Exception as exc:  # the type only: a message may hold a path or an endpoint
        _log.warning("dashboard events unreadable: %s", type(exc).__name__)
        return ()
    return today_events(items, model.report_date)


def _stored_note(config: Config, env: Mapping[str, str], day: date) -> Note:
    """Today's stored note, or why there is none. Never raises: the page goes on without it."""
    try:
        note = stored_note(_store(config, env), day)
    except Exception as exc:  # the type only: a message may hold a path or an endpoint
        _log.warning("dashboard stored note unreadable: %s", type(exc).__name__)
        return Note(reason="the stored note could not be read")
    return note if note is not None else Note(reason="no brief has been written today yet")


@st.cache_data(ttl=DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _cached(
    _config: Config,
    backend: str,
    where: str,
    hidden: frozenset[int],
    version: str,
    minute: datetime,
) -> Snapshot:
    # backend, where, hidden, the data version and the minute are the key; the config is not
    # hashed. The minute keeps the as-of, health and data-age text at most a minute old.
    return build_snapshot(_config, os.environ, current_time(), hidden)


def load_snapshot(config: Config, hidden: frozenset[int] = frozenset()) -> Snapshot:
    s = config.storage
    minute = current_time().replace(second=0, microsecond=0)
    return _cached(config, s.backend, _where(config), hidden, data_version(config), minute)


def build_bars(
    conn: duckdb.DuckDBPyConnection,
    now: datetime,
    instrument_id: str,
    exclude_tiers: frozenset[int],
    days: int = BAR_DAYS,
) -> pd.DataFrame:
    """Completed daily (or weekly) bars of one instrument, oldest first: one row per ET date.

    ``interval`` says which: a weekly series (``1w``, e.g. CFTC positioning) has one bar per
    reference date, and an instrument with daily bars never mixes in weekly ones.
    Each day takes its main value (close, else yield, rate, level, net position, open interest)
    from the highest visible tier,
    then open / high / low / volume from that same tier and source only, so a candle is never
    stitched together from two feeds. Hidden tiers are excluded in SQL: hosted mode never reads
    a tier-1 bar. Point-in-time at ``now``; the current ET date is left out (still forming).
    """
    tiers = [t for t in (1, 2, 3) if t not in exclude_tiers] or [0]
    marks = ", ".join(str(int(t)) for t in tiers)
    raw = conn.execute(
        "SELECT CAST(timezone('America/New_York', as_of) AS DATE) AS d, field, value, tier, "  # noqa: S608 (the interpolated tiers are ints)
        "source, unit, interval, ingested_at FROM obs_pit(?) WHERE instrument_id = ? "
        f"AND interval IN ('1d', '1w') AND field IN ({_BAR_FIELDS}) "
        f"AND as_of > ? AND tier IN ({marks}) "
        "AND CAST(timezone('America/New_York', as_of) AS DATE) < ?",
        [now, instrument_id, now - timedelta(days=days), et_date(now)],
    ).fetchdf()
    if raw.empty:
        return pd.DataFrame(columns=list(BAR_COLUMNS))
    if (raw["interval"] == "1d").any():
        raw = raw[raw["interval"] == "1d"]
    main = raw[raw["field"].isin(list(_MAIN_RANK))].copy()
    main["rank"] = main["field"].map(_MAIN_RANK)
    main = main.sort_values(
        ["d", "tier", "rank", "source", "ingested_at"], ascending=[True, False, True, True, False]
    ).drop_duplicates("d")
    out = main[["d", "tier", "source", "field", "unit", "interval", "value"]].rename(
        columns={"value": "close"}
    )
    extra = raw[raw["field"].isin(["open", "high", "low", "volume"])]
    if not extra.empty:
        extra = extra.sort_values("ingested_at").drop_duplicates(
            ["d", "tier", "source", "field"], keep="last"
        )
        wide = extra.pivot(index=["d", "tier", "source"], columns="field", values="value")
        out = out.merge(wide.reset_index(), on=["d", "tier", "source"], how="left")
    for col in ("open", "high", "low", "volume"):
        if col not in out:
            out[col] = float("nan")
    out["d"] = pd.to_datetime(out["d"])
    return out[list(BAR_COLUMNS)].sort_values("d").reset_index(drop=True)


@st.cache_data(ttl=DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_bars(
    _config: Config,
    backend: str,
    where: str,
    instrument_id: str,
    hidden: frozenset[int],
    version: str,
) -> pd.DataFrame:
    with shared_connection(_config) as conn:
        return build_bars(conn, current_time(), instrument_id, hidden)


def load_bars(config: Config, instrument_id: str, hidden: frozenset[int]) -> pd.DataFrame:
    return _cached_bars(
        config, config.storage.backend, _where(config), instrument_id, hidden,
        data_version(config),
    )  # fmt: skip


def build_series(
    conn: duckdb.DuckDBPyConnection,
    now: datetime,
    instrument_ids: tuple[str, ...],
    exclude_tiers: frozenset[int],
    days: int = BAR_DAYS,
) -> dict[str, pd.DataFrame]:
    """:func:`build_bars` for several instruments on one connection (the Macro page, T54).

    Same rules: hidden tiers excluded in SQL, point-in-time at ``now``, today left out. An
    instrument with no visible bars maps to an empty frame, never to a missing key."""
    return {iid: build_bars(conn, now, iid, exclude_tiers, days) for iid in instrument_ids}


@st.cache_data(ttl=DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_series(
    _config: Config,
    backend: str,
    where: str,
    ids: tuple[str, ...],
    hidden: frozenset[int],
    days: int,
    version: str,
) -> dict[str, pd.DataFrame]:
    with shared_connection(_config) as conn:
        return build_series(conn, current_time(), ids, hidden, days)


def load_series(
    config: Config,
    instrument_ids: tuple[str, ...],
    hidden: frozenset[int],
    days: int = BAR_DAYS,
) -> dict[str, pd.DataFrame]:
    """Daily (or weekly) bars of several instruments over ``days`` calendar days, cached per
    mode and data version; one shared connection, never a new one per click."""
    return _cached_series(
        config, config.storage.backend, _where(config), tuple(instrument_ids), hidden, days,
        data_version(config),
    )  # fmt: skip


#: the Company section reads the filings fetched in the last this many days: every morning run
#: re-fetches each current filing, so older fetches only hold rows newer ones replaced (T67)
FILINGS_READ_DAYS = 14


def _recent_table(
    store: LocalBackend | R2Backend, table: str, days: int, now: datetime
) -> pd.DataFrame:
    """Rows of a ``derived/`` table from partitions dated in the last ``days`` days (ET)."""
    import io

    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    from deskbrief.schemas import empty_frame
    from deskbrief.store.backend import table_prefix

    first = et_date(now) - timedelta(days=days)
    keys = []
    for k in store.list(table_prefix(table, current_clean_version(store))):
        m = re.search(r"/date=(\d{4}-\d{2}-\d{2})/", k)
        if k.endswith(".parquet") and m and date.fromisoformat(m[1]) >= first:
            keys.append(k)
    template = empty_frame(table)
    if not keys:
        return template
    frames = [pq.read_table(io.BytesIO(store.get_bytes(k))).to_pandas() for k in keys]
    merged = pd.concat(frames, ignore_index=True)
    return merged[list(template.columns)].astype(dict(template.dtypes))


def build_company(
    config: Config, env: Mapping[str, str], now: datetime, instrument_id: str
) -> CompanyFilings:
    """The Company section's facts for one stock (``query.company_filings_from_frame``)."""
    frame = _recent_table(_store(config, env), "filings", FILINGS_READ_DAYS, now)
    return company_filings_from_frame(frame, instrument_id, now, config.filings.insider_days)


@st.cache_data(ttl=DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_company(
    _config: Config, backend: str, where: str, instrument_id: str, hour: datetime
) -> CompanyFilings:
    # filings change once a morning: an hour's cache keeps a click from re-reading the store.
    # A failure raises, and Streamlit does not cache an exception, so the next render retries.
    return build_company(_config, os.environ, current_time(), instrument_id)


def load_company(config: Config, instrument_id: str) -> CompanyFilings | None:
    """The Company section's facts, cached for the hour; None when the store cannot be read
    (that failure is never cached: the next render reads again, PR #102 review)."""
    hour = current_time().replace(minute=0, second=0, microsecond=0)
    s = config.storage
    try:
        return _cached_company(config, s.backend, _where(config), instrument_id, hour)
    except Exception as exc:  # the type only: a message may hold a path or an endpoint
        _log.warning("dashboard filings unreadable: %s", type(exc).__name__)
        return None


#: what this dashboard does not do (spec 10.3 asks for more); shown under "Known limits"
DASHBOARD_LIMITS = (
    "Dashboard: no stale greying; the as-of text and health badge carry data age.",
    "Dashboard: no 30-day z-score column; unusual moves are a sigma tag on their watchlist row.",
    "Dashboard: the yield curve (on Macro) uses official Treasury values only (no intraday curve).",
    "Dashboard: no live polling; new data shows on a page load about a minute after it is written.",
    f"Dashboard: sparklines cover {SPARK_DAYS} days, not the spec's 1-day sparkline.",
)


@dataclass(frozen=True)
class Overview:
    """What a page may render in this mode."""

    mode: str
    mode_label: str
    as_of: datetime
    health: HealthBlock
    headline: tuple[tuple[str, Row], ...]  # (short label, row)
    sections: tuple[Section, ...]
    lineage: dict[str, Lineage]
    sparks: dict[str, tuple[float, ...]]
    known_limits: tuple[KnownLimit, ...]
    withheld: int  # rows hidden by the mode (shown as one honest note, never as values)
    moves: tuple[Notable, ...] = ()  # unusual moves, largest first (the report's own scoring)
    #: UST tenors: (ET date, yield, source) of official values, oldest first
    curve: dict[str, tuple[tuple[date, float, str], ...]] = field(default_factory=dict)
    #: P-024 (T62): the one ranking, the read and Check before using, from the model
    movers: tuple[Mover, ...] = ()
    read: Read | None = None
    caveats: tuple[Caveat, ...] = ()
    previous_brief: datetime | None = None
    note: Note | None = None  # T66: the stored note (or why there is none)
    brief_time: datetime | None = None  # the brief "new since" and the ranking follow
    archived: datetime | None = None  # the latest brief actually archived (pane heading)
    today: tuple[Event, ...] = ()  # T65: today's scheduled releases and auctions


def overview(snap: Snapshot, mode: str, hidden: frozenset[int]) -> Overview:
    model = snap.model
    keep = {r.instrument_id for r in model.rows() if r.tier not in hidden}
    sections = tuple(
        Section(s.asset_class, s.title, tuple(r for r in s.rows if r.instrument_id in keep))
        for s in model.sections
    )
    by_id = {r.instrument_id: r for s in sections for r in s.rows}
    tiers = "+".join(TIER_NAME[t] for t in (1, 2, 3) if t not in hidden)
    return Overview(
        mode=mode,
        mode_label=f"{mode} · {tiers}",
        as_of=model.report_time,
        health=model.health,
        headline=tuple(
            (i.label, by_id[i.instrument_id])
            for i in model.headline.items
            if i.instrument_id in by_id
        ),
        sections=tuple(s for s in sections if s.rows),
        lineage={
            lg.value.instrument_id: lg
            for lg in model.lineage
            if lg.role == "value" and lg.value.tier not in hidden
        },
        sparks={
            k: tuple(v for t, v, _, _ in pts if t not in hidden)
            for k, pts in snap.series.items()
            if k in by_id
        },
        moves=tuple(
            n
            for n in model.notable
            if n.kind == "move" and n.instrument_id in by_id and by_id[n.instrument_id].available
        ),
        curve={
            k: tuple((d, v, src) for t, v, d, src in snap.series.get(k, ()) if t == 3)
            for k in CURVE_TENORS
            if k in by_id
        },
        known_limits=model.known_limits
        + tuple(KnownLimit("dashboard", t) for t in DASHBOARD_LIMITS),
        withheld=len(model.rows()) - len(keep),
        # a second guard: a mover or caveat about a withheld instrument never reaches the page
        movers=tuple(m for m in model.movers if m.instrument_id in by_id and m.tier not in hidden),
        read=model.read,
        caveats=tuple(c for c in model.caveats if all(i in by_id for i in c.instrument_ids)),
        previous_brief=model.previous_brief,
        note=snap.note,
        brief_time=snap.brief_time,
        archived=snap.archived,
        today=model.today,
    )
