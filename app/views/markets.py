"""Markets, the Home page v2 (P-022, T58; spec 10.3 as rewritten by T58).

One focal point, the chart of the selected instrument, and five blocks around it:

* a slim top bar: brand and date, a compact search and one status dot (the page tabs are
  ``st.navigation``'s, drawn by the router ``app/Home.py``);
* a headline strip of five instruments: value, change pill, sparkline and tier glyph;
* a watchlist rail grouped by asset class, with full names and a sigma tag on unusual moves;
* the chart card: name, price, change pill and provenance in its header, range pills
  (1M 3M 6M 1Y All) and a Candles/Line toggle, compare and CSV in a small menu, the
  TradingView chart with its attribution, one row of 1D to 1Y changes and a footer line;
* a right rail: "Do the sources agree?" and its sources, then risk and range;
* tabs under the chart: All values, Lineage, Checks, Corrections.

The display mode, withheld instruments and the glyph legend sit in the page footer. Every
figure uses compact provenance (``compact=True``).

Selection stays in the session (T57): each tile and watchlist row sits under a transparent
button whose callback sets ``st.query_params["id"]``. The selected instrument, range and chart
style are also kept in plain session-state keys, so they survive a visit to another page
(Markets, then Macro, then Markets) even though Streamlit drops the widgets' own state and the
URL's parameters on a page change. The chart card is an ``st.fragment``: a range, style or
compare change reruns only the card.

``app/Home.py`` is the router and runs this page; start the app with
``DESKBRIEF_DISPLAY_MODE=local streamlit run app/Home.py``. Hosted (Streamlit Community Cloud)
leaves the variable unset, which means ``hosted``: tier-1 rows are never rendered. Reads only;
see ``app/components/data.py`` for the read-only connection.
"""

from __future__ import annotations

import os
import sys
from html import escape
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src"):  # `app` is a plain folder; src/ covers a non-installed checkout
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app import theme  # noqa: E402
from app.components import chart, data, instrument, kit, panels, tearsheet, tvchart  # noqa: E402
from app.components.values import TIER_NAME  # noqa: E402

from deskbrief.config import Config, load_config  # noqa: E402
from deskbrief.report.model import Row  # noqa: E402
from deskbrief.store.backend import StorageError  # noqa: E402
from deskbrief.timeutil import et_date  # noqa: E402


@st.cache_resource
def _config(path: str) -> Config:
    return load_config(path)


SEARCH_HINT = "Search: 10y, gold, AAPL"
HEALTH_PAGE = "pages/4_Data_health.py"  # relative to the router, app/Home.py
MACRO_PAGE = "pages/2_Macro.py"
MACRO_LINK = "Yield curve and 2s10s on Macro"
NO_COMPARE = "Compare with…"
#: the chart styles of the Home chart (candles only when the bars carry real OHLC)
STYLES = (chart.CANDLES, chart.LINE)
CHART_HEIGHT = 420
WATCH_HEIGHT = 730
TABS = ("All values", "Lineage", "Checks", "Corrections")
#: widget keys of the chart controls
RNG_KEY, STYLE_KEY = "db-rng", "db-style"
#: plain session-state keys (not widgets): they survive a visit to another page
KEEP_ID, KEEP_RNG, KEEP_STYLE = "db-keep-id", "db-keep-rng", "db-keep-style"


def _html(target: object, body: str) -> None:
    target.markdown(body, unsafe_allow_html=True)  # type: ignore[attr-defined]


def _rows(ov: data.Overview) -> dict[str, Row]:
    return {r.instrument_id: r for s in ov.sections for r in s.rows}


def _selected(ov: data.Overview, query: str) -> str:
    """The instrument on the chart: a single search match, else ``?id=``, else the one kept
    from earlier in the session, else the default."""
    rows = _rows(ov)
    if query:
        found = [r for s in panels.search(ov.sections, query) for r in s.rows]
        if len(found) == 1:  # Enter on a single match selects it
            return found[0].instrument_id
    wanted = st.query_params.get("id", "")
    for iid in (wanted, st.session_state.get(KEEP_ID, ""), panels.DEFAULT_ID):
        if iid in rows and rows[iid].available:
            return str(iid)
    first = next((r for s in ov.sections for r in s.rows if r.available), None)
    return first.instrument_id if first else ""


def _select(iid: str) -> None:
    """A click on an instrument: the URL and the session carry it; search and range stay."""
    st.query_params["id"] = iid
    st.session_state[KEEP_ID] = iid


