"""Completeness for the Yahoo and Twelve Data feeds, which have no ``config.datasets`` schedule.

Each rule judges the last finished session (P-009), like the Alpaca minute-bar rule. The eod run
(22:30 UTC) therefore judges the previous session for XNYS-based rules; see the note in
:mod:`deskbrief.checks.completeness`. Early closes (P-011) shorten the tick window.

* ``yahoo/history``: one ``close`` bar for the session day. Missing -> fail.
* ``yahoo/quote`` and ``twelvedata/fx_quote``: at least one tick inside the XNYS session (the
  intraday job only runs then; an early close ends the window early). None -> fail.
* ``twelvedata/fx_series_1m``: the ``2 * FIX_WINDOW_MIN + 1`` bars around the ECB fix on the
  last TARGET day whose window has closed; 100% pass, [95%, 100%) warn, below fail.
* Instruments with ``asset_class == "index"`` (Nikkei, FTSE, DAX) trade on exchanges whose
  calendars are not modelled, so they are ``skip`` with a reason, never ``fail``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import duckdb

from deskbrief.calendars import is_early_close, prev_business_day, session_bounds_utc
from deskbrief.checks.base import (
    ET_DATE_SQL,
    Check,
    closed_reason,
    finished_session_date,
    make_result,
    query,
    records,
)
from deskbrief.config import Feed, Instrument
from deskbrief.models import DQResult, DQStatus, RunContext
from deskbrief.sources.twelvedata import FIX_WINDOW_MIN
from deskbrief.timeutil import CET, local_to_utc

MIN_FIX_RATIO = 0.95


def _skip(
    ctx: RunContext, check: Check, inst: Instrument, feed: Feed, reason: str, **info: Any
) -> DQResult:
    return make_result(
        ctx, check, DQStatus.SKIP, source=feed.source, instrument_id=inst.id,
        reason=reason, dataset=feed.dataset, **info,
    )  # fmt: skip


def _feeds(ctx: RunContext, source: str, dataset: str) -> list[tuple[Instrument, Feed]]:
    return ctx.config.feeds_for(source, dataset)


def _history(ctx: RunContext, check: Check, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
    feeds = _feeds(ctx, "yahoo", "history")
    day = finished_session_date(ctx)
    closed = closed_reason("XNYS", day)
    present: set[str] = set()
    if not closed:
        df = query(
            duck,
            f"SELECT DISTINCT instrument_id FROM obs_pit(?) "  # noqa: S608
            f"WHERE source = 'yahoo' AND dataset = 'history' AND interval = '1d' "
            f"AND field = 'close' AND {ET_DATE_SQL} = ?",
            [ctx.now_utc, day],
        )
        present = set(df["instrument_id"])
    out: list[DQResult] = []
    for inst, feed in feeds:
        info = {"session_day": day.isoformat()}
        if inst.asset_class == "index":
            out.append(_skip(ctx, check, inst, feed, "exchange calendar not modelled", **info))
        elif closed:
            out.append(_skip(ctx, check, inst, feed, closed, **info))
        else:
            ok = inst.id in present
            out.append(
                make_result(
                    ctx, check, DQStatus.PASS if ok else DQStatus.FAIL, source="yahoo",
                    instrument_id=inst.id, observed=1.0 if ok else 0.0, threshold=1.0,
                    dataset="history", **info,
                )
            )  # fmt: skip
    return out


def _ticks(ctx: RunContext, check: Check, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
    day = finished_session_date(ctx)
    closed = closed_reason("XNYS", day)
    bounds = session_bounds_utc(day)
    counts: dict[tuple[str, str, str], int] = {}
    if bounds and not closed:
        df = query(
            duck,
            "SELECT instrument_id, source, dataset, count(*) AS n FROM obs_pit(?) "
            "WHERE interval = 'tick' AND field = 'last' AND source IN ('yahoo', 'twelvedata') "
            "AND as_of >= ? AND as_of <= ? GROUP BY ALL",
            [ctx.now_utc, bounds[0], bounds[1]],
        )
        counts = {(r["instrument_id"], r["source"], r["dataset"]): int(r["n"]) for r in records(df)}
    out: list[DQResult] = []
    for source, dataset in (("yahoo", "quote"), ("twelvedata", "fx_quote")):
        for inst, feed in _feeds(ctx, source, dataset):
            info = {"session_day": day.isoformat(), "early_close": is_early_close(day)}
            if inst.asset_class == "index":
                out.append(_skip(ctx, check, inst, feed, "exchange calendar not modelled", **info))
            elif closed or bounds is None:
                out.append(_skip(ctx, check, inst, feed, closed or "holiday", **info))
            else:
                n = counts.get((inst.id, source, dataset), 0)
                out.append(
                    make_result(
                        ctx, check, DQStatus.PASS if n else DQStatus.FAIL, source=source,
                        instrument_id=inst.id, observed=float(n), threshold=1.0,
                        dataset=dataset, **info,
                    )
                )  # fmt: skip
    return out


def _fix_day(ctx: RunContext) -> date:
    """Latest TARGET business day on or before the target whose fix window has closed."""
    day = ctx.target_date
    while True:
        window_end = local_to_utc(day, ctx.config.crosschecks.fx_vs_ecb.fix_time_cet, CET)
        window_end += timedelta(minutes=FIX_WINDOW_MIN + 1)
        if closed_reason("TARGET", day) is None and window_end <= ctx.now_utc:
            return day
        day = prev_business_day("TARGET", day)
        if day < ctx.target_date - timedelta(days=10):  # pragma: no cover - defensive
            return day


def _fx_series(ctx: RunContext, check: Check, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
    feeds = _feeds(ctx, "twelvedata", "fx_series_1m")
    day = _fix_day(ctx)
    fix = local_to_utc(day, ctx.config.crosschecks.fx_vs_ecb.fix_time_cet, CET)
    first, end = (
        fix - timedelta(minutes=FIX_WINDOW_MIN),
        fix + timedelta(minutes=FIX_WINDOW_MIN + 1),
    )
    expected = 2 * FIX_WINDOW_MIN + 1
    df = query(
        duck,
        "SELECT instrument_id, count(DISTINCT as_of) AS n FROM obs_pit(?) "
        "WHERE source = 'twelvedata' AND dataset = 'fx_series_1m' AND interval = '1m' "
        "AND field = 'close' AND as_of >= ? AND as_of < ? GROUP BY ALL",
        [ctx.now_utc, first, end],
    )
    counts = {r["instrument_id"]: int(r["n"]) for r in records(df)}
    out: list[DQResult] = []
    for inst, feed in feeds:
        n = counts.get(inst.id, 0)
        ratio = n / expected
        status = (
            DQStatus.PASS
            if ratio >= 1.0
            else DQStatus.WARN
            if ratio >= MIN_FIX_RATIO
            else DQStatus.FAIL
        )
        out.append(
            make_result(
                ctx, check, status, source=feed.source, instrument_id=inst.id, observed=ratio,
                threshold=MIN_FIX_RATIO, dataset=feed.dataset, fix_day=day.isoformat(),
                bars=n, expected_bars=expected,
            )
        )  # fmt: skip
    return out


def evaluate_feeds(
    ctx: RunContext, check: Check, duck: duckdb.DuckDBPyConnection
) -> list[DQResult]:
    return [*_history(ctx, check, duck), *_ticks(ctx, check, duck), *_fx_series(ctx, check, duck)]
