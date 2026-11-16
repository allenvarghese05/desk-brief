# ruff: noqa: PT018
"""Alpaca source tests (card T14). No network: HTTP is mocked with respx.

Fixtures under tests/fixtures/synthetic/alpaca are SYNTHETIC FIXTURES (schema-faithful to the
documented Alpaca Market Data v2 JSON, invented values). The key id and secret below are
synthetic test values built at runtime from low-entropy pieces.
"""

from __future__ import annotations

import importlib
import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest
import respx

from deskbrief.config import Config, SourceSettings, load_config
from deskbrief.logging import configure_logging
from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse, RunContext, Trigger
from deskbrief.schemas import validate
from deskbrief.sources import registry
from deskbrief.sources.alpaca import (
    BARS_URL,
    MAX_PAGES,
    QUOTES_URL,
    SIP_DELAY,
    TRADES_URL,
    AlpacaSource,
)
from deskbrief.sources.base import ParseError, Source
from deskbrief.sources.http import DefaultHttpClient
from deskbrief.store.backend import raw_meta_key
from deskbrief.store.local import LocalBackend
from deskbrief.store.raw import RawWriter

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "tests" / "fixtures" / "synthetic" / "alpaca"
KEY_ID = "-".join(["fake", "alpaca", "id"] * 3)
SECRET = "-".join(["fake", "alpaca", "secret"] * 3)
XCHECK = ["SPY", "QQQ", "IWM", "GLD", "TLT"]
LATEST_SYMBOLS = [
    *["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA"],
    *["USO", "BNO", "GLD", "SHY", "IEF", "TLT"],
]


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def src(config: Config) -> AlpacaSource:
    return AlpacaSource(config)


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_KEY_ID", KEY_ID)
    monkeypatch.setenv("ALPACA_SECRET_KEY", SECRET)


def ctx_for(
    config: Config,
    job: JobKind,
    target: date = date(2026, 11, 16),
    now: datetime | None = None,
) -> RunContext:
    return RunContext(
        run_id="r1",
        job=job,
        trigger=Trigger.SCHEDULE,
        target_date=target,
        now_utc=now or datetime(2026, 11, 16, 22, 30, tzinfo=UTC),  # 17:30 ET, after the close
        git_sha="abc",
        config_hash="h",
        config=config,
        store=object(),  # type: ignore[arg-type]
    )


def by_dataset(reqs: list[FetchRequest], dataset: str) -> list[FetchRequest]:
    return [r for r in reqs if r.dataset == dataset]


T0 = datetime(2026, 11, 16, 22, 30, tzinfo=UTC)


def raw_for(
    dataset: str,
    body: bytes,
    *,
    part: str | None = None,
    http_status: int | None = 200,
    error: str | None = None,
    status: FetchStatus | None = None,
) -> RawResponse:
    params = {"symbols": "SPY"}
    if part:
        params["part"] = part
    req = FetchRequest(source="alpaca", dataset=dataset, params=params, symbols=("SPY",))
    return RawResponse(
        request=req,
        fetch_id="f1",
        requested_at=T0,
        responded_at=T0,
        http_status=http_status,
        content_type="application/json",
        body=body,
        error=error,
        status=status,
    )


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


def trades_raw(body: bytes | None = None) -> RawResponse:
    return raw_for("iex_latest", body or fx("trades_latest.json"), part="trades")


def quotes_raw(body: bytes | None = None) -> RawResponse:
    return raw_for("iex_latest", body or fx("quotes_latest.json"), part="quotes")


def bar(t: str = "2026-11-13T14:30:00Z", **over: Any) -> dict[str, Any]:
    return {"t": t, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100, **over}


def bars_body(rows: dict[str, list[dict[str, Any]]] | None, token: str | None = None) -> bytes:
    return json.dumps({"bars": rows, "next_page_token": token}).encode()


# ---------------------------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("job", [JobKind.MORNING, JobKind.INTRADAY])
def test_plan_latest_is_one_trades_and_one_quotes_request(
    src: AlpacaSource, config: Config, job: JobKind
) -> None:
    reqs = src.plan(job, ctx_for(config, job))
    assert [r.dataset for r in reqs] == ["iex_latest", "iex_latest"]
    assert sorted(r.params["part"] for r in reqs) == ["quotes", "trades"]
    for r in reqs:
        assert r.source == "alpaca" and r.params["feed"] == "iex"
        assert sorted(r.symbols) == sorted(LATEST_SYMBOLS)
        assert r.params["symbols"] == ",".join(r.symbols)
        assert r.window is None


@pytest.mark.parametrize("job", list(JobKind))
def test_plan_never_carries_secrets(src: AlpacaSource, config: Config, job: JobKind) -> None:
    for r in src.plan(job, ctx_for(config, job)):
        assert not any(w in k.lower() for k in r.params for w in ("key", "secret", "token"))
        assert KEY_ID not in repr(r) and SECRET not in repr(r)


