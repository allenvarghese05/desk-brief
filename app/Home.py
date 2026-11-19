"""Desk Brief dashboard: the workspace (spec 10.3 pages 1 and 2, T36).

Market strip and biggest moves on top; a watchlist on the left; the selected instrument's chart
with a 30-day overview of every market below it; details, tier ladder, lineage, checks and
the yield curve on the right. The old Instrument page lives here now (``?id=`` selects).

Run locally: ``DESKBRIEF_DISPLAY_MODE=local streamlit run app/Home.py``. Hosted (Streamlit
Community Cloud) leaves the variable unset, which means ``hosted``: tier-1 rows are never
rendered. Reads only; see ``app/components/data.py`` for the read-only R2 connection and the
adapter that T23 will switch to the serving files.
"""

from __future__ import annotations

import os
import sys
from html import escape
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):  # `app` is a plain folder; src/ covers a non-installed checkout
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app import theme  # noqa: E402
from app.components import chart, data, instrument, panels, tearsheet  # noqa: E402

from deskbrief.config import Config, load_config  # noqa: E402
from deskbrief.report.model import Row  # noqa: E402
from deskbrief.store.backend import StorageError  # noqa: E402
from deskbrief.timeutil import et_date  # noqa: E402


@st.cache_resource
def _config(path: str) -> Config:
    return load_config(path)


SEARCH_HINT = "Search: 10y, gold, AAPL"


def _html(target: object, body: str) -> None:
    target.markdown(body, unsafe_allow_html=True)  # type: ignore[attr-defined]


def _selected(ov: data.Overview, query: str) -> str:
    """The instrument on the chart: a single search match, else ``?id=``, else the default."""
    rows = {r.instrument_id: r for s in ov.sections for r in s.rows}
    if query:
        found = [r for s in panels.search(ov.sections, query) for r in s.rows]
        if len(found) == 1:  # Enter on a single match selects it
            return found[0].instrument_id
    wanted = st.query_params.get("id", "")
    for iid in (wanted, panels.DEFAULT_ID):
        if iid in rows and rows[iid].available:
            return iid
    first = next((r for s in ov.sections for r in s.rows if r.available), None)
    return first.instrument_id if first else ""


def _watchlist(ov: data.Overview, query: str, sel: str, keep: dict[str, str]) -> None:
    if not query:
        _html(st, panels.watchlist(ov.sections, ov.as_of, sel, keep))
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
    _html(st, panels.watchlist(found, ov.as_of, sel, keep, fold_proxies=False))


NO_COMPARE = "Compare with…"


def _chart(config: Config, ov: data.Overview, row: Row, hidden: frozenset[int]) -> pd.DataFrame:
    """Header, performance over horizons, controls, the chart and its caption; returns all
    daily bars of the instrument (the risk panel needs more than the range in view)."""
    _html(st, panels.chart_header(row, ov.as_of))
    _html(
        st, instrument.verdict_html(instrument.ladder(config, row.instrument_id, hidden, ov.as_of))
    )
    bars = data.load_bars(config, row.instrument_id, hidden)
    _html(st, tearsheet.horizons_html(tearsheet.horizons(row, bars)))
    if bars.empty:
        _html(st, '<div class="db-empty">No daily history for this instrument yet.</div>')
        return bars
    c1, c2, c3, c4 = st.columns([2.6, 1.6, 2.2, 1.4], vertical_alignment="center")
    rng = (
        c1.segmented_control("Range", list(chart.RANGES), key="rng", label_visibility="collapsed")
        or chart.DEFAULT_RANGE
    )
    st.query_params["r"] = rng
    styles = [chart.CANDLES, chart.LINE] if chart.has_ohlc(bars) else [chart.LINE]
    style = (
        c2.segmented_control("Chart style", styles, key="style", label_visibility="collapsed")
        if len(styles) > 1
        else chart.LINE
    ) or styles[0]
    others = {
        r.instrument_id: f"{panels.short(r)} · {r.name}"
        for s in ov.sections
        for r in s.rows
        if r.available and r.instrument_id != row.instrument_id
    }
    vs = c3.selectbox(
        "Compare with",
        [NO_COMPARE, *others],
        format_func=lambda i: others.get(i, i),
        key=f"vs-{row.instrument_id}",
        label_visibility="collapsed",
    )
    view = chart.window(bars, rng)
    c4.download_button(
        "Download CSV",
        view.drop(columns=[c for c in ("ok",) if c in view]).to_csv(index=False),
        file_name=f"{row.instrument_id}_daily.csv",
        mime="text/csv",
        width="stretch",
    )
    last = row.change.last.value if row.change is not None else None
    if vs != NO_COMPARE:
        other = chart.window(data.load_bars(config, vs, hidden), rng)
        long = chart.rebased(view, other, panels.short(row), others[vs].split(" · ")[0])
        if long.empty:
            _html(st, '<div class="db-empty">No common days to compare in this range.</div>')
        else:
            names = list(dict.fromkeys(long["series"]))
            st.altair_chart(chart.compare_chart(long, names[0], names[1]), theme=None)
            st.caption(
                "Both series set to 100 on the first common day; dashed: "
                f"{escape(others[vs])}. {panels.short(row)}: "
                f"{chart.caption(view, latest_line=False)} {others[vs].split(' · ')[0]}: "
                f"{chart.caption(other, latest_line=False)}"
            )
            return bars
    if len(view) < 2:
        _html(st, '<div class="db-empty">Fewer than two days in this range.</div>')
    else:
        st.altair_chart(chart.price_chart(view, style=style, last=last), theme=None)
    st.caption(chart.caption(view))
    return bars


