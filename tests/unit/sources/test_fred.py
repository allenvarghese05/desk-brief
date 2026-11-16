# ruff: noqa: PT018
"""FRED source tests (card T11). No network: HTTP is mocked with respx.

Fixtures under tests/fixtures/synthetic/fred are SYNTHETIC FIXTURES (schema-faithful to the
documented FRED ``series/observations`` JSON, invented values). The API key below is a
synthetic test value.
"""

from __future__ import annotations

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
from deskbrief.sources.fred import ENDPOINT, FredSource
from deskbrief.sources.http import DefaultHttpClient

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "tests" / "fixtures" / "synthetic" / "fred"
KEY = "fredTestKey1234567890abcdef"
SERIES = [
    "DGS2",
    "DGS10",
    "DGS20",
    "DGS30",
    "DFF",
    "DCOILWTICO",
    "DCOILBRENTEU",
    "CPIAUCSL",
    "UNRATE",
]


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def src(config: Config) -> FredSource:
    return FredSource(config)


def ctx_for(config: Config, job: JobKind = JobKind.MORNING) -> RunContext:
    return RunContext(
        run_id="r1",
        job=job,
        trigger=Trigger.SCHEDULE,
        target_date=date(2026, 11, 16),
        now_utc=datetime(2026, 11, 16, 11, 15, tzinfo=UTC),
        git_sha="abc",
        config_hash="h",
        config=config,
        store=object(),  # type: ignore[arg-type]
    )


def raw_for(
    series: str,
    body: bytes,
    *,
    http_status: int | None = 200,
    error: str | None = None,
    status: FetchStatus | None = None,
) -> RawResponse:
    req = FetchRequest(
        source="fred", dataset="series", params={"series_id": series}, symbols=(series,)
    )
    t = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
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


def fixture_raw(series: str, name: str) -> RawResponse:
    return raw_for(series, (FIX / name).read_bytes())


def payload(observations: list[dict[str, Any]]) -> bytes:
    return json.dumps({"observations": observations}).encode()


def obs(d: str, v: str) -> dict[str, str]:
    return {"realtime_start": "2026-11-15", "realtime_end": "2026-11-15", "date": d, "value": v}


# ---------------------------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------------------------


def test_plan_one_request_per_configured_series(src: FredSource, config: Config) -> None:
    reqs = src.plan(JobKind.MORNING, ctx_for(config))
    assert sorted(r.symbols[0] for r in reqs) == sorted(SERIES)
    for r in reqs:
        assert r.source == "fred" and r.dataset == "series"
        assert r.symbols == (r.params["series_id"],)
        assert r.params["limit"] == "10"  # last ~10 obs so revisions are visible
        assert r.params["sort_order"] == "desc"
        assert r.params["file_type"] == "json"


def test_plan_never_carries_the_key(src: FredSource, config: Config) -> None:
    for r in src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD)):
        assert not any("key" in k.lower() for k in r.params)
        assert KEY not in repr(r)


def test_plan_backfill_ends_at_target_date(src: FredSource, config: Config) -> None:
    reqs = src.plan(JobKind.BACKFILL, ctx_for(config, JobKind.BACKFILL))
    assert reqs and all(r.params["observation_end"] == "2026-11-16" for r in reqs)
    assert all(
        "observation_end" not in r.params for r in src.plan(JobKind.MORNING, ctx_for(config))
    )


@pytest.mark.parametrize("job", [JobKind.INTRADAY, JobKind.REBUILD])
def test_plan_empty_for_jobs_without_fred(src: FredSource, config: Config, job: JobKind) -> None:
    assert src.plan(job, ctx_for(config, job)) == []


def test_plan_skips_series_without_a_spec(config: Config, caplog: pytest.LogCaptureFixture) -> None:
    src = FredSource(config)
    src._specs = {k: v for k, v in src._specs.items() if k != "UNRATE"}
    reqs = src.plan(JobKind.MORNING, ctx_for(config))
    assert "UNRATE" not in [r.symbols[0] for r in reqs] and len(reqs) == 8


