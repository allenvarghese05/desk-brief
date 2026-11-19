"""Cboe daily index history (spec §5.2, cards T13 and T47). Tier 3, dataset ``vix_history``.

Symbols (P-021b): VIX, VIX9D, VIX3M, VIX6M, VXN, GVZ and SKEW. Each has its own static CSV,
``{symbol}_History.csv``, with the whole history. Two layouts exist (live probe 2026-11-19,
``docs/sources/cboe.md``): ``DATE,OPEN,HIGH,LOW,CLOSE`` (VIX family, VXN) and ``DATE,<SYMBOL>``
(GVZ, SKEW). :data:`SYMBOL_SPECS` maps each symbol to its layout and the value column.

Plan: one request per configured symbol. ``plan`` adds ``lookback_days``; ``plan_range`` (P-020)
adds ``start``/``end``. ``parse`` emits only rows whose date is in
``[target_date - lookback_days, target_date]`` or ``[start, end]`` (all rows if the request carries
neither, e.g. a backfill). Only the value column is emitted as ``close`` (unit and tier from
config), interval ``1d``, ``as_of`` = 00:00 ET of the row date (P-006). Dates are ``MM/DD/YYYY``
(``YYYY-MM-DD`` also accepted). Lines starting with ``#`` and up to a few title lines before the
header are tolerated; anything else (HTML, error page, a header that is not the symbol's layout,
bad date/number) is a :class:`ParseError`. An empty/header-only body is the legitimate empty
case, as is a window that holds no rows.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from deskbrief.config import Config
from deskbrief.logging import get_logger
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.sources.registry import register, registered
from deskbrief.timeutil import et_to_utc

NAME = "cboe"
DATASET = "vix_history"
SYMBOL = "VIX"
# cdn.cboe.com answers HTTP 307 -> cdn-api.cboe.com (probe 2026-11-18, docs/sources/cboe.md)
BASE_URL = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices"
URL = f"{BASE_URL}/VIX_History.csv"
LOOKBACK_DAYS = 10
OHLC = ("DATE", "OPEN", "HIGH", "LOW", "CLOSE")
HEADER = list(OHLC)
_PREAMBLE_LINES = 5


@dataclass(frozen=True)
class SymbolSpec:
    """One Cboe file: its header (upper case) and the column emitted as ``close``."""

    header: tuple[str, ...]
    value_column: str

    @property
    def value_index(self) -> int:
        return self.header.index(self.value_column)


def _single(symbol: str) -> SymbolSpec:
    return SymbolSpec(header=("DATE", symbol), value_column=symbol)


_OHLC_SPEC = SymbolSpec(header=OHLC, value_column="CLOSE")

#: Layouts confirmed by the live probe on 2026-11-19 (docs/sources/cboe.md).
SYMBOL_SPECS: dict[str, SymbolSpec] = {
    "VIX": _OHLC_SPEC,
    "VIX9D": _OHLC_SPEC,
    "VIX3M": _OHLC_SPEC,
    "VIX6M": _OHLC_SPEC,
    "VXN": _OHLC_SPEC,
    "GVZ": _single("GVZ"),
    "SKEW": _single("SKEW"),
}


def url_for(symbol: str) -> str:
    return f"{BASE_URL}/{symbol}_History.csv"


def _parse_date(text: str) -> date:
    text = text.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007 - date only
        except ValueError:
            continue
    raise ParseError(f"cboe: bad date {text!r}")


def _window(req: FetchRequest) -> tuple[date | None, date | None]:
    """The inclusive date window a request asks for, or ``(None, None)`` for every row."""
    start, end = req.params.get("start"), req.params.get("end")
    if start is not None or end is not None:
        if start is None or end is None:
            raise ParseError("cboe: range request needs both start and end")
        try:
            return date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            raise ParseError(f"cboe: bad range {start!r}..{end!r}") from None
    lookback = req.params.get("lookback_days")
    if lookback is None or req.target_date is None:
        return None, None
    try:
        return req.target_date - timedelta(days=int(lookback)), req.target_date
    except ValueError:
        raise ParseError(f"cboe: bad lookback_days {lookback!r}") from None
    except OverflowError:
        raise ParseError(f"cboe: lookback_days out of range {lookback!r}") from None


class CboeSource:
    name = NAME
    parser_version = "2"

    def __init__(self, config: Config) -> None:
        self._config = config

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        """Daily requests (last ``LOOKBACK_DAYS``). The history job uses :meth:`plan_range`."""
        if job is JobKind.INTRADAY:
            return []
        return self._requests({"lookback_days": str(LOOKBACK_DAYS)}, ctx.target_date)

    def plan_range(self, start: date, end: date, ctx: RunContext) -> list[FetchRequest]:
        """One request per configured symbol for the ET dates ``[start, end]`` (P-020).

        Each file holds the whole history, so any range is one download per symbol.
        """
        if end < start:
            raise ValueError("end must not be before start")
        return self._requests({"start": start.isoformat(), "end": end.isoformat()}, end)

    def _requests(self, params: dict[str, str], target: date) -> list[FetchRequest]:
        requests: list[FetchRequest] = []
        for symbol in self._config.feed_index(NAME, DATASET):
            if symbol not in SYMBOL_SPECS:
                get_logger(source=NAME).error(
                    "cboe.plan.skip", symbol=symbol, reason="no layout in SYMBOL_SPECS"
                )
                continue
            requests.append(
                FetchRequest(
                    source=NAME,
                    dataset=DATASET,
                    params=dict(params),
                    symbols=(symbol,),
                    target_date=target,
                )
            )
        return requests

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        return http.get(req, url_for(req.symbols[0]))

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        if raw.error is not None or raw.http_status is None or not 200 <= raw.http_status < 300:
            raise ParseError(f"cboe: failed fetch ({raw.error or raw.http_status})")
        req = raw.request
        symbol = req.symbols[0] if len(req.symbols) == 1 else None
        spec = SYMBOL_SPECS.get(symbol) if symbol is not None else None
        if symbol is None or spec is None:
            raise ParseError(f"cboe: no layout for request symbols {req.symbols!r}")
        try:
            text = raw.body.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ParseError("cboe: body is not UTF-8 text") from None
        lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        if not lines:
            return empty_frame("parsed_observations")
        header = list(spec.header)
        start = next(
            (
                i
                for i, line in enumerate(lines[:_PREAMBLE_LINES])
                if [h.strip().upper() for h in line.split(",")] == header
            ),
            None,
        )
        if start is None:
            raise ParseError(f"cboe: not {symbol}_History.csv (first line {lines[0][:60]!r})")
        entry = self._config.feed_index(NAME, DATASET).get(symbol)
        if entry is None:
            return empty_frame("parsed_observations")
        inst, feed = entry
        lo, hi = _window(req)
        col, idx = spec.value_column, spec.value_index
        rows: list[dict[str, object]] = []
        last_kept: list[str] | None = None
        try:
            for rec in csv.reader(io.StringIO("\n".join(lines[start + 1 :]))):
                day = _parse_date(rec[0]) if rec else None
                if day is not None and lo is not None and hi is not None and not lo <= day <= hi:
                    last_kept = None
                    continue  # outside the window: not validated, so old bad rows cannot hurt
                if len(rec) != len(header):
                    raise ParseError(f"cboe: row has {len(rec)} columns: {rec!r}")
                assert day is not None
                try:
                    value = float(rec[idx])
                except ValueError:
                    raise ParseError(f"cboe: bad {col} {rec[idx]!r}") from None
                if not math.isfinite(value):
                    raise ParseError(f"cboe: non-finite {col} {rec[idx]!r}")
                last_kept = rec
                rows.append(
                    {
                        "instrument_id": inst.id,
                        "source": NAME,
                        "dataset": DATASET,
                        "symbol": symbol,
                        "tier": feed.tier,
                        "field": "close",
                        "interval": "1d",
                        "as_of": et_to_utc(day, "00:00"),
                        "value": value * feed.scale,
                        "unit": inst.unit,
                        "is_derived": False,
                        "is_fallback": False,
                    }
                )
        except csv.Error as exc:
            raise ParseError(f"cboe: unreadable CSV ({exc})") from None
        if last_kept is not None and not text.endswith(("\n", "\r")):
            # The file is a static CSV that ends with a newline; a missing one means the download
            # was cut, possibly mid-number ("16." would parse as 16.0).
            raise ParseError(f"cboe: download looks truncated (last row {last_kept!r})")
        if not rows:
            return empty_frame("parsed_observations")
        return to_frame("parsed_observations", rows)


if NAME not in registered():
    register(NAME, CboeSource)
