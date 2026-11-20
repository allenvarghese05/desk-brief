"""T48 end to end: Yahoo history makes ``close_vs_official`` and ``proxy_vs_exact`` compare.

The 2026-11-18 investigation (docs/investigations/2026-11-18-runs-and-xchecks.md §3) found these
12 keys skipping on every run, because the ETFs' only daily close was the disabled Stooq feed.
Here a realistic store is built through the real Yahoo parser: about three months of daily bars
from ``plan_range`` for the 12 ETFs/stocks and the four futures, Alpaca SIP last bars, and FRED
yields. Every one of the 12 keys must produce a real result, and the dashboard must draw SPY and
MSFT bars from it.

SYNTHETIC FIXTURE: all prices are invented; only the XNYS session dates are real. Reference
session Friday 2026-11-13, previous session Thursday 2026-11-12, run at Monday 07:00 ET.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from xc_fakes import NOW, duck_for, fred, make_ctx, sip_bar

from deskbrief.calendars import is_business_day
from deskbrief.config import Config
from deskbrief.crosschecks.base import run_crosschecks
from deskbrief.schemas import to_frame
from deskbrief.sources import yahoo
from deskbrief.sources.yahoo import YahooSource
from deskbrief.store.datasets import Datasets
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_date

REPO = Path(__file__).resolve().parents[3]

START, REF, PREV = date(2026, 8, 17), date(2026, 11, 13), date(2026, 11, 12)
LAST_BAR = datetime(2026, 11, 13, 20, 59, tzinfo=UTC)  # the 1m bar ending 16:00 ET
GROWTH = 0.0001  # every series rises 1 bp a session, so paired returns agree
BASE = {
    "SPY": 600.0, "QQQ": 500.0, "IWM": 220.0, "AAPL": 230.0, "MSFT": 420.0, "NVDA": 140.0,
    "USO": 75.0, "BNO": 28.0, "GLD": 240.0, "SHY": 82.0, "IEF": 95.0, "TLT": 90.0,
    "ES=F": 6000.0, "CL=F": 70.0, "BZ=F": 74.0, "GC=F": 2600.0,
}  # fmt: skip
SIP_SYMBOLS = ("SPY", "QQQ", "IWM", "GLD", "TLT")


def sessions(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if is_business_day("XNYS", d):
            days.append(d)
        d += timedelta(days=1)
    return days


DAYS = sessions(START, REF)


def close_on(symbol: str, d: date) -> float:
    return BASE[symbol] * (1 + GROWTH) ** DAYS.index(d)


class FakeYF:
    __version__ = "9.9.9"

    def __init__(self) -> None:
        self.config = SimpleNamespace(debug=SimpleNamespace(hide_exceptions=True))

    def Ticker(self, symbol: str) -> SimpleNamespace:  # noqa: N802 - mirrors yfinance
        def history(**_: Any) -> pd.DataFrame:
            if symbol not in BASE:
                return pd.DataFrame()
            close = [close_on(symbol, d) for d in DAYS]
            idx = pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York") for d in DAYS])
            return pd.DataFrame(
                {
                    "Open": close,
                    "High": [c * 1.005 for c in close],
                    "Low": [c * 0.995 for c in close],
                    "Close": close,
                    "Volume": [1e6] * len(close),
                },
                index=idx,
            )

        return SimpleNamespace(history=history, history_metadata={})


def append(store: LocalBackend, config: Config, rows: list[dict[str, Any]], run_id: str) -> None:
    by_date: dict[date, list[dict[str, Any]]] = {}
    for r in rows:
        by_date.setdefault(et_date(r["as_of"]), []).append(r)
    for d, part in by_date.items():
        Datasets(store).append(
            "observations", to_frame("observations", part),
            make_ctx(config, store, run_id=run_id), d,
        )  # fmt: skip


def yahoo_rows(config: Config, monkeypatch: pytest.MonkeyPatch, symbols: set[str]) -> list[dict]:  # type: ignore[type-arg]
    """Yahoo ``history`` rows for ``symbols`` through plan_range, fetch and the real parser."""
    monkeypatch.setattr(yahoo, "yf", FakeYF())
    src = YahooSource(config, sleep=lambda _: None, now=lambda: NOW)
    (req,) = src.plan_range(START, REF, make_ctx(config, LocalBackend("/nonexistent")))
    req = replace(req, symbols=tuple(s for s in req.symbols if s in symbols))
    raw = src.fetch(req, http=None)  # type: ignore[arg-type]
    assert raw.error is None
    parsed = src.parse(raw)
    extra = {"fetch_id": raw.fetch_id, "run_id": "HIST", "ingested_at": NOW, "parser_version": "1"}
    return [{**r, **extra} for r in parsed.to_dict("records")]


def other_rows() -> list[dict[str, Any]]:
    """SIP last bars equal to the Yahoo close, and flat FRED yields on both sessions."""
    rows = [sip_bar(s, LAST_BAR, close_on(s, REF)) for s in SIP_SYMBOLS]
    for inst, series in (("UST_2Y", "DGS2"), ("UST_10Y", "DGS10"), ("UST_20Y", "DGS20")):
        for d in (PREV, REF):
            day = datetime(d.year, d.month, d.day, 5, tzinfo=UTC)  # 00:00 ET
            rows.append(fred(inst, series, 4.0, as_of=day))
    return rows


def results(store: LocalBackend, config: Config) -> dict[tuple[str, str], dict[str, Any]]:
    ctx = make_ctx(config, store)
    xdf, _ = run_crosschecks(
        ctx, duck_for(store, config), only=["close_vs_official", "proxy_vs_exact"]
    )
    out = {}
    for rec in xdf.to_dict("records"):
        rec["details"] = json.loads(rec["details_json"])
        out[(rec["check_name"], rec["group_id"] or rec["left_instrument"])] = rec
    return out


@pytest.fixture
def full_store(
    store: LocalBackend, config: Config, monkeypatch: pytest.MonkeyPatch
) -> LocalBackend:
    append(store, config, yahoo_rows(config, monkeypatch, set(BASE)), "HIST")
    append(store, config, other_rows(), "RUN1")
    return store


def test_the_12_dead_keys_now_compare(full_store: LocalBackend, config: Config) -> None:
    out = results(full_store, config)
    close = {k[1]: v for k, v in out.items() if k[0] == "close_vs_official"}
    proxy = {k[1]: v for k, v in out.items() if k[0] == "proxy_vs_exact"}
    assert len(close) == 5
    assert len(proxy) == 7
    for key, rec in {**close, **proxy}.items():
        assert rec["status"] != "skip", (key, rec["details"].get("reason"))
        assert "reason" not in rec["details"], key
        assert rec["diff_bps"] is not None
    # close_vs_official: SIP last bar equals the Yahoo close -> 0 bp
    for rec in close.values():
        assert (rec["left_source"], rec["right_source"]) == ("alpaca", "yahoo")
        assert rec["status"] == "pass"
        assert rec["diff_bps"] == pytest.approx(0.0, abs=1e-9)
        assert rec["details"]["ref_date"] == "2026-11-13"
    # proxy_vs_exact: Yahoo on both legs for the return groups, Yahoo vs FRED for duration
    for gid in ("sp500", "oil_wti", "oil_brent", "gold"):
        assert (proxy[gid]["left_source"], proxy[gid]["right_source"]) == ("yahoo", "yahoo")
        assert proxy[gid]["details"]["provider"] == "same provider"
        assert proxy[gid]["status"] == "pass"
    for gid in ("ust_2y", "ust_10y", "ust_long"):
        assert (proxy[gid]["left_source"], proxy[gid]["right_source"]) == ("yahoo", "fred")
        assert proxy[gid]["details"]["provider"] == "different providers"
        assert proxy[gid]["diff_bps"] == pytest.approx(1.0, abs=1e-6)  # ETF +1 bp, yield flat
        assert proxy[gid]["status"] == "pass"


def test_without_yahoo_equity_history_the_same_keys_skip(
    store: LocalBackend, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the state the investigation found: futures history, SIP bars and yields, no ETF daily close
    futures = {"ES=F", "CL=F", "BZ=F", "GC=F"}
    append(store, config, yahoo_rows(config, monkeypatch, futures), "HIST")
    append(store, config, other_rows(), "RUN1")
    out = results(store, config)
    assert len(out) == 12
    assert {rec["status"] for rec in out.values()} == {"skip"}


@pytest.mark.parametrize("iid", ["SPY", "MSFT"])
def test_dashboard_draws_bars_from_yahoo_history(
    full_store: LocalBackend, config: Config, iid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(REPO))  # `app` is a plain folder, not an installed package
    from app.components.data import build_bars

    hosted = frozenset(config.dashboard.hosted_hidden_tiers)
    bars = build_bars(duck_for(full_store, config), NOW, iid, hosted)
    assert len(bars) == len(DAYS)  # every session in the loaded range, hosted mode included
    assert set(bars["source"]) == {"yahoo"}
    assert set(bars["tier"]) == {2}
    last = bars.iloc[-1]
    assert last["d"].date() == REF
    assert last["close"] == pytest.approx(close_on(iid, REF))
    assert last["high"] == pytest.approx(close_on(iid, REF) * 1.005)
    assert last["low"] == pytest.approx(close_on(iid, REF) * 0.995)


def test_hosted_mode_withholds_close_vs_official(
    full_store: LocalBackend, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``close_vs_official`` now has real results, and its left leg is the tier-1 SIP bar, so
    the hosted Cross-checks page (exposed_sources + keep_rows) must drop every such row."""
    monkeypatch.syspath_prepend(str(REPO))  # `app` is a plain folder, not an installed package
    from app.components import kit

    xdf, _ = run_crosschecks(
        make_ctx(config, full_store), duck_for(full_store, config),
        only=["close_vs_official", "proxy_vs_exact"],
    )  # fmt: skip
    real = xdf[(xdf["check_name"] == "close_vs_official") & (xdf["status"] != "skip")]
    assert len(real) == 5  # real results, not skips: the case the hosted filter must catch
    hosted = frozenset(config.dashboard.hosted_hidden_tiers)
    exposed = kit.exposed_sources(config, hosted)
    assert "alpaca" in exposed
    shown = kit.keep_rows(xdf, ("left_source", "right_source"), exposed)
    assert "close_vs_official" not in set(shown["check_name"])
    assert set(shown["check_name"]) == {"proxy_vs_exact"}  # yahoo/fred rows stay visible
    assert len(shown) == 7
    # the local (non-hosted) view keeps everything
    assert len(kit.keep_rows(xdf, ("left_source", "right_source"), frozenset())) == len(xdf)