# ---------------------------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------------------------


def test_parse_daily_yield_skips_missing_dot(src: FredSource) -> None:
    df = src.parse(fixture_raw("DGS10", "dgs10.json"))
    validate("parsed_observations", df)
    assert len(df) == 4  # the "." row (Veterans Day) is skipped, not NaN
    assert df["value"].notna().all()
    assert set(df["field"]) == {"yield"} and set(df["interval"]) == {"1d"}
    assert set(df["unit"]) == {"pct"} and set(df["tier"]) == {3}
    assert set(df["instrument_id"]) == {"UST_10Y"} and set(df["symbol"]) == {"DGS10"}
    assert set(df["source"]) == {"fred"} and set(df["dataset"]) == {"series"}
    assert not df["is_derived"].any() and not df["is_fallback"].any()
    row = df[df["value"] == 4.31].iloc[0]
    # 1d as_of = 00:00 America/New_York on the reference date (EST in November = 05:00 UTC)
    assert row["as_of"] == pd_ts("2026-11-13T05:00:00Z")
    assert 4.25 in set(df["value"]) and 4.22 in set(df["value"])


def pd_ts(s: str) -> Any:
    import pandas as pd

    return pd.Timestamp(s)


def test_parse_as_of_is_et_midnight_in_summer(src: FredSource) -> None:
    df = src.parse(raw_for("DGS10", payload([obs("2026-07-01", "4.0")])))
    assert df.iloc[0]["as_of"] == pd_ts("2026-07-01T04:00:00Z")  # EDT


def test_parse_monthly_cpi(src: FredSource) -> None:
    df = src.parse(fixture_raw("CPIAUCSL", "cpiaucsl.json"))
    validate("parsed_observations", df)
    assert len(df) == 3
    assert set(df["instrument_id"]) == {"CPI"}
    assert set(df["interval"]) == {"1mo"} and set(df["unit"]) == {"index_pts"}
    assert set(df["field"]) == {"close"}
    first = df[df["value"] == 325.123].iloc[0]
    assert first["as_of"] == pd_ts("2026-09-01T04:00:00Z")  # first of month 00:00 ET (EDT)


def test_parse_monthly_as_of_snaps_to_first_of_month(src: FredSource) -> None:
    df = src.parse(raw_for("UNRATE", payload([obs("2026-09-15", "4.4")])))
    assert df.iloc[0]["as_of"] == pd_ts("2026-09-01T04:00:00Z")


def test_parse_unemployment_rate(src: FredSource) -> None:
    df = src.parse(raw_for("UNRATE", payload([obs("2026-09-01", "4.4")])))
    assert df.iloc[0]["field"] == "rate" and df.iloc[0]["unit"] == "pct"
    assert df.iloc[0]["interval"] == "1mo" and df.iloc[0]["instrument_id"] == "UNRATE"


def test_parse_fed_funds_is_daily_rate(src: FredSource) -> None:
    df = src.parse(raw_for("DFF", payload([obs("2026-11-13", "3.88")])))
    assert df.iloc[0]["field"] == "rate" and df.iloc[0]["interval"] == "1d"
    assert df.iloc[0]["unit"] == "pct" and df.iloc[0]["instrument_id"] == "DFF"


def test_parse_oil_is_usd_close(src: FredSource) -> None:
    df = src.parse(fixture_raw("DCOILWTICO", "dcoilwtico.json"))
    validate("parsed_observations", df)
    assert set(df["field"]) == {"close"} and set(df["unit"]) == {"USD"}
    assert set(df["interval"]) == {"1d"} and set(df["instrument_id"]) == {"WTI"}
    brent = src.parse(raw_for("DCOILBRENTEU", payload([obs("2026-11-09", "64.10")])))
    assert brent.iloc[0]["instrument_id"] == "BRENT" and brent.iloc[0]["unit"] == "USD"


