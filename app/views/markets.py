"""Markets, the Home page v3 (P-023, T60; spec 10.3 as rewritten by T60). No containers.

* a top bar: brand and date, a compact search and one status line ("Last run Fri 07:00 · 14
  checks ok"; the page tabs are ``st.navigation``'s, drawn by the router ``app/Home.py``);
* a one-line market strip of five instruments as plain text, the shared basis said once;
* two columns: the hero on the left (label, figure, the 1D pill, provenance line, range and
  style controls with Compare and CSV in "More", the TradingView chart, the 1W to 1Y horizon
  row with "Charts by TradingView" at its right, then "Do the sources agree?" and "Risk and
  range" side by side), and the watchlist on the right: full length, no inner scroll, groups
  that fold, a sigma tag on unusual moves and the same CSS hover content on every row;
* below 960 px the watchlist drops under the hero; below 640 px sources and risk stack;
* tabs under the main area: All values, Lineage, Checks, Corrections.

Blocks sit on the page background: each opens with a small uppercase label over a hairline
(``panels.label``); no panel, border, radius or shadow (the flat-CSS checker in the tests).
The display mode, withheld instruments and the glyph legend sit in the page footer. Every
figure uses compact provenance.

Selection stays in the session (T57): each strip item and watchlist row sits under a
transparent button whose callback sets ``st.query_params["id"]``. The selected instrument,
range, chart style and folded groups are also kept in plain session-state keys, so they survive
a visit to another page (Markets, then Macro, then Markets). The hero is an ``st.fragment``: a
range, style or compare change reruns only the hero.

``app/Home.py`` is the router and runs this page; start the app with
``DESKBRIEF_DISPLAY_MODE=local streamlit run app/Home.py``. Hosted (Streamlit Community Cloud)
leaves the variable unset, which means ``hosted``: tier-1 rows are never rendered. Reads only;
see ``app/components/data.py`` for the read-only connection.
"""
# ruff: noqa: RUF001  (the sigma sign is intentional output)

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
from app.components import (  # noqa: E402
    brief,
    chart,
    company,
    data,
    instrument,
    kit,
    panels,
    tearsheet,
    tvchart,
)
from app.components.values import TIER_NAME  # noqa: E402

from deskbrief.config import Config, load_config  # noqa: E402
from deskbrief.report.model import Row  # noqa: E402
from deskbrief.store.backend import StorageError  # noqa: E402
from deskbrief.timeutil import et_date  # noqa: E402


@st.cache_resource
def _config(path: str) -> Config:
    return load_config(path)


SEARCH_HINT = "Search: 10y, gold, AAPL  ( / )"
HEALTH_PAGE = "pages/4_Data_health.py"  # relative to the router, app/Home.py
MACRO_PAGE = "pages/2_Macro.py"
MACRO_LINK = "Yield curve and 2s10s on Macro"
NO_COMPARE = "Compare with…"
#: the chart styles of the Home chart (candles only when the bars carry real OHLC)
STYLES = (chart.CANDLES, chart.LINE)
CHART_HEIGHT = 380
TABS = ("All values", "Lineage", "Checks", "Corrections")
#: widget keys of the chart controls
RNG_KEY, STYLE_KEY = "db-rng", "db-style"
#: plain session-state keys (not widgets): they survive a visit to another page
KEEP_ID, KEEP_RNG, KEEP_STYLE = "db-keep-id", "db-keep-rng", "db-keep-style"
#: plain session-state key: the watchlist groups the user folded (groups start open)
KEEP_FOLDED = "db-keep-folded"
#: plain session-state key: in "Unusual first", the groups the user opened (they start folded)
KEEP_OPENED = "db-keep-opened"
#: the watchlist switch (P-024): its widget key and the plain key that survives a page change
MODE_KEY, KEEP_MODE = "db-wl-mode", "db-keep-mode"
#: "/" focuses the search field (P-024 keyboard); typing in any field is left alone
SLASH_JS = """<script>
(() => {
  const w = window.parent && window.parent.document ? window.parent : window;
  if (w.__dbSlash) return;
  w.__dbSlash = true;
  w.document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target, tag = t && t.tagName ? t.tagName.toLowerCase() : "";
    if (tag === "input" || tag === "textarea" || (t && t.isContentEditable)) return;
    const box = w.document.querySelector('.st-key-q input');
    if (box) { e.preventDefault(); box.focus(); box.select(); }
  });
})();
</script>"""


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


