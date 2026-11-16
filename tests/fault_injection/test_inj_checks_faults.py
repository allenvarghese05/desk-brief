"""INJECTED FAULT: stale price, x10 unit flip and a FRED revision through run_checks (T20).

SYNTHETIC FIXTURE: every observation below is made up. Named ``test_inj_checks_*`` so pytest
collects it without a conftest hook.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from deskbrief.checks import run_checks
from deskbrief.config import Config, load_config
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.schemas import to_frame
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import ET, et_date

CONFIG: Config = load_config(Path(__file__).resolve().parents[2] / "config.yaml")
WED = date(2026, 11, 18)


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


def row(d: date, value: float, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "instrument_id": "SPY",
        "source": "stooq",
        "dataset": "daily",
        "symbol": "spy.us",
        "tier": 3,
        "field": "close",
        "interval": "1d",
        "as_of": at(d, 0),
        "value": value,
        "unit": "USD",
        "is_derived": False,
        "is_fallback": False,
        "fetch_id": "F1",
        "run_id": "R1",
        "ingested_at": at(d, 21),
        "parser_version": "t",
    }
    return base | over


def run(
    store: LocalBackend, rows: list[dict[str, Any]], now: datetime, run_id: str
) -> pd.DataFrame:
    ds = Datasets(store)
    by_key: dict[tuple[str, date], list[dict[str, Any]]] = {}
    for r in rows:
        by_key.setdefault((r["run_id"], et_date(r["as_of"])), []).append(r)
    for (rid, part), chunk in by_key.items():
        wctx = RunContext(rid, JobKind.EOD, Trigger.MANUAL, part, now, "g", "c", CONFIG, store)
        ds.append("observations", to_frame("observations", chunk), wctx, part)
    ctx = RunContext(
        run_id, JobKind.EOD, Trigger.MANUAL, et_date(now), now, "g", "c", CONFIG, store
    )
    dq, _ = run_checks(ctx, connect(store, CONFIG))
    return dq


def select(dq: pd.DataFrame, check: str, **kw: str) -> pd.DataFrame:
    m = dq["check_name"] == check
    for k, v in kw.items():
        m &= dq[k] == v
    return dq[m]


def test_injected_stale_price_fails_freshness(tmp_path: Path) -> None:
    """INJECTED FAULT: Stooq stopped updating; the newest close is a week old."""
    rows = [row(WED - timedelta(days=7 + i), 100.0 + i, run_id="R0") for i in range(3)]
    dq = run(LocalBackend(tmp_path), rows, at(WED, 22, 30), "R9")
    res = select(dq, "freshness", source="stooq")
    assert list(res["status"]) == ["fail"]


def test_injected_x10_unit_flip_fails_validity_and_anomaly(tmp_path: Path) -> None:
    """INJECTED FAULT: a 10-year yield arrives as 42.5 instead of 4.25 (unit flip)."""
    start = date(2026, 9, 1)
    steps = [0.01, -0.02, 0.015, -0.01, 0.02, -0.015, 0.01, -0.005] * 4
    level, rows = 4.2, []
    for i, s in enumerate(steps):
        level += s
        rows.append(
            row(
                start + timedelta(days=i),
                level,
                instrument_id="UST_10Y",
                source="treasury",
                dataset="par_curve",
                symbol="10 Yr",
                field="yield",
                unit="pct",
                run_id="R0",
                ingested_at=at(start, 21),
            )
        )
    flip_day = start + timedelta(days=len(steps))
    rows.append(
        row(
            flip_day,
            level * 10,
            instrument_id="UST_10Y",
            source="treasury",
            dataset="par_curve",
            symbol="10 Yr",
            field="yield",
            unit="pct",
            run_id="R9",
            ingested_at=at(flip_day, 18, 30),
        )
    )
    dq = run(LocalBackend(tmp_path), rows, at(flip_day + timedelta(days=1), 7), "R9")
    assert list(select(dq, "validity", instrument_id="UST_10Y")["status"]) == ["fail"]
    assert list(select(dq, "anomaly", instrument_id="UST_10Y")["status"]) == ["fail"]


def test_injected_fred_revision_records_row_and_warns(tmp_path: Path) -> None:
    """INJECTED FAULT: FRED revises last month's unemployment rate 3.1 -> 3.2."""
    common = {
        "instrument_id": "UNRATE",
        "source": "fred",
        "dataset": "series",
        "symbol": "UNRATE",
        "field": "rate",
        "interval": "1mo",
        "unit": "pct",
        "as_of": at(date(2026, 10, 1), 0),
    }
    rows = [
        row(WED, 3.1, **common, run_id="R0", fetch_id="F0", ingested_at=at(date(2026, 11, 6), 9)),
        row(WED, 3.2, **common, run_id="R9", fetch_id="F9", ingested_at=at(WED, 17)),
    ]
    store = LocalBackend(tmp_path)
    dq = run(store, rows, at(WED, 22, 30), "R9")
    res = select(dq, "revision", instrument_id="UNRATE")
    assert list(res["status"]) == ["warn"]
    now = at(WED, 22, 30)
    ctx = RunContext("R9", JobKind.EOD, Trigger.MANUAL, WED, now, "g", "c", CONFIG, store)
    _, revs = run_checks(ctx, connect(store, CONFIG))
    assert len(revs) == 1
    assert (revs.loc[0, "old_value"], revs.loc[0, "new_value"]) == (3.1, 3.2)