@pytest.mark.parametrize("series", SERIES)
def test_every_configured_series_parses_and_validates(src: FredSource, series: str) -> None:
    day = "2026-09-01" if series in {"CPIAUCSL", "UNRATE"} else "2026-11-13"
    df = src.parse(raw_for(series, payload([obs(day, "1.5")])))
    validate("parsed_observations", df)
    assert len(df) == 1 and df.iloc[0]["symbol"] == series


def test_parse_applies_feed_scale(config: Config) -> None:
    src = FredSource(config)
    inst, feed = src._index["DGS10"]
    src._index["DGS10"] = (inst, feed.model_copy(update={"scale": 0.5}))
    df = src.parse(raw_for("DGS10", payload([obs("2026-11-13", "4.0")])))
    assert df.iloc[0]["value"] == 2.0


def test_parse_empty_observations_is_empty_frame(src: FredSource) -> None:
    df = src.parse(fixture_raw("DGS10", "empty.json"))
    assert df.empty
    validate("parsed_observations", df)


def test_parse_all_missing_is_empty_frame(src: FredSource) -> None:
    df = src.parse(raw_for("DGS10", payload([obs("2026-11-11", "."), obs("2026-11-10", ".")])))
    assert df.empty
    validate("parsed_observations", df)


def test_parse_unconfigured_series_is_empty_frame(src: FredSource) -> None:
    df = src.parse(raw_for("GDP", payload([obs("2026-07-01", "30000.1")])))
    assert df.empty


def test_parse_ignores_unknown_keys(src: FredSource) -> None:
    body = json.dumps({"extra": 1, "observations": [{**obs("2026-11-13", "4.1"), "x": 2}]})
    assert len(src.parse(raw_for("DGS10", body.encode()))) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b'"observations"',
        b"{}",
        b'{"observations": null}',
        b'{"observations": {}}',
        b'{"observations": ["x"]}',
        b'{"observations": [{"date": "2026-11-13"}]}',
        b'{"observations": [{"value": "4.0"}]}',
        b'{"observations": [{"date": "13/11/2026", "value": "4.0"}]}',
        b'{"observations": [{"date": 20261113, "value": "4.0"}]}',
        b'{"observations": [{"date": "2026-11-13", "value": "abc"}]}',
        b'{"observations": [{"date": "2026-11-13", "value": 4.0}]}',
        b'{"observations": [{"date": "2026-11-13", "value": "nan"}]}',
        b'{"observations": [{"date": "2026-11-13", "value": "inf"}]}',
        b'{"error_code": 400, "error_message": "Bad Request. api_key is invalid."}',
        b"\xff\xfe",
    ],
)
def test_parse_malformed_raises(src: FredSource, body: bytes) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for("DGS10", body))


def test_parse_one_bad_row_rejects_whole_payload(src: FredSource) -> None:
    body = payload([obs("2026-11-13", "4.0"), obs("2026-11-12", "oops")])
    with pytest.raises(ParseError):
        src.parse(raw_for("DGS10", body))


def test_parse_failed_fetch_raises(src: FredSource) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for("DGS10", b"", http_status=500, error="HTTP 500"))


def test_parse_without_series_symbol_raises(src: FredSource) -> None:
    req = FetchRequest(source="fred", dataset="series")
    t = datetime(2026, 11, 15, tzinfo=UTC)
    raw = RawResponse(req, "f", t, t, 200, "application/json", payload([]))
    with pytest.raises(ParseError):
        src.parse(raw)


def test_parse_is_pure_replay(src: FredSource) -> None:
    raw = fixture_raw("DGS10", "dgs10.json")
    assert src.parse(raw).equals(src.parse(raw))


# ---------------------------------------------------------------------------------------------
# fetch + key handling
# ---------------------------------------------------------------------------------------------


def http_client(config: Config) -> DefaultHttpClient:
    return DefaultHttpClient(
        {"fred": SourceSettings(rate_per_min=6000.0, timeout_s=5.0, retries=0)},
        sleep=lambda s: None,
    )


