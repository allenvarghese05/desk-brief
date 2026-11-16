"""Yahoo Finance source via ``yfinance`` (spec §5.2; card T16). Tier 2 second opinion.

Datasets
--------
* ``quote``          last price + quote instant per symbol -> ``field=last``, ``interval=tick``.
* ``history``        daily OHLC bars -> ``field in (open, high, low, close)``, ``interval=1d``.
* ``daily_fallback`` daily close for symbols that Stooq normally serves (T3 fallback) ->
  ``field=close``, ``interval=1d``, ``is_fallback=true``. Only planned when config has
  ``dataset: daily_fallback`` yahoo feeds (see ``docs/sources/yahoo.md``).

yfinance does its own HTTP, so :meth:`YahooSource.fetch` ignores the shared ``HttpClient``.
``RawResponse.body`` is therefore **not** a byte-exact provider response: it is a JSON
serialization of what the library returned plus ``yfinance.__version__`` (documented limit).
Rate limiting reuses T03's :class:`~deskbrief.sources.http.TokenBucket`, one token per
symbol call. ``fetch`` never raises: an exception becomes ``RawResponse.error`` (redacted by the
model); a failure of one symbol among several is kept in the body and skipped by ``parse``
(completeness checks see the missing row).

Time semantics (P-006): ``tick`` ``as_of`` = Yahoo's ``regularMarketTime`` (the quote instant);
``1d`` ``as_of`` = 00:00 America/New_York on the bar's exchange-local date.
Volume is not emitted: it is shares for ETFs but contracts or zero for futures and indices.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import yfinance as yf

from deskbrief.config import Config, Feed
from deskbrief.ids import new_id
from deskbrief.logging import get_logger
from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse, RunContext
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.sources.http import TokenBucket
from deskbrief.timeutil import ET, Clock, ensure_utc, utc_now

SOURCE = "yahoo"
HISTORY_LOOKBACK_DAYS = 10  # enough to span weekends/holidays around the target date
BAR_FIELDS = (("open", "open"), ("high", "high"), ("low", "low"), ("close", "close"))

#: Datasets planned per job. ``daily_fallback`` is skipped when config has no such feeds.
_JOB_DATASETS: dict[JobKind, tuple[str, ...]] = {
    JobKind.MORNING: ("quote", "history", "daily_fallback"),
    JobKind.INTRADAY: ("quote",),
    JobKind.EOD: ("quote", "history", "daily_fallback"),
    JobKind.BACKFILL: ("history", "daily_fallback"),
    JobKind.REBUILD: ("history", "daily_fallback"),
}
_BAR_DATASETS = ("history", "daily_fallback")


def _finite_float(value: object) -> float | None:
    """``value`` as a finite float, or None for missing/NaN/inf (never raises)."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _to_utc_iso(value: object) -> str | None:
    """Quote instant from yfinance (aware Timestamp/datetime or epoch seconds) as UTC ISO."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), UTC).isoformat()
    if isinstance(value, datetime) and value.tzinfo is not None:
        return ensure_utc(value).isoformat()
    return None


class YahooSource:
    name = SOURCE
    parser_version = "1"

    def __init__(
        self,
        config: Config,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Clock = utc_now,
    ) -> None:
        self._config = config
        self._now = now
        settings = config.sources[SOURCE]
        self._bucket = TokenBucket(settings.rate_per_min, 1.0, monotonic, sleep)

    # -- plan ------------------------------------------------------------------------------

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        reqs: list[FetchRequest] = []
        for dataset in _JOB_DATASETS.get(job, ()):
            symbols = tuple(self._config.feed_index(SOURCE, dataset))
            if not symbols:
                continue
            params: dict[str, str] = {}
            if dataset in _BAR_DATASETS:
                start = ctx.target_date - timedelta(days=HISTORY_LOOKBACK_DAYS)
                params = {
                    "start": start.isoformat(),
                    "end": (ctx.target_date + timedelta(days=1)).isoformat(),  # exclusive
                }
            reqs.append(
                FetchRequest(
                    source=SOURCE,
                    dataset=dataset,
                    params=params,
                    symbols=symbols,
                    target_date=ctx.target_date,
                )
            )
        return reqs

    # -- fetch -----------------------------------------------------------------------------

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        """Call yfinance once per symbol and serialize the result. Never raises."""
        log = get_logger(source=SOURCE, dataset=req.dataset)
        requested_at = self._now()
        url = f"yfinance://{req.dataset}"
        status: FetchStatus | None = None
        try:
            symbols = self._collect(req)
            body = json.dumps(
                {
                    "dataset": req.dataset,
                    "yfinance_version": str(yf.__version__),
                    "fetched_at": requested_at.isoformat(),
                    "symbols": symbols,
                },
                sort_keys=True,
            ).encode()
            http_status: int | None = 200
            error = None
            errors = [v["error"] for v in symbols.values() if "error" in v]
            if symbols and len(errors) == len(symbols):
                http_status, error = None, errors[0]  # every symbol failed
            elif not any(("price" in v) or v.get("bars") for v in symbols.values()):
                status = FetchStatus.EMPTY
            if errors:
                log.warning("yahoo.symbol_errors", n_errors=len(errors), n_symbols=len(symbols))
        except Exception as exc:  # fetch must never raise; the model redacts `error`
            body, http_status, error = b"", None, f"{type(exc).__name__}: {exc}"
        return RawResponse(
            request=req,
            fetch_id=new_id(),
            requested_at=requested_at,
            responded_at=self._now(),
            http_status=http_status,
            content_type="application/json",
            body=body,
            error=error,
            url_redacted=url,
            status=status,
        )

    def _collect(self, req: FetchRequest) -> dict[str, dict[str, Any]]:
        if req.dataset != "quote" and req.dataset not in _BAR_DATASETS:
            raise ValueError(f"unsupported yahoo dataset {req.dataset!r}")
        out: dict[str, dict[str, Any]] = {}
        for symbol in req.symbols:
            self._bucket.acquire()
            try:
                out[symbol] = (
                    self._quote(symbol)
                    if req.dataset == "quote"
                    else self._bars(symbol, req.params)
                )
            except Exception as exc:  # isolate one symbol from the rest
                out[symbol] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    @staticmethod
    def _quote(symbol: str) -> dict[str, Any]:
        ticker = yf.Ticker(symbol)
        ticker.history(period="1d", interval="1d", auto_adjust=False)  # fills history_metadata
        meta = ticker.history_metadata or {}
        price = _finite_float(meta.get("regularMarketPrice"))
        when = _to_utc_iso(meta.get("regularMarketTime"))
        if price is None or when is None:
            return {"no_data": True}
        return {"price": price, "time": when, "currency": meta.get("currency")}

    @staticmethod
    def _bars(symbol: str, params: Any) -> dict[str, Any]:
        frame = yf.Ticker(symbol).history(
            start=params.get("start"), end=params.get("end"), interval="1d", auto_adjust=False
        )
        bars: list[dict[str, Any]] = []
        for idx, rec in frame.iterrows():
            ts = pd.Timestamp(idx)
            bar: dict[str, Any] = {"date": ts.date().isoformat()}  # exchange-local date
            for key, col in (
                ("open", "Open"),
                ("high", "High"),
                ("low", "Low"),
                ("close", "Close"),
            ):
                bar[key] = _finite_float(rec.get(col))
            bars.append(bar)
        return {"bars": bars}

    # -- parse -----------------------------------------------------------------------------

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        dataset = raw.request.dataset
        if dataset not in ("quote", *_BAR_DATASETS):
            raise ParseError(f"unsupported yahoo dataset {dataset!r}")
        if not raw.body:
            return empty_frame("parsed_observations")
        symbols = _load_symbols(raw.body)
        index = self._config.feed_index(SOURCE, dataset)
        rows: list[dict[str, Any]] = []
        for symbol, entry in symbols.items():
            if not isinstance(entry, dict):
                raise ParseError(f"{symbol}: expected an object, got {type(entry).__name__}")
            if "error" in entry or entry.get("no_data"):
                continue
            if dataset == "quote":
                parsed: list[tuple[str, float, datetime]] = [_quote_point(symbol, entry)]
            else:
                parsed = _bar_points(symbol, entry, only_close=dataset == "daily_fallback")
            hit = index.get(symbol)
            if hit is None:
                continue  # provider rows without a configured instrument stay in raw only
            inst, feed = hit
            rows.extend(
                _obs(dataset, symbol, inst.id, inst.unit, feed, field, value, as_of)
                for field, value, as_of in parsed
            )
        if not rows:
            return empty_frame("parsed_observations")
        return to_frame("parsed_observations", rows)


def _load_symbols(body: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise ParseError(f"yahoo body is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("symbols"), dict):
        raise ParseError("yahoo body must be an object with a 'symbols' object")
    return doc["symbols"]  # type: ignore[no-any-return]


def _quote_point(symbol: str, entry: dict[str, Any]) -> tuple[str, float, datetime]:
    price = _finite_float(entry.get("price"))
    if price is None:
        raise ParseError(f"{symbol}: bad quote price {entry.get('price')!r}")
    try:
        when = datetime.fromisoformat(str(entry["time"]))
    except (KeyError, ValueError) as exc:
        raise ParseError(f"{symbol}: bad quote time {entry.get('time')!r}") from exc
    if when.tzinfo is None:
        raise ParseError(f"{symbol}: quote time must be timezone-aware: {entry['time']!r}")
    return "last", price, ensure_utc(when)


def _bar_points(
    symbol: str, entry: dict[str, Any], *, only_close: bool
) -> list[tuple[str, float, datetime]]:
    bars = entry.get("bars")
    if not isinstance(bars, list):
        raise ParseError(f"{symbol}: 'bars' must be a list")
    points: list[tuple[str, float, datetime]] = []
    for bar in bars:
        if not isinstance(bar, dict):
            raise ParseError(f"{symbol}: bar must be an object")
        try:
            day = date.fromisoformat(str(bar["date"]))
        except (KeyError, ValueError) as exc:
            raise ParseError(f"{symbol}: bad bar date {bar.get('date')!r}") from exc
        as_of = datetime(day.year, day.month, day.day, tzinfo=ET).astimezone(UTC)
        for field, key in BAR_FIELDS:
            if only_close and field != "close":
                continue
            if bar.get(key) is None:
                continue  # partial bar (e.g. open session): skip the field, not the bar
            value = _finite_float(bar[key])
            if value is None:
                raise ParseError(f"{symbol}: bad {key} {bar[key]!r} on {day}")
            points.append((field, value, as_of))
    return points


def _obs(
    dataset: str,
    symbol: str,
    instrument_id: str,
    unit: str,
    feed: Feed,
    field: str,
    value: float,
    as_of: datetime,
) -> dict[str, Any]:
    return {
        "instrument_id": instrument_id,
        "source": SOURCE,
        "dataset": dataset,
        "symbol": symbol,
        "tier": feed.tier,
        "field": field,
        "interval": "tick" if dataset == "quote" else "1d",
        "as_of": as_of,
        "value": value * feed.scale,
        "unit": unit,
        "is_derived": False,
        "is_fallback": dataset == "daily_fallback",
    }
