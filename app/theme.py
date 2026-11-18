"""Dashboard look (spec 10.2; apple-design + minimalist-ui review, T36).

Market first, plumbing second. Warm off-white canvas, near-black ink, one hairline rule colour.
Colour carries meaning only: muted green / rust for direction, red reserved for check failures,
grey for anything unavailable. Numbers are tabular and right-aligned. No gradients, no shadows,
no motion. Every text colour clears 4.5:1 on the canvas (figures next to each token).
"""

from __future__ import annotations

import streamlit as st

CANVAS = "#fbfaf8"  # page background (.streamlit/config.toml backgroundColor)
SURFACE = "#ffffff"  # headline tiles
INK = "#1f2421"  # 15.1:1 on CANVAS
MUTED = "#676b67"  # 5.2:1 on CANVAS (was #7b807c, 3.8:1)
RULE = "#e8e5df"  # hairlines and tile borders
UP = "#2f7046"  # direction up, 5.7:1
DOWN = "#a8473a"  # direction down, 5.6:1 (rust, not the failure red)
FAIL = "#b3261e"  # check failures only, 6.3:1
WARN = "#86681a"  # check warnings, 5.0:1

SANS = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", "Segoe UI", sans-serif'
MONO = '"SF Mono", ui-monospace, "JetBrains Mono", Menlo, monospace'

