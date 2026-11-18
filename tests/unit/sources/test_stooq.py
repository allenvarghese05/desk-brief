"""Stooq source tests (card T13). Fixtures are SYNTHETIC (tests/fixtures/synthetic/stooq)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pytest
import respx

from config_support import enable_all_sources
from deskbrief.config import Config, load_config
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext, Trigger
from deskbrief.schemas import columns, validate
from deskbrief.sources import registry
from deskbrief.sources.base import ParseError, Source
from deskbrief.sources.http import DefaultHttpClient
from deskbrief.sources.stooq import StooqSource

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic" / "stooq" / "spy_us_daily.csv"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def config() -> Config:
    return enable_all_sources(load_config(ROOT / "config.yaml"))


@pytest.fixture
def src(config: Config) -> StooqSource:
    return StooqSource(config)


def ctx(config: Config, target: date = date(2026, 11, 16)) -> RunContext:
    return RunContext(
        run_id="R",
        job=JobKind.MORNING,
        trigger=Trigger.MANUAL,
        target_date=target,
        now_utc=NOW,
        git_sha="x",
        config_hash="h",
        config=config,
        store=cast(Any, None),
    )


def raw_for(
    body: bytes,
    symbol: str = "spy.us",
    status: int | None = 200,
    error: str | None = None,
) -> RawResponse:
    req = FetchRequest("stooq", "daily", params={"s": symbol}, symbols=(symbol,))
    return RawResponse(
        request=req,
        fetch_id="F",
        requested_at=NOW,
        responded_at=NOW,
        http_status=status,
        content_type="text/csv",
        body=body,
        error=error,
    )


def test_plan_one_request_per_configured_symbol(src: StooqSource, config: Config) -> None:
    reqs = src.plan(JobKind.MORNING, ctx(config))
    symbols = [r.symbols[0] for r in reqs]
    assert symbols == list(config.feed_index("stooq", "daily"))
    assert "spy.us" in symbols
    assert "aapl.us" in symbols
    first = reqs[0]
    assert first.params["i"] == "d"
    assert first.params["d2"] == "20261116"
    assert first.params["d1"] == "20261106"
    assert first.target_date == date(2026, 11, 16)


def test_plan_skips_intraday(src: StooqSource, config: Config) -> None:
    assert src.plan(JobKind.INTRADAY, ctx(config)) == []


def test_golden_parse(src: StooqSource) -> None:
    df = src.parse(raw_for(FIXTURE.read_bytes()))
    validate("parsed_observations", df)
    assert list(df.columns) == list(columns("parsed_observations"))
    assert len(df) == 4 * 5
    close = df[df["field"] == "close"].sort_values("as_of")
    assert close["value"].tolist() == [602.40, 601.15, 605.10, 602.80]
    assert set(close["instrument_id"]) == {"SPY"}
    assert set(close["tier"]) == {3}
    assert set(close["unit"]) == {"USD"}
    assert set(close["interval"]) == {"1d"}
    assert not close["is_derived"].any()
    assert not close["is_fallback"].any()
    # P-006: 00:00 America/New_York on the reference date (EST in November = 05:00 UTC)
    assert close["as_of"].iloc[0] == pd.Timestamp("2026-11-09 05:00", tz="UTC")
    vol = df[(df["field"] == "volume")].sort_values("as_of")
    assert vol["value"].iloc[0] == 51234567
    assert set(vol["unit"]) == {"shares"}
    assert set(df["field"]) == {"open", "high", "low", "close", "volume"}


def test_unconfigured_symbol_yields_nothing(src: StooqSource) -> None:
    df = src.parse(raw_for(FIXTURE.read_bytes(), symbol="zzz.us"))
    assert df.empty
    validate("parsed_observations", df)


@pytest.mark.parametrize("body", [b"", b"\n  \n", b"Date,Open,High,Low,Close,Volume\n"])
def test_empty_cases(src: StooqSource, body: bytes) -> None:
    df = src.parse(raw_for(body))
    assert df.empty
    validate("parsed_observations", df)


@pytest.mark.parametrize(
    "body",
    [
        b"No data",
        b"Exceeded the daily hits limit",
        b"<!DOCTYPE html><html><body>Get your apikey</body></html>",
        b"Date,Open,High,Low,Close\n2026-11-09,1,2,3,4\n",
        b"Date,Open,High,Low,Close,Volume\n2026-11-09,1,2,3,4\n",
        b"Date,Open,High,Low,Close,Volume\nnot-a-date,1,2,3,4,5\n",
        b"Date,Open,High,Low,Close,Volume\n2026-11-09,1,2,abc,4,5\n",
        b"Date,Open,High,Low,Close,Volume\n2026-11-09,1,2,3,nan,5\n",
        b"\xff\xfe\x00bad",
    ],
)
def test_malformed_raises_parse_error(src: StooqSource, body: bytes) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for(body))


def test_failed_fetch_raises_parse_error(src: StooqSource) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for(b"", status=503, error="HTTP 503"))
    with pytest.raises(ParseError):
        src.parse(raw_for(b"", status=None, error="ConnectError"))


def test_scale_applied(config: Config) -> None:
    data = config.model_dump()
    for inst in data["instruments"]:
        for feed in inst["feeds"]:
            if feed["symbol"] == "spy.us":
                feed["scale"] = 0.1
    scaled = StooqSource(Config.model_validate(data))
    df = scaled.parse(raw_for(FIXTURE.read_bytes()))
    close = df[df["field"] == "close"].sort_values("as_of")
    assert close["value"].iloc[0] == pytest.approx(60.24)
    assert df[df["field"] == "volume"]["value"].iloc[0] == 51234567  # volume is not scaled


def test_fetch_uses_http_client_with_params(src: StooqSource, config: Config) -> None:
    req = src.plan(JobKind.MORNING, ctx(config))[0]
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://stooq.com/q/d/l/").respond(200, content=FIXTURE.read_bytes())
        with DefaultHttpClient(config.sources) as http:
            raw = src.fetch(req, http)
    assert raw.ok
    assert route.calls[0].request.url.params["s"] == req.symbols[0]
    assert route.calls[0].request.url.params["d2"] == "20261116"


def test_registered_and_conforms(config: Config) -> None:
    registry.clear()
    registry.register("stooq", StooqSource)
    built = registry.get_sources(config)
    assert [s.name for s in built] == ["stooq"]
    assert isinstance(built[0], Source)


def test_csv_error_becomes_parse_error(src: StooqSource, monkeypatch: pytest.MonkeyPatch) -> None:
    """T08 (PR #11 finding 5): csv.Error from the reader is a ParseError."""
    import csv

    def boom(*_a: object, **_k: object) -> object:
        raise csv.Error("field larger than field limit (131072)")

    monkeypatch.setattr("deskbrief.sources.stooq.csv.reader", boom)
    with pytest.raises(ParseError):
        src.parse(raw_for(FIXTURE.read_bytes()))


def test_oversized_field_is_a_parse_error(src: StooqSource) -> None:
    huge = b"x" * 200_000
    body = b"Date,Open,High,Low,Close,Volume\n2026-11-09,1,2,3,4," + huge + b"\n"
    with pytest.raises(ParseError):
        src.parse(raw_for(body))
