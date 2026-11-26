"""fiscaldata upcoming auctions (card T65). No network; the payload is a SYNTHETIC FIXTURE
(``tests/fixtures/synthetic/fiscaldata``, invented dates, CUSIPs and amounts)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from fakes_t65 import FIX, NOW, RoutedHttp, ctx_for, t65_config

from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse
from deskbrief.pipeline.clean import clean_table_response
from deskbrief.schemas import validate
from deskbrief.sources.base import ParseError, Source
from deskbrief.sources.fiscaldata import DATASET, ENDPOINT, FiscalDataSource
from deskbrief.sources.tables import TableSource

BODY = (FIX / "fiscaldata" / "upcoming_auctions.json").read_bytes()


@pytest.fixture
def source() -> FiscalDataSource:
    return FiscalDataSource(t65_config())


def _req(source: FiscalDataSource) -> FetchRequest:
    (req,) = source.plan(JobKind.MORNING, ctx_for(source.config))
    return req


def _raw(req: FetchRequest, body: bytes = BODY, status: int = 200) -> RawResponse:
    return RawResponse(req, "F1", NOW, NOW, status, "application/json", body)


def test_protocols_and_plan(source: FiscalDataSource) -> None:
    assert isinstance(source, Source)
    assert isinstance(source, TableSource)
    req = _req(source)
    assert (req.source, req.dataset, source.table_for(req)) == ("fiscaldata", DATASET, "events")
    assert dict(req.params) == {
        "filter": "auction_date:gte:2026-11-26,auction_date:lte:2026-12-03",
        "sort": "auction_date",
        "page[size]": "100",
    }
    for job in (JobKind.EOD, JobKind.INTRADAY, JobKind.BACKFILL, JobKind.REBUILD):
        assert source.plan(job, ctx_for(source.config, job)) == []


def test_fetch_is_keyless(source: FiscalDataSource) -> None:
    http = RoutedHttp(lambda url: (200, BODY))
    source.fetch(_req(source), http)
    (call,) = http.calls
    assert call["url"] == ENDPOINT
    assert call["secret_params"] == {}
    assert call["secret_headers"] == {}


def test_parse_table_rows(source: FiscalDataSource) -> None:
    rows = source.parse_table(_raw(_req(source)))
    assert list(rows["title"]) == [
        "4-Week Bill auction",
        "26-Week Bill auction",
        "2-Year Note auction",
    ]  # the 20-Year Bond auction is after the window
    assert list(rows["event_date"]) == [date(2026, 11, 27), date(2026, 11, 30), date(2026, 12, 1)]
    assert set(rows["kind"]) == {"auction"}
    assert rows["scheduled_at"].isna().all()  # the dataset gives no close time
    assert list(rows["event_id"]) == [
        "auction:2026-11-27:EXMPB0011",
        "auction:2026-11-30:EXMPB0029",
        "auction:2026-12-01:2-Year-Note",
    ]
    first = json.loads(rows.iloc[0]["details_json"])
    assert first["offering_amt_usd"] == 11_000_000_000.0  # the newer record date wins
    assert first["record_date"] == "2026-11-25"
    assert json.loads(rows.iloc[2]["details_json"])["cusip"] is None
    assert source.parse(_raw(_req(source))).empty  # no observations


def test_rows_validate_with_lineage(source: FiscalDataSource) -> None:
    result = clean_table_response(source, _raw(_req(source)), "RUN-1", "events")
    assert result.ok, result.detail
    validate("events", result.rows)
    assert set(result.rows["source"]) == {"fiscaldata"}
    assert set(result.rows["dataset"]) == {DATASET}


def test_empty_data_is_zero_rows(source: FiscalDataSource) -> None:
    body = json.dumps({"data": [], "meta": {"total-pages": 1}}).encode()
    assert source.parse_table(_raw(_req(source), body)).empty


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"meta": {}}',
        json.dumps({"data": [{"auction_date": "soon"}], "meta": {}}).encode(),
        json.dumps({"data": [], "meta": {"total-pages": 2}}).encode(),
        json.dumps(
            {"data": [{"auction_date": "2026-11-27", "security_type": "Bill"}], "meta": {}}
        ).encode(),
    ],
)
def test_malformed_payloads_raise(source: FiscalDataSource, body: bytes) -> None:
    with pytest.raises(ParseError):
        source.parse_table(_raw(_req(source), body))


def test_failed_fetch_raises(source: FiscalDataSource) -> None:
    raw = RawResponse(_req(source), "F", NOW, NOW, 503, "", b"x", error="HTTP 503")
    assert raw.fetch_status is FetchStatus.ERROR
    with pytest.raises(ParseError):
        source.parse_table(raw)
