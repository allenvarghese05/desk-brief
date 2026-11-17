"""``yahoo_vs_primary``: Yahoo last vs the primary feed at the same minute (spec §8.2).

**Equities.** Members are the instruments in ``crosschecks.iex_vs_sip.symbols`` that also have
``yahoo/quote`` and ``alpaca/sip_bars_1m`` feeds: the Alpaca source (T14) fetches SIP bars only
for those symbols, so other equities (AAPL, MSFT, NVDA, USO, BNO, SHY, IEF) would skip every day.
Left = the *last* Yahoo ``last`` tick (interval ``tick``) inside the XNYS regular session of the
reference date, ``open <= as_of < close`` (early closes honoured). A tick stamped exactly at the
close is excluded (it would pair with the last 15:59 bar and cause a spurious warn); pre-market,
after-hours and closing-auction prints never pair with a regular-session bar. When only
pre-market ticks exist the skip reason says so. Right = the SIP 1m bar ``close`` starting in the
tick's minute ±1 (offsets tried in order 0, -1, +1).

**FX.** Members are fx instruments with ``yahoo/quote`` and ``twelvedata/fx_series_1m`` feeds.
Twelve Data bars exist only in a window around the ECB fix (14:15 CET ±10 min, T15), so the
comparison is anchored on the Twelve Data bars: the Yahoo tick of the reference date closest to
any Twelve Data bar, within ±1 minute (bar start vs the tick's minute), is paired with that bar.
No such tick gives a specific ``skip`` ("no Yahoo tick within ±1m of the ECB-fix bars").
Twelve Data emits field ``close`` for its bars (never ``mid``).

Reference date (P-009): last completed XNYS session for equities, last completed weekday for FX;
the current ET date is excluded and ``obs_pit`` is read. Left = Yahoo, right = primary. Prices
and FX use relative bp. A missing side (source not yet merged, budget exhausted) is a ``skip``
with the reason.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from deskbrief.calendars import session_bounds_utc
from deskbrief.config import Instrument
from deskbrief.crosschecks.base import (
    Obs,
    Side,
    compare,
    et_day_bounds,
    group_id_for,
    query_obs,
    reference_date,
    register,
    tolerance_for,
)
from deskbrief.models import RunContext, XResult

NAME = "yahoo_vs_primary"
MINUTE_OFFSETS = (0, -1, 1)
FX_FIELDS = ("close",)  # Twelve Data emits close for fx_series_1m (T15); it never emits mid


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def equity_members(ctx: RunContext) -> list[Instrument]:
    """Instruments with yahoo quote + SIP bar feeds that are in ``iex_vs_sip.symbols``."""
    cfg = ctx.config
    yahoo_ids = {i.id for i, _ in cfg.feeds_for("yahoo", "quote")}
    sip_ids = {i.id for i, _ in cfg.feeds_for("alpaca", "sip_bars_1m")}
    wanted = set(cfg.crosschecks.iex_vs_sip.symbols)
    return [i for i in cfg.instruments if i.id in wanted and i.id in yahoo_ids and i.id in sip_ids]


class YahooVsPrimary:
    name = NAME

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]:
        return [*self._equities(ctx, duck), *self._fx(ctx, duck)]

    # -- equities ---------------------------------------------------------------------------

    def _equities(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]:
        members = equity_members(ctx)
        if not members:
            return []
        ref = reference_date(ctx, "XNYS")
        bounds = session_bounds_utc(ref)
        assert bounds is not None  # ref is an XNYS business day
        open_utc, close_utc = bounds
        start, end = et_day_bounds(ref)
        ticks = query_obs(
            ctx, duck, source="yahoo", dataset="quote", interval="tick",
            start=start, end=end, fields=("last",),
        )  # fmt: skip
        bars = query_obs(
            ctx, duck, source="alpaca", dataset="sip_bars_1m", interval="1m",
            start=start, end=end, fields=("close",),
        )  # fmt: skip
        out: list[XResult] = []
        for inst in members:
            mine = [t for t in ticks if t.instrument_id == inst.id]
            in_session = [t for t in mine if open_utc <= t.as_of < close_utc]
            y_obs = max(in_session, key=lambda t: t.as_of) if in_session else None
            left = Side.from_obs(y_obs) if y_obs else Side(inst.id, "yahoo", "quote", inst.unit)
            right = Side(inst.id, "alpaca", "sip_bars_1m", inst.unit)
            extra: dict[str, object] = {
                "kind": "equity",
                "minute_offset": None,
                "session_close_utc": close_utc.isoformat(),
            }
            skip: str | None = None
            if y_obs is None:
                if not mine:
                    skip = f"missing side: no yahoo quote tick on {ref}"
                elif all(t.as_of < open_utc for t in mine):
                    skip = (
                        f"missing side: only pre-market yahoo quote ticks before the session "
                        f"open on {ref}"
                    )
                else:
                    skip = f"missing side: no yahoo quote tick before the session close on {ref}"
            else:
                by_start = {b.as_of: b for b in bars if b.instrument_id == inst.id}
                m = _minute(y_obs.as_of)
                for off in MINUTE_OFFSETS:
                    cand = by_start.get(m + timedelta(minutes=off))
                    if cand:
                        right = Side.from_obs(cand)
                        extra["minute_offset"] = off
                        break
                else:
                    skip = (
                        f"missing side: no alpaca/sip_bars_1m bar within 1 minute of "
                        f"{m.isoformat()}"
                    )
            out.append(self._result(ctx, inst, "same", left, right, y_obs, ref, extra, skip))
        return out

    # -- FX ---------------------------------------------------------------------------------

    def _fx(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]:
        cfg = ctx.config
        yahoo_ids = {i.id for i, _ in cfg.feeds_for("yahoo", "quote")}
        td_ids = {i.id for i, _ in cfg.feeds_for("twelvedata", "fx_series_1m")}
        members = [i for i in cfg.instruments if i.kind == "fx" and i.id in yahoo_ids & td_ids]
        if not members:
            return []
        ref = reference_date(ctx, None)
        start, end = et_day_bounds(ref)
        ticks = query_obs(
            ctx, duck, source="yahoo", dataset="quote", interval="tick",
            start=start, end=end, fields=("last",),
        )  # fmt: skip
        bars = query_obs(
            ctx, duck, source="twelvedata", dataset="fx_series_1m", interval="1m",
            start=start, end=end, fields=FX_FIELDS,
        )  # fmt: skip
        out: list[XResult] = []
        for inst in members:
            mine_t = [t for t in ticks if t.instrument_id == inst.id]
            mine_b = [b for b in bars if b.instrument_id == inst.id]
            pair = _nearest_pair(mine_t, mine_b)
            extra: dict[str, object] = {
                "kind": "fx",
                "minute_offset": None,
                "n_yahoo_ticks": len(mine_t),
                "n_twelvedata_bars": len(mine_b),
            }
            skip: str | None = None
            y_obs: Obs | None = None
            left = Side(inst.id, "yahoo", "quote", inst.unit)
            right = Side(inst.id, "twelvedata", "fx_series_1m", inst.unit)
            if pair is not None:
                y_obs, bar, off = pair
                left, right = Side.from_obs(y_obs), Side.from_obs(bar)
                extra["minute_offset"] = off
            elif not mine_b:
                skip = f"missing side: no twelvedata/fx_series_1m bars on {ref}"
            elif not mine_t:
                skip = f"missing side: no yahoo quote tick on {ref}"
            else:
                skip = "no Yahoo tick within ±1m of the ECB-fix bars"
            out.append(self._result(ctx, inst, "level", left, right, y_obs, ref, extra, skip))
        return out

    def _result(
        self,
        ctx: RunContext,
        inst: Instrument,
        relation: str,
        left: Side,
        right: Side,
        y_obs: Obs | None,
        ref: date,
        extra: dict[str, object],
        skip: str | None,
    ) -> XResult:
        return compare(
            ctx,
            check_name=NAME,
            relation=relation,
            group_id=group_id_for(ctx.config, inst.id),
            at_time=y_obs.as_of if y_obs else ctx.now_utc,
            left=left,
            right=right,
            tol=tolerance_for(ctx.config, NAME),
            ref_date=ref,
            extra=extra,
            skip_reason=skip,
        )


def _nearest_pair(ticks: list[Obs], bars: list[Obs]) -> tuple[Obs, Obs, int] | None:
    """Yahoo tick closest to any bar whose start is within ±1 minute of the tick's minute.

    Returns ``(tick, bar, minute_offset)`` where the offset is ``bar start - tick minute`` in
    minutes. Ties go to the smaller minute offset, then the later bar.
    """
    best: tuple[tuple[float, float, float], Obs, Obs, int] | None = None
    for bar in bars:
        centre = bar.as_of + timedelta(seconds=30)
        for tick in ticks:
            off = round((bar.as_of - _minute(tick.as_of)).total_seconds() / 60)
            if abs(off) > 1:
                continue
            key = (
                abs((tick.as_of - centre).total_seconds()),
                float(abs(off)),
                -bar.as_of.timestamp(),
            )
            if best is None or key < best[0]:
                best = (key, tick, bar, off)
    if best is None:
        return None
    return best[1], best[2], best[3]


register(YahooVsPrimary())
