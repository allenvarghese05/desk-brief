"""Twelve Data source (card T15; spec §5.2, §5.3, §8.2): FX batch quotes and a 1-minute series.

Datasets (symbols and tiers from ``config.feed_index("twelvedata", ...)``):

* ``fx_quote``      one batch ``/quote`` call for the configured pairs. Fields ``last`` (the quote's
  ``close``) and, when the provider returns them, ``bid`` / ``ask``; ``interval=tick``;
  ``as_of`` is the quote's own unix ``timestamp``.
* ``fx_series_1m``  one batch ``/time_series`` call (``interval=1min``, ``timezone=UTC``) for the
  window around the ECB fix: ``crosschecks.fx_vs_ecb.fix_time_cet`` +/- 10 minutes, converted to
  UTC with the DST-aware CET helper (``timeutil.local_to_utc``), so 14:15 CET is 13:15 UTC in
  winter and 12:15 UTC in summer. Field ``close``, ``interval=1m``, ``as_of`` = bar start.

Plans: ``morning`` and ``intraday`` -> ``fx_quote``; ``eod`` -> ``fx_series_1m`` on TARGET business
days only (a TARGET holiday or weekend plans nothing: skip, not fail). ``backfill`` and
``rebuild`` plan nothing (quotes cannot be backfilled; rebuild replays raw files).

Error handling
--------------
* **Provider errors with HTTP 200** (``{"status": "error", "code": ..., "message": ...}``) are
  detected in ``fetch``: the returned ``RawResponse`` keeps the body byte-for-byte but has
  ``status=ERROR`` (``RATE_LIMITED`` when the payload's code is 429) and an ``error`` naming the
  code and message (key masked), so ``source_log`` records the failure like any HTTP error.
  ``parse`` also raises :class:`ParseError` on such a body, so replaying a stored raw file fails
  the same way.
* **Batch with one bad symbol**: ``parse`` emits the good symbols and logs
  ``twelvedata.parse.symbol_error`` for each bad one (provider error entry, unusable value, or a
  requested symbol missing from the response). Requested-but-missing symbols count as failures,
  so a 200 object containing none of the requested symbols (``{}``, an unrelated object) raises
  :class:`ParseError`, as does any response where no requested symbol is usable. A present entry
  with an empty ``values`` list is a legitimate empty frame. ``fetch`` leaves such a response
  ``ok``.

Key and budget
--------------
``fetch`` reads ``TWELVEDATA_API_KEY`` and passes it ONLY as ``secret_params={"apikey": ...}``.
A missing key returns an error ``RawResponse`` (no HTTP call, ``http_status=None``).

Twelve Data bills one credit **per symbol** (a 3-pair batch costs 3), but the T03 tracker reserves
one unit per HTTP attempt and ``HttpClient.get`` has no weight argument, so a batch is tracked as
1 unit and the daily count under-reports real use by up to ``len(symbols)``. Closing that gap needs
a contract change (see ``docs/sources/twelvedata.md``); until then :func:`credits_for` states the
real cost per request for the runner/reporting, and the configured ``daily_budget`` (750 of the
provider's 800) leaves headroom. Exhausted budget gives ``BUDGET_EXHAUSTED`` with no HTTP call
(done by the client).

Payloads are SYNTHETIC in tests until the owner's live probe (``docs/sources/twelvedata.md``).
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from deskbrief.calendars import is_business_day
from deskbrief.config import Config, Feed, Instrument
from deskbrief.ids import new_id
from deskbrief.logging import get_logger
from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse, RunContext
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.sources import registry
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.sources.http import mask_secrets
from deskbrief.timeutil import CET, local_to_utc, utc_now

QUOTE_ENDPOINT = "https://api.twelvedata.com/quote"
SERIES_ENDPOINT = "https://api.twelvedata.com/time_series"
API_KEY_ENV = "TWELVEDATA_API_KEY"
FIX_WINDOW_MIN = 10  # minutes either side of the ECB fix
SERIES_OUTPUTSIZE = 60  # provider default is 30; the window needs 21 bars
_BAR_FORMAT = "%Y-%m-%d %H:%M:%S"
_DATASETS = ("fx_quote", "fx_series_1m")


def credits_for(req: FetchRequest) -> int:
    """Provider credits one request costs: one per symbol (at least 1)."""
    return max(1, len(req.symbols))


class TwelveDataSource:
    name = "twelvedata"
    parser_version = "1"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._quote: dict[str, tuple[Instrument, Feed]] = config.feed_index(self.name, "fx_quote")
        self._series: dict[str, tuple[Instrument, Feed]] = config.feed_index(
            self.name, "fx_series_1m"
        )

    # -- plan -----------------------------------------------------------------------------

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        if job in (JobKind.MORNING, JobKind.INTRADAY):
            return self._plan_quote(ctx)
        if job is JobKind.EOD:
            return self._plan_series(ctx)
        return []

    def _plan_quote(self, ctx: RunContext) -> list[FetchRequest]:
        if not self._quote:
            return []
        symbols = tuple(self._quote)
        return [
            FetchRequest(
                source=self.name,
                dataset="fx_quote",
                params={"symbol": ",".join(symbols), "format": "JSON"},
                symbols=symbols,
                target_date=ctx.target_date,
            )
        ]

    def _plan_series(self, ctx: RunContext) -> list[FetchRequest]:
        if not self._series:
            return []
        day = ctx.target_date
        if not is_business_day("TARGET", day):
            get_logger(source=self.name).info("twelvedata.plan.skip", date=str(day), why="TARGET")
            return []
        fix = local_to_utc(day, self.config.crosschecks.fx_vs_ecb.fix_time_cet, CET)
        first = fix - timedelta(minutes=FIX_WINDOW_MIN)
        last = fix + timedelta(minutes=FIX_WINDOW_MIN)  # last bar START, inclusive
        symbols = tuple(self._series)
        return [
            FetchRequest(
                source=self.name,
                dataset="fx_series_1m",
                params={
                    "symbol": ",".join(symbols),
                    "interval": "1min",
                    "start_date": first.strftime(_BAR_FORMAT),
                    "end_date": last.strftime(_BAR_FORMAT),
                    "timezone": "UTC",
                    "order": "asc",
                    "outputsize": str(SERIES_OUTPUTSIZE),
                    "format": "JSON",
                },
                symbols=symbols,
                target_date=day,
                window=(first, last + timedelta(minutes=1)),  # [start, end): includes last bar
            )
        ]

    # -- fetch ----------------------------------------------------------------------------

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        key = os.environ.get(API_KEY_ENV, "").strip()
        if not key:
            now = utc_now()
            get_logger(source=self.name, dataset=req.dataset).error(
                "twelvedata.fetch.no_api_key", env=API_KEY_ENV, symbols=",".join(req.symbols)
            )
            return RawResponse(
                request=req,
                fetch_id=_new_fetch_id(),
                requested_at=now,
                responded_at=now,
                http_status=None,
                content_type="",
                body=b"",
                error=f"{API_KEY_ENV} is not set; no request made",
                status=FetchStatus.ERROR,
            )
        url = SERIES_ENDPOINT if req.dataset == "fx_series_1m" else QUOTE_ENDPOINT
        raw = http.get(req, url, params=req.params, secret_params={"apikey": key})
        return _flag_payload_error(raw, key)

    # -- parse ----------------------------------------------------------------------------

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        if not raw.ok:
            raise ParseError(f"Twelve Data fetch failed: {raw.error or raw.http_status}")
        dataset = raw.request.dataset
        if dataset not in _DATASETS:
            raise ParseError(f"unexpected Twelve Data dataset {dataset!r}")
        doc = _document(raw.body)
        index = self._quote if dataset == "fx_quote" else self._series
        entries = _entries(doc, dataset, raw.request.symbols)
        log = get_logger(source=self.name, dataset=dataset)
        rows: list[dict[str, Any]] = []
        good = bad = 0
        for sym in raw.request.symbols:
            if sym not in entries:  # a requested symbol the provider did not return is a failure
                bad += 1
                log.warning(
                    "twelvedata.parse.symbol_error", symbol=sym, error="missing in response"
                )
        for symbol, entry in entries.items():
            known = index.get(symbol)
            if known is None:
                continue  # unconfigured symbol: stays in the raw file only
            inst, feed = known
            try:
                if dataset == "fx_quote":
                    new = _quote_rows(entry)
                else:
                    new = _series_rows(entry, raw.request.window)
            except _SymbolError as exc:
                bad += 1
                log.warning("twelvedata.parse.symbol_error", symbol=symbol, error=str(exc))
                continue
            good += 1
            for field, interval, as_of, value in new:
                rows.append(
                    {
                        "instrument_id": inst.id,
                        "source": self.name,
                        "dataset": dataset,
                        "symbol": symbol,
                        "tier": feed.tier,
                        "field": field,
                        "interval": interval,
                        "as_of": as_of,
                        "value": value * feed.scale,
                        "unit": inst.unit,
                        "is_derived": False,
                        "is_fallback": False,
                    }
                )
        if good == 0 and bad > 0:
            raise ParseError(f"Twelve Data {dataset}: no usable symbol ({bad} failed)")
        if not rows:
            return empty_frame("parsed_observations")
        return to_frame("parsed_observations", rows)


class _SymbolError(Exception):
    """One symbol's entry is unusable; the other symbols in the batch are still parsed."""