def _health(ov: data.Overview, run: tuple[object, object] | None) -> None:
    with st.container(key="db-pick-health"):
        _html(st, panels.status(ov, run))  # type: ignore[arg-type]
        # the counts are in the label, so they are read out without hovering (T57 review)
        if st.button(f"Open Data health: {ov.health.text}", key="pick-health"):
            st.switch_page(HEALTH_PAGE)


def _strip(ov: data.Overview, sel: str) -> None:
    """The one-line market strip; each item selects its instrument in-session."""
    names = _names(ov)
    lead, items = panels.strip_items(ov, sel)
    with st.container(horizontal=True, key="db-strip"):
        if lead:
            _html(st, panels.strip_lead(lead))
        for iid, body in items:
            _pick("strip", iid, body, names.get(iid, iid))


def _fold(title: str, keep: str = KEEP_FOLDED) -> None:
    """A click on a group header: fold or unfold it (kept for the session)."""
    toggled = set(st.session_state.get(keep, ()))
    toggled ^= {title}
    st.session_state[keep] = sorted(toggled)


def _groups(
    groups: list[panels.WatchGroup], names: dict[str, str], sel: str, *, start_folded: bool = False
) -> None:
    """Every group with a header that folds it. A folded group still shows the selected row,
    so the selection never folds away. Groups start open ("By group") or folded (everything
    else under "Unusual first", P-024)."""
    keep = KEEP_OPENED if start_folded else KEEP_FOLDED
    toggled = set(st.session_state.get(keep, ()))
    for g in groups:
        is_folded = (g.title not in toggled) if start_folded else (g.title in toggled)
        with st.container(key=f"db-pick-grp-{g.key}"):
            _html(st, panels.group_title(g.title, folded=is_folded, count=len(g)))
            verb = "Unfold" if is_folded else "Fold"
            st.button(
                f"{verb} {g.title}", key=f"fold-{g.key}", on_click=_fold, args=(g.title, keep)
            )
        rows = [(i, b) for i, b in g.rows if not is_folded or i == sel]
        for iid, body in rows:
            _pick("wl", iid, body, names.get(iid, iid))
        if g.proxies:
            opened = any(iid == sel for iid, _ in g.proxies)
            if is_folded:
                for iid, body in g.proxies:
                    if iid == sel:
                        _pick("wl", iid, body, names.get(iid, iid))
                continue
            with st.expander(g.fold_label, expanded=opened):
                for iid, body in g.proxies:
                    _pick("wl", iid, body, names.get(iid, iid))


def _mode() -> str:
    """The watchlist switch (P-024): "Unusual first" by default, kept for the session."""
    options = (panels.UNUSUAL, panels.BY_GROUP)
    _restore(MODE_KEY, KEEP_MODE, options, panels.UNUSUAL)
    with st.container(key="db-wl-switch"):
        mode = st.segmented_control(
            "Watchlist order", list(options), key=MODE_KEY, label_visibility="collapsed",
            on_change=_keep, args=(MODE_KEY, KEEP_MODE),
        ) or panels.UNUSUAL  # fmt: skip
    st.session_state[KEEP_MODE] = mode
    return str(mode)


