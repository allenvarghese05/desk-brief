"""Alpaca Market Data source (card T14; spec §5.2, §5.4, §11).

IEX latest trade/quote, plus IEX and SIP 1-minute bars.

Datasets (``data.alpaca.markets/v2/stocks``; config ``feed_index("alpaca", <dataset>)``):

* ``iex_latest``  latest trade (``last``) and latest quote (``bid``/``ask``) for every configured
  symbol, ``interval=tick``, tier 1. Two requests per plan (trades, quotes), told apart by the
  public param ``part`` (not sent to the API). ``as_of`` is the trade/quote instant (P-006).
  A quote side of ``0`` means "no quote" and is skipped.
* ``iex_bars_1m`` IEX 1-minute bars over a window, tier 1.
* ``sip_bars_1m`` SIP 1-minute bars, tier 2. The free plan only serves SIP data older than 15
  minutes, so ``plan`` clamps the window end to ``ctx.now_utc - SIP_DELAY`` (16 minutes) and
  drops the request when nothing is left. ``fetch`` cannot see the clock, so the guard lives in
  ``plan`` (tested over a sweep of ``now`` values).

Bars emit ``open/high/low/close`` (unit from the instrument, scaled by ``Feed.scale``) and
``volume`` (unit ``shares``, never scaled), ``interval=1m``, ``as_of`` = bar start (P-006).

Plans: ``morning`` and ``intraday`` -> ``iex_latest``; ``eod`` and ``backfill`` -> IEX and SIP
bars for the ``crosschecks.iex_vs_sip.symbols`` over the XNYS session of ``ctx.target_date``
(``session_bounds_utc``: DST and early closes honoured; closed days plan nothing);
``rebuild`` plans nothing. There is no ``daily_bars`` dataset (ruling P-003).

Keys: ``ALPACA_KEY_ID`` / ``ALPACA_SECRET_KEY`` are passed ONLY as ``secret_headers``
(``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY``); never in ``FetchRequest``, ``url_redacted``, the
raw body or logs. **Missing key**: no HTTP call; an error ``RawResponse`` naming the variable(s)
(same pattern as FRED).

Pagination: bars follow ``next_page_token`` in ``fetch``, bounded by ``MAX_PAGES`` and a repeated
token check, so it can never loop forever. One page is returned byte-for-byte. Several pages are
stored as ``{"pages":[<page 1 bytes>,<page 2 bytes>,...]}`` so each page stays verbatim; any
page failure fails the whole fetch (never a partial frame). When pagination stops on the page cap,
a repeated token or a non-JSON later page, the response is still an error, but its body keeps the
pages already fetched in that same ``pages`` shape (replayable raw). ``parse`` accepts both shapes
and refuses the error response. Free-plan IEX prices and volume cover one exchange only.

Errors: a non-2xx fetch, or a provider error body (``{"code":..,"message":..}``, also inside a
200), raises :class:`ParseError`. A legitimately empty ``bars`` is ``empty_frame`` and the fetch is
marked ``FetchStatus.EMPTY``; on a business day the T20 completeness check catches it.

Payloads are SYNTHETIC in tests until the owner's live probe (``docs/sources/alpaca.md``).
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from deskbrief.calendars import session_bounds_utc
from deskbrief.config import Config, Feed, Instrument
from deskbrief.ids import new_id
from deskbrief.logging import get_logger
from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse, RunContext
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.sources import registry
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.timeutil import utc_now

BASE_URL = "https://data.alpaca.markets/v2/stocks"
TRADES_URL = f"{BASE_URL}/trades/latest"
QUOTES_URL = f"{BASE_URL}/quotes/latest"
BARS_URL = f"{BASE_URL}/bars"
KEY_ID_ENV = "ALPACA_KEY_ID"
SECRET_KEY_ENV = "ALPACA_SECRET_KEY"  # noqa: S105 - env var name, not a credential

#: SIP data is only available once it is older than 15 minutes; one minute of margin.
SIP_DELAY = timedelta(minutes=16)
#: Hard cap on pages per bars fetch (10000 bars/page; a session of 5 symbols is ~2000 bars).
MAX_PAGES = 20
PAGE_LIMIT = "10000"

BARS_DATASETS: dict[str, str] = {"iex_bars_1m": "iex", "sip_bars_1m": "sip"}
LATEST_DATASET = "iex_latest"
DATASETS = (LATEST_DATASET, *BARS_DATASETS)
PART_KEYS = {"trades": TRADES_URL, "quotes": QUOTES_URL}

_PRICE_FIELDS = (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"))
_TS = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})")


def bar_symbols(config: Config, dataset: str) -> tuple[str, ...]:
    """Symbols whose ``dataset`` bars the eod/backfill plan fetches.

    Only ``crosschecks.iex_vs_sip.symbols`` that have a feed for ``dataset``: other configured
    ``sip_bars_1m`` feeds (AAPL, MSFT, ...) are never fetched, so the completeness check must
    not expect them (T25c: they failed on every run).
    """
    index = config.feed_index("alpaca", dataset)
    return tuple(s for s in config.crosschecks.iex_vs_sip.symbols if s in index)


class AlpacaSource:
    name = "alpaca"
    parser_version = "1"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._indexes: dict[str, dict[str, tuple[Instrument, Feed]]] = {
            d: config.feed_index("alpaca", d) for d in DATASETS
        }

    # -- plan -----------------------------------------------------------------------------

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        if job in (JobKind.MORNING, JobKind.INTRADAY):
            return self._latest_requests(ctx)
        if job in (JobKind.EOD, JobKind.BACKFILL):
            return self._bars_requests(ctx)
        return []

    def _latest_requests(self, ctx: RunContext) -> list[FetchRequest]:
        symbols = tuple(self._indexes[LATEST_DATASET])
        if not symbols:
            return []
        return [
            FetchRequest(
                source=self.name,
                dataset=LATEST_DATASET,
                params={"symbols": ",".join(symbols), "feed": "iex", "part": part},
                symbols=symbols,
                target_date=ctx.target_date,
            )
            for part in PART_KEYS
        ]

    def _bars_requests(self, ctx: RunContext) -> list[FetchRequest]:
        log = get_logger(source=self.name)
        bounds = session_bounds_utc(ctx.target_date)
        if bounds is None:
            log.info("alpaca.plan.closed", target_date=ctx.target_date.isoformat())
            return []
        start, end = bounds
        requests: list[FetchRequest] = []
        for dataset, feed in BARS_DATASETS.items():
            symbols = tuple(
                s for s in bar_symbols(self.config, dataset) if s in self._indexes[dataset]
            )
            if not symbols:
                continue
            window_end = end
            if feed == "sip":  # the free plan refuses SIP data newer than 15 minutes
                window_end = min(end, ctx.now_utc - SIP_DELAY)
            if window_end <= start:
                log.warning("alpaca.plan.sip_too_recent", dataset=dataset)
                continue
            requests.append(
                FetchRequest(
                    source=self.name,
                    dataset=dataset,
                    params={
                        "symbols": ",".join(symbols),
                        "timeframe": "1Min",
                        "feed": feed,
                        "adjustment": "raw",
                        "sort": "asc",
                        "limit": PAGE_LIMIT,
                    },
                    symbols=symbols,
                    target_date=ctx.target_date,
                    window=(start, window_end),
                )
            )
        return requests

    # -- fetch ----------------------------------------------------------------------------

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        key_id = os.environ.get(KEY_ID_ENV, "").strip()
        secret = os.environ.get(SECRET_KEY_ENV, "").strip()
        missing = [n for n, v in ((KEY_ID_ENV, key_id), (SECRET_KEY_ENV, secret)) if not v]
        if missing:
            get_logger(source=self.name, dataset=req.dataset).error(
                "alpaca.fetch.no_api_key", env=",".join(missing), symbols=",".join(req.symbols)
            )
            return _error_response(req, f"{', '.join(missing)} is not set; no request made")
        headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}

        if req.dataset == LATEST_DATASET:
            url = PART_KEYS.get(req.params.get("part", ""))
            if url is None:
                return _error_response(req, "Alpaca iex_latest request has no valid 'part'")
            params = {k: v for k, v in req.params.items() if k != "part"}
            return _mark_empty(http.get(req, url, params=params, secret_headers=headers))
        if req.dataset in BARS_DATASETS:
            if req.window is None:
                return _error_response(req, "Alpaca bars request has no window")
            params = {
                **req.params,
                "start": req.window[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": req.window[1].strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            return _mark_empty(self._fetch_pages(req, http, params, headers))
        return _error_response(req, f"Alpaca dataset {req.dataset!r} is not supported")

    def _fetch_pages(
        self,
        req: FetchRequest,
        http: HttpClient,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> RawResponse:
        """Follow ``next_page_token`` (bounded). Any failing page fails the whole fetch."""
        pages: list[RawResponse] = []
        seen: set[str] = set()
        page_params = dict(params)
        for number in range(1, MAX_PAGES + 1):
            raw = http.get(req, BARS_URL, params=page_params, secret_headers=headers)
            if not raw.ok:
                return raw
            doc = _try_json(raw.body)
            if doc is None:
                if not pages:
                    return raw  # parse() reports it; the stored bytes stay exact
                return _error_response(req, f"Alpaca page {number} is not valid JSON", pages)
            pages.append(raw)
            token = doc.get("next_page_token")
            if not isinstance(token, str) or not token:
                return pages[0] if len(pages) == 1 else _merge_pages(req, pages)
            if token in seen:
                return _error_response(
                    req, f"Alpaca pagination loop: next_page_token repeated on page {number}", pages
                )
            seen.add(token)
            page_params["page_token"] = token
        return _error_response(req, f"Alpaca pagination exceeded {MAX_PAGES} pages", pages)

    # -- parse ----------------------------------------------------------------------------

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        if not raw.ok:
            raise ParseError(f"Alpaca fetch failed: {raw.error or raw.http_status}")
        dataset = raw.request.dataset
        doc = _load(raw.body)
        if dataset == LATEST_DATASET:
            rows = self._latest_rows(doc, raw.request.params.get("part"))
        elif dataset in BARS_DATASETS:
            rows = self._bar_rows(doc, dataset)
        else:
            raise ParseError(f"Alpaca dataset {dataset!r} is not supported")
        if not rows:
            return empty_frame("parsed_observations")
        return to_frame("parsed_observations", rows)

    def _row(
        self,
        dataset: str,
        symbol: str,
        field: str,
        interval: str,
        as_of: datetime,
        value: float,
        *,
        shares: bool = False,
    ) -> dict[str, Any] | None:
        entry = self._indexes[dataset].get(symbol)
        if entry is None:
            return None
        inst, feed = entry
        return {
            "instrument_id": inst.id,
            "source": self.name,
            "dataset": dataset,
            "symbol": symbol,
            "tier": feed.tier,
            "field": field,
            "interval": interval,
            "as_of": as_of,
            "value": value if shares else value * feed.scale,
            "unit": "shares" if shares else inst.unit,
            "is_derived": False,
            "is_fallback": False,
        }

    def _latest_rows(self, doc: dict[str, Any], part: str | None) -> list[dict[str, Any]]:
        if part not in PART_KEYS:
            raise ParseError("Alpaca iex_latest response needs request param part=trades|quotes")
        _raise_if_error(doc)
        items = doc.get(part)
        if not isinstance(items, dict):
            raise ParseError(f"Alpaca payload has no {part!r} object")
        rows: list[dict[str, Any] | None] = []
        for symbol, item in items.items():
            if symbol not in self._indexes[LATEST_DATASET]:
                continue
            if not isinstance(item, dict):
                raise ParseError(f"Alpaca {part} entry for {symbol} is not an object")
            when = _timestamp(item.get("t"), f"{part} {symbol} 't'")
            if part == "trades":
                price = _number(item.get("p"), f"trade {symbol} 'p'")
                rows.append(self._row(LATEST_DATASET, symbol, "last", "tick", when, price))
                continue
            for key, field in (("bp", "bid"), ("ap", "ask")):
                price = _number(item.get(key), f"quote {symbol} {key!r}")
                if price > 0:  # Alpaca sends 0 for a side with no quote
                    rows.append(self._row(LATEST_DATASET, symbol, field, "tick", when, price))
        return [r for r in rows if r is not None]

    def _bar_rows(self, doc: dict[str, Any], dataset: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in _pages(doc):
            _raise_if_error(page)
            if "bars" not in page:
                raise ParseError("Alpaca payload has no 'bars'")
            bars = page["bars"]
            if bars is None:
                continue
            if not isinstance(bars, dict):
                raise ParseError("Alpaca 'bars' is not an object")
            for symbol, series in bars.items():
                if symbol not in self._indexes[dataset]:
                    continue
                if series is None:
                    continue
                if not isinstance(series, list):
                    raise ParseError(f"Alpaca bars for {symbol} is not a list")
                for item in series:
                    rows.extend(self._one_bar(dataset, symbol, item))
        return rows

    def _one_bar(self, dataset: str, symbol: str, item: Any) -> list[dict[str, Any]]:
        if not isinstance(item, dict):
            raise ParseError(f"Alpaca bar for {symbol} is not an object: {item!r}")
        when = _timestamp(item.get("t"), f"bar {symbol} 't'")
        out = [
            self._row(
                dataset, symbol, field, "1m", when, _number(item.get(k), f"bar {symbol} {k!r}")
            )
            for k, field in _PRICE_FIELDS
        ]
        volume = _number(item.get("v"), f"bar {symbol} 'v'")
        out.append(self._row(dataset, symbol, "volume", "1m", when, volume, shares=True))
        return [r for r in out if r is not None]


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def _error_response(
    req: FetchRequest, message: str, pages: list[RawResponse] | None = None
) -> RawResponse:
    first = pages[0].requested_at if pages else utc_now()
    return RawResponse(
        request=req,
        fetch_id=new_id(),
        requested_at=first,
        responded_at=utc_now(),
        http_status=None,
        content_type="",
        # a failed pagination keeps what it already fetched, verbatim, so raw can be replayed
        body=_pages_body(pages) if pages else b"",
        error=message,
        attempts=max(1, sum(p.attempts for p in pages or [])),
        status=FetchStatus.ERROR,
    )


def _pages_body(pages: list[RawResponse]) -> bytes:
    return b'{"pages":[' + b",".join(p.body for p in pages) + b"]}"


def _merge_pages(req: FetchRequest, pages: list[RawResponse]) -> RawResponse:
    """One response holding every page's bytes verbatim: ``{"pages":[p1,p2,...]}``."""
    body = _pages_body(pages)
    return RawResponse(
        request=req,
        fetch_id=pages[0].fetch_id,
        requested_at=pages[0].requested_at,
        responded_at=pages[-1].responded_at,
        http_status=pages[-1].http_status,
        content_type=pages[0].content_type,
        body=body,
        attempts=sum(p.attempts for p in pages),
        url_redacted=pages[0].url_redacted,
    )


