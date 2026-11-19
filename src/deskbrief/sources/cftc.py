"""CFTC Commitments of Traders (card T49, ruling P-021c). Tier 3, dataset ``cot``, interval ``1w``.

Report variant: **Legacy, futures only** (Socrata dataset ``6dca-aqww`` on
``publicreporting.cftc.gov``; keyless). The legacy report is the one variant that covers all three
markets with the same trader split: the disaggregated report covers physical commodities only
(gold, not the S&P e-mini or the 10-year note) and the TFF report financial futures only. Its
non-commercial category is the conventional "speculative" position, which is what P-021 and the
T45 label ("net speculative position") name. Details and the live probe: ``docs/sources/cftc.md``.

Symbols are ``<CFTC contract market code>:<field>``, for example ``088691:net_position``. Each
market has two instruments: ``net_position`` = non-commercial long minus non-commercial short,
``open_interest`` = total open interest (``open_interest_all``). :data:`MARKETS` lists the codes.

* ``plan`` one request for every configured market: the reports dated in the last
  ``LOOKBACK_DAYS`` (a few weeks, so a late or revised report is picked up). Not for ``intraday``
  or ``rebuild``.
* ``plan_range`` (P-020) the same single request over ``[start, end]``; two years of three markets
  is about 312 rows, one call.
* ``parse`` is a pure function of the JSON body: a list of row objects. Anything else (an error
  object, HTML, a bad number or date, a duplicate report for one market and date, a body that
  hits the row limit) is a :class:`ParseError`; an empty list is the legitimate empty case.

``as_of`` = 00:00 ET of the report's position date (normally the Tuesday), not the Friday release
(the ``1w`` rule in ``store/backend.py``, P-021).
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from deskbrief.config import Config, Feed, Instrument
from deskbrief.logging import get_logger
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.sources.registry import register, registered
from deskbrief.timeutil import et_to_utc

NAME = "cftc"
DATASET = "cot"
#: Legacy Commitments of Traders, futures only (Socrata SODA endpoint).
URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
LOOKBACK_DAYS = 35
#: Socrata ``$limit``; a body with this many rows may be cut, so it is refused.
ROW_LIMIT = 5000
FIELDS = ("net_position", "open_interest")

#: CFTC contract market codes (probe 2026-11-19, docs/sources/cftc.md).
MARKETS: dict[str, str] = {
    "088691": "GOLD - COMMODITY EXCHANGE INC.",
    "13874A": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
    "043602": "UST 10Y NOTE - CHICAGO BOARD OF TRADE",
}

_COLUMNS = (
    "cftc_contract_market_code",
    "report_date_as_yyyy_mm_dd",
    "open_interest_all",
    "noncomm_positions_long_all",
    "noncomm_positions_short_all",
)


def split_symbol(symbol: str) -> tuple[str, str] | None:
    """``"088691:net_position"`` -> ``("088691", "net_position")``; None if not that shape."""
    code, sep, field = symbol.partition(":")
    if not sep or code not in MARKETS or field not in FIELDS:
        return None
    return code, field


def _count(row: dict[str, Any], key: str) -> int:
    text = row.get(key)
    if not isinstance(text, str):
        raise ParseError(f"cftc: missing {key} in {row!r}")
    try:
        value = float(text)
    except ValueError:
        raise ParseError(f"cftc: bad {key} {text!r}") from None
    if not math.isfinite(value) or value < 0 or value != int(value):
        raise ParseError(f"cftc: {key} is not a contract count: {text!r}")
    return int(value)


def _report_date(row: dict[str, Any]) -> date:
    text = row.get("report_date_as_yyyy_mm_dd")
    if not isinstance(text, str):
        raise ParseError(f"cftc: missing report date in {row!r}")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        raise ParseError(f"cftc: bad report date {text!r}") from None


def _window(req: FetchRequest) -> tuple[date, date]:
    try:
        return date.fromisoformat(req.params["start"]), date.fromisoformat(req.params["end"])
    except (KeyError, ValueError):
        raise ParseError(f"cftc: request needs ISO start and end, got {req.params!r}") from None


class CftcSource:
    name = NAME
    parser_version = "1"

    def __init__(self, config: Config) -> None:
        self._config = config

    def _index(self) -> dict[str, tuple[Instrument, Feed]]:
        return self._config.feed_index(NAME, DATASET)

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        """The reports dated in the last ``LOOKBACK_DAYS``. The history job uses ``plan_range``."""
        if job in (JobKind.INTRADAY, JobKind.REBUILD):
            return []
        end = ctx.target_date
        return self._requests(end - timedelta(days=LOOKBACK_DAYS), end)

    def plan_range(self, start: date, end: date, ctx: RunContext) -> list[FetchRequest]:
        """One request for every configured market over the ET dates ``[start, end]`` (P-020)."""
        if end < start:
            raise ValueError("end must not be before start")
        return self._requests(start, end)

    def _requests(self, start: date, end: date) -> list[FetchRequest]:
        log = get_logger(source=NAME)
        codes: list[str] = []
        for symbol in self._index():
            parts = split_symbol(symbol)
            if parts is None:
                log.error("cftc.plan.skip", symbol=symbol, reason="not <market code>:<field>")
                continue
            if parts[0] not in codes:
                codes.append(parts[0])
        if not codes:
            return []
        quoted = ",".join(f"'{c}'" for c in codes)
        where = (
            f"cftc_contract_market_code in({quoted}) AND report_date_as_yyyy_mm_dd between "
            f"'{start.isoformat()}T00:00:00' and '{end.isoformat()}T00:00:00'"
        )
        params = {
            "$select": ",".join(_COLUMNS),
            "$where": where,
            "$order": "report_date_as_yyyy_mm_dd,cftc_contract_market_code",
            "$limit": str(ROW_LIMIT),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        return [
            FetchRequest(
                source=NAME, dataset=DATASET, params=params, symbols=tuple(codes), target_date=end
            )
        ]

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        query = {k: v for k, v in req.params.items() if k.startswith("$")}
        return http.get(req, URL, params=query, headers={"Accept": "application/json"})

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        if raw.error is not None or raw.http_status is None or not 200 <= raw.http_status < 300:
            raise ParseError(f"cftc: failed fetch ({raw.error or raw.http_status})")
        lo, hi = _window(raw.request)
        try:
            doc = json.loads(raw.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ParseError(f"cftc: body is not JSON ({type(exc).__name__})") from None
        if not isinstance(doc, list):
            detail = doc.get("message") if isinstance(doc, dict) else None
            raise ParseError(f"cftc: expected a list of rows (message: {detail!r})")
        if len(doc) >= ROW_LIMIT:
            raise ParseError(f"cftc: {len(doc)} rows hit the row limit; the answer may be cut")
        by_code: dict[str, list[tuple[str, Instrument, Feed]]] = {}
        for symbol, (inst, feed) in self._index().items():
            parts = split_symbol(symbol)
            if parts is not None:
                by_code.setdefault(parts[0], []).append((parts[1], inst, feed))
        seen: set[tuple[str, date]] = set()
        rows: list[dict[str, object]] = []
        for item in doc:
            if not isinstance(item, dict):
                raise ParseError(f"cftc: row is not an object: {item!r}")
            code = item.get("cftc_contract_market_code")
            if not isinstance(code, str):
                raise ParseError(f"cftc: row has no contract market code: {item!r}")
            day = _report_date(item)
            if (code, day) in seen:
                raise ParseError(f"cftc: two reports for {code} on {day}")
            seen.add((code, day))
            targets = by_code.get(code)
            if not targets or not lo <= day <= hi:
                continue
            values = {
                "open_interest": _count(item, "open_interest_all"),
                "net_position": _count(item, "noncomm_positions_long_all")
                - _count(item, "noncomm_positions_short_all"),
            }
            for field, inst, feed in targets:
                rows.append(
                    {
                        "instrument_id": inst.id,
                        "source": NAME,
                        "dataset": DATASET,
                        "symbol": f"{code}:{field}",
                        "tier": feed.tier,
                        "field": field,
                        "interval": "1w",
                        "as_of": et_to_utc(day, "00:00"),
                        "value": float(values[field]) * feed.scale,
                        "unit": inst.unit,
                        "is_derived": field == "net_position",
                        "is_fallback": False,
                    }
                )
        if not rows:
            return empty_frame("parsed_observations")
        return to_frame("parsed_observations", rows)


if NAME not in registered():
    register(NAME, CftcSource)