def test_plan_rebuild_is_empty(src: AlpacaSource, config: Config) -> None:
    assert src.plan(JobKind.REBUILD, ctx_for(config, JobKind.REBUILD)) == []


def test_plan_eod_is_bars_for_the_iex_vs_sip_symbols(src: AlpacaSource, config: Config) -> None:
    reqs = src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD))
    assert sorted({r.dataset for r in reqs}) == ["iex_bars_1m", "sip_bars_1m"]
    iex, sip = by_dataset(reqs, "iex_bars_1m"), by_dataset(reqs, "sip_bars_1m")
    assert len(iex) == 1 and len(sip) == 1
    for r in (iex[0], sip[0]):
        assert sorted(r.symbols) == sorted(XCHECK)
        assert r.params["timeframe"] == "1Min" and r.params["adjustment"] == "raw"
        assert r.params["sort"] == "asc" and r.target_date == date(2026, 11, 16)
    assert iex[0].params["feed"] == "iex" and sip[0].params["feed"] == "sip"


def test_plan_eod_window_is_the_xnys_session_in_est(src: AlpacaSource, config: Config) -> None:
    reqs = src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD))
    for r in reqs:
        assert r.window == (
            datetime(2026, 11, 16, 14, 30, tzinfo=UTC),  # 09:30 EST
            datetime(2026, 11, 16, 21, 0, tzinfo=UTC),  # 16:00 EST
        )


@pytest.mark.parametrize(
    ("day", "now", "start", "end"),
    [
        # EDT summer
        (date(2026, 7, 15), datetime(2026, 7, 15, 22, 30, tzinfo=UTC), (13, 30), (20, 0)),
        # DST starts Sunday 2026-03-08: Friday is EST, Monday is EDT
        (date(2026, 3, 6), datetime(2026, 3, 6, 23, 0, tzinfo=UTC), (14, 30), (21, 0)),
        (date(2026, 3, 9), datetime(2026, 3, 9, 22, 30, tzinfo=UTC), (13, 30), (20, 0)),
        # DST ends Sunday 2026-11-01: Friday is EDT, Monday is EST
        (date(2026, 10, 30), datetime(2026, 10, 30, 22, 30, tzinfo=UTC), (13, 30), (20, 0)),
        (date(2026, 11, 2), datetime(2026, 11, 2, 22, 30, tzinfo=UTC), (14, 30), (21, 0)),
        # 2026-11-27 early close: 13:00 EST = 18:00 UTC
        (date(2026, 11, 27), datetime(2026, 11, 27, 22, 30, tzinfo=UTC), (14, 30), (18, 0)),
    ],
)
def test_plan_eod_window_across_dst_and_early_close(
    src: AlpacaSource,
    config: Config,
    day: date,
    now: datetime,
    start: tuple[int, int],
    end: tuple[int, int],
) -> None:
    reqs = src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD, day, now))
    assert len(reqs) == 2
    for r in reqs:
        assert r.window == (
            datetime(day.year, day.month, day.day, *start, tzinfo=UTC),
            datetime(day.year, day.month, day.day, *end, tzinfo=UTC),
        )


@pytest.mark.parametrize("day", [date(2026, 11, 26), date(2026, 11, 21), date(2026, 12, 25)])
def test_plan_eod_on_a_closed_day_plans_nothing(
    src: AlpacaSource, config: Config, day: date
) -> None:
    now = datetime(day.year, day.month, day.day, 22, 30, tzinfo=UTC)
    assert src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD, day, now)) == []
    assert src.plan(JobKind.BACKFILL, ctx_for(config, JobKind.BACKFILL, day, now)) == []


def test_plan_backfill_covers_the_target_date_session(src: AlpacaSource, config: Config) -> None:
    ctx = ctx_for(config, JobKind.BACKFILL, date(2026, 11, 13), T0)
    reqs = src.plan(JobKind.BACKFILL, ctx)
    assert sorted(r.dataset for r in reqs) == ["iex_bars_1m", "sip_bars_1m"]
    for r in reqs:
        assert r.target_date == date(2026, 11, 13)
        assert r.window == (
            datetime(2026, 11, 13, 14, 30, tzinfo=UTC),
            datetime(2026, 11, 13, 21, 0, tzinfo=UTC),
        )


