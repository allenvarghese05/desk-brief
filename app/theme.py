"""Dashboard look v3: editorial calm, no containers (P-023, T60), on the P-022 dark palette.

Blocks sit on the page background. A section opens with a small uppercase label over a hairline
rule (``.db-label``), and sections are separated by hairlines and whitespace: no panel
backgrounds, borders, radius or shadows on any block container (P-023 supersedes P-022's
"panels one shade lighter"). Only the elements P-023 lists may have a background of their own:
the hero change pill, the selected and hovered watchlist row, tooltips, popovers and dialogs,
the search field, the selected segment of a segmented control, inline markers and data
encodings (tier glyphs, sigma tags, status dots, check markers, stale tags, check squares,
chart canvases, the 52-week range track and marker), the selected-tab underline and the hairline
under the tab row, and keyboard focus indicators. ``tests/unit/app/conftest.py`` holds the
default-deny checker that enforces this on every stylesheet.

Four type sizes: the hero figure (42 px, 36 px on a phone), 15 px (strip values, the verdict
answer, the brand), 13 px (body, rows, tables) and 11.5 px (labels, qualifiers, meta).
Hierarchy comes from weight and colour; figures use tabular numerals.

Kept from P-022: one blue accent for the selected state and focus, muted green and red for up
and down only, a failed check is a ✕ and the word "failed" in amber, a warning "warn" in the
same amber, stale data grey with a "stale" tag. The colour tokens are mirrored in
``.streamlit/config.toml``. The older names (``CANVAS``, ``INK``, ``MUTED``, ``RULE``, ``GRID``,
``FAIL``, ``WARN``) stay as aliases used by the page helpers in ``app/components/kit.py``.
"""

from __future__ import annotations

import streamlit as st

# ---- design tokens (P-022 colours, P-023 type and geometry) -------------------------------
PAGE = "#0B0D10"  # page background (config backgroundColor); every block sits on it
PANEL = "#13161B"  # chart canvas tokens only (tvchart); never a block background (P-023)
RAISED = "#181C22"  # hover and selected row, search field, selected segment
DIVIDER = "#22262E"  # hairlines between rows and under section labels
HAIR = DIVIDER
TEXT = "#E8EAED"  # 16.0:1 on PAGE
SECONDARY = "#9AA0A6"  # 7.3:1 on PAGE
META = "#8A9099"  # meta text (as-of, field, labels): 6.0:1 on PAGE, 5.3:1 on RAISED
UP = "#2EBD85"
DOWN = "#F6465D"
UP_TINT = "rgba(46, 189, 133, .13)"
DOWN_TINT = "rgba(246, 70, 93, .13)"
#: text of the "down" hero pill: DOWN on its tint is 3.9:1, this is 4.8:1 (DOWN is unchanged)
DOWN_PILL = "#FF6377"
HOVER = RAISED  # a clickable row under the pointer
ACCENT = "#5B9DFF"
ACCENT_TINT = "rgba(91, 157, 255, .14)"
AMBER = "#E6B450"  # failed and warn markers (with ✕ / "failed" and "warn", never colour alone)
AMBER_TINT = "rgba(230, 180, 80, .14)"
TOOLTIP = "#20252D"

RADIUS = 6  # px: the few exempt elements (pill, selected row, search field, tooltips)
GAP = 48  # px, between the hero and the watchlist
#: the four type sizes (P-023); SIZE_FIGURE is the 15 px size under its older name
SIZE_HERO, SIZE_HERO_PHONE, SIZE_LG, SIZE_BODY, SIZE_META = 42, 36, 15, 13, 11.5
SIZE_FIGURE = SIZE_LG
TYPE_SIZES = (SIZE_HERO, SIZE_HERO_PHONE, SIZE_LG, SIZE_BODY, SIZE_META)

SANS = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif'
MONO = '"JetBrains Mono", "SF Mono", ui-monospace, Menlo, monospace'

# ---- aliases kept for kit's helpers (older names of the tokens above) ---------------------
CANVAS = PAGE
SURFACE = PAGE
RULE = DIVIDER
GRID = DIVIDER
INK = TEXT
MUTED = SECONDARY
FAIL = AMBER  # a failure is amber + ✕ + "failed"; red means "down" only (P-022)
WARN = AMBER
STALE = META