def _new_fetch_id() -> str:
    return new_id()


def _payload_error(doc: Any) -> tuple[int | None, str] | None:
    """``(code, message)`` if ``doc`` is a provider error object, else None."""
    if isinstance(doc, dict) and doc.get("status") == "error":
        code, message = doc.get("code"), doc.get("message")
        return (
            code if isinstance(code, int) and not isinstance(code, bool) else None,
            message if isinstance(message, str) else "no message",
        )
    return None


def _flag_payload_error(raw: RawResponse, key: str) -> RawResponse:
    """Turn an HTTP-200 provider error payload into an error ``RawResponse`` (body untouched)."""
    if not raw.ok or not raw.body:
        return raw
    try:
        doc = json.loads(raw.body)
    except (ValueError, UnicodeDecodeError):
        return raw  # parse() will reject it
    found = _payload_error(doc)
    if found is None:
        return raw
    code, message = found
    status = FetchStatus.RATE_LIMITED if code == 429 else FetchStatus.ERROR
    text = mask_secrets(f"Twelve Data error {code if code is not None else '?'}: {message}", [key])
    get_logger(source="twelvedata", dataset=raw.request.dataset).warning(
        "twelvedata.fetch.payload_error", code=code, status=str(status), error=text
    )
    return dataclasses.replace(raw, status=status, error=text)


