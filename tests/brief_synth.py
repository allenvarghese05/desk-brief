"""Builders for the brief/query tests (importable as ``brief_synth``).

SYNTHETIC FIXTURE: every price, yield, check result and fetch below is made up. Values are
chosen so a wrong baseline (e.g. Thursday instead of Friday) gives a visibly different number.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from deskbrief.calendars import prev_business_day
from deskbrief.config import Config, load_config
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.schemas import to_frame
from deskbrief.store import duck
from deskbrief.store.datasets import Datasets
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_date, et_to_utc

REPO_ROOT = Path(__file__).resolve().parents[1]
_ids = itertools.count(1)


def load() -> Config:
    return load_config(REPO_ROOT / "config.yaml")


def fixture_config() -> Config:
    """A small frozen config (independent of ``config.yaml``) for golden snapshots."""
    return load_config(REPO_ROOT / "tests" / "unit" / "report" / "golden" / "config.fixture.yaml")


def et(d: date, hhmm: str = "00:00") -> datetime:
    """UTC instant of an Eastern wall-clock time (00:00 ET is the as_of of a 1d row)."""
    return et_to_utc(d, hhmm)


def obs(
    instrument_id: str,
    value: float,
    as_of: datetime,
    *,
    unit: str = "USD",
    field: str = "close",
    interval: str = "1d",
    tier: int = 3,
    source: str = "stooq",
    dataset: str = "daily",
    symbol: str = "SYN",
    is_fallback: bool = False,
    is_derived: bool = False,
    ingested_at: datetime | None = None,
    fetch_id: str | None = None,
) -> dict[str, Any]:
    n = next(_ids)
    if ingested_at is None:  # daily rows land 20:00 ET that day, ticks a minute later
        ingested_at = (
            et(et_date(as_of), "20:00")
            if interval in ("1d", "1mo")
            else as_of + timedelta(minutes=1)
        )
    return {
        "instrument_id": instrument_id,
        "source": source,
        "dataset": dataset,
        "symbol": symbol,
        "tier": tier,
        "field": field,
        "interval": interval,
        "as_of": as_of,
        "value": value,
        "unit": unit,
        "is_derived": is_derived,
        "is_fallback": is_fallback,
        "fetch_id": fetch_id or f"F{n}",
        "run_id": f"RUNOBS{n // 1000}",
        "ingested_at": ingested_at,
        "parser_version": "synthetic-1",
    }


def daily(instrument_id: str, d: date, value: float, **kw: Any) -> dict[str, Any]:
    return obs(instrument_id, value, et(d), **kw)


def tick(instrument_id: str, when: datetime, value: float, **kw: Any) -> dict[str, Any]:
    kw.setdefault("interval", "tick")
    kw.setdefault("tier", 2)
    kw.setdefault("field", "last")
    kw.setdefault("source", "yahoo")
    kw.setdefault("dataset", "quote")
    return obs(instrument_id, value, when, **kw)


def _ctx(config: Config, store: LocalBackend, run_id: str, target: date) -> RunContext:
    return RunContext(
        run_id=run_id,
        job=JobKind.MORNING,
        trigger=Trigger.MANUAL,
        target_date=target,
        now_utc=datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
        git_sha="deadbeef",
        config_hash="cafe",
        config=config,
        store=store,
    )


class World:
    """A tmp-dir store plus helpers to write synthetic rows into it, then ``connect()``."""

    def __init__(self, root: Path, config: Config | None = None) -> None:
        self.config = config or load()
        self.store = LocalBackend(root / "data")
        self._runs = itertools.count(1)

    def _append(self, table: str, rows: Iterable[dict[str, Any]], part: Any) -> None:
        groups: dict[date, list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(part(r), []).append(r)
        for d, chunk in sorted(groups.items()):
            run_id = f"RUN{table[:3].upper()}{next(self._runs)}"
            ctx = _ctx(self.config, self.store, run_id, d)
            Datasets(self.store).append(table, to_frame(table, chunk), ctx, d)

    def observations(self, rows: Iterable[dict[str, Any]]) -> None:
        self._append("observations", rows, lambda r: et_date(r["as_of"]))

    def dq(
        self,
        target: date,
        results: list[tuple[str, str, str | None, str | None]],
        at: str = "07:00",
        check_type: str = "freshness",
        run_id: str = "RUNDQ",
    ) -> None:
        """``(check_name, status, source, instrument_id)`` rows for ``target`` (run ``run_id``)."""
        rows = [
            {
                "result_id": f"DQ{next(_ids)}",
                "run_id": run_id,
                "check_name": name,
                "check_type": check_type,
                "source": source,
                "instrument_id": inst,
                "target_date": target,
                "evaluated_at": et(target, at),
                "status": status,
                "observed": 1.5,
                "threshold": 1.0,
                "details_json": "{}",
            }
            for name, status, source, inst in results
        ]
        self._append("dq_results", rows, lambda r: r["target_date"])

    def xchecks(
        self,
        target: date,
        results: list[tuple[str, str, float]],
        *,
        ref: date | None = None,
        mirror: bool = False,
        run_id: str = "RUNXC",
        at: str = "07:00",
    ) -> None:
        """``(check_name, status, diff_bps)`` rows for ``target``, written by run ``run_id``.

        ``ref`` is the data date the check compared (``details.ref_date``; default: the
        previous NYSE session, so it differs from ``target`` on purpose). ``mirror`` also writes
        the ``consistency`` ``dq_results`` row the runner mirrors for each cross-check (spec §7.2).
        """
        ref = ref or prev_business_day("XNYS", target)
        rows = [
            {
                "result_id": f"XC{next(_ids)}",
                "run_id": run_id,
                "check_name": name,
                "group_id": "sp500",
                "target_date": target,
                "at_time": et(target, at),
                "relation": "return",
                "left_instrument": "SPY",
                "left_source": "alpaca",
                "right_instrument": "ES_F",
                "right_source": "yahoo",
                "left_value": 1.0,
                "right_value": 1.0,
                "expected_value": 1.0,
                "diff_bps": diff,
                "warn_bps": 5.0,
                "fail_bps": 10.0,
                "status": status,
                "details_json": json.dumps({"ref_date": ref.isoformat()}),
            }
            for name, status, diff in results
        ]
        self._append("crosscheck_results", rows, lambda r: r["target_date"])
        if mirror:
            self.dq(
                target,
                [(name, status, None, "SPY") for name, status, _ in results],
                check_type="consistency",
                run_id=run_id,
                at=at,
            )

    def fetches(self, target: date, results: list[tuple[str, str]]) -> None:
        """``(source, status)`` fetch attempts requested at 06:00 ET on ``target``."""
        rows = [
            {
                "fetch_id": f"SL{next(_ids)}",
                "run_id": "RUNSL",
                "source": source,
                "dataset": "daily",
                "symbols": "SYN",
                "request_redacted": "GET /synthetic",
                "requested_at": et(target, "06:00"),
                "responded_at": et(target, "06:00") + timedelta(seconds=2),
                "latency_ms": 2000,
                "http_status": 200,
                "status": status,
                "bytes": 10,
                "rows_parsed": 1,
                "rows_quarantined": 0,
                "attempt": 1,
            }
            for source, status in results
        ]
        self._append("source_log", rows, lambda r: et_date(r["requested_at"]))

    def log_fetch(self, fetch_id: str, when: datetime, raw_key: str) -> None:
        """One ok ``source_log`` row with a known ``fetch_id`` (for lineage tests)."""
        row = {
            "fetch_id": fetch_id,
            "run_id": "RUNSL",
            "source": "yahoo",
            "dataset": "quote",
            "symbols": "SYN",
            "request_redacted": "GET /synthetic",
            "requested_at": when,
            "responded_at": when + timedelta(seconds=1),
            "latency_ms": 1000,
            "http_status": 200,
            "status": "ok",
            "raw_key": raw_key,
            "sha256": "0" * 64,
            "bytes": 10,
            "rows_parsed": 1,
            "rows_quarantined": 0,
            "attempt": 1,
        }
        self._append("source_log", [row], lambda r: et_date(r["requested_at"]))

    def run(self, target: date, status: str = "ok") -> None:
        row = {
            "run_id": f"RUNRUN{next(_ids)}",
            "job": "morning",
            "trigger": "manual",
            "target_date": target,
            "started_at": et(target, "06:55"),
            "finished_at": et(target, "07:00"),
            "status": status,
            "git_sha": "deadbeef",
            "config_hash": "cafe",
            "n_fetch": 1,
            "n_fetch_failed": 0,
            "n_obs": 1,
            "n_dq_fail": 0,
            "n_dq_warn": 0,
        }
        self._append("runs", [row], lambda r: r["target_date"])

    def connect(self) -> duckdb.DuckDBPyConnection:
        return duck.connect(self.store, self.config)


# ---------------------------------------------------------------------------
# the standard scenario: Monday 16 Nov 2026, 07:00 ET (12:00 UTC); Friday 13 Nov is the NYSE close
# ---------------------------------------------------------------------------

WED, THU, FRI, MON = date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16)
MON_0700 = et(MON, "07:00")


def monday_rows() -> list[dict[str, Any]]:
    tsy = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
    yh = {"source": "yahoo", "dataset": "history", "tier": 2, "unit": "index_pts"}
    ecb = {"source": "ecb", "dataset": "eurofxref", "field": "rate", "unit": "fx_rate"}
    return [
        daily("UST_10Y", THU, 4.45, **tsy),
        daily("UST_10Y", FRI, 4.40, **tsy),
        tick("UST_10Y", et(MON, "06:58"), 4.37, unit="pct"),
        daily("UST_2Y", THU, 4.20, **tsy),
        daily("UST_2Y", FRI, 4.18, **tsy),
        daily("EURUSD", THU, 1.0800, **ecb),
        daily("EURUSD", FRI, 1.0842, **ecb),
        daily("ES_F", WED, 7000.0, **yh),  # decoy: neither Friday nor the last value
        daily("ES_F", THU, 6024.0, **yh),
        daily("ES_F", FRI, 6000.0, **yh),
        tick("ES_F", et(MON, "06:59"), 5976.0, unit="index_pts"),
        daily("WTI", FRI, 72.00, source="yahoo", dataset="history", tier=2),
        daily("WTI", MON, 55.0, ingested_at=et(MON, "06:30")),  # unfinished day: must be ignored
        tick("WTI", et(MON, "06:58"), 71.20),
        daily("VIX", THU, 15.6, source="cboe", dataset="vix_history", unit="index_pts"),
        daily("VIX", FRI, 16.2, source="cboe", dataset="vix_history", unit="index_pts"),
        daily("SPY", FRI, 598.50),
        tick("SPY", et(MON, "06:59"), 600.00, tier=1, source="alpaca", dataset="iex_latest"),
    ]