def test_plan_backfill_early_close(src: AlpacaSource, config: Config) -> None:
    ctx = ctx_for(config, JobKind.BACKFILL, date(2026, 11, 27), T0 + timedelta(days=30))
    for r in src.plan(JobKind.BACKFILL, ctx):
        assert r.window is not None and r.window[1] == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_plan_sip_window_is_clamped_to_16_minutes_before_now(
    src: AlpacaSource, config: Config
) -> None:
    now = datetime(2026, 11, 16, 20, 50, tzinfo=UTC)  # 15:50 ET, session still open
    reqs = src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD, now=now))
    iex, sip = by_dataset(reqs, "iex_bars_1m")[0], by_dataset(reqs, "sip_bars_1m")[0]
    assert sip.window is not None and sip.window[1] == now - timedelta(minutes=16)
    assert sip.window[0] == datetime(2026, 11, 16, 14, 30, tzinfo=UTC)
    # IEX is real time on the free plan: not clamped
    assert iex.window is not None and iex.window[1] == datetime(2026, 11, 16, 21, 0, tzinfo=UTC)


def test_sip_delay_is_16_minutes() -> None:
    assert timedelta(minutes=16) == SIP_DELAY


def test_plan_sip_window_exactly_16_minutes_old_is_allowed_unclamped(
    src: AlpacaSource, config: Config
) -> None:
    close = datetime(2026, 11, 16, 21, 0, tzinfo=UTC)
    sip = by_dataset(
        src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD, now=close + timedelta(minutes=16))),
        "sip_bars_1m",
    )[0]
    assert sip.window is not None and sip.window[1] == close
    # one minute earlier and the close is inside the delay window: clamped
    sip = by_dataset(
        src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD, now=close + timedelta(minutes=15))),
        "sip_bars_1m",
    )[0]
    assert sip.window is not None and sip.window[1] == close - timedelta(minutes=1)


def test_plan_sip_is_skipped_when_the_whole_window_is_too_recent(
    src: AlpacaSource, config: Config
) -> None:
    now = datetime(2026, 11, 16, 14, 40, tzinfo=UTC)  # 10 min after the open: now-16m < open
    reqs = src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD, now=now))
    assert by_dataset(reqs, "sip_bars_1m") == []
    assert len(by_dataset(reqs, "iex_bars_1m")) == 1


@pytest.mark.parametrize("job", [JobKind.EOD, JobKind.BACKFILL])
@pytest.mark.parametrize("minutes_after_open", range(0, 460, 7))
def test_sip_window_always_ends_at_least_16_minutes_before_now(
    src: AlpacaSource, config: Config, job: JobKind, minutes_after_open: int
) -> None:
    now = datetime(2026, 11, 16, 14, 30, tzinfo=UTC) + timedelta(minutes=minutes_after_open)
    for r in by_dataset(src.plan(job, ctx_for(config, job, now=now)), "sip_bars_1m"):
        assert r.window is not None
        assert r.window[1] <= now - timedelta(minutes=16)
        assert r.window[0] < r.window[1]


def test_plan_skips_xcheck_symbol_without_a_feed(config: Config) -> None:
    src = AlpacaSource(config)
    del src._indexes["sip_bars_1m"]["TLT"]
    sip = by_dataset(src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD)), "sip_bars_1m")[0]
    assert "TLT" not in sip.symbols and len(sip.symbols) == 4


# ---------------------------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------------------------


def test_parse_latest_trades(src: AlpacaSource) -> None:
    df = src.parse(trades_raw())
    validate("parsed_observations", df)
    assert len(df) == 3
    assert set(df["field"]) == {"last"} and set(df["interval"]) == {"tick"}
    assert set(df["tier"]) == {1} and set(df["unit"]) == {"USD"}
    assert set(df["source"]) == {"alpaca"} and set(df["dataset"]) == {"iex_latest"}
    assert not df["is_derived"].any() and not df["is_fallback"].any()
    spy = df[df["symbol"] == "SPY"].iloc[0]
    assert spy["value"] == 601.25 and spy["instrument_id"] == "SPY"
    # tick as_of = the trade instant (nanoseconds truncated to the microsecond)
    assert spy["as_of"] == pd.Timestamp("2026-11-13T20:59:58.123456Z")
    aapl = df[df["symbol"] == "AAPL"].iloc[0]
    assert aapl["as_of"] == pd.Timestamp("2026-11-13T20:59:50Z")


def test_parse_latest_quotes_give_bid_and_ask_and_skip_a_zero_side(src: AlpacaSource) -> None:
    df = src.parse(quotes_raw())
    validate("parsed_observations", df)
    got = {(r.symbol, r.field): r.value for r in df.itertuples()}
    assert got == {("SPY", "bid"): 601.2, ("SPY", "ask"): 601.3, ("QQQ", "bid"): 512.35}
    assert set(df["interval"]) == {"tick"} and set(df["tier"]) == {1}
    spy_bid = df[(df["symbol"] == "SPY") & (df["field"] == "bid")].iloc[0]
    assert spy_bid["as_of"] == pd.Timestamp("2026-11-13T20:59:59.25Z")


