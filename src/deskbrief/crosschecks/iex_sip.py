"""``iex_vs_sip``: Alpaca IEX 1m close vs SIP 1m close, minute by minute (spec §8.2).

Members are ``crosschecks.iex_vs_sip.symbols`` (the Alpaca source, T14, fetches both bar sets
only for those symbols, in ``eod``/``backfill``). Evaluated on the last completed XNYS session
before today (P-009, reading ``obs_pit``); the session is ``session_bounds_utc(ref)`` so early
closes count only minutes up to the early close (P-011).

Bars pair on their start minute (``as_of`` = bar start, P-006). A minute present on only one
side, or with no bar at all, is excluded from the statistics and counted in the details
(``n_missing_iex``, ``n_missing_sip``, ``n_missing_both``); a zero SIP close cannot be a
reference and is counted in ``n_zero_reference``. The gap is the relative difference of the IEX
close against the SIP close in bp (SIP is the reference, 1 bp = 0.01%).

Status (``max_fail_minutes_pct`` = M): ``fail`` if more than M % of compared minutes have
``|gap| > fail_bps``; else ``warn`` if more than M % have ``|gap| > warn_bps``; else ``pass``.
The row's ``diff_bps`` is the median absolute gap; the 95th percentile (nearest rank), the share
of minutes over warn/fail and the minute counts are in the details. No compared minute -> ``skip``.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any

import duckdb

from deskbrief.calendars import session_bounds_utc
from deskbrief.config import IexVsSipSettings
from deskbrief.crosschecks.base import (
    Obs,
    Side,
    group_id_for,
    query_obs,
    reference_date,
    register,
    rel_bps,
)
from deskbrief.models import DQStatus, RunContext, XResult

NAME = "iex_vs_sip"


class IexVsSip:
    name = NAME

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]:
        cfg = ctx.config
        tol = cfg.crosschecks.iex_vs_sip
        ref = reference_date(ctx, "XNYS")
        bounds = session_bounds_utc(ref)
        assert bounds is not None  # ref is an XNYS business day
        open_utc, close_utc = bounds
        n_session = int((close_utc - open_utc).total_seconds() // 60)
        bars: dict[str, list[Obs]] = {
            dataset: query_obs(
                ctx,
                duck,
                source="alpaca",
                dataset=dataset,
                interval="1m",
                start=open_utc,
                end=close_utc,
                fields=("close",),
            )
            for dataset in ("iex_bars_1m", "sip_bars_1m")
        }
        out: list[XResult] = []
        for sym in tol.symbols:
            iex = {o.as_of: o for o in bars["iex_bars_1m"] if o.instrument_id == sym}
            sip = {o.as_of: o for o in bars["sip_bars_1m"] if o.instrument_id == sym}
            both = sorted(iex.keys() & sip.keys())
            usable = [m for m in both if sip[m].value != 0]
            gaps = [round(abs(rel_bps(iex[m].value, sip[m].value)), 9) for m in usable]
            details: dict[str, Any] = {
                "ref_date": ref.isoformat(),
                "left": _side(sym, "iex_bars_1m", cfg.instrument(sym).unit).detail(),
                "right": _side(sym, "sip_bars_1m", cfg.instrument(sym).unit).detail(),
                "basis": "relative",
                "session_open_utc": open_utc.isoformat(),
                "session_close_utc": close_utc.isoformat(),
                "n_session_minutes": n_session,
                "n_iex_bars": len(iex),
                "n_sip_bars": len(sip),
                "n_compared": len(usable),
                "n_zero_reference": len(both) - len(usable),
                "n_missing_sip": len(iex.keys() - sip.keys()),
                "n_missing_iex": len(sip.keys() - iex.keys()),
                "n_missing_both": n_session - len(iex.keys() | sip.keys()),
                "max_fail_minutes_pct": tol.max_fail_minutes_pct,
            }
            diff: float | None = None
            status = DQStatus.SKIP
            if gaps:
                n = len(gaps)
                over_warn = 100.0 * sum(g > tol.warn_bps for g in gaps) / n
                over_fail = 100.0 * sum(g > tol.fail_bps for g in gaps) / n
                diff = statistics.median(gaps)
                details.update(
                    median_abs_gap_bps=diff,
                    p95_abs_gap_bps=sorted(gaps)[math.ceil(0.95 * n) - 1],
                    pct_minutes_over_warn=over_warn,
                    pct_minutes_over_fail=over_fail,
                )
                if over_fail > tol.max_fail_minutes_pct:
                    status = DQStatus.FAIL
                elif over_warn > tol.max_fail_minutes_pct:
                    status = DQStatus.WARN
                else:
                    status = DQStatus.PASS
            else:
                details["reason"] = "no minute with both an IEX and a SIP bar"
            out.append(_result(ctx, sym, close_utc, tol, diff, status, details))
        return out


def _side(inst: str, dataset: str, unit: str) -> Side:
    return Side(inst, "alpaca", dataset, unit, "close")


def _result(
    ctx: RunContext,
    sym: str,
    at_time: datetime,
    tol: IexVsSipSettings,
    diff: float | None,
    status: DQStatus,
    details: dict[str, Any],
) -> XResult:
    return XResult(
        run_id=ctx.run_id,
        check_name=NAME,
        group_id=group_id_for(ctx.config, sym),
        target_date=ctx.target_date,
        at_time=at_time,
        relation="same",
        left_instrument=sym,
        left_source="alpaca",
        left_value=None,
        right_instrument=sym,
        right_source="alpaca",
        right_value=None,
        expected_value=None,
        diff_bps=diff,
        warn_bps=tol.warn_bps,
        fail_bps=tol.fail_bps,
        status=status,
        details=details,
    )


register(IexVsSip())
