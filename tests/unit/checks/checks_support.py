"""Helpers for the check tests (importable as ``checks_support``).

SYNTHETIC FIXTURE: every observation, price and timestamp is made up for tests.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime, time
from typing import Any

import duckdb

from deskbrief.config import Config
from deskbrief.models import DQResult, JobKind, RunContext, Trigger
from deskbrief.schemas import to_frame
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import ET, et_date

__all__ = [
    "ET_NOW",
    "as_of_1d",
    "ctx_at",
    "duck_for",
    "find",
    "obs",
    "write_obs",
]


def as_of_1d(d: date) -> datetime:
    """00:00 America/New_York on ``d`` as UTC (P-006 ``as_of`` for daily rows)."""
    return datetime.combine(d, time(0), tzinfo=ET).astimezone(UTC)


def ET_NOW(d: date, hhmm: str) -> datetime:  # noqa: N802 - reads like a constructor
    """UTC instant of wall-clock ``hhmm`` ET on ``d``."""
    h, m = hhmm.split(":")
    return datetime.combine(d, time(int(h), int(m)), tzinfo=ET).astimezone(UTC)


def obs(**over: Any) -> dict[str, Any]:
    """SYNTHETIC FIXTURE: one valid ``observations`` row (a made-up Stooq SPY close)."""
    row: dict[str, Any] = {
        "instrument_id": "SPY",
        "source": "stooq",
        "dataset": "daily",
        "symbol": "spy.us",
        "tier": 3,
        "field": "close",
        "interval": "1d",
        "as_of": as_of_1d(date(2026, 11, 13)),
        "value": 100.0,
        "unit": "USD",
        "is_derived": False,
        "is_fallback": False,
        "fetch_id": "F1",
        "run_id": "R1",
        "ingested_at": datetime(2026, 11, 13, 22, 0, tzinfo=UTC),
        "parser_version": "t1",
    }
    row.update(over)
    return row


def ctx_at(
    config: Config,
    store: LocalBackend,
    now: datetime,
    *,
    target: date | None = None,
    job: JobKind = JobKind.EOD,
    run_id: str = "R1",
) -> RunContext:
    return RunContext(
        run_id=run_id,
        job=job,
        trigger=Trigger.MANUAL,
        target_date=target or et_date(now),
        now_utc=now,
        git_sha="deadbeef",
        config_hash="cafe",
        config=config,
        store=store,
    )


def write_obs(store: LocalBackend, config: Config, rows: list[dict[str, Any]]) -> None:
    """Append rows, one file per (run_id, ET date of as_of), like the runner's flush."""
    groups: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["run_id"], et_date(r["as_of"]))].append(r)
    for (run_id, part), chunk in groups.items():
        ctx = ctx_at(config, store, datetime(2026, 1, 1, tzinfo=UTC), run_id=run_id)
        Datasets(store).append("observations", to_frame("observations", chunk), replace(ctx), part)


def duck_for(store: LocalBackend, config: Config) -> duckdb.DuckDBPyConnection:
    return connect(store, config)


def find(results: list[DQResult], **match: Any) -> list[DQResult]:
    """Results whose attributes (or ``details`` keys) equal every ``match`` item."""
    out = []
    for r in results:
        details = json.loads(r.details_json())
        if all(getattr(r, k, details.get(k)) == v for k, v in match.items()):
            out.append(r)
    return out
