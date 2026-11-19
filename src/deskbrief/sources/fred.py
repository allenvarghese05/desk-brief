"""FRED source (card T11; spec §5.2): ``series/observations`` for the official macro series.

* ``plan``  one request per configured series: the last 10 observations
  (``sort_order=desc&limit=10``), so a later revision of a recent value is visible to the
  revision check. Backfill jobs also pass ``observation_end=<target_date>``. ``intraday`` and
  ``rebuild`` plan nothing (FRED is not an intraday source; rebuild replays raw files).
  Daily and monthly series are on dataset ``series``; weekly series (``1w``, NFCI) are on
  ``series_weekly`` (card T46), which has its own freshness entry with a ``release_weekday``.
* ``plan_range`` (P-020, card T46) one request per series covering ``[start, end]``
  (``observation_start``/``observation_end``, ``limit=100000``, FRED's maximum), so two years
  of daily data is one call per series.
* ``fetch`` reads ``FRED_API_KEY`` from the environment and passes it ONLY as
  ``secret_params={"api_key": ...}``; it is never in ``FetchRequest.params``, ``url_redacted``,
  the raw response or a log line. **Missing key**: no HTTP call is made and an error
  ``RawResponse`` is returned (``http_status=None``, ``error`` naming ``FRED_API_KEY``), so the
  failure lands in ``source_log`` like any other fetch error and the run continues degraded.
* ``parse`` is a pure function of the body. A value of ``"."`` means "missing" and the row is
  skipped (never NaN). Malformed payloads raise :class:`ParseError`; zero usable rows is a
  legitimate ``empty_frame``.

Field/interval per series are fixed here (FRED does not say); tier, unit and scale come from
config. ``1d`` ``as_of`` = 00:00 ET on the observation date; ``1mo`` ``as_of`` = 00:00 ET on the
first of the month (P-006); ``1w`` ``as_of`` = 00:00 ET on the reference date FRED gives (the
Friday that ends the NFCI week, P-021). Payloads are SYNTHETIC in tests until the owner's live probe
(``docs/sources/fred.md``).
"""

from __future__ import annotations

import json
import math
import os
from datetime import date
from typing import Any, NamedTuple

import pandas as pd

from deskbrief.config import Config, Feed, Instrument
from deskbrief.logging import get_logger
from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse, RunContext
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.sources import registry
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.timeutil import et_to_utc, utc_now

ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"
API_KEY_ENV = "FRED_API_KEY"
OBSERVATION_LIMIT = 10
#: FRED's maximum ``limit``; a range request asks for everything in its window in one call.
RANGE_LIMIT = 100_000
MISSING = "."
#: Dataset of daily and monthly series, and of weekly series (P-021, card T46).
DAILY_DATASET = "series"
WEEKLY_DATASET = "series_weekly"
DATASETS = (DAILY_DATASET, WEEKLY_DATASET)


class SeriesSpec(NamedTuple):
    field: str
    interval: str  # "1d", "1w" or "1mo"
    #: business days between publication and the newest value, when the series lags more than
    #: the dataset's ``config.datasets`` entry (``lag_bdays: 1``); used by completeness
    lag_bdays: int | None = None


#: The EIA spot prices behind DCOILWTICO / DCOILBRENTEU are released weekly (normally on
#: Wednesday, through the Monday or Friday before), so the newest value is up to ~6 business
#: days old before the next release, and a holiday week adds one or two. Run 3 (2026-11-18)
#: failed both on a 1-day lag. Approximation, not a live-verified schedule (docs/sources/fred.md).
OIL_LAG_BDAYS = 8

#: The broad dollar index (DTWEXBGS) comes from the Fed's weekly H.10 release, published on
#: Monday afternoon (about 16:15 ET) with daily values through the Friday before. So the newest
#: value is 1 business day old after the release and 5 just before the next one; 6 also covers
#: a Monday eod run before FRED has posted the release (and a Monday holiday, which the SIFMA
#: calendar skips). FRED's series page, checked 2026-11-19 (docs/sources/fred.md).
DOLLAR_LAG_BDAYS = 6


#: How each configured FRED series is observed. Unit, tier and scale come from config.
SERIES_SPECS: dict[str, SeriesSpec] = {
    "DGS2": SeriesSpec("yield", "1d"),
    "DGS10": SeriesSpec("yield", "1d"),
    "DGS20": SeriesSpec("yield", "1d"),
    "DGS30": SeriesSpec("yield", "1d"),
    "DFF": SeriesSpec("rate", "1d"),
    "DCOILWTICO": SeriesSpec("close", "1d", OIL_LAG_BDAYS),
    "DCOILBRENTEU": SeriesSpec("close", "1d", OIL_LAG_BDAYS),
    "CPIAUCSL": SeriesSpec("close", "1mo"),
    "UNRATE": SeriesSpec("rate", "1mo"),
    # card T46 (P-021a)
    "DFII10": SeriesSpec("yield", "1d"),  # 10-year TIPS real yield; may be negative
    "T10YIE": SeriesSpec("yield", "1d"),  # 10-year breakeven inflation
    "DTWEXBGS": SeriesSpec("level", "1d", DOLLAR_LAG_BDAYS),  # broad dollar index
    "BAMLH0A0HYM2": SeriesSpec("rate", "1d"),  # ICE BofA US high-yield OAS
    "NFCI": SeriesSpec("level", "1w"),  # Chicago Fed NFCI; negative = looser than average
}


