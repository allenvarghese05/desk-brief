"""SEC EDGAR source (card T65). No network: documents come from INVENTED fixtures
(``tests/fixtures/synthetic/edgar``, the fictional Example Corp and Invented Asset Management).
"""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
from datetime import date

import httpx
import pandas as pd
import pytest
from fakes_t65 import (
    EDGAR,
    EXMP_CIK,
    FAKE_UA,
    HOLDER_CIK,
    NOW,
    RoutedHttp,
    ctx_for,
    edgar_route,
    t65_config,
)

from deskbrief.logging import configure_logging
from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse
from deskbrief.pipeline.clean import clean_table_response
from deskbrief.pipeline.runner import _source_log_row
from deskbrief.schemas import validate
from deskbrief.sources.base import ParseError, Source
from deskbrief.sources.edgar import (
    COMPANY_DATASET,
    HOLDERS_DATASET,
    EdgarSource,
    filter_info_table,
    side_of,
)
from deskbrief.sources.http import DefaultHttpClient
from deskbrief.sources.tables import TableSource, is_table_source

UA_ENV = "DESKBRIEF_SEC_USER_AGENT"


@pytest.fixture
def source() -> EdgarSource:
    return EdgarSource(t65_config())


@pytest.fixture
def with_ua(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(UA_ENV, FAKE_UA)


def _req(source: EdgarSource, dataset: str) -> FetchRequest:
    return next(
        r for r in source.plan(JobKind.MORNING, ctx_for(source.config)) if r.dataset == dataset
    )


def _rows(source: EdgarSource, dataset: str, **route: str) -> tuple[pd.DataFrame, RoutedHttp]:
    http = RoutedHttp(edgar_route(**route))
    raw = source.fetch(_req(source, dataset), http)
    assert raw.ok, raw.error
    return source.parse_table(raw), http


# --------------------------------------------------------------------------------------------
# protocol and plan
# --------------------------------------------------------------------------------------------


def test_is_a_table_source_for_filings(source: EdgarSource) -> None:
    assert isinstance(source, Source)
    assert isinstance(source, TableSource)
    assert is_table_source(source)
    assert source.table == "filings"
    req = _req(source, COMPANY_DATASET)
    assert source.table_for(req) == "filings"
    assert source.parse(RawResponse(req, "f", NOW, NOW, 200, "application/json", b"{}")).empty


def test_plan_morning_only_one_fetch_per_company_and_holder(source: EdgarSource) -> None:
    reqs = source.plan(JobKind.MORNING, ctx_for(source.config))
    assert [(r.dataset, r.symbols) for r in reqs] == [
        (COMPANY_DATASET, ("AAPL",)),
        (HOLDERS_DATASET, (HOLDER_CIK,)),
    ]
    assert dict(reqs[0].params) == {"cik": EXMP_CIK, "since": "2026-08-28"}  # 90 days back
    assert not any("agent" in k.lower() for r in reqs for k in r.params)
    for job in (JobKind.EOD, JobKind.INTRADAY, JobKind.BACKFILL, JobKind.REBUILD):
        assert source.plan(job, ctx_for(source.config, job)) == []


def test_no_holder_fetch_without_a_cusip() -> None:
    cfg = t65_config()
    companies = tuple(c.model_copy(update={"cusip": None}) for c in cfg.filings.companies)
    cfg = cfg.model_copy(
        update={"filings": cfg.filings.model_copy(update={"companies": companies})}
    )
    reqs = EdgarSource(cfg).plan(JobKind.MORNING, ctx_for(cfg))
    assert [r.dataset for r in reqs] == [COMPANY_DATASET]


# --------------------------------------------------------------------------------------------
# User-Agent: from the environment, a secret header only, never logged or stored
# --------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("with_ua")
def test_user_agent_read_from_env_and_sent_only_as_secret_header(source: EdgarSource) -> None:
    rows, http = _rows(source, COMPANY_DATASET)
    assert not rows.empty
    assert http.calls
    for call in http.calls:
        assert call["secret_headers"] == {"User-Agent": FAKE_UA}
        assert FAKE_UA not in call["url"]
        assert not call["params"]


def test_missing_user_agent_is_a_clean_failed_fetch(
    source: EdgarSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(UA_ENV, raising=False)
    http = RoutedHttp(edgar_route())
    raw = source.fetch(_req(source, COMPANY_DATASET), http)
    assert http.calls == []  # no request made
    assert raw.fetch_status is FetchStatus.ERROR
    assert raw.error == f"{UA_ENV} is not set; no request made"
    assert raw.body == b""


def test_user_agent_redacted_from_logs_url_and_source_log(
    source: EdgarSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the real ``DefaultHttpClient``: the UA reaches the wire, and is in
    no log line, no ``url_redacted``, no raw body and no ``source_log`` row, even on errors."""
    monkeypatch.setenv(UA_ENV, FAKE_UA)
    seen: list[str] = []
    route = edgar_route(fail="form4_buy.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        status, body = route(str(request.url))
        return httpx.Response(status, content=body)

    stream = io.StringIO()
    configure_logging(stream=stream)
    client = DefaultHttpClient(
        source.config.sources,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
    )
    ok_raw = source.fetch(_req(source, HOLDERS_DATASET), client)
    bad_raw = source.fetch(_req(source, COMPANY_DATASET), client)  # one Form 4 answers 500
    assert ok_raw.ok
    assert not bad_raw.ok
    assert seen
    assert set(seen) == {FAKE_UA}
    logs = stream.getvalue()
    assert "http.done" in logs
    for raw in (ok_raw, bad_raw):
        log_row = _source_log_row(ctx_for(source.config), raw, None, 0, 0, None, None)
        assert FAKE_UA not in json.dumps(log_row, default=str)
        assert FAKE_UA not in (raw.url_redacted or "")
        assert FAKE_UA.encode() not in raw.body
    for piece in FAKE_UA.split():
        if len(piece) >= 8:
            assert piece not in logs
    assert FAKE_UA not in logs


# --------------------------------------------------------------------------------------------
# company: latest 10-K / 10-Q, Form 4 trades
# --------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("with_ua")
def test_company_rows(source: EdgarSource) -> None:
    rows, http = _rows(source, COMPANY_DATASET)
    fetched = [c["url"].rsplit("/", 1)[-1] for c in http.calls]
    assert fetched == [
        f"CIK{EXMP_CIK}.json",
        "form4_sale.xml",
        "form4a_buy.xml",
        "form4_buy.xml",
        "form4_grant.xml",
    ]  # form4_old.xml was filed before the 90-day window: not fetched
    heads = rows[rows["seq"] == 0].set_index("accession")
    assert heads.loc["0009999999-26-000002", "form"] == "10-K"  # the latest 10-K
    assert heads.loc["0009999999-26-000007", "form"] == "10-Q"  # the latest 10-Q
    assert "0009999999-25-000001" not in heads.index
    assert "0009999999-26-000004" not in heads.index
    assert heads.loc["0009999999-26-000007", "period"] == date(2026, 9, 30)
    assert heads.loc["0009999999-26-000007", "url"].endswith(
        "/9999999/000999999926000007/exmp-20260930.htm"
    )
    assert "0009999999-26-000011" not in heads.index  # the 8-K is not stored
    assert set(rows["instrument_id"]) == {"AAPL"}
    assert set(rows["cik"]) == {EXMP_CIK}
    assert set(rows["source"]) == {"edgar"}
    assert set(rows["dataset"]) == {COMPANY_DATASET}


@pytest.mark.usefixtures("with_ua")
def test_form4_trades_side_value_and_10b5_1(source: EdgarSource) -> None:
    rows, _ = _rows(source, COMPANY_DATASET)
    trades = rows[rows["row_kind"] == "insider_trade"]
    sale = trades[trades["accession"] == "0009999999-26-000010"].sort_values("seq")
    assert list(sale["seq"]) == [1, 2]
    s, f = sale.iloc[0], sale.iloc[1]
    assert (s["party"], s["party_cik"], s["party_role"]) == (
        "Example Jane",
        "0008888888",
        "Officer: Chief Financial Officer",
    )
    assert (s["trade_code"], s["side"], s["shares"], s["price"], s["value_usd"]) == (
        "S",
        "sell",
        1000.0,
        50.0,
        50000.0,
    )
    assert s["trade_date"] == date(2026, 11, 18)
    assert bool(s["plan_10b5_1"]) is True  # its footnote names a Rule 10b5-1 plan
    assert (f["trade_code"], f["side"], bool(f["plan_10b5_1"])) == ("F", "other", False)
    grant = trades[trades["accession"] == "0009999999-26-000006"].iloc[0]
    assert grant["side"] == "other"
    assert pd.isna(grant["price"])
    assert pd.isna(grant["value_usd"])
    buy = trades[trades["accession"] == "0009999999-26-000009"].iloc[0]
    assert (buy["side"], buy["party_role"], buy["value_usd"]) == ("buy", "Director", 9000.0)
    assert not rows[rows["row_kind"] != "insider_trade"]["plan_10b5_1"].any()


def test_side_of_codes() -> None:
    assert [side_of(c) for c in ("P", "S", "A", "M", "F", "G")] == [
        "buy", "sell", "other", "other", "other", "other",
    ]  # fmt: skip


def test_checkbox_flags_open_market_trades_only(source: EdgarSource) -> None:
    from deskbrief.sources.edgar import Filing, parse_form4

    text = (EDGAR / "form4_old.xml").read_text()  # aff10b5One = 1, code S, no footnote
    filing = Filing("0009999999-26-000005", "4", date(2026, 7, 1), None, "form4_old.xml")
    assert parse_form4(text, filing).trades[0]["plan_10b5_1"] is True
    grant = (
        (EDGAR / "form4_grant.xml")
        .read_text()
        .replace("<aff10b5One>0</aff10b5One>", "<aff10b5One>1</aff10b5One>")
    )
    assert parse_form4(grant, filing).trades[0]["plan_10b5_1"] is False  # code A


@pytest.mark.usefixtures("with_ua")
def test_form4_amendment_names_its_original_on_seq_0_only(source: EdgarSource) -> None:
    rows, _ = _rows(source, COMPANY_DATASET)
    amend = rows[rows["accession"] == "0009999999-26-000008"].sort_values("seq")
    assert list(amend["form"].unique()) == ["4/A"]
    assert amend.iloc[0]["amends_accession"] == "0009999999-26-000009"
    assert amend.iloc[1:]["amends_accession"].isna().all()
    assert amend.iloc[1]["shares"] == 250.0
    others = rows[rows["accession"] != "0009999999-26-000008"]
    assert others["amends_accession"].isna().all()


# --------------------------------------------------------------------------------------------
# holders: 13F changes, restatement vs NEW HOLDINGS amendments
# --------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("with_ua")
def test_holder_restatement_carries_the_period_and_amends_the_original(
    source: EdgarSource,
) -> None:
    rows, http = _rows(source, HOLDERS_DATASET)
    assert not any("000777777726000001" in c["url"] for c in http.calls)  # older quarter
    assert set(rows["accession"]) == {"0007777777-26-000004"}
    head, change = rows.sort_values("seq").iloc[0], rows.sort_values("seq").iloc[1]
    assert (head["form"], head["amends_accession"], head["period"]) == (
        "13F-HR/A",
        "0007777777-26-000003",
        date(2026, 9, 30),
    )
    assert change["row_kind"] == "holder_change"
    assert pd.isna(change["amends_accession"])
    assert (change["party"], change["party_cik"]) == ("Invented Asset Management", HOLDER_CIK)
    # 12,500 restated against 10,000 a quarter earlier; the put is not a share position
    assert (change["shares"], change["shares_change"], change["value_usd"]) == (
        12500.0,
        2500.0,
        625000.0,
    )
    assert json.loads(change["details_json"])["previous_period"] == "2026-06-30"
    assert set(rows["instrument_id"]) == {"AAPL"}
    assert set(rows["cik"]) == {EXMP_CIK}


@pytest.mark.usefixtures("with_ua")
def test_new_holdings_amendment_amends_nothing_and_is_added_in(source: EdgarSource) -> None:
    rows, _ = _rows(source, HOLDERS_DATASET, amendment="new_holdings")
    assert rows["amends_accession"].isna().all()
    change = rows[rows["row_kind"] == "holder_change"]
    assert list(change["accession"]) == ["0007777777-26-000003"]  # the original carries it
    assert (change.iloc[0]["shares"], change.iloc[0]["shares_change"]) == (12300.0, 2300.0)
    amend = rows[rows["accession"] == "0007777777-26-000004"]
    assert list(amend["row_kind"]) == ["filing"]
    assert json.loads(amend.iloc[0]["details_json"])["merged_into"] == "0007777777-26-000003"


def test_filter_info_table_keeps_only_the_wanted_cusips() -> None:
    body = (EDGAR / "infotable_000777777726000002.xml").read_bytes()
    kept = filter_info_table(body, ["exmp00010"])
    entries = [e for e in ET.fromstring(kept).iter() if e.tag.endswith("infoTable")]
    assert len(entries) == 2  # the share position and the put, both on EXMP's CUSIP
    assert "OTHR00019" not in kept


# --------------------------------------------------------------------------------------------
# failures and validation
# --------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("with_ua")
def test_any_failed_request_fails_the_whole_fetch(source: EdgarSource) -> None:
    http = RoutedHttp(edgar_route(fail="form4_buy.xml"))
    raw = source.fetch(_req(source, COMPANY_DATASET), http)
    assert raw.fetch_status is FetchStatus.ERROR
    assert raw.http_status == 500
    assert raw.body == b""
    assert "form4 request failed" in (raw.error or "")
    with pytest.raises(ParseError):
        source.parse_table(raw)


@pytest.mark.usefixtures("with_ua")
def test_rate_limited_request_marks_the_fetch_rate_limited(source: EdgarSource) -> None:
    def route(url: str) -> tuple[int, bytes]:
        return (429, b"slow down") if "form4_sale" in url else edgar_route()(url)

    raw = source.fetch(_req(source, COMPANY_DATASET), RoutedHttp(route))
    assert raw.fetch_status is FetchStatus.RATE_LIMITED


@pytest.mark.usefixtures("with_ua")
def test_doctype_is_refused(source: EdgarSource) -> None:
    def route(url: str) -> tuple[int, bytes]:
        status, body = edgar_route()(url)
        if url.endswith("form4_sale.xml"):
            body = body.replace(
                b"<ownershipDocument>", b'<!DOCTYPE x [<!ENTITY a "b">]><ownershipDocument>'
            )
        return status, body

    raw = source.fetch(_req(source, COMPANY_DATASET), RoutedHttp(route))
    with pytest.raises(ParseError, match="DTD"):
        source.parse_table(raw)


@pytest.mark.usefixtures("with_ua")
def test_malformed_submissions_quarantine(source: EdgarSource) -> None:
    def route(url: str) -> tuple[int, bytes]:
        return (200, b"{not json") if "submissions" in url else edgar_route()(url)

    raw = source.fetch(_req(source, COMPANY_DATASET), RoutedHttp(route))
    assert raw.ok  # the bundle is evidence; the parse refuses it
    result = clean_table_response(source, raw, "RUN", "filings")
    assert result.reason == "parse_error"
    assert result.rows.empty


@pytest.mark.usefixtures("with_ua")
@pytest.mark.parametrize("dataset", [COMPANY_DATASET, HOLDERS_DATASET])
def test_rows_validate_against_the_filings_schema(source: EdgarSource, dataset: str) -> None:
    raw = source.fetch(_req(source, dataset), RoutedHttp(edgar_route()))
    result = clean_table_response(source, raw, "RUN-1", "filings")
    assert result.ok, result.detail
    validate("filings", result.rows)
    assert set(result.rows["fetch_id"]) == {raw.fetch_id}
    assert not result.rows.duplicated(["instrument_id", "accession", "seq"]).any()
