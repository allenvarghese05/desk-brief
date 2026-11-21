"""Macro desk (T54, P-021; P-022 template since T59): volatility term structure, the curve and
2s10s, gold against real yields, cross-asset correlations and CFTC positioning on one page.

Five blocks under the page heading, whose control is the chart range (3M to All): the VIX term
structure, the Treasury curve (its change by tenor and 2s10s, one tab each), gold against the
real yield, the correlation heatmap and CFTC positioning (one tab per market); then the page
footer with the display mode and the glyph legend.

Every figure goes through ``values.value_cell`` / ``change_cell`` (compact; see
``app/components/macro.py``). Time series are TradingView charts with the "Charts by
TradingView" attribution (``tvchart.chart`` draws it); the heatmap is an HTML table. Bars come
from ``data.load_series`` on the shared read-only connection, which excludes hidden tiers in
SQL, so hosted mode never reads tier 1.

Navigation stays in the session (T59): each table row (an instrument's name and figures) sits
under a transparent button that opens the instrument on Markets with ``st.switch_page`` and
``query_params``, and stores it under Markets' kept-selection key, so it is still selected after
a later visit. The chart range is kept the same way across page visits.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from app import Home, theme  # noqa: E402
from app.components import data, macro, tvchart  # noqa: E402
from app.components import kit as _kit  # noqa: E402

from deskbrief.store.backend import StorageError  # noqa: E402

RNG_KEY, KEEP_RNG = "mx-rng", "db-keep-mx-rng"
CHART_H, PAIR_H, COT_H = 230, 170, 170


def _html(body: str) -> None:
    st.markdown(body, unsafe_allow_html=True)


def _open(iid: str) -> None:
    """Open ``iid`` on Markets in this session (no browser reload, no ``target="_self"``)."""
    st.session_state[macro.MARKETS_KEEP_ID] = iid
    # the router's own st.Page for Markets (the default page): a path string cannot name the
    # default page reliably, the Page object always can
    st.switch_page(Home.pages()[0], query_params={"id": iid})


def _keep() -> None:
    st.session_state[KEEP_RNG] = st.session_state.get(RNG_KEY)


def _panel(where: str, panel: macro.Panel) -> None:
    """Words and curve, the table (each row under its in-session button), what follows it,
    and the caption as the card's footer."""
    if panel.top:
        _html(panel.top)
    if panel.table is not None:
        with st.container(key=f"db-mx-tbl-{where}"):
            _html(panel.table.head)
            for iid, name, row in panel.table.rows:
                if not iid:
                    _html(row)
                    continue
                with st.container(key=f"db-pick-mx{where}-{iid}"):
                    _html(f'<div class="db-pickbody">{row}</div>')
                    if st.button(f"Open {name} on Markets", key=f"mx-{where}-{iid}"):
                        _open(iid)
    if panel.bottom:
        _html(panel.bottom)
    _kit.card_footer(panel.caption)


def _too_short(what: str, frames: list[pd.DataFrame], need: int, unit: str) -> bool:
    """True (and says so) when a history is too short for a line; nothing stored: say nothing."""
    if all(len(f) >= need for f in frames):
        return False
    if any(not f.empty for f in frames):
        _html(macro.short_history(what, frames, need, unit))
    return True


ctx = _kit.context("Macro", controls=True)
_html(macro.MACRO_CSS)
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

# the page control: the range every chart draws (kept across page visits like Markets' range)
if st.session_state.get(RNG_KEY) not in macro.RANGES:
    kept = st.session_state.get(KEEP_RNG)
    st.session_state[RNG_KEY] = kept if kept in macro.RANGES else macro.DEFAULT_RANGE
with ctx.controls:  # type: ignore[attr-defined]
    rng = (
        st.segmented_control(
            "Chart range", list(macro.RANGES), key=RNG_KEY, label_visibility="collapsed",
            on_change=_keep,
        )
        or macro.DEFAULT_RANGE
    )  # fmt: skip

left, right = st.columns(2, gap="small")
with left, _kit.card("mx-vix", "VIX term structure · 9D, 30D, 3M, 6M"):
    _panel("vix", macro.vix_term(bars))
with right, _kit.card("mx-curve", "Treasury curve · change by tenor and 2s10s"):
    by_tenor, s2s10s = st.tabs(["Change by tenor", "2s10s"])
    with by_tenor:
        _panel("curve", macro.curve_change(bars))
    with s2s10s:
        _html('<div class="db-ct-note">2s10s · 10-year minus 2-year yield</div>')
        spread = macro.spread_2s10s(bars)
        _html(macro.spread_figures(spread))
        if not _too_short("2s10s history", [spread], macro.MIN_DAILY, "days"):
            view = macro.window(spread, rng)
            tvchart.chart(
                [macro.tv_line(view, "2s10s", field="10Y minus 2Y yield spread, bp")],
                key="mx-2s10s-chart", height=CHART_H, precision=0,
                label="2s10s spread in basis points",
            )  # fmt: skip
            _kit.card_footer(
                macro.caption(
                    "Daily 2s10s spread, 10Y minus 2Y yield in basis points; below zero the "
                    "curve is inverted",
                    [view],
                )
            )

left, right = st.columns(2, gap="small")
with left, _kit.card("mx-gold", "Gold vs 10-year real yield"):
    figures, gold, real = macro.gold_real(bars)
    _html(figures)
    if not _too_short("Gold and real-yield history", [gold, real], macro.MIN_DAILY, "days"):
        gname = str(gold["source"].iloc[-1])
        g, r = macro.window(gold, rng), macro.window(real, rng)
        tvchart.chart([macro.tv_line(g, "Gold")], key="mx-gold-chart", height=PAIR_H,
                      precision=2, label="Gold daily closes")  # fmt: skip
        tvchart.chart(
            [macro.tv_line(r, "10Y real yield", field="real yield", color=theme.AMBER)],
            key="mx-real-chart", height=PAIR_H, precision=2,
            label="10-year real yield, percent",
        )  # fmt: skip
        _kit.card_footer(
            macro.caption(
                f"Daily gold closes (top, from {gname}) and the 10-year TIPS real yield "
                "(bottom, percent): gold usually rises when the real yield falls",
                [g, r],
            )
        )
with right, _kit.card("mx-corr", f"Correlation · {macro.CORR_DAYS} days of daily changes"):
    _panel("heat", macro.heatmap(bars))

with _kit.card(
    "mx-cot", "CFTC positioning · net non-commercial (speculative) futures, weekly"
):  # one tab per market: depth in tabs, not down the page
    for tab, (net_id, oi_id, market) in zip(
        st.tabs([m for _, _, m in macro.COT]), macro.COT, strict=True
    ):
        with tab:
            _html(macro.cot_card(rows.get(net_id), rows.get(oi_id), market, ov.as_of))
            hist = bars.get(net_id)
            if hist is None or hist.empty:
                continue
            if _too_short("Net position history", [hist], macro.MIN_WEEKLY, "weekly reports"):
                continue
            view = macro.window(hist, rng)
            tvchart.chart([macro.tv_line(view, "Net position")], key=f"mx-cot-{net_id}",
                          height=COT_H, precision=0,
                          label=f"{market} net speculative position, contracts")  # fmt: skip
            _kit.card_footer(
                macro.caption(
                    "Weekly net positions, contracts (above zero net long, below net short)",
                    [view],
                )
            )

_kit.page_footer(
    ctx,
    f"{ov.withheld} instrument(s) withheld in {ov.mode} mode: live (tier 1) data is not shown "
    "here until provider display terms are verified."
    if ov.withheld
    else "",
)
