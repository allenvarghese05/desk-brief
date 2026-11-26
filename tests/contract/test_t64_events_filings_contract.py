"""Contract tests for card T64 (ruling P-024), widened for T62, T65 and T66: the ``events`` and
``filings`` tables, the ``fiscaldata`` and ``edgar`` sources, and the ``brief``, ``events`` and
``filings`` config sections.

Every frame and config below is a SYNTHETIC FIXTURE built in code. The company is the invented
"Example Corp (EXMP)"; its CIK, accession numbers, insiders, holders and figures are made up.
"""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from zoneinfo import ZoneInfo

import pytest
import yaml
from pandera.errors import SchemaErrors
from pydantic import ValidationError

from deskbrief import schemas
from deskbrief.config import (
    KNOWN_SOURCES,
    SEC_MAX_RATE_PER_MIN,
    BriefSettings,
    ChangeUnit,
    Config,
    EventsSettings,
    FilingsSettings,
    FredRelease,
    NoteSettings,
    RegimeRule,
    RegimeRuleSet,
    RelatedPair,
    SourceSettings,
    load_config,
)
from deskbrief.models import (
    EventKind,
    FilingForm,
    FilingRowKind,
    JobKind,
    RunContext,
    TradeSide,
    Trigger,
)
from deskbrief.schemas import (
    ENUMS,
    EVENTS_NATURAL_KEY,
    EVENTS_SCHEMA,
    FILINGS_NATURAL_KEY,
    FILINGS_SCHEMA,
    OBS_NATURAL_KEY,
    REPLACE_SCOPE,
    TABLE_SCHEMAS,
    TABLES,
    columns,
    current_rows,
    empty_frame,
    to_frame,
    validate,
)
from deskbrief.store import backend
from deskbrief.store.backend import DERIVED_PREFIX, TABLE_LAYER, table_key, table_prefix
from deskbrief.store.datasets import PARTITION_RULES, Datasets, check_partition
from deskbrief.store.duck import DAILY_CLOSE_FIELDS, TABLE_VIEWS
from deskbrief.store.local import LocalBackend

ET = ZoneInfo("America/New_York")
CONFIG_PATH = Path("config.yaml")

#: The fetch instant of the synthetic rows: 06:05 ET on Tue 2026-03-10 (UTC-4 after the DST
#: change), so the ET partition date is 2026-03-10.
INGESTED = datetime(2026, 3, 10, 10, 5, tzinfo=UTC)
PART = date(2026, 3, 10)


# ---------------------------------------------------------------------------------------------
# enums pinned
# ---------------------------------------------------------------------------------------------


def test_event_kind_enum() -> None:
    assert [k.value for k in EventKind] == ["release", "auction"]
    assert ENUMS["event_kind"] == ("release", "auction")


def test_filing_form_enum() -> None:
    expected = ("10-K", "10-K/A", "10-Q", "10-Q/A", "4", "4/A", "13F-HR", "13F-HR/A")
    assert tuple(f.value for f in FilingForm) == expected
    assert ENUMS["filing_form"] == expected


def test_filing_row_and_trade_side_enums() -> None:
    assert ENUMS["filing_row"] == ("filing", "insider_trade", "holder_change")
    assert tuple(r.value for r in FilingRowKind) == ENUMS["filing_row"]
    assert ENUMS["trade_side"] == ("buy", "sell", "other")
    assert tuple(s.value for s in TradeSide) == ENUMS["trade_side"]


def test_existing_enums_unchanged() -> None:
    assert ENUMS["unit"] == ("USD", "pct", "fx_rate", "index_pts", "shares", "index", "contracts")
    assert ENUMS["interval"] == ("tick", "1m", "1d", "1w", "1mo")
    assert ENUMS["job"] == ("morning", "intraday", "eod", "backfill", "rebuild", "history")


def test_known_sources_gain_fiscaldata_and_edgar() -> None:
    assert {"fiscaldata", "edgar"} <= KNOWN_SOURCES
    assert {
        "treasury",
        "fred",
        "ecb",
        "stooq",
        "cboe",
        "alpaca",
        "twelvedata",
        "yahoo",
        "cftc",
    } <= KNOWN_SOURCES
    assert SEC_MAX_RATE_PER_MIN == 600.0  # SEC fair access: 10 requests per second


def test_change_unit_literal_matches_change_rows() -> None:
    assert get_args(ChangeUnit) == ("pct", "bp", "pts", "pp")


# ---------------------------------------------------------------------------------------------
# events table
# ---------------------------------------------------------------------------------------------

EVENTS_COLUMNS = {
    "event_id": "str",
    "source": "str",
    "dataset": "str",
    "kind": "enum:event_kind",
    "title": "str",
    "event_date": "date",
    "scheduled_at": "ts?",
    "period": "str?",
    "url": "str?",
    "details_json": "str",
    "fetch_id": "str",
    "run_id": "str",
    "ingested_at": "ts",
}


def test_events_schema_pinned() -> None:
    assert TABLES["events"] == EVENTS_COLUMNS
    assert columns("events") == tuple(EVENTS_COLUMNS)
    assert EVENTS_NATURAL_KEY == ("source", "dataset", "event_id")
    assert TABLE_SCHEMAS["events"] is EVENTS_SCHEMA


def _event(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": "auction:2026-03-10:7-year",
        "source": "fiscaldata",
        "dataset": "upcoming_auctions",
        "kind": "auction",
        "title": "7-Year Note auction",
        "event_date": date(2026, 3, 10),
        "scheduled_at": datetime(2026, 3, 10, 13, 0, tzinfo=ET),
        "period": None,
        "url": None,
        "details_json": '{"offering_usd":"invented"}',
        "fetch_id": "F1",
        "run_id": "R1",
        "ingested_at": INGESTED,
    }
    row.update(over)
    return row