def _watchlist(ov: data.Overview, query: str, sel: str, window: int = 30) -> None:
    names = _names(ov)
    _html(st, panels.label("Watchlist", f"σ vs last {window} moves"))
    mode = panels.BY_GROUP if query else _mode()
    sigs = panels.sigmas(ov)
    if not query and mode == panels.UNUSUAL:
        _html(st, '<div class="db-wl-head db-wl-head-mv" aria-hidden="true"><i></i>'
                  "<span>Chg</span><span>σ</span></div>")  # fmt: skip
        movers = panels.movers_group(ov, sel)
        since = brief.when(ov.previous_brief)
        aside = f"since {since} brief" if since else ""
        _html(
            st,
            panels.group_title(f"{movers.title} · {len(movers)}")
            + (f'<div class="db-note db-wl-since">{escape(aside)}</div>' if aside else ""),
        )
        if not movers.rows:
            _html(st, '<div class="db-empty">Nothing moved more than usual.</div>')
        for iid, body in movers.rows:
            _pick("wl", iid, body, names.get(iid, iid))
        rest = panels.without(ov.sections, movers.ids)
        n_rest = sum(len(s.rows) for s in rest)
        _html(st, panels.group_title(f"Everything else · {n_rest}"))
        groups = panels.watchlist(rest, ov.as_of, sel, sigs=sigs)
        _groups(groups, names, sel, start_folded=True)
        return
    _html(st, '<div class="db-wl-head" aria-hidden="true"><i></i><span>Last</span>'
              "<span>Chg</span></div>")  # fmt: skip
    if not query:
        _groups(panels.watchlist(ov.sections, ov.as_of, sel, sigs=sigs), names, sel)
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
    groups = panels.watchlist(found, ov.as_of, sel, fold_proxies=False, sigs=sigs)
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
) -> tuple[str, bool]:  # fmt: skip
    """The chart itself; returns the footer line and whether the TradingView chart drew (its
    attribution then goes at the right of the horizon row)."""
    name = panels.short(row)
    view = chart.window(bars, rng)
    if vs != NO_COMPARE:
        other_name = others[vs].split(" · ")[0]
        other = chart.window(data.load_bars(config, vs, hidden), rng)
        series = chart.tv_compare_series(view, other, name, other_name)
        if series and series[0]["points"]:
            drawn = tvchart.chart(
                series, key="db-tv", height=CHART_HEIGHT, precision=2,
                label=f"{name} and {other_name} rebased to 100", with_attribution=False,
            )  # fmt: skip
            return (
                f"Both series set to 100 on the first common day; dashed: {others[vs]}. "
                f"{name}: {chart.caption(view, latest_line=False)} "
                f"{other_name}: {chart.caption(other, latest_line=False)}"
            ), drawn
        _html(st, f'<div class="db-empty">No common days with {escape(other_name)} in this '
                  "range; showing this instrument alone.</div>")  # fmt: skip
    if len(view) < 2:
        _html(st, '<div class="db-empty">Fewer than two days in this range.</div>')
        return chart.caption(view, latest_line=False), False
    last = row.change.last.value if row.change is not None else None
    drawn = tvchart.chart(
        chart.tv_series(view, style=style), key="db-tv", height=CHART_HEIGHT,
        precision=chart.tv_precision(view), last=last, label=f"{name} price chart",
        with_attribution=False, markers=_filing_marker(config, row, view),
    )  # fmt: skip
    # the Altair fallback draws no latest-value line, so its footer does not mention one
    return chart.caption(view, latest_line=drawn and last is not None), drawn


def _is_stock(config: Config, iid: str) -> bool:
    """Single stocks only (P-024): ETFs, indices and futures never show the Company section."""
    return any(i.id == iid and i.kind == "stock" for i in config.instruments)


def _filing_marker(config: Config, row: Row, view: pd.DataFrame) -> list[tvchart.Marker]:
    """The latest 10-Q/10-K as a note on the chart at the first bar on or after its filed date
    (T67), when that bar is in the range shown."""
    if view.empty or not _is_stock(config, row.instrument_id):
        return []
    mark = company.marker(data.load_company(config, row.instrument_id))
    if mark is None:
        return []
    filed, text = mark
    bars = [d.date() for d in pd.to_datetime(view["d"])]
    at = next((d for d in bars if d >= filed), None)
    return [{"time": f"{at:%Y-%m-%d}", "text": text}] if at is not None else []


def _company(config: Config, row: Row) -> None:
    """The Company section under the hero, for single stocks only (T67)."""
    if not _is_stock(config, row.instrument_id):
        return
    co = data.load_company(config, row.instrument_id)
    with st.container(key="db-company"):
        _html(st, company.section_html(co, config.filings.insider_days))


