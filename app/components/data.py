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
Filtering happens in :func:`overview`, *after* the cache, so a cached snapshot can never leak a
hidden tier into a hosted page.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import duckdb
import streamlit as st
from app.components.values import TIER_NAME

from deskbrief.config import Config
from deskbrief.query import Lineage, series_all
from deskbrief.report.model import BriefModel, HealthBlock, KnownLimit, Row, Section
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
CACHE_TTL_SECONDS = 60


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


@dataclass(frozen=True)
class Snapshot:
    """Everything the Overview needs, unfiltered (tiers are filtered later, per mode)."""

    model: BriefModel
    series: dict[str, tuple[tuple[int, float], ...]]  # (tier, value) of official daily values


def build_snapshot(config: Config, env: Mapping[str, str], now: datetime) -> Snapshot:
    """THE adapter: read the store through ``query.py`` (T23 will swap in serving files)."""
    conn = open_connection(config, env)
    try:
        model = BriefModel.from_store(conn, config, et_date(now), now)
        history = series_all(conn, SPARK_DAYS, now)
    finally:
        conn.close()
    return Snapshot(model, {k: tuple((r.tier, r.value) for r in v) for k, v in history.items()})


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _cached(_config: Config, backend: str, where: str) -> Snapshot:
    # backend and where are the cache key; the config itself is not hashed
    return build_snapshot(_config, os.environ, current_time())


def load_snapshot(config: Config) -> Snapshot:
    s = config.storage
    return _cached(config, s.backend, s.bucket if s.backend == "r2" else s.local_root)


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
            k: tuple(v for t, v in pts if t not in hidden)
            for k, pts in snap.series.items()
            if k in by_id
        },
        known_limits=model.known_limits,
        withheld=len(model.rows()) - len(keep),
    )
