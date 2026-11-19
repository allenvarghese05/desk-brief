"""Yahoo daily history for ETFs and single stocks (card T48, P-019) and ``plan_range`` (P-020).

No network: ``yfinance`` is replaced by a fake. Bars are SYNTHETIC (invented prices on real XNYS
session dates); the frame layout (``Open``/``High``/``Low``/``Close``/``Adj Close``/``Volume``,
``America/New_York`` index) is the one the live probe saw on 2026-11-19 (docs/sources/yahoo.md).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from deskbrief.calendars import is_business_day
from deskbrief.config import Config, load_config
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.schemas import validate
from deskbrief.sources import yahoo
from deskbrief.sources.base import supports_range
from deskbrief.sources.yahoo import RANGE_DATASET, YahooSource

#: P-019: every ETF and single stock in config.yaml
EQUITIES = {"SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "USO", "BNO", "GLD", "SHY", "IEF", "TLT"}
START, END = date(2024, 11, 18), date(2026, 11, 18)  # the T53 2-year window
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)  # 07:00 ET, after the 18 Nov close


def sessions(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if is_business_day("XNYS", d):
            days.append(d)
        d += timedelta(days=1)
    return days


def frame(days: list[date], base: float) -> pd.DataFrame:
    """Synthetic daily bars; ``Adj Close`` differs from ``Close`` the way dividends make it."""
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York") for d in days])
    close = [base * (1 + 0.0001 * i) for i in range(len(days))]
    return pd.DataFrame(
        {
            "Open": close,
            "High": [c * 1.01 for c in close],
            "Low": [c * 0.99 for c in close],
            "Close": close,
            "Adj Close": [c * 0.98 for c in close],
            "Volume": [1e6] * len(days),
        },
        index=idx,
    )


class FakeYF:
    __version__ = "9.9.9"

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.config = SimpleNamespace(debug=SimpleNamespace(hide_exceptions=True))
        self.frames = frames
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def Ticker(self, symbol: str) -> SimpleNamespace:  # noqa: N802 - mirrors yfinance
        def history(**kwargs: Any) -> pd.DataFrame:
            self.calls.append((symbol, kwargs))
            f = self.frames.get(symbol, pd.DataFrame())
            if f.empty:
                return f
            lo, hi = pd.Timestamp(kwargs["start"]), pd.Timestamp(kwargs["end"])
            local = f.index.tz_localize(None)
            return f[(local >= lo) & (local < hi)]

        return SimpleNamespace(history=history, history_metadata={})


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config()


def ctx(config: Config) -> RunContext:
    return RunContext(
        run_id="r1", job=JobKind.HISTORY, trigger=Trigger.MANUAL, target_date=END, now_utc=NOW,
        git_sha="x", config_hash="y", config=config, store=None,  # type: ignore[arg-type]
    )  # fmt: skip


def make(config: Config) -> YahooSource:
    return YahooSource(config, sleep=lambda _: None, now=lambda: NOW)


# -- config (P-019) ------------------------------------------------------------------------------


def test_every_etf_and_stock_has_a_tier2_yahoo_history_feed(config: Config) -> None:
    equities = {i.id for i in config.instruments if i.kind in ("etf", "stock")}
    assert equities == EQUITIES
    for inst in config.instruments:
        if inst.id not in EQUITIES:
            continue
        feeds = [f for f in inst.feeds if (f.source, f.dataset) == ("yahoo", "history")]
        assert len(feeds) == 1, inst.id
        assert feeds[0].tier == 2
        assert feeds[0].symbol == inst.id  # Yahoo tickers are the plain symbol
        assert feeds[0].scale == 1.0


def test_daily_fallback_and_stooq_are_untouched(config: Config) -> None:
    assert config.feeds_for("yahoo", "daily_fallback") == []
    assert config.sources["stooq"].enabled is False
    assert {i.id for i, _ in config.feeds_for("stooq", "daily")} == EQUITIES


def test_alpaca_datasets_are_unchanged(config: Config) -> None:
    # P-003: no Alpaca daily bars
    datasets = {f.dataset for i in config.instruments for f in i.feeds if f.source == "alpaca"}
    assert datasets == {"iex_latest", "iex_bars_1m", "sip_bars_1m"}


# -- plan_range (P-020) ----------------------------------------------------------------------------


def test_yahoo_has_range_support(config: Config) -> None:
    assert supports_range(make(config))


def test_plan_range_is_one_history_request_for_every_history_symbol(config: Config) -> None:
    reqs = make(config).plan_range(START, END, ctx(config))
    assert len(reqs) == 1
    req = reqs[0]
    assert (req.source, req.dataset) == ("yahoo", RANGE_DATASET) == ("yahoo", "history")
    assert set(req.symbols) == set(config.feed_index("yahoo", "history"))
    assert set(req.symbols) >= EQUITIES
    assert req.params == {"start": "2024-11-18", "end": "2026-11-19"}  # end is exclusive
    assert req.target_date == END


def test_plan_range_single_day(config: Config) -> None:
    (req,) = make(config).plan_range(END, END, ctx(config))
    assert req.params == {"start": "2026-11-18", "end": "2026-11-19"}


def test_plan_range_rejects_end_before_start(config: Config) -> None:
    with pytest.raises(ValueError, match="end must not be before start"):
        make(config).plan_range(END, START, ctx(config))


def test_plan_range_without_history_feeds_plans_nothing(config: Config) -> None:
    insts = tuple(
        i.model_copy(update={"feeds": tuple(f for f in i.feeds if f.dataset != "history")})
        for i in config.instruments
    )
    cfg = config.model_copy(update={"instruments": insts})
    assert make(cfg).plan_range(START, END, ctx(cfg)) == []


def test_plan_range_leaves_daily_plans_alone(config: Config) -> None:
    src = make(config)
    daily = src.plan(JobKind.EOD, replace(ctx(config), job=JobKind.EOD))
    hist = next(r for r in daily if r.dataset == "history")
    assert hist.params == {"start": "2026-11-08", "end": "2026-11-19"}  # 10-day lookback


def test_two_year_range_fetches_once_per_symbol_and_parses_every_session(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    days = sessions(START, END)
    fake = FakeYF({s: frame(days, 100.0 + n) for n, s in enumerate(sorted(EQUITIES))})
    monkeypatch.setattr(yahoo, "yf", fake)
    src = make(config)
    (req,) = src.plan_range(START, END, ctx(config))
    eq_req = replace(req, symbols=tuple(sorted(EQUITIES)))
    raw = src.fetch(eq_req, http=None)  # type: ignore[arg-type]
    assert raw.error is None
    assert raw.http_status == 200
    assert len(fake.calls) == len(EQUITIES)  # one call per symbol for the whole range
    assert {c[1]["start"] for c in fake.calls} == {"2024-11-18"}
    assert {c[1]["end"] for c in fake.calls} == {"2026-11-19"}
    assert all(c[1]["auto_adjust"] is False for c in fake.calls)

    df = src.parse(raw)
    validate("parsed_observations", df)
    assert len(days) > 480  # about two years of XNYS sessions
    assert len(df) == len(EQUITIES) * len(days) * 4  # open, high, low, close per session
    assert set(df["tier"]) == {2}
    assert not df["is_fallback"].any()
    assert set(df["interval"]) == {"1d"}
    assert set(df["dataset"]) == {"history"}
    spy = df[(df["instrument_id"] == "SPY") & (df["field"] == "close")].sort_values("as_of")
    et_times = (pd.Timestamp(t).tz_convert("America/New_York") for t in spy["as_of"])
    months = {t.strftime("%Y-%m") for t in et_times}
    assert len(months) == 25  # Nov 2024 through Nov 2026
    # the unadjusted Close is stored, not Adj Close
    base = 100.0 + sorted(EQUITIES).index("SPY")
    assert spy["value"].iloc[-1] == pytest.approx(base * (1 + 0.0001 * (len(days) - 1)))
    # raw keeps what came back, with the version
    body = json.loads(raw.body)
    assert body["yfinance_version"] == "9.9.9"
    assert len(body["symbols"]["SPY"]["bars"]) == len(days)