def _values(config: Config, ov: data.Overview, row: Row, hidden: frozenset[int]) -> None:
    body, n = tearsheet.all_values_html(config, row.instrument_id, hidden, ov.as_of)
    _html(
        st,
        f'<div class="db-kicker">Every value we hold · {n} '
        f"{'line' if n == 1 else 'lines'}</div>"
        '<div class="db-note" style="margin-bottom:.4rem">Latest value per source, dataset, '
        "field and interval: when it was true (as of) and when we fetched it. Hover a value "
        "for its full trail.</div>" + body,
    )


def _details(config: Config, ov: data.Overview, row: Row, bars: pd.DataFrame,
             hidden: frozenset[int]) -> None:  # fmt: skip
    today = et_date(ov.as_of)
    rows = instrument.ladder(config, row.instrument_id, hidden, ov.as_of)
    _html(
        st,
        '<div class="db-kicker">Do the sources agree?</div>'
        + instrument.ladder_html(rows)
        + '<div class="db-kicker">Cross-checks, last 14 days</div>'
        + tearsheet.agreement_html(config, row.instrument_id, hidden, today),
    )
    _html(st, '<div class="db-kicker">Risk and range</div>' + tearsheet.risk_html(row, bars))
    if rows:
        with st.expander("Lineage: source, fetch and raw file"):
            _html(st, instrument.lineage_html(config, rows[0]))
    with st.expander(f"Data checks, last {instrument.CHECK_DAYS} days"):
        _html(st, instrument.checks_html(config, row.instrument_id, hidden, today))
    with st.expander(f"Corrections, last {tearsheet.REVISION_DAYS} days"):
        _html(st, tearsheet.revisions_html(config, row.instrument_id, hidden, ov.as_of))
    _html(st, '<div class="db-kicker">Yield curve · official</div>' + panels.yield_curve(ov))


def main() -> None:
    st.set_page_config(page_title="Desk Brief", layout="wide", initial_sidebar_state="collapsed")
    theme.apply()
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
    wanted_rng = st.query_params.get("r", chart.DEFAULT_RANGE)
    st.session_state.setdefault("rng", wanted_rng if wanted_rng in chart.RANGES else "3M")
    st.session_state.setdefault("style", chart.CANDLES)

    left, mid, right = st.columns([3, 4, 3], vertical_alignment="center")
    _html(left, panels.brand(ov))
    query = mid.text_input(
        "Search instruments", placeholder=SEARCH_HINT, label_visibility="collapsed", key="q"
    ).strip()
    _html(right, panels.nav() + panels.status(ov))
    sel = _selected(ov, query)
    keep = {"r": st.session_state.get("rng") or chart.DEFAULT_RANGE, "q": query}
    if sel:
        st.query_params["id"] = sel
    if query:
        st.query_params["q"] = query
    elif "q" in st.query_params:
        del st.query_params["q"]

    _html(st, panels.tiles(ov, sel, keep))
    _html(st, panels.moves(ov, keep))

    rows = {r.instrument_id: r for s in ov.sections for r in s.rows}
    col_l, col_c, col_r = st.columns([2.5, 6, 2.7], gap="small")
    with col_l, st.container(key="db-watch"):
        _watchlist(ov, query, sel, keep)
    bars = pd.DataFrame()
    with col_c:
        with st.container(key="db-chart"):
            if sel:
                bars = _chart(config, ov, rows[sel], hidden)
            else:
                _html(st, '<div class="db-empty">No instruments to chart yet.</div>')
        if sel:
            with st.container(key="db-values"):
                _values(config, ov, rows[sel], hidden)
        with st.container(key="db-overview"):
            _html(st, '<div class="db-kicker">Market overview · last 30 days</div>')
            _html(st, panels.overview(ov, sel, keep))
    with col_r, st.container(key="db-detail"):
        if sel:
            _details(config, ov, rows[sel], bars, hidden)

    if ov.withheld:
        st.caption(
            f"{ov.withheld} instrument(s) withheld in {ov.mode} mode: live (tier 1) data is "
            "not shown here until provider display terms are verified."
        )
    with st.expander("Known limits"):
        for limit in ov.known_limits:
            st.markdown(f"- {limit.text}")


main()