def _keep(widget: str, keep: str) -> None:
    """Copy a control's value to a plain key that survives a page change."""
    st.session_state[keep] = st.session_state.get(widget)


def _pick(where: str, iid: str, body: str, name: str) -> None:
    """``body`` drawn as is, under a transparent button that selects ``iid`` in-session."""
    with st.container(key=f"db-pick-{where}-{iid}"):
        _html(st, f'<div class="db-pickbody">{body}</div>')  # a block, so markdown keeps it whole
        st.button(f"Select {name}", key=f"pick-{where}-{iid}", on_click=_select, args=(iid,))


def _names(ov: data.Overview) -> dict[str, str]:
    return {r.instrument_id: f"{panels.short(r)}, {r.name}" for s in ov.sections for r in s.rows}


# ---------------------------------------------------------------------------
# top bar, headline strip, watchlist
# ---------------------------------------------------------------------------


def _health(ov: data.Overview) -> None:
    with st.container(key="db-pick-health"):
        _html(st, panels.status(ov))
        # the counts are in the label, so they are read out without hovering (T57 review)
        if st.button(f"Open Data health: {ov.health.text}", key="pick-health"):
            st.switch_page(HEALTH_PAGE)


def _tiles(ov: data.Overview, sel: str) -> None:
    names = _names(ov)
    with st.container(horizontal=True, key="db-tiles"):
        for iid, body in panels.tile_items(ov, sel):
            _pick("tile", iid, body, names.get(iid, iid))


def _groups(groups: list[panels.WatchGroup], names: dict[str, str], sel: str) -> None:
    for g in groups:
        _html(st, panels.group_title(g.title))
        for iid, body in g.rows:
            _pick("wl", iid, body, names.get(iid, iid))
        if g.proxies:
            opened = any(iid == sel for iid, _ in g.proxies)
            with st.expander(g.fold_label, expanded=opened):
                for iid, body in g.proxies:
                    _pick("wl", iid, body, names.get(iid, iid))


def _watchlist(ov: data.Overview, query: str, sel: str) -> None:
    names = _names(ov)
    _html(st, '<div class="db-card-h">Watchlist</div>')
    sparks, sigs = ov.sparks, panels.sigmas(ov)
    if not query:
        groups = panels.watchlist(ov.sections, ov.as_of, sel, sparks=sparks, sigs=sigs)
        _groups(groups, names, sel)
        return
    found = panels.search(ov.sections, query)
    n = sum(len(s.rows) for s in found)
    if not found:
        _html(
            st,
            f'<div class="db-empty">No instrument matches "{escape(query)}". '
            "Try a ticker (SPY, AAPL), a name (gold, yen) or a tenor (2y, 10y).</div>",
        )
        return
    noun = "match" if n == 1 else "matches"
    _html(st, f'<div class="db-note">{n} {noun} for "{escape(query)}"</div>')
    groups = panels.watchlist(found, ov.as_of, sel, fold_proxies=False, sparks=sparks, sigs=sigs)
    _groups(groups, names, sel)


# ---------------------------------------------------------------------------
# the chart card (one fragment: range, style and compare rerun only this card)
# ---------------------------------------------------------------------------


def _restore(widget: str, keep: str, options: tuple[str, ...], default: str) -> None:
    """Before a control is drawn: give it the value kept from earlier in the session (or the
    default) when Streamlit has dropped its state, e.g. after a visit to another page."""
    if st.session_state.get(widget) not in options:
        kept = st.session_state.get(keep)
        st.session_state[widget] = kept if kept in options else default


def _controls(row: Row, bars: pd.DataFrame, others: dict[str, str]) -> tuple[str, str, str]:
    """Range pills, the Candles/Line toggle and the ``⋯`` menu (compare, CSV)."""
    iid = row.instrument_id
    with st.container(horizontal=True, key="db-ctrls"):
        rng = (
            st.segmented_control(
                "Range", list(chart.RANGES), key=RNG_KEY, label_visibility="collapsed",
                on_change=_keep, args=(RNG_KEY, KEEP_RNG),
            )
            or chart.DEFAULT_RANGE
        )  # fmt: skip
        st.session_state[KEEP_RNG] = rng
        style = chart.LINE
        if chart.has_ohlc(bars):
            style = (
                st.segmented_control(
                    "Chart style", list(STYLES), key=STYLE_KEY, label_visibility="collapsed",
                    on_change=_keep, args=(STYLE_KEY, KEEP_STYLE),
                )
                or chart.CANDLES
            )  # fmt: skip
        with st.popover("More", icon=":material/more_horiz:"):
            vs = st.selectbox(
                "Compare with",
                [NO_COMPARE, *others],
                format_func=lambda i: others.get(i, i),
                key=f"vs-{iid}",
            )
            view = chart.window(bars, rng)
            st.download_button(
                "Download CSV of the range shown",
                view.drop(columns=[c for c in ("ok",) if c in view]).to_csv(index=False),
                file_name=f"{iid}_daily.csv",
                mime="text/csv",
                width="stretch",
                disabled=bars.empty,
            )
    return rng, style, vs


