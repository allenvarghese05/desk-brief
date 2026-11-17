"""T23: ``scorecard_daily`` from source_log and cross-check disagreements.

SYNTHETIC FIXTURE: all rows come from ``t23_synth`` (made up).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from t23_synth import NOON, D, ctx_for, fetch, put, run_row, xcheck

from deskbrief import scorecard
from deskbrief.models import JobKind
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


def card(store: LocalBackend, **kw: Any) -> pd.DataFrame:
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
    assert card(store).set_index("source").loc["yahoo"]["fetches"] == 5  # only RUNA is on record
    df = card(store, current=ctx_for(store, "RUNB")).set_index("source")
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


def two_jobs(tmp_path: Path) -> LocalBackend:
    """A morning run fetched yahoo; the eod run fetched yahoo and stooq (all synthetic)."""
    store = LocalBackend(tmp_path / "data")
    for run_id, job, hours in (("RUNM", "morning", -6), ("RUNE", "eod", 0)):
        row = run_row(run_id, D, NOON + timedelta(hours=hours), job)
        put(store, "runs", [row], run_id, D)
    morning = [
        fetch("RUNM", "yahoo", "ok", 100, 1),
        fetch("RUNM", "ecb", "error", 50, 2, age_s=None),
    ]
    put(store, "source_log", morning, "RUNM", D)
    eod = [fetch("RUNE", "yahoo", "ok", 300, 3), fetch("RUNE", "stooq", "ok", 400, 4)]
    put(store, "source_log", eod, "RUNE", D)
    return store


def test_sources_of_different_jobs_on_one_date_are_all_scored(tmp_path: Path) -> None:
    df = card(two_jobs(tmp_path)).set_index("source")
    assert list(df.index) == ["ecb", "stooq", "yahoo"]  # morning-only ecb survives eod
    assert df.loc["ecb"]["error_rate"] == 1.0
    assert df.loc["yahoo"]["fetches"] == 2  # one latest run per job, summed
    assert df.loc["yahoo"]["median_latency_ms"] == 200  # pooled over both: median(100, 300)


def test_an_eod_rerun_of_one_source_replaces_only_that_source(tmp_path: Path) -> None:
    store = two_jobs(tmp_path)
    put(store, "runs", [run_row("RUNE2", D, NOON + timedelta(hours=1))], "RUNE2", D)
    put(store, "source_log", [fetch("RUNE2", "stooq", "error", 700, 5, age_s=None)], "RUNE2", D)
    df = card(store).set_index("source")
    assert df.loc["stooq"]["ok_rate"] == 0.0  # the re-run replaced RUNE's stooq
    assert df.loc["stooq"]["fetches"] == 1
    assert df.loc["yahoo"]["fetches"] == 2  # yahoo keeps RUNE's numbers (and the morning's)
    assert df.loc["yahoo"]["median_latency_ms"] == 200


def test_a_full_eod_rerun_replaces_every_eod_source(tmp_path: Path) -> None:
    store = two_jobs(tmp_path)
    put(store, "runs", [run_row("RUNE2", D, NOON + timedelta(hours=1))], "RUNE2", D)
    rerun = [fetch("RUNE2", "yahoo", "ok", 900, 5), fetch("RUNE2", "stooq", "ok", 800, 6)]
    put(store, "source_log", rerun, "RUNE2", D)
    df = card(store).set_index("source")
    assert df.loc["stooq"]["median_latency_ms"] == 800
    assert df.loc["yahoo"]["fetches"] == 2  # morning's run plus RUNE2; RUNE is gone
    assert df.loc["yahoo"]["median_latency_ms"] == 500  # median(100, 900)


def test_every_intraday_poll_counts_but_other_jobs_keep_only_the_latest(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path / "data")
    for n, ms in enumerate((100, 300, 500), start=1):  # three 15-minute polls, distinct runs
        run_id = f"RUNI{n}"
        row = run_row(run_id, D, NOON + timedelta(minutes=15 * n), "intraday")
        put(store, "runs", [row], run_id, D)
        put(store, "source_log", [fetch(run_id, "alpaca", "ok", ms, n)], run_id, D)
    df = card(store).set_index("source")
    assert df.loc["alpaca"]["fetches"] == 3
    assert df.loc["alpaca"]["median_latency_ms"] == 300  # pooled over the three polls


def test_a_morning_run_rescores_a_day_after_its_eod_with_its_own_fetches(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path / "data")
    put(store, "runs", [run_row("RUNE", D, NOON)], "RUNE", D)  # D's eod, long finished
    put(store, "source_log", [fetch("RUNE", "stooq", "ok", 400, 1)], "RUNE", D)
    morning = ctx_for(store, "RUNM", D, JobKind.MORNING)  # next morning, no runs row yet
    put(store, "source_log", [fetch("RUNM", "ecb", "ok", 50, 2)], "RUNM", D)
    assert scorecard.write_scorecard(morning) is not None
    assert set(Datasets(store).read("scorecard_daily")["source"]) == {"ecb", "stooq"}
