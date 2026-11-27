"""FRED release dates (card T65). No network; the payload is a SYNTHETIC FIXTURE
(``tests/fixtures/synthetic/fred_releases``, invented dates). The key is a fake test value."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from fakes_t65 import FIX, NOW, RoutedHttp, ctx_for, t65_config

from deskbrief.models import FetchRequest, JobKind, RawResponse
from deskbrief.pipeline.clean import clean_response, clean_table_response
from deskbrief.schemas import validate
from deskbrief.sources.base import ParseError
from deskbrief.sources.fred import (
    ENDPOINT,
    RELEASE_DATASET,
    RELEASE_DATES_ENDPOINT,
    FredSource,
)
from deskbrief.sources.tables import is_table_source, table_of

BODY = (FIX / "fred_releases" / "release_dates_10.json").read_bytes()
KEY = "-".join(["fake", "fred", "value"] * 3)


@pytest.fixture
def source() -> FredSource:
    return FredSource(t65_config())


def _releases(source: FredSource, job: JobKind = JobKind.MORNING) -> list[FetchRequest]:
    reqs = source.plan(job, ctx_for(source.config, job))
    return [r for r in reqs if r.dataset == RELEASE_DATASET]


def _raw(req: FetchRequest, body: bytes = BODY) -> RawResponse:
    return RawResponse(req, "F1", NOW, NOW, 200, "application/json", body)


def test_release_requests_in_the_morning_only(source: FredSource) -> None:
    reqs = _releases(source)
    assert [r.params["release_id"] for r in reqs] == ["10", "50"]
    assert dict(reqs[0].params) == {
        "release_id": "10",
        "file_type": "json",
        "realtime_start": "2026-11-26",
        "realtime_end": "2026-12-03",
        "include_release_dates_with_no_data": "true",
        "sort_order": "asc",
    }
    assert reqs[0].symbols == ("release:10",)
    for job in (JobKind.EOD, JobKind.BACKFILL, JobKind.INTRADAY):
        assert _releases(source, job) == []
    # the observation requests are unchanged
    obs = [r for r in source.plan(JobKind.MORNING, ctx_for(source.config)) if r not in reqs]
    assert obs
    assert all(r.dataset in ("series", "series_weekly") for r in obs)


def test_no_release_requests_without_configured_releases() -> None:
    assert _releases(FredSource(t65_config(fred_releases=False))) == []


def test_table_routing(source: FredSource) -> None:
    rel, obs = _releases(source)[0], source.plan(JobKind.EOD, ctx_for(source.config))[0]
    assert is_table_source(source)
    assert table_of(source, rel) == "events"
    assert table_of(source, obs) is None


def test_fetch_uses_release_endpoint_and_secret_key(
    source: FredSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRED_API_KEY", KEY)
    http = RoutedHttp(lambda url: (200, BODY))
    req = _releases(source)[0]
    source.fetch(req, http)
    (call,) = http.calls
    assert call["url"] == RELEASE_DATES_ENDPOINT != ENDPOINT
    assert call["secret_params"] == {"api_key": KEY}
    assert KEY not in json.dumps(call["params"])


def test_missing_key_fails_without_a_request(
    source: FredSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    http = RoutedHttp(lambda url: (200, BODY))
    raw = source.fetch(_releases(source)[0], http)
    assert http.calls == []
    assert not raw.ok


def test_parse_table_rows(source: FredSource) -> None:
    rows = source.parse_table(_raw(_releases(source)[0]))
    assert list(rows["event_date"]) == [date(2026, 11, 26), date(2026, 12, 2)]  # window only
    assert list(rows["event_id"]) == ["release:10:2026-11-26", "release:10:2026-12-02"]
    assert set(rows["title"]) == {"Consumer Price Index"}  # the configured title
    assert set(rows["kind"]) == {"release"}
    # 08:30 ET; 2026-11-26 is EST (UTC-5)
    assert rows.iloc[0]["scheduled_at"] == datetime(2026, 11, 26, 13, 30, tzinfo=UTC)
    details = json.loads(rows.iloc[0]["details_json"])
    assert details == {"release_id": 10, "release_name": "Invented Price Index Release"}
    assert rows.iloc[0]["url"] == "https://fred.stlouisfed.org/release?rid=10"


def test_rows_validate_and_observation_parse_is_empty(source: FredSource) -> None:
    raw = _raw(_releases(source)[0])
    result = clean_table_response(source, raw, "RUN-1", "events")
    assert result.ok, result.detail
    validate("events", result.rows)
    assert set(result.rows["dataset"]) == {RELEASE_DATASET}
    # a rebuild replays this raw through parse(): no observations and no quarantine
    assert source.parse(raw).empty
    assert clean_response(source, raw, "RUN-1").ok


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        json.dumps({"error_message": "Bad Request."}).encode(),
        json.dumps({"release_dates": [{"release_id": 10, "date": "soon"}]}).encode(),
        json.dumps({"release_dates": [{"release_id": 99, "date": "2026-11-27"}]}).encode(),
        json.dumps({"release_dates": [], "count": 20000, "limit": 10000}).encode(),
    ],
)
def test_malformed_payloads_raise(source: FredSource, body: bytes) -> None:
    with pytest.raises(ParseError):
        source.parse_table(_raw(_releases(source)[0], body))