@respx.mock
def test_fetch_sends_key_on_the_wire_only(
    src: FredSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRED_API_KEY", KEY)
    stream = io.StringIO()
    configure_logging(stream=stream)
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            content=(FIX / "dgs10.json").read_bytes(),
            headers={"content-type": "application/json"},
        )
    )
    req = next(r for r in src.plan(JobKind.MORNING, ctx_for(config)) if r.symbols == ("DGS10",))
    raw = src.fetch(req, http_client(config))

    # the key reached the server ...
    sent = route.calls.last.request
    assert sent.url.params["api_key"] == KEY
    assert sent.url.params["series_id"] == "DGS10" and sent.url.params["limit"] == "10"
    # ... and nowhere else
    assert raw.ok and raw.fetch_status is FetchStatus.OK
    assert KEY not in repr(req) and KEY not in str(dict(req.params))
    assert raw.url_redacted is not None and KEY not in raw.url_redacted
    assert "api_key" not in raw.url_redacted or "***" in raw.url_redacted
    assert KEY not in repr(raw.request) and (raw.error is None or KEY not in raw.error)
    assert KEY not in stream.getvalue()
    assert len(src.parse(raw)) == 4


@respx.mock
def test_fetch_http_error_does_not_leak_key(
    src: FredSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRED_API_KEY", KEY)
    stream = io.StringIO()
    configure_logging(stream=stream)
    respx.get(ENDPOINT).mock(return_value=httpx.Response(400, json={"error_code": 400}))
    req = src.plan(JobKind.MORNING, ctx_for(config))[0]
    raw = src.fetch(req, http_client(config))
    assert raw.fetch_status is FetchStatus.ERROR and raw.http_status == 400
    assert KEY not in (raw.error or "") and KEY not in (raw.url_redacted or "")
    assert KEY not in stream.getvalue()


@respx.mock
def test_fetch_transport_error_does_not_leak_key(
    src: FredSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRED_API_KEY", KEY)
    stream = io.StringIO()
    configure_logging(stream=stream)
    respx.get(ENDPOINT).mock(side_effect=httpx.ConnectError(f"boom api_key={KEY}"))
    req = src.plan(JobKind.MORNING, ctx_for(config))[0]
    raw = src.fetch(req, http_client(config))
    assert raw.fetch_status is FetchStatus.ERROR
    assert KEY not in (raw.error or "") and KEY not in stream.getvalue()


@respx.mock
@pytest.mark.parametrize("value", [None, "", "   "])
def test_fetch_without_key_returns_error_response_and_makes_no_request(
    src: FredSource, config: Config, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("FRED_API_KEY", raising=False)
    else:
        monkeypatch.setenv("FRED_API_KEY", value)
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={}))
    req = src.plan(JobKind.MORNING, ctx_for(config))[0]
    raw = src.fetch(req, http_client(config))
    assert not route.called
    assert not raw.ok and raw.fetch_status is FetchStatus.ERROR
    assert raw.error is not None and "FRED_API_KEY" in raw.error
    assert raw.http_status is None and raw.body == b""
    assert raw.request is req


@respx.mock
def test_fetch_key_is_stripped(
    src: FredSource, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRED_API_KEY", f"  {KEY}\n")
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"observations": []}))
    req = src.plan(JobKind.MORNING, ctx_for(config))[0]
    src.fetch(req, http_client(config))
    assert route.calls.last.request.url.params["api_key"] == KEY


# ---------------------------------------------------------------------------------------------
# registry / protocol
# ---------------------------------------------------------------------------------------------


def test_fred_source_satisfies_protocol(src: FredSource) -> None:
    assert isinstance(src, Source)
    assert src.name == "fred" and src.parser_version


def test_registered_under_fred(config: Config) -> None:
    import importlib

    import deskbrief.sources.fred as mod

    registry.clear()
    try:
        importlib.reload(mod)
        assert registry.registered() == ["fred"]
        built = registry.get_sources(config)
        assert [s.name for s in built] == ["fred"]
    finally:
        registry.clear()