def test_parse_iex_bars(src: AlpacaSource) -> None:
    raw = raw_for("iex_bars_1m", fx("bars_iex_page1.json"))
    df = src.parse(raw)
    validate("parsed_observations", df)
    assert len(df) == 3 * 5  # three bars x open/high/low/close/volume
    assert set(df["field"]) == {"open", "high", "low", "close", "volume"}
    assert set(df["interval"]) == {"1m"} and set(df["tier"]) == {1}
    assert set(df["dataset"]) == {"iex_bars_1m"}
    first = df[(df["symbol"] == "SPY") & (df["as_of"] == pd.Timestamp("2026-11-13T14:30:00Z"))]
    vals = {r.field: (r.value, r.unit) for r in first.itertuples()}
    assert vals == {
        "open": (600.0, "USD"),
        "high": (600.5, "USD"),
        "low": (599.8, "USD"),
        "close": (600.4, "USD"),
        "volume": (12000.0, "shares"),  # volume is in shares, not USD
    }


def test_parse_sip_bars_are_tier_2(src: AlpacaSource) -> None:
    df = src.parse(raw_for("sip_bars_1m", fx("bars_sip.json")))
    validate("parsed_observations", df)
    assert len(df) == 10
    assert set(df["tier"]) == {2} and set(df["dataset"]) == {"sip_bars_1m"}
    assert set(df["interval"]) == {"1m"}


def test_parse_bar_start_is_as_of_and_et_date_is_the_session_date(src: AlpacaSource) -> None:
    from deskbrief.timeutil import et_date

    df = src.parse(raw_for("iex_bars_1m", fx("bars_iex_page1.json")))
    assert {et_date(t.to_pydatetime()) for t in df["as_of"]} == {date(2026, 11, 13)}


def test_parse_multi_page_wrapper(src: AlpacaSource) -> None:
    pages = [fx("bars_iex_page1.json"), fx("bars_iex_page2.json"), fx("bars_iex_page3.json")]
    body = b'{"pages":[' + b",".join(pages) + b"]}"
    df = src.parse(raw_for("iex_bars_1m", body))
    validate("parsed_observations", df)
    assert len(df) == 5 * 5
    assert sorted(set(df["symbol"])) == ["GLD", "QQQ", "SPY"]


def test_parse_empty_bars_is_empty_frame(src: AlpacaSource) -> None:
    for body in (fx("bars_empty.json"), b'{"bars": null}', b'{"bars": {"SPY": []}}'):
        df = src.parse(raw_for("iex_bars_1m", body))
        assert df.empty
        validate("parsed_observations", df)


def test_parse_empty_latest_is_empty_frame(src: AlpacaSource) -> None:
    assert src.parse(trades_raw(b'{"trades": {}}')).empty
    assert src.parse(quotes_raw(b'{"quotes": {}}')).empty


def test_parse_ignores_symbols_that_are_not_configured(src: AlpacaSource) -> None:
    body = bars_body({"ZZZZ": [bar()], "SPY": [bar()]})
    df = src.parse(raw_for("iex_bars_1m", body))
    assert set(df["symbol"]) == {"SPY"}
    # AAPL has no iex_bars_1m feed in config, so it is skipped for that dataset
    assert src.parse(raw_for("iex_bars_1m", bars_body({"AAPL": [bar()]}))).empty


def test_parse_applies_scale_to_prices_not_volume(config: Config) -> None:
    src = AlpacaSource(config)
    inst, feed = src._indexes["iex_bars_1m"]["SPY"]
    src._indexes["iex_bars_1m"]["SPY"] = (inst, feed.model_copy(update={"scale": 0.5}))
    df = src.parse(raw_for("iex_bars_1m", bars_body({"SPY": [bar()]})))
    got = {r.field: r.value for r in df.itertuples()}
    assert got == {"open": 0.5, "high": 1.0, "low": 0.25, "close": 0.75, "volume": 100.0}


def test_parse_is_pure_replay(src: AlpacaSource) -> None:
    raw = raw_for("iex_bars_1m", fx("bars_iex_page1.json"))
    assert src.parse(raw).equals(src.parse(raw))