def test_auction_and_release_rows_validate() -> None:
    release = _event(
        event_id="release:10:2026-03-11",
        source="fred",
        dataset="release_dates",
        kind="release",
        title="Consumer Price Index",
        event_date=date(2026, 3, 11),
        scheduled_at=None,  # date only
        period="2026-02",
        url="https://fred.example.test/releases/10",
    )
    df = to_frame("events", [_event(), release])
    validate("events", df)
    assert df["scheduled_at"].iloc[0] == datetime(2026, 3, 10, 17, 0, tzinfo=UTC)
    assert df["scheduled_at"].isna().iloc[1]


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "earnings"},  # earnings dates are out of scope (P-024)
        # 13:00 ET on the 11th is not on event_date (the 10th)
        {"scheduled_at": datetime(2026, 3, 11, 13, 0, tzinfo=ET)},
        {"event_id": ""},
    ],
)
def test_events_reject_bad_values(bad: dict[str, Any]) -> None:
    with pytest.raises(SchemaErrors):
        validate("events", to_frame("events", [_event(**bad)]))


def test_events_reject_naive_and_missing() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_frame("events", [_event(scheduled_at=datetime(2026, 3, 10, 13, 0))])
    with pytest.raises(ValueError, match="required"):
        to_frame("events", [_event(title=None)])


# ---------------------------------------------------------------------------------------------
# filings table
# ---------------------------------------------------------------------------------------------

FILINGS_COLUMNS = {
    "instrument_id": "str",
    "cik": "str",
    "accession": "str",
    "seq": "int",
    "row_kind": "enum:filing_row",
    "form": "enum:filing_form",
    "filed_date": "date",
    "period": "date?",
    "url": "str",
    "party": "str?",
    "party_cik": "str?",
    "party_role": "str?",
    "trade_date": "date?",
    "trade_code": "str?",
    "side": "enum:trade_side?",
    "shares": "float?",
    "shares_change": "float?",
    "price": "float?",
    "value_usd": "float?",
    "plan_10b5_1": "bool",
    "details_json": "str",
    "amends_accession": "str?",
    "source": "str",
    "dataset": "str",
    "fetch_id": "str",
    "run_id": "str",
    "ingested_at": "ts",
}

EXMP_CIK = "0009999999"  # invented
EXMP_URL = "https://www.sec.gov/Archives/edgar/data/9999999/000999999926000001/"


def test_filings_schema_pinned() -> None:
    assert TABLES["filings"] == FILINGS_COLUMNS
    assert columns("filings") == tuple(FILINGS_COLUMNS)
    assert FILINGS_NATURAL_KEY == ("instrument_id", "accession", "seq")
    assert TABLE_SCHEMAS["filings"] is FILINGS_SCHEMA


def _filing(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "instrument_id": "EXMP",
        "cik": EXMP_CIK,
        "accession": "0009999999-26-000001",
        "seq": 0,
        "row_kind": "filing",
        "form": "10-Q",
        "filed_date": date(2026, 2, 19),
        "period": date(2025, 12, 31),
        "url": EXMP_URL,
        "plan_10b5_1": False,
        "details_json": "{}",
        "source": "edgar",
        "dataset": "submissions",
        "fetch_id": "F1",
        "run_id": "R1",
        "ingested_at": INGESTED,
    }
    row.update(over)
    return row


def _insider_trade(**over: Any) -> dict[str, Any]:
    base = {
        "accession": "0009999999-26-000002",
        "seq": 1,
        "row_kind": "insider_trade",
        "form": "4",
        "filed_date": date(2026, 3, 2),
        "period": None,
        "party": "Jane Example",
        "party_cik": "0008888888",
        "party_role": "Officer: CFO",
        "trade_date": date(2026, 2, 27),
        "trade_code": "S",
        "side": "sell",
        "shares": 1000.0,
        "price": 142.5,
        "value_usd": 142500.0,
        "plan_10b5_1": True,
        "dataset": "form4",
    }
    base.update(over)
    return _filing(**base)


def _holder_change(**over: Any) -> dict[str, Any]:
    base = {
        "accession": "0007777777-26-000003",
        "seq": 1,
        "row_kind": "holder_change",
        "form": "13F-HR",
        "filed_date": date(2026, 2, 13),
        "period": date(2025, 12, 31),  # the 13F as-of quarter
        "party": "Invented Asset Management",
        "party_cik": "0007777777",
        "shares": 50000.0,
        "shares_change": -12000.0,  # a cut: may be negative
        "value_usd": 7130000.0,
        "dataset": "13f",
    }
    base.update(over)
    return _filing(**base)


def test_filing_trade_and_holder_rows_validate() -> None:
    df = to_frame("filings", [_filing(), _insider_trade(), _holder_change()])
    validate("filings", df)
    assert list(df["row_kind"]) == ["filing", "insider_trade", "holder_change"]
    assert list(df["plan_10b5_1"]) == [False, True, False]


@pytest.mark.parametrize(
    "bad",
    [
        {"cik": "9999999"},  # not zero-padded to 10 digits
        {"accession": "000999999926000001"},  # no dashes
        {"form": "8-K"},  # not a stored form
        {"row_kind": "earnings"},
        {"seq": -1},
    ],
)
def test_filings_reject_bad_keys(bad: dict[str, Any]) -> None:
    with pytest.raises(SchemaErrors):
        validate("filings", to_frame("filings", [_filing(**bad)]))


@pytest.mark.parametrize(
    "bad",
    [
        {"side": "short"},
        {"shares": -5.0},  # direction lives in side, never in the sign
        {"price": -1.0},
        {"value_usd": -1.0},
        {"party_cik": "123"},
    ],
)
def test_insider_trade_rejects_bad_values(bad: dict[str, Any]) -> None:
    with pytest.raises(SchemaErrors):
        validate("filings", to_frame("filings", [_insider_trade(**bad)]))


