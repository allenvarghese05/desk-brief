"""Treasury upcoming auctions from fiscaldata.treasury.gov (card T65, P-024 "Today").

Endpoint: ``v1/accounting/od/upcoming_auctions`` of the Fiscal Data API (no key). One request
per ``morning`` run asks for every auction whose ``auction_date`` is in
``[target_date, target_date + events.horizon_days]``:

* ``filter=auction_date:gte:<start>,auction_date:lte:<end>``, ``sort=auction_date``,
  ``page[size]=100`` (a 31-day horizon holds far fewer auctions; more than one page is a
  :class:`ParseError`, because writing half the scope would break the all-or-nothing rule).

Fields used (every value arrives as a string; ``"null"`` means not given): ``record_date``,
``security_type`` (Bill, Note, Bond, ...), ``security_term`` ("4-Week", "10-Year"),
``reopening``, ``cusip``, ``offering_amt`` (USD), ``announcemt_date``, ``auction_date``,
``issue_date``. The dataset gives no auction close time, so ``scheduled_at`` is null.

Rows (``events``): ``kind = auction``, ``event_date`` = ``auction_date``, title
``"<term> <type> auction"`` ("4-Week Bill auction"), ``details_json`` with the type, term,
reopening, CUSIP, offering amount (USD), announcement, issue and record dates. Rows outside the
requested window are dropped.

Identity (PR #102 review): an auction is a slot ``(auction_date, security_term,
security_type)``. When the response holds several ``record_date`` snapshots of a slot, only the
newest snapshot is kept, so an auction published first without a CUSIP and later with one is
one event, not two. Within that snapshot one CUSIP on one date is one auction (the last record
wins). ``event_id = auction:<auction_date>:<cusip or term-type>``; two CUSIP-less auctions in
one slot get ``-1``, ``-2`` ... in a stable order. A reopening has a new auction date, so it is
a new event even with the same CUSIP. Ids are unique within a fetch, which is all the writer
rules need (readers replace the whole ``(source, dataset)`` scope per run).

Probe status and terms: ``docs/sources/fiscaldata.md``. Payloads in tests are SYNTHETIC.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from typing import Any

import pandas as pd

from deskbrief.config import Config
from deskbrief.models import EventKind, FetchRequest, JobKind, RawResponse, RunContext
from deskbrief.schemas import empty_frame
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.sources.registry import register
from deskbrief.sources.tables import FISCALDATA_AUCTIONS, TableName, table_frame

SOURCE = FISCALDATA_AUCTIONS.source
DATASET = FISCALDATA_AUCTIONS.dataset
ENDPOINT = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/"
    "upcoming_auctions"
)
PAGE_SIZE = 100
PAGE_URL = "https://fiscaldata.treasury.gov/datasets/upcoming-auctions/"
_NULLS = {"", "null", "none", "n/a"}


def _text(record: dict[str, Any], key: str) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ParseError(f"fiscaldata: {key} must be a string, got {type(value).__name__}")
    value = value.strip()
    return None if value.lower() in _NULLS else value


def _date(record: dict[str, Any], key: str, *, required: bool = False) -> date | None:
    text = _text(record, key)
    if text is None:
        if required:
            raise ParseError(f"fiscaldata: record has no {key}")
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ParseError(f"fiscaldata: bad {key} {text!r}") from exc


def _amount(record: dict[str, Any], key: str) -> float | None:
    text = _text(record, key)
    if text is None:
        return None
    try:
        value = float(text.replace(",", "").replace("$", ""))
    except ValueError as exc:
        raise ParseError(f"fiscaldata: bad {key} {text!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise ParseError(f"fiscaldata: {key} is not a non-negative number: {text!r}")
    return value


def _window(params: dict[str, str]) -> tuple[date | None, date | None]:
    """``(start, end)`` from the request's ``filter`` (absent bounds are ``None``)."""
    lo = hi = None
    for part in params.get("filter", "").split(","):
        bits = part.split(":")
        if len(bits) == 3 and bits[0] == "auction_date":
            try:
                day = date.fromisoformat(bits[2])
            except ValueError:
                continue
            if bits[1] == "gte":
                lo = day
            elif bits[1] == "lte":
                hi = day
    return lo, hi


