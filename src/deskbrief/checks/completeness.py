"""Completeness: does every expected (instrument, feed) have data? (spec §8.1)

* Daily feeds of the datasets in ``config.datasets`` must have >= 1 ``1d`` observation for the
  data day (see :func:`expected_for`). Missing -> fail, or warn on a SIFMA early close.
  Skipped on closed days and until ``expected_by_et`` + grace has passed. Monthly FRED series
  are not daily feeds and are not evaluated.
  A feed still within grace without data is ``skip`` with reason "waiting" (freshness agrees).
  FRED oil series (DCOILWTICO, DCOILBRENTEU) are ``skip`` on data days when the oil market is
  shut although SIFMA is open (see ``base.OIL_CALENDARS``).
* Alpaca minute bars need >= 95% of the session's minutes on the last finished XNYS session
  (early closes use their shorter session): 100% pass, [95%, 100%) warn, below fail.

P-009 consequence for the eod run: it fires at 22:30 UTC (17:30 ET), when the target date is
still "today" in ET and so unfinished. The minute-bar rule therefore judges the *previous*
session's bars at eod, not the one that just closed; that session's bars are judged by the next
run. The same ``finished_session_date`` rule applies to the Yahoo and Twelve Data checks in
:mod:`deskbrief.checks.feed_completeness`.
"""

from __future__ import annotations

from typing import Any

import duckdb

from deskbrief.calendars import session_bounds_utc
from deskbrief.checks.base import (
    ET_DATE_SQL,
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
from deskbrief.models import CheckType, DQResult, DQStatus, RunContext
from deskbrief.sources.fred import SERIES_SPECS

MINUTE_DATASETS: frozenset[tuple[str, str]] = frozenset(
    {("alpaca", "iex_bars_1m"), ("alpaca", "sip_bars_1m")}
)
MIN_MINUTE_RATIO = 0.95


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
            present: set[str] = set()
            if exp.skip_reason is None:
                df = query(
                    duck,
                    f"SELECT DISTINCT instrument_id FROM obs_pit(?) "  # noqa: S608
                    f"WHERE source = ? AND dataset = ? AND interval = '1d' AND {ET_DATE_SQL} = ?",
                    [ctx.now_utc, sched.source, sched.dataset, exp.data_day],
                )
                present = set(df["instrument_id"])
            for inst, feed in feeds:
                info: dict[str, Any] = {
                    "dataset": sched.dataset,
                    "data_day": exp.data_day.isoformat(),
                }
                reason = exp.skip_reason
                if reason is None and market_closed(feed.symbol, exp.data_day):
                    reason = "oil market closed"  # SIFMA open, oil shut (e.g. Good Friday)
                elif reason is None and waiting and inst.id not in present:
                    reason = "waiting"  # same wording as freshness
                if reason:
                    out.append(
                        make_result(
                            ctx, self, DQStatus.SKIP, source=sched.source,
                            instrument_id=inst.id, reason=reason, **info,
                        )
                    )  # fmt: skip
                    continue
                ok = inst.id in present
                status = (
                    DQStatus.PASS if ok else DQStatus.WARN if exp.early_close else DQStatus.FAIL
                )
                out.append(
                    make_result(
                        ctx, self, status, source=sched.source, instrument_id=inst.id,
                        observed=1.0 if ok else 0.0, threshold=1.0,
                        early_close=exp.early_close, **info,
                    )
                )  # fmt: skip
        return out

    def _minutes(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        feeds = [
            (inst, feed)
            for inst in ctx.config.instruments
            for feed in inst.feeds
            if (feed.source, feed.dataset) in MINUTE_DATASETS
        ]
        day = finished_session_date(ctx)
        reason = closed_reason("XNYS", day)
        bounds = session_bounds_utc(day)
        if reason or bounds is None:
            return [
                make_result(
                    ctx, self, DQStatus.SKIP, source=f.source, instrument_id=i.id,
                    reason=reason or "holiday", dataset=f.dataset, session_day=day.isoformat(),
                )
                for i, f in feeds
            ]  # fmt: skip
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
        out: list[DQResult] = []
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
