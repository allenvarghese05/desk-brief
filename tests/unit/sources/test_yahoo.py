"""Yahoo source tests (card T16). yfinance is replaced by a fake; no network.

Fixtures under ``tests/fixtures/synthetic/yahoo`` are SYNTHETIC (invented values).
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from deskbrief.config import Config, Feed, load_config
from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse, RunContext, Trigger
from deskbrief.schemas import validate
from deskbrief.sources import registry, yahoo
from deskbrief.sources.base import ParseError, Source
from deskbrief.sources.yahoo import YahooSource

FIXTURES = Path(__file__).parents[2] / "fixtures" / "synthetic" / "yahoo"
T0 = datetime(2026, 11, 16, 11, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# fake yfinance
# ---------------------------------------------------------------------------------------------


def bars_frame(rows: list[tuple[str, float, float, float, float]], tz: str) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=tz) for d, *_ in rows])
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Volume": [0.0] * len(rows),
        },
        index=idx,
    )


class FakeYF:
    """Stand-in for the ``yfinance`` module: ``Ticker`` + ``__version__``."""

    __version__ = "9.9.9"

    def __init__(self) -> None:
        # real yfinance 1.8 defaults to hide_exceptions=True: errors come back as empty frames
        self.config = SimpleNamespace(debug=SimpleNamespace(hide_exceptions=True))
        self.hide_seen: list[bool] = []
        self.frames: dict[str, pd.DataFrame] = {}
        self.meta: dict[str, dict[str, Any]] = {}
        self.raises: dict[str, Exception] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def Ticker(self, symbol: str) -> SimpleNamespace:  # noqa: N802 - mirrors yfinance
        outer = self

        def history(**kwargs: Any) -> pd.DataFrame:
            outer.calls.append((symbol, kwargs))
            outer.hide_seen.append(outer.config.debug.hide_exceptions)
            if symbol in outer.raises:
                if outer.config.debug.hide_exceptions:
                    return pd.DataFrame()  # what real yfinance does: swallow and log
                raise outer.raises[symbol]
            return outer.frames.get(symbol, pd.DataFrame())

        return SimpleNamespace(history=history, history_metadata=self.meta.get(symbol, {}))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeYF:
    f = FakeYF()
    monkeypatch.setattr(yahoo, "yf", f)
    return f


class Clock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def config() -> Config:
    return load_config()


def with_feeds(config: Config, instrument: str, feeds: tuple[Feed, ...]) -> Config:
    insts = tuple(
        i.model_copy(update={"feeds": feeds}) if i.id == instrument else i
        for i in config.instruments
    )
    return config.model_copy(update={"instruments": insts})


def make(config: Config, now: datetime = T0) -> YahooSource:
    return YahooSource(config, sleep=lambda _: None, now=Clock(now))


def ctx(config: Config, job: JobKind = JobKind.MORNING) -> RunContext:
    return RunContext(
        run_id="r1",
        job=job,
        trigger=Trigger.MANUAL,
        target_date=date(2026, 11, 16),
        now_utc=T0,
        git_sha="x",
        config_hash="y",
        config=config,
        store=None,  # type: ignore[arg-type]
    )


def raw_from(
    dataset: str,
    payload: dict[str, Any],
    symbols: tuple[str, ...] = (),
    at: datetime = T0,
) -> RawResponse:
    return RawResponse(
        request=FetchRequest(source="yahoo", dataset=dataset, symbols=symbols),
        fetch_id="f1",
        requested_at=at,
        responded_at=at,
        http_status=200,
        content_type="application/json",
        body=json.dumps(payload).encode(),
    )


def fixture_raw(dataset: str) -> RawResponse:
    payload = json.loads((FIXTURES / f"{dataset}.json").read_text())
    return raw_from(dataset, payload)


def row(df: pd.DataFrame, instrument: str, field: str) -> pd.Series:
    sel = df[(df["instrument_id"] == instrument) & (df["field"] == field)]
    assert len(sel) >= 1, f"no {instrument}/{field} row"
    return sel.iloc[0]


# ---------------------------------------------------------------------------------------------
# registry / protocol
# ---------------------------------------------------------------------------------------------


def test_is_a_source_and_registers(config: Config) -> None:
    src = YahooSource(config)
    assert isinstance(src, Source)
    assert src.name == "yahoo"


def test_module_registers_itself_on_import(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_FACTORIES", {})
    assert "yahoo" not in registry.registered()
    importlib.reload(yahoo)  # re-runs the module-level registration against the empty map
    assert "yahoo" in registry.registered()
    assert [s.name for s in registry.get_sources(config)] == ["yahoo"]


# ---------------------------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------------------------


def test_plan_morning_has_quote_and_history_with_configured_symbols(config: Config) -> None:
    reqs = make(config).plan(JobKind.MORNING, ctx(config))
    by_ds = {r.dataset: r for r in reqs}
    assert set(by_ds) == {"quote", "history"}  # no daily_fallback feeds in config
    assert set(by_ds["quote"].symbols) == set(config.feed_index("yahoo", "quote"))
    assert set(by_ds["history"].symbols) == set(config.feed_index("yahoo", "history"))
    assert "^TNX" in by_ds["history"].symbols
    assert by_ds["history"].params["start"] == "2026-11-06"
    assert by_ds["history"].params["end"] == "2026-11-17"  # exclusive: includes the target date
    assert all(r.source == "yahoo" and r.target_date == date(2026, 11, 16) for r in reqs)


def test_plan_intraday_is_quote_only(config: Config) -> None:
    reqs = make(config).plan(JobKind.INTRADAY, ctx(config, JobKind.INTRADAY))
    assert [r.dataset for r in reqs] == ["quote"]


def test_plan_eod_and_backfill_include_history(config: Config) -> None:
    assert {r.dataset for r in make(config).plan(JobKind.EOD, ctx(config))} == {
        "quote",
        "history",
    }
    assert [r.dataset for r in make(config).plan(JobKind.BACKFILL, ctx(config))] == ["history"]


def test_plan_daily_fallback_only_when_configured(config: Config) -> None:
    cfg = with_feeds(
        config,
        "AAPL",
        (
            Feed(source="yahoo", dataset="daily_fallback", symbol="AAPL", tier=3),
            Feed(source="alpaca", dataset="iex_latest", symbol="AAPL", tier=1),
        ),
    )
    reqs = make(cfg).plan(JobKind.EOD, ctx(cfg))
    fb = [r for r in reqs if r.dataset == "daily_fallback"]
    assert len(fb) == 1
    assert fb[0].symbols == ("AAPL",)
    assert "daily_fallback" in {r.dataset for r in make(cfg).plan(JobKind.MORNING, ctx(cfg))}
    assert "daily_fallback" not in {r.dataset for r in make(cfg).plan(JobKind.INTRADAY, ctx(cfg))}


# ---------------------------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------------------------


def quote_req(*symbols: str) -> FetchRequest:
    return FetchRequest(source="yahoo", dataset="quote", symbols=symbols)


def history_req(*symbols: str, dataset: str = "history") -> FetchRequest:
    return FetchRequest(
        source="yahoo",
        dataset=dataset,
        symbols=symbols,
        params={"start": "2026-11-06", "end": "2026-11-17"},
    )


def test_fetch_quote_serializes_price_time_and_version(config: Config, fake: FakeYF) -> None:
    fake.meta["^TNX"] = {
        "regularMarketPrice": 4.25,
        "regularMarketTime": pd.Timestamp("2026-11-13 13:59:54", tz="America/Chicago"),
        "currency": "USD",
    }
    raw = make(config).fetch(quote_req("^TNX"), http=None)  # type: ignore[arg-type]
    assert raw.http_status == 200
    assert raw.error is None
    assert raw.fetch_status is FetchStatus.OK
    assert raw.url_redacted == "yfinance://quote"
    body = json.loads(raw.body)
    assert body["yfinance_version"] == "9.9.9"
    assert body["symbols"]["^TNX"]["price"] == 4.25
    assert body["symbols"]["^TNX"]["time"] == "2026-11-13T19:59:54+00:00"
    assert raw.latency_ms >= 0


def test_fetch_quote_accepts_epoch_seconds(config: Config, fake: FakeYF) -> None:
    fake.meta["SPY"] = {"regularMarketPrice": 500.0, "regularMarketTime": 1_794_000_000}
    raw = make(config).fetch(quote_req("SPY"), http=None)  # type: ignore[arg-type]
    t = json.loads(raw.body)["symbols"]["SPY"]["time"]
    assert t == datetime.fromtimestamp(1_794_000_000, UTC).isoformat()


def test_fetch_history_serializes_bars_by_exchange_local_date(config: Config, fake: FakeYF) -> None:
    fake.frames["^TNX"] = bars_frame(
        [("2026-11-12", 4.1, 4.2, 4.0, 4.15), ("2026-11-13", 4.15, 4.3, 4.1, 4.25)],
        "America/Chicago",
    )
    raw = make(config).fetch(history_req("^TNX"), http=None)  # type: ignore[arg-type]
    bars = json.loads(raw.body)["symbols"]["^TNX"]["bars"]
    assert [b["date"] for b in bars] == ["2026-11-12", "2026-11-13"]
    assert bars[1]["close"] == 4.25
    (_, kwargs) = fake.calls[0]
    assert kwargs["interval"] == "1d"
    assert kwargs["start"] == "2026-11-06"
    assert kwargs["end"] == "2026-11-17"
    assert kwargs["auto_adjust"] is False


def test_fetch_respects_rate_limit_between_symbols(config: Config, fake: FakeYF) -> None:
    for s in ("^TNX", "SPY"):
        fake.meta[s] = {"regularMarketPrice": 1.0, "regularMarketTime": 1_794_000_000}
    sleeps: list[float] = []
    src = YahooSource(config, sleep=sleeps.append, now=Clock(), monotonic=lambda: 0.0)
    src.fetch(quote_req("^TNX", "SPY", "QQQ"), http=None)  # type: ignore[arg-type]
    # 30/min, burst 1, frozen clock: first free, then 2 s, then 4 s
    assert sleeps == [pytest.approx(2.0), pytest.approx(4.0)]


SUNDAY = datetime(2026, 11, 15, 15, 0, tzinfo=UTC)


def test_fetch_all_symbols_empty_on_non_business_day_is_status_empty(
    config: Config, fake: FakeYF
) -> None:
    raw = make(config, SUNDAY).fetch(history_req("SPY", "QQQ"), http=None)  # type: ignore[arg-type]
    assert raw.error is None
    assert raw.status is FetchStatus.EMPTY
    assert raw.fetch_status is FetchStatus.EMPTY
    assert raw.ok


def test_fetch_all_symbols_empty_on_xnys_business_day_is_error(
    config: Config, fake: FakeYF
) -> None:
    raw = make(config, T0).fetch(history_req("SPY", "QQQ"), http=None)  # type: ignore[arg-type]
    assert raw.fetch_status is FetchStatus.ERROR
    assert raw.error is not None
    assert "no data on a trading day" in raw.error
    assert not raw.ok


def test_fetch_makes_yfinance_raise_and_restores_the_setting(config: Config, fake: FakeYF) -> None:
    fake.raises["SPY"] = RuntimeError("rate limited")  # swallowed by real yfinance by default
    raw = make(config).fetch(quote_req("SPY"), http=None)  # type: ignore[arg-type]
    assert fake.hide_seen == [False]  # hide_exceptions was off during the call
    assert fake.config.debug.hide_exceptions is True  # restored
    assert raw.fetch_status is FetchStatus.ERROR
    assert raw.error is not None
    assert "rate limited" in raw.error


def test_fetch_restores_hide_exceptions_even_when_collect_fails(
    config: Config, fake: FakeYF
) -> None:
    src = make(config)

    def boom() -> float:
        raise KeyboardInterrupt("stop")  # not an Exception: escapes the per-symbol handler

    src._bucket.acquire = boom  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        src.fetch(quote_req("SPY"), http=None)  # type: ignore[arg-type]
    assert fake.config.debug.hide_exceptions is True


def test_fetch_per_symbol_error_is_redacted_in_body(config: Config, fake: FakeYF) -> None:
    secret = "".join(["Qw", "7z", "Lp", "93", "Xk"])
    fake.raises["SPY"] = RuntimeError(f"GET https://q.example/v1?crumb=1&api_key={secret}")
    fake.meta["QQQ"] = {"regularMarketPrice": 1.0, "regularMarketTime": 1_794_000_000}
    raw = make(config).fetch(quote_req("SPY", "QQQ"), http=None)  # type: ignore[arg-type]
    assert secret.encode() not in raw.body
    assert "RuntimeError" in json.loads(raw.body)["symbols"]["SPY"]["error"]


def test_fetch_non_native_currency_does_not_fail_the_dataset(config: Config, fake: FakeYF) -> None:
    fake.meta["SPY"] = {
        "regularMarketPrice": 500.0,
        "regularMarketTime": 1_794_000_000,
        "currency": object(),
    }
    fake.meta["QQQ"] = {"regularMarketPrice": 400.0, "regularMarketTime": 1_794_000_000}
    raw = make(config).fetch(quote_req("SPY", "QQQ"), http=None)  # type: ignore[arg-type]
    assert raw.fetch_status is FetchStatus.OK
    body = json.loads(raw.body)["symbols"]
    assert body["SPY"]["price"] == 500.0
    assert isinstance(body["SPY"]["currency"], str)
    assert body["QQQ"]["currency"] is None


def test_fetch_exception_on_every_symbol_is_error_response(config: Config, fake: FakeYF) -> None:
    secret = "".join(["zq", "Xv", "81", "mK", "p2"])  # built at runtime: keeps scanners quiet
    fake.raises["SPY"] = RuntimeError(f"Too Many Requests. Rate limited. api_key={secret}")
    raw = make(config).fetch(quote_req("SPY"), http=None)  # type: ignore[arg-type]
    assert raw.fetch_status is FetchStatus.ERROR
    assert raw.http_status is None
    assert raw.error is not None
    assert "RuntimeError" in raw.error
    assert "Too Many Requests" in raw.error
    assert secret not in raw.error  # redacted
    assert raw.attempts == 1


def test_fetch_partial_failure_keeps_good_symbols(config: Config, fake: FakeYF) -> None:
    fake.meta["SPY"] = {"regularMarketPrice": 500.0, "regularMarketTime": 1_794_000_000}
    fake.raises["QQQ"] = ValueError("boom")
    raw = make(config).fetch(quote_req("SPY", "QQQ"), http=None)  # type: ignore[arg-type]
    assert raw.fetch_status is FetchStatus.OK
    body = json.loads(raw.body)
    assert body["symbols"]["SPY"]["price"] == 500.0
    assert "ValueError: boom" in body["symbols"]["QQQ"]["error"]


def test_fetch_unexpected_failure_outside_symbol_loop_never_raises(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        config = SimpleNamespace(debug=SimpleNamespace(hide_exceptions=True))

        @property
        def __version__(self) -> str:
            raise RuntimeError("no version")

    monkeypatch.setattr(yahoo, "yf", Broken())
    raw = make(config).fetch(quote_req("SPY"), http=None)  # type: ignore[arg-type]
    assert raw.fetch_status is FetchStatus.ERROR
    assert raw.error is not None
    assert "no version" in raw.error


def test_fetch_unsupported_dataset_is_error(config: Config, fake: FakeYF) -> None:
    req = FetchRequest(source="yahoo", dataset="bogus", symbols=("SPY",))
    raw = make(config).fetch(req, http=None)  # type: ignore[arg-type]
    assert raw.fetch_status is FetchStatus.ERROR
    assert raw.error is not None
    assert "bogus" in raw.error


def test_fetch_nan_price_counts_as_no_data(config: Config, fake: FakeYF) -> None:
    fake.meta["SPY"] = {"regularMarketPrice": float("nan"), "regularMarketTime": 1_794_000_000}
    raw = make(config, SUNDAY).fetch(quote_req("SPY"), http=None)  # type: ignore[arg-type]
    assert raw.status is FetchStatus.EMPTY


# ---------------------------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------------------------


def test_parse_quote_fixture_last_tick_with_tier_and_unit(config: Config) -> None:
    df = make(config).parse(fixture_raw("quote"))
    validate("parsed_observations", df)
    assert set(df["field"]) == {"last"}
    assert set(df["interval"]) == {"tick"}
    assert set(df["dataset"]) == {"quote"}
    assert set(df["source"]) == {"yahoo"}
    assert not df["is_fallback"].any()
    assert not df["is_derived"].any()
    assert set(df["instrument_id"]) == {"UST_10Y", "ES_F", "SPY"}  # AAPL errored: no row
    tnx = row(df, "UST_10Y", "last")
    assert tnx["value"] == pytest.approx(4.25)
    assert tnx["unit"] == "pct"
    assert tnx["tier"] == 2
    assert tnx["symbol"] == "^TNX"
    assert tnx["as_of"] == pd.Timestamp("2026-11-13T19:59:54Z")  # P-006: the quote instant
    assert row(df, "ES_F", "last")["unit"] == "index_pts"
    assert row(df, "SPY", "last")["unit"] == "USD"


def test_parse_applies_feed_scale(config: Config) -> None:
    cfg = with_feeds(
        config,
        "UST_10Y",
        (
            Feed(source="yahoo", dataset="quote", symbol="^TNX", tier=2, scale=0.1),
            Feed(source="yahoo", dataset="history", symbol="^TNX", tier=2, scale=0.1),
        ),
    )
    q = make(cfg).parse(
        raw_from(
            "quote", {"symbols": {"^TNX": {"price": 42.5, "time": "2026-11-13T19:59:54+00:00"}}}
        )
    )
    assert row(q, "UST_10Y", "last")["value"] == pytest.approx(4.25)
    h = make(cfg).parse(fixture_raw("history"))
    tnx_close = row(h, "UST_10Y", "close")
    assert tnx_close["value"] == pytest.approx(0.415)  # 4.15 * 0.1: scale hits every price field
    assert row(h, "UST_10Y", "high")["value"] == pytest.approx(0.42)


def test_parse_history_fixture_daily_bars(config: Config) -> None:
    df = make(config).parse(fixture_raw("history"))
    validate("parsed_observations", df)
    assert set(df["interval"]) == {"1d"}
    assert set(df["dataset"]) == {"history"}
    assert set(df["field"]) == {"open", "high", "low", "close"}
    assert not df["is_fallback"].any()
    tnx = df[df["instrument_id"] == "UST_10Y"]
    assert len(tnx) == 8  # 2 bars x 4 fields
    close = tnx[tnx["field"] == "close"].sort_values("as_of")
    assert list(close["value"]) == [4.15, 4.25]
    # P-006: 1d as_of is 00:00 America/New_York on the reference date (EST in November)
    assert close.iloc[1]["as_of"] == pd.Timestamp("2026-11-13T05:00:00Z")
    assert close.iloc[1]["tier"] == 2
    # ES=F Sunday bar (non-null close) is dropped; only Friday's 4 fields remain
    es = df[df["instrument_id"] == "ES_F"]
    assert set(es["as_of"]) == {pd.Timestamp("2026-11-13T05:00:00Z")}
    assert len(es) == 4
    assert "SPY" not in set(df["instrument_id"])


def history_payload(symbol: str, day: str, tz: str | None = "America/New_York") -> dict[str, Any]:
    bar = {"date": day, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}
    return {"symbols": {symbol: {"tz": tz, "bars": [bar]}}}


def test_parse_drops_weekend_bar_with_non_null_close(config: Config) -> None:
    at = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)  # Tuesday: Sunday's bar is long finished
    raw = raw_from("history", history_payload("ES=F", "2026-11-15"), at=at)
    assert make(config).parse(raw).empty
    raw = raw_from("history", history_payload("ES=F", "2026-11-14"), at=at)  # Saturday
    assert make(config).parse(raw).empty


def test_parse_drops_in_progress_bar_of_non_xnys_instrument(config: Config) -> None:
    payload = history_payload("ES=F", "2026-11-16")  # Monday bar, fetched Monday 06:15 ET
    assert make(config).parse(raw_from("history", payload, at=T0)).empty
    next_day = datetime(2026, 11, 17, 11, 15, tzinfo=UTC)
    df = make(config).parse(raw_from("history", payload, at=next_day))
    assert set(df["field"]) == {"open", "high", "low", "close"}


def test_parse_uses_the_exchange_timezone_for_today(config: Config) -> None:
    # 2026-11-16 20:00Z is already Tuesday 05:00 in Tokyo: Monday's N225 bar is final
    at = datetime(2026, 11, 16, 20, 0, tzinfo=UTC)
    payload = history_payload("^N225", "2026-11-16", tz="Asia/Tokyo")
    assert not make(config).parse(raw_from("history", payload, at=at)).empty
    # an unknown tz name falls back to ET (conservative) instead of failing
    bad = history_payload("^N225", "2026-11-16", tz="Not/AZone")
    assert make(config).parse(raw_from("history", bad, at=T0)).empty


def test_parse_xnys_instrument_bar_needs_a_closed_session(config: Config) -> None:
    cfg = with_feeds(
        config,
        "AAPL",
        (Feed(source="yahoo", dataset="daily_fallback", symbol="AAPL", tier=3),),
    )
    payload = history_payload("AAPL", "2026-11-16")
    before_open = datetime(2026, 11, 16, 11, 15, tzinfo=UTC)
    mid_session = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)
    after_close = datetime(2026, 11, 16, 22, 30, tzinfo=UTC)  # close is 21:00Z in November
    for at in (before_open, mid_session):
        assert make(cfg).parse(raw_from("daily_fallback", payload, at=at)).empty
    df = make(cfg).parse(raw_from("daily_fallback", payload, at=after_close))
    assert len(df) == 1
    assert df.iloc[0]["field"] == "close"
    holiday = history_payload("AAPL", "2026-11-26")  # Thanksgiving
    late = datetime(2026, 11, 30, 15, 0, tzinfo=UTC)
    assert make(cfg).parse(raw_from("daily_fallback", holiday, at=late)).empty


def test_parse_daily_fallback_flags_and_close_only(config: Config) -> None:
    cfg = with_feeds(
        config,
        "AAPL",
        (Feed(source="yahoo", dataset="daily_fallback", symbol="AAPL", tier=3),),
    )
    payload = {
        "symbols": {
            "AAPL": {
                "bars": [{"date": "2026-11-13", "open": 1, "high": 2, "low": 0.5, "close": 1.5}]
            }
        }
    }
    df = make(cfg).parse(raw_from("daily_fallback", payload))
    validate("parsed_observations", df)
    assert len(df) == 1
    r = df.iloc[0]
    assert (r["field"], r["interval"], r["dataset"]) == ("close", "1d", "daily_fallback")
    assert bool(r["is_fallback"]) is True
    assert r["tier"] == 3
    assert r["unit"] == "USD"
    assert r["as_of"] == pd.Timestamp("2026-11-13T05:00:00Z")


def test_parse_ignores_unconfigured_symbols(config: Config) -> None:
    df = make(config).parse(
        raw_from("quote", {"symbols": {"ZZZ": {"price": 1.0, "time": "2026-11-13T19:59:54+00:00"}}})
    )
    assert df.empty


def test_parse_empty_cases_return_schema_valid_empty_frame(config: Config) -> None:
    src = make(config)
    for payload in ({"symbols": {}}, {"symbols": {"SPY": {"bars": []}}}):
        df = src.parse(raw_from("history", payload))
        assert df.empty
        validate("parsed_observations", df)
    empty_body = RawResponse(
        request=quote_req("SPY"),
        fetch_id="f",
        requested_at=T0,
        responded_at=T0,
        http_status=200,
        content_type="",
        body=b"",
    )
    assert src.parse(empty_body).empty


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2]",
        b'{"no_symbols": 1}',
        b'{"symbols": []}',
        b'{"symbols": {"SPY": {"price": "abc", "time": "2026-11-13T19:59:54+00:00"}}}',
        b'{"symbols": {"SPY": {"price": 1.0, "time": "garbage"}}}',
        b'{"symbols": {"SPY": {"price": 1.0, "time": "2026-11-13T19:59:54"}}}',
        b'{"symbols": {"SPY": {"price": 1.0}}}',
    ],
)
def test_parse_malformed_quote_raises_parse_error(config: Config, body: bytes) -> None:
    raw = RawResponse(
        request=quote_req("SPY"),
        fetch_id="f",
        requested_at=T0,
        responded_at=T0,
        http_status=200,
        content_type="application/json",
        body=body,
    )
    with pytest.raises(ParseError):
        make(config).parse(raw)


@pytest.mark.parametrize(
    "bars",
    [
        "x",
        [1],
        [{"date": "nope", "close": 1.0}],
        [{"date": "2026-11-13", "close": "abc"}],
        [{"close": 1.0}],
    ],
)
def test_parse_malformed_history_raises_parse_error(config: Config, bars: object) -> None:
    with pytest.raises(ParseError):
        make(config).parse(raw_from("history", {"symbols": {"SPY": {"bars": bars}}}))


def test_parse_unknown_dataset_raises(config: Config) -> None:
    with pytest.raises(ParseError, match="bogus"):
        make(config).parse(raw_from("bogus", {"symbols": {}}))


def test_fetch_output_round_trips_through_parse(config: Config, fake: FakeYF) -> None:
    fake.meta["^TNX"] = {
        "regularMarketPrice": 4.25,
        "regularMarketTime": pd.Timestamp("2026-11-13 13:59:54", tz="America/Chicago"),
    }
    fake.frames["ES=F"] = bars_frame([("2026-11-13", 1.0, 2.0, 0.5, 1.5)], "America/New_York")
    src = make(config)
    q = src.parse(src.fetch(quote_req("^TNX"), http=None))  # type: ignore[arg-type]
    h = src.parse(src.fetch(history_req("ES=F"), http=None))  # type: ignore[arg-type]
    validate("parsed_observations", q)
    validate("parsed_observations", h)
    assert row(q, "UST_10Y", "last")["value"] == pytest.approx(4.25)
    assert row(h, "ES_F", "close")["value"] == pytest.approx(1.5)


# -- T08: the "empty on a business day is an error" rule depends on the symbol set ----------------

THANKSGIVING = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)  # Thursday, XNYS closed


def test_empty_on_xnys_holiday_is_not_an_error_for_us_equities(
    config: Config, fake: FakeYF
) -> None:
    raw = make(config, THANKSGIVING).fetch(history_req("SPY", "QQQ"), http=None)  # type: ignore[arg-type]
    assert raw.error is None
    assert raw.fetch_status is FetchStatus.EMPTY


@pytest.mark.parametrize("symbols", [("ES=F",), ("^TNX",), ("SPY", "ES=F")])
def test_empty_on_xnys_holiday_is_an_error_when_a_non_xnys_symbol_expects_data(
    config: Config, fake: FakeYF, symbols: tuple[str, ...]
) -> None:
    """Futures, indices and yields trade on an XNYS holiday weekday, so no data means a block."""
    raw = make(config, THANKSGIVING).fetch(history_req(*symbols), http=None)  # type: ignore[arg-type]
    assert raw.fetch_status is FetchStatus.ERROR
    assert raw.error is not None
    assert "no data" in raw.error


def test_empty_on_a_weekend_is_status_empty_for_non_xnys_symbols(
    config: Config, fake: FakeYF
) -> None:
    raw = make(config, SUNDAY).fetch(history_req("ES=F", "^TNX"), http=None)  # type: ignore[arg-type]
    assert raw.error is None
    assert raw.fetch_status is FetchStatus.EMPTY


def test_non_xnys_bar_dated_today_is_dropped_even_after_its_session_ends(config: Config) -> None:
    """T08 (documented limit): with no calendar for futures/indices/yields, today's bar is only
    emitted from the next day, even when the session has already finished."""
    payload = history_payload("^TNX", "2026-11-16", tz="America/Chicago")
    late_evening = datetime(2026, 11, 16, 23, 30, tzinfo=UTC)  # 17:30 Chicago, session over
    assert make(config).parse(raw_from("history", payload, at=late_evening)).empty
    next_day = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
    assert not make(config).parse(raw_from("history", payload, at=next_day)).empty