def _draw(
    config: Config, row: Row, bars: pd.DataFrame, rng: str, style: str, vs: str,
    others: dict[str, str], hidden: frozenset[int],
) -> str:  # fmt: skip
    """The chart itself (with its attribution); returns the footer line."""
    name = panels.short(row)
    view = chart.window(bars, rng)
    if vs != NO_COMPARE:
        other_name = others[vs].split(" · ")[0]
        other = chart.window(data.load_bars(config, vs, hidden), rng)
        series = chart.tv_compare_series(view, other, name, other_name)
        if series and series[0]["points"]:
            tvchart.chart(series, key="db-tv", height=CHART_HEIGHT, precision=2,
                          label=f"{name} and {other_name} rebased to 100")  # fmt: skip
            return (
                f"Both series set to 100 on the first common day; dashed: {others[vs]}. "
                f"{name}: {chart.caption(view, latest_line=False)} "
                f"{other_name}: {chart.caption(other, latest_line=False)}"
            )
        _html(st, f'<div class="db-empty">No common days with {escape(other_name)} in this '
                  "range; showing this instrument alone.</div>")  # fmt: skip
    if len(view) < 2:
        _html(st, '<div class="db-empty">Fewer than two days in this range.</div>')
        return chart.caption(view, latest_line=False)
    last = row.change.last.value if row.change is not None else None
    drawn = tvchart.chart(
        chart.tv_series(view, style=style), key="db-tv", height=CHART_HEIGHT,
        precision=chart.tv_precision(view), last=last, label=f"{name} price chart",
    )  # fmt: skip
    # the Altair fallback draws no latest-value line, so its footer does not mention one
    return chart.caption(view, latest_line=drawn and last is not None)


@st.fragment
def _hero(config: Config, ov: data.Overview, row: Row, hidden: frozenset[int]) -> None:
    """Header (name, price, change, provenance) with the controls on its right, the chart,
    the row of changes over horizons and the footer line naming what the chart shows."""
    iid = row.instrument_id
    bars = data.load_bars(config, iid, hidden)
    wanted = st.query_params.get("r", "")
    first_rng = wanted if wanted in chart.RANGES else chart.DEFAULT_RANGE
    _restore(RNG_KEY, KEEP_RNG, tuple(chart.RANGES), first_rng)
    _restore(STYLE_KEY, KEEP_STYLE, STYLES, chart.CANDLES)
    others = {
        r.instrument_id: f"{panels.short(r)} · {r.name}"
        for s in ov.sections
        for r in s.rows
        if r.available and r.instrument_id != iid
    }
    left, right = st.columns([4, 5], vertical_alignment="top")
    _html(left, panels.chart_header(row, ov.as_of))
    with right:
        rng, style, vs = _controls(row, bars, others)
    st.query_params["r"] = rng
    if bars.empty:
        _html(st, '<div class="db-empty">No daily history for this instrument yet.</div>')
        foot = ""
    else:
        foot = _draw(config, row, bars, rng, style, vs, others, hidden)
    _html(st, tearsheet.horizons_html(tearsheet.horizons(row, bars)))
    if foot:
        _html(st, f'<div class="db-chfoot">{escape(foot)}</div>')


# ---------------------------------------------------------------------------
# tabs under the chart and the right rail
# ---------------------------------------------------------------------------


def _kicker(title: str) -> str:
    return f'<div class="db-kicker">{escape(title)}</div>'


