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

import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import duckdb
import pandas as pd
import streamlit as st
from app.components.values import TIER_NAME

from deskbrief.config import Config
from deskbrief.query import Lineage, series_all
from deskbrief.report.model import BriefModel, HealthBlock, KnownLimit, Notable, Row, Section
from deskbrief.store import duck
from deskbrief.store.backend import StorageError
from deskbrief.store.local import LocalBackend
from deskbrief.store.r2 import R2Backend
from deskbrief.timeutil import et_date, utc_now

HOSTED, LOCAL = "hosted", "local"
#: tiers never shown when hosted, whatever config says, until H9 records the provider terms
ALWAYS_HIDDEN_HOSTED = frozenset({1})
READER_ENV = ("R2_ACCOUNT_ID", "R2_READER_ACCESS_KEY_ID", "R2_READER_SECRET_ACCESS_KEY")
SPARK_DAYS = 30
#: Treasury tenors drawn on the yield curve, shortest first (official values only)
CURVE_TENORS = ("UST_3M", "UST_2Y", "UST_10Y", "UST_20Y", "UST_30Y")
CACHE_TTL_SECONDS = 60
#: how far back the price chart reads daily bars (calendar days; the store may hold less)
BAR_DAYS = 400
BAR_COLUMNS = ("d", "open", "high", "low", "close", "volume", "tier", "source", "field", "unit")
_MAIN_RANK = {"close": 0, "yield": 1, "rate": 2}


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


def open_connection(config: Config, env: Mapping[str, str]) -> duckdb.DuckDBPyConnection:
    """DuckDB views over the configured store: read-only R2, or the local backend for dev."""
    if config.storage.backend == "r2":
        renv = reader_env(env)
        return duck.connect(R2Backend.from_env(config.storage.bucket, renv), config, env=renv)
    return duck.connect(LocalBackend(config.storage.local_root), config)


@st.cache_resource(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _shared(
    _config: Config, backend: str, where: str
) -> tuple[duckdb.DuckDBPyConnection, threading.Lock]:
    # backend and where are the cache key; rebuilt every 60 s so new files are seen
    return open_connection(_config, os.environ), threading.Lock()


def _where(config: Config) -> str:
    s = config.storage
    return s.bucket if s.backend == "r2" else s.local_root


@contextmanager
def shared_connection(config: Config) -> Iterator[duckdb.DuckDBPyConnection]:
    """One read-only connection per process for the workspace's small per-click queries.

    Building a connection copies every cached table into a fresh DuckDB (seconds on R2), so the
    chart, ladder, lineage and checks share one, rebuilt at most once a minute. The lock keeps
    two browser sessions from using it at the same time (DuckDB connections are not
    thread-safe; cursors would not see its temporary tables).
    """
    conn, lock = _shared(config, config.storage.backend, _where(config))
    with lock:
        yield conn


@dataclass(frozen=True)
class Snapshot:
    """Everything the Overview needs, unfiltered (tiers are filtered later, per mode)."""

    model: BriefModel
    #: (tier, value, ET date) of official daily values, oldest first
    series: dict[str, tuple[tuple[int, float, date], ...]]


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
    """
    conn = open_connection(config, env)
    try:
        model = BriefModel.from_store(conn, config, et_date(now), now, exclude_tiers=exclude_tiers)
        history = series_all(conn, SPARK_DAYS, now)
    finally:
        conn.close()
    return Snapshot(
        model,
        {k: tuple((r.tier, r.value, et_date(r.as_of)) for r in v) for k, v in history.items()},
    )


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _cached(_config: Config, backend: str, where: str, hidden: frozenset[int]) -> Snapshot:
    # backend, where and hidden are the cache key; the config itself is not hashed
    return build_snapshot(_config, os.environ, current_time(), hidden)


def load_snapshot(config: Config, hidden: frozenset[int] = frozenset()) -> Snapshot:
    s = config.storage
    return _cached(config, s.backend, s.bucket if s.backend == "r2" else s.local_root, hidden)


def build_bars(
    conn: duckdb.DuckDBPyConnection,
    now: datetime,
    instrument_id: str,
    exclude_tiers: frozenset[int],
    days: int = BAR_DAYS,
) -> pd.DataFrame:
    """Completed daily bars of one instrument, oldest first: one row per ET date.

    Each day takes its main value (close, else yield, else rate) from the highest visible tier,
    then open / high / low / volume from that same tier and source only, so a candle is never
    stitched together from two feeds. Hidden tiers are excluded in SQL: hosted mode never reads
    a tier-1 bar. Point-in-time at ``now``; the current ET date is left out (still forming).
    """
    tiers = [t for t in (1, 2, 3) if t not in exclude_tiers] or [0]
    marks = ", ".join(str(int(t)) for t in tiers)
    raw = conn.execute(
        "SELECT CAST(timezone('America/New_York', as_of) AS DATE) AS d, field, value, tier, "  # noqa: S608 (the interpolated tiers are ints)
        "source, unit, ingested_at FROM obs_pit(?) WHERE instrument_id = ? "
        "AND interval = '1d' AND field IN ('open', 'high', 'low', 'close', 'yield', 'rate', "
        f"'volume') AND as_of > ? AND tier IN ({marks}) "
        "AND CAST(timezone('America/New_York', as_of) AS DATE) < ?",
        [now, instrument_id, now - timedelta(days=days), et_date(now)],
    ).fetchdf()
    if raw.empty:
        return pd.DataFrame(columns=list(BAR_COLUMNS))
    main = raw[raw["field"].isin(list(_MAIN_RANK))].copy()
    main["rank"] = main["field"].map(_MAIN_RANK)
    main = main.sort_values(
        ["d", "tier", "rank", "source", "ingested_at"], ascending=[True, False, True, True, False]
    ).drop_duplicates("d")
    out = main[["d", "tier", "source", "field", "unit", "value"]].rename(columns={"value": "close"})
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


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _cached_bars(
    _config: Config, backend: str, where: str, instrument_id: str, hidden: frozenset[int]
) -> pd.DataFrame:
    with shared_connection(_config) as conn:
        return build_bars(conn, current_time(), instrument_id, hidden)


def load_bars(config: Config, instrument_id: str, hidden: frozenset[int]) -> pd.DataFrame:
    return _cached_bars(config, config.storage.backend, _where(config), instrument_id, hidden)


#: what this dashboard does not do (spec 10.3 asks for more); shown under "Known limits"
DASHBOARD_LIMITS = (
    "Dashboard: no stale greying; the as-of text and health badge carry data age.",
    "Dashboard: no 30-day z-score column; unusual moves are listed under Biggest moves.",
    "Dashboard: the yield curve uses official Treasury values only (no intraday curve).",
    "Dashboard: no live polling; refresh the page (data cached for 60 s).",
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
    curve: dict[str, tuple[tuple[date, float], ...]] = field(default_factory=dict)  # UST tenors


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
            k: tuple(v for t, v, _ in pts if t not in hidden)
            for k, pts in snap.series.items()
            if k in by_id
        },
        moves=tuple(
            n
            for n in model.notable
            if n.kind == "move" and n.instrument_id in by_id and by_id[n.instrument_id].available
        ),
        curve={
            k: tuple((d, v) for t, v, d in snap.series.get(k, ()) if t == 3)
            for k in CURVE_TENORS
            if k in by_id
        },
        known_limits=model.known_limits
        + tuple(KnownLimit("dashboard", t) for t in DASHBOARD_LIMITS),
        withheld=len(model.rows()) - len(keep),
    )