CSS = f"""
<style>
.block-container {{ padding-top: 3rem; padding-bottom: 4rem; max-width: 1280px; }}
.stMarkdown, .stMarkdown p, .db-top, .db-tiles, .db-sec, .db-table {{ font-family: {SANS}; }}

/* ---- top bar ---- */
.db-top {{ display: flex; align-items: baseline; gap: .9rem; flex-wrap: nowrap;
  white-space: nowrap; color: {INK}; }}
.db-brand {{ font-size: 1.3rem; font-weight: 650; letter-spacing: -.01em; }}
.db-top .db-mode {{ margin-left: auto; }}
.db-when {{ color: {MUTED}; font-size: .9rem; font-variant-numeric: tabular-nums; }}
.db-status {{ display: flex; justify-content: flex-end; align-items: baseline; gap: .8rem;
  flex-wrap: wrap; font-size: .85rem; padding-top: .45rem; }}
.db-health {{ text-decoration: none; color: {MUTED}; white-space: nowrap; }}
.db-health:hover {{ text-decoration: underline; }}
.db-dot {{ display: inline-block; width: .55rem; height: .55rem; border-radius: 50%;
  margin-right: .35rem; vertical-align: .05rem; }}
.db-dot-ok {{ background: {UP}; }} .db-dot-warn {{ background: {WARN}; }}
.db-dot-fail {{ background: {FAIL}; }} .db-dot-unknown {{ background: {MUTED}; }}
.db-health-ok, .db-health-warn, .db-health-fail, .db-health-unknown {{ color: {MUTED}; }}
.db-mode, .db-meta, .db-tag, .db-note {{ color: {MUTED}; font-size: .8rem; }}
.db-rule {{ border: 0; border-top: 1px solid {RULE}; margin: .4rem 0 1.1rem 0; }}

/* ---- headline tiles ---- */
.db-tiles {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: .75rem; margin-bottom: 1rem; }}
@media (max-width: 760px) {{ .db-tiles {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
/* Streamlit colours and underlines every link in markdown; ours are quiet */
.stMarkdown a.db-tile, .stMarkdown a.db-tile:hover, .stMarkdown .db-name a,
.stMarkdown a.db-health {{ color: {INK} !important; text-decoration: none !important; }}
.stMarkdown a.db-health {{ color: {MUTED} !important; }}
.stMarkdown .db-name a:hover, .stMarkdown a.db-health:hover {{
  text-decoration: underline !important; }}
.db-tile {{ background: {SURFACE}; border: 1px solid {RULE}; border-radius: 10px;
  padding: .85rem 1rem .75rem 1rem; color: {INK}; text-decoration: none; display: block; }}
.db-tile:hover {{ border-color: #d6d2ca; }}
.db-tile-label {{ color: {MUTED}; font-size: .8rem; font-weight: 500; }}
.db-tile-value {{ font-size: 1.55rem; font-weight: 600; letter-spacing: -.015em;
  font-variant-numeric: tabular-nums; line-height: 1.25; margin-top: .15rem; }}
.db-tile-row {{ display: flex; align-items: center; justify-content: space-between;
  gap: .5rem; margin-top: .2rem; }}
.db-tile-chg {{ font-size: .95rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
.db-tile {{ position: relative; }}
.db-tile .db-fig {{ display: block; text-align: left; }}
.db-tile {{ padding-bottom: 1.9rem; }}
.db-tile .db-tagline {{ position: absolute; left: 1rem; bottom: .7rem; }}

/* ---- the one-line market summary ---- */
.db-moves {{ display: flex; gap: .6rem; align-items: baseline; flex-wrap: wrap;
  font-size: .92rem; color: {INK}; padding: .2rem 0 1.1rem 0; }}
.db-moves-label {{ color: {MUTED}; font-size: .75rem; font-weight: 600;
  letter-spacing: .06em; text-transform: uppercase; margin-right: .2rem; }}
.db-move {{ white-space: nowrap; }}
.db-move + .db-move::before {{ content: "·"; color: {MUTED}; margin-right: .6rem; }}

/* ---- sections ---- */
.db-sec {{ margin-bottom: 1.6rem; }}
.db-sec-title {{ color: {MUTED}; font-size: .75rem; font-weight: 600; letter-spacing: .06em;
  text-transform: uppercase; border-bottom: 1px solid {RULE}; padding-bottom: .4rem;
  margin-bottom: .1rem; }}
.db-table {{ width: 100%; border-collapse: collapse; font-size: .92rem; color: {INK}; }}
.db-table th {{ text-align: left; font-weight: 500; color: {MUTED}; font-size: .75rem;
  border-bottom: 1px solid {RULE}; padding: .3rem .5rem; }}
.db-table td {{ padding: .45rem .5rem; border-bottom: 1px solid {RULE}; vertical-align: top; }}
.stMarkdown table.db-table, .stMarkdown .db-table th, .stMarkdown .db-table td {{
  border-left: 0 !important; border-right: 0 !important; border-top: 0 !important; }}
.stMarkdown table.db-table {{ border: 0 !important; }}
.stMarkdown .db-table tr {{ background: transparent !important; }}
.db-sec .db-table {{ table-layout: fixed; }}
.db-sec .db-table td {{ padding: .5rem .25rem; }}
.db-sec .db-table td.db-name {{ width: 46%; overflow: hidden; }}
.db-sec .db-table td.db-c {{ width: 26%; padding-left: .6rem; white-space: nowrap; }}
.db-sec .db-table td:first-child {{ padding-left: 0; }}
.db-sec .db-table td:last-child {{ padding-right: 0; }}
.db-table .num, .db-fig {{ text-align: right; font-variant-numeric: tabular-nums; }}
.db-fig {{ display: inline-block; }}
.db-val {{ display: block; font-weight: 500; }}
.db-meta {{ display: block; white-space: nowrap; }}
.db-up {{ color: {UP}; }} .db-down {{ color: {DOWN}; }} .db-flat {{ color: {MUTED}; }}
.db-name a {{ color: {INK}; text-decoration: none; font-weight: 600; }}
.db-name a:hover {{ text-decoration: underline; }}
.db-full {{ display: block; color: {MUTED}; font-size: .78rem; line-height: 1.3;
  overflow-wrap: anywhere; }}
.db-name-proxy {{ font-style: italic; }}
.db-unavailable {{ color: {MUTED}; font-style: italic; }}
.db-spark {{ display: block; stroke: {MUTED}; fill: none; stroke-width: 1.3; }}
.db-spark.db-up {{ stroke: {UP}; }} .db-spark.db-down {{ stroke: {DOWN}; }}
details.db-more {{ margin-top: .35rem; }}
details.db-more summary {{ color: {MUTED}; font-size: .8rem; cursor: pointer; list-style: none;
  padding: .2rem 0; }}
details.db-more summary::-webkit-details-marker {{ display: none; }}
details.db-more summary::before {{ content: "+ "; }}
details.db-more[open] summary::before {{ content: "\\2212  "; }}
.db-empty {{ color: {MUTED}; font-size: .9rem; padding: .6rem 0; }}

/* ---- compact source tag: glyph + time visible; field and source on hover and for readers */
.db-tagline {{ display: block; color: {MUTED}; font-size: .75rem; white-space: nowrap;
  font-variant-numeric: tabular-nums; }}
.db-sr {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
.db-tag {{ border: 1px solid {RULE}; border-radius: 3px; padding: 0 .3rem; margin-left: .3rem;
  font-size: .7rem; font-style: normal; }}

/* ---- yield curve ---- */
.db-curve text {{ font-size: 11px; fill: {MUTED}; font-family: {SANS}; }}
.db-curve .db-curve-now {{ stroke: {INK}; stroke-width: 1.8; fill: none; }}
.db-curve .db-curve-then {{ stroke: {MUTED}; stroke-width: 1.2; fill: none;
  stroke-dasharray: 4 3; }}
.db-curve-legend {{ display: flex; gap: 1rem; color: {MUTED}; font-size: .78rem; }}

/* ---- search ---- */
div[data-testid="stTextInput"] input {{ font-family: {SANS}; font-size: .95rem; }}
</style>
"""


def apply() -> None:
    """Inject the stylesheet once per run."""
    st.markdown(CSS, unsafe_allow_html=True)
