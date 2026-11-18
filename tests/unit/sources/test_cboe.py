"""Cboe source tests (card T13). Fixtures are SYNTHETIC (tests/fixtures/synthetic/cboe)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pytest
import respx

from deskbrief.config import Config, load_config
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext, Trigger
from deskbrief.schemas import validate
from deskbrief.sources import registry
from deskbrief.sources.base import ParseError, Source
from deskbrief.sources.cboe import URL, CboeSource
from deskbrief.sources.http import DefaultHttpClient

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic" / "cboe" / "VIX_History.csv"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
HDR = b"DATE,OPEN,HIGH,LOW,CLOSE\n"


def ctx(config: Config) -> RunContext:
    return RunContext(
        run_id="R",
        job=JobKind.MORNING,
        trigger=Trigger.MANUAL,
        target_date=date(2026, 11, 16),
        now_utc=NOW,
        git_sha="x",
        config_hash="h",
        config=config,
        store=cast(Any, None),
    )


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def src(config: Config) -> CboeSource:
    return CboeSource(config)


def raw_for(
    body: bytes,
    *,
    lookback: str | None = "10",
    target: date | None = date(2026, 11, 16),
    status: int | None = 200,
    error: str | None = None,
) -> RawResponse:
    params = {} if lookback is None else {"lookback_days": lookback}
    req = FetchRequest("cboe", "vix_history", params=params, symbols=("VIX",), target_date=target)
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


def test_plan(src: CboeSource, config: Config) -> None:
    reqs = src.plan(JobKind.EOD, ctx(config))
    assert len(reqs) == 1
    r = reqs[0]
    assert (r.source, r.dataset, r.symbols) == ("cboe", "vix_history", ("VIX",))
    assert r.params["lookback_days"] == "10"
    assert r.target_date == date(2026, 11, 16)
    assert src.plan(JobKind.INTRADAY, ctx(config)) == []


def test_golden_parse_recent_window_only(src: CboeSource) -> None:
    df = src.parse(raw_for(FIXTURE.read_bytes()))
    validate("parsed_observations", df)
    # window is 2026-11-06..2026-11-16: the 1990 row is dropped
    assert len(df) == 4
    df = df.sort_values("as_of")
    assert df["value"].tolist() == [17.95, 17.20, 18.75, 16.85]
    assert set(df["instrument_id"]) == {"VIX"}
    assert set(df["field"]) == {"close"}
    assert set(df["tier"]) == {3}
    assert set(df["unit"]) == {"index_pts"}
    assert set(df["interval"]) == {"1d"}
    # P-006: 00:00 America/New_York (EST after 2026-11-01 = 05:00 UTC)
    assert df["as_of"].iloc[0] == pd.Timestamp("2026-11-06 05:00", tz="UTC")
    assert df["as_of"].iloc[-1] == pd.Timestamp("2026-11-13 05:00", tz="UTC")


def test_all_rows_without_lookback(src: CboeSource) -> None:
    df = src.parse(raw_for(FIXTURE.read_bytes(), lookback=None))
    assert len(df) == 5
    assert df["value"].min() == 16.85
    assert pd.Timestamp("1990-01-02 05:00", tz="UTC") in set(df["as_of"])  # EST


def test_window_edges_inclusive(src: CboeSource) -> None:
    df = src.parse(raw_for(FIXTURE.read_bytes(), lookback="3", target=date(2026, 11, 13)))
    assert len(df) == 2  # 11/10 and 11/13 (11/09 is 4 days back)
    assert df["value"].sort_values().tolist() == [16.85, 18.75]


def test_window_without_rows_is_empty(src: CboeSource) -> None:
    df = src.parse(raw_for(FIXTURE.read_bytes(), target=date(2026, 12, 25)))
    assert df.empty
    validate("parsed_observations", df)


@pytest.mark.parametrize("body", [b"", b" \n", HDR])
def test_empty_cases(src: CboeSource, body: bytes) -> None:
    assert src.parse(raw_for(body)).empty


def test_title_lines_before_header_tolerated(src: CboeSource) -> None:
    body = b"Cboe Volatility Index\nVIX Daily Prices\n" + HDR + b"11/13/2026,1,2,0.5,16.85\n"
    assert src.parse(raw_for(body))["value"].tolist() == [16.85]


def test_iso_dates_accepted(src: CboeSource) -> None:
    assert src.parse(raw_for(HDR + b"2026-11-13,1,2,0.5,16.85\n"))["value"].tolist() == [16.85]


@pytest.mark.parametrize(
    "body",
    [
        b"No data",
        b"<!DOCTYPE html><html><body>Access Denied</body></html>",
        b"DATE,OPEN,HIGH,LOW\n11/13/2026,1,2,3\n",
        HDR + b"11/13/2026,1,2,0.5\n",
        HDR + b"13-Nov-2026,1,2,0.5,16.85\n",
        HDR + b"11/13/2026,1,2,0.5,n/a\n",
        HDR + b"11/13/2026,1,2,0.5,inf\n",
        b"\xff\xfe\x00bad",
    ],
)
def test_malformed_raises_parse_error(src: CboeSource, body: bytes) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for(body))


def test_bad_lookback_param(src: CboeSource) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for(FIXTURE.read_bytes(), lookback="ten"))


def test_failed_fetch_raises_parse_error(src: CboeSource) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for(b"", status=404, error="HTTP 404"))


def test_fetch_gets_the_history_file(src: CboeSource, config: Config) -> None:
    req = src.plan(JobKind.MORNING, ctx(config))[0]
    with respx.mock(assert_all_called=True) as mock:
        mock.get(URL).respond(200, content=FIXTURE.read_bytes())
        with DefaultHttpClient(config.sources) as http:
            raw = src.fetch(req, http)
    assert raw.ok
    assert raw.body == FIXTURE.read_bytes()
    assert len(src.parse(raw)) == 4


def test_registered_and_conforms(config: Config) -> None:
    registry.clear()
    registry.register("cboe", CboeSource)
    built = registry.get_sources(config)
    assert [s.name for s in built] == ["cboe"]
    assert isinstance(built[0], Source)


# -- T08 follow-ups (PR #11 review findings 2-5) -------------------------------------------


def test_bad_row_outside_the_window_does_not_break_the_run(src: CboeSource) -> None:
    """Finding 2: only rows inside the requested window are validated."""
    body = (
        HDR
        + b"01/02/1990,1,2,0.5,n/a\n"  # historical row with a bad CLOSE
        + b"01/03/1990,1,2,0.5\n"  # historical row with a missing column
        + b"11/13/2026,1,2,0.5,16.85\n"
    )
    assert src.parse(raw_for(body))["value"].tolist() == [16.85]


def test_bad_row_inside_the_window_still_raises(src: CboeSource) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for(HDR + b"11/13/2026,1,2,0.5,n/a\n"))


def test_bad_historical_row_still_fails_a_backfill_without_window(src: CboeSource) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for(HDR + b"01/02/1990,1,2,0.5,n/a\n", lookback=None))


def test_truncated_download_ending_mid_number_raises(src: CboeSource) -> None:
    """Finding 3: a body cut mid-number (no trailing newline) would parse '16.' as 16.0."""
    body = HDR + b"11/12/2026,1,2,0.5,17.10\n11/13/2026,1,2,0.5,16."
    with pytest.raises(ParseError, match="truncated"):
        src.parse(raw_for(body))


def test_truncation_outside_the_window_is_ignored(src: CboeSource) -> None:
    body = HDR + b"11/13/2026,1,2,0.5,16.85\n01/02/1990,1,2,0.5,17."
    assert src.parse(raw_for(body))["value"].tolist() == [16.85]


def test_url_is_the_cdn_api_host_the_old_host_redirects_to() -> None:
    """T25: cdn.cboe.com answers 307 with Location cdn-api.cboe.com (probe 2026-11-18)."""
    assert URL == "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    code = (ROOT / "src" / "deskbrief" / "sources" / "cboe.py").read_text()
    assert "UNVERIFIED" not in code


@pytest.mark.parametrize("lookback", ["99999999999", "-99999999999"])
def test_overflowing_lookback_is_a_parse_error(src: CboeSource, lookback: str) -> None:
    """Finding 5: date arithmetic overflow becomes ParseError, not OverflowError."""
    with pytest.raises(ParseError):
        src.parse(raw_for(HDR + b"11/13/2026,1,2,0.5,16.85\n", lookback=lookback))
