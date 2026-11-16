# ruff: noqa: PT018
"""Treasury par yield curve source tests (card T10). No network.

The CSV fixture is SYNTHETIC (modeled on Treasury's documented format) until a real capture
replaces it; see docs/sources/treasury.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from deskbrief.config import Config, load_config
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext, Trigger
from deskbrief.schemas import validate
from deskbrief.sources import registry
from deskbrief.sources.base import ParseError, Source
from deskbrief.sources.treasury import TreasurySource

FIXTURE = Path(__file__).parents[2] / "fixtures" / "synthetic" / "treasury" / "par_curve_202611.csv"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config()


@pytest.fixture
def src(config: Config) -> TreasurySource:
    return TreasurySource(config)


def ctx(config: Config, job: JobKind, target: date) -> RunContext:
    return RunContext(
        run_id="r",
        job=job,
        trigger=Trigger.MANUAL,
        target_date=target,
        now_utc=NOW,
        git_sha="x",
        config_hash="h",
        config=config,
        store=object(),  # type: ignore[arg-type]
    )


def raw(body: bytes, *, http_status: int | None = 200) -> RawResponse:
    req = FetchRequest("treasury", "par_curve", params={"month": "202611"})
    return RawResponse(
        request=req,
        fetch_id="f1",
        requested_at=NOW,
        responded_at=NOW,
        http_status=http_status,
        content_type="text/csv",
        body=body,
    )


# ---------------------------------------------------------------- plan


def months(reqs: list[FetchRequest]) -> list[str]:
    return [r.params["month"] for r in reqs]


def test_plan_morning_uses_previous_business_day(src: TreasurySource, config: Config) -> None:
    # Monday 2026-11-16 -> Friday 11-13
    reqs = src.plan(JobKind.MORNING, ctx(config, JobKind.MORNING, date(2026, 11, 16)))
    assert months(reqs) == ["202611"]
    assert reqs[0].target_date == date(2026, 11, 13)
    assert reqs[0].source == "treasury" and reqs[0].dataset == "par_curve"
    assert set(reqs[0].symbols) == {"3 Mo", "2 Yr", "10 Yr", "20 Yr", "30 Yr"}


def test_plan_morning_after_thanksgiving_skips_holiday(src: TreasurySource, config: Config) -> None:
    reqs = src.plan(JobKind.MORNING, ctx(config, JobKind.MORNING, date(2026, 11, 27)))
    assert reqs[0].target_date == date(2026, 11, 25)


def test_plan_morning_crosses_month_boundary(src: TreasurySource, config: Config) -> None:
    reqs = src.plan(JobKind.MORNING, ctx(config, JobKind.MORNING, date(2026, 1, 2)))
    assert months(reqs) == ["202512"]
    assert reqs[0].target_date == date(2025, 12, 31)


def test_plan_eod_today(src: TreasurySource, config: Config) -> None:
    reqs = src.plan(JobKind.EOD, ctx(config, JobKind.EOD, date(2026, 11, 16)))
    assert months(reqs) == ["202611"]
    assert reqs[0].target_date == date(2026, 11, 16)


@pytest.mark.parametrize(
    "d", [date(2026, 11, 14), date(2026, 11, 15), date(2026, 11, 26), date(2026, 12, 25)]
)
def test_plan_eod_weekend_or_holiday_is_skipped(
    src: TreasurySource, config: Config, d: date
) -> None:
    assert src.plan(JobKind.EOD, ctx(config, JobKind.EOD, d)) == []


def test_plan_backfill_month_of_target(src: TreasurySource, config: Config) -> None:
    reqs = src.plan(JobKind.BACKFILL, ctx(config, JobKind.BACKFILL, date(2026, 11, 20)))
    assert months(reqs) == ["202611"]


def test_plan_other_jobs_empty(src: TreasurySource, config: Config) -> None:
    assert src.plan(JobKind.INTRADAY, ctx(config, JobKind.INTRADAY, date(2026, 11, 16))) == []
    assert src.plan(JobKind.REBUILD, ctx(config, JobKind.REBUILD, date(2026, 11, 16))) == []


def test_plan_range_one_request_per_month(src: TreasurySource) -> None:
    reqs = src.plan_range(date(2026, 10, 15), date(2027, 1, 3))
    assert months(reqs) == ["202610", "202611", "202612", "202701"]
    assert reqs[0].target_date == date(2026, 10, 15)
    assert reqs[1].target_date == date(2026, 11, 1)


def test_plan_range_single_day_and_invalid(src: TreasurySource) -> None:
    assert months(src.plan_range(date(2026, 11, 5), date(2026, 11, 5))) == ["202611"]
    with pytest.raises(ValueError, match="end must not"):
        src.plan_range(date(2026, 11, 5), date(2026, 11, 4))


# ---------------------------------------------------------------- fetch


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[FetchRequest, str, Mapping[str, str] | None]] = []

    def get(self, req: FetchRequest, url: str, **kw: Any) -> RawResponse:
        self.calls.append((req, url, kw.get("params")))
        return raw(b"")


def test_fetch_builds_month_url_without_secrets(src: TreasurySource) -> None:
    http = FakeHttp()
    req = src.plan_range(date(2026, 11, 1), date(2026, 11, 30))[0]
    src.fetch(req, http)
    (r, url, params) = http.calls[0]
    assert r is req
    assert url.startswith("https://home.treasury.gov/")
    assert "/2026/" in url
    assert params is not None
    assert params["field_tdr_date_value_month"] == "202611"
    assert params["type"] == "daily_treasury_yield_curve"
    assert params["_format"] == "csv"


# ---------------------------------------------------------------- parse


def test_golden_parse_synthetic_fixture(src: TreasurySource) -> None:
    df = src.parse(raw(FIXTURE.read_bytes()))
    validate("parsed_observations", df)
    # 3 dates x 5 configured tenors, minus the 20 Yr blank on 11/10
    assert len(df) == 14
    assert set(df["source"]) == {"treasury"} and set(df["dataset"]) == {"par_curve"}
    assert set(df["field"]) == {"yield"} and set(df["unit"]) == {"pct"}
    assert set(df["interval"]) == {"1d"} and set(df["tier"]) == {3}
    assert not df["is_derived"].any() and not df["is_fallback"].any()
    assert set(df["symbol"]) == {"3 Mo", "2 Yr", "10 Yr", "20 Yr", "30 Yr"}  # 1.5 Month ignored
    row = df[
        (df["instrument_id"] == "UST_10Y") & (df["as_of"] == pd.Timestamp("2026-11-13 05:00Z"))
    ]
    assert row["value"].iloc[0] == pytest.approx(4.25)
    # 1d as_of = 00:00 America/New_York (EST in November -> 05:00Z)
    assert df["as_of"].dt.hour.eq(5).all()
    blank = df[
        (df["instrument_id"] == "UST_20Y") & (df["as_of"] == pd.Timestamp("2026-11-10 05:00Z"))
    ]
    assert blank.empty


def test_as_of_respects_dst(src: TreasurySource) -> None:
    body = b"Date,10 Yr\n07/01/2026,4.00\n"
    df = src.parse(raw(body))
    assert df["as_of"].iloc[0] == pd.Timestamp("2026-07-01 04:00Z")


def test_header_case_whitespace_and_unknown_columns(src: TreasurySource) -> None:
    body = b'\xef\xbb\xbf" DATE ", 10  yr ,"Mystery"\n"11/13/2026"," 4.25 ","x"\n'
    df = src.parse(raw(body))
    assert len(df) == 1 and df["symbol"].iloc[0] == "10 Yr"
    assert df["value"].iloc[0] == pytest.approx(4.25)


def test_iso_dates_and_na_cells(src: TreasurySource) -> None:
    df = src.parse(raw(b"Date,10 Yr,2 Yr\n2026-11-13,N/A,3.7\n"))
    assert list(df["symbol"]) == ["2 Yr"]


def test_scale_applied(config: Config) -> None:
    feeds = [
        f.model_copy(update={"scale": 0.1}) if f.source == "treasury" else f
        for inst in config.instruments
        for f in inst.feeds
    ]
    assert feeds  # sanity
    insts = tuple(
        i.model_copy(
            update={
                "feeds": tuple(
                    f.model_copy(update={"scale": 0.1}) if f.source == "treasury" else f
                    for f in i.feeds
                )
            }
        )
        for i in config.instruments
    )
    scaled = TreasurySource(config.model_copy(update={"instruments": insts}))
    df = scaled.parse(raw(b"Date,10 Yr\n11/13/2026,4.25\n"))
    assert df["value"].iloc[0] == pytest.approx(0.425)


@pytest.mark.parametrize("body", [b"", b"   \n", b'"Date","10 Yr"\n', b"# only a comment\n"])
def test_empty_month_returns_empty_frame(src: TreasurySource, body: bytes) -> None:
    df = src.parse(raw(body))
    assert df.empty
    validate("parsed_observations", df)


def test_no_configured_tenor_columns_gives_empty(src: TreasurySource) -> None:
    df = src.parse(raw(b"Date,1.5 Month\n11/13/2026,4.1\n"))
    assert df.empty


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>Service unavailable</body></html>",
        b"10 Yr,2 Yr\n4.25,3.7\n",  # no Date column
        b"Date,10 Yr\nnot-a-date,4.25\n",
        b"Date,10 Yr\n11/13/2026,abc\n",
        b"Date,10 Yr\n11/13/2026,inf\n",
        b"Date,10 Yr\n11/13/2026,4.25,9.9\n",  # ragged row
        b"\xff\xfe\x00bad",
    ],
)
def test_malformed_raises_parse_error(src: TreasurySource, body: bytes) -> None:
    with pytest.raises(ParseError):
        src.parse(raw(body))


def test_error_response_raises_parse_error(src: TreasurySource) -> None:
    with pytest.raises(ParseError):
        src.parse(raw(b"oops", http_status=500))


# ---------------------------------------------------------------- wiring


def test_is_a_source_and_registered(config: Config) -> None:
    s = TreasurySource(config)
    assert isinstance(s, Source)
    assert s.name == "treasury"
    registry.clear()
    registry.register("treasury", TreasurySource)
    assert [x.name for x in registry.get_sources(config) if x.name == "treasury"] == ["treasury"]
    registry.clear()