def one_bar(key: str, json_value: str) -> bytes:
    """A one-bar bars payload where ``key`` is set to the raw JSON text ``json_value``."""
    fields = {"t": '"2026-11-13T14:30:00Z"', "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
    fields[key] = json_value
    inner = ", ".join(f'"{k}": {v}' for k, v in fields.items())
    return ('{"bars": {"SPY": [{' + inner + "}]}}").encode()


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b"{}",
        b"\xff\xfe",
        b'{"bars": []}',
        b'{"bars": {"SPY": "x"}}',
        b'{"bars": {"SPY": ["x"]}}',
        b'{"bars": {"SPY": [{"t": "2026-11-13T14:30:00Z", "o": 1, "h": 1, "l": 1, "c": 1}]}}',
        b'{"bars": {"SPY": [{"o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]}}',
        b'{"bars": {"SPY": [{"t": "yesterday", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]}}',
        one_bar("t", '"2026-11-13T14:30:00"'),  # no zone
        one_bar("o", '"1"'),
        one_bar("o", "true"),
        one_bar("o", "NaN"),
        b'{"pages": {}}',
        b'{"pages": ["x"]}',
        b'{"pages": [{"bars": {}}, {"nope": 1}]}',
    ],
)
def test_parse_malformed_bars_raise(src: AlpacaSource, body: bytes) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for("iex_bars_1m", body))


@pytest.mark.parametrize(
    "body",
    [
        b'{"trades": []}',
        b'{"trades": {"SPY": "x"}}',
        b'{"trades": {"SPY": {"t": "2026-11-13T20:59:50Z"}}}',
        b'{"trades": {"SPY": {"p": 1.0}}}',
        b'{"trades": {"SPY": {"t": "2026-11-13T20:59:50Z", "p": "1.0"}}}',
        b'{"quotes": {"SPY": {"t": "2026-11-13T20:59:50Z", "bp": 1.0}}}',
        b'{"quotes": {}}',
    ],
)
def test_parse_malformed_trades_raise(src: AlpacaSource, body: bytes) -> None:
    with pytest.raises(ParseError):
        src.parse(trades_raw(body))


def test_parse_malformed_quotes_raise(src: AlpacaSource) -> None:
    for body in (
        b'{"quotes": {"SPY": {"t": "2026-11-13T20:59:50Z", "bp": 1.0}}}',
        b'{"quotes": {"SPY": {"t": "2026-11-13T20:59:50Z", "bp": "1", "ap": 2}}}',
        b'{"trades": {}}',
    ):
        with pytest.raises(ParseError):
            src.parse(quotes_raw(body))


@pytest.mark.parametrize("name", ["error_unauthorized.json", "error_sip_recent.json"])
def test_parse_error_payload_raises_with_the_message(src: AlpacaSource, name: str) -> None:
    message = json.loads(fx(name))["message"]
    with pytest.raises(ParseError, match=message):
        src.parse(raw_for("iex_bars_1m", fx(name)))
    with pytest.raises(ParseError, match=message):
        src.parse(trades_raw(fx(name)))


def test_parse_error_payload_with_http_200_still_raises(src: AlpacaSource) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for("sip_bars_1m", fx("error_sip_recent.json"), http_status=200))


def test_parse_failed_fetch_raises(src: AlpacaSource) -> None:
    raw = raw_for("iex_bars_1m", b"", http_status=403, error="HTTP 403")
    with pytest.raises(ParseError):
        src.parse(raw)


def test_parse_latest_without_part_raises(src: AlpacaSource) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for("iex_latest", fx("trades_latest.json")))


def test_parse_unknown_dataset_raises(src: AlpacaSource) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for("daily_bars", fx("bars_sip.json")))


# ---------------------------------------------------------------------------------------------
# fetch: requests, keys
# ---------------------------------------------------------------------------------------------


def http_client(retries: int = 0) -> DefaultHttpClient:
    return DefaultHttpClient(
        {"alpaca": SourceSettings(rate_per_min=60000.0, timeout_s=5.0, retries=retries)},
        sleep=lambda s: None,
    )


def json_response(body: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body, headers={"content-type": "application/json"})


def eod_req(src: AlpacaSource, config: Config, dataset: str) -> FetchRequest:
    reqs = src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD, now=T0))
    return by_dataset(reqs, dataset)[0]


def latest_req(src: AlpacaSource, config: Config, part: str) -> FetchRequest:
    reqs = src.plan(JobKind.INTRADAY, ctx_for(config, JobKind.INTRADAY))
    return next(r for r in reqs if r.params["part"] == part)


@respx.mock
@pytest.mark.usefixtures("keys")
def test_fetch_latest_trades_and_quotes_use_their_endpoints(
    src: AlpacaSource, config: Config
) -> None:
    trades = respx.get(TRADES_URL).mock(return_value=json_response(fx("trades_latest.json")))
    quotes = respx.get(QUOTES_URL).mock(return_value=json_response(fx("quotes_latest.json")))
    t = src.fetch(latest_req(src, config, "trades"), http_client())
    q = src.fetch(latest_req(src, config, "quotes"), http_client())
    assert t.ok and q.ok and trades.call_count == 1 and quotes.call_count == 1
    for route in (trades, quotes):
        params = route.calls.last.request.url.params
        assert params["feed"] == "iex" and "part" not in params
        assert sorted(params["symbols"].split(",")) == sorted(LATEST_SYMBOLS)
    assert len(src.parse(t)) == 3 and len(src.parse(q)) == 3


