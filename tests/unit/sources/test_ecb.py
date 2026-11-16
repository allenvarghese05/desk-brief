"""ECB source tests (card T12). No network.

Fixture ``tests/fixtures/synthetic/ecb/exr_d_usd_gbp_jpy.csv`` is a SYNTHETIC FIXTURE modeled
on ECB's documented SDMX CSV; replace with a real capture (see docs/sources/ecb.md).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from deskbrief.config import Config, load_config
from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse, RunContext, Trigger
from deskbrief.schemas import PARSED_OBS_SCHEMA
from deskbrief.sources import registry
from deskbrief.sources.base import ParseError, Source
from deskbrief.sources.ecb import URL, EcbSource

FIXTURE = Path(__file__).parents[2] / "fixtures/synthetic/ecb/exr_d_usd_gbp_jpy.csv"
HEADER = "KEY,CURRENCY,CURRENCY_DENOM,TIME_PERIOD,OBS_VALUE\n"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config()


@pytest.fixture
def src(config: Config) -> EcbSource:
    return EcbSource(config)


def raw_of(body: bytes, status: int = 200, req: FetchRequest | None = None) -> RawResponse:
    return RawResponse(
        request=req or FetchRequest(source="ecb", dataset="eurofxref"),
        fetch_id="f1",
        requested_at=NOW,
        responded_at=NOW,
        http_status=status,
        content_type="text/csv",
        body=body,
    )


def rows(*lines: str) -> bytes:
    return (HEADER + "".join(ln + "\n" for ln in lines)).encode()


def ctx(config: Config, target: date) -> RunContext:
    return RunContext(
        run_id="r1",
        job=JobKind.EOD,
        trigger=Trigger.MANUAL,
        target_date=target,
        now_utc=NOW,
        git_sha="x",
        config_hash="y",
        config=config,
        store=None,  # type: ignore[arg-type]
    )


def by_symbol(df: pd.DataFrame, day: str) -> pd.DataFrame:
    sub = df[df["as_of"] == pd.Timestamp(f"{day} 05:00", tz="UTC")]
    return sub.set_index("symbol")


def test_is_a_source_and_registered(src: EcbSource) -> None:
    assert isinstance(src, Source)
    assert src.name == "ecb"
    assert "ecb" in registry.registered()


def test_golden_parse(src: EcbSource) -> None:
    df = src.parse(raw_of(FIXTURE.read_bytes()))
    PARSED_OBS_SCHEMA.validate(df)
    assert len(df) == 6
    assert set(df["symbol"]) == {"EURUSD", "GBPUSD", "USDJPY"}
    assert (df["source"] == "ecb").all()
    assert (df["dataset"] == "eurofxref").all()
    assert (df["field"] == "rate").all()
    assert (df["unit"] == "fx_rate").all()
    assert (df["interval"] == "1d").all()
    assert (df["tier"] == 3).all()
    assert not df["is_fallback"].any()
    fri = by_symbol(df, "2026-11-13")  # 00:00 EST = 05:00 UTC
    assert fri.loc["EURUSD", "value"] == pytest.approx(1.16)
    assert not fri.loc["EURUSD", "is_derived"]
    assert fri.loc["GBPUSD", "is_derived"]
    assert fri.loc["USDJPY", "is_derived"]


def test_cross_rate_math_against_hand_computation(src: EcbSource) -> None:
    df = src.parse(raw_of(FIXTURE.read_bytes()))
    # 2026-11-13: EURUSD 1.1600, EURGBP 0.8800, EURJPY 180.00
    fri = by_symbol(df, "2026-11-13")
    assert fri.loc["GBPUSD", "value"] == pytest.approx(1.16 / 0.88, rel=1e-12)
    assert fri.loc["GBPUSD", "value"] == pytest.approx(1.3181818181818181, rel=1e-12)
    assert fri.loc["USDJPY", "value"] == pytest.approx(180.0 / 1.16, rel=1e-12)
    assert fri.loc["USDJPY", "value"] == pytest.approx(155.17241379310345, rel=1e-12)
    # 2026-11-12: EURUSD 1.1625, EURGBP 0.8750, EURJPY 181.50
    thu = by_symbol(df, "2026-11-12")
    assert thu.loc["GBPUSD", "value"] == pytest.approx(1.328571428571, rel=1e-9)
    assert thu.loc["USDJPY", "value"] == pytest.approx(156.129032258, rel=1e-9)


def test_as_of_is_midnight_et_in_both_dst_regimes(src: EcbSource) -> None:
    body = rows(
        "k,USD,EUR,2026-07-01,1.10",
        "k,GBP,EUR,2026-07-01,0.85",
        "k,JPY,EUR,2026-07-01,160",
    )
    df = src.parse(raw_of(body))
    assert set(df["as_of"]) == {pd.Timestamp("2026-07-01 04:00", tz="UTC")}  # EDT


def test_unconfigured_symbol_skipped(config: Config) -> None:
    src = EcbSource(config)
    src._feeds = {k: v for k, v in src._feeds.items() if k != "USDJPY"}
    df = src.parse(raw_of(FIXTURE.read_bytes()))
    assert set(df["symbol"]) == {"EURUSD", "GBPUSD"}


def test_extra_currencies_and_other_denominators_ignored(src: EcbSource) -> None:
    body = rows(
        "k,USD,EUR,2026-11-13,1.16",
        "k,GBP,EUR,2026-11-13,0.88",
        "k,JPY,EUR,2026-11-13,180",
        "k,CHF,EUR,2026-11-13,0.93",
        "k,USD,GBP,2026-11-13,9.99",
    )
    df = src.parse(raw_of(body))
    assert len(df) == 3
    assert by_symbol(df, "2026-11-13").loc["EURUSD", "value"] == pytest.approx(1.16)


@pytest.mark.parametrize("missing", ["USD", "GBP", "JPY"])
def test_missing_currency_raises_parse_error(src: EcbSource, missing: str) -> None:
    vals = {"USD": "1.16", "GBP": "0.88", "JPY": "180"}
    body = rows(*(f"k,{c},EUR,2026-11-13,{v}" for c, v in vals.items() if c != missing))
    with pytest.raises(ParseError, match=missing):
        src.parse(raw_of(body))


@pytest.mark.parametrize("body", [b"", b"   \n", HEADER.encode(), b"# only a comment\n"])
def test_empty_payload_gives_empty_frame(src: EcbSource, body: bytes) -> None:
    df = src.parse(raw_of(body))
    assert df.empty
    PARSED_OBS_SCHEMA.validate(df)


@pytest.mark.parametrize(
    "body",
    [
        b"<html>Service Unavailable</html>",
        b"foo,bar\n1,2\n",
        b"\xff\xfe\x00",
        rows("k,USD,EUR,not-a-date,1.16"),
        rows("k,USD,EUR,2026-11-13,abc"),
        rows("k,USD,EUR,2026-11-13,0"),
        rows("k,USD,EUR,2026-11-13,-1.2"),
        rows("k,USD,EUR,2026-11-13,NaN"),
        rows("k,USD,EUR,2026-11-13,1.16", "k,USD,EUR,2026-11-13,1.17"),
    ],
)
def test_malformed_raises_parse_error(src: EcbSource, body: bytes) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_of(body))


class FakeHttp:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status, self.body = status, body
        self.calls: list[tuple[str, Mapping[str, str] | None]] = []

    def get(
        self, req: FetchRequest, url: str, *, params: Mapping[str, str] | None = None, **_: object
    ) -> RawResponse:
        self.calls.append((url, params))
        return raw_of(self.body, self.status, req)


def test_fetch_requests_ecb_url(src: EcbSource, config: Config) -> None:
    (req,) = src.plan(JobKind.EOD, ctx(config, date(2026, 11, 13)))
    http = FakeHttp(200, b"x")
    raw = src.fetch(req, http)
    assert http.calls == [(URL, req.params)]
    assert URL.startswith("https://data-api.ecb.europa.eu/")
    assert raw.fetch_status is FetchStatus.OK


class Fake404(FakeHttp):
    """http client stand-in that sets ``error`` like DefaultHttpClient does for non-2xx."""

    def get(
        self, req: FetchRequest, url: str, *, params: Mapping[str, str] | None = None, **_: object
    ) -> RawResponse:
        raw = super().get(req, url, params=params)
        return dataclasses.replace(raw, error=f"HTTP {self.status}")


def _planned(src: EcbSource, config: Config) -> FetchRequest:
    (req,) = src.plan(JobKind.EOD, ctx(config, date(2026, 11, 13)))
    return req


def test_fetch_404_with_sdmx_no_results_body_is_empty(src: EcbSource, config: Config) -> None:
    # body text UNVERIFIED (from ECB docs); see docs/sources/ecb.md
    body = b"No results found"
    raw = src.fetch(_planned(src, config), Fake404(404, body))
    assert raw.fetch_status is FetchStatus.EMPTY
    assert raw.error is None
    assert raw.body == body  # evidence stored byte for byte


@pytest.mark.parametrize(
    "body",
    [
        b"Not Found",
        b"",
        b'{"error":"unknown dataflow"}',
        b"<html><body>404 Not Found</body></html>",
    ],
)
def test_fetch_404_with_unrelated_body_is_error(
    src: EcbSource, config: Config, body: bytes
) -> None:
    raw = src.fetch(_planned(src, config), Fake404(404, body))
    assert raw.fetch_status is FetchStatus.ERROR
    assert raw.error
    assert raw.body == body


def test_fetch_gateway_html_404_mentioning_no_results_is_error(
    src: EcbSource, config: Config
) -> None:
    body = b"<html><title>404</title><p>No results found for your request</p></html>"
    raw = src.fetch(_planned(src, config), Fake404(404, body))
    assert raw.fetch_status is FetchStatus.ERROR


def test_fetch_404_without_client_error_text_still_errors(src: EcbSource, config: Config) -> None:
    raw = src.fetch(_planned(src, config), FakeHttp(404, b"Not Found"))
    assert raw.fetch_status is FetchStatus.ERROR
    assert raw.error


def test_fetch_server_error_stays_error(src: EcbSource, config: Config) -> None:
    (req,) = src.plan(JobKind.EOD, ctx(config, date(2026, 11, 13)))
    raw = src.fetch(req, FakeHttp(500, b"boom"))
    assert raw.fetch_status is FetchStatus.ERROR


def test_plan_eod_business_day(src: EcbSource, config: Config) -> None:
    (req,) = src.plan(JobKind.EOD, ctx(config, date(2026, 11, 13)))
    assert req.source == "ecb"
    assert req.dataset == "eurofxref"
    assert req.target_date == date(2026, 11, 13)
    assert req.params == {
        "startPeriod": "2026-11-13",
        "endPeriod": "2026-11-13",
        "format": "csvdata",
    }
    assert set(req.symbols) == {"EURUSD", "GBPUSD", "USDJPY"}


@pytest.mark.parametrize(
    "holiday",
    [
        date(2026, 1, 1),  # New Year
        date(2026, 4, 3),  # Good Friday
        date(2026, 4, 6),  # Easter Monday
        date(2026, 5, 1),  # Labour Day
        date(2026, 12, 25),
        date(2026, 12, 26),  # also a Saturday
        date(2026, 11, 14),  # Saturday
    ],
)
def test_plan_skips_target_holidays_and_weekends(
    src: EcbSource, config: Config, holiday: date
) -> None:
    assert src.plan(JobKind.EOD, ctx(config, holiday)) == []
    assert src.plan(JobKind.BACKFILL, ctx(config, holiday)) == []


def test_plan_morning_uses_previous_target_day(src: EcbSource, config: Config) -> None:
    # Monday 2026-11-16 morning -> Friday 13th
    (req,) = src.plan(JobKind.MORNING, ctx(config, date(2026, 11, 16)))
    assert req.target_date == date(2026, 11, 13)
    # Tuesday 2026-04-07 morning (after Easter Monday) -> Thursday 2026-04-02 (Good Friday and
    # Easter Monday are TARGET holidays)
    (req,) = src.plan(JobKind.MORNING, ctx(config, date(2026, 4, 7)))
    assert req.target_date == date(2026, 4, 2)
    # Friday 2026-01-02 morning -> Wednesday 2025-12-31
    (req,) = src.plan(JobKind.MORNING, ctx(config, date(2026, 1, 2)))
    assert req.target_date == date(2025, 12, 31)


def test_plan_intraday_is_empty(src: EcbSource, config: Config) -> None:
    assert src.plan(JobKind.INTRADAY, ctx(config, date(2026, 11, 13))) == []


def test_rows_without_required_currencies_raise(src: EcbSource) -> None:
    with pytest.raises(ParseError, match="none for USD/GBP/JPY"):
        src.parse(raw_of(rows("k,CHF,EUR,2026-11-13,0.93", "k,USD,GBP,2026-11-13,1.3")))


@pytest.mark.parametrize("value", ["1_16", "1e0", "+1.16", " 1.16", "1,16", "inf", "\u0661.16"])
def test_strict_decimal_rejected(src: EcbSource, value: str) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_of(rows(f"k,USD,EUR,2026-11-13,{value}")))


def test_non_finite_cross_raises(src: EcbSource) -> None:
    body = rows(
        "k,USD,EUR,2026-11-13,1.16",
        "k,GBP,EUR,2026-11-13,0." + "0" * 330 + "1",  # underflows to 0.0 -> GBPUSD = inf/zero
        "k,JPY,EUR,2026-11-13,180",
    )
    with pytest.raises(ParseError):
        src.parse(raw_of(body))


def test_cross_overflow_raises(src: EcbSource) -> None:
    body = rows(
        "k,USD,EUR,2026-11-13,0." + "0" * 300 + "1",
        "k,GBP,EUR,2026-11-13,0.88",
        "k,JPY,EUR,2026-11-13," + "9" * 20,  # USDJPY = 1e20 / 1e-301 overflows to inf
    )
    with pytest.raises(ParseError):
        src.parse(raw_of(body))


def test_time_period_outside_requested_window_raises(src: EcbSource, config: Config) -> None:
    req = _planned(src, config)  # window 2026-11-13..2026-11-13
    ok = rows(
        *(f"k,{c},EUR,2026-11-13,{v}" for c, v in (("USD", 1.16), ("GBP", 0.88), ("JPY", 180)))
    )
    assert len(src.parse(raw_of(ok, req=req))) == 3
    bad = rows(
        *(f"k,{c},EUR,2026-11-12,{v}" for c, v in (("USD", 1.16), ("GBP", 0.88), ("JPY", 180)))
    )
    with pytest.raises(ParseError, match="outside requested window"):
        src.parse(raw_of(bad, req=req))