def _try_json(body: bytes) -> dict[str, Any] | None:
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _is_empty_payload(doc: dict[str, Any]) -> bool:
    """True for a well-formed response that carries no data (``bars: {}``, ``trades: {}``)."""
    for key in ("trades", "quotes"):
        if key in doc:
            return isinstance(doc[key], dict) and not doc[key]
    pages = doc["pages"] if isinstance(doc.get("pages"), list) else [doc]
    for page in pages:
        if not isinstance(page, dict) or "bars" not in page:
            return False
        bars = page["bars"]
        if bars is None:
            continue
        if not isinstance(bars, dict) or any(bool(v) for v in bars.values()):
            return False
    return True


def _mark_empty(raw: RawResponse) -> RawResponse:
    """Tag a successful, well-formed, data-free response ``EMPTY`` (body untouched)."""
    if not raw.ok or raw.status is not None:
        return raw
    doc = _try_json(raw.body)
    if doc is not None and _is_empty_payload(doc):
        return dataclasses.replace(raw, status=FetchStatus.EMPTY)
    return raw


def _load(body: bytes) -> dict[str, Any]:
    doc = _try_json(body)
    if doc is None:
        raise ParseError("Alpaca body is not a valid JSON object")
    return doc


def _pages(doc: dict[str, Any]) -> list[dict[str, Any]]:
    if "pages" not in doc:
        return [doc]
    pages = doc["pages"]
    if not isinstance(pages, list) or not all(isinstance(p, dict) for p in pages):
        raise ParseError("Alpaca 'pages' is not a list of objects")
    return pages


def _raise_if_error(doc: dict[str, Any]) -> None:
    """Provider error body: ``{"code": 40110000, "message": "..."}`` (also inside a 200)."""
    message = doc.get("message")
    if isinstance(message, str) and not any(k in doc for k in ("bars", "trades", "quotes")):
        raise ParseError(f"Alpaca error {doc.get('code')}: {message}")


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ParseError(f"Alpaca {what} is not a number: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ParseError(f"Alpaca {what} is not finite: {value!r}")
    return number


def _timestamp(value: Any, what: str) -> datetime:
    """RFC 3339 with a zone (``Z`` in practice). Nanoseconds are truncated to microseconds."""
    match = _TS.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ParseError(f"Alpaca {what} is not an RFC 3339 timestamp: {value!r}")
    stem, frac, zone = match.groups()
    text = f"{stem}.{frac[:6]:0<6}" if frac else stem
    try:
        return datetime.fromisoformat(f"{text}{'+00:00' if zone == 'Z' else zone}").astimezone(UTC)
    except ValueError as exc:
        raise ParseError(f"Alpaca {what} is not a valid timestamp: {value!r}") from exc


registry.register("alpaca", AlpacaSource)
