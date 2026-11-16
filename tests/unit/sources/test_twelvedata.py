# ruff: noqa: PT018
"""Twelve Data source tests (card T15). No network: HTTP is mocked with respx.

Fixtures under tests/fixtures/synthetic/twelvedata are SYNTHETIC FIXTURES (schema-faithful to the
documented ``/quote`` and ``/time_series`` JSON, invented values). The API key below is a
synthetic test value built at runtime.
"""

from __future__ import annotations

import importlib
import io
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from deskbrief.config import Config, SourceSettings, load_config
from deskbrief.logging import configure_logging
from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse, RunContext, Trigger
from deskbrief.schemas import validate
from deskbrief.sources import registry
from deskbrief.sources.base import ParseError, Source
from deskbrief.sources.http import BudgetTracker, DefaultHttpClient, InMemoryBudgetStore
from deskbrief.sources.twelvedata import (
    QUOTE_ENDPOINT,
    SERIES_ENDPOINT,
    TwelveDataSource,
    credits_for,
)

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "tests" / "fixtures" / "synthetic" / "twelvedata"
# Fake key built at runtime from low-entropy pieces, so no credential-shaped literal is committed.
KEY = "-".join(["fake", "twelve", "value"] * 3)
PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY"]


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def src(config: Config) -> TwelveDataSource:
    return TwelveDataSource(config)


def ctx_for(config: Config, job: JobKind = JobKind.MORNING, day: date | None = None) -> RunContext:
    return RunContext(
        run_id="r1",
        job=job,
        trigger=Trigger.SCHEDULE,
        target_date=day or date(2026, 11, 13),
        now_utc=datetime(2026, 11, 13, 22, 30, tzinfo=UTC),
        git_sha="abc",
        config_hash="h",
        config=config,
        store=object(),  # type: ignore[arg-type]
    )


def raw_for(
    dataset: str,
    body: bytes,
    *,
    http_status: int | None = 200,
    error: str | None = None,
    status: FetchStatus | None = None,
    window: tuple[datetime, datetime] | None = None,
) -> RawResponse:
    req = FetchRequest(
        source="twelvedata",
        dataset=dataset,
        params={"symbol": ",".join(PAIRS)},
        symbols=tuple(PAIRS),
        window=window,
    )
    t = datetime(2026, 11, 13, 13, 16, tzinfo=UTC)
    return RawResponse(
        request=req,
        fetch_id="f1",
        requested_at=t,
        responded_at=t,
        http_status=http_status,
        content_type="application/json",
        body=body,
        error=error,
        status=status,
    )


WIN = (datetime(2026, 11, 13, 13, 5, tzinfo=UTC), datetime(2026, 11, 13, 13, 26, tzinfo=UTC))


def quote_raw(name: str = "quote_batch.json") -> RawResponse:
    return raw_for("fx_quote", (FIX / name).read_bytes())


def series_raw(name: str = "series_batch.json") -> RawResponse:
    return raw_for("fx_series_1m", (FIX / name).read_bytes(), window=WIN)


def http_client(
    budget: BudgetTracker | None = None, *, retries: int = 0, daily_budget: int | None = None
) -> DefaultHttpClient:
    return DefaultHttpClient(
        {
            "twelvedata": SourceSettings(
                rate_per_min=6000.0, timeout_s=5.0, retries=retries, daily_budget=daily_budget
            )
        },
        budget=budget,
        sleep=lambda s: None,
    )


# ---------------------------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("job", [JobKind.MORNING, JobKind.INTRADAY])
def test_plan_quote_batch_for_morning_and_intraday(
    src: TwelveDataSource, config: Config, job: JobKind
) -> None:
    reqs = src.plan(job, ctx_for(config, job))
    assert len(reqs) == 1
    r = reqs[0]
    assert r.source == "twelvedata" and r.dataset == "fx_quote"
    assert sorted(r.symbols) == PAIRS
    assert r.params["symbol"] == ",".join(r.symbols)
    assert r.window is None


