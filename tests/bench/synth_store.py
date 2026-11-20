"""Build a synthetic local store shaped like production after the 2-year history load (T56).

SYNTHETIC FIXTURE: every value, run and check result written here is made up (a deterministic
random walk per instrument). It never contains provider data. The store is only for the
dashboard benchmark (``bench_dashboard.py``) and the Phase 4C screenshots (P-022).

Shape (``--files``, default 2000, sets the number of ``observations`` files):

* one history file per weekday over two years (daily rows of every daily feed in
  ``config.yaml``; Yahoo history bars carry open, high, low, close and volume);
* the last 7 weekdays (``COMPACTED_DAYS``, eod's window, P-017) each get 40 intraday polls
  and are then compacted into ``run=compacted-{date}.parquet``, as production looks after eod;
* the remaining files are intraday polls on the 23 weekdays before those, each a file of Yahoo
  quote ticks (one tick per quoted instrument), spread evenly over those days;
* one ``runs`` row and two ``dq_results`` rows on each of the last 30 weekdays.

Command (one line, from the repo root; DIR must not exist yet or be empty)::

    .venv/bin/python tests/bench/synth_store.py DIR

It writes ``DIR/data`` (the store, through ``Datasets.append`` like the pipeline) and
``DIR/config.yaml`` (the repo's config with ``local_root`` pointed at ``DIR/data``).
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from deskbrief.config import Config, Instrument, load_config
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.schemas import to_frame
from deskbrief.store.compaction import compact_partition
from deskbrief.store.datasets import Datasets
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_to_utc

REPO = Path(__file__).resolve().parents[2]
#: last day of the synthetic store (a Thursday); the benchmark reads it at 21:00 ET
END = date(2026, 11, 19)
YEARS = 2
RECENT_WEEKDAYS = 30
COMPACTED_DAYS = 7
POLLS_PER_COMPACTED_DAY = 40
#: daily feeds written into the history files: (source, dataset) -> field
_DAILY_FIELD = {
    ("yahoo", "history"): "close",
    ("fred", "series"): "close",
    ("cboe", "vix_history"): "close",
    ("treasury", "par_curve"): "yield",
    ("ecb", "eurofxref"): "rate",
    ("stooq", "daily"): "close",
}


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _field(inst: Instrument, source: str, dataset: str) -> str:
    if inst.unit == "index" and source == "fred":
        return "level"
    if inst.unit == "pct" and source == "fred":
        return "rate"
    return _DAILY_FIELD[(source, dataset)]


def _level(inst: Instrument) -> float:
    return {"pct": 4.0, "fx_rate": 1.1, "index": 0.0, "contracts": 1e5}.get(inst.unit, 100.0)


class _Walk:
    """A deterministic random walk per instrument (seeded by its id)."""

    def __init__(self, config: Config) -> None:
        self._rng = random.Random(56)
        self.value = {i.id: _level(i) for i in config.instruments}

    def step(self, inst: Instrument) -> float:
        v = self.value[inst.id]
        scale = 0.02 if inst.unit in ("pct", "index") else max(abs(v), 1.0) * 0.01
        v = v + self._rng.gauss(0.0, scale)
        if inst.unit not in ("index", "contracts"):
            v = max(v, 0.01)
        self.value[inst.id] = v
        return v


def _obs(
    inst: Instrument,
    feed: Any,
    field: str,
    interval: str,
    as_of: datetime,
    ingested_at: datetime,
    value: float,
    run_id: str,
    n: int,
    unit: str | None = None,
) -> dict[str, Any]:
    return {
        "instrument_id": inst.id,
        "source": feed.source,
        "dataset": feed.dataset,
        "symbol": feed.symbol,
        "tier": feed.tier,
        "field": field,
        "interval": interval,
        "as_of": as_of,
        "value": value,
        "unit": unit or inst.unit,
        "is_derived": False,
        "is_fallback": False,
        "fetch_id": f"SYN-{run_id}-{n}",
        "run_id": run_id,
        "ingested_at": ingested_at,
        "parser_version": "synthetic-1",
    }


def _history_rows(config: Config, walk: _Walk, d: date, run_id: str) -> list[dict[str, Any]]:
    as_of, ingested = et_to_utc(d, "00:00"), et_to_utc(d, "20:00")
    rows: list[dict[str, Any]] = []
    for inst in config.instruments:
        for feed in inst.feeds:
            if (feed.source, feed.dataset) not in _DAILY_FIELD:
                continue
            close = walk.step(inst)
            field = _field(inst, feed.source, feed.dataset)
            rows.append(_obs(inst, feed, field, "1d", as_of, ingested, close, run_id, len(rows)))
            if (feed.source, feed.dataset) == ("yahoo", "history"):
                for f, v, unit in (
                    ("open", close * 0.998, None),
                    ("high", close * 1.006, None),
                    ("low", close * 0.994, None),
                    ("volume", 1e6, "shares"),
                ):
                    rows.append(
                        _obs(inst, feed, f, "1d", as_of, ingested, v, run_id, len(rows), unit)
                    )
    return rows


def _tick_rows(config: Config, walk: _Walk, at: datetime, run_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for inst in config.instruments:
        for feed in inst.feeds:
            if (feed.source, feed.dataset) == ("yahoo", "quote"):
                v = walk.step(inst)
                rows.append(
                    _obs(
                        inst,
                        feed,
                        "last",
                        "tick",
                        at,
                        at + timedelta(seconds=30),
                        v,
                        run_id,
                        len(rows),
                    )
                )
    return rows


def _ctx(config: Config, store: LocalBackend, run_id: str, target: date) -> RunContext:
    return RunContext(
        run_id=run_id,
        job=JobKind.HISTORY,
        trigger=Trigger.MANUAL,
        target_date=target,
        now_utc=datetime(2026, 11, 20, 2, 0, tzinfo=UTC),
        git_sha="synthetic",
        config_hash="synthetic",
        config=config,
        store=store,
    )


def _poll_times(days: list[date], polls: int) -> Iterator[tuple[date, datetime]]:
    """``polls`` intraday instants spread over ``days`` (09:30 to 16:00 ET)."""
    per_day = math.ceil(polls / len(days)) if days else 0
    left = polls
    for d in days:
        for k in range(min(per_day, left)):
            minute = 570 + (390 * k) // max(per_day, 1)
            yield d, et_to_utc(d, f"{minute // 60:02d}:{minute % 60:02d}")
        left -= min(per_day, left)


def build(out: Path, files: int = 2000) -> Path:
    """Write the store and its config under ``out``; return the config path."""
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise SystemExit(f"{out} is not empty")
    config_path = out / "config.yaml"
    text = (REPO / "config.yaml").read_text(encoding="utf-8")
    config_path.write_text(
        text.replace("local_root: ./.data", f"local_root: {out / 'data'}"), encoding="utf-8"
    )
    config = load_config(config_path)
    store = LocalBackend(out / "data")
    ds = Datasets(store)
    walk = _Walk(config)
    history = weekdays(END.replace(year=END.year - YEARS) + timedelta(days=1), END)
    for d in history:
        run_id = f"SYNHIST-{d.isoformat()}"
        rows = _history_rows(config, walk, d, run_id)
        ds.append("observations", to_frame("observations", rows), _ctx(config, store, run_id, d), d)
    recent = history[-RECENT_WEEKDAYS:]
    open_days, compacted = recent[:-COMPACTED_DAYS], recent[-COMPACTED_DAYS:]
    polls = list(_poll_times(open_days, max(files - len(history), 0)))
    polls += _poll_times(compacted, POLLS_PER_COMPACTED_DAY * len(compacted))
    for n, (d, at) in enumerate(polls):
        run_id = f"SYNPOLL-{n:05d}"
        rows = _tick_rows(config, walk, at, run_id)
        ds.append("observations", to_frame("observations", rows), _ctx(config, store, run_id, d), d)
    for d in compacted:
        compact_partition(store, "observations", d)
    for d in recent:
        run_id = f"SYNRUN-{d.isoformat()}"
        ctx = _ctx(config, store, run_id, d)
        ds.append("runs", to_frame("runs", [_run_row(run_id, d)]), ctx, d)
        ds.append("dq_results", to_frame("dq_results", _dq_rows(run_id, d)), ctx, d)
    return config_path


def _run_row(run_id: str, d: date) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "job": "eod",
        "trigger": "schedule",
        "target_date": d,
        "started_at": et_to_utc(d, "16:30"),
        "finished_at": et_to_utc(d, "16:40"),
        "status": "ok",
        "git_sha": "synthetic",
        "config_hash": "synthetic",
        "n_fetch": 1,
        "n_fetch_failed": 0,
        "n_obs": 1,
        "n_dq_fail": 0,
        "n_dq_warn": 0,
    }


def _dq_rows(run_id: str, d: date) -> list[dict[str, Any]]:
    return [
        {
            "result_id": f"SYNDQ-{d.isoformat()}-{name}",
            "run_id": run_id,
            "check_name": name,
            "check_type": "freshness",
            "source": None,
            "instrument_id": None,
            "target_date": d,
            "evaluated_at": et_to_utc(d, "16:40"),
            "status": "pass",
            "observed": 1.0,
            "threshold": 2.0,
            "details_json": "{}",
        }
        for name in ("synthetic_fresh_a", "synthetic_fresh_b")
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("out", type=Path, help="directory to create (must be empty)")
    parser.add_argument("--files", type=int, default=2000, help="observations files")
    args = parser.parse_args(argv)
    config_path = build(args.out.resolve(), args.files)
    print(config_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
