"""US Treasury daily par yield curve source (card T10, spec §5.2).

Endpoint: the monthly CSV download of the "Daily Treasury Par Yield Curve Rates" page on
``home.treasury.gov`` (no key). One request returns every business day of one month, with a
``Date`` column (``MM/DD/YYYY``) followed by one column per tenor (``"1 Mo"`` ... ``"30 Yr"``;
newer files also carry e.g. ``"1.5 Month"``). Tenor columns are matched to configured feed
symbols (``config.feed_index("treasury", "par_curve")``) ignoring case and whitespace; unknown
columns are ignored. Blank / ``N/A`` cells mean "not published" and yield no observation.

``as_of`` is 00:00 America/New_York of the curve date (P-006). Published after the close;
holidays follow SIFMA, so ``plan`` skips non-business days for ``eod``.

Backfill: ``RunContext`` carries a single ``target_date``, so ``plan(BACKFILL, ctx)`` covers the
month containing it. Use :meth:`TreasurySource.plan_range` to cover an arbitrary date range
(one request per month).

Probe status and verification steps: ``docs/sources/treasury.md``.
"""

from __future__ import annotations

import csv
import math
from datetime import date, datetime

import pandas as pd

from deskbrief.calendars import is_business_day, prev_business_day
from deskbrief.config import Config
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.sources.registry import register
from deskbrief.timeutil import et_to_utc

SOURCE = "treasury"
DATASET = "par_curve"
BASE_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
)
_MISSING = {"", "n/a", "na", "-", "--"}


def _norm(header: str) -> str:
    return " ".join(header.split()).lower()


def _month_key(d: date) -> str:
    return f"{d.year:04d}{d.month:02d}"


def _parse_date(text: str) -> date:
    text = text.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(text, fmt).date()  # noqa: DTZ007 - naive date only
        except ValueError:
            continue
        if not 1990 <= d.year <= 2100:
            break
        return d
    raise ParseError(f"treasury: empty or unparseable date {text!r}")


class TreasurySource:
    name = SOURCE
    parser_version = "1"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._index = config.feed_index(SOURCE, DATASET)

    # -- plan ---------------------------------------------------------------------------

    def _request(self, month: date, target: date) -> FetchRequest:
        return FetchRequest(
            source=SOURCE,
            dataset=DATASET,
            params={"month": _month_key(month)},
            symbols=tuple(self._index),
            target_date=target,
        )

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        if job is JobKind.MORNING:
            d = prev_business_day("SIFMA_US", ctx.target_date)
            return [self._request(d, d)]
        if job is JobKind.EOD:
            if not is_business_day("SIFMA_US", ctx.target_date):
                return []  # weekend / bond-market holiday: skip, not fail
            return [self._request(ctx.target_date, ctx.target_date)]
        if job is JobKind.BACKFILL:
            return self.plan_range(ctx.target_date, ctx.target_date)
        return []

    def plan_range(self, start: date, end: date) -> list[FetchRequest]:
        """One request per calendar month overlapping ``[start, end]`` (inclusive)."""
        if end < start:
            raise ValueError("end must not be before start")
        reqs: list[FetchRequest] = []
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            first = date(year, month, 1)
            reqs.append(self._request(first, max(first, start)))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return reqs

    # -- fetch --------------------------------------------------------------------------

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        month = req.params["month"]
        return http.get(
            req,
            BASE_URL.format(year=month[:4]),
            params={
                "type": "daily_treasury_yield_curve",
                "field_tdr_date_value_month": month,
                "_format": "csv",
            },
        )

    # -- parse --------------------------------------------------------------------------

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        if not raw.ok:
            raise ParseError(f"treasury: unusable response ({raw.fetch_status}: {raw.error})")
        try:
            text = raw.body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseError(f"treasury: body is not UTF-8: {exc}") from exc
        lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        if not lines:
            return empty_frame("parsed_observations")

        reader = csv.DictReader(lines, skipinitialspace=True)
        fields: dict[str, str] = {}
        for raw_name in reader.fieldnames or []:
            name = _norm(raw_name)
            if not name:
                continue  # trailing comma in the header
            if name in fields:
                raise ParseError(f"treasury: duplicate column {raw_name!r}")
            fields[name] = raw_name
        if "date" not in fields:
            raise ParseError("treasury: no 'Date' column (not a par yield curve CSV?)")
        by_norm = {_norm(sym): sym for sym in self._index}
        cols = [(fields[n], by_norm[n]) for n in fields if n in by_norm]

        rows: list[dict[str, object]] = []
        seen: dict[date, tuple[str, ...]] = {}
        try:
            for rec in reader:
                if None in rec:
                    raise ParseError("treasury: row has more cells than the header")
                if all((v or "").strip() == "" for v in rec.values()):
                    continue  # e.g. a trailing ",,,,," line
                if any(rec[f] is None for f in fields.values()):
                    raise ParseError("treasury: row has fewer cells than the header (truncated?)")
                day = _parse_date(rec[fields["date"]] or "")
                cells = tuple((rec[col] or "").strip() for col, _ in cols)
                if day in seen:
                    if seen[day] != cells:
                        raise ParseError(f"treasury: conflicting duplicate rows for {day}")
                    continue
                seen[day] = cells
                as_of = et_to_utc(day, "00:00")
                for (_, symbol), cell in zip(cols, cells, strict=True):
                    if cell.lower() in _MISSING:
                        continue
                    try:
                        value = float(cell)
                    except ValueError as exc:
                        raise ParseError(
                            f"treasury: non-numeric {symbol!r} on {day}: {cell!r}"
                        ) from exc
                    if not math.isfinite(value):
                        raise ParseError(f"treasury: non-finite {symbol!r} on {day}: {cell!r}")
                    inst, feed = self._index[symbol]
                    rows.append(
                        {
                            "instrument_id": inst.id,
                            "source": SOURCE,
                            "dataset": DATASET,
                            "symbol": symbol,
                            "tier": feed.tier,
                            "field": "yield",
                            "interval": "1d",
                            "as_of": as_of,
                            "value": value * feed.scale,
                            "unit": inst.unit,
                            "is_derived": False,
                            "is_fallback": False,
                        }
                    )
        except csv.Error as exc:
            raise ParseError(f"treasury: CSV error: {exc}") from exc
        if not rows:
            return empty_frame("parsed_observations")
        return to_frame("parsed_observations", rows)


register(SOURCE, TreasurySource)
