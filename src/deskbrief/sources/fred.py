"""FRED source (card T11; spec §5.2): ``series/observations`` for the official macro series.

* ``plan``  one request per configured series (``config.feed_index("fred", "series")``):
  the last 10 observations (``sort_order=desc&limit=10``), so a later revision of a recent value
  is visible to the revision check. Backfill jobs also pass ``observation_end=<target_date>``.
  ``intraday`` and ``rebuild`` plan nothing (FRED is not an intraday source; rebuild replays
  raw files).
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
first of the month (P-006). Payloads are SYNTHETIC in tests until the owner's live probe
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
MISSING = "."


class SeriesSpec(NamedTuple):
    field: str
    interval: str  # "1d" or "1mo"


#: How each configured FRED series is observed. Unit, tier and scale come from config.
SERIES_SPECS: dict[str, SeriesSpec] = {
    "DGS2": SeriesSpec("yield", "1d"),
    "DGS10": SeriesSpec("yield", "1d"),
    "DGS20": SeriesSpec("yield", "1d"),
    "DGS30": SeriesSpec("yield", "1d"),
    "DFF": SeriesSpec("rate", "1d"),
    "DCOILWTICO": SeriesSpec("close", "1d"),
    "DCOILBRENTEU": SeriesSpec("close", "1d"),
    "CPIAUCSL": SeriesSpec("close", "1mo"),
    "UNRATE": SeriesSpec("rate", "1mo"),
}


class FredSource:
    name = "fred"
    parser_version = "1"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._index: dict[str, tuple[Instrument, Feed]] = config.feed_index("fred", "series")
        self._specs: dict[str, SeriesSpec] = dict(SERIES_SPECS)

    # -- plan -----------------------------------------------------------------------------

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        if job in (JobKind.INTRADAY, JobKind.REBUILD):
            return []
        log = get_logger(source=self.name)
        requests: list[FetchRequest] = []
        for series_id in self._index:
            if series_id not in self._specs:
                log.error("fred.plan.skip", series=series_id, reason="no series spec")
                continue
            params = {
                "series_id": series_id,
                "file_type": "json",
                "sort_order": "desc",
                "limit": str(OBSERVATION_LIMIT),
            }
            if job is JobKind.BACKFILL:
                params["observation_end"] = ctx.target_date.isoformat()
            requests.append(
                FetchRequest(
                    source=self.name,
                    dataset="series",
                    params=params,
                    symbols=(series_id,),
                    target_date=ctx.target_date,
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
                    "dataset": "series",
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