@pytest.mark.parametrize("missing", ["party", "trade_date", "trade_code", "side", "shares"])
def test_insider_trade_rows_carry_their_trade_fields(missing: str) -> None:
    with pytest.raises(SchemaErrors, match="insider_trade rows need"):
        validate("filings", to_frame("filings", [_insider_trade(**{missing: None})]))


@pytest.mark.parametrize("missing", ["party", "party_cik", "shares", "shares_change"])
def test_holder_change_rows_carry_their_holdings(missing: str) -> None:
    with pytest.raises(SchemaErrors, match="holder_change rows need"):
        validate("filings", to_frame("filings", [_holder_change(**{missing: None})]))


def test_thirteen_f_rows_carry_period() -> None:
    with pytest.raises(SchemaErrors, match="13F rows need period"):
        validate("filings", to_frame("filings", [_holder_change(period=None)]))
    filing_13f = _filing(form="13F-HR", accession="0007777777-26-000003", period=None)
    with pytest.raises(SchemaErrors, match="13F rows need period"):
        validate("filings", to_frame("filings", [filing_13f]))


@pytest.mark.parametrize(
    "bad",
    [
        {"seq": 1},  # a filing row is seq 0
        {"row_kind": "insider_trade", "seq": 0},  # seq 0 is only the filing row
    ],
)
def test_seq_zero_is_exactly_the_filing_row(bad: dict[str, Any]) -> None:
    row = _insider_trade(**bad) if bad.get("row_kind") else _filing(**bad)
    with pytest.raises(SchemaErrors, match="seq 0 is the filing row"):
        validate("filings", to_frame("filings", [row]))


def test_facts_sit_on_their_form() -> None:
    with pytest.raises(SchemaErrors, match="facts match their form"):
        validate("filings", to_frame("filings", [_insider_trade(form="10-Q")]))
    with pytest.raises(SchemaErrors, match="facts match their form"):
        validate("filings", to_frame("filings", [_holder_change(form="4")]))


def test_plan_flag_only_on_insider_trades() -> None:
    with pytest.raises(SchemaErrors, match="plan_10b5_1 only"):
        validate("filings", to_frame("filings", [_filing(plan_10b5_1=True)]))


def test_scheduled_at_falls_on_event_date_in_et() -> None:
    # 23:30 ET on the 10th is 03:30 UTC on the 11th: still the 10th in New York
    late = _event(scheduled_at=datetime(2026, 3, 11, 3, 30, tzinfo=UTC))
    validate("events", to_frame("events", [late]))
    early = _event(scheduled_at=datetime(2026, 3, 10, 3, 30, tzinfo=UTC))  # 23:30 ET on the 9th
    with pytest.raises(SchemaErrors, match="scheduled_at must fall on event_date"):
        validate("events", to_frame("events", [early]))


# ---------------------------------------------------------------------------------------------
# current_rows: the newest run replaces a whole scope (review B1)
# ---------------------------------------------------------------------------------------------

RUN1_AT = datetime(2026, 3, 9, 10, 5, tzinfo=UTC)  # Monday's run
RUN2_AT = datetime(2026, 3, 10, 10, 5, tzinfo=UTC)  # Tuesday's run


def _cpi(day: int, run: str, at: datetime) -> dict[str, Any]:
    return _event(
        event_id=f"release:10:2026-03-{day:02d}",
        source="fred",
        dataset="release_dates",
        kind="release",
        title="Consumer Price Index",
        event_date=date(2026, 3, day),
        scheduled_at=datetime(2026, 3, day, 8, 30, tzinfo=ET),
        run_id=run,
        fetch_id=f"F-{run}",
        ingested_at=at,
    )


def _log(
    run: str,
    at: datetime,
    status: str = "ok",
    dataset: str = "release_dates",
    quarantined: int = 0,
) -> dict[str, Any]:
    return {
        "fetch_id": f"F-{run}-{dataset}-{status}-{quarantined}",
        "run_id": run,
        "source": "fred",
        "dataset": dataset,
        "symbols": "",
        "request_redacted": "GET /releases/dates",
        "requested_at": at,
        "responded_at": at,
        "latency_ms": 100,
        "http_status": 200 if status in ("ok", "empty") else 500,
        "status": status,
        "bytes": 10,
        "rows_parsed": 0,
        "rows_quarantined": quarantined,
        "attempt": 1,
    }


def test_replace_scopes_pinned() -> None:
    assert REPLACE_SCOPE == {
        "events": ("source", "dataset"),
        "filings": ("instrument_id", "accession"),
    }


def test_rescheduled_event_disappears_from_its_old_date() -> None:
    """Monday's fetch had CPI on the 11th; Tuesday's has it moved to the 12th."""
    auction = _event(run_id="R1", fetch_id="F-R1-a", ingested_at=RUN1_AT)  # another dataset
    rows = to_frame("events", [_cpi(11, "R1", RUN1_AT), auction, _cpi(12, "R2", RUN2_AT)])
    current = current_rows("events", rows)
    validate("events", current)
    cpi = current[current["source"] == "fred"]
    assert list(cpi["event_date"]) == [date(2026, 3, 12)]
    # the fiscaldata scope was not re-fetched: Monday's auction stays current
    assert list(current[current["source"] == "fiscaldata"]["run_id"]) == ["R1"]


def test_newer_successful_empty_fetch_empties_the_scope() -> None:
    rows = to_frame("events", [_cpi(11, "R1", RUN1_AT)])
    log = to_frame("source_log", [_log("R1", RUN1_AT), _log("R2", RUN2_AT, "empty")])
    assert current_rows("events", rows, log).empty
    # without the log the reader cannot know; it keeps the last rows written
    assert len(current_rows("events", rows)) == 1


def test_newer_failed_fetch_keeps_the_previous_rows() -> None:
    rows = to_frame("events", [_cpi(11, "R1", RUN1_AT)])
    log = to_frame(
        "source_log",
        [_log("R1", RUN1_AT), _log("R2", RUN2_AT, "empty"), _log("R2", RUN2_AT, "error")],
    )
    assert list(current_rows("events", rows, log)["run_id"]) == ["R1"]


