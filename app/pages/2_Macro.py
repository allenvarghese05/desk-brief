"""Macro desk (T54, P-021): volatility term structure, the curve and 2s10s, gold against real
yields, cross-asset correlations and CFTC positioning on one page.

Every figure goes through ``values.value_cell`` / ``change_cell`` (see
``app/components/macro.py``); charts carry gridlines and lines only, with a caption naming
sources, tiers and dates. Bars come from ``data.load_series`` on the shared read-only
connection, which excludes hidden tiers in SQL, so hosted mode never reads tier 1. Names link
to the instrument on Markets (``./?id=``).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import duckdb  # noqa: E402
import streamlit as st  # noqa: E402
from app.components import data, macro  # noqa: E402
from app.components import kit as _kit  # noqa: E402

from deskbrief.store.backend import StorageError  # noqa: E402


def _html(body: str) -> None:
    st.markdown(body, unsafe_allow_html=True)


def _kicker(title: str) -> None:
    _html(f'<div class="db-kicker">{title}</div>')


ctx = _kit.context("Macro")
try:
    snap = data.load_snapshot(ctx.config, ctx.hidden)
    bars = data.load_series(ctx.config, macro.ALL_IDS, ctx.hidden, macro.HISTORY_DAYS)
except StorageError as exc:  # names missing variables, never their values
    st.error(f"Cannot read the data store: {exc}")
    st.stop()
except (duckdb.Error, OSError):  # may hold paths or endpoints: say nothing about the cause
    st.error("Cannot read the data store.")
    st.stop()
ov = data.overview(snap, ctx.mode, ctx.hidden)
rows = {r.instrument_id: r for s in ov.sections for r in s.rows}

left, right = st.columns(2, gap="small")
with left, st.container(key="db-mx-vix"):
    _kicker("VIX term structure · 9D, 30D, 3M, 6M")
    body, cap = macro.vix_term(bars)
    _html(body)
    if cap:
        st.caption(cap)
with right, st.container(key="db-mx-curve"):
    _kicker("Treasury curve · change by tenor")
    body, cap = macro.curve_change(bars)
    _html(body)
    if cap:
        st.caption(cap)

left, right = st.columns(2, gap="small")
with left, st.container(key="db-mx-2s10s"):
    _kicker("2s10s · 10-year minus 2-year yield")
    spread = macro.spread_2s10s(bars)
    _html(macro.spread_figures(spread))
    if len(spread) >= macro.MIN_DAILY:
        st.altair_chart(macro.line_chart(spread, "2s10s (bp)", zero=True, fmt=",.0f"), theme=None)
        st.caption(macro.caption("Daily 2s10s spread, 10Y minus 2Y yield in basis points; "
                                 "zero line marks inversion", [spread]))  # fmt: skip
    elif not spread.empty:
        _html(macro.short_history("2s10s history", [spread], macro.MIN_DAILY, "days"))
with right, st.container(key="db-mx-gold"):
    _kicker("Gold vs 10-year real yield")
    body, gold, real = macro.gold_real(bars)
    _html(body)
    if len(gold) >= macro.MIN_DAILY and len(real) >= macro.MIN_DAILY:
        gname = str(gold["source"].iloc[-1])
        st.altair_chart(
            macro.pair_chart(gold, "Gold", real, "10Y real yield (inverted)", reverse_b=True),
            theme=None,
        )
        st.caption(
            macro.caption(
                "Daily gold closes (solid) and the 10-year TIPS real yield (dashed, drawn "
                f"inverted: up means a lower real yield); gold from {gname}",
                [gold, real],
            )
        )
    elif not gold.empty or not real.empty:
        _html(macro.short_history("Gold and real-yield history", [gold, real], macro.MIN_DAILY,
                                  "days"))  # fmt: skip

with st.container(key="db-mx-corr"):
    _kicker(f"Correlation · {macro.CORR_DAYS} days of daily changes")
    body, cap = macro.heatmap(bars)
    _html(body)
    if cap:
        st.caption(cap)

with st.container(key="db-mx-cot"):
    _kicker("CFTC positioning · net non-commercial (speculative) futures, weekly")
    cols = st.columns(len(macro.COT), gap="small")
    for col, (net_id, oi_id, market) in zip(cols, macro.COT, strict=True):
        with col:
            _html(macro.cot_card(rows.get(net_id), rows.get(oi_id), market, ov.as_of))
            hist = bars.get(net_id)
            if hist is None or hist.empty:
                continue
            if len(hist) >= macro.MIN_WEEKLY:
                st.altair_chart(macro.bar_chart(hist, "net position (contracts)"), theme=None)
                st.caption(macro.caption("Weekly net positions, contracts (green long, red "
                                         "short)", [hist]))  # fmt: skip
            else:
                _html(macro.short_history("Net position history", [hist], macro.MIN_WEEKLY,
                                          "weekly reports"))  # fmt: skip

if ov.withheld:
    st.caption(
        f"{ov.withheld} instrument(s) withheld in {ov.mode} mode: live (tier 1) data is not "
        "shown here until provider display terms are verified."
    )
