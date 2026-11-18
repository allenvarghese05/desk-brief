"""Completeness: does every expected (instrument, feed) have data? (spec §8.1)

* Daily feeds of the datasets in ``config.datasets`` must have >= 1 ``1d`` observation for the
  data day (see :func:`expected_for`). Missing -> fail, or warn on a SIFMA early close.
  Skipped on closed days and until ``expected_by_et`` + grace has passed. Monthly FRED series
  are not daily feeds and are not evaluated.
  A feed still within grace without data is ``skip`` with reason "waiting" (freshness agrees).
  FRED oil series (DCOILWTICO, DCOILBRENTEU) are ``skip`` on data days when the oil market is
  shut although SIFMA is open (see ``base.OIL_CALENDARS``). A FRED series with its own
  ``SeriesSpec.lag_bdays`` is judged on that lag instead of the dataset's: the EIA spot prices
  behind the oil series are published weekly, so a 1-day lag failed them most days (T25c).
* Alpaca minute bars need >= 95% of the session's minutes on the last finished XNYS session
  (early closes use their shorter session): 100% pass, [95%, 100%) warn, below fail. Only the
  bar feeds the Alpaca plan fetches (``crosschecks.iex_vs_sip.symbols``, see
  ``sources.alpaca.bar_symbols``) are judged; the other configured ``sip_bars_1m`` feeds are
  ``skip`` (T25c: they failed on every run although nothing ever fetches them).

P-009 consequence for the eod run: it fires at 22:30 UTC (17:30 ET), when the target date is
still "today" in ET and so unfinished. The minute-bar rule therefore judges the *previous*
session's bars at eod, not the one that just closed; that session's bars are judged by the next
run. The same ``finished_session_date`` rule applies to the Yahoo and Twelve Data checks in
:mod:`deskbrief.checks.feed_completeness`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import duckdb

from deskbrief.calendars import add_business_days, is_early_close, session_bounds_utc
from deskbrief.checks.base import (
    ET_DATE_SQL,
    Expected,
    closed_reason,
    expected_for,
    finished_session_date,
    make_result,
    market_closed,
    query,
    records,
    register,
    source_enabled,
    within_grace,
)
from deskbrief.checks.feed_completeness import evaluate_feeds
from deskbrief.config import DatasetSchedule
from deskbrief.models import CheckType, DQResult, DQStatus, RunContext
from deskbrief.sources.alpaca import bar_symbols
from deskbrief.sources.fred import SERIES_SPECS

MINUTE_DATASETS: frozenset[tuple[str, str]] = frozenset(
    {("alpaca", "iex_bars_1m"), ("alpaca", "sip_bars_1m")}
)
MIN_MINUTE_RATIO = 0.95
#: skip reason for configured bar feeds the Alpaca plan never fetches (``bar_symbols``)
NOT_PLANNED = "not fetched: Alpaca bars cover crosschecks.iex_vs_sip.symbols only"


def _data_day(sched: DatasetSchedule, exp: Expected, symbol: str) -> date:
    """The observation date ``symbol`` must have: the dataset's, unless the FRED series has its
    own publication lag (``SeriesSpec.lag_bdays``, e.g. the weekly EIA oil prices)."""
    spec = SERIES_SPECS.get(symbol) if sched.source == "fred" else None
    if spec is None or spec.lag_bdays is None:
        return exp.data_day
    return add_business_days(sched.calendar, exp.publication_day, -spec.lag_bdays)


def _early(sched: DatasetSchedule, day: date) -> bool:
    return sched.calendar == "SIFMA_US" and is_early_close(day, cal=sched.calendar)


def _as_date(value: Any) -> date:
    return value.date() if isinstance(value, datetime) else value  # type: ignore[no-any-return]


class CompletenessCheck:
    name = "completeness"
    check_type = CheckType.COMPLETENESS

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        return [
            *self._daily(ctx, duck),
            *self._minutes(ctx, duck),
            *evaluate_feeds(ctx, self, duck),
        ]

    def _daily(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        out: list[DQResult] = []
        for sched in ctx.config.datasets:
            if not source_enabled(ctx, sched.source):
                continue  # switched off in config.yaml: nothing is expected from it
            exp = expected_for(ctx, sched)
            waiting = exp.skip_reason is None and within_grace(ctx, exp)
            feeds = [
                (inst, feed)
                for inst, feed in ctx.config.feeds_for(sched.source, sched.dataset)
                if SERIES_SPECS.get(feed.symbol) is None
                or SERIES_SPECS[feed.symbol].interval == "1d"
            ]
            days = {inst.id: _data_day(sched, exp, feed.symbol) for inst, feed in feeds}
            present: set[tuple[str, date]] = set()
            if exp.skip_reason is None and days:
                df = query(
                    duck,
                    f"SELECT DISTINCT instrument_id, {ET_DATE_SQL} AS d FROM obs_pit(?) "  # noqa: S608
                    f"WHERE source = ? AND dataset = ? AND interval = '1d' "
                    f"AND {ET_DATE_SQL} BETWEEN ? AND ?",
                    [ctx.now_utc, sched.source, sched.dataset, min(days.values()),
                     max(days.values())],
                )  # fmt: skip
                present = {(r["instrument_id"], _as_date(r["d"])) for r in records(df)}
            for inst, feed in feeds:
                day = days[inst.id]
                early = exp.early_close if day == exp.data_day else _early(sched, day)
                info: dict[str, Any] = {"dataset": sched.dataset, "data_day": day.isoformat()}
                have = (inst.id, day) in present
                reason = exp.skip_reason
                if reason is None and market_closed(feed.symbol, day):
                    reason = "oil market closed"  # SIFMA open, oil shut (e.g. Good Friday)
                elif reason is None and waiting and not have:
                    reason = "waiting"  # same wording as freshness
                if reason:
                    out.append(
                        make_result(
                            ctx, self, DQStatus.SKIP, source=sched.source,
                            instrument_id=inst.id, reason=reason, **info,
                        )
                    )  # fmt: skip
                    continue
                status = DQStatus.PASS if have else DQStatus.WARN if early else DQStatus.FAIL
                out.append(
                    make_result(
                        ctx, self, status, source=sched.source, instrument_id=inst.id,
                        observed=1.0 if have else 0.0, threshold=1.0,
                        early_close=early, **info,
                    )
                )  # fmt: skip
        return out

    def _minutes(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        all_feeds = [
            (inst, feed)
            for inst in ctx.config.instruments
            for feed in inst.feeds
            if (feed.source, feed.dataset) in MINUTE_DATASETS
        ]
        day = finished_session_date(ctx)
        planned = {d: set(bar_symbols(ctx.config, d)) for _, d in MINUTE_DATASETS}
        feeds = [(i, f) for i, f in all_feeds if f.symbol in planned[f.dataset]]
        unplanned = [
            make_result(
                ctx, self, DQStatus.SKIP, source=f.source, instrument_id=i.id,
                reason=NOT_PLANNED, dataset=f.dataset, session_day=day.isoformat(),
            )
            for i, f in all_feeds
            if f.symbol not in planned[f.dataset]
        ]  # fmt: skip
        reason = closed_reason("XNYS", day)
        bounds = session_bounds_utc(day)
        if reason or bounds is None:
            return [
                make_result(
                    ctx, self, DQStatus.SKIP, source=f.source, instrument_id=i.id,
                    reason=reason or "holiday", dataset=f.dataset, session_day=day.isoformat(),
                )
                for i, f in feeds
            ] + unplanned  # fmt: skip
        open_utc, close_utc = bounds
        expected = int((close_utc - open_utc).total_seconds() // 60)
        df = query(
            duck,
            "SELECT instrument_id, source, dataset, count(DISTINCT as_of) AS n "
            "FROM obs_pit(?) WHERE interval = '1m' AND field = 'close' "
            "AND as_of >= ? AND as_of < ? GROUP BY ALL",
            [ctx.now_utc, open_utc, close_utc],
        )
        counts = {(r["instrument_id"], r["source"], r["dataset"]): int(r["n"]) for r in records(df)}
        out: list[DQResult] = list(unplanned)
        for inst, feed in feeds:
            n = counts.get((inst.id, feed.source, feed.dataset), 0)
            ratio = n / expected
            if ratio >= 1.0:
                status = DQStatus.PASS
            elif ratio >= MIN_MINUTE_RATIO:
                status = DQStatus.WARN
            else:
                status = DQStatus.FAIL
            out.append(
                make_result(
                    ctx, self, status, source=feed.source, instrument_id=inst.id,
                    observed=ratio, threshold=MIN_MINUTE_RATIO, dataset=feed.dataset,
                    session_day=day.isoformat(), bars=n, expected_bars=expected,
                    early_close=expected < 390,
                )
            )  # fmt: skip
        return out


register(CompletenessCheck())