class FiscalDataSource:
    """Treasury upcoming auctions; a table source for ``events`` (``sources/tables.py``)."""

    name = SOURCE
    parser_version = "1"
    table: TableName = "events"

    def __init__(self, config: Config) -> None:
        self.config = config

    def table_for(self, req: FetchRequest) -> TableName | None:
        return "events" if req.dataset == DATASET else None

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        """One request in the ``morning`` job; every other job plans nothing (Today is a
        morning section, and a catch-up run for a past day must not refetch the calendar)."""
        if job is not JobKind.MORNING:
            return []
        start = ctx.target_date
        end = start + timedelta(days=self.config.events.horizon_days)
        params = {
            "filter": f"auction_date:gte:{start.isoformat()},auction_date:lte:{end.isoformat()}",
            "sort": "auction_date",
            "page[size]": str(PAGE_SIZE),
        }
        return [FetchRequest(SOURCE, DATASET, params=params, target_date=start)]

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        return http.get(req, ENDPOINT, params=req.params)

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        """No observations: the auctions go to ``events`` through :meth:`parse_table`."""
        return empty_frame("parsed_observations")

    def parse_table(self, raw: RawResponse) -> pd.DataFrame:
        if not raw.ok:
            raise ParseError(f"fiscaldata fetch failed: {raw.error or raw.http_status}")
        try:
            doc = json.loads(raw.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ParseError(f"fiscaldata body is not valid JSON: {type(exc).__name__}") from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
            raise ParseError("fiscaldata payload has no 'data' list")
        meta = doc.get("meta")
        pages = meta.get("total-pages") if isinstance(meta, dict) else None
        if isinstance(pages, int) and pages > 1:
            raise ParseError(f"fiscaldata answered {pages} pages; only one page is read")
        lo, hi = _window(dict(raw.request.params))
        slots: dict[tuple[date, str, str], list[tuple[date | None, dict[str, Any]]]] = {}
        for record in doc["data"]:
            if not isinstance(record, dict):
                raise ParseError(f"fiscaldata record is not an object: {record!r}")
            row = self._row(record)
            day = row["event_date"]
            if (lo is not None and day < lo) or (hi is not None and day > hi):
                continue
            details = json.loads(row["details_json"])
            slot = (day, details["security_term"], details["security_type"])
            slots.setdefault(slot, []).append((_date(record, "record_date"), row))
        rows: list[dict[str, Any]] = []
        for (_day, term, sec_type), found in slots.items():
            newest = max((rec or date.min) for rec, _ in found)
            snapshot = [r for rec, r in found if (rec or date.min) == newest]
            rows.extend(_identify(snapshot, f"{term}-{sec_type}".replace(" ", "")))
        rows.sort(key=lambda r: (r["event_date"], r["event_id"]))
        return table_frame("events", rows)

    def _row(self, record: dict[str, Any]) -> dict[str, Any]:
        day = _date(record, "auction_date", required=True)
        assert day is not None
        sec_type = _text(record, "security_type")
        term = _text(record, "security_term")
        if sec_type is None or term is None:
            raise ParseError("fiscaldata: record needs security_type and security_term")
        cusip = _text(record, "cusip")
        details = {
            "security_type": sec_type,
            "security_term": term,
            "reopening": _text(record, "reopening"),
            "cusip": cusip,
            "offering_amt_usd": _amount(record, "offering_amt"),
            "announcement_date": _iso(_date(record, "announcemt_date")),
            "issue_date": _iso(_date(record, "issue_date")),
            "record_date": _iso(_date(record, "record_date")),
        }
        return {
            "event_id": "",  # set by _identify once the whole slot is known
            "source": SOURCE,
            "dataset": DATASET,
            "kind": EventKind.AUCTION.value,
            "title": f"{term} {sec_type} auction",
            "event_date": day,
            "scheduled_at": None,
            "period": None,
            "url": PAGE_URL,
            "details_json": json.dumps(details, sort_keys=True, separators=(",", ":")),
        }


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def _identify(snapshot: list[dict[str, Any]], term_type: str) -> list[dict[str, Any]]:
    """Event ids for the records of one slot's newest snapshot (see the module docstring)."""
    by_cusip: dict[str, dict[str, Any]] = {}
    no_cusip: list[dict[str, Any]] = []
    for row in snapshot:
        cusip = json.loads(row["details_json"])["cusip"]
        if cusip:
            by_cusip[cusip] = row  # one CUSIP on one date is one auction: the last record wins
        else:
            no_cusip.append(row)
    out: list[dict[str, Any]] = []
    for cusip, row in by_cusip.items():
        out.append({**row, "event_id": f"auction:{row['event_date'].isoformat()}:{cusip}"})
    no_cusip.sort(key=lambda r: r["details_json"])
    for n, row in enumerate(no_cusip, start=1):
        ident = term_type if len(no_cusip) == 1 else f"{term_type}-{n}"
        out.append({**row, "event_id": f"auction:{row['event_date'].isoformat()}:{ident}"})
    return out


register(SOURCE, FiscalDataSource)