def test_plan_never_carries_the_key(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    for job in (JobKind.MORNING, JobKind.EOD):
        for r in src.plan(job, ctx_for(config, job)):
            assert not any("key" in k.lower() for k in r.params)
            assert KEY not in repr(r)


def test_plan_eod_series_window_around_fix_winter(src: TwelveDataSource, config: Config) -> None:
    # 2026-11-13: CET = UTC+1 -> fix 14:15 CET = 13:15 UTC
    reqs = src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD))
    assert len(reqs) == 1
    r = reqs[0]
    assert r.dataset == "fx_series_1m" and sorted(r.symbols) == PAIRS
    assert r.window == (
        datetime(2026, 11, 13, 13, 5, tzinfo=UTC),
        datetime(2026, 11, 13, 13, 26, tzinfo=UTC),  # end exclusive: the 13:25 bar is included
    )
    assert r.params["interval"] == "1min"
    assert r.params["timezone"] == "UTC"
    assert r.params["start_date"] == "2026-11-13 13:05:00"
    assert r.params["end_date"] == "2026-11-13 13:25:00"
    assert r.params["order"] == "asc"
    assert int(r.params["outputsize"]) >= 21


@pytest.mark.parametrize(
    ("day", "start_utc"),
    [
        (date(2026, 7, 15), "12:05"),  # CEST (UTC+2): 14:15 -> 12:15 UTC
        (date(2026, 3, 27), "13:05"),  # Friday before the EU switch (29 Mar): CET
        (date(2026, 3, 30), "12:05"),  # Monday after: CEST
        (date(2026, 10, 23), "12:05"),  # Friday before the EU switch back (25 Oct): CEST
        (date(2026, 10, 26), "13:05"),  # Monday after: CET
    ],
)
def test_plan_eod_window_follows_cet_dst(
    src: TwelveDataSource, config: Config, day: date, start_utc: str
) -> None:
    (r,) = src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD, day))
    assert r.window is not None
    assert r.window[0].strftime("%H:%M") == start_utc
    assert r.params["start_date"] == f"{day.isoformat()} {start_utc}:00"
    assert (r.window[1] - r.window[0]).total_seconds() == 21 * 60


@pytest.mark.parametrize("day", [date(2026, 12, 25), date(2026, 12, 26), date(2026, 11, 14)])
def test_plan_eod_skips_non_target_days(src: TwelveDataSource, config: Config, day: date) -> None:
    # TARGET closed: Christmas, 26 Dec, and a Saturday -> nothing planned (skip, not fail)
    assert src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD, day)) == []


@pytest.mark.parametrize("job", [JobKind.BACKFILL, JobKind.REBUILD])
def test_plan_nothing_for_backfill_and_rebuild(
    src: TwelveDataSource, config: Config, job: JobKind
) -> None:
    assert src.plan(job, ctx_for(config, job)) == []


def test_plan_empty_without_configured_feeds(config: Config) -> None:
    src = TwelveDataSource(config)
    src._quote, src._series = {}, {}
    assert src.plan(JobKind.MORNING, ctx_for(config)) == []
    assert src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD)) == []


# ---------------------------------------------------------------------------------------------
# parse: fx_quote
# ---------------------------------------------------------------------------------------------


def test_parse_quote_batch(src: TwelveDataSource) -> None:
    df = src.parse(quote_raw())
    validate("parsed_observations", df)
    assert set(df["instrument_id"]) == {"EURUSD", "GBPUSD", "USDJPY"}
    assert set(df["interval"]) == {"tick"} and set(df["unit"]) == {"fx_rate"}
    assert set(df["tier"]) == {1} and set(df["source"]) == {"twelvedata"}
    assert set(df["dataset"]) == {"fx_quote"}
    assert not df["is_derived"].any() and not df["is_fallback"].any()
    last = df[df["field"] == "last"].set_index("symbol")["value"]
    assert last["EUR/USD"] == pytest.approx(1.0842)
    assert last["GBP/USD"] == pytest.approx(1.2671)
    assert last["USD/JPY"] == pytest.approx(154.52)
    # as_of = the quote's own unix timestamp (UTC), not the fetch time
    eur = df[(df["symbol"] == "EUR/USD") & (df["field"] == "last")].iloc[0]
    assert eur["as_of"] == datetime(2026, 11, 13, 13, 15, tzinfo=UTC)


def test_parse_quote_emits_bid_ask_only_when_present(src: TwelveDataSource) -> None:
    df = src.parse(quote_raw())
    extra = df[df["field"].isin(["bid", "ask"])]
    assert set(extra["symbol"]) == {"EUR/USD"} and set(extra["field"]) == {"bid", "ask"}
    assert extra.set_index("field")["value"]["bid"] == pytest.approx(1.08418)
    assert extra.set_index("field")["value"]["ask"] == pytest.approx(1.08422)
    assert len(df) == 3 + 2