def _document(body: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ParseError(f"Twelve Data body is not valid JSON: {type(exc).__name__}") from exc
    if not isinstance(doc, dict):
        raise ParseError("Twelve Data payload is not a JSON object")
    found = _payload_error(doc)
    if found is not None:
        raise ParseError(
            f"Twelve Data error {found[0] if found[0] is not None else '?'}: {found[1]}"
        )
    return doc


def _entries(doc: Mapping[str, Any], dataset: str, requested: tuple[str, ...]) -> dict[str, Any]:
    """``{symbol: entry}``. A single-symbol request comes back un-nested; wrap it."""
    if dataset == "fx_quote":
        sym = doc.get("symbol")
        if isinstance(sym, str):
            return {sym: doc}
    elif isinstance(doc.get("values"), list):
        meta = doc.get("meta")
        meta_sym = meta.get("symbol") if isinstance(meta, dict) else None
        sym = meta_sym if isinstance(meta_sym, str) else (requested[0] if requested else None)
        if isinstance(sym, str):
            return {sym: doc}
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def _number(value: Any, what: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise _SymbolError(f"{what} is not numeric: {value!r}") from exc
    if not math.isfinite(out) or out <= 0:
        raise _SymbolError(f"{what} is not a positive finite rate: {value!r}")
    return out


def _entry_error(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise _SymbolError(f"entry is not an object: {entry!r}")
    found = _payload_error(entry)
    if found is not None:
        raise _SymbolError(f"provider error {found[0]}: {found[1]}")


def _quote_rows(entry: Any) -> list[tuple[str, str, datetime, float]]:
    _entry_error(entry)
    stamp = entry.get("timestamp")
    if isinstance(stamp, bool) or not isinstance(stamp, int | float | str):
        raise _SymbolError("quote has no timestamp")
    try:
        as_of = datetime.fromtimestamp(float(stamp), UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise _SymbolError(f"quote timestamp is invalid: {stamp!r}") from exc
    out = [("last", "tick", as_of, _number(entry.get("close"), "close"))]
    for field in ("bid", "ask"):
        if entry.get(field) is not None:
            out.append((field, "tick", as_of, _number(entry[field], field)))
    return out


def _series_rows(
    entry: Any, window: tuple[datetime, datetime] | None
) -> list[tuple[str, str, datetime, float]]:
    _entry_error(entry)
    values = entry.get("values")
    if not isinstance(values, list):
        raise _SymbolError("series entry has no 'values' list")
    out: list[tuple[str, str, datetime, float]] = []
    for bar in values:
        if not isinstance(bar, dict):
            raise _SymbolError(f"bar is not an object: {bar!r}")
        text = bar.get("datetime")
        try:
            start = datetime.strptime(text, _BAR_FORMAT).replace(tzinfo=UTC)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise _SymbolError(f"bar has a bad datetime: {text!r}") from exc
        close = _number(bar.get("close"), "close")
        if window is None or window[0] <= start < window[1]:
            out.append(("close", "1m", start, close))
    return out


registry.register("twelvedata", TwelveDataSource)