def _tabs(config: Config, ov: data.Overview, row: Row, hidden: frozenset[int]) -> None:
    iid = row.instrument_id
    today = et_date(ov.as_of)
    values, lineage, checks, corrections = st.tabs(list(TABS))
    with values:
        body, n = tearsheet.all_values_html(config, iid, hidden, ov.as_of)
        _html(
            st,
            _kicker(f"Every value we hold · {n} {'line' if n == 1 else 'lines'}")
            + '<div class="db-note" style="margin-bottom:.4rem">Latest value per source, '
            "dataset, field and interval: when it was true (as of) and when we fetched it. "
            "Hover, focus or tap a glyph for its full trail.</div>" + body,
        )
    with lineage:
        ladder = instrument.ladder(config, iid, hidden, ov.as_of)
        if not ladder:
            _html(st, '<div class="db-empty">No value in a tier shown in this mode.</div>')
        for r in ladder:
            title = f"Lineage: source, fetch and raw file · {r.source} · {TIER_NAME[r.tier]}"
            _html(st, _kicker(title) + instrument.lineage_html(config, r))
    with checks:
        _html(
            st,
            _kicker(f"Cross-checks, last {tearsheet.XCHECK_DAYS} days")
            + tearsheet.agreement_html(config, iid, hidden, today)
            + _kicker(f"Data checks, last {instrument.CHECK_DAYS} days")
            + instrument.checks_html(config, iid, hidden, today)
            + _kicker("Known limits"),
        )
        for limit in ov.known_limits:
            st.markdown(f"- {limit.text}")
    with corrections:
        _html(
            st,
            _kicker(f"Corrections, last {tearsheet.REVISION_DAYS} days")
            + tearsheet.revisions_html(config, iid, hidden, ov.as_of),
        )


def _rail(config: Config, ov: data.Overview, row: Row, hidden: frozenset[int]) -> None:
    iid = row.instrument_id
    ladder = instrument.ladder(config, iid, hidden, ov.as_of)
    with st.container(key="db-detail"):
        summary = tearsheet.xcheck_summary(config, iid, hidden, et_date(ov.as_of))
        _html(st, instrument.verdict_card_html(ladder, summary))
    with st.container(key="db-risk"):
        bars = data.load_bars(config, iid, hidden)
        _html(st, '<div class="db-card-h">Risk and range</div>' + tearsheet.risk_html(row, bars))
        if ladder and st.button(
            "Lineage: source, fetch and raw file", key=f"src-{iid}",
            icon=":material/chevron_right:", type="tertiary",
        ):  # fmt: skip
            kit.details(
                f"{panels.short(row)} · source and lineage",
                instrument.lineage_html(config, ladder[0]),
            )
        st.page_link(MACRO_PAGE, label=MACRO_LINK, icon=":material/chevron_right:")


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Desk Brief", layout="wide", initial_sidebar_state="collapsed")
    theme.apply()
    _html(st, panels.HOME_CSS)
    path = os.environ.get("DESKBRIEF_CONFIG") or str(ROOT / "config.yaml")
    config = _config(path)
    mode = data.resolve_mode(os.environ.get(config.dashboard.mode_env))
    hidden = data.hidden_tiers(mode, config)
    try:
        snap = data.load_snapshot(config, hidden)
    except StorageError as exc:  # message names missing variables, never their values
        st.error(f"Cannot read the data store: {exc}")
        return
    except (duckdb.Error, OSError):  # may hold paths or endpoints: say nothing about the cause
        st.error("Cannot read the data store.")
        return
    ov = data.overview(snap, mode, hidden)

    # the URL carries the selection, range and search, so a click or a reload keeps them
    st.session_state.setdefault("q", st.query_params.get("q", ""))
    left, mid, right = st.columns([3, 4, 3], vertical_alignment="center")
    _html(left, panels.brand(ov))
    query = mid.text_input(
        "Search instruments", placeholder=SEARCH_HINT, label_visibility="collapsed", key="q"
    ).strip()
    with right:
        _health(ov)
    sel = _selected(ov, query)
    if sel:
        st.query_params["id"] = sel
        st.session_state[KEEP_ID] = sel
    if query:
        st.query_params["q"] = query
    elif "q" in st.query_params:
        del st.query_params["q"]

    _tiles(ov, sel)

    rows = _rows(ov)
    col_l, col_c, col_r = st.columns([2.9, 8, 3.1], gap="small")
    # the rail scrolls on its own, at about the chart card's height (P-022: one focal point,
    # depth inside a block rather than down the page)
    with col_l, st.container(key="db-watch", height=WATCH_HEIGHT, border=False):
        _watchlist(ov, query, sel)
    with col_c:
        with st.container(key="db-chart"):
            if sel:
                _hero(config, ov, rows[sel], hidden)
            else:
                _html(st, '<div class="db-empty">No instruments to chart yet.</div>')
        if sel:
            with st.container(key="db-values"):
                _tabs(config, ov, rows[sel], hidden)
    with col_r:
        if sel:
            _rail(config, ov, rows[sel], hidden)

    _html(st, panels.footer(ov, kit.glyph_legend(hidden)))


main()