def test_scope_rule_needs_events_dataset_to_match_source_log() -> None:
    """Writer rule: ``events.dataset`` is the fetch's ``source_log`` dataset. A genuinely empty
    fetch logged under another dataset name cannot empty the scope (and must not)."""
    rows = to_frame("events", [_cpi(11, "R1", RUN1_AT)])
    log = to_frame(
        "source_log", [_log("R1", RUN1_AT), _log("R2", RUN2_AT, "empty", dataset="other")]
    )
    assert list(current_rows("events", rows, log)["run_id"]) == ["R1"]
    same = to_frame("source_log", [_log("R1", RUN1_AT), _log("R2", RUN2_AT, "empty")])
    assert current_rows("events", rows, same).empty
    doc = current_rows.__doc__ or ""
    assert "``events.dataset`` must be the fetch's dataset" in doc
    assert "exactly the ``source_log`` source and dataset" in (schemas.__doc__ or "")


def test_newer_ok_but_quarantined_run_keeps_the_previous_rows() -> None:
    """N1: a 200 whose parse failed is logged ``ok`` with rows quarantined and writes nothing."""
    rows = to_frame("events", [_cpi(11, "R1", RUN1_AT)])
    log = to_frame("source_log", [_log("R1", RUN1_AT), _log("R2", RUN2_AT, "ok", quarantined=1)])
    assert list(current_rows("events", rows, log)["run_id"]) == ["R1"]


def test_newer_ok_run_with_no_rows_keeps_the_previous_rows() -> None:
    """N1: ``ok`` with zero rows written is not "nothing scheduled"; only ``empty`` says that."""
    rows = to_frame("events", [_cpi(11, "R1", RUN1_AT)])
    log = to_frame("source_log", [_log("R1", RUN1_AT), _log("R2", RUN2_AT, "ok")])
    assert list(current_rows("events", rows, log)["run_id"]) == ["R1"]


def test_newer_empty_run_with_quarantine_keeps_the_previous_rows() -> None:
    rows = to_frame("events", [_cpi(11, "R1", RUN1_AT)])
    log = to_frame("source_log", [_log("R1", RUN1_AT), _log("R2", RUN2_AT, "empty", quarantined=1)])
    assert list(current_rows("events", rows, log)["run_id"]) == ["R1"]


def test_genuine_empty_replaces_and_only_when_newer() -> None:
    rows = to_frame("events", [_cpi(11, "R2", RUN2_AT)])
    newer_empty = to_frame("source_log", [_log("R3", RUN2_AT + timedelta(days=1), "empty")])
    assert current_rows("events", rows, newer_empty).empty
    older_empty = to_frame("source_log", [_log("R1", RUN1_AT, "empty")])
    assert list(current_rows("events", rows, older_empty)["run_id"]) == ["R2"]
    # a run whose fetches were partly empty and partly quarantined is not genuinely empty
    mixed = to_frame(
        "source_log",
        [
            _log("R3", RUN2_AT + timedelta(days=1), "empty"),
            _log("R3", RUN2_AT + timedelta(days=1), "ok", quarantined=2),
        ],
    )
    assert list(current_rows("events", rows, mixed)["run_id"]) == ["R2"]


def test_newest_means_latest_run_ties_go_to_the_larger_run_id() -> None:
    same_time = [_cpi(11, "01AAA", RUN2_AT), _cpi(12, "01BBB", RUN2_AT)]
    current = current_rows("events", to_frame("events", same_time))
    assert list(current["run_id"]) == ["01BBB"]
    doc = current_rows.__doc__ or ""
    assert "serialised" in doc
    assert "ties go to the" in doc


def test_current_rows_rejects_naive_and_normalises_zones() -> None:
    rows = to_frame("events", [_cpi(11, "R1", RUN1_AT), _cpi(12, "R2", RUN2_AT)])
    naive = rows.assign(ingested_at=rows["ingested_at"].dt.tz_localize(None))
    with pytest.raises(ValueError, match="tz-aware"):
        current_rows("events", naive)
    log = to_frame("source_log", [_log("R3", RUN2_AT + timedelta(days=1), "empty")])
    naive_log = log.assign(responded_at=log["responded_at"].dt.tz_localize(None))
    with pytest.raises(ValueError, match="tz-aware"):
        current_rows("events", rows, naive_log)
    # an aware but non-UTC frame is compared in UTC and returned unchanged in type
    in_et = rows.assign(ingested_at=rows["ingested_at"].dt.tz_convert(ET))
    current = current_rows("events", in_et)
    assert list(current["run_id"]) == ["R2"]
    assert str(current["ingested_at"].dt.tz) == "America/New_York"


def _trades(run: str, at: datetime, n: int) -> list[dict[str, Any]]:
    common = {"run_id": run, "fetch_id": f"F-{run}", "ingested_at": at}
    out = [
        _filing(accession="0009999999-26-000002", form="4", period=None, dataset="form4", **common)
    ]
    out += [_insider_trade(seq=i, **common) for i in range(1, n + 1)]
    return out


def test_reparse_with_fewer_facts_leaves_no_phantom_rows() -> None:
    """Monday's parse found three trades in one Form 4; Tuesday's re-parse finds two."""
    other = _filing(run_id="R1", fetch_id="F-R1", ingested_at=RUN1_AT)  # another accession
    rows = to_frame("filings", [*_trades("R1", RUN1_AT, 3), other, *_trades("R2", RUN2_AT, 2)])
    current = current_rows("filings", rows)
    validate("filings", current)
    form4 = current[current["accession"] == "0009999999-26-000002"]
    assert list(form4["seq"]) == [0, 1, 2]
    assert set(form4["run_id"]) == {"R2"}
    assert list(current[current["accession"] == "0009999999-26-000001"]["run_id"]) == ["R1"]