_PICK = 'div[class*="st-key-db-pick-"]'

CSS = f"""
<style>
/* ---- page ---- */
.block-container {{ padding: 72px 40px 56px 40px; max-width: 1480px; }}
header[data-testid="stHeader"] {{ background: {PAGE}; }}
/* no sidebar rule: st.navigation(position="top") draws no sidebar list on a wide screen, and
   on a phone Streamlit moves the page list into the sidebar, which must stay reachable */
.stMarkdown, .stMarkdown p, .stCaption {{ font-family: {SANS}; font-size: {SIZE_BODY}px; }}
.stCaption, [data-testid="stCaptionContainer"] {{ color: {META} !important;
  font-size: {SIZE_META}px !important; }}
.stMarkdown a, .stMarkdown a:hover {{ color: inherit !important;
  text-decoration: none !important; }}
@media (max-width: 640px) {{ .block-container {{ padding: 64px 18px 40px 18px; }} }}

/* ---- section label: small caps over a hairline (P-023), the same for every block ---- */
.db-label, .db-sec-title, .db-wl-grp, .db-kicker {{ color: {META}; font-size: {SIZE_META}px;
  font-weight: 600; letter-spacing: .09em; text-transform: uppercase; }}
.db-label {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
  border-top: 1px solid {DIVIDER}; padding-top: 10px; margin-bottom: 14px; }}
.db-label > span {{ font-weight: 400; letter-spacing: .01em; text-transform: none;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }}
.db-kicker {{ margin: 24px 0 10px 0; }}
.db-kicker:first-child {{ margin-top: 0; }}

/* ---- native chrome, flat (config.toml cannot reach these alone) ---- */
[data-testid="stExpander"] details {{ background: transparent; border: 0; border-radius: 0;
  box-shadow: none; }}
[data-testid="stExpander"] summary {{ background: transparent; border: 0; border-radius: 0;
  padding-left: 0; color: {META}; font-size: {SIZE_BODY}px; }}
[data-testid="stExpanderDetails"] {{ background: transparent; border: 0; border-radius: 0;
  padding-left: 0; padding-right: 0; }}
/* Streamlit 1.67 tabs (react-aria): stTab, stTabPanel, the selection indicator and the
   tab list's ::after hairline (checked against the rendered DOM) */
[data-testid="stTabs"] [data-testid="stTabPanel"] {{ background: transparent; border: 0;
  border-radius: 0; box-shadow: none; padding: 18px 0 0 0; }}
[data-testid="stTabs"] [role="tablist"] {{ background: transparent; gap: 28px; }}
[data-testid="stTabs"] [data-testid="stTab"] {{ background: transparent; padding: 0 0 11px 0; }}
[data-testid="stTabs"] [data-testid="stTab"] p {{ font-size: {SIZE_BODY}px; font-weight: 500; }}
[data-testid="stTabs"] .react-aria-SelectionIndicator {{ height: 1px; }}
/* the selected underline in the text colour: Streamlit's own
   ``.<cls>[data-selected] .react-aria-SelectionIndicator`` is (0,3,0), so this needs (0,4,0) */
[data-testid="stTabs"] [data-testid="stTab"][data-selected] .react-aria-SelectionIndicator {{
  background: {TEXT}; }}
[data-testid="stTabs"] [role="tablist"]::after {{ background: {DIVIDER}; height: 1px; }}
[data-testid="stDataFrame"] {{ border: 0; border-radius: 0; box-shadow: none; }}
/* a bordered st.container (border=True) draws no box; keyed db-* blocks set their own rules */
div[data-testid="stVerticalBlock"]:not([class*="st-key-db-"]) {{ border: 0; border-radius: 0;
  box-shadow: none; }}
div[data-testid="stVegaLiteChart"] {{ background: transparent; }}

/* ---- figures ---- */
.db-fig {{ display: inline-block; text-align: right; font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum"; }}
.db-val {{ display: block; font-weight: 500; }}
.db-meta {{ display: block; white-space: nowrap; color: {META}; font-size: {SIZE_META}px; }}
.db-asofline {{ display: block; color: {META}; font-size: {SIZE_META}px; font-weight: 400;
  white-space: nowrap; font-variant-numeric: tabular-nums; }}
.db-tagline {{ display: block; color: {META}; font-size: {SIZE_META}px; font-weight: 400;
  white-space: nowrap; font-variant-numeric: tabular-nums; }}
.db-up {{ color: {UP}; }} .db-down {{ color: {DOWN}; }} .db-flat {{ color: {SECONDARY}; }}
/* a change in a list is coloured text, never a pill (P-023: the hero pill is the only one) */
.db-fig .db-val:has(+ .db-prov) {{ display: inline; }}
.db-fig > .db-val:has(+ .db-of-chg) {{ display: inline; }}
.db-unavailable {{ color: {SECONDARY}; font-style: italic; }}
.db-sr {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
/* caveat words (proxy, IEX, fallback, HYPOTHETICAL): small qualifier text beside the figure */
.db-tag, .db-flag {{ display: inline; color: {META}; margin-left: 6px; font-size: {SIZE_META}px;
  font-style: normal; font-weight: 500; letter-spacing: .01em; }}
.db-flags {{ white-space: nowrap; }}

/* ---- compact source tag: a focusable tier glyph with a CSS tooltip (hover, focus, tap) ---- */
.db-prov {{ position: relative; display: inline-flex; align-items: center; justify-content: center;
  min-width: 18px; min-height: 18px; margin-left: 4px; color: {META}; font-size: {SIZE_META}px;
  cursor: help; vertical-align: 1px; border-radius: 4px; outline: none; z-index: 2; }}
.db-prov:hover, .db-prov:focus, .db-prov:focus-visible {{ color: {TEXT}; }}
.db-prov:focus-visible {{ box-shadow: 0 0 0 2px {ACCENT}; }}
.db-tip {{ display: none; position: absolute; left: 50%; bottom: calc(100% + 8px);
  transform: translateX(-50%); background: {TOOLTIP}; color: {TEXT}; border-radius: {RADIUS}px;
  padding: 10px 12px; width: max-content; max-width: 280px;
  font: 400 {SIZE_META}px/1.5 {SANS}; white-space: normal; text-align: left; z-index: 30;
  box-shadow: 0 8px 24px rgba(0,0,0,.4); pointer-events: none; text-transform: none;
  letter-spacing: 0; }}
.db-tip .db-src, .db-tip .db-asof {{ color: {SECONDARY}; }}
.db-prov:hover .db-tip, .db-prov:focus .db-tip, .db-prov:focus-within .db-tip,
.db-tipwrap:hover .db-tip, .db-tipwrap:focus .db-tip,
.st-key-db-pick-health:hover .db-tip {{ display: block; }}
.db-tipwrap {{ position: relative; outline: none; }}
.st-key-db-pick-health:has(button:focus-visible) .db-tip {{ display: block; }}
.db-glyphs {{ display: flex; gap: 16px; flex-wrap: wrap; color: {META};
  font-size: {SIZE_META}px; }}
.db-glyphs b {{ font-weight: 400; color: {SECONDARY}; margin-right: 4px; }}

/* ---- check markers (P-022): amber for failed and warn, grey for stale; never red ---- */
.db-mark {{ display: inline-block; border-radius: 4px; padding: 0 6px; font-size: {SIZE_META}px;
  font-weight: 500; white-space: nowrap; }}
.db-mark-fail {{ color: {AMBER}; background: {AMBER_TINT}; }}
.db-mark-warn {{ color: {AMBER}; background: {AMBER_TINT}; }}
.db-mark-pass {{ color: {UP}; }}
.db-mark-stale, .db-mark-skip {{ color: {META}; background: {RAISED}; }}
.db-st-pass {{ color: {UP}; }} .db-st-warn, .db-st-fail {{ color: {AMBER}; }}
.db-st-skip {{ color: {META}; }}

/* ---- top bar ---- */
.db-top {{ display: flex; align-items: baseline; gap: 12px; white-space: nowrap; color: {TEXT}; }}
.db-brand {{ font-size: {SIZE_LG}px; font-weight: 600; letter-spacing: -.01em; }}
.db-when {{ color: {META}; font-size: {SIZE_BODY}px; font-variant-numeric: tabular-nums; }}
.db-status {{ display: flex; justify-content: flex-end; align-items: center; gap: 14px;
  flex-wrap: wrap; font-size: {SIZE_BODY}px; color: {SECONDARY}; }}
.db-health {{ white-space: nowrap; color: {SECONDARY}; }}
.db-dot {{ display: inline-block; width: 6px; height: 6px; border-radius: 50%;
  margin-right: 8px; vertical-align: 2px; }}
.db-dot-ok {{ background: {UP}; }} .db-dot-warn, .db-dot-fail {{ background: {AMBER}; }}
.db-dot-unknown, .db-dot-muted {{ background: {META}; }}
.db-mode, .db-note {{ color: {META}; font-size: {SIZE_META}px; }}
.db-rule {{ border: 0; border-top: 1px solid {DIVIDER}; margin: 8px 0 16px 0; }}
div[data-testid="stTextInput"] input {{ font-family: {SANS}; font-size: {SIZE_BODY}px;
  background: {RAISED}; border: 0; border-radius: {RADIUS}px; }}
div[data-testid="stTextInput"] > div {{ border: 0; }}

/* ---- selectable items: the markup draws, a transparent button on top does the click ---- */
{_PICK} {{ position: relative; gap: 0 !important; cursor: pointer; }}
{_PICK} [data-testid="stElementContainer"]:has([data-testid="stButton"]) {{
  position: absolute; inset: 0; z-index: 1; margin: 0; width: 100% !important; }}
{_PICK} [data-testid="stButton"], {_PICK} [data-testid="stButton"] button {{
  width: 100%; height: 100%; min-height: 0; padding: 0; margin: 0; }}
{_PICK} [data-testid="stButton"] button {{ opacity: 0; cursor: pointer; border: 0; }}
/* Streamlit's markdown block pulls the next element up by 16 px; a row under its button
   must keep its full height, or rows overlap */
{_PICK} [data-testid="stMarkdownContainer"] {{ margin-bottom: 0; }}
{_PICK}:has(button:focus-visible) {{ outline: 2px solid {ACCENT}; outline-offset: 2px; }}

/* ---- performance over horizons: one flat row ---- */
.db-hzs {{ display: flex; align-items: baseline; gap: 8px 24px; flex-wrap: wrap; margin: 0; }}
.db-hz {{ display: flex; align-items: baseline; gap: 7px; font-size: {SIZE_BODY}px;
  font-weight: 500; font-variant-numeric: tabular-nums; }}
.db-hz-label {{ color: {META}; font-size: {SIZE_META}px; font-weight: 400; }}
.db-hz .db-fig {{ display: inline-block; text-align: left; }}
.db-hz .db-unavailable {{ font-size: {SIZE_META}px; font-weight: 400; }}

/* ---- every value we hold ---- */
.db-values td {{ font-size: {SIZE_BODY}px; }}
.db-values .db-fig {{ text-align: right; }}
.db-marks {{ letter-spacing: .15rem; font-weight: 500; }}
.db-gapline {{ border-bottom: 1px solid {DIVIDER}; padding: 0 0 6px 0; }}
.db-checks .db-gapline {{ display: block; }}

/* ---- details ---- */
.db-dl {{ width: 100%; border-collapse: collapse; font-size: {SIZE_BODY}px; color: {TEXT}; }}
.db-dl th {{ text-align: left; font-weight: 400; color: {SECONDARY}; padding: 9px 0;
  border-bottom: 1px solid {DIVIDER}; white-space: nowrap; vertical-align: top; }}
.db-dl td {{ text-align: right; padding: 9px 0 9px 10px; border-bottom: 1px solid {DIVIDER};
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }}
.db-dl .db-fig {{ text-align: right; }}
.db-dl .db-meta {{ white-space: normal; line-height: 1.35; margin-top: 2px; }}
.db-range {{ position: relative; height: 2px; background: {DIVIDER}; border-radius: 2px;
  margin: 14px 0 8px 0; }}
.db-range span {{ position: absolute; top: -5px; width: 2px; height: 12px; background: {TEXT};
  border-radius: 1px; }}
.db-range-ends {{ display: flex; justify-content: space-between; gap: 12px; color: {META};
  font-size: {SIZE_META}px; font-variant-numeric: tabular-nums; }}
.db-verdict {{ display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap;
  margin-top: 12px; font-size: {SIZE_BODY}px; color: {TEXT}; min-width: 0; }}
.db-verdict b {{ font-weight: 600; }}
.db-verdict-ok b {{ color: {UP}; }} .db-verdict-warn b, .db-verdict-fail b {{ color: {AMBER}; }}
.db-verdict-muted b {{ color: {SECONDARY}; }}
.db-checks {{ font-size: {SIZE_BODY}px; color: {TEXT}; }}
.db-checks div {{ display: flex; justify-content: space-between; gap: 8px; padding: 7px 0;
  border-bottom: 1px solid {DIVIDER}; }}

/* ---- tables: flat, hairline rows ---- */
.db-table {{ width: 100%; border-collapse: collapse; font-size: {SIZE_BODY}px; color: {TEXT}; }}
.db-table th {{ text-align: left; font-weight: 400; color: {META}; font-size: {SIZE_META}px;
  border-bottom: 1px solid {DIVIDER}; padding: 10px 8px 8px 0; }}
.db-table td {{ padding: 10px 8px 10px 0; border-bottom: 1px solid {DIVIDER};
  vertical-align: top; }}
.stMarkdown table.db-table, .stMarkdown .db-table th, .stMarkdown .db-table td,
.stMarkdown table.db-dl, .stMarkdown .db-dl th, .stMarkdown .db-dl td {{
  border-left: 0 !important; border-right: 0 !important; border-top: 0 !important; }}
.stMarkdown table.db-table, .stMarkdown table.db-dl {{ border: 0 !important; }}
.stMarkdown .db-table tr, .stMarkdown .db-dl tr {{ background: transparent !important; }}
.db-table .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.db-spark {{ display: block; stroke: {SECONDARY}; fill: none; stroke-width: 1.4;
  color: {SECONDARY}; }}
.db-spark.db-up {{ stroke: {UP}; color: {UP}; }}
.db-spark.db-down {{ stroke: {DOWN}; color: {DOWN}; }}
.db-empty {{ color: {SECONDARY}; font-size: {SIZE_BODY}px; padding: 10px 0; }}

/* ---- yield curve ---- */
.db-curve {{ color: {ACCENT}; }}
.db-curve text {{ font-size: {SIZE_META}px; fill: {META}; font-family: {SANS}; }}
.db-curve .db-curve-grid {{ stroke: {DIVIDER}; }}
.db-curve .db-curve-now {{ stroke: {ACCENT}; stroke-width: 1.8; fill: none; }}
.db-curve .db-curve-then, .db-curve .db-curve-old {{ stroke: {SECONDARY}; stroke-width: 1.2;
  fill: none; stroke-dasharray: 4 3; }}
.db-curve .db-curve-old {{ stroke-dasharray: 1.5 3; opacity: .8; }}
.db-tenors {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 6px;
  margin-top: 8px; }}
.db-tenor {{ font-size: {SIZE_BODY}px; font-weight: 500; font-variant-numeric: tabular-nums; }}
.db-tenor .db-fig {{ display: block; text-align: left; }}
.db-curve-legend {{ display: flex; gap: 16px; color: {META}; font-size: {SIZE_META}px; }}

/* ---- Macro page (T54; its table grid is ``macro.MACRO_CSS`` since T59) ---- */
.db-mx-words {{ color: {TEXT}; font-size: {SIZE_BODY}px; margin: 4px 0 8px 0; }}
.db-mx-strip {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
  gap: 8px; margin: 6px 0 8px 0; }}
.db-mx-cell {{ font-size: {SIZE_BODY}px; font-weight: 500; font-variant-numeric: tabular-nums; }}
.db-mx-cell .db-fig {{ display: block; text-align: left; }}
.db-mx-diag {{ color: {META}; }}
.db-mx-cot-name {{ color: {TEXT}; font-weight: 500; font-size: {SIZE_LG}px; }}
.db-short {{ border-top: 1px solid {DIVIDER}; padding: 10px 0; margin: 4px 0; }}
</style>
"""


def apply() -> None:
    """Inject the stylesheet once per run."""
    st.markdown(CSS, unsafe_allow_html=True)
