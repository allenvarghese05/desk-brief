"""T23: serving files (spec §6) and the serving hook.

SYNTHETIC FIXTURE: data comes from ``brief_synth`` (made up).
INJECTED FAULT: the scorecard step raising inside the hook.
"""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from t23_synth import ctx_for

from brief_synth import FRI, MON, MON_0700, World, et, monday_rows, tick
from deskbrief.models import JobKind, RunContext
from deskbrief.pipeline import serving
from deskbrief.pipeline.jobs import steps_for
from deskbrief.store.local import LocalBackend

FILES = (
    "latest_snapshot.parquet",
    "health.json",
    "scorecard_30d.parquet",
    "crosschecks_7d.parquet",
    "series_30d.parquet",
    "runs_14d.parquet",
)


def ctx(w: World, job: JobKind = JobKind.MORNING, run_id: str = "RUNSL") -> RunContext:
    assert isinstance(w.store, LocalBackend)
    return replace(ctx_for(w.store, run_id, MON, job), now_utc=MON_0700, config=w.config)


def frame(w: World, name: str) -> pd.DataFrame:
    return pq.read_table(io.BytesIO(w.store.get_bytes(f"serving/{name}"))).to_pandas()


def build(tmp_path: Path, rows: list[dict[str, Any]] | None = None) -> World:
    w = World(tmp_path)
    w.observations(monday_rows() if rows is None else rows)
    w.xchecks(MON, [("proxy_vs_exact", "warn", 6.0)], mirror=True)
    w.xchecks(MON - timedelta(days=10), [("proxy_vs_exact", "fail", 20.0)])  # outside 7 days
    w.dq(MON, [("fresh", "pass", "yahoo", "ES_F")])
    w.fetches(MON, [("yahoo", "ok")])
    w.run(MON)
    return w


def test_every_serving_file_is_written(tmp_path: Path) -> None:
    w = build(tmp_path)
    assert sorted(serving.build_serving_files(ctx(w))) == sorted(f"serving/{n}" for n in FILES)
    assert all(w.store.exists(f"serving/{n}") for n in FILES)
    assert len(frame(w, "runs_14d.parquet")) == 1


def test_snapshot_has_tier_as_of_and_source_for_every_headline_instrument(tmp_path: Path) -> None:
    w = build(tmp_path)
    serving.build_serving_files(ctx(w))
    snap = frame(w, "latest_snapshot.parquet").set_index("instrument_id")
    heads = w.config.report.headline_instruments
    assert set(heads) <= set(snap.index)
    for iid in heads:
        row = snap.loc[iid]
        assert row["tier"] in (1, 2, 3)
        assert row["source"]
        assert pd.notna(row["as_of"])
        assert row["is_headline"]
    assert not snap.loc["SPY", "is_headline"]
    es = snap.loc["ES_F"]
    assert (es["source"], es["tier"], es["value"]) == ("yahoo", 2, 5976.0)
    assert es["as_of"] == pd.Timestamp("2026-11-16 06:59", tz="America/New_York")
    assert snap.loc["EURUSD", "label"] == "Euro / US dollar (ECB reference rate)"  # P-009


def test_health_json_names_headline_instruments_with_no_data(tmp_path: Path) -> None:
    rows = [r for r in monday_rows() if r["instrument_id"] != "WTI"]
    w = build(tmp_path, rows)
    serving.build_serving_files(ctx(w))
    health = json.loads(w.store.get_bytes("serving/health.json"))
    assert health["headline_missing"] == ["WTI"]  # said so, never silently dropped
    assert "WTI" not in set(frame(w, "latest_snapshot.parquet")["instrument_id"])
    assert health["target_date"] == "2026-11-16"
    assert health["n_checks"] >= 2
    assert {s["source"]: s["status"] for s in health["sources"]}["yahoo"] == "ok"


def test_crosschecks_are_labelled_by_ref_date_and_limited_to_seven_days(tmp_path: Path) -> None:
    w = build(tmp_path)
    serving.build_serving_files(ctx(w))
    xc = frame(w, "crosschecks_7d.parquet")
    assert len(xc) == 1
    assert xc["ref_date"].iloc[0].isoformat() == FRI.isoformat()  # the date compared
    assert xc["target_date"].iloc[0].isoformat() == MON.isoformat()  # the run's date


def test_every_job_rescores_its_target_date_before_serving(tmp_path: Path) -> None:
    w = build(tmp_path)
    serving.serving_step(ctx(w, JobKind.MORNING))  # the morning job also writes the scorecard
    assert w.store.exists("derived/scorecard_daily/date=2026-11-16.parquet")
    assert frame(w, "scorecard_30d.parquet")["source"].tolist() == ["yahoo"]
    serving.serving_step(ctx(w, JobKind.EOD))  # idempotent overwrite, no KeyExistsError
    assert len(frame(w, "scorecard_30d.parquet")) == 1


def test_a_backfill_of_an_old_date_still_serves_todays_window(tmp_path: Path) -> None:
    w = build(tmp_path)
    old = replace(ctx(w, JobKind.BACKFILL), target_date=MON - timedelta(days=20))
    serving.serving_step(old)
    health = json.loads(w.store.get_bytes("serving/health.json"))
    assert health["target_date"] == "2026-11-16"  # today's ET date, not the backfilled date
    assert len(frame(w, "runs_14d.parquet")) == 1  # today's run is still in the window
    assert len(frame(w, "crosschecks_7d.parquet")) == 1
    assert health["n_checks"] >= 2


def test_only_eod_compacts(tmp_path: Path) -> None:
    w = build(tmp_path)
    w.observations([tick("ES_F", et(MON, "06:30"), 5970.0)])  # a second Monday file to compact
    serving.serving_step(ctx(w, JobKind.MORNING))
    assert not any("compacted" in k for k in w.store.list("clean/v1/observations/"))
    serving.serving_step(ctx(w, JobKind.EOD))
    assert any("run=compacted-2026-11-16" in k for k in w.store.list("clean/v1/observations/"))


def test_eod_runs_scorecard_then_compaction_then_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    for name in ("write_scorecard", "compact_recent", "build_serving_files"):
        monkeypatch.setattr(serving, name, lambda *a, _n=name, **k: calls.append(_n))
    serving.serving_step(ctx(World(tmp_path), JobKind.EOD))
    assert calls == ["write_scorecard", "compact_recent", "build_serving_files"]


def test_a_failing_compaction_does_not_stop_serving_but_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = build(tmp_path)

    def boom(*a: object, **k: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(serving, "compact_recent", boom)
    with pytest.raises(RuntimeError, match="compaction: OSError"):
        serving.serving_step(ctx(w, JobKind.EOD))
    assert w.store.exists("serving/latest_snapshot.parquet")
    assert w.store.exists("derived/scorecard_daily/date=2026-11-16.parquet")


def test_a_failing_scorecard_does_not_stop_serving_but_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = build(tmp_path)

    def boom(*a: object, **k: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(serving, "write_scorecard", boom)
    with pytest.raises(RuntimeError, match="scorecard: OSError"):
        serving.serving_step(ctx(w, JobKind.EOD))
    assert w.store.exists("serving/latest_snapshot.parquet")  # the dashboard still gets fresh files
    assert not w.store.exists("derived/scorecard_daily/date=2026-11-16.parquet")


def test_serving_is_wired_into_every_job_and_the_morning_report_is_kept() -> None:
    assert all(steps_for(j).build_serving is serving.serving_step for j in JobKind)
    morning = steps_for(JobKind.MORNING)
    assert morning.build_report.__name__ != "_noop_report"
    assert morning.run_checks is not None
