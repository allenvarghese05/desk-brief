"""Add a P-024 morning to a synthetic store, for the Phase 4E screenshots (cards T62-T67).

SYNTHETIC FIXTURE: every value written here is made up. It runs only on a store built by
``synth_store.py`` (whose own values are a made-up random walk) and adds, for the brief of
Friday 20 Nov 2026 at 07:00 ET (the day after that store's last day):

* Friday-morning Yahoo quotes that move more than usual (S&P futures down, VIX up, Brent far
  from WTI) and official Thursday values for the high-yield spread and the 2-year (a later
  print of the same day replaces the random walk's), each sized in sigma against its own
  series' last 30 daily moves;
* a fictional single stock, "Example Corp (EXMP)", with a price history, a Friday quote and
  invented SEC filings (a 10-Q, Form 4 trades, 13F holder changes) under the invented CIK
  0009999999;
* Friday's scheduled releases and a Treasury auction in ``events`` (titles are generic names,
  times invented), with the fetches that wrote them;
* an archived brief whose stored note was rendered by the note templates from a synthetic
  reply (the picks a model would return; no API call).

Command (one line, from the repo root, after ``synth_store.py DIR``)::

    .venv/bin/python tests/bench/p024_demo.py DIR

It edits ``DIR/config.yaml`` (adds EXMP and the filings settings) and writes into ``DIR/data``.
The frozen clock for the screenshots is :data:`NOW`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from deskbrief.checks.anomaly import daily_moves
from deskbrief.config import load_config
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.query import overnight_changes, series_by_feed, series_key
from deskbrief.report.model import BriefModel, Note
from deskbrief.report.note import allowed_contrasts, hidden_tiers, markets, render
from deskbrief.schemas import to_frame
from deskbrief.store import duck
from deskbrief.store.backend import report_key
from deskbrief.store.datasets import Datasets
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_date, et_to_utc

THU, FRI = date(2026, 11, 19), date(2026, 11, 20)
#: the screenshots' frozen clock: Friday 20 Nov 2026, 07:00 ET
NOW = et_to_utc(FRI, "07:00")
EXMP_CIK = "0009999999"
EXMP_URL = "https://www.sec.gov/Archives/edgar/data/9999999/000999999926000041/"
EXMP_BLOCK = """
  - id: EXMP
    name: Example Corp (fictional)
    asset_class: equity
    kind: stock
    unit: USD
    feeds:
      - {source: yahoo, dataset: quote, symbol: EXMP, tier: 2}
      - {source: yahoo, dataset: history, symbol: EXMP, tier: 2}
"""
#: (instrument, field kind, target sigma, direction): the Friday moves
MOVES = (
    ("ES_F", "quote", 3.6, -1),
    ("VIX", "quote", 3.2, +1),
    ("BRENT", "quote", 4.4, -1),
    ("WTI", "quote", 0.4, -1),
    ("EXMP", "quote", 3.3, -1),
    ("HY_OAS", "official", 3.8, +1),
    ("UST_2Y", "official", 1.6, -1),
)


def _ctx(config: Any, store: LocalBackend, run_id: str, d: date) -> RunContext:
    return RunContext(
        run_id=run_id, job=JobKind.MORNING, trigger=Trigger.MANUAL, target_date=d,
        now_utc=NOW, git_sha="synthetic", config_hash="synthetic", config=config, store=store,
    )  # fmt: skip


def _obs(iid: str, source: str, dataset: str, symbol: str, tier: int, field: str,
         interval: str, as_of: datetime, value: float, unit: str, ingested: datetime,
         n: int) -> dict[str, Any]:  # fmt: skip
    return {
        "instrument_id": iid, "source": source, "dataset": dataset, "symbol": symbol,
        "tier": tier, "field": field, "interval": interval, "as_of": as_of, "value": value,
        "unit": unit, "is_derived": False, "is_fallback": False, "fetch_id": f"P024-{n}",
        "run_id": "P024DEMO", "ingested_at": ingested, "parser_version": "synthetic-1",
    }  # fmt: skip


def _sized(values: list[float], unit: str, z: float) -> float:
    """The size of a move ``z`` sigma above the mean of the series' last 30 |daily moves|."""
    moves = daily_moves(np.array(values, dtype=float), unit)[-30:]
    return float(moves.mean() + z * moves.std(ddof=1))


def _exmp_history(config: Any, store: LocalBackend) -> None:
    """A made-up random walk for EXMP over the store's weekdays."""
    ds, rng, v = Datasets(store), np.random.default_rng(67), 140.0
    d = date(2025, 6, 2)
    while d <= THU:
        if d.weekday() < 5:
            v *= 1 + rng.normal(0.0, 0.012)
            ohlc = (("close", v), ("open", v * 0.998), ("high", v * 1.008), ("low", v * 0.992))
            rows = [
                _obs("EXMP", "yahoo", "history", "EXMP", 2, f, "1d", et_to_utc(d, "00:00"), x,
                     "USD", et_to_utc(d, "20:00"), i)
                for i, (f, x) in enumerate(ohlc)
            ]  # fmt: skip
            run = f"P024EXMP-{d.isoformat()}"
            ds.append("observations", to_frame("observations", rows),
                      _ctx(config, store, run, d), d)  # fmt: skip
        d += timedelta(days=1)


