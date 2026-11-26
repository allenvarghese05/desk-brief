"""``clean_table_response`` and the scoped buffer (card T65). SYNTHETIC FIXTURE: a fake table
source returns invented event rows built in code."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest

from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext
from deskbrief.pipeline.clean import RunBuffer, clean_table_response
from deskbrief.schemas import empty_frame
from deskbrief.sources.base import ParseError
from deskbrief.sources.tables import TableName, is_table_source, table_frame

AT = datetime(2026, 11, 26, 11, 20, tzinfo=UTC)


def _event(event_id: str = "auction:2026-11-27:X", **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": event_id,
        "source": "fiscaldata",
        "dataset": "upcoming_auctions",
        "kind": "auction",
        "title": "4-Week Bill auction",
        "event_date": date(2026, 11, 27),
        "details_json": "{}",
    }
    row.update(kw)
    return row


class TableFake:
    name = "fiscaldata"
    parser_version = "1"

    def __init__(self, result: Any) -> None:
        self.result = result

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        return []

    def fetch(self, req: FetchRequest, http: Any) -> RawResponse:
        raise NotImplementedError

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        return empty_frame("parsed_observations")

    def table_for(self, req: FetchRequest) -> TableName | None:
        return "events"

    def parse_table(self, raw: RawResponse) -> pd.DataFrame:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result  # type: ignore[no-any-return]


def _raw() -> RawResponse:
    req = FetchRequest("fiscaldata", "upcoming_auctions")
    return RawResponse(req, "FETCH-1", AT, AT, 200, "application/json", b"{}")


def test_clean_rows_get_lineage() -> None:
    src = TableFake(table_frame("events", [_event()]))
    assert is_table_source(src)  # type: ignore[arg-type]
    res = clean_table_response(src, _raw(), "RUN-1", "events")  # type: ignore[arg-type]
    assert res.ok
    row = res.rows.iloc[0]
    assert (row["fetch_id"], row["run_id"]) == ("FETCH-1", "RUN-1")
    assert row["ingested_at"] == pd.Timestamp(AT)


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (table_frame("events", [_event(dataset="other")]), "schema_invalid"),  # writer rule
        (table_frame("events", [_event(source="fred")]), "schema_invalid"),
        (table_frame("events", [_event(), _event()]), "schema_invalid"),  # key twice
        (table_frame("events", [_event()]).drop(columns=["title"]), "schema_invalid"),
        (table_frame("events", [_event(kind="meeting")]), "schema_invalid"),
        (ParseError("bad payload"), "parse_error"),
        (KeyError("bug"), "parser_exception"),
        ("not a frame", "parse_error"),
    ],
)
def test_failures_quarantine_the_whole_fetch(result: Any, reason: str) -> None:
    res = clean_table_response(TableFake(result), _raw(), "RUN-1", "events")  # type: ignore[arg-type]
    assert res.reason == reason
    assert res.rows.empty


def test_buffer_withholds_tainted_scopes_and_duplicate_keys() -> None:
    buf = RunBuffer()
    ok = clean_table_response(TableFake(table_frame("events", [_event()])), _raw(), "R", "events")  # type: ignore[arg-type]
    buf.add_table_rows("events", ok.rows)
    assert len(buf.table_rows("events")) == 1
    buf.taint_scope("events", ("fiscaldata", "upcoming_auctions"))
    assert buf.table_rows("events").empty
    assert buf.withheld == [
        "events ('fiscaldata', 'upcoming_auctions'): withheld, a fetch that feeds it failed"
    ]

    dup = RunBuffer()
    dup.add_table_rows("events", ok.rows)
    dup.add_table_rows("events", ok.rows)  # the same key from a second fetch
    assert dup.table_rows("events").empty
    assert "duplicate natural key" in dup.withheld[0]
    with pytest.raises(ValueError, match="not a scoped table"):
        buf.taint_scope("observations", ("x",))  # type: ignore[arg-type]
