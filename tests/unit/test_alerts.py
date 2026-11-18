"""T40 alerts: one test per kind, severity ladders, once-per-run write, and step wiring.

SYNTHETIC FIXTURE: every row, price and volume is made up; the store is a temp directory.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent / "checks"))
sys.path.insert(0, str(Path(__file__).parent / "pipeline"))

from checks_support import ET_NOW, as_of_1d, ctx_at, obs, write_obs
from pipeline_fakes import FakeSource, make_env

from deskbrief import alerts
from deskbrief.config import Config, load_config
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.pipeline.jobs import JobSteps, steps_for
from deskbrief.pipeline.runner import run_job
from deskbrief.schemas import to_frame, validate
from deskbrief.store.datasets import Datasets
from deskbrief.store.local import LocalBackend

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET = date(2026, 11, 16)  # Monday
NOW = datetime(2026, 11, 16, 11, 20, tzinfo=UTC)  # 06:20 ET


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(REPO_ROOT / "config.yaml")


@pytest.fixture
def store(tmp_path: Path) -> LocalBackend:
    return LocalBackend(tmp_path / "data")


@pytest.fixture
def ctx(config: Config, store: LocalBackend) -> RunContext:
    return ctx_at(config, store, NOW, target=TARGET, job=JobKind.MORNING, run_id="RUN1")


def dq_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "result_id": "x", "run_id": "RUN1", "check_name": "anomaly", "check_type": "anomaly",
        "source": "stooq", "instrument_id": "SPY", "target_date": TARGET, "evaluated_at": NOW,
        "status": "warn", "observed": 3.5, "threshold": 3.0,
        "details_json": json.dumps({"as_of_day": "2026-11-13", "dataset": "daily"}),
    }  # fmt: skip
    row.update(over)
    return row


def x_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "result_id": "x", "run_id": "RUN1", "check_name": "close_vs_official", "group_id": "g1",
        "target_date": TARGET, "at_time": NOW, "relation": "level", "left_instrument": "UST10Y",
        "left_source": "treasury", "left_value": 4.2, "right_instrument": "UST10Y_FRED",
        "right_source": "fred", "right_value": 4.0, "expected_value": None, "diff_bps": 20.0,
        "warn_bps": 2.0, "fail_bps": 5.0, "status": "fail", "details_json": "{}",
    }  # fmt: skip
    row.update(over)
    return row


def log_row(source: str, status: str, n: int = 0) -> dict[str, Any]:
    return {
        "fetch_id": f"{source}-{n}-{status}", "run_id": "RUN1", "source": source, "dataset": "d",
        "symbols": "A", "request_redacted": "{}", "requested_at": NOW, "responded_at": NOW,
        "latency_ms": 1, "http_status": 200, "status": status, "bytes": 0, "rows_parsed": 0,
        "rows_quarantined": 0, "attempt": 1,
    }  # fmt: skip


def put(store: LocalBackend, ctx: RunContext, table: str, rows: list[dict[str, Any]]) -> None:
    Datasets(store).append(table, to_frame(table, rows), ctx, TARGET)


def build(ctx: RunContext, store: LocalBackend, config: Config) -> pd.DataFrame:
    from deskbrief.store.duck import connect

    duck = connect(store, config)
    try:
        return alerts.build_alerts(ctx, duck)
    finally:
        duck.close()


def only(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    return df[df["kind"] == kind].reset_index(drop=True)


# --- z-score moves (read from the anomaly check, which owns daily_moves / MIN_MOVES) ---------


@pytest.mark.parametrize(
    ("status", "z", "severity"),
    [("fail", 5.5, "crit"), ("warn", 3.5, "warn"), ("pass", 2.4, "info")],
)
def test_zscore_move_severity_ladder(ctx, store, config, status, z, severity) -> None:  # type: ignore[no-untyped-def]
    put(store, ctx, "dq_results", [dq_row(status=status, observed=z)])
    out = only(build(ctx, store, config), "zscore_move")
    assert len(out) == 1
    r = out.iloc[0]
    assert (r["severity"], r["instrument_id"], r["source"]) == (severity, "SPY", "stooq")
    assert r["observed"] == pytest.approx(z)
    assert r["as_of"] == as_of_1d(date(2026, 11, 13))
    assert "SPY" in r["message"]


def test_zscore_ignores_calm_skipped_and_other_runs(ctx, store, config) -> None:  # type: ignore[no-untyped-def]
    put(
        store,
        ctx,
        "dq_results",
        [
            dq_row(status="pass", observed=alerts.Z_INFO - 0.1, instrument_id="A"),
            dq_row(status="skip", observed=None, threshold=None, instrument_id="B"),
            dq_row(status="fail", observed=9.0, run_id="OTHER", instrument_id="C"),
        ],
    )
    assert only(build(ctx, store, config), "zscore_move").empty


# --- volume spikes ---------------------------------------------------------------------------


def volume_rows(last_ratio: float, n_hist: int = 25) -> list[dict[str, Any]]:
    """Daily volumes: ``n_hist`` days of ~1M shares, then a day of ``last_ratio`` x the mean."""
    days = [date(2026, 10, 12) + timedelta(days=i) for i in range(n_hist + 1)]
    days = [d for d in days if d.weekday() < 5][: n_hist + 1]
    hist = [1_000_000.0 + (i % 3) * 1000 for i in range(len(days) - 1)]
    vols = [*hist, last_ratio * sum(hist) / len(hist)]
    return [
        obs(
            field="volume",
            unit="shares",
            as_of=as_of_1d(d),
            value=v,
            run_id="R0",
            ingested_at=as_of_1d(d) + timedelta(hours=20),
        )
        for d, v in zip(days, vols, strict=True)
    ], days[-1]  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("ratio", "severity"),
    [
        (1.9, None),
        (alerts.VOLUME_RATIO_INFO + 0.01, "info"),
        (alerts.VOLUME_RATIO_WARN + 0.01, "warn"),
        (alerts.VOLUME_RATIO_CRIT + 0.1, "crit"),
    ],
)  # fmt: skip
def test_volume_spike_ladder(store, config, ratio, severity) -> None:  # type: ignore[no-untyped-def]
    rows, last = volume_rows(ratio)
    write_obs(store, config, rows)
    ctx = ctx_at(config, store, ET_NOW(last + timedelta(days=1), "07:00"), run_id="RUN1")
    out = only(build(ctx, store, config), "volume_spike")
    if severity is None:
        assert out.empty
        return
    assert len(out) == 1
    r = out.iloc[0]
    assert r["severity"] == severity
    assert r["observed"] == pytest.approx(ratio, rel=0.01)
    assert r["instrument_id"] == "SPY"
    assert r["as_of"] == as_of_1d(last)


def test_volume_spike_needs_enough_history(store, config) -> None:  # type: ignore[no-untyped-def]
    rows, last = volume_rows(10.0, n_hist=alerts.MIN_MOVES - 3)
    write_obs(store, config, rows)
    ctx = ctx_at(config, store, ET_NOW(last + timedelta(days=1), "07:00"), run_id="RUN1")
    assert only(build(ctx, store, config), "volume_spike").empty


def test_volume_spike_ignores_a_stale_series(store, config) -> None:  # type: ignore[no-untyped-def]
    rows, last = volume_rows(10.0)
    write_obs(store, config, rows)
    ctx = ctx_at(config, store, ET_NOW(last + timedelta(days=9), "07:00"), run_id="RUN1")
    assert only(build(ctx, store, config), "volume_spike").empty  # an old spike is not news


def test_no_volume_data_is_not_an_error(ctx, store, config) -> None:  # type: ignore[no-untyped-def]
    assert build(ctx, store, config).empty


# --- cross-check fails -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "severity", "threshold"), [("fail", "crit", 5.0), ("warn", "warn", 2.0)]
)
def test_crosscheck_alerts(ctx, store, config, status, severity, threshold) -> None:  # type: ignore[no-untyped-def]
    put(
        store,
        ctx,
        "crosscheck_results",
        [x_row(status=status), x_row(status="pass", left_instrument="Z")],
    )
    out = only(build(ctx, store, config), "crosscheck_fail")
    assert len(out) == 1
    r = out.iloc[0]
    assert (r["severity"], r["instrument_id"], r["source"]) == (severity, "UST10Y", "treasury")
    assert (r["observed"], r["threshold"]) == (20.0, threshold)
    assert "fred" in r["message"]
    assert "close_vs_official" in r["message"]


# --- source down (source_log) ----------------------------------------------------------------


def test_source_down_ladder(ctx, store, config) -> None:  # type: ignore[no-untyped-def]
    rows = (
        [log_row("alpha", "error", i) for i in range(3)]  # all failed -> crit
        + [log_row("beta", "ok", 0), log_row("beta", "rate_limited", 1)]  # half -> warn
        + [log_row("gamma", "ok", i) for i in range(9)] + [log_row("gamma", "error", 9)]  # info
        + [log_row("delta", "ok", 0), log_row("delta", "empty", 1)]  # healthy
        + [log_row("eps", "budget_exhausted", 0)]
    )  # fmt: skip
    put(store, ctx, "source_log", rows)
    out = only(build(ctx, store, config), "source_down").set_index("source")
    assert out["severity"].to_dict() == {
        "alpha": "crit", "beta": "warn", "gamma": "info", "eps": "crit",
    }  # fmt: skip
    assert out.loc["alpha", "observed"] == 1.0
    assert out.loc["beta", "observed"] == 0.5
    assert out.loc["alpha", "threshold"] == alerts.SOURCE_DOWN_CRIT_FRACTION
    assert pd.isna(out.loc["alpha", "instrument_id"])


# --- stale (freshness) -----------------------------------------------------------------------


def test_stale_from_freshness_results(ctx, store, config) -> None:  # type: ignore[no-untyped-def]
    d = json.dumps({"dataset": "daily", "data_day": "2026-11-13", "state": "late"})

    def fresh(status: str, source: str, observed: float | None) -> dict[str, Any]:
        return dq_row(
            check_name="freshness", check_type="freshness", status=status, instrument_id=None,
            source=source, observed=observed, threshold=None if observed is None else 30.0,
            details_json=d,
        )  # fmt: skip

    rows = [
        fresh("fail", "treasury", 130.0),
        fresh("warn", "fred", 45.0),
        fresh("pass", "ecb", 0.0),
        fresh("skip", "cboe", None),
    ]
    put(store, ctx, "dq_results", rows)
    out = only(build(ctx, store, config), "stale").set_index("source")
    assert out["severity"].to_dict() == {"treasury": "crit", "fred": "warn"}
    assert out.loc["treasury", "observed"] == 130.0
    assert "2026-11-13" in out.loc["treasury", "message"]


# --- frame shape and the once-per-run write --------------------------------------------------


def test_frame_validates_and_alert_ids_are_unique(ctx, store, config) -> None:  # type: ignore[no-untyped-def]
    put(store, ctx, "crosscheck_results", [x_row(), x_row(left_instrument="B")])
    put(store, ctx, "source_log", [log_row("alpha", "error")])
    out = build(ctx, store, config)
    validate("alerts", out)
    assert set(out["run_id"]) == {"RUN1"}
    assert out["alert_id"].is_unique


def test_step_writes_one_file_and_a_second_call_adds_nothing(ctx, store, config) -> None:  # type: ignore[no-untyped-def]
    put(store, ctx, "crosscheck_results", [x_row()])
    put(store, ctx, "source_log", [log_row("alpha", "error")])
    alerts.alerts_step(ctx)
    alerts.alerts_step(ctx)  # e.g. a re-invoked step in the same run
    keys = [k for k in store.list("") if "/alerts/" in k and k.endswith(".parquet")]
    assert len(keys) == 1
    assert f"date={TARGET.isoformat()}" in keys[0]
    assert "run=RUN1" in keys[0]
    df = Datasets(store).read("alerts")
    assert len(df) == 2
    assert df["alert_id"].is_unique


def test_step_writes_nothing_when_there_is_no_evidence(ctx, store) -> None:  # type: ignore[no-untyped-def]
    alerts.alerts_step(ctx)
    assert not [k for k in store.list("") if "/alerts/" in k]


# --- wiring (spec 5.4: checks -> alerts -> scorecard/serving) --------------------------------


def test_alerts_run_after_checks_and_before_serving(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    order: list[str] = []
    from deskbrief.pipeline import jobs

    def checks(c: RunContext) -> Any:
        order.append("checks")

    monkeypatch.setattr(alerts, "alerts_step", lambda c: order.append("alerts"))
    monkeypatch.setattr(jobs, "serving_step", lambda c: order.append("serving"))
    monkeypatch.setattr(jobs, "run_checks_step", checks)
    for job in JobKind:
        order.clear()
        s = steps_for(job)
        s.run_checks(None)  # type: ignore[arg-type]
        s.build_serving(None)  # type: ignore[arg-type]
        assert order == ["checks", "alerts", "serving"], job


def test_a_failing_alerts_step_still_runs_serving_and_degrades_the_run(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from deskbrief.pipeline import jobs

    ran: list[str] = []

    def boom(c: RunContext) -> None:
        raise RuntimeError("alerts exploded")

    monkeypatch.setattr(alerts, "alerts_step", boom)
    monkeypatch.setattr(jobs, "serving_step", lambda c: ran.append("serving"))
    env = make_env(tmp_path, [FakeSource("stooq")])
    env.steps_for = lambda job: JobSteps(build_serving=steps_for(job).build_serving)
    m = run_job(env, JobKind.EOD, target_date=TARGET, trigger=Trigger.MANUAL)
    assert ran == ["serving"]
    assert any("alerts exploded" in n for n in m.notes)


def test_real_run_writes_alerts_once_between_checks_and_serving(tmp_path: Path) -> None:
    env = make_env(
        tmp_path, [FakeSource("stooq"), FakeSource("ecb", mode="raise_fetch")], real_steps=True
    )
    m = run_job(env, JobKind.EOD, target_date=TARGET, trigger=Trigger.MANUAL)
    keys = [k for k in env.store.list("") if "/alerts/" in k and k.endswith(".parquet")]
    assert len(keys) == 1
    df = Datasets(env.store).read("alerts")
    assert set(df["run_id"]) == {m.run_id}
    assert (
        (df["kind"] == "source_down") & (df["source"] == "ecb") & (df["severity"] == "crit")
    ).any()
