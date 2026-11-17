"""``fx_vs_ecb``: FX at the ECB fix vs the ECB reference rate (spec §8.2).

For each ``crosschecks.fx_vs_ecb.pairs`` entry two rows are emitted on the last completed TARGET
business day before today (P-009, reading ``obs_pit``), both with the ECB side as the reference:

* ``twelvedata``: the ``fx_series_1m`` ``close`` of the bar *ending* at the fix
  (``fix_time_cet`` in Europe/Berlin, DST-aware: 14:15 CET is 13:15Z in winter, 12:15Z in
  summer). ``as_of`` is the bar start (P-006), so that bar starts one minute earlier (14:14 CET).
  That bar-start convention is unverified until the Twelve Data live probe; if ``datetime``
  turns out to be the bar end, ``BAR_LENGTH`` becomes 0 and the bar at the fix itself is used;
  the bar starting at 14:15 closes at about 14:16, after the fix, so its close is a price from
  after the ECB reference rate was set. Twelve Data emits ``close`` (never ``mid``); bars exist
  only around the fix (T15, fix +-10 min, which covers the 14:14 bar). The row's ``at_time`` is
  still the fix; the bar actually used is in ``details.left.as_of``.
* ``yahoo``: the ``quote`` ``last`` tick nearest to the fix within +-1 minute, inclusive (T24B
  runs a Yahoo intraday at the fix, P-012). Ticks are read over the same +-10 minute window as
  the bars so a skip can say whether there were no ticks at all or none close enough.

The ECB side is labelled "ECB reference rate" (``field=rate``, never a close, P-009) in
``details.right_label``; ``details.right_is_derived`` is true for the GBPUSD/USDJPY crosses
via EUR. FX differences are relative to the ECB rate in bp. A missing side is a ``skip``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from deskbrief.crosschecks.base import (
    Obs,
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
from deskbrief.timeutil import CET, local_to_utc

NAME = "fx_vs_ecb"
ECB_LABEL = "ECB reference rate"
FIX_WINDOW = timedelta(minutes=10)  # the window T15 fetches around the fix
TICK_TOLERANCE = timedelta(minutes=1)  # inclusive: ticks at exactly +-60 s count, +-61 s do not
# the 1m bar ending at the fix starts BAR_LENGTH before it. Bar-start convention unverified until
# the Twelve Data live probe: BAR_LENGTH becomes 0 if `datetime` turns out to be the bar end.
BAR_LENGTH = timedelta(minutes=1)


class FxVsEcb:
    name = NAME

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]:
        cfg = ctx.config
        s = cfg.crosschecks.fx_vs_ecb
        tol = tolerance_for(cfg, NAME)
        ref = reference_date(ctx, "TARGET")
        fix = local_to_utc(ref, s.fix_time_cet, CET)
        day_start, day_end = et_day_bounds(ref)
        ecb = query_obs(
            ctx, duck, source="ecb", dataset="eurofxref", interval="1d",
            start=day_start, end=day_end, fields=("rate",),
        )  # fmt: skip
        bars = query_obs(
            ctx, duck, source="twelvedata", dataset="fx_series_1m", interval="1m",
            start=fix - FIX_WINDOW, end=fix + FIX_WINDOW + timedelta(minutes=1),
            fields=("close",),
        )  # fmt: skip
        ticks = query_obs(
            ctx, duck, source="yahoo", dataset="quote", interval="tick",
            start=fix - FIX_WINDOW, end=fix + FIX_WINDOW + timedelta(seconds=1),
            fields=("last",),
        )  # fmt: skip
        out: list[XResult] = []
        for pair in s.pairs:
            unit = cfg.instrument(pair).unit
            e_obs = pick_by_field((o for o in ecb if o.instrument_id == pair), ("rate",))
            right = Side.from_obs(e_obs) if e_obs else Side(pair, "ecb", "eurofxref", unit)
            mine_bars = [o for o in bars if o.instrument_id == pair]
            mine_ticks = [o for o in ticks if o.instrument_id == pair]
            bar_start = fix - BAR_LENGTH
            at_fix = [o for o in mine_bars if o.as_of == bar_start]
            close_ticks = [o for o in mine_ticks if abs(o.as_of - fix) <= TICK_TOLERANCE]
            near = min(close_ticks, key=lambda o: abs(o.as_of - fix), default=None)
            yahoo_skip = _yahoo_skip_reason(pair, fix, mine_ticks) if near is None else None
            derived = _is_derived(ctx, duck, e_obs)  # once per pair, not once per source
            # n_yahoo_ticks counts ticks in the +-10 minute window around the fix, unlike
            # `yahoo_vs_primary`, where it counts the ET day's ticks.
            for source, dataset, found, n_key, n, skip in (
                ("twelvedata", "fx_series_1m", at_fix[0] if at_fix else None,
                 "n_twelvedata_bars", len(mine_bars), None),
                ("yahoo", "quote", near, "n_yahoo_ticks", len(mine_ticks), yahoo_skip),
            ):  # fmt: skip
                left = Side.from_obs(found) if found else Side(pair, source, dataset, unit)
                out.append(
                    compare(
                        ctx,
                        check_name=NAME,
                        relation="level",
                        group_id=group_id_for(cfg, pair),
                        at_time=fix,
                        left=left,
                        right=right,
                        tol=tol,
                        ref_date=ref,
                        skip_reason=skip,
                        extra={
                            "right_label": ECB_LABEL,
                            "right_is_derived": derived,
                            "fix_time_cet": s.fix_time_cet,
                            "fix_utc": fix.isoformat(),
                            n_key: n,
                        },
                    )
                )
        return out


def _yahoo_skip_reason(pair: str, fix: datetime, ticks: list[Obs]) -> str:
    """Why no Yahoo tick qualifies: none at all, or none within the tolerance of the fix."""
    if not ticks:
        return f"no Yahoo ticks around the fix ({pair})"
    gap = min(abs((o.as_of - fix).total_seconds()) for o in ticks)
    tol_s = TICK_TOLERANCE.total_seconds()
    return f"no Yahoo tick within +-{tol_s:g} s of the fix ({pair}; nearest {gap:.0f} s away)"


def _is_derived(ctx: RunContext, duck: duckdb.DuckDBPyConnection, o: Obs | None) -> bool | None:
    """``is_derived`` of the ECB row used (``None`` when there is no ECB row)."""
    if o is None:
        return None
    row = duck.execute(
        "SELECT is_derived FROM obs_pit(?::TIMESTAMPTZ) "
        "WHERE source = 'ecb' AND dataset = 'eurofxref' AND instrument_id = ? "
        "AND field = 'rate' AND interval = '1d' AND as_of = ?::TIMESTAMPTZ",
        [ctx.now_utc, o.instrument_id, o.as_of],
    ).fetchone()
    return bool(row[0]) if row else None


register(FxVsEcb())