def _baselines(config: Any, store: LocalBackend) -> dict[str, Any]:
    """The baseline the brief compares each market with (a Friday quote's previous close)."""
    conn = duck.connect(store, config)
    probe = NOW - timedelta(minutes=30)  # before any Friday quote this script writes
    out: dict[str, Any] = {}
    for c in overnight_changes(conn, FRI, probe):
        if c.last.kind == "fx" or c.status == "no_move_yet":
            out[c.last.instrument_id] = c.last  # FX: its own last quote; else Thursday's close
        elif c.previous is not None:
            out[c.last.instrument_id] = c.previous
    conn.close()
    return out


def _moves(config: Any, store: LocalBackend) -> None:
    conn = duck.connect(store, config)
    feeds = series_by_feed(conn, 60, NOW)
    conn.close()
    bases = _baselines(config, store)
    rows: list[dict[str, Any]] = []
    for iid, kind, z, sign in MOVES:
        if kind == "quote":
            base = bases.get(iid)
            if base is None:
                continue
            hist = [r.value for r in feeds.get(series_key(base), ())] or [base.value]
            size = _sized(hist, base.unit, z) if len(hist) > 2 else 0.01 * z
            value = base.value * (1 + sign * size)
            rows.append(_obs(iid, "yahoo", "quote", iid, 2, "last", "tick",
                             et_to_utc(FRI, "06:52"), value, base.unit, et_to_utc(FRI, "06:53"),
                             len(rows)))  # fmt: skip
        else:
            src = ("fred", "series") if iid == "HY_OAS" else ("treasury", "par_curve")
            key = next(k for k in feeds if k[0] == iid and k[1:3] == src)
            series = feeds[key]
            hist = [r.value for r in series][:-1]  # up to Wednesday
            size = _sized(hist, "pct", z) / 100.0
            last = series[-1]
            rows.append(_obs(iid, last.source, last.dataset, last.symbol, last.tier, last.field,
                             "1d", et_to_utc(THU, "00:00"), hist[-1] + sign * size, "pct",
                             et_to_utc(THU, "21:00"), len(rows)))  # fmt: skip
    ds = Datasets(store)
    for d in (THU, FRI):
        part = [r for r in rows if et_date(r["as_of"]) == d]
        if part:
            ds.append("observations", to_frame("observations", part),
                      _ctx(config, store, f"P024MOVES-{d}", d), d)  # fmt: skip


def _quiet(config: Any, store: LocalBackend) -> None:
    """A small Friday move for every other quoted market, off the baseline the brief uses, so
    only the planned moves rank."""
    conn = duck.connect(store, config)
    feeds = series_by_feed(conn, 60, NOW)
    conn.close()
    bases = _baselines(config, store)
    planned = {m[0] for m in MOVES}
    rng = np.random.default_rng(24)
    rows: list[dict[str, Any]] = []
    for inst in config.instruments:
        quote = next((f for f in inst.feeds if (f.source, f.dataset) == ("yahoo", "quote")), None)
        base = bases.get(inst.id)
        if quote is None or base is None or inst.id in planned:
            continue
        hist = [r.value for r in feeds.get(series_key(base), ())]
        unit = base.unit
        size = 0.3 * (_sized(hist, unit, 0.0) if len(hist) > 2 else 0.002)
        size *= float(rng.choice((-1.0, 1.0)))
        value = base.value + size / 100.0 if unit == "pct" else base.value * (1 + size)
        rows.append(_obs(inst.id, "yahoo", "quote", quote.symbol, 2, "last", "tick",
                         et_to_utc(FRI, "06:50"), value, unit, et_to_utc(FRI, "06:51"),
                         1000 + len(rows)))  # fmt: skip
    Datasets(store).append("observations", to_frame("observations", rows),
                           _ctx(config, store, "P024QUIET", FRI), FRI)  # fmt: skip


def _events(config: Any, store: LocalBackend) -> None:
    ingested = et_to_utc(FRI, "06:40")
    plan = [  # (source, dataset, id, kind, title, time ET)
        ("fred", "release_dates", "8", "release", "Durable goods orders", "08:30"),
        ("fred", "release_dates", "11", "release", "Consumer confidence", "10:00"),
        ("fiscaldata", "upcoming_auctions", "7y", "auction", "7-year note auction", "13:00"),
    ]
    events, log = [], []
    for n, (source, dataset, eid, kind, title, hhmm) in enumerate(plan):
        fetch = f"P024EV-{n}"
        events.append({
            "event_id": f"{eid}:{FRI.isoformat()}", "source": source, "dataset": dataset,
            "kind": kind, "title": title, "event_date": FRI,
            "scheduled_at": et_to_utc(FRI, hhmm), "period": None, "url": None,
            "details_json": "{}", "fetch_id": fetch, "run_id": "P024DEMO",
            "ingested_at": ingested,
        })  # fmt: skip
        log.append({
            "fetch_id": fetch, "run_id": "P024DEMO", "source": source, "dataset": dataset,
            "symbols": eid, "request_redacted": "GET /synthetic", "requested_at": ingested,
            "responded_at": ingested, "latency_ms": 100, "http_status": 200, "status": "ok",
            "bytes": 10, "rows_parsed": 1, "rows_quarantined": 0, "attempt": 1,
        })  # fmt: skip
    ds, ctx = Datasets(store), _ctx(config, store, "P024DEMO", FRI)
    ds.append("events", to_frame("events", events), ctx, FRI)
    ds.append("source_log", to_frame("source_log", log), ctx, FRI)


