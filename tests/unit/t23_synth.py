"""Builders for the T23 tests (importable as ``t23_synth``).

SYNTHETIC FIXTURE: every fetch, latency, cross-check and price below is made up. Numbers are
chosen so counting the wrong run, or a skipped fetch, gives a visibly different result.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from deskbrief.config import Config, load_config
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.schemas import to_frame
from deskbrief.store.datasets import Datasets
from deskbrief.store.local import LocalBackend

REPO_ROOT = Path(__file__).resolve().parents[2]
D = date(2026, 11, 16)  # a Monday (ET)
NOON = datetime(2026, 11, 16, 17, 0, tzinfo=UTC)  # 12:00 ET


def config() -> Config:
    return load_config(REPO_ROOT / "config.yaml")


def ctx_for(
    store: LocalBackend, run_id: str, target: date = D, job: JobKind = JobKind.EOD
) -> RunContext:
    return RunContext(
        run_id=run_id,
        job=job,
        trigger=Trigger.MANUAL,
        target_date=target,
        now_utc=NOON,
        git_sha="deadbeef",
        config_hash="cafe",
        config=config(),
        store=store,
    )


def put(
    store: LocalBackend, table: str, rows: list[dict[str, Any]], run_id: str, part: date
) -> str:
    return Datasets(store).append(table, to_frame(table, rows), ctx_for(store, run_id, part), part)


def run_row(run_id: str, target: date, started: datetime, job: str = "eod") -> dict[str, Any]:
    return {
        "run_id": run_id, "job": job, "trigger": "manual", "target_date": target,
        "started_at": started, "status": "ok", "git_sha": "deadbeef", "config_hash": "cafe",
        "n_fetch": 1, "n_fetch_failed": 0, "n_obs": 1, "n_dq_fail": 0, "n_dq_warn": 0,
    }  # fmt: skip


def fetch(
    run_id: str, source: str, status: str, latency_ms: int, n: int, *, age_s: int | None = 60
) -> dict[str, Any]:
    """One ``source_log`` row; the data it returned is ``age_s`` older than the response."""
    asked = NOON + timedelta(minutes=n)
    answered = asked + timedelta(milliseconds=latency_ms)
    return {
        "fetch_id": f"{run_id}-{source}-{n}", "run_id": run_id, "source": source,
        "dataset": "daily", "symbols": "SYN", "request_redacted": "{}", "requested_at": asked,
        "responded_at": answered, "latency_ms": latency_ms,
        "http_status": 200, "status": status, "bytes": 10, "rows_parsed": 1,
        "rows_quarantined": 0, "attempt": 1,
        "data_max_as_of": None if age_s is None else answered - timedelta(seconds=age_s),
    }  # fmt: skip


def xcheck(run_id: str, n: int, status: str, left: str, right: str) -> dict[str, Any]:
    return {
        "result_id": f"{run_id}-X{n}", "run_id": run_id, "check_name": f"synthetic_{n}",
        "group_id": "sp500", "target_date": D, "at_time": NOON, "relation": "return",
        "left_instrument": "SPY", "left_source": left, "right_instrument": "ES_F",
        "right_source": right, "left_value": 1.0, "right_value": 1.0, "expected_value": 1.0,
        "diff_bps": 1.0, "warn_bps": 5.0, "fail_bps": 10.0, "status": status,
        "details_json": json.dumps({"ref_date": "2026-11-13"}),
    }  # fmt: skip
