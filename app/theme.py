"""Dashboard look: a dark trading terminal (spec 10.2; apple-design + minimalist-ui, T36).

Market first, plumbing second. Near-black canvas, slightly lifted panels, one hairline colour.
Colour carries meaning only: green / red for direction, amber for warnings, one blue accent for
the selection and focus. Figures are tabular and right-aligned. No gradients, no shadows, no
motion. Every text colour clears 4.5:1 on every surface it sits on (figures next to each token,
measured against PANEL, the darkest surface text sits on is CANVAS).
"""

from __future__ import annotations

import streamlit as st

CANVAS = "#0b0e13"  # page background (.streamlit/config.toml backgroundColor)
PANEL = "#11161d"  # panels, tiles (config secondaryBackgroundColor)
RAISED = "#171d26"  # hover and the selected watchlist row
RULE = "#222a35"  # hairlines and panel borders (not text)
GRID = "#1a2029"  # chart grid lines (not text)
SURFACE = PANEL  # kept for callers of the light theme's name
INK = "#e7eaf0"  # 15.1:1 on PANEL
MUTED = "#8d96a3"  # 6.1:1 on PANEL
UP = "#3fbf86"  # 7.8:1
DOWN = "#f0645c"  # 5.8:1
FAIL = "#ff6b62"  # check failures, 6.5:1
WARN = "#d9a93a"  # check warnings, 8.4:1
ACCENT = "#6ea8ff"  # selection and focus, 7.5:1

SANS = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", "Segoe UI", sans-serif'
MONO = '"SF Mono", ui-monospace, "JetBrains Mono", Menlo, monospace'

