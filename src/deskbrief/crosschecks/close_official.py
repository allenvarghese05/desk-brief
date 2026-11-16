"""``close_vs_official``: Alpaca SIP last 1m bar before the close vs the Stooq daily close.

Members: instruments in ``crosschecks.iex_vs_sip.symbols`` that have both an
``alpaca/sip_bars_1m`` and a ``stooq/daily`` feed (the Alpaca source, T14, fetches SIP bars only
for those symbols, so the other equities would skip every day). Evaluated on the last
completed XNYS session before today (P-009; reading ``obs_pit``):

* left  = SIP ``close`` of the 1m bar *ending* at the session close, i.e. the bar starting one
  minute before ``session_bounds_utc(date)[1]`` (16:00 ET, or 13:00 ET on early-close days),
* right = Stooq ``close`` for the date (the official reference).

The closing auction prints after the last 1m bar, so small gaps are expected (spec §8.2). Prices
use relative bp. A missing bar (no trade in the last minute) is a ``skip``, not a fallback.
"""

from __future__ import annotations

from datetime import timedelta

import duckdb

from deskbrief.calendars import is_early_close, session_bounds_utc
from deskbrief.crosschecks.base import (
    Side,
    compare,
    et_day_bounds,
    group_id_for,
    pick_by_field,
    query_obs,
    reference_date,
    register,
    tolerance_for,
)
from deskbrief.models import RunContext, XResult

NAME = "close_vs_official"


class CloseVsOfficial:
    name = NAME

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]:
        cfg = ctx.config
        tol = tolerance_for(cfg, NAME)
        ref = reference_date(ctx, "XNYS")
        bounds = session_bounds_utc(ref)
        assert bounds is not None  # ref is an XNYS business day
        close_utc = bounds[1]
        bar_start = close_utc - timedelta(minutes=1)
        sip = query_obs(
            ctx, duck, source="alpaca", dataset="sip_bars_1m", interval="1m",
            start=bar_start, end=close_utc, fields=("close",),
        )  # fmt: skip
        d_start, d_end = et_day_bounds(ref)
        stooq = query_obs(
            ctx, duck, source="stooq", dataset="daily", interval="1d",
            start=d_start, end=d_end, fields=("close",),
        )  # fmt: skip
        wanted = set(cfg.crosschecks.iex_vs_sip.symbols)
        sip_ids = {i.id for i, _ in cfg.feeds_for("alpaca", "sip_bars_1m")}
        stooq_ids = {i.id for i, _ in cfg.feeds_for("stooq", "daily")}
        early = is_early_close(ref)
        out: list[XResult] = []
        for inst in cfg.instruments:
            if inst.id not in wanted or inst.id not in sip_ids or inst.id not in stooq_ids:
                continue
            s_obs = pick_by_field((o for o in sip if o.instrument_id == inst.id), ("close",))
            o_obs = pick_by_field((o for o in stooq if o.instrument_id == inst.id), ("close",))
            left = (
                Side.from_obs(s_obs) if s_obs else Side(inst.id, "alpaca", "sip_bars_1m", inst.unit)
            )
            right = Side.from_obs(o_obs) if o_obs else Side(inst.id, "stooq", "daily", inst.unit)
            out.append(
                compare(
                    ctx,
                    check_name=NAME,
                    relation="same",
                    group_id=group_id_for(cfg, inst.id),
                    at_time=close_utc,
                    left=left,
                    right=right,
                    tol=tol,
                    ref_date=ref,
                    extra={
                        "session_close_utc": close_utc.isoformat(),
                        "expected_bar_start_utc": bar_start.isoformat(),
                        "early_close": early,
                        "expected": "small gaps: closing auction vs last 1m bar",
                    },
                )
            )
        return out


register(CloseVsOfficial())
