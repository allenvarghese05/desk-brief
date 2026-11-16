"""ECB euro foreign exchange reference rates (spec §5.2, card T12).

Endpoint: ECB Data Portal SDMX API, series ``EXR/D.USD+GBP+JPY.EUR.SP00.A`` (daily reference
rates, units of currency per 1 EUR), ``format=csvdata``. No key. Published around 16:00 CET on
TARGET business days. See ``docs/sources/ecb.md`` for the probe steps and attribution.

``parse`` emits only the configured ECB instruments (``config.feed_index("ecb", "eurofxref")``):

* ``EURUSD`` = ECB USD rate (direct, ``is_derived=False``)
* ``GBPUSD`` = EURUSD / EURGBP   (``is_derived=True``)
* ``USDJPY`` = EURJPY / EURUSD   (``is_derived=True``)

All rows are ``field=rate`` (a reference rate, not a close; P-009), ``interval=1d`` with
``as_of`` = 00:00 America/New_York on the reference date (P-006). The raw EUR/GBP and EUR/JPY
rates stay in the raw file only.

Failure policy: a date for which any of USD/GBP/JPY is missing, or a duplicate or non-positive
rate, raises :class:`ParseError` (no partial crosses). No data rows at all is a legitimate
empty frame (header only). Data rows with none of the three currencies, a ``TIME_PERIOD``
outside the requested window, or a non-finite/non-positive derived cross also raise.

``fetch`` treats HTTP 404 as ``empty`` only for a ``text/plain`` response whose whole body is
exactly the SDMX message "No results found." (UNVERIFIED: taken from ECB documentation, to be
confirmed by the live probe in ``docs/sources/ecb.md``). Any other 404 (wrong URL/dataflow,
JSON, HTML, gateway text, other content types) stays an error.

A ``TIME_PERIOD`` outside the requested window raises ``ParseError`` on purpose (fail loud: the
server ignored the window, so the payload is not what was asked for). Revisit once the probe
shows the API's boundary behaviour.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import math
import re
from datetime import date

import pandas as pd

from deskbrief.calendars import is_business_day, prev_business_day
from deskbrief.config import Config
from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse, RunContext
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.sources.registry import register
from deskbrief.timeutil import et_to_utc

SOURCE = "ecb"
DATASET = "eurofxref"
URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD+GBP+JPY.EUR.SP00.A"
_CURRENCIES = ("USD", "GBP", "JPY")
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)  # strict: no "1_16", "nan", "1e3"
# UNVERIFIED: the SDMX "no results" message as documented by ECB (case-sensitive, nothing else in
# the body, served as text/plain). Confirm with the live probe in docs/sources/ecb.md.
_NO_RESULTS = re.compile(r"No results found\.?")
_REQUIRED_COLUMNS = ("CURRENCY", "CURRENCY_DENOM", "TIME_PERIOD", "OBS_VALUE")


class EcbSource:
    name = SOURCE
    parser_version = "1"

    def __init__(self, config: Config) -> None:
        self._feeds = config.feed_index(SOURCE, DATASET)

    # -- plan ------------------------------------------------------------------------------

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        """One single-day request for the reference date, or none on TARGET holidays.

        morning: the previous TARGET business day (today's fixing is not out before 07:00 ET);
        eod/backfill/rebuild: ``ctx.target_date`` itself; intraday: nothing (daily source).
        A morning run on a TARGET holiday or weekend therefore re-plans the same previous
        business day as the next run (idempotent).
        """
        if job is JobKind.INTRADAY:
            return []
        if job is JobKind.MORNING:
            ref = prev_business_day("TARGET", ctx.target_date)
        elif is_business_day("TARGET", ctx.target_date):
            ref = ctx.target_date
        else:
            return []
        return [
            FetchRequest(
                source=SOURCE,
                dataset=DATASET,
                params={
                    "startPeriod": ref.isoformat(),
                    "endPeriod": ref.isoformat(),
                    "format": "csvdata",
                },
                symbols=tuple(self._feeds),
                target_date=ref,
            )
        ]

    # -- fetch -----------------------------------------------------------------------------

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        raw = http.get(req, URL, params=req.params)
        if raw.http_status == 404:
            if _is_no_results(raw):
                return dataclasses.replace(raw, error=None, status=FetchStatus.EMPTY)
            if raw.error is None:  # anomalous 404 on a planned day: surface it
                return dataclasses.replace(raw, error="HTTP 404: unrecognized not-found response")
        return raw

    # -- parse -----------------------------------------------------------------------------

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        rates = _read_rates(raw.body)
        _check_window(rates, raw.request)
        if not rates:
            return empty_frame("parsed_observations")
        rows = []
        for day in sorted(rates):
            by_ccy = rates[day]
            missing = [c for c in _CURRENCIES if c not in by_ccy]
            if missing:
                raise ParseError(f"ECB {day}: missing currencies {missing}")
            usd, gbp, jpy = (by_ccy[c] for c in _CURRENCIES)
            values = {
                "EURUSD": (usd, False),
                "GBPUSD": (usd / gbp, True),
                "USDJPY": (jpy / usd, True),
            }
            for symbol, (value, _) in values.items():
                if not math.isfinite(value) or value <= 0:
                    raise ParseError(f"ECB {day}: {symbol} is not finite and positive: {value}")
            as_of = et_to_utc(day, "00:00")
            for symbol, (value, derived) in values.items():
                if symbol not in self._feeds:
                    continue
                inst, feed = self._feeds[symbol]
                rows.append(
                    {
                        "instrument_id": inst.id,
                        "source": SOURCE,
                        "dataset": DATASET,
                        "symbol": symbol,
                        "tier": feed.tier,
                        "field": "rate",
                        "interval": "1d",
                        "as_of": as_of,
                        "value": value * feed.scale,
                        "unit": inst.unit,
                        "is_derived": derived,
                        "is_fallback": False,
                    }
                )
        if not rows:
            return empty_frame("parsed_observations")
        return to_frame("parsed_observations", rows)


def _is_no_results(raw: RawResponse) -> bool:
    """True only for a text/plain body that is exactly the SDMX no-results message."""
    media_type = raw.content_type.split(";")[0].strip().lower()
    if not media_type.startswith("text/plain"):
        return False
    try:
        text = raw.body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return _NO_RESULTS.fullmatch(text.strip()) is not None


def _read_rates(body: bytes) -> dict[date, dict[str, float]]:
    """``{reference date: {currency: EUR rate}}`` from SDMX CSV; ``{}`` if there are no rows."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError(f"ECB payload is not UTF-8: {exc}") from exc
    # TODO: drop the "#" comment skip once the real capture replaces the synthetic fixture.
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return {}
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    missing_cols = [c for c in _REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing_cols:
        raise ParseError(f"ECB CSV missing columns {missing_cols}")
    out: dict[date, dict[str, float]] = {}
    n_rows = 0
    for rec in reader:
        n_rows += 1
        if rec["CURRENCY_DENOM"] != "EUR" or rec["CURRENCY"] not in _CURRENCIES:
            continue
        try:
            day = date.fromisoformat(rec["TIME_PERIOD"])
            if not _DECIMAL.fullmatch(rec["OBS_VALUE"]):
                raise ValueError("OBS_VALUE is not a plain decimal")
            value = float(rec["OBS_VALUE"])
        except (TypeError, ValueError) as exc:
            raise ParseError(f"ECB row not parseable: {rec!r}") from exc
        if not math.isfinite(value) or value <= 0:
            raise ParseError(f"ECB non-positive or non-finite rate: {rec!r}")
        ccy = rec["CURRENCY"]
        if ccy in out.setdefault(day, {}):
            raise ParseError(f"ECB duplicate {ccy} rate for {day}")
        out[day][ccy] = value
    if n_rows and not out:
        raise ParseError("ECB CSV has data rows but none for USD/GBP/JPY against EUR")
    return out


def _check_window(rates: dict[date, dict[str, float]], req: FetchRequest) -> None:
    """Reject dates outside the requested ``startPeriod``/``endPeriod`` (server ignored them)."""
    try:
        start = (
            date.fromisoformat(req.params["startPeriod"]) if "startPeriod" in req.params else None
        )
        end = date.fromisoformat(req.params["endPeriod"]) if "endPeriod" in req.params else None
    except ValueError as exc:
        raise ParseError(f"bad request window in params: {exc}") from exc
    for day in rates:
        if (start and day < start) or (end and day > end):
            raise ParseError(f"ECB TIME_PERIOD {day} outside requested window {start}..{end}")


register(SOURCE, EcbSource)