def test_parse_quote_single_symbol_unnested(src: TwelveDataSource) -> None:
    body = json.dumps(json.loads((FIX / "quote_batch.json").read_bytes())["GBP/USD"]).encode()
    df = src.parse(raw_for("fx_quote", body))
    assert list(df["symbol"]) == ["GBP/USD"] and list(df["field"]) == ["last"]


def test_parse_quote_unconfigured_symbol_ignored(src: TwelveDataSource) -> None:
    doc = json.loads((FIX / "quote_batch.json").read_bytes())
    doc["XAU/USD"] = dict(doc["GBP/USD"], symbol="XAU/USD")
    df = src.parse(raw_for("fx_quote", json.dumps(doc).encode()))
    assert "XAU/USD" not in set(df["symbol"]) and len(df) == 5


def test_parse_applies_feed_scale(config: Config) -> None:
    src = TwelveDataSource(config)
    inst, feed = src._quote["USD/JPY"]
    src._quote["USD/JPY"] = (inst, feed.model_copy(update={"scale": 0.01}))
    df = src.parse(quote_raw())
    jpy = df[(df["symbol"] == "USD/JPY") & (df["field"] == "last")].iloc[0]
    assert jpy["value"] == pytest.approx(1.5452)


def test_parse_quote_partial_batch_keeps_good_symbols(
    src: TwelveDataSource, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    df = src.parse(quote_raw("quote_partial.json"))
    validate("parsed_observations", df)
    assert set(df["symbol"]) == {"EUR/USD", "USD/JPY"} and len(df) == 2
    logged = stream.getvalue()
    assert "twelvedata.parse.symbol_error" in logged and "GBP/USD" in logged


@pytest.mark.parametrize(
    "bad",
    [
        {"symbol": "GBP/USD", "timestamp": 1794575701, "close": "abc"},
        {"symbol": "GBP/USD", "timestamp": 1794575701, "close": "nan"},
        {"symbol": "GBP/USD", "timestamp": 1794575701, "close": "-1.2"},
        {"symbol": "GBP/USD", "timestamp": 1794575701},
        {"symbol": "GBP/USD", "close": "1.2671"},  # no timestamp: cannot place it in time
        {"symbol": "GBP/USD", "timestamp": "soon", "close": "1.2671"},
        "not an object",
    ],
)
def test_parse_quote_bad_symbol_entry_is_dropped_not_fatal(src: TwelveDataSource, bad: Any) -> None:
    doc = json.loads((FIX / "quote_batch.json").read_bytes())
    doc["GBP/USD"] = bad
    df = src.parse(raw_for("fx_quote", json.dumps(doc).encode()))
    assert set(df["symbol"]) == {"EUR/USD", "USD/JPY"}


@pytest.mark.parametrize("dataset", ["fx_quote", "fx_series_1m"])
@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"status": "ok"}',
        b'{"foo": {"bar": 1}, "baz": 2}',
        b'{"XAU/USD": {"symbol": "XAU/USD", "timestamp": 1794575700, "close": "2400.1"}}',
    ],
    ids=["empty-object", "status-only", "unrelated", "only-unconfigured-symbols"],
)
def test_parse_response_without_any_requested_symbol_is_parse_error(
    src: TwelveDataSource, dataset: str, body: bytes
) -> None:
    with pytest.raises(ParseError, match="no usable"):
        src.parse(raw_for(dataset, body, window=WIN))


def test_parse_quote_all_symbols_failed_is_parse_error(src: TwelveDataSource) -> None:
    err = {"code": 400, "message": "nope", "status": "error"}
    body = json.dumps({p: err for p in PAIRS}).encode()
    with pytest.raises(ParseError, match="no usable"):
        src.parse(raw_for("fx_quote", body))


# ---------------------------------------------------------------------------------------------
# parse: fx_series_1m
# ---------------------------------------------------------------------------------------------


