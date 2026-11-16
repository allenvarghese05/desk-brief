"""Helpers for cross-check tests (importable as ``xc_fakes``).

SYNTHETIC FIXTURE: every observation, price and timestamp below is made up for tests.
Dates: 2026-11-13 is a Friday; DST ended 2026-11-01, so ET is UTC-5 (00:00 ET = 05:00Z).
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb

from deskbrief.config import Config
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.schemas import to_frame
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_date

FRI = date(2026, 11, 13)
MON = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)  # Monday 07:00 ET
INGESTED = datetime(2026, 11, 13, 22, 0, tzinfo=UTC)
DAY_FRI = datetime(2026, 11, 13, 5, 0, tzinfo=UTC)  # 00:00 ET, interval 1d as_of
CLOSE_FRI = datetime(2026, 11, 13, 21, 0, tzinfo=UTC)  # 16:00 ET
EARLY_DAY = date(2026, 11, 27)  # day after Thanksgiving: XNYS closes 13:00 ET
EARLY_CLOSE = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
EARLY_NOW = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)  # Monday after

_RUNS = itertools.count(1)


def make_ctx(
    config: Config,
    store: LocalBackend,
    *,
    target: date = MON,
    now: datetime = NOW,
    run_id: str = "01XCHECKRUNAAAAAAAAAAAAAA",
) -> RunContext:
    return RunContext(
        run_id=run_id,
        job=JobKind.MORNING,
        trigger=Trigger.MANUAL,
        target_date=target,
        now_utc=now,
        git_sha="deadbeef",
        config_hash="cafe",
        config=config,
        store=store,
    )


def obs(**over: Any) -> dict[str, Any]:
    """One valid ``observations`` row (a made-up SPY Stooq close) with overrides."""
    row: dict[str, Any] = {
        "instrument_id": "SPY",
        "source": "stooq",
        "dataset": "daily",
        "symbol": "spy.us",
        "tier": 3,
        "field": "close",
        "interval": "1d",
        "as_of": DAY_FRI,
        "value": 100.0,
        "unit": "USD",
        "is_derived": False,
        "is_fallback": False,
        "fetch_id": f"F{next(_RUNS)}",
        "run_id": "R1",
        "ingested_at": INGESTED,
        "parser_version": "t1",
    }
    row.update(over)
    return row


def put(store: LocalBackend, config: Config, rows: list[dict[str, Any]]) -> None:
    """Append rows, one file per ET date (each call uses a fresh run id)."""
    by_date: dict[date, list[dict[str, Any]]] = {}
    for r in rows:
        by_date.setdefault(et_date(r["as_of"]), []).append(r)
    run_id = f"RUN{next(_RUNS)}"
    ds = Datasets(store)
    for d, part in by_date.items():
        ds.append(
            "observations",
            to_frame("observations", part),
            make_ctx(config, store, run_id=run_id),
            d,
        )


def duck_for(store: LocalBackend, config: Config) -> duckdb.DuckDBPyConnection:
    return connect(store, config)


# -- row builders for each source's conventions -----------------------------------------------


def treasury(inst: str, col: str, value: float, **over: Any) -> dict[str, Any]:
    return obs(
        instrument_id=inst, source="treasury", dataset="par_curve", symbol=col, field="yield",
        unit="pct", value=value, **over,
    )  # fmt: skip


def fred(inst: str, series: str, value: float, **over: Any) -> dict[str, Any]:
    return obs(
        instrument_id=inst, source="fred", dataset="series", symbol=series, field="yield",
        unit="pct", value=value, **over,
    )  # fmt: skip


def stooq_close(inst: str, value: float, **over: Any) -> dict[str, Any]:
    return obs(
        instrument_id=inst, source="stooq", dataset="daily", symbol=f"{inst.lower()}.us",
        value=value, **over,
    )  # fmt: skip


def sip_bar(inst: str, start: datetime, value: float, **over: Any) -> dict[str, Any]:
    return obs(
        instrument_id=inst, source="alpaca", dataset="sip_bars_1m", symbol=inst, tier=2,
        interval="1m", as_of=start, value=value, **over,
    )  # fmt: skip


def yahoo_tick(inst: str, symbol: str, at: datetime, value: float, **over: Any) -> dict[str, Any]:
    unit = over.pop("unit", "USD")
    return obs(
        instrument_id=inst, source="yahoo", dataset="quote", symbol=symbol, tier=2, field="last",
        interval="tick", as_of=at, value=value, unit=unit, **over,
    )  # fmt: skip


def td_bar(
    inst: str, symbol: str, start: datetime, value: float, field: str = "close", **over: Any
) -> dict[str, Any]:
    return obs(
        instrument_id=inst, source="twelvedata", dataset="fx_series_1m", symbol=symbol, tier=1,
        field=field, interval="1m", as_of=start, value=value, unit="fx_rate", **over,
    )  # fmt: skip


def minutes(base: datetime, n: int) -> datetime:
    return base + timedelta(minutes=n)
