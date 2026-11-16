"""``official_pair``: Treasury par curve vs FRED ``DGS*`` for the same reference date (spec §8.2).

FRED republishes the Treasury curve, so the expected difference is ~0 bp. The check therefore
measures **publication lag and revisions**, not accuracy: ``details.publication_lag_s`` is
``FRED ingested_at - Treasury ingested_at`` for the two rows used (positive = FRED later).
When FRED has not published the date yet the result is ``skip`` (``missing side``) and the
Treasury row's age is recorded as ``details.pending_age_s``.

Compared on the last completed SIFMA business day before today (P-009), reading ``obs_pit``.
Both sides are yields in ``pct``, so the difference is an absolute level in bp
(``1 bp = 0.01`` percentage point). Left = Treasury, right = FRED.
"""

from __future__ import annotations

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
from deskbrief.timeutil import et_to_utc

NAME = "official_pair"
FIELDS = ("yield",)


class OfficialPair:
    name = NAME

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]:
        cfg = ctx.config
        tol = tolerance_for(cfg, NAME)
        ref = reference_date(ctx, "SIFMA_US")
        start, end = et_day_bounds(ref)
        at_time = et_to_utc(ref, "00:00")
        treasury = query_obs(
            ctx, duck, source="treasury", dataset="par_curve", interval="1d",
            start=start, end=end, fields=FIELDS,
        )  # fmt: skip
        fred = query_obs(
            ctx, duck, source="fred", dataset="series", interval="1d",
            start=start, end=end, fields=FIELDS,
        )  # fmt: skip
        tsy_index = cfg.feed_index("treasury", "par_curve")
        fred_index = cfg.feed_index("fred", "series")

        def find(rows: list[Obs], instrument_id: str, symbol: str) -> Obs | None:
            return pick_by_field(
                (r for r in rows if r.instrument_id == instrument_id and r.symbol == symbol),
                FIELDS,
            )

        out: list[XResult] = []
        for fred_id, tsy_col in cfg.crosschecks.official_pair.pairs:
            t_inst, _ = tsy_index[tsy_col]
            f_inst, _ = fred_index[fred_id]
            t_obs = find(treasury, t_inst.id, tsy_col)
            f_obs = find(fred, f_inst.id, fred_id)
            left = (
                Side.from_obs(t_obs)
                if t_obs
                else Side(t_inst.id, "treasury", "par_curve", t_inst.unit)
            )
            right = (
                Side.from_obs(f_obs) if f_obs else Side(f_inst.id, "fred", "series", f_inst.unit)
            )
            extra: dict[str, object] = {
                "treasury_column": tsy_col,
                "fred_series": fred_id,
                "expected": "difference ~0 bp: FRED republishes Treasury; this measures lag",
                "publication_lag_s": None,
                "pending_age_s": None,
            }
            if t_obs and f_obs:
                extra["publication_lag_s"] = (f_obs.ingested_at - t_obs.ingested_at).total_seconds()
            elif t_obs:
                extra["pending_age_s"] = (ctx.now_utc - t_obs.ingested_at).total_seconds()
            out.append(
                compare(
                    ctx,
                    check_name=NAME,
                    relation="level",
                    group_id=group_id_for(cfg, t_inst.id, f_inst.id),
                    at_time=at_time,
                    left=left,
                    right=right,
                    tol=tol,
                    ref_date=ref,
                    extra=extra,
                )
            )
        return out


register(OfficialPair())