def dataset_for(spec: SeriesSpec) -> str:
    """``series_weekly`` for a weekly series, else ``series``."""
    return WEEKLY_DATASET if spec.interval == "1w" else DAILY_DATASET


class FredSource:
    name = "fred"
    parser_version = "1"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._index: dict[str, tuple[Instrument, Feed]] = {
            symbol: entry
            for ds in DATASETS
            for symbol, entry in config.feed_index("fred", ds).items()
        }
        self._specs: dict[str, SeriesSpec] = dict(SERIES_SPECS)

    # -- plan -----------------------------------------------------------------------------

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        if job in (JobKind.INTRADAY, JobKind.REBUILD):
            return []
        extra = {"observation_end": ctx.target_date.isoformat()} if job is JobKind.BACKFILL else {}
        params = {"sort_order": "desc", "limit": str(OBSERVATION_LIMIT), **extra}
        return self._requests(params, ctx.target_date)

    def plan_range(self, start: date, end: date, ctx: RunContext) -> list[FetchRequest]:
        """One request per configured series for the ET dates ``[start, end]`` (P-020)."""
        if end < start:
            raise ValueError("end must not be before start")
        params = {
            "sort_order": "asc",
            "limit": str(RANGE_LIMIT),
            "observation_start": start.isoformat(),
            "observation_end": end.isoformat(),
        }
        return self._requests(params, end)

    def _requests(self, params: dict[str, str], target: date) -> list[FetchRequest]:
        """One request per configured series with a spec whose dataset matches its feed."""
        log = get_logger(source=self.name)
        requests: list[FetchRequest] = []
        for series_id, (_, feed) in self._index.items():
            spec = self._specs.get(series_id)
            if spec is None:
                log.error("fred.plan.skip", series=series_id, reason="no series spec")
                continue
            if feed.dataset != dataset_for(spec):
                log.error(
                    "fred.plan.skip",
                    series=series_id,
                    reason=f"interval {spec.interval} belongs on dataset {dataset_for(spec)}",
                )
                continue
            requests.append(
                FetchRequest(
                    source=self.name,
                    dataset=feed.dataset,
                    params={"series_id": series_id, "file_type": "json", **params},
                    symbols=(series_id,),
                    target_date=target,
                )
            )
        return requests

    # -- fetch ----------------------------------------------------------------------------

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        key = os.environ.get(API_KEY_ENV, "").strip()
        if not key:
            now = utc_now()
            get_logger(source=self.name, dataset=req.dataset).error(
                "fred.fetch.no_api_key", env=API_KEY_ENV, symbols=",".join(req.symbols)
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
        return http.get(req, ENDPOINT, params=req.params, secret_params={"api_key": key})

    # -- parse ----------------------------------------------------------------------------

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        if not raw.ok:
            raise ParseError(f"FRED fetch failed: {raw.error or raw.http_status}")
        series_id = raw.request.symbols[0] if raw.request.symbols else None
        if series_id is None:
            raise ParseError("FRED request has no series symbol")
        observations = _observations(raw.body)
        rows: list[tuple[date, float]] = []
        for item in observations:
            day, value = _row(item)
            if value is not None:
                rows.append((day, value))

        entry = self._index.get(series_id)
        spec = self._specs.get(series_id)
        if entry is None or spec is None or not rows:
            return empty_frame("parsed_observations")
        inst, feed = entry
        out: list[dict[str, Any]] = []
        for day, value in rows:
            ref = day.replace(day=1) if spec.interval == "1mo" else day
            out.append(
                {
                    "instrument_id": inst.id,
                    "source": self.name,
                    "dataset": feed.dataset,
                    "symbol": series_id,
                    "tier": feed.tier,
                    "field": spec.field,
                    "interval": spec.interval,
                    "as_of": et_to_utc(ref, "00:00"),
                    "value": value * feed.scale,
                    "unit": inst.unit,
                    "is_derived": False,
                    "is_fallback": False,
                }
            )
        return to_frame("parsed_observations", out)


def _new_fetch_id() -> str:
    from deskbrief.ids import new_id

    return new_id()


def _observations(body: bytes) -> list[Any]:
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ParseError(f"FRED body is not valid JSON: {type(exc).__name__}") from exc
    if not isinstance(doc, dict):
        raise ParseError("FRED payload is not a JSON object")
    items = doc.get("observations")
    if not isinstance(items, list):
        detail = doc.get("error_message")
        raise ParseError(
            "FRED payload has no 'observations' list"
            + (f" (error_message: {detail})" if isinstance(detail, str) else "")
        )
    return items


def _row(item: Any) -> tuple[date, float | None]:
    """``(observation date, value)``; value is None for the ``"."`` missing marker."""
    if not isinstance(item, dict):
        raise ParseError(f"FRED observation is not an object: {item!r}")
    raw_day, raw_value = item.get("date"), item.get("value")
    if not isinstance(raw_day, str) or not isinstance(raw_value, str):
        raise ParseError(f"FRED observation needs string 'date' and 'value': {item!r}")
    try:
        day = date.fromisoformat(raw_day)
    except ValueError as exc:
        raise ParseError(f"FRED observation has a bad date: {raw_day!r}") from exc
    if raw_value.strip() == MISSING:
        return day, None
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ParseError(f"FRED observation has a non-numeric value: {raw_value!r}") from exc
    if not math.isfinite(value):
        raise ParseError(f"FRED observation value is not finite: {raw_value!r}")
    return day, value


registry.register("fred", FredSource)