def test_one_run_writing_an_accession_from_two_fetches_stays_whole() -> None:
    """The filing row and its facts may come from two fetches of the same run."""
    filing_row = _trades("R2", RUN1_AT, 0)  # seq 0, fetched first
    facts = [
        _insider_trade(seq=i, run_id="R2", fetch_id="F-R2-b", ingested_at=RUN2_AT) for i in (1, 2)
    ]
    current = current_rows("filings", to_frame("filings", [*filing_row, *facts]))
    assert list(current["seq"]) == [0, 1, 2]


def test_current_rows_keeps_dtypes_and_handles_empty() -> None:
    assert current_rows("events", empty_frame("events")).empty
    assert current_rows("filings", empty_frame("filings")).empty
    rows = to_frame("filings", _trades("R1", RUN1_AT, 1))
    current = current_rows("filings", rows)
    assert list(current.dtypes) == list(rows.dtypes)


ORIGINAL = "0009999999-26-000002"  # the Form 4 in _trades
AMENDMENT = "0009999999-26-000009"


def _amendment(run: str, at: datetime) -> list[dict[str, Any]]:
    common = {"run_id": run, "fetch_id": f"F-{run}-a", "ingested_at": at}
    return [
        _filing(
            accession=AMENDMENT,
            form="4/A",
            period=None,
            dataset="form4",
            amends_accession=ORIGINAL,
            **common,
        ),
        _insider_trade(accession=AMENDMENT, form="4/A", seq=1, **common),
    ]


def test_an_amended_filing_is_superseded() -> None:
    rows = to_frame("filings", [*_trades("R1", RUN1_AT, 3), *_amendment("R2", RUN2_AT)])
    validate("filings", rows)
    current = current_rows("filings", rows)
    assert set(current["accession"]) == {AMENDMENT}  # the original's 4 rows are gone
    assert list(current["seq"]) == [0, 1]
    doc = schemas.__doc__ or ""
    assert "``amends_accession``" in doc


def test_an_amendment_of_another_instrument_supersedes_nothing() -> None:
    other = [{**r, "instrument_id": "OTHER"} for r in _amendment("R2", RUN2_AT)]
    rows = to_frame("filings", [*_trades("R1", RUN1_AT, 1), *other])
    assert set(current_rows("filings", rows)["accession"]) == {ORIGINAL, AMENDMENT}


@pytest.mark.parametrize(
    "bad",
    [
        {"amends_accession": ORIGINAL},  # on a 10-Q, not an /A form
        {"form": "10-Q/A", "amends_accession": "0009999999-26-000001"},  # amends itself
        {"form": "10-Q/A", "amends_accession": "not-an-accession"},
    ],
)
def test_amends_accession_rules(bad: dict[str, Any]) -> None:
    with pytest.raises(SchemaErrors):
        validate("filings", to_frame("filings", [_filing(**bad)]))


def test_old_accessions_are_filtered_by_date_not_replaced() -> None:
    """Each accession is its own scope: last quarter's 10-Q stays current next to this one, so
    readers pick by ``filed_date`` / ``period`` (documented)."""
    old = _filing(accession="0009999999-25-000001", filed_date=date(2025, 11, 1))
    rows = to_frame("filings", [old, _filing(run_id="R2", ingested_at=RUN2_AT)])
    assert len(current_rows("filings", rows)) == 2
    doc = schemas.__doc__ or ""
    assert "old accessions are never replaced" in doc
    assert "``filings.insider_days``" in doc
    assert "latest ``period``" in doc


# ---------------------------------------------------------------------------------------------
# store: layer, partition, round trip; neither table is an observation table
# ---------------------------------------------------------------------------------------------


def test_new_tables_live_under_derived() -> None:
    assert TABLE_LAYER["events"] == DERIVED_PREFIX
    assert TABLE_LAYER["filings"] == DERIVED_PREFIX
    assert table_prefix("events") == "derived/events/"
    assert (
        table_key(table="filings", part_date=PART, run_id="R1")
        == "derived/filings/date=2026-03-10/run=R1.parquet"
    )
    doc = backend.__doc__ or ""
    assert "events              ET date of ``ingested_at``" in doc
    assert "filings             ET date of ``ingested_at``" in doc


def test_partition_is_the_et_date_of_ingested_at() -> None:
    assert PARTITION_RULES["events"][0] == "ingested_at"
    assert PARTITION_RULES["filings"][0] == "ingested_at"
    # the event is next week, the filing last month: both belong to the fetch date's partition
    events = to_frame("events", [_event(event_date=date(2026, 3, 17))])
    filings = to_frame("filings", [_filing()])
    check_partition("events", events, PART)
    check_partition("filings", filings, PART)
    late = to_frame("events", [_event(ingested_at=datetime(2026, 3, 11, 3, 0, tzinfo=UTC))])
    check_partition("events", late, PART)  # 23:00 ET on the 10th
    with pytest.raises(ValueError, match="not partition date"):
        check_partition("events", events, date(2026, 3, 9))


def _ctx(store: LocalBackend) -> RunContext:
    return RunContext(
        run_id="RUNT64",
        job=JobKind.MORNING,
        trigger=Trigger.MANUAL,
        target_date=PART,
        now_utc=INGESTED,
        git_sha="deadbeef",
        config_hash="cafe",
        config=load_config(CONFIG_PATH),
        store=store,
    )


