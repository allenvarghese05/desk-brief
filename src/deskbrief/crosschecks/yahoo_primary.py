"""``yahoo_vs_primary``: Yahoo last vs the primary feed at the same minute (spec §8.2).

* Equities (instruments with ``yahoo/quote`` and ``alpaca/sip_bars_1m`` feeds): the latest
  Yahoo ``last`` tick (interval ``tick``) of the reference session vs the Alpaca SIP 1m bar
  ``close``. The bar must start in the tick's minute ±1 (offsets tried in order 0, -1, +1).
* FX (instruments with ``yahoo/quote`` and ``twelvedata/fx_series_1m`` feeds): Yahoo ``last`` vs
  the Twelve Data 1m bar (``mid`` preferred, else ``close``), same ±1 minute rule.

Reference date (P-009): last completed XNYS session for equities, last completed weekday for FX;
the current ET date is excluded and ``obs_pit`` is read. Left = Yahoo, right = primary. Prices
and FX use relative bp. Neither Alpaca nor Twelve Data is guaranteed to have data (their source
cards may not have merged, budgets run out): a missing side is a ``skip`` with the reason.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from deskbrief.config import CalendarName, Instrument
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

NAME = "yahoo_vs_primary"
MINUTE_OFFSETS = (0, -1, 1)
FX_FIELDS = ("mid", "close")


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


class YahooVsPrimary:
    name = NAME

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]:
        cfg = ctx.config
        out: list[XResult] = []
        groups: tuple[tuple[str, str, str, tuple[str, ...], CalendarName | None], ...] = (
            ("equity", "alpaca", "sip_bars_1m", ("close",), "XNYS"),
            ("fx", "twelvedata", "fx_series_1m", FX_FIELDS, None),
        )
        for label, p_source, p_dataset, p_fields, cal in groups:
            ref = reference_date(ctx, cal)
            start, end = et_day_bounds(ref)
            yahoo_ids = {i.id for i, _ in cfg.feeds_for("yahoo", "quote")}
            primary_ids = {i.id for i, _ in cfg.feeds_for(p_source, p_dataset)}
            members = [
                i
                for i in cfg.instruments
                if i.id in yahoo_ids and i.id in primary_ids and (i.kind == "fx") == (label == "fx")
            ]
            if not members:
                continue
            ticks = query_obs(
                ctx, duck, source="yahoo", dataset="quote", interval="tick",
                start=start, end=end, fields=("last",),
            )  # fmt: skip
            bars = query_obs(
                ctx, duck, source=p_source, dataset=p_dataset, interval="1m",
                start=start - timedelta(minutes=1), end=end + timedelta(minutes=1),
                fields=p_fields,
            )  # fmt: skip
            for inst in members:
                out.append(
                    self._one(ctx, inst, label, p_source, p_dataset, p_fields, ticks, bars, ref)
                )
        return out

    def _one(
        self,
        ctx: RunContext,
        inst: Instrument,
        label: str,
        p_source: str,
        p_dataset: str,
        p_fields: tuple[str, ...],
        ticks: list[Obs],
        bars: list[Obs],
        ref: date,
    ) -> XResult:
        tol = tolerance_for(ctx.config, NAME)
        mine = [t for t in ticks if t.instrument_id == inst.id]
        y_obs = max(mine, key=lambda t: t.as_of) if mine else None
        left = Side.from_obs(y_obs) if y_obs else Side(inst.id, "yahoo", "quote", inst.unit)
        right = Side(inst.id, p_source, p_dataset, inst.unit)
        at_time = y_obs.as_of if y_obs else ctx.now_utc
        extra: dict[str, object] = {"kind": label, "minute_offset": None}
        skip: str | None = None
        if y_obs is None:
            skip = f"missing side: no yahoo quote tick on {ref}"
        else:
            by_start: dict[datetime, list[Obs]] = {}
            for b in bars:
                if b.instrument_id == inst.id:
                    by_start.setdefault(b.as_of, []).append(b)
            m = _minute(y_obs.as_of)
            for off in MINUTE_OFFSETS:
                cand = by_start.get(m + timedelta(minutes=off))
                if cand:
                    best = pick_by_field(cand, p_fields)
                    assert best is not None
                    right = Side.from_obs(best)
                    extra["minute_offset"] = off
                    break
            else:
                skip = (
                    f"missing side: no {p_source}/{p_dataset} bar within 1 minute of "
                    f"{m.isoformat()}"
                )
        return compare(
            ctx,
            check_name=NAME,
            relation="level" if label == "fx" else "same",
            group_id=group_id_for(ctx.config, inst.id),
            at_time=at_time,
            left=left,
            right=right,
            tol=tol,
            ref_date=ref,
            extra=extra,
            skip_reason=skip,
        )


register(YahooVsPrimary())
