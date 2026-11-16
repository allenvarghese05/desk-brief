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
empty frame, as is an HTTP 404 from ``fetch`` (the API's "no results" answer for a day
without a fixing).
"""

from __future__ import annotations

import csv
import dataclasses
import io
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
        if raw.http_status == 404:  # SDMX "no results": no fixing for the period
            return dataclasses.replace(raw, error=None, status=FetchStatus.EMPTY)
        return raw

    # -- parse -----------------------------------------------------------------------------

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        rates = _read_rates(raw.body)
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


def _read_rates(body: bytes) -> dict[date, dict[str, float]]:
    """``{reference date: {currency: EUR rate}}`` from SDMX CSV; ``{}`` if there are no rows."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError(f"ECB payload is not UTF-8: {exc}") from exc
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return {}
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    missing_cols = [c for c in _REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing_cols:
        raise ParseError(f"ECB CSV missing columns {missing_cols}")
    out: dict[date, dict[str, float]] = {}
    for rec in reader:
        if rec["CURRENCY_DENOM"] != "EUR" or rec["CURRENCY"] not in _CURRENCIES:
            continue
        try:
            day = date.fromisoformat(rec["TIME_PERIOD"])
            value = float(rec["OBS_VALUE"])
        except (TypeError, ValueError) as exc:
            raise ParseError(f"ECB row not parseable: {rec!r}") from exc
        if not value > 0 or value == float("inf"):
            raise ParseError(f"ECB non-positive or non-finite rate: {rec!r}")
        ccy = rec["CURRENCY"]
        if ccy in out.setdefault(day, {}):
            raise ParseError(f"ECB duplicate {ccy} rate for {day}")
        out[day][ccy] = value
    return out


register(SOURCE, EcbSource)