def test_append_and_read_round_trip(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path)
    ds = Datasets(store)
    ctx = _ctx(store)
    key_e = ds.append("events", to_frame("events", [_event()]), ctx, PART)
    rows = [_filing(), _insider_trade(), _holder_change()]
    key_f = ds.append("filings", to_frame("filings", rows), ctx, PART)
    assert key_e == "derived/events/date=2026-03-10/run=RUNT64.parquet"
    assert key_f == "derived/filings/date=2026-03-10/run=RUNT64.parquet"
    events, filings = ds.read("events"), ds.read("filings")
    validate("events", events)
    validate("filings", filings)
    assert list(filings["seq"]) == [0, 1, 1]
    assert filings["shares_change"].iloc[2] == -12000.0
    # zero-row frames are valid too (a run with nothing scheduled)
    validate("events", empty_frame("events"))
    validate("filings", empty_frame("filings"))


def test_neither_table_is_an_observation_table() -> None:
    assert "events" not in TABLE_VIEWS.values()
    assert "filings" not in TABLE_VIEWS.values()
    assert TABLE_VIEWS["obs_all"] == "observations"
    for table in ("events", "filings"):
        cols = set(columns(table))
        assert not {"value", "field", "interval", "as_of", "tier"} & cols
        assert not set(OBS_NATURAL_KEY) <= cols
    assert DAILY_CLOSE_FIELDS == ("close", "yield", "rate", "level")


# ---------------------------------------------------------------------------------------------
# source settings: fiscaldata, edgar, the SEC User-Agent env var and rate limit
# ---------------------------------------------------------------------------------------------


def test_real_config_has_fiscaldata_and_edgar_disabled_until_t65() -> None:
    cfg = load_config(CONFIG_PATH)
    fiscal, edgar = cfg.sources["fiscaldata"], cfg.sources["edgar"]
    assert not fiscal.enabled
    assert not edgar.enabled
    assert edgar.user_agent_env == "DESKBRIEF_SEC_USER_AGENT"
    assert edgar.user_agent is None  # nothing committed
    assert edgar.rate_per_min <= SEC_MAX_RATE_PER_MIN
    assert edgar.rate_per_min == 300  # 5 per second, half SEC's limit


def test_user_agent_env_is_exclusive_with_user_agent() -> None:
    base = {"rate_per_min": 10, "timeout_s": 5, "retries": 1}
    assert SourceSettings.model_validate(base).user_agent_env is None
    s = SourceSettings.model_validate({**base, "user_agent_env": "DESKBRIEF_SEC_USER_AGENT"})
    assert s.user_agent_env == "DESKBRIEF_SEC_USER_AGENT"
    with pytest.raises(ValidationError, match="not both"):
        SourceSettings.model_validate(
            {**base, "user_agent": "x", "user_agent_env": "DESKBRIEF_SEC_USER_AGENT"}
        )
    with pytest.raises(ValidationError):
        SourceSettings.model_validate({**base, "user_agent_env": "has space"})


def _raw() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


def test_edgar_requires_user_agent_env() -> None:
    raw = _raw()
    del raw["sources"]["edgar"]["user_agent_env"]
    with pytest.raises(ValidationError, match="user_agent_env is required"):
        Config.model_validate(raw)


def test_edgar_rate_above_sec_limit_rejected() -> None:
    raw = _raw()
    raw["sources"]["edgar"]["rate_per_min"] = 601
    with pytest.raises(ValidationError, match="exceeds SEC"):
        Config.model_validate(raw)
    raw["sources"]["edgar"]["rate_per_min"] = 600
    Config.model_validate(raw)


# ---------------------------------------------------------------------------------------------
# brief: regime rules, related pairs, note
# ---------------------------------------------------------------------------------------------


def test_real_config_regime_rules() -> None:
    brief = load_config(CONFIG_PATH).brief
    assert [s.label for s in brief.regime_rules] == ["Risk-off", "Risk-on"]
    assert (brief.quiet_label, brief.mixed_label, brief.unclear_label) == (
        "Quiet",
        "Mixed",
        "No clear read",
    )
    risk_off = brief.regime_rules[0]
    assert risk_off.min_met == 2
    assert [(r.market, r.direction) for r in risk_off.rules] == [
        ("ES_F", "down"),
        ("VIX", "up"),
        ("HY_OAS", "up"),
        ("UST_2Y", "down"),
    ]
    assert [r.label for r in risk_off.rules] == [
        "Equity futures down",
        "VIX up",
        "High-yield spreads wider",
        "Front-end yields down",
    ]
    risk_on = brief.regime_rules[1]
    # the mirror image: same markets, opposite directions
    flip = {"up": "down", "down": "up"}
    assert [(r.market, flip[r.direction]) for r in risk_on.rules] == [
        (r.market, r.direction) for r in risk_off.rules
    ]
    assert all(
        r.min_sigma == 1.0 and r.min_change is None for s in brief.regime_rules for r in s.rules
    )


def test_real_config_related_pairs() -> None:
    pairs = load_config(CONFIG_PATH).brief.related_pairs
    assert [(p.pair, p.limit, p.unit) for p in pairs] == [
        (("BRENT", "WTI"), 1.0, "pct"),
        (("GOLD_F", "GLD"), 1.0, "pct"),
    ]


def test_real_config_note() -> None:
    note = load_config(CONFIG_PATH).brief.note
    assert note.enabled
    assert note.model == "claude-sonnet-5-5"
    assert note.api_key_env == "ANTHROPIC_API_KEY"
    assert note.max_sentences == 5
    assert note.timeout_s == 30
    assert note.effort == "low"
    assert note.max_output_tokens == 8000
    assert note.monthly_cap_usd == 5.0
    assert (note.price_input_usd_per_mtok, note.price_output_usd_per_mtok) == (2.0, 10.0)
    for word in ("buy", "sell", "target", "will rise", "will fall", "recommend", "predict"):
        assert word in note.forbidden_words


