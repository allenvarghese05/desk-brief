"""Stooq daily CSV source (spec §5.2, card T13). Tier 3, dataset ``daily``.

One request per configured symbol (``spy.us``): ``https://stooq.com/q/d/l/?s=<sym>&i=d``
with ``d1``/``d2`` (YYYYMMDD) bounding a window of ``LOOKBACK_DAYS`` calendar days ending on
the run's target date (so late prints are visible). Not planned for ``intraday``.

``parse`` emits ``open/high/low/close`` (``Feed.scale`` applied; unit from the instrument) and
``volume`` (unit ``shares``), interval ``1d``, ``as_of`` = 00:00 ET of the row date (P-006).
Lines starting with ``#`` are ignored (fixture headers).

Failure modes -> :class:`ParseError`: a failed fetch, a non-CSV body served with status 200
(HTML, "No data", "Exceeded the daily hits limit", an apikey/captcha page), a wrong header, or an
unparsable date/number. Stooq answers "No data" for a window with no rows, which this parser
cannot tell from an unknown symbol, so it is an error, not an empty frame; a header-only or
empty body is the legitimate empty case.

Access: Stooq has been reported to gate CSV downloads behind a captcha/apikey. The day-1 probe
could not be run for this card, so access is unverified (docs/sources/stooq.md).
"""

from __future__ import annotations

import csv
import io
import math
from datetime import date, timedelta

import pandas as pd

from deskbrief.config import Config
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.sources.registry import register, registered
from deskbrief.timeutil import et_to_utc

NAME = "stooq"
DATASET = "daily"
URL = "https://stooq.com/q/d/l/"
LOOKBACK_DAYS = 10
HEADER = ["Date", "Open", "High", "Low", "Close", "Volume"]
_PRICE_FIELDS = (("Open", "open"), ("High", "high"), ("Low", "low"), ("Close", "close"))


def _num(text: str, what: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ParseError(f"stooq: bad {what} {text!r}") from None
    if not math.isfinite(value):
        raise ParseError(f"stooq: non-finite {what} {text!r}")
    return value


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


class StooqSource:
    name = NAME
    parser_version = "1"

    def __init__(self, config: Config) -> None:
        self._config = config

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        if job is JobKind.INTRADAY:
            return []
        end = ctx.target_date
        start = end - timedelta(days=LOOKBACK_DAYS)
        return [
            FetchRequest(
                source=NAME,
                dataset=DATASET,
                params={"s": symbol, "i": "d", "d1": _ymd(start), "d2": _ymd(end)},
                symbols=(symbol,),
                target_date=end,
            )
            for symbol in self._config.feed_index(NAME, DATASET)
        ]

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        return http.get(req, URL, params=req.params)

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        if raw.error is not None or raw.http_status is None or not 200 <= raw.http_status < 300:
            raise ParseError(f"stooq: failed fetch ({raw.error or raw.http_status})")
        try:
            text = raw.body.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ParseError("stooq: body is not UTF-8 text") from None
        lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        if not lines:
            return empty_frame("parsed_observations")
        if [h.strip() for h in lines[0].split(",")] != HEADER:
            raise ParseError(f"stooq: not the daily CSV (first line {lines[0][:60]!r})")
        symbol = raw.request.symbols[0] if raw.request.symbols else raw.request.params.get("s", "")
        entry = self._config.feed_index(NAME, DATASET).get(symbol)
        if entry is None:
            return empty_frame("parsed_observations")
        inst, feed = entry
        rows: list[dict[str, object]] = []
        for rec in csv.reader(io.StringIO("\n".join(lines[1:]))):
            if len(rec) != len(HEADER):
                raise ParseError(f"stooq: row has {len(rec)} columns: {rec!r}")
            try:
                day = date.fromisoformat(rec[0].strip())
            except ValueError:
                raise ParseError(f"stooq: bad date {rec[0]!r}") from None
            base = {
                "instrument_id": inst.id,
                "source": NAME,
                "dataset": DATASET,
                "symbol": symbol,
                "tier": feed.tier,
                "interval": "1d",
                "as_of": et_to_utc(day, "00:00"),
                "is_derived": False,
                "is_fallback": False,
            }
            for col, fld in _PRICE_FIELDS:
                value = _num(rec[HEADER.index(col)], col) * feed.scale
                rows.append({**base, "field": fld, "value": value, "unit": inst.unit})
            rows.append(
                {**base, "field": "volume", "value": _num(rec[5], "Volume"), "unit": "shares"}
            )
        if not rows:
            return empty_frame("parsed_observations")
        return to_frame("parsed_observations", rows)


if NAME not in registered():
    register(NAME, StooqSource)
