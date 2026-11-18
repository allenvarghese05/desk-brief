"""Desk Brief dashboard: Overview (spec 10.3, page 1).

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
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):  # `app` is a plain folder; src/ covers a non-installed checkout
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app import theme  # noqa: E402
from app.components import data, panels  # noqa: E402

from deskbrief.config import Config, load_config  # noqa: E402
from deskbrief.store.backend import StorageError  # noqa: E402


@st.cache_resource
def _config(path: str) -> Config:
    return load_config(path)


#: where each section sits; anything not listed follows in rows of three
GRID = (
    ("Rates", "__curve__"),
    ("FX", "Commodities", "Equities"),
    ("Global", "Volatility", "Macro"),
)
SEARCH_HINT = "Search: 10y, gold, AAPL"


def _html(target: object, body: str) -> None:
    target.markdown(body, unsafe_allow_html=True)  # type: ignore[attr-defined]


def _grid(ov: data.Overview) -> None:
    by_title = {s.title: s for s in ov.sections}
    layout = [list(row) for row in GRID]
    rest = [t for t in by_title if not any(t in row for row in GRID)]
    layout += [rest[i : i + 3] for i in range(0, len(rest), 3)]
    for row in layout:
        cells = [c for c in row if c == "__curve__" or c in by_title]
        if not cells or cells == ["__curve__"]:
            continue
        widths = [3, 2] if "__curve__" in cells else [1] * len(cells)
        cols = st.columns(widths, gap="large")
        for col, cell in zip(cols, cells, strict=False):
            if cell == "__curve__":
                _html(col, panels.yield_curve(ov))
            else:
                _html(col, panels.section(by_title[cell], ov.as_of))


def _results(ov: data.Overview, query: str) -> None:
    found = panels.search(ov.sections, query)
    n = sum(len(s.rows) for s in found)
    if not found:
        _html(
            st,
            f'<div class="db-empty">No instrument matches "{escape(query)}". '
            "Try a ticker (SPY, AAPL), a name (gold, yen) or a tenor (2y, 10y).</div>",
        )
        return
    if n == 1 and st.session_state.get("_jumped") != query:  # Enter on a single match opens it
        st.session_state["_jumped"] = query
        st.switch_page("pages/2_Instrument.py", query_params={"id": found[0].rows[0].instrument_id})
    noun = "match" if n == 1 else "matches"
    _html(st, f'<div class="db-note">{n} {noun} for "{escape(query)}"</div>')
    for sec in found:
        _html(st, panels.section(sec, ov.as_of, fold_proxies=False))


def main() -> None:
    st.set_page_config(page_title="Desk Brief", layout="wide")
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

    left, mid, right = st.columns([3.4, 4, 2.6], vertical_alignment="center")
    _html(left, panels.brand(ov))
    query = mid.text_input(
        "Search instruments", placeholder=SEARCH_HINT, label_visibility="collapsed", key="q"
    ).strip()
    _html(right, panels.status(ov))
    _html(st, '<hr class="db-rule">')
    _html(st, panels.tiles(ov))
    _html(st, panels.moves(ov))

    if query:
        _results(ov, query)
    else:
        _grid(ov)

    if ov.withheld:
        st.caption(
            f"{ov.withheld} instrument(s) withheld in {ov.mode} mode: live (tier 1) data is "
            "not shown here until provider display terms are verified."
        )
    panels.provenance_drawer(ov)
    with st.expander("Known limits"):
        for limit in ov.known_limits:
            st.markdown(f"- {limit.text}")


main()