def test_rule_needs_exactly_one_threshold() -> None:
    ok = RegimeRule.model_validate(
        {"market": "VIX", "direction": "up", "label": "VIX up", "min_sigma": 1.5}
    )
    assert ok.min_change is None
    size = RegimeRule.model_validate(
        {
            "market": "VIX",
            "direction": "up",
            "label": "VIX up",
            "min_change": 2,
            "change_unit": "pts",
        }
    )
    assert (size.min_change, size.change_unit) == (2, "pts")
    for bad in (
        {},  # neither
        {"min_sigma": 1.0, "min_change": 2, "change_unit": "pts"},  # both
        {"min_change": 2},  # size without its unit
        {"min_sigma": 1.0, "change_unit": "pts"},  # unit without a size
        {"min_sigma": 0},  # not positive
        {"min_change": 2, "change_unit": "furlongs"},
    ):
        with pytest.raises(ValidationError):
            RegimeRule.model_validate({"market": "VIX", "direction": "up", "label": "x", **bad})
    with pytest.raises(ValidationError):
        RegimeRule.model_validate(
            {"market": "VIX", "direction": "sideways", "label": "x", "min_sigma": 1}
        )


def test_rule_set_min_met_must_be_reachable() -> None:
    rule = {"market": "VIX", "direction": "up", "label": "VIX up", "min_sigma": 1}
    RegimeRuleSet.model_validate({"label": "Risk-off", "rules": [rule], "min_met": 1})
    for min_met in (0, 2):
        with pytest.raises(ValidationError):
            RegimeRuleSet.model_validate({"label": "Risk-off", "rules": [rule], "min_met": min_met})
    with pytest.raises(ValidationError):
        RegimeRuleSet.model_validate({"label": "Risk-off", "rules": [], "min_met": 1})


def test_brief_labels_unique_and_not_reserved() -> None:
    rule = {"market": "VIX", "direction": "up", "label": "VIX up", "min_sigma": 1}
    one = {"label": "Risk-off", "rules": [rule], "min_met": 1}
    BriefSettings.model_validate({"regime_rules": [one]})
    with pytest.raises(ValidationError, match="duplicate labels"):
        BriefSettings.model_validate({"regime_rules": [one, one]})
    with pytest.raises(ValidationError, match="reserved"):
        BriefSettings.model_validate({"regime_rules": [{**one, "label": "Quiet"}]})
    with pytest.raises(ValidationError, match="reserved"):
        BriefSettings.model_validate({"regime_rules": [{**one, "label": "No clear read"}]})
    with pytest.raises(ValidationError, match="must differ"):
        BriefSettings.model_validate({"quiet_label": "Calm", "mixed_label": "Calm"})
    with pytest.raises(ValidationError, match="must differ"):
        BriefSettings.model_validate({"unclear_label": "Mixed"})


def _rule(market: str, direction: str) -> dict[str, Any]:
    return {
        "market": market,
        "direction": direction,
        "label": f"{market} {direction}",
        "min_sigma": 1,
    }


READ = BriefSettings.model_validate(
    {
        "regime_rules": [
            {
                "label": "Risk-off",
                "min_met": 2,
                "rules": [_rule("ES_F", "down"), _rule("VIX", "up"), _rule("HY_OAS", "up")],
            },
            {
                "label": "Risk-on",
                "min_met": 2,
                "rules": [_rule("ES_F", "up"), _rule("VIX", "down"), _rule("HY_OAS", "down")],
            },
        ]
    }
)


@pytest.mark.parametrize(
    ("n_ranked", "met", "label"),
    [
        (0, {}, "Quiet"),  # nothing ranked
        (0, {"Risk-off": 3}, "Quiet"),  # rules never outrank "nothing ranked"
        (3, {"Risk-off": 2}, "Risk-off"),  # min_met reached, no other label touched
        (3, {"Risk-off": 3, "Risk-on": 0}, "Risk-off"),
        (2, {"Risk-on": 2}, "Risk-on"),
        (3, {"Risk-off": 2, "Risk-on": 1}, "Mixed"),  # the rules split
        (3, {"Risk-off": 1, "Risk-on": 1}, "Mixed"),
        (3, {"Risk-off": 1}, "No clear read"),  # some met, not enough
        (3, {}, "No clear read"),  # moves ranked, no rule met
        (3, {"Unknown": 5}, "No clear read"),  # counts for labels that are not rule sets
    ],
)
def test_read_label(n_ranked: int, met: dict[str, int], label: str) -> None:
    assert READ.read_label(n_ranked, met) == label


def test_real_config_read_labels() -> None:
    brief = load_config(CONFIG_PATH).brief
    assert brief.read_label(0, {}) == "Quiet"
    assert brief.read_label(4, {"Risk-off": 4}) == "Risk-off"
    assert brief.read_label(4, {"Risk-on": 2}) == "Risk-on"
    assert brief.read_label(4, {"Risk-off": 2, "Risk-on": 1}) == "Mixed"
    assert brief.read_label(4, {"Risk-off": 1}) == "No clear read"


def test_related_pair_shape() -> None:
    rp = RelatedPair.model_validate({"pair": ["BRENT", "WTI"], "limit": 1.0})
    assert rp.unit == "pct"
    for bad in (
        {"pair": ["WTI", "WTI"], "limit": 1.0},
        {"pair": ["BRENT", "WTI"], "limit": 0},
        {"pair": ["BRENT"], "limit": 1.0},
        {"pair": ["BRENT", "WTI"], "limit": 1.0, "unit": "furlongs"},
    ):
        with pytest.raises(ValidationError):
            RelatedPair.model_validate(bad)
    with pytest.raises(ValidationError, match="duplicate pair"):
        BriefSettings.model_validate(
            {
                "related_pairs": [
                    {"pair": ["BRENT", "WTI"], "limit": 1},
                    {"pair": ["WTI", "BRENT"], "limit": 2},
                ]
            }
        )