@respx.mock
@pytest.mark.usefixtures("keys")
def test_fetch_sends_keys_in_headers_only(src: AlpacaSource, config: Config) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    route = respx.get(BARS_URL).mock(return_value=json_response(fx("bars_sip.json")))
    req = eod_req(src, config, "sip_bars_1m")
    raw = src.fetch(req, http_client())

    sent = route.calls.last.request
    assert sent.headers["APCA-API-KEY-ID"] == KEY_ID
    assert sent.headers["APCA-API-SECRET-KEY"] == SECRET
    assert KEY_ID not in str(sent.url) and SECRET not in str(sent.url)
    params = sent.url.params
    assert params["feed"] == "sip" and params["timeframe"] == "1Min"
    assert params["start"] == "2026-11-16T14:30:00Z" and params["end"] == "2026-11-16T21:00:00Z"
    assert params["adjustment"] == "raw" and params["sort"] == "asc" and params["limit"] == "10000"
    assert sorted(params["symbols"].split(",")) == sorted(XCHECK)
    assert "page_token" not in params

    assert raw.ok and raw.fetch_status is FetchStatus.OK and raw.body == fx("bars_sip.json")
    for secret in (KEY_ID, SECRET):
        assert secret not in repr(req) and secret not in str(dict(req.params))
        assert secret not in repr(raw.request)
        assert secret not in (raw.url_redacted or "") and secret not in (raw.error or "")
        assert secret not in stream.getvalue()
    assert len(src.parse(raw)) == 10


@respx.mock
@pytest.mark.usefixtures("keys")
def test_keys_never_reach_the_raw_body_or_sidecar(
    src: AlpacaSource, config: Config, tmp_path: Path
) -> None:
    respx.get(BARS_URL).mock(return_value=json_response(fx("bars_sip.json")))
    raw = src.fetch(eod_req(src, config, "sip_bars_1m"), http_client())
    store = LocalBackend(tmp_path / "data")
    key = RawWriter(store).write(raw, ctx_for(config, JobKind.EOD))
    meta = store.get_bytes(raw_meta_key(key))
    stored = store.get_bytes(key) + meta
    for secret in (KEY_ID, SECRET):
        assert secret.encode() not in stored
    sidecar = json.loads(meta)
    assert sidecar["params"]["feed"] == "sip" and sidecar["url_redacted"]


@respx.mock
@pytest.mark.usefixtures("keys")
def test_http_error_does_not_leak_keys(src: AlpacaSource, config: Config) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    respx.get(BARS_URL).mock(return_value=json_response(fx("error_unauthorized.json"), 401))
    raw = src.fetch(eod_req(src, config, "iex_bars_1m"), http_client())
    assert raw.fetch_status is FetchStatus.ERROR and raw.http_status == 401
    for secret in (KEY_ID, SECRET):
        assert secret not in (raw.error or "") and secret not in (raw.url_redacted or "")
        assert secret not in stream.getvalue()


@respx.mock
@pytest.mark.usefixtures("keys")
def test_transport_error_does_not_leak_keys(src: AlpacaSource, config: Config) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    respx.get(BARS_URL).mock(
        side_effect=httpx.ConnectError(f"boom {KEY_ID} {SECRET} APCA-API-SECRET-KEY: {SECRET}")
    )
    raw = src.fetch(eod_req(src, config, "iex_bars_1m"), http_client())
    assert raw.fetch_status is FetchStatus.ERROR
    for secret in (KEY_ID, SECRET):
        assert secret not in (raw.error or "") and secret not in stream.getvalue()


