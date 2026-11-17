"""``proxy_vs_exact``: proxy ETF daily return vs the exact instrument or yield move (spec §8.2).

One row per configured group that has a ``proxy`` and relation ``return`` or ``inverse_duration``
(``same``/``level`` groups are other checks). Evaluated for the last completed XNYS session
before today (P-009, reading ``obs_pit``) against the session before it; both days of a series
must come from one ``(source, dataset)``, preferring the higher tier, so sources are never mixed.

* ``return`` (SPY vs ES_F, USO vs WTI, BNO vs BRENT, GLD vs GOLD_F): left = proxy daily return,
  right = exact daily return (decimal, ``close/prev - 1``); ``diff_bps`` is the difference of the
  returns (1 bp = 0.01% return). The status is the group tolerance, except that **opposite
  directions with both moves above 25 bp is a fail**. Futures roll windows (:func:`in_roll_window`)
  cap a fail at ``warn``.
* ``inverse_duration`` (SHY/IEF/TLT vs a yield): expected ETF return = ``-D * dy`` with ``dy`` the
  change of the ``pct`` yield in decimal (TLT, D = 16.5, +10 bp -> -1.65%). left = actual ETF
  return, ``expected_value`` = expected return, right = the current yield. The yield comes from
  the group's ``official`` instrument, else its ``exact`` one (e.g. FRED ``DGS10``, else ``^TNX``).

Skips (missing day or side, zero previous value) carry a ``reason``. ``in_roll_window`` is an
APPROXIMATION of the exchange calendars (VERIFY against CME/ICE): it only softens a fail to a warn.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import duckdb

from deskbrief.calendars import add_business_days, is_business_day, prev_business_day
from deskbrief.config import Group
from deskbrief.crosschecks.base import (
    Obs,
    Side,
    et_day_bounds,
    pick_by_field,
    query_obs,
    reference_date,
    register,
    return_diff_bps,
    simple_return,
    status_for,
)
from deskbrief.models import DQStatus, RunContext, XResult
from deskbrief.timeutil import et_date, et_to_utc

NAME = "proxy_vs_exact"
DIRECTION_MIN_BPS = 25.0  # both moves must exceed this for a direction disagreement to fail

CLOSE = ("close",)
YIELD = ("yield", "close")
Pair = tuple[Obs, Obs]  # (previous day, reference day)


def in_roll_window(instrument_id: str, d: date) -> bool:
    """True if ``d`` is in the approximate futures roll window of ``instrument_id``.

    ES_F: 8 days before through 1 day after the third Friday of Mar/Jun/Sep/Dec.
    WTI: 10 sessions before through 1 session after the last trade day (3 sessions before the
    25th, or before the session preceding it when the 25th is closed). BRENT: the last 3
    sessions of a month and the first of the next. Other instruments have no rule.
    """
    if instrument_id == "ES_F":
        if d.month not in (3, 6, 9, 12):
            return False
        third_friday = date(d.year, d.month, 15 + (4 - date(d.year, d.month, 15).weekday()) % 7)
        return third_friday - timedelta(days=8) <= d <= third_friday + timedelta(days=1)
    if instrument_id == "WTI":
        the_25th = date(d.year, d.month, 25)
        anchor = (
            the_25th if is_business_day("XNYS", the_25th) else prev_business_day("XNYS", the_25th)
        )
        expiry = add_business_days("XNYS", anchor, -3)
        return add_business_days("XNYS", expiry, -10) <= d <= add_business_days("XNYS", expiry, 1)
    if instrument_id == "BRENT":
        last = date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)
        while not is_business_day("XNYS", last):
            last -= timedelta(days=1)
        first_of_month = (
            is_business_day("XNYS", d) and prev_business_day("XNYS", d).month != d.month
        )
        return add_business_days("XNYS", last, -2) <= d <= last or first_of_month
    return False


class ProxyVsExact:
    name = NAME

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]:
        ref = reference_date(ctx, "XNYS")
        prev = prev_business_day("XNYS", ref)
        out: list[XResult] = []
        for g in ctx.config.groups:
            if g.proxy is None:
                continue
            if g.relation == "return":
                out.append(self._return(ctx, duck, g, prev, ref))
            elif g.relation == "inverse_duration":
                out.append(self._inverse(ctx, duck, g, prev, ref))
        return out

    # -- return -----------------------------------------------------------------------------

    def _return(
        self, ctx: RunContext, duck: duckdb.DuckDBPyConnection, g: Group, prev: date, ref: date
    ) -> XResult:
        assert g.proxy is not None
        exact_id = g.exact or g.official
        assert exact_id is not None  # Group validator: a return group has two instruments
        p_pair = _daily(ctx, duck, g.proxy, CLOSE, prev, ref)
        e_pair = _daily(ctx, duck, exact_id, CLOSE, prev, ref)
        details = _base(prev, ref, "daily_return_diff")
        details["roll_window"] = in_roll_window(exact_id, ref)
        details["capped_at_warn"] = False
        left_src, right_src = _src(p_pair), _src(e_pair)
        ret_p = ret_e = diff = None
        status = DQStatus.SKIP
        reason = _missing([(g.proxy, "close", p_pair), (exact_id, "close", e_pair)])
        if p_pair:
            ret_p = _ret(p_pair, details, "left")
        if e_pair:
            ret_e = _ret(e_pair, details, "right")
        if reason is None and ret_p is not None and ret_e is not None:
            diff = return_diff_bps(ret_p, ret_e)
            status = status_for(diff, g.tolerance.warn_bps, g.tolerance.fail_bps)
            p_bps, e_bps = round(ret_p * 1e4, 9), round(ret_e * 1e4, 9)
            opposite = p_bps * e_bps < 0 and min(abs(p_bps), abs(e_bps)) > DIRECTION_MIN_BPS
            details["direction_disagreement"] = opposite
            if opposite:
                status = DQStatus.FAIL
            if status is DQStatus.FAIL and details["roll_window"]:
                status, details["capped_at_warn"] = DQStatus.WARN, True
        elif reason is None:
            reason = "previous value is zero"
        return _result(
            ctx, g, "return", (g.proxy, left_src, ret_p), (exact_id, right_src, ret_e), ret_e,
            diff, status, ref, details, reason,
        )  # fmt: skip

    # -- inverse duration -------------------------------------------------------------------

    def _inverse(
        self, ctx: RunContext, duck: duckdb.DuckDBPyConnection, g: Group, prev: date, ref: date
    ) -> XResult:
        assert g.proxy is not None
        assert g.duration is not None
        p_pair = _daily(ctx, duck, g.proxy, CLOSE, prev, ref)
        candidates = list(dict.fromkeys(i for i in (g.official, g.exact) if i))
        y_id, y_pair = candidates[0], None
        for cand in candidates:
            y_pair = _daily(ctx, duck, cand, YIELD, prev, ref)
            if y_pair:
                y_id = cand
                break
        details = _base(prev, ref, "daily_return_diff")
        details["duration"] = g.duration
        reason = _missing([(g.proxy, "close", p_pair), (y_id, "yield", y_pair)])
        ret_p: float | None = None
        expected: float | None = None
        diff: float | None = None
        status = DQStatus.SKIP
        if p_pair:
            ret_p = _ret(p_pair, details, "left")
        if y_pair:
            d_yield = y_pair[1].value - y_pair[0].value  # percentage points
            expected = -g.duration * d_yield / 100.0
            details["right"] = _leg(y_pair, None)
            details["delta_yield_bp"] = d_yield * 100.0
            details["expected_return"] = expected
        if reason is None and ret_p is not None and expected is not None:
            diff = return_diff_bps(ret_p, expected)
            status = status_for(diff, g.tolerance.warn_bps, g.tolerance.fail_bps)
        elif reason is None:
            reason = "previous value is zero"
        return _result(
            ctx, g, "inverse_duration", (g.proxy, _src(p_pair), ret_p),
            (y_id, _src(y_pair), y_pair[1].value if y_pair else None), expected,
            diff, status, ref, details, reason,
        )  # fmt: skip


# -- helpers --------------------------------------------------------------------------------


def _daily(
    ctx: RunContext,
    duck: duckdb.DuckDBPyConnection,
    inst_id: str,
    fields: tuple[str, ...],
    prev: date,
    ref: date,
) -> Pair | None:
    """Both days' value (first field of ``fields`` present) from one ``(source, dataset)``.

    Highest-tier feed first. A yield is ``yield`` at Treasury/FRED but ``close`` (unit ``pct``)
    at Yahoo (``^TNX``), so yields pass both fields.
    """
    start, end = et_day_bounds(prev)[0], et_day_bounds(ref)[1]
    seen: set[tuple[str, str]] = set()
    for feed in sorted(ctx.config.instrument(inst_id).feeds, key=lambda f: -f.tier):
        key = (feed.source, feed.dataset)
        if key in seen:
            continue
        seen.add(key)
        rows = query_obs(
            ctx, duck, source=feed.source, dataset=feed.dataset, interval="1d",
            start=start, end=end, fields=fields,
        )  # fmt: skip
        picked = [
            pick_by_field(
                (o for o in rows if o.instrument_id == inst_id and et_date(o.as_of) == d), fields
            )
            for d in (prev, ref)
        ]
        if picked[0] and picked[1]:
            return picked[0], picked[1]
    return None


def _src(pair: Pair | None) -> str:
    return pair[1].source if pair else "*"


def _base(prev: date, ref: date, basis: str) -> dict[str, Any]:
    return {"ref_date": ref.isoformat(), "prev_date": prev.isoformat(), "basis": basis}


def _leg(pair: Pair, ret: float | None) -> dict[str, Any]:
    prev, cur = pair
    return {
        **Side.from_obs(cur).detail(),
        "prev_as_of": prev.as_of.isoformat(),
        "prev_value": prev.value,
        "value": cur.value,
        "return": ret,
    }


def _ret(pair: Pair, details: dict[str, Any], side: str) -> float | None:
    try:
        ret = simple_return(pair[0].value, pair[1].value)
    except ZeroDivisionError:
        ret = None
    details[side] = _leg(pair, ret)
    return ret


def _missing(legs: list[tuple[str, str, Pair | None]]) -> str | None:
    gone = [f"{i}/{f}" for i, f, pair in legs if pair is None]
    if not gone:
        return None
    return "missing side (need both sessions from one source): " + ", ".join(gone)


def _result(
    ctx: RunContext,
    g: Group,
    relation: str,
    left: tuple[str, str, float | None],
    right: tuple[str, str, float | None],
    expected: float | None,
    diff: float | None,
    status: DQStatus,
    ref: date,
    details: dict[str, Any],
    reason: str | None,
) -> XResult:
    if reason is not None:
        details["reason"] = reason
        diff, status = None, DQStatus.SKIP
    at_time: datetime = et_to_utc(ref, "00:00")
    return XResult(
        run_id=ctx.run_id,
        check_name=NAME,
        group_id=g.id,
        target_date=ctx.target_date,
        at_time=at_time,
        relation=relation,
        left_instrument=left[0],
        left_source=left[1],
        left_value=left[2],
        right_instrument=right[0],
        right_source=right[1],
        right_value=right[2],
        expected_value=expected,
        diff_bps=diff,
        warn_bps=g.tolerance.warn_bps,
        fail_bps=g.tolerance.fail_bps,
        status=status,
        details=details,
    )


register(ProxyVsExact())
