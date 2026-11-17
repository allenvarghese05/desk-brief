"""Dashboard look (spec 10.2, minimalist-ui): warm neutral text, no gradients, no shadows, no card
borders. One muted green / rust pair marks direction; red is reserved for check failures and
unavailable values are grey (there is no stale greying yet).
Numbers are tabular and right-aligned.
"""

from __future__ import annotations

import streamlit as st

INK = "#1f2421"
MUTED = "#7b807c"
RULE = "#e4e1db"
UP = "#4f7a5a"  # direction: muted green
DOWN = "#9a5b4f"  # direction: muted rust (not the failure red)
FAIL = "#b3261e"  # check failures only
WARN = "#8a6d1f"

CSS = f"""
<style>
.block-container {{ padding-top: 2rem; max-width: 1100px; }}
.db-top {{ display: flex; gap: 1.5rem; align-items: baseline; flex-wrap: wrap;
  border-bottom: 1px solid {RULE}; padding-bottom: .6rem; margin-bottom: 1rem; color: {INK}; }}
.db-brand {{ font-size: 1.25rem; font-weight: 600; margin-right: auto; }}
.db-health-ok {{ color: {UP}; }} .db-health-warn {{ color: {WARN}; }}
.db-health-fail {{ color: {FAIL}; }} .db-health-unknown {{ color: {MUTED}; }}
.db-mode, .db-meta, .db-tag, .db-note {{ color: {MUTED}; font-size: .8rem; }}
.db-tag {{ border: 1px solid {RULE}; border-radius: 3px; padding: 0 .3rem; margin-left: .3rem; }}
.db-strip {{ display: flex; gap: 2rem; flex-wrap: wrap; padding: .5rem 0 1rem 0;
  border-bottom: 1px solid {RULE}; margin-bottom: 1rem; }}
.db-hl-label {{ display: block; color: {MUTED}; font-size: .75rem; letter-spacing: .04em; }}
.db-table {{ width: 100%; border-collapse: collapse; font-size: .92rem; }}
.db-table th {{ text-align: left; font-weight: 500; color: {MUTED}; font-size: .75rem;
  border-bottom: 1px solid {RULE}; padding: .3rem .5rem; }}
.db-table td {{ padding: .35rem .5rem; border-bottom: 1px solid {RULE}; vertical-align: top; }}
.db-table .num, .db-fig {{ text-align: right; font-variant-numeric: tabular-nums; }}
.db-fig {{ display: inline-block; }}
.db-val {{ display: block; font-weight: 500; }}
.db-meta {{ display: block; white-space: nowrap; }}
.db-up {{ color: {UP}; }} .db-down {{ color: {DOWN}; }}
.db-name-proxy {{ font-style: italic; }}
.db-unavailable {{ color: {MUTED}; font-style: italic; }}
.db-spark {{ display: block; stroke: {MUTED}; fill: none; stroke-width: 1.2; }}
</style>
"""


def apply() -> None:
    """Inject the stylesheet once per run."""
    st.markdown(CSS, unsafe_allow_html=True)