@respx.mock
@pytest.mark.parametrize(
    ("key_id", "secret", "missing"),
    [
        (None, None, ["ALPACA_KEY_ID", "ALPACA_SECRET_KEY"]),
        (KEY_ID, None, ["ALPACA_SECRET_KEY"]),
        (None, SECRET, ["ALPACA_KEY_ID"]),
        ("", SECRET, ["ALPACA_KEY_ID"]),
        (KEY_ID, "   ", ["ALPACA_SECRET_KEY"]),
    ],
)
def test_fetch_without_keys_returns_error_response_and_makes_no_request(
    src: AlpacaSource,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    key_id: str | None,
    secret: str | None,
    missing: list[str],
) -> None:
    for name, value in (("ALPACA_KEY_ID", key_id), ("ALPACA_SECRET_KEY", secret)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    route = respx.get(url__startswith="https://data.alpaca.markets").mock(
        return_value=httpx.Response(200, json={})
    )
    for req in (eod_req(src, config, "iex_bars_1m"), latest_req(src, config, "trades")):
        raw = src.fetch(req, http_client())
        assert not route.called
        assert not raw.ok and raw.fetch_status is FetchStatus.ERROR
        assert raw.http_status is None and raw.body == b"" and raw.request is req
        assert raw.error is not None and all(name in raw.error for name in missing)


@respx.mock
def test_fetch_strips_whitespace_around_keys(
    src: AlpacaSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALPACA_KEY_ID", f" {KEY_ID}\n")
    monkeypatch.setenv("ALPACA_SECRET_KEY", f"{SECRET}\n")
    route = respx.get(BARS_URL).mock(return_value=json_response(fx("bars_sip.json")))
    src.fetch(eod_req(src, config, "sip_bars_1m"), http_client())
    assert route.calls.last.request.headers["APCA-API-KEY-ID"] == KEY_ID
    assert route.calls.last.request.headers["APCA-API-SECRET-KEY"] == SECRET


@respx.mock
@pytest.mark.usefixtures("keys")
def test_sip_recent_403_is_an_error_and_parse_raises(src: AlpacaSource, config: Config) -> None:
    respx.get(BARS_URL).mock(return_value=json_response(fx("error_sip_recent.json"), 403))
    raw = src.fetch(eod_req(src, config, "sip_bars_1m"), http_client())
    assert raw.fetch_status is FetchStatus.ERROR and raw.http_status == 403
    with pytest.raises(ParseError):
        src.parse(raw)


@respx.mock
@pytest.mark.usefixtures("keys")
def test_empty_bars_on_a_business_day_is_status_empty(src: AlpacaSource, config: Config) -> None:
    respx.get(BARS_URL).mock(return_value=json_response(fx("bars_empty.json")))
    raw = src.fetch(eod_req(src, config, "iex_bars_1m"), http_client())
    assert raw.ok and raw.fetch_status is FetchStatus.EMPTY
    assert raw.body == fx("bars_empty.json")  # still stored byte-for-byte
    assert src.parse(raw).empty


@respx.mock
@pytest.mark.usefixtures("keys")
def test_empty_latest_is_status_empty(src: AlpacaSource, config: Config) -> None:
    respx.get(TRADES_URL).mock(return_value=json_response(b'{"trades": {}}'))
    raw = src.fetch(latest_req(src, config, "trades"), http_client())
    assert raw.fetch_status is FetchStatus.EMPTY and src.parse(raw).empty


# ---------------------------------------------------------------------------------------------
# fetch: pagination
# ---------------------------------------------------------------------------------------------


@respx.mock
@pytest.mark.usefixtures("keys")
def test_pagination_follows_next_page_token_and_merges_pages(
    src: AlpacaSource, config: Config
) -> None:
    pages = {
        None: fx("bars_iex_page1.json"),
        "synthetic-page-2": fx("bars_iex_page2.json"),
        "synthetic-page-3": fx("bars_iex_page3.json"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(pages[request.url.params.get("page_token")])

    route = respx.get(BARS_URL).mock(side_effect=handler)
    raw = src.fetch(eod_req(src, config, "iex_bars_1m"), http_client())

    assert route.call_count == 3
    tokens = [c.request.url.params.get("page_token") for c in route.calls]
    assert tokens == [None, "synthetic-page-2", "synthetic-page-3"]
    for call in route.calls:  # headers on every page, never the secret in the URL
        assert call.request.headers["APCA-API-KEY-ID"] == KEY_ID
        assert SECRET not in str(call.request.url)
        assert call.request.url.params["start"] == "2026-11-16T14:30:00Z"
    assert raw.ok and raw.attempts == 3
    # each page's bytes are kept verbatim inside the stored body
    for page in pages.values():
        assert page in raw.body
    assert json.loads(raw.body)["pages"][0]["next_page_token"] == "synthetic-page-2"
    df = src.parse(raw)
    validate("parsed_observations", df)
    assert len(df) == 25 and sorted(set(df["symbol"])) == ["GLD", "QQQ", "SPY"]


@respx.mock
@pytest.mark.usefixtures("keys")
def test_single_page_is_returned_unwrapped(src: AlpacaSource, config: Config) -> None:
    respx.get(BARS_URL).mock(return_value=json_response(fx("bars_sip.json")))
    raw = src.fetch(eod_req(src, config, "sip_bars_1m"), http_client())
    assert raw.body == fx("bars_sip.json") and raw.attempts == 1


@respx.mock
@pytest.mark.usefixtures("keys")
def test_pagination_token_loop_stops_with_an_error(src: AlpacaSource, config: Config) -> None:
    looping = json.dumps({"bars": {"SPY": [bar()]}, "next_page_token": "same"}).encode()
    route = respx.get(BARS_URL).mock(return_value=json_response(looping))
    raw = src.fetch(eod_req(src, config, "iex_bars_1m"), http_client())
    assert route.call_count == 2  # page 1 -> token "same"; page 2 repeats it
    assert not raw.ok and raw.fetch_status is FetchStatus.ERROR
    assert raw.error is not None and "loop" in raw.error and raw.body == b""
    with pytest.raises(ParseError):
        src.parse(raw)


@respx.mock
@pytest.mark.usefixtures("keys")
def test_pagination_is_bounded_even_when_every_token_is_new(
    src: AlpacaSource, config: Config
) -> None:
    counter = iter(range(10_000))

    def handler(request: httpx.Request) -> httpx.Response:
        body = {"bars": {"SPY": [bar()]}, "next_page_token": f"t{next(counter)}"}
        return json_response(json.dumps(body).encode())

    route = respx.get(BARS_URL).mock(side_effect=handler)
    raw = src.fetch(eod_req(src, config, "iex_bars_1m"), http_client())
    assert route.call_count == MAX_PAGES
    assert not raw.ok and raw.error is not None and str(MAX_PAGES) in raw.error


@respx.mock
@pytest.mark.usefixtures("keys")
def test_a_failing_later_page_fails_the_whole_fetch(src: AlpacaSource, config: Config) -> None:
    responses = iter([json_response(fx("bars_iex_page1.json")), httpx.Response(500)])
    route = respx.get(BARS_URL).mock(side_effect=lambda request: next(responses))
    raw = src.fetch(eod_req(src, config, "iex_bars_1m"), http_client())
    assert route.call_count == 2
    assert not raw.ok and raw.http_status == 500 and raw.fetch_status is FetchStatus.ERROR
    with pytest.raises(ParseError):
        src.parse(raw)  # never a partial frame from page 1 alone


@respx.mock
@pytest.mark.usefixtures("keys")
def test_a_non_json_later_page_is_an_error(src: AlpacaSource, config: Config) -> None:
    responses = iter([json_response(fx("bars_iex_page1.json")), json_response(b"<html>oops")])
    respx.get(BARS_URL).mock(side_effect=lambda request: next(responses))
    raw = src.fetch(eod_req(src, config, "iex_bars_1m"), http_client())
    assert not raw.ok and raw.error is not None and "page 2" in raw.error


@respx.mock
@pytest.mark.usefixtures("keys")
def test_a_non_json_first_page_is_returned_as_is_and_parse_raises(
    src: AlpacaSource, config: Config
) -> None:
    respx.get(BARS_URL).mock(return_value=json_response(b"<html>oops"))
    raw = src.fetch(eod_req(src, config, "iex_bars_1m"), http_client())
    assert raw.ok and raw.body == b"<html>oops"
    with pytest.raises(ParseError):
        src.parse(raw)


@respx.mock
@pytest.mark.usefixtures("keys")
def test_pagination_empty_string_token_means_last_page(src: AlpacaSource, config: Config) -> None:
    body = json.dumps({"bars": {"SPY": [bar()]}, "next_page_token": ""}).encode()
    route = respx.get(BARS_URL).mock(return_value=json_response(body))
    raw = src.fetch(eod_req(src, config, "iex_bars_1m"), http_client())
    assert route.call_count == 1 and raw.body == body


@respx.mock
@pytest.mark.usefixtures("keys")
@pytest.mark.parametrize(
    "req",
    [
        FetchRequest(source="alpaca", dataset="iex_latest", params={"symbols": "SPY"}),
        FetchRequest(source="alpaca", dataset="iex_bars_1m", params={"symbols": "SPY"}),
        FetchRequest(source="alpaca", dataset="daily_bars", params={"symbols": "SPY"}),
    ],
)
def test_fetch_of_a_malformed_request_is_an_error_without_http(req: FetchRequest) -> None:
    route = respx.get(url__startswith="https://data.alpaca.markets").mock(
        return_value=httpx.Response(200, json={})
    )
    raw = AlpacaSource(load_config(ROOT / "config.yaml")).fetch(req, http_client())
    assert not route.called and not raw.ok and raw.fetch_status is FetchStatus.ERROR


# ---------------------------------------------------------------------------------------------
# registry / protocol
# ---------------------------------------------------------------------------------------------


def test_alpaca_source_satisfies_protocol(src: AlpacaSource) -> None:
    assert isinstance(src, Source)
    assert src.name == "alpaca" and src.parser_version


def test_registers_itself_at_import(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    import deskbrief.sources.alpaca as mod

    monkeypatch.setattr(registry, "_FACTORIES", {})
    assert registry.registered() == []
    importlib.reload(mod)
    assert registry.registered() == ["alpaca"]
    built = registry.get_sources(config)
    assert [s.name for s in built] == ["alpaca"]