def test_parse_series_window_close_only(src: TwelveDataSource) -> None:
    df = src.parse(series_raw())
    validate("parsed_observations", df)
    # 13:04 and 13:26 (EUR/USD) fall outside [13:05, 13:26) and are dropped
    assert len(df) == 9
    assert set(df["field"]) == {"close"} and set(df["interval"]) == {"1m"}
    assert set(df["unit"]) == {"fx_rate"} and set(df["tier"]) == {1}
    assert set(df["dataset"]) == {"fx_series_1m"}
    eur = df[df["symbol"] == "EUR/USD"].sort_values("as_of")
    assert list(eur["as_of"]) == [
        datetime(2026, 11, 13, 13, 5, tzinfo=UTC),
        datetime(2026, 11, 13, 13, 15, tzinfo=UTC),  # bar START = 14:15 CET
        datetime(2026, 11, 13, 13, 25, tzinfo=UTC),
    ]
    assert list(eur["value"]) == pytest.approx([1.08315, 1.08415, 1.08515])
    fix_bar = df[
        (df["symbol"] == "USD/JPY") & (df["as_of"] == datetime(2026, 11, 13, 13, 15, tzinfo=UTC))
    ]
    assert fix_bar["value"].iloc[0] == pytest.approx(154.525)


def test_parse_series_without_window_keeps_all_bars(src: TwelveDataSource) -> None:
    df = src.parse(raw_for("fx_series_1m", (FIX / "series_batch.json").read_bytes()))
    assert len(df) == 11


def test_parse_series_single_symbol_unnested(src: TwelveDataSource) -> None:
    one = json.loads((FIX / "series_batch.json").read_bytes())["GBP/USD"]
    req = FetchRequest(
        source="twelvedata", dataset="fx_series_1m", params={}, symbols=("GBP/USD",), window=WIN
    )
    t = datetime(2026, 11, 13, 13, 30, tzinfo=UTC)
    raw = RawResponse(
        request=req,
        fetch_id="f",
        requested_at=t,
        responded_at=t,
        http_status=200,
        content_type="application/json",
        body=json.dumps(one).encode(),
    )
    df = src.parse(raw)
    assert set(df["symbol"]) == {"GBP/USD"} and len(df) == 3


def test_parse_series_partial_batch(src: TwelveDataSource) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    doc = json.loads((FIX / "series_batch.json").read_bytes())
    doc["USD/JPY"] = {"code": 400, "message": "No data is available (synthetic)", "status": "error"}
    df = src.parse(raw_for("fx_series_1m", json.dumps(doc).encode(), window=WIN))
    assert set(df["symbol"]) == {"EUR/USD", "GBP/USD"}
    assert "twelvedata.parse.symbol_error" in stream.getvalue()


def test_parse_series_empty_values_is_empty_frame(src: TwelveDataSource) -> None:
    doc = {
        p: {"meta": {"symbol": p, "interval": "1min"}, "values": [], "status": "ok"} for p in PAIRS
    }
    df = src.parse(raw_for("fx_series_1m", json.dumps(doc).encode(), window=WIN))
    assert df.empty
    validate("parsed_observations", df)


def test_parse_series_all_symbols_failed_is_parse_error(src: TwelveDataSource) -> None:
    err = {"code": 400, "message": "nope", "status": "error"}
    with pytest.raises(ParseError, match="no usable"):
        src.parse(raw_for("fx_series_1m", json.dumps({p: err for p in PAIRS}).encode()))


@pytest.mark.parametrize(
    "bar",
    [
        {"datetime": "2026-11-13 13:15:00", "close": "x"},
        {"datetime": "2026-11-13 13:15:00"},
        {"datetime": "yesterday", "close": "1.1"},
        {"close": "1.1"},
        "bar",
    ],
)
def test_parse_series_malformed_bar_raises(src: TwelveDataSource, bar: Any) -> None:
    doc = {"EUR/USD": {"meta": {}, "values": [bar], "status": "ok"}}
    with pytest.raises(ParseError):
        src.parse(raw_for("fx_series_1m", json.dumps(doc).encode(), window=WIN))


# ---------------------------------------------------------------------------------------------
# parse: errors and malformed bodies
# ---------------------------------------------------------------------------------------------


def test_parse_200_error_payload_raises(src: TwelveDataSource) -> None:
    # replaying a stored raw file whose body is a provider error with HTTP 200
    with pytest.raises(ParseError, match="401"):
        src.parse(raw_for("fx_quote", (FIX / "error_200.json").read_bytes()))


def test_parse_failed_fetch_raises(src: TwelveDataSource) -> None:
    with pytest.raises(ParseError, match="fetch failed"):
        src.parse(raw_for("fx_quote", b"", http_status=500, error="HTTP 500"))


@pytest.mark.parametrize("body", [b"", b"<html>", b"[1, 2]", b"3", b"\xff\xfe"])
def test_parse_malformed_body_raises(src: TwelveDataSource, body: bytes) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for("fx_quote", body))