CSS = f"""
<style>
.block-container {{ padding: 1.1rem 1.6rem 3rem 1.6rem; max-width: 1760px; }}
header[data-testid="stHeader"] {{ background: transparent; }}
.stMarkdown, .stMarkdown p, .db-top, .db-tiles, .db-sec, .db-table, .db-wl {{
  font-family: {SANS}; }}
.stMarkdown a, .stMarkdown a:hover {{ color: inherit !important;
  text-decoration: none !important; }}

/* ---- top bar ---- */
.db-top {{ display: flex; align-items: baseline; gap: .9rem; white-space: nowrap; color: {INK}; }}
.db-brand {{ font-size: 1.15rem; font-weight: 650; letter-spacing: -.01em; }}
.db-top .db-mode {{ margin-left: auto; }}
.db-when {{ color: {MUTED}; font-size: .85rem; font-variant-numeric: tabular-nums; }}
.db-status {{ display: flex; justify-content: flex-end; align-items: baseline; gap: .8rem;
  flex-wrap: wrap; font-size: .8rem; }}
.stMarkdown a.db-health {{ color: {MUTED} !important; white-space: nowrap; }}
.stMarkdown a.db-health:hover {{ color: {INK} !important; }}
.db-dot {{ display: inline-block; width: .5rem; height: .5rem; border-radius: 50%;
  margin-right: .4rem; vertical-align: .05rem; }}
.db-dot-ok {{ background: {UP}; }} .db-dot-warn {{ background: {WARN}; }}
.db-dot-fail {{ background: {FAIL}; }} .db-dot-unknown {{ background: {MUTED}; }}
.db-mode, .db-meta, .db-tag, .db-note {{ color: {MUTED}; font-size: .78rem; }}
.db-rule {{ border: 0; border-top: 1px solid {RULE}; margin: .5rem 0 .8rem 0; }}
div[data-testid="stTextInput"] input {{ font-family: {SANS}; font-size: .9rem;
  background: {PANEL}; border-color: {RULE}; }}

/* ---- page navigation (top bar) ---- */
.db-nav {{ display: flex; gap: .2rem; flex-wrap: wrap; justify-content: flex-end;
  font-size: .8rem; margin-bottom: .25rem; }}
.stMarkdown a.db-nav-link {{ color: {MUTED} !important; padding: .2rem .55rem;
  border-radius: 6px; white-space: nowrap; }}
.stMarkdown a.db-nav-link:hover {{ color: {INK} !important; background: {RAISED}; }}
.db-nav-link.is-here {{ color: {INK}; background: {RAISED}; }}

/* ---- market strip ---- */
.db-tiles {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: .6rem;
  margin-bottom: .6rem; }}
@media (max-width: 900px) {{ .db-tiles {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
.db-tile {{ position: relative; display: block; background: {PANEL}; border: 1px solid {RULE};
  border-radius: 8px; padding: .65rem .85rem 1.6rem .85rem; color: {INK}; }}
.db-tile:hover, .db-tile.is-sel {{ border-color: {ACCENT}; }}
.db-tile-label {{ color: {MUTED}; font-size: .74rem; font-weight: 600; letter-spacing: .04em;
  text-transform: uppercase; }}
.db-tile-value {{ font-size: 1.45rem; font-weight: 600; letter-spacing: -.015em;
  font-variant-numeric: tabular-nums; line-height: 1.25; margin-top: .1rem; }}
.db-tile-row {{ display: flex; align-items: center; justify-content: space-between; gap: .5rem; }}
.db-tile-chg {{ font-size: .9rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
.db-tile .db-fig {{ display: block; text-align: left; }}
.db-tile .db-tagline {{ position: absolute; left: .85rem; bottom: .55rem; }}

/* ---- biggest moves ---- */
.db-moves {{ display: flex; gap: .6rem; align-items: baseline; flex-wrap: wrap;
  font-size: .88rem; color: {INK}; padding: .1rem 0 .7rem 0; }}
.db-moves-label, .db-sec-title, .db-wl-grp, .db-kicker {{ color: {MUTED}; font-size: .7rem;
  font-weight: 600; letter-spacing: .07em; text-transform: uppercase; }}
.db-moves-label {{ margin-right: .2rem; }}
.db-move {{ white-space: nowrap; }}
.db-move b {{ font-weight: 600; }}
.db-move + .db-move::before {{ content: "·"; color: {MUTED}; margin-right: .6rem; }}

/* ---- panels: keyed Streamlit containers ---- */
.st-key-db-watch, .st-key-db-chart, .st-key-db-detail, .st-key-db-overview {{
  background: {PANEL}; border: 1px solid {RULE}; border-radius: 10px; padding: .8rem .9rem; }}

/* ---- watchlist ---- */
.db-wl-grp {{ padding: .7rem .25rem .3rem .25rem; border-bottom: 1px solid {RULE}; }}
.db-wl-grp:first-child {{ padding-top: .1rem; }}
.db-wl-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto 4.4rem; gap: .5rem;
  align-items: start; padding: .45rem .35rem; border-bottom: 1px solid {RULE};
  border-left: 2px solid transparent; color: {INK}; font-size: .86rem; }}
.db-wl-row:hover {{ background: {RAISED}; }}
.db-wl-row.is-sel {{ background: {RAISED}; border-left-color: {ACCENT}; }}
.db-wl-name b {{ display: block; font-weight: 600; }}
.db-wl-name small {{ display: block; color: {MUTED}; font-size: .72rem; line-height: 1.3;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.db-wl-val, .db-wl-chg {{ text-align: right; font-variant-numeric: tabular-nums;
  white-space: nowrap; }}
.db-wl-chg {{ font-weight: 600; }}
.db-wl .db-val {{ font-weight: 500; }}
.db-wl-gap {{ grid-column: 2 / 4; text-align: right; }}
details.db-more summary {{ color: {MUTED}; font-size: .76rem; cursor: pointer; list-style: none;
  padding: .45rem .3rem; }}
details.db-more summary::-webkit-details-marker {{ display: none; }}
details.db-more summary::before {{ content: "+ "; }}
details.db-more[open] summary::before {{ content: "\\2212  "; }}

/* ---- chart header ---- */
.db-ch {{ display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap; }}
.db-ch-name {{ font-size: 1.05rem; font-weight: 600; color: {INK}; }}
.db-ch-sub {{ color: {MUTED}; font-size: .8rem; }}
.db-ch-value {{ font-size: 2rem; font-weight: 600; letter-spacing: -.02em;
  font-variant-numeric: tabular-nums; line-height: 1.15; color: {INK}; }}
.db-ch-chg {{ font-size: 1rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
.db-ch .db-fig {{ display: inline-block; text-align: left; }}
.db-ch .db-meta {{ display: block; margin-top: .15rem; }}
.db-ch-foot {{ color: {MUTED}; font-size: .76rem; margin-top: .2rem; }}
div[data-testid="stVegaLiteChart"] {{ background: transparent; }}

/* ---- overview: small multiples ---- */
.db-ov {{ display: flex; flex-wrap: wrap; gap: .4rem 1.1rem; }}
.db-ov-sec {{ flex: 0 1 auto; margin: .2rem 0 .5rem 0; max-width: 100%; }}
.db-ov-grid {{ display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .45rem; }}
.db-ov-grid .db-card {{ width: 172px; }}
.db-card {{ display: block; background: {CANVAS}; border: 1px solid {RULE}; border-radius: 8px;
  padding: .55rem .65rem .45rem .65rem; color: {INK}; }}
.db-card:hover, .db-card.is-sel {{ border-color: {ACCENT}; }}
.db-card-top {{ display: flex; justify-content: space-between; align-items: baseline; gap: .4rem;
  font-size: .8rem; }}
.db-card-top b {{ font-weight: 600; }}
.db-card-val {{ font-size: 1.02rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
.db-card .db-fig {{ display: block; text-align: left; }}
.db-card .db-spark {{ margin-top: .3rem; width: 100%; height: 34px; }}
.db-card .db-spark-area {{ fill: currentColor; opacity: .09; stroke: none; }}
.db-card .db-up, .db-card .db-down, .db-card .db-flat {{ white-space: nowrap; }}

/* ---- details panel ---- */
.db-dl {{ width: 100%; border-collapse: collapse; font-size: .84rem; color: {INK}; }}
.db-dl th {{ text-align: left; font-weight: 400; color: {MUTED}; padding: .38rem 0;
  border-bottom: 1px solid {RULE}; white-space: nowrap; vertical-align: top; }}
.db-dl td {{ text-align: right; padding: .38rem 0 .38rem .6rem; border-bottom: 1px solid {RULE};
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }}
.db-dl .db-fig {{ text-align: right; }}
.db-dl .db-meta {{ white-space: normal; line-height: 1.35; margin-top: .1rem; }}
.db-dl .db-tagline {{ white-space: normal; }}
.db-range {{ position: relative; height: 4px; background: {RULE}; border-radius: 2px;
  margin: .55rem 0 .2rem 0; }}
.db-range span {{ position: absolute; top: -3px; width: 2px; height: 10px; background: {INK}; }}
.db-range-ends {{ display: flex; justify-content: space-between; color: {MUTED};
  font-size: .72rem; font-variant-numeric: tabular-nums; }}
.db-kicker {{ margin: .9rem 0 .3rem 0; }}
.db-kicker:first-child {{ margin-top: 0; }}
.db-checks {{ font-size: .8rem; color: {INK}; }}
.db-checks div {{ display: flex; justify-content: space-between; gap: .5rem;
  padding: .28rem 0; border-bottom: 1px solid {RULE}; }}
.db-st-pass {{ color: {UP}; }} .db-st-warn {{ color: {WARN}; }}
.db-st-fail {{ color: {FAIL}; }} .db-st-skip {{ color: {MUTED}; }}

/* ---- tables (other pages, drawer) ---- */
.db-table {{ width: 100%; border-collapse: collapse; font-size: .88rem; color: {INK}; }}
.db-table th {{ text-align: left; font-weight: 500; color: {MUTED}; font-size: .75rem;
  border-bottom: 1px solid {RULE}; padding: .3rem .5rem; }}
.db-table td {{ padding: .45rem .5rem; border-bottom: 1px solid {RULE}; vertical-align: top; }}
.stMarkdown table.db-table, .stMarkdown .db-table th, .stMarkdown .db-table td,
.stMarkdown table.db-dl, .stMarkdown .db-dl th, .stMarkdown .db-dl td {{
  border-left: 0 !important; border-right: 0 !important; border-top: 0 !important; }}
.stMarkdown table.db-table, .stMarkdown table.db-dl {{ border: 0 !important; }}
.stMarkdown .db-table tr, .stMarkdown .db-dl tr {{ background: transparent !important; }}
.db-table .num, .db-fig {{ text-align: right; font-variant-numeric: tabular-nums; }}
.db-fig {{ display: inline-block; }}
.db-val {{ display: block; font-weight: 500; }}
.db-meta {{ display: block; white-space: nowrap; }}
.db-up {{ color: {UP}; }} .db-down {{ color: {DOWN}; }} .db-flat {{ color: {MUTED}; }}
.db-unavailable {{ color: {MUTED}; font-style: italic; }}
.db-spark {{ display: block; stroke: {MUTED}; fill: none; stroke-width: 1.3; color: {MUTED}; }}
.db-spark.db-up {{ stroke: {UP}; color: {UP}; }}
.db-spark.db-down {{ stroke: {DOWN}; color: {DOWN}; }}
.db-empty {{ color: {MUTED}; font-size: .86rem; padding: .6rem 0; }}
.db-tag {{ border: 1px solid {RULE}; border-radius: 3px; padding: 0 .3rem; margin-left: .3rem;
  font-size: .68rem; font-style: normal; font-weight: 400; }}

/* ---- compact source tag: glyph + time visible; field and source on hover and for readers */
.db-tagline {{ display: block; color: {MUTED}; font-size: .72rem; white-space: nowrap;
  font-variant-numeric: tabular-nums; font-weight: 400; }}
.db-sr {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}

/* ---- yield curve ---- */
.db-curve text {{ font-size: 11px; fill: {MUTED}; font-family: {SANS}; }}
.db-curve .db-curve-grid {{ stroke: {GRID}; }}
.db-curve .db-curve-now {{ stroke: {ACCENT}; stroke-width: 1.8; fill: none; }}
.db-curve .db-curve-then {{ stroke: {MUTED}; stroke-width: 1.2; fill: none;
  stroke-dasharray: 4 3; }}
.db-curve-legend {{ display: flex; gap: 1rem; color: {MUTED}; font-size: .76rem; }}
</style>
"""


def apply() -> None:
    """Inject the stylesheet once per run."""
    st.markdown(CSS, unsafe_allow_html=True)