@st.fragment
def _hero(config: Config, ov: data.Overview, row: Row, hidden: frozenset[int]) -> None:
    """The hero (P-023): label, figure, pill and provenance line, the controls, the chart, the
    1W-1Y horizon row with the TradingView attribution at its right (only when the TradingView
    chart drew) and the footer line naming what the chart shows."""
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
    _html(st, panels.chart_header(row, ov.as_of))
    rng, style, vs = _controls(row, bars, others)
    st.query_params["r"] = rng
    drawn = False
    if bars.empty:
        _html(st, '<div class="db-empty">No daily history for this instrument yet.</div>')
        foot = ""
    else:
        foot, drawn = _draw(config, row, bars, rng, style, vs, others, hidden)
    attr = tvchart.attribution_html() if drawn else ""
    hzs = tearsheet.horizons_html(tearsheet.horizons(row, bars), from_week=True)
    _html(st, f'<div class="db-under">{hzs}{attr}</div>')
    if foot:
        _html(st, f'<div class="db-chfoot">{escape(foot)}</div>')


# ---------------------------------------------------------------------------
# sources and risk under the chart, tabs under the main area
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


def _notes(config: Config, ov: data.Overview, row: Row, hidden: frozenset[int]) -> None:
    """The "Do the sources agree?" and "Risk and range" blocks side by side under the chart, as flat
    label-value rows (they stack below 640 px); the links sit in their own block under the
    range, so they never overlap its labels (T60 bug 1)."""
    iid = row.instrument_id
    ladder = instrument.ladder(config, iid, hidden, ov.as_of)
    with st.container(key="db-notes"):
        sources, risk = st.columns(2, gap="large")
        with sources, st.container(key="db-detail"):
            summary = tearsheet.xcheck_summary(config, iid, hidden, et_date(ov.as_of))
            _html(st, instrument.verdict_card_html(ladder, summary))
        with risk, st.container(key="db-risk"):
            bars = data.load_bars(config, iid, hidden)
            _html(st, panels.label("Risk and range") + tearsheet.risk_html(row, bars))
            with st.container(key="db-links"):
                if ladder and st.button(
                    "Lineage: source, fetch and raw file", key=f"src-{iid}", type="tertiary",
                ):  # fmt: skip
                    kit.details(
                        f"{panels.short(row)} · source and lineage",
                        instrument.lineage_html(config, ladder[0]),
                    )
                st.page_link(MACRO_PAGE, label=MACRO_LINK)


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Desk Brief", layout="wide", initial_sidebar_state="collapsed")
    theme.apply()
    _html(st, panels.HOME_CSS + brief.BRIEF_CSS + company.COMPANY_CSS)
    st.html(SLASH_JS, unsafe_allow_javascript=True)
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
    left, mid, right = st.columns([3, 3, 4], vertical_alignment="center")
    _html(left, panels.brand(ov))
    query = mid.text_input(
        "Search instruments", placeholder=SEARCH_HINT, label_visibility="collapsed", key="q"
    ).strip()
    with right:
        _health(ov, snap.last_run)
    sel = _selected(ov, query)
    if sel:
        st.query_params["id"] = sel
        st.session_state[KEEP_ID] = sel
    if query:
        st.query_params["q"] = query
    elif "q" in st.query_params:
        del st.query_params["q"]

    _strip(ov, sel)

    rows = _rows(ov)
    # three aligned panes (P-024). Source order = phone order: the brief, the watchlist, the
    # selected market; on a wide screen CSS places them watchlist, market, brief
    with st.container(key="db-main"):
        brief_col, watch_col, hero_col = st.columns(3, gap="large")
        with brief_col, st.container(key="db-brief"):
            pane = brief.pane(ov.read, ov.caveats, ov.as_of, ov.previous_brief, ov.note, ov.today)
            _html(st, pane)
        with watch_col, st.container(key="db-watch"):
            _watchlist(ov, query, sel, config.checks.anomaly.window_days)
        with hero_col, st.container(key="db-hero"):
            if sel:
                _hero(config, ov, rows[sel], hidden)
                _company(config, rows[sel])
                _notes(config, ov, rows[sel], hidden)
            else:
                _html(st, '<div class="db-empty">No instruments to chart yet.</div>')
    if sel:
        with st.container(key="db-values"):
            _tabs(config, ov, rows[sel], hidden)

    _html(st, panels.footer(ov, kit.glyph_legend(hidden)))


main()
