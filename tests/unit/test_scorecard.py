"""T23: ``scorecard_daily`` from source_log and cross-check disagreements.

SYNTHETIC FIXTURE: all rows come from ``t23_synth`` (made up).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
from t23_synth import NOON, D, ctx_for, fetch, put, run_row, xcheck

from deskbrief import scorecard
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend


def world(tmp_path: Path, *, run_b_row: bool = True) -> LocalBackend:
    """RUNA is an earlier run of the day (all-ok, must be ignored); RUNB is the latest."""
    store = LocalBackend(tmp_path / "data")
    runs = [run_row("RUNA", D, NOON - timedelta(hours=2))]
    if run_b_row:
        runs.append(run_row("RUNB", D, NOON))
    put(store, "runs", runs[:1], "RUNA", D)
    if run_b_row:
        put(store, "runs", runs[1:], "RUNB", D)
    put(store, "source_log", [fetch("RUNA", "yahoo", "ok", 10, n) for n in range(5)], "RUNA", D)
    put(
        store,
        "source_log",
        [
            fetch("RUNB", "yahoo", "ok", 100, 1),
            fetch("RUNB", "yahoo", "ok", 200, 2),
            fetch("RUNB", "yahoo", "error", 900, 3, age_s=None),
            fetch("RUNB", "yahoo", "skipped_calendar", 0, 4, age_s=None),  # not a fetch attempt
            fetch("RUNB", "stooq", "ok", 400, 5, age_s=3600),
        ],
        "RUNB",
        D,
    )
    put(store, "crosscheck_results", [xcheck("RUNA", 0, "fail", "alpaca", "yahoo")], "RUNA", D)
    put(
        store,
        "crosscheck_results",
        [
            xcheck("RUNB", 1, "pass", "alpaca", "yahoo"),
            xcheck("RUNB", 2, "warn", "alpaca", "yahoo"),
            xcheck("RUNB", 3, "fail", "yahoo", "ecb"),
            xcheck("RUNB", 4, "skip", "stooq", "stooq"),  # a skipped check compared nothing
        ],
        "RUNB",
        D,
    )
    return store


def card(store: LocalBackend, **kw: str) -> pd.DataFrame:
    return scorecard.compute_scorecard(connect(store, ctx_for(store, "X").config), D, **kw)


def test_numbers_come_from_the_latest_run_only(tmp_path: Path) -> None:
    df = card(world(tmp_path)).set_index("source")
    assert list(df.index) == ["stooq", "yahoo"]  # skipped_calendar is not a fetch attempt
    y = df.loc["yahoo"]
    assert y["fetches"] == 3  # RUNA's five fetches are ignored
    assert y["ok_rate"] == pytest.approx(2 / 3)
    assert y["error_rate"] == pytest.approx(1 / 3)
    assert y["median_latency_ms"] == 200
    assert y["p95_latency_ms"] == pytest.approx(830)  # 200 + 0.9 * (900 - 200)
    assert y["median_data_age_s"] == pytest.approx(60)  # the error row has no data
    s = df.loc["stooq"]
    assert (s["fetches"], s["ok_rate"], s["median_latency_ms"], s["median_data_age_s"]) == (
        1,
        1.0,
        400,
        3600,
    )
    assert (df["date"] == D).all()


def test_disagreement_rate_counts_both_sides_and_ignores_skips(tmp_path: Path) -> None:
    df = card(world(tmp_path)).set_index("source")
    y = df.loc["yahoo"]  # pass + warn (right side) and fail (left side); RUNA's fail is ignored
    assert y["n_crosschecks"] == 3
    assert y["disagreement_rate"] == pytest.approx(2 / 3)
    s = df.loc["stooq"]
    assert s["n_crosschecks"] == 0
    assert pd.isna(s["disagreement_rate"])  # nothing was compared: unknown, not 0 %


def test_current_run_is_scored_before_its_runs_row_exists(tmp_path: Path) -> None:
    store = world(tmp_path, run_b_row=False)  # the runner writes ``runs`` after the hooks
    assert scorecard.latest_run_id(connect(store, ctx_for(store, "X").config), D) == "RUNA"
    df = card(store, run_id="RUNB").set_index("source")
    assert df.loc["yahoo"]["fetches"] == 3


def test_write_overwrites_the_day_and_skips_an_empty_day(tmp_path: Path) -> None:
    store = world(tmp_path)
    ctx = ctx_for(store, "RUNB")
    key = scorecard.write_scorecard(ctx)
    assert key == "derived/scorecard_daily/date=2026-11-16.parquet"
    assert scorecard.write_scorecard(ctx) == key  # a rerun overwrites, never KeyExistsError
    assert len(Datasets(store).read("scorecard_daily")) == 2
    empty = ctx_for(store, "RUNZ", target=D + timedelta(days=1))
    assert scorecard.write_scorecard(empty) is None  # nothing fetched: keep any earlier file
