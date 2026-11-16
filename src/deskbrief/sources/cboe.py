"""Cboe VIX history source (spec §5.2, card T13). Tier 3, dataset ``vix_history``, symbol ``VIX``.

Plan: one request for the whole ``VIX_History.csv`` (a single static CSV since 1990).
``plan`` adds ``lookback_days`` to the request params; ``parse`` emits only rows whose date is
in ``[target_date - lookback_days, target_date]`` (all rows if the request carries no
``lookback_days`` or no ``target_date``, e.g. a backfill). Only ``close`` is emitted
(unit and tier from config), interval ``1d``, ``as_of`` = 00:00 ET of the row date (P-006).
Dates are ``MM/DD/YYYY`` (``YYYY-MM-DD`` also accepted). Lines starting with ``#`` and up to a
few title lines before the header are tolerated; anything else (HTML, error page, missing
header, bad date/number) is a :class:`ParseError`. An empty/header-only body is the legitimate
empty case, as is a window that holds no rows.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime, timedelta

import pandas as pd

from deskbrief.config import Config
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.sources.registry import register, registered
from deskbrief.timeutil import et_to_utc

NAME = "cboe"
DATASET = "vix_history"
SYMBOL = "VIX"
URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
LOOKBACK_DAYS = 10
HEADER = ["DATE", "OPEN", "HIGH", "LOW", "CLOSE"]
_PREAMBLE_LINES = 5


def _parse_date(text: str) -> date:
    text = text.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007 - date only
        except ValueError:
            continue
    raise ParseError(f"cboe: bad date {text!r}")


class CboeSource:
    name = NAME
    parser_version = "1"

    def __init__(self, config: Config) -> None:
        self._config = config

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        if job is JobKind.INTRADAY or SYMBOL not in self._config.feed_index(NAME, DATASET):
            return []
        return [
            FetchRequest(
                source=NAME,
                dataset=DATASET,
                params={"lookback_days": str(LOOKBACK_DAYS)},
                symbols=(SYMBOL,),
                target_date=ctx.target_date,
            )
        ]

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        return http.get(req, URL)

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        if raw.error is not None or raw.http_status is None or not 200 <= raw.http_status < 300:
            raise ParseError(f"cboe: failed fetch ({raw.error or raw.http_status})")
        try:
            text = raw.body.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ParseError("cboe: body is not UTF-8 text") from None
        lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        if not lines:
            return empty_frame("parsed_observations")
        start = next(
            (
                i
                for i, line in enumerate(lines[:_PREAMBLE_LINES])
                if [h.strip().upper() for h in line.split(",")] == HEADER
            ),
            None,
        )
        if start is None:
            raise ParseError(f"cboe: not VIX_History.csv (first line {lines[0][:60]!r})")
        entry = self._config.feed_index(NAME, DATASET).get(SYMBOL)
        if entry is None:
            return empty_frame("parsed_observations")
        inst, feed = entry
        req = raw.request
        lookback = req.params.get("lookback_days")
        lo = hi = None
        if lookback is not None and req.target_date is not None:
            try:
                lo = req.target_date - timedelta(days=int(lookback))
            except ValueError:
                raise ParseError(f"cboe: bad lookback_days {lookback!r}") from None
            hi = req.target_date
        rows: list[dict[str, object]] = []
        for rec in csv.reader(io.StringIO("\n".join(lines[start + 1 :]))):
            if len(rec) != len(HEADER):
                raise ParseError(f"cboe: row has {len(rec)} columns: {rec!r}")
            day = _parse_date(rec[0])
            try:
                close = float(rec[4])
            except ValueError:
                raise ParseError(f"cboe: bad CLOSE {rec[4]!r}") from None
            if not math.isfinite(close):
                raise ParseError(f"cboe: non-finite CLOSE {rec[4]!r}")
            if lo is not None and hi is not None and not lo <= day <= hi:
                continue
            rows.append(
                {
                    "instrument_id": inst.id,
                    "source": NAME,
                    "dataset": DATASET,
                    "symbol": SYMBOL,
                    "tier": feed.tier,
                    "field": "close",
                    "interval": "1d",
                    "as_of": et_to_utc(day, "00:00"),
                    "value": close * feed.scale,
                    "unit": inst.unit,
                    "is_derived": False,
                    "is_fallback": False,
                }
            )
        if not rows:
            return empty_frame("parsed_observations")
        return to_frame("parsed_observations", rows)


if NAME not in registered():
    register(NAME, CboeSource)