def test_parse_unknown_dataset_raises(src: TwelveDataSource) -> None:
    with pytest.raises(ParseError, match="dataset"):
        src.parse(raw_for("nope", b"{}"))


# ---------------------------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------------------------


@respx.mock
def test_fetch_quote_sends_key_on_the_wire_only(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    stream = io.StringIO()
    configure_logging(stream=stream)
    route = respx.get(QUOTE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            content=(FIX / "quote_batch.json").read_bytes(),
            headers={"content-type": "application/json"},
        )
    )
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    raw = src.fetch(req, http_client())

    sent = route.calls.last.request
    assert sent.url.params["apikey"] == KEY
    assert sent.url.params["symbol"] == ",".join(req.symbols)
    assert raw.ok and raw.fetch_status is FetchStatus.OK
    assert KEY not in repr(req) and KEY not in str(dict(req.params))
    assert raw.url_redacted is not None and KEY not in raw.url_redacted
    assert KEY not in repr(raw.request) and (raw.error is None or KEY not in raw.error)
    assert KEY.encode() not in raw.body
    assert KEY not in stream.getvalue()
    assert len(src.parse(raw)) == 5


@respx.mock
def test_fetch_series_hits_time_series_endpoint(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    route = respx.get(SERIES_ENDPOINT).mock(
        return_value=httpx.Response(200, content=(FIX / "series_batch.json").read_bytes())
    )
    (req,) = src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD))
    raw = src.fetch(req, http_client())
    params = route.calls.last.request.url.params
    assert params["apikey"] == KEY and params["interval"] == "1min"
    assert params["start_date"] == "2026-11-13 13:05:00"
    assert raw.ok
    assert len(src.parse(raw)) == 9


