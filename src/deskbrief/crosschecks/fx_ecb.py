"""``fx_vs_ecb``: FX at the ECB fix vs the ECB reference rate (spec §8.2).

For each ``crosschecks.fx_vs_ecb.pairs`` entry two rows are emitted on the last completed TARGET
business day before today (P-009, reading ``obs_pit``), both with the ECB side as the reference:

* ``twelvedata``: the ``fx_series_1m`` ``close`` of the bar *starting* at the fix
  (``fix_time_cet`` in Europe/Berlin, DST-aware: 14:15 CET is 13:15Z in winter, 12:15Z in
  summer). Twelve Data emits ``close`` (never ``mid``); bars exist only around the fix (T15).
* ``yahoo``: the ``quote`` ``last`` tick nearest to the fix within +-1 minute (T24B runs a Yahoo
  intraday at the fix, P-012).

The ECB side is labelled "ECB reference rate" (``field=rate``, never a close, P-009) in
``details.right_label``; ``details.right_is_derived`` is true for the GBPUSD/USDJPY crosses
via EUR. FX differences are relative to the ECB rate in bp. A missing side is a ``skip``.
"""

from __future__ import annotations

from datetime import timedelta

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
TICK_TOLERANCE = timedelta(minutes=1)


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
            start=fix - TICK_TOLERANCE, end=fix + TICK_TOLERANCE + timedelta(seconds=1),
            fields=("last",),
        )  # fmt: skip
        out: list[XResult] = []
        for pair in s.pairs:
            unit = cfg.instrument(pair).unit
            e_obs = pick_by_field((o for o in ecb if o.instrument_id == pair), ("rate",))
            right = Side.from_obs(e_obs) if e_obs else Side(pair, "ecb", "eurofxref", unit)
            mine_bars = [o for o in bars if o.instrument_id == pair]
            mine_ticks = [o for o in ticks if o.instrument_id == pair]
            at_fix = [o for o in mine_bars if o.as_of == fix]
            near = min(mine_ticks, key=lambda o: abs(o.as_of - fix), default=None)
            for source, dataset, found, n_key, n in (
                ("twelvedata", "fx_series_1m", at_fix[0] if at_fix else None,
                 "n_twelvedata_bars", len(mine_bars)),
                ("yahoo", "quote", near, "n_yahoo_ticks", len(mine_ticks)),
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
                        extra={
                            "right_label": ECB_LABEL,
                            "right_is_derived": _is_derived(ctx, duck, e_obs),
                            "fix_time_cet": s.fix_time_cet,
                            "fix_utc": fix.isoformat(),
                            n_key: n,
                        },
                    )
                )
        return out


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