def test_note_defaults_and_bounds() -> None:
    note = NoteSettings()
    assert note.model == "claude-sonnet-5-5"
    assert note.monthly_cap_usd == 5.0
    assert note.forbidden_words == ()
    assert note.effort == "low"
    assert note.max_output_tokens == 8000  # thinking counts against it; the note is ~200
    for bad in (
        {"effort": "minimal"},
        {"max_output_tokens": 1999},  # too small: thinking could starve the visible text
        {"monthly_cap_usd": 0},
        {"max_sentences": 0},
        {"timeout_s": 0},
        {"api_key_env": "lower"},
        {"forbidden_words": ["Buy"]},
        {"forbidden_words": [" sell"]},
        {"forbidden_words": ["buy", "buy"]},
        {"forbidden_words": [""]},
    ):
        with pytest.raises(ValidationError):
            NoteSettings.model_validate(bad)


def test_brief_cross_references_known_markets() -> None:
    raw = _raw()
    raw["brief"]["regime_rules"][0]["rules"][0]["market"] = "NOPE"
    with pytest.raises(ValidationError, match="unknown market 'NOPE'"):
        Config.model_validate(raw)
    raw = _raw()
    raw["brief"]["related_pairs"][0]["pair"] = ["BRENT", "NOPE"]
    with pytest.raises(ValidationError, match="unknown market 'NOPE'"):
        Config.model_validate(raw)


def test_brief_section_is_optional() -> None:
    raw = _raw()
    del raw["brief"]
    cfg = Config.model_validate(raw)
    assert cfg.brief == BriefSettings()
    assert cfg.brief.regime_rules == ()
    assert cfg.brief.note.model == "claude-sonnet-5-5"


# ---------------------------------------------------------------------------------------------
# events and filings settings (T65)
# ---------------------------------------------------------------------------------------------


def test_events_settings() -> None:
    cfg = load_config(CONFIG_PATH)
    assert cfg.events == EventsSettings()
    assert cfg.events.horizon_days == 7
    assert cfg.events.fred_releases == ()  # T65 lists them after its probe
    rel = FredRelease.model_validate(
        {"release_id": 10, "title": "Consumer Price Index", "time_et": "08:30"}
    )
    assert rel.time_et == "08:30"
    for bad in (
        {"release_id": 0, "title": "x", "time_et": "08:30"},
        {"release_id": "10", "title": "x", "time_et": "08:30"},
        {"release_id": 10, "title": "x", "time_et": "8.30"},
    ):
        with pytest.raises(ValidationError):
            FredRelease.model_validate(bad)
    dup = {"release_id": 10, "title": "x", "time_et": "08:30"}
    with pytest.raises(ValidationError, match="duplicate release_id"):
        EventsSettings.model_validate({"fred_releases": [dup, dup]})
    for bad_horizon in (0, 32):
        with pytest.raises(ValidationError):
            EventsSettings.model_validate({"horizon_days": bad_horizon})


def test_events_fred_releases_need_fred() -> None:
    raw = _raw()
    raw["events"] = {"fred_releases": [{"release_id": 10, "title": "x", "time_et": "08:30"}]}
    Config.model_validate(raw)
    del raw["sources"]["fred"]
    # fred feeds would also complain; the events error must be among the reasons
    with pytest.raises(ValidationError, match=r"events\.fred_releases needs the fred source"):
        Config.model_validate(raw)


def test_filings_settings() -> None:
    cfg = load_config(CONFIG_PATH)
    assert cfg.filings == FilingsSettings()
    assert cfg.filings.insider_days == 90
    company = {"instrument": "AAPL", "cik": EXMP_CIK, "cusip": "EXMP00010"}  # invented
    s = FilingsSettings.model_validate(
        {"companies": [company], "holders": [{"cik": "0007777777", "name": "Invented AM"}]}
    )
    assert s.companies[0].cik == EXMP_CIK
    for bad in (
        {"companies": [{**company, "cik": "123"}]},
        {"companies": [{**company, "cusip": "short"}]},
        {"companies": [company, company]},  # instrument ids stay unique
        {"holders": [{"cik": "0007777777", "name": "a"}, {"cik": "0007777777", "name": "b"}]},
        {"insider_days": 0},
    ):
        with pytest.raises(ValidationError):
            FilingsSettings.model_validate(bad)


def test_two_instruments_may_share_an_issuer_cik() -> None:
    """Share classes: one issuer, two listed instruments (ruling 4)."""
    s = FilingsSettings.model_validate(
        {
            "companies": [
                {"instrument": "AAPL", "cik": EXMP_CIK},
                {"instrument": "MSFT", "cik": EXMP_CIK},
            ]
        }
    )
    assert [c.cik for c in s.companies] == [EXMP_CIK, EXMP_CIK]


def test_filings_companies_must_be_configured_stocks() -> None:
    raw = _raw()
    raw["filings"] = {"companies": [{"instrument": "AAPL", "cik": EXMP_CIK}]}
    assert Config.model_validate(raw).filings.companies[0].instrument == "AAPL"
    raw["filings"] = {"companies": [{"instrument": "SPY", "cik": EXMP_CIK}]}
    with pytest.raises(ValidationError, match="SPY is a etf, not a stock"):
        Config.model_validate(raw)
    raw["filings"] = {"companies": [{"instrument": "EXMP", "cik": EXMP_CIK}]}
    with pytest.raises(ValidationError, match="unknown instrument 'EXMP'"):
        Config.model_validate(raw)
    raw["filings"] = {"companies": [{"instrument": "AAPL", "cik": EXMP_CIK}]}
    del raw["sources"]["edgar"]
    with pytest.raises(ValidationError, match="filings needs the edgar source"):
        Config.model_validate(raw)


def test_existing_sections_unchanged_by_defaults() -> None:
    """A config written before T64 (no brief/events/filings, no new sources) still loads."""
    raw = _raw()
    for key in ("brief", "events", "filings"):
        raw.pop(key, None)
    for src in ("fiscaldata", "edgar"):
        del raw["sources"][src]
    cfg = Config.model_validate(raw)
    assert cfg.brief == BriefSettings()
    assert cfg.events == EventsSettings()
    assert cfg.filings == FilingsSettings()