@respx.mock
def test_fetch_http_error_does_not_leak_key(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    stream = io.StringIO()
    configure_logging(stream=stream)
    respx.get(QUOTE_ENDPOINT).mock(return_value=httpx.Response(401, json={"code": 401}))
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    raw = src.fetch(req, http_client())
    assert raw.fetch_status is FetchStatus.ERROR and raw.http_status == 401
    assert KEY not in (raw.error or "") and KEY not in (raw.url_redacted or "")
    assert KEY not in stream.getvalue()


@respx.mock
def test_fetch_transport_error_does_not_leak_key(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    stream = io.StringIO()
    configure_logging(stream=stream)
    respx.get(QUOTE_ENDPOINT).mock(side_effect=httpx.ConnectError(f"boom apikey={KEY}"))
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    raw = src.fetch(req, http_client())
    assert raw.fetch_status is FetchStatus.ERROR
    assert KEY not in (raw.error or "") and KEY not in stream.getvalue()


@respx.mock
def test_fetch_200_with_error_payload_becomes_error_status(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    stream = io.StringIO()
    configure_logging(stream=stream)
    respx.get(QUOTE_ENDPOINT).mock(
        return_value=httpx.Response(200, content=(FIX / "error_200.json").read_bytes())
    )
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    raw = src.fetch(req, http_client())
    assert raw.http_status == 200 and not raw.ok
    assert raw.fetch_status is FetchStatus.ERROR
    assert raw.error is not None and "401" in raw.error and "apikey" in raw.error
    assert raw.body == (FIX / "error_200.json").read_bytes()  # stored byte-for-byte
    assert raw.request is req and raw.fetch_id
    assert KEY not in stream.getvalue()
    with pytest.raises(ParseError):
        src.parse(raw)


@respx.mock
def test_fetch_200_error_429_is_rate_limited(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    body = {
        "code": 429,
        "message": "You have run out of API credits (synthetic)",
        "status": "error",
    }
    respx.get(QUOTE_ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    raw = src.fetch(req, http_client())
    assert raw.fetch_status is FetchStatus.RATE_LIMITED and not raw.ok


@respx.mock
def test_fetch_200_error_message_echoing_key_is_masked(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    body = {"code": 401, "message": f"bad apikey {KEY}", "status": "error"}
    respx.get(QUOTE_ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    raw = src.fetch(req, http_client())
    assert raw.error is not None and KEY not in raw.error


@respx.mock
def test_fetch_partial_batch_stays_ok(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    respx.get(QUOTE_ENDPOINT).mock(
        return_value=httpx.Response(200, content=(FIX / "quote_partial.json").read_bytes())
    )
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    raw = src.fetch(req, http_client())
    assert raw.ok and raw.fetch_status is FetchStatus.OK
    assert len(src.parse(raw)) == 2


@respx.mock
@pytest.mark.parametrize("value", [None, "", "   "])
def test_fetch_without_key_returns_error_response_and_makes_no_request(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    else:
        monkeypatch.setenv("TWELVEDATA_API_KEY", value)
    route = respx.get(QUOTE_ENDPOINT).mock(return_value=httpx.Response(200, json={}))
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    raw = src.fetch(req, http_client())
    assert not route.called
    assert not raw.ok and raw.fetch_status is FetchStatus.ERROR
    assert raw.error is not None and "TWELVEDATA_API_KEY" in raw.error
    assert raw.http_status is None and raw.body == b""
    assert raw.request is req


@respx.mock
def test_fetch_key_is_stripped(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", f"  {KEY}\n")
    route = respx.get(QUOTE_ENDPOINT).mock(
        return_value=httpx.Response(200, content=(FIX / "quote_batch.json").read_bytes())
    )
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    src.fetch(req, http_client())
    assert route.calls.last.request.url.params["apikey"] == KEY


# ---------------------------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------------------------


def tracker(used: int, limit: int) -> tuple[BudgetTracker, InMemoryBudgetStore]:
    store = InMemoryBudgetStore()
    day = datetime.now(UTC).date()
    store.save("twelvedata", day, used)
    return BudgetTracker(store, {"twelvedata": limit}), store


def test_credits_for_counts_one_credit_per_symbol(src: TwelveDataSource, config: Config) -> None:
    (quote,) = src.plan(JobKind.MORNING, ctx_for(config))
    (series,) = src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD))
    assert credits_for(quote) == 3 and credits_for(series) == 3
    assert credits_for(FetchRequest(source="twelvedata", dataset="fx_quote")) == 1


@respx.mock
def test_budget_exhausted_makes_no_http_call(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    route = respx.get(QUOTE_ENDPOINT).mock(return_value=httpx.Response(200, json={}))
    budget, store = tracker(used=750, limit=750)
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    raw = src.fetch(req, http_client(budget, daily_budget=750))
    assert not route.called
    assert raw.fetch_status is FetchStatus.BUDGET_EXHAUSTED and not raw.ok
    assert raw.error is not None and "budget" in raw.error
    assert store.load("twelvedata", datetime.now(UTC).date()) == 750  # nothing consumed
    with pytest.raises(ParseError):
        src.parse(raw)


@respx.mock
def test_budget_known_gap_batch_counts_one_tracked_unit(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KNOWN GAP (documented in docs/sources/twelvedata.md): the T03 tracker reserves one unit
    per HTTP attempt and the HttpClient protocol has no weight argument, so a 3-symbol batch is
    tracked as 1 although Twelve Data bills 3 credits (``credits_for``). This test pins the
    current behaviour so that fixing it (a contract PR) has to update it deliberately."""
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    respx.get(QUOTE_ENDPOINT).mock(
        return_value=httpx.Response(200, content=(FIX / "quote_batch.json").read_bytes())
    )
    budget, store = tracker(used=0, limit=750)
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    src.fetch(req, http_client(budget, daily_budget=750))
    assert store.load("twelvedata", datetime.now(UTC).date()) == 1
    assert credits_for(req) == 3


@respx.mock
def test_budget_last_unit_then_exhausted(
    src: TwelveDataSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", KEY)
    route = respx.get(QUOTE_ENDPOINT).mock(
        return_value=httpx.Response(200, content=(FIX / "quote_batch.json").read_bytes())
    )
    budget, _ = tracker(used=749, limit=750)
    client = http_client(budget, daily_budget=750)
    (req,) = src.plan(JobKind.MORNING, ctx_for(config))
    assert src.fetch(req, client).ok
    assert src.fetch(req, client).fetch_status is FetchStatus.BUDGET_EXHAUSTED
    assert route.call_count == 1


# ---------------------------------------------------------------------------------------------
# registry / protocol
# ---------------------------------------------------------------------------------------------


def test_source_satisfies_protocol(src: TwelveDataSource) -> None:
    assert isinstance(src, Source)
    assert src.name == "twelvedata" and src.parser_version


def test_registered_under_twelvedata(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    import deskbrief.sources.twelvedata as mod

    monkeypatch.setattr(registry, "_FACTORIES", {})
    importlib.reload(mod)
    assert registry.registered() == ["twelvedata"]
    built = registry.get_sources(config)
    assert [s.name for s in built] == ["twelvedata"]