def _filings(config: Any, store: LocalBackend) -> None:
    base = {"instrument_id": "EXMP", "cik": EXMP_CIK, "url": EXMP_URL, "plan_10b5_1": False,
            "details_json": "{}", "source": "edgar", "fetch_id": "P024FIL", "run_id": "P024DEMO",
            "ingested_at": et_to_utc(FRI, "06:45")}  # fmt: skip
    rows = [{**base, "accession": "0009999999-26-000041", "seq": 0, "row_kind": "filing",
             "form": "10-Q", "filed_date": date(2026, 11, 4), "period": date(2026, 9, 30),
             "dataset": "submissions"}]  # fmt: skip
    trades = [  # (n, side, shares, price, under a 10b5-1 plan)
        (1, "sell", 12000.0, 151.20, True), (2, "sell", 8000.0, 149.80, True),
        (3, "sell", 5000.0, 147.10, False), (4, "sell", 15000.0, 152.40, True),
        (5, "buy", 2000.0, 139.90, False),
    ]  # fmt: skip
    for n, side, shares, price, plan in trades:
        rows.append({**base, "accession": f"0009999999-26-00005{n}", "seq": 1,
                     "row_kind": "insider_trade", "form": "4",
                     "filed_date": date(2026, 9, 1) + timedelta(days=12 * n), "period": None,
                     "party": f"Insider {chr(64 + n)} Example", "party_cik": f"000888888{n}",
                     "party_role": "Officer", "trade_date": date(2026, 8, 28)
                     + timedelta(days=12 * n), "trade_code": "S" if side == "sell" else "P",
                     "side": side, "shares": shares, "price": price,
                     "value_usd": shares * price, "plan_10b5_1": plan,
                     "dataset": "form4"})  # fmt: skip
    holders = [("Invented Asset Management", "0007777771", 410000.0),
               ("Example Pension Fund", "0007777772", -125000.0),
               ("Placeholder Capital", "0007777773", 98000.0),
               ("Sample Index Trust", "0007777774", -40000.0)]  # fmt: skip
    for name, cik, change in holders:
        rows.append({**base, "cik": EXMP_CIK, "accession": f"{cik}-26-000003", "seq": 1,
                     "row_kind": "holder_change", "form": "13F-HR",
                     "filed_date": date(2026, 11, 13), "period": date(2026, 9, 30),
                     "party": name, "party_cik": cik, "shares": 2_000_000.0 + change,
                     "shares_change": change, "dataset": "13f"})  # fmt: skip
    Datasets(store).append("filings", to_frame("filings", rows),
                           _ctx(config, store, "P024DEMO", FRI), FRI)  # fmt: skip


def _note(config: Any, store: LocalBackend) -> None:
    """Archive a brief whose note was rendered by the templates from a synthetic reply (the
    picks a model would return), so no API is called."""
    conn = duck.connect(store, config)
    model = BriefModel.from_store(conn, config, FRI, NOW)
    conn.close()
    hidden = hidden_tiers(config)
    reply = {
        "items": [
            {"market_id": "HY_OAS", "show": "move_and_sigma"},
            {"market_id": "ES_F", "show": "move_and_level"},
            {"market_id": "VIX", "show": "move"},
        ],
        "contrasts": [{"a": "BRENT", "b": "WTI"}],
    }
    s = config.brief.note
    out = render(reply, markets(model, hidden), allowed_contrasts(model, config), s.max_sentences)
    note = Note(out.sentences, out.n_numbers, out.n_numbers, model=f"{s.model} (synthetic)")
    body = replace(model, note=note).to_json().encode()
    store.put_bytes(report_key(FRI, 1, "model.json"), body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("dir", type=Path, help="a store built by synth_store.py")
    args = parser.parse_args(argv)
    cfg_path = args.dir.resolve() / "config.yaml"
    text = cfg_path.read_text(encoding="utf-8")
    if "id: EXMP" not in text:
        text = text.replace("\ngroups:", EXMP_BLOCK.rstrip("\n") + "\n\ngroups:", 1)
        text += f'\nfilings:\n  companies:\n    - {{instrument: EXMP, cik: "{EXMP_CIK}"}}\n'
        cfg_path.write_text(text, encoding="utf-8")
    config = load_config(cfg_path)
    store = LocalBackend(Path(config.storage.local_root))
    _exmp_history(config, store)
    _moves(config, store)
    _quiet(config, store)
    _events(config, store)
    _filings(config, store)
    _note(config, store)
    print(json.dumps({"config": str(cfg_path), "now": NOW.isoformat()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
