"""Dashboard look v2: refined dark (P-022, T57).

Near-black page, panels one shade lighter, a raised shade for hover and selection. Cards have
no borders and are told apart by shade; padding (24 px) is larger than the gaps between cards
(20 px). One blue accent, for the selected state and focus only. Muted green and red mean up
and down, and nothing else. A failed check is a ✕ and the word "failed" on an amber tint, a
warning is "warn" in the same amber family, and stale data is grey with a "stale" tag
(P-022 supersedes spec §10.2's "red reserved for check failures"). Four type sizes (40, 26, 14
and 12 px), Inter with tabular figures.

The tokens are mirrored in ``.streamlit/config.toml``. The older names (``CANVAS``, ``INK``,
``MUTED``, ``RULE``, ``GRID``, ``FAIL``, ``WARN``) stay as aliases for the pages that later
cards (T58, T59) restyle.
"""

from __future__ import annotations

import streamlit as st

# ---- design tokens (P-022) ----------------------------------------------------------------
PAGE = "#0B0D10"  # page background (config backgroundColor)
PANEL = "#13161B"  # cards (config secondaryBackgroundColor)
RAISED = "#181C22"  # hover, selection, controls inside a card
DIVIDER = "#22262E"  # hairlines between rows (never a card border)
TEXT = "#E8EAED"  # 15.2:1 on PANEL
SECONDARY = "#9AA0A6"  # 6.9:1 on PANEL
META = "#8A9099"  # meta text (as-of, field, kickers): 5.6:1 on PANEL, 4.7:1 on DIVIDER
UP = "#2EBD85"
DOWN = "#F6465D"
UP_TINT = "rgba(46, 189, 133, .13)"
DOWN_TINT = "rgba(246, 70, 93, .13)"
#: text of a "down" pill: DOWN on its tint is 3.9:1 on the hover shade, this is 4.8:1 (the
#: DOWN token itself is unchanged)
DOWN_PILL = "#FF6377"
HOVER = "#1D2229"  # an overview card under the pointer or selected (one step above RAISED)
ACCENT = "#5B9DFF"
ACCENT_TINT = "rgba(91, 157, 255, .14)"
AMBER = "#E6B450"  # failed and warn markers (with ✕ / "failed" and "warn", never colour alone)
AMBER_TINT = "rgba(230, 180, 80, .14)"
TOOLTIP = "#20252D"

RADIUS = 12  # px, cards
PAD = 24  # px, card padding
GAP = 20  # px, between cards
SIZE_HERO, SIZE_FIGURE, SIZE_BODY, SIZE_META = 40, 26, 14, 12  # px

SANS = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif'
MONO = '"JetBrains Mono", "SF Mono", ui-monospace, Menlo, monospace'

# ---- aliases for pages not yet on v2 (T58/T59 retire them) --------------------------------
CANVAS = PAGE
SURFACE = PANEL
RULE = DIVIDER
GRID = DIVIDER
INK = TEXT
MUTED = SECONDARY
FAIL = AMBER  # a failure is amber + ✕ + "failed"; red means "down" only (P-022)
WARN = AMBER
STALE = META

#: keyed Streamlit containers drawn as cards
CARDS = (
    "db-watch", "db-chart", "db-detail", "db-overview", "db-values",
    "db-mx-vix", "db-mx-curve", "db-mx-2s10s", "db-mx-gold", "db-mx-corr", "db-mx-cot",
)  # fmt: skip

_CARD_SEL = ", ".join(f".st-key-{k}" for k in CARDS)
_PICK = 'div[class*="st-key-db-pick-"]'

CSS = f"""
<style>
/* ---- page ---- */
.block-container {{ padding: 76px 28px 3rem 28px; max-width: 1600px; }}
header[data-testid="stHeader"] {{ background: {PAGE}; }}
/* no sidebar rule: st.navigation(position="top") draws no sidebar list on a wide screen, and
   on a phone Streamlit moves the page list into the sidebar, which must stay reachable */
.stMarkdown, .stMarkdown p, .stCaption {{ font-family: {SANS}; font-size: {SIZE_BODY}px; }}
.stCaption, [data-testid="stCaptionContainer"] {{ color: {META} !important;
  font-size: {SIZE_META}px !important; }}
.stMarkdown a, .stMarkdown a:hover {{ color: inherit !important;
  text-decoration: none !important; }}
[data-testid="stHorizontalBlock"] {{ gap: {GAP}px; }}

/* ---- cards: shade, radius and padding; no borders, no boxes inside boxes ---- */
{_CARD_SEL} {{ background: {PANEL}; border-radius: {RADIUS}px; padding: {PAD}px; }}

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
/* a compact change is a tinted pill */
.db-fig > .db-val:has(+ .db-of-chg) {{ display: inline-block; padding: 1px 7px;
  border-radius: 6px; font-weight: 500; background: {RAISED}; }}
.db-fig > .db-up:has(+ .db-of-chg) {{ background: {UP_TINT}; }}
.db-fig > .db-down:has(+ .db-of-chg) {{ background: {DOWN_TINT}; color: {DOWN_PILL}; }}
.db-fig .db-val:has(+ .db-prov) {{ display: inline; }}
.db-unavailable {{ color: {SECONDARY}; font-style: italic; }}
.db-sr {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
.db-tag, .db-flag {{ display: inline-block; background: {RAISED}; color: {SECONDARY};
  border-radius: 5px; padding: 0 6px; margin-left: 6px; font-size: 11px; font-style: normal;
  font-weight: 500; letter-spacing: .02em; vertical-align: 1px; }}
.db-flags {{ white-space: nowrap; }}

/* ---- compact source tag: a focusable tier glyph with a CSS tooltip (hover, focus, tap) ---- */
.db-prov {{ position: relative; display: inline-flex; align-items: center; justify-content: center;
  min-width: 18px; min-height: 18px; margin-left: 4px; color: {META}; font-size: 11px;
  cursor: help; vertical-align: 1px; border-radius: 4px; outline: none; z-index: 2; }}
.db-prov:hover, .db-prov:focus, .db-prov:focus-visible {{ color: {TEXT}; }}
.db-prov:focus-visible {{ box-shadow: 0 0 0 2px {ACCENT}; }}
.db-tip {{ display: none; position: absolute; left: 50%; bottom: calc(100% + 8px);
  transform: translateX(-50%); background: {TOOLTIP}; color: {TEXT}; border-radius: 8px;
  padding: 10px 12px; width: max-content; max-width: 280px; font: 400 {SIZE_META}px/1.5 {SANS};
  white-space: normal; text-align: left; z-index: 30; box-shadow: 0 8px 24px rgba(0,0,0,.4);
  pointer-events: none; }}
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
.db-mark {{ display: inline-block; border-radius: 5px; padding: 0 7px; font-size: {SIZE_META}px;
  font-weight: 500; white-space: nowrap; }}
.db-mark-fail {{ color: {AMBER}; background: {AMBER_TINT}; }}
.db-mark-warn {{ color: {AMBER}; background: {AMBER_TINT}; }}
.db-mark-pass {{ color: {UP}; }}
.db-mark-stale, .db-mark-skip {{ color: {META}; background: {RAISED}; }}
.db-st-pass {{ color: {UP}; }} .db-st-warn, .db-st-fail {{ color: {AMBER}; }}
.db-st-skip {{ color: {META}; }}

/* ---- top bar ---- */
.db-top {{ display: flex; align-items: baseline; gap: 12px; white-space: nowrap; color: {TEXT}; }}
.db-brand {{ font-size: 16px; font-weight: 600; letter-spacing: -.01em; }}
.db-when {{ color: {META}; font-size: 13px; font-variant-numeric: tabular-nums; }}
.db-status {{ display: flex; justify-content: flex-end; align-items: center; gap: 14px;
  flex-wrap: wrap; font-size: 13px; color: {SECONDARY}; }}
.db-health {{ white-space: nowrap; color: {SECONDARY}; }}
.db-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-right: 8px; vertical-align: 1px; }}
.db-dot-ok {{ background: {UP}; }} .db-dot-warn, .db-dot-fail {{ background: {AMBER}; }}
.db-dot-unknown {{ background: {META}; }}
.db-mode, .db-note {{ color: {META}; font-size: {SIZE_META}px; }}
.db-rule {{ border: 0; border-top: 1px solid {DIVIDER}; margin: 8px 0 16px 0; }}
div[data-testid="stTextInput"] input {{ font-family: {SANS}; font-size: 13px;
  background: {PANEL}; border: 0; border-radius: 10px; }}
div[data-testid="stTextInput"] > div {{ border: 0; }}

/* ---- selectable items: the markup draws, a transparent button on top does the click ---- */
{_PICK} {{ position: relative; gap: 0 !important; cursor: pointer; }}
{_PICK} [data-testid="stElementContainer"]:has([data-testid="stButton"]) {{
  position: absolute; inset: 0; z-index: 1; margin: 0; width: 100% !important; }}
{_PICK} [data-testid="stButton"], {_PICK} [data-testid="stButton"] button {{
  width: 100%; height: 100%; min-height: 0; padding: 0; margin: 0; }}
{_PICK} [data-testid="stButton"] button {{ opacity: 0; cursor: pointer; border: 0; }}
{_PICK}:has(button:focus-visible) {{ outline: 2px solid {ACCENT}; outline-offset: 2px;
  border-radius: {RADIUS}px; }}

/* ---- market strip ---- */
.st-key-db-tiles {{ gap: {GAP}px !important; margin-bottom: 4px; }}
.st-key-db-tiles > div {{ flex: 1 1 0; min-width: 150px; }}
.db-tile {{ position: relative; display: block; background: {PANEL}; border-radius: {RADIUS}px;
  padding: 18px 20px 16px 20px; color: {TEXT}; }}
{_PICK}:hover .db-tile, .db-tile.is-sel {{ background: {RAISED}; }}
.db-tile.is-sel {{ box-shadow: inset 0 0 0 1px rgba(91, 157, 255, .35); }}
.db-tile-label {{ color: {SECONDARY}; font-size: 12.5px; font-weight: 500; }}
.db-tile-value {{ font-size: {SIZE_FIGURE}px; font-weight: 500; letter-spacing: -.02em;
  white-space: nowrap; line-height: 1.25; margin-top: 2px; }}
.db-tile-row {{ display: flex; align-items: center; justify-content: space-between; gap: 8px;
  margin-top: 6px; }}
.db-tile-chg {{ font-size: 12.5px; }}
.db-tile .db-fig {{ display: block; text-align: left; }}
@media (max-width: 640px) {{ .st-key-db-tiles > div {{ min-width: 140px; }}
  .db-tile {{ padding: 14px 16px; }} .db-tile-value {{ font-size: 20px; }} }}

/* ---- biggest moves ---- */
.st-key-db-moves {{ gap: 6px 18px !important; align-items: baseline; margin-bottom: 8px; }}
.st-key-db-moves > div {{ flex: 0 0 auto !important; width: auto !important; }}
.db-moves-label, .db-sec-title, .db-wl-grp, .db-kicker {{ color: {META}; font-size: 11.5px;
  font-weight: 500; letter-spacing: .04em; text-transform: uppercase; }}
.db-move {{ white-space: nowrap; font-size: 13px; color: {TEXT}; padding: 2px 0; }}
.db-move b {{ font-weight: 500; margin-right: 6px; }}
.db-move .db-fig {{ text-align: left; }}

/* ---- watchlist ---- */
.st-key-db-watch [data-testid="stVerticalBlock"] {{ gap: 0; }}
.st-key-db-watch {{ padding: 12px 0; }}
.db-wl-grp {{ padding: 16px {PAD}px 6px {PAD}px; }}
.db-wl-row {{ position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto 76px;
  gap: 10px; align-items: center; padding: 9px {PAD}px; color: {TEXT}; font-size: 13.5px; }}
{_PICK}:hover .db-wl-row {{ background: {RAISED}; }}
.db-wl-row.is-sel {{ background: {RAISED}; }}
.db-wl-row.is-sel::before {{ content: ""; position: absolute; left: 0; top: 8px; bottom: 8px;
  width: 2px; background: {ACCENT}; }}
.db-wl-name b {{ display: block; font-weight: 500; }}
.db-wl-name small {{ display: block; color: {META}; font-size: {SIZE_META}px; line-height: 1.35;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.db-wl-val, .db-wl-chg {{ text-align: right; white-space: nowrap; }}
.db-wl-chg {{ font-size: 12px; }}
.db-wl-gap {{ grid-column: 2 / 4; text-align: right; }}
.st-key-db-watch [data-testid="stExpander"] details {{ border: 0; background: transparent; }}
.st-key-db-watch [data-testid="stExpander"] summary {{ color: {META}; font-size: 12.5px;
  padding: 8px {PAD}px; }}

/* ---- chart card ---- */
.db-ch {{ display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }}
.db-ch-name {{ font-size: 15px; font-weight: 500; color: {TEXT}; }}
.db-ch-sub {{ color: {META}; font-size: 13px; }}
.db-ch-value {{ font-size: {SIZE_HERO}px; font-weight: 500; letter-spacing: -.025em;
  line-height: 1.1; color: {TEXT}; }}
.db-ch-chg {{ font-size: 15px; font-weight: 500; }}
.db-ch .db-fig {{ display: inline-block; text-align: left; }}
.db-ch .db-meta {{ display: block; margin-top: 4px; }}
.db-ch-foot {{ color: {META}; font-size: 12.5px; margin-top: 4px; }}
div[data-testid="stVegaLiteChart"] {{ background: transparent; }}

/* ---- performance over horizons ---- */
.db-hzs {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); margin: 16px 0;
  border-top: 1px solid {DIVIDER}; border-bottom: 1px solid {DIVIDER}; }}
.db-hz {{ padding: 12px 14px; font-size: {SIZE_BODY}px; font-weight: 500;
  font-variant-numeric: tabular-nums; border-right: 1px solid {DIVIDER}; }}
.db-hz:last-child {{ border-right: 0; }}
.db-hz-label {{ display: block; color: {META}; font-size: 11.5px; font-weight: 500; }}
.db-hz .db-fig {{ display: block; text-align: left; }}
.db-hz .db-unavailable {{ font-size: {SIZE_META}px; font-weight: 400; }}
@media (max-width: 900px) {{ .db-hzs {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} }}

/* ---- every value we hold ---- */
.db-values td {{ font-size: 13px; }}
.db-values .db-fig {{ text-align: right; }}
.db-marks {{ letter-spacing: .15rem; font-weight: 500; }}
.db-gapline {{ border-bottom: 1px solid {DIVIDER}; padding: 0 0 6px 0; }}
.db-checks .db-gapline {{ display: block; }}

/* ---- overview: small multiples ---- */
.db-ov-sec {{ margin: 4px 0 12px 0; }}
.st-key-db-overview [data-testid="stVerticalBlock"] {{ gap: 8px; }}
div[class*="st-key-db-ovgrid-"] {{ gap: 10px !important; }}
div[class*="st-key-db-ovgrid-"] > div {{ flex: 0 0 172px; }}
.db-card {{ display: block; background: {RAISED}; border-radius: 10px; padding: 10px 12px 8px;
  color: {TEXT}; }}
{_PICK}:hover .db-card, .db-card.is-sel {{ background: {HOVER}; }}
.db-card.is-sel {{ box-shadow: inset 0 0 0 1px rgba(91, 157, 255, .35); }}
.db-card-top {{ display: flex; justify-content: space-between; align-items: baseline; gap: 6px;
  font-size: 13px; }}
.db-card-top b {{ font-weight: 500; }}
.db-card-val {{ font-size: 15px; font-weight: 500; font-variant-numeric: tabular-nums; }}
.db-card .db-fig {{ display: block; text-align: left; }}
.db-card .db-spark {{ margin-top: 4px; width: 100%; height: 34px; }}
.db-card .db-spark-area {{ fill: currentColor; opacity: .09; stroke: none; }}

/* ---- details ---- */
.db-dl {{ width: 100%; border-collapse: collapse; font-size: 13px; color: {TEXT}; }}
.db-dl th {{ text-align: left; font-weight: 400; color: {SECONDARY}; padding: 10px 0;
  border-bottom: 1px solid {DIVIDER}; white-space: nowrap; vertical-align: top; }}
.db-dl td {{ text-align: right; padding: 10px 0 10px 10px; border-bottom: 1px solid {DIVIDER};
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }}
.db-dl .db-fig {{ text-align: right; }}
.db-dl .db-meta {{ white-space: normal; line-height: 1.35; margin-top: 2px; }}
.db-range {{ position: relative; height: 4px; background: {RAISED}; border-radius: 2px;
  margin: 12px 0 8px 0; }}
.db-range span {{ position: absolute; top: -4px; width: 2px; height: 12px; background: {TEXT};
  border-radius: 1px; }}
.db-range-ends {{ display: flex; justify-content: space-between; color: {META};
  font-size: {SIZE_META}px; font-variant-numeric: tabular-nums; }}
.db-kicker {{ margin: 20px 0 8px 0; }}
.db-kicker:first-child {{ margin-top: 0; }}
.db-verdict {{ display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap;
  margin-top: 12px; padding: 10px 12px; border-radius: 8px; font-size: 13px; color: {TEXT};
  background: {RAISED}; }}
.db-verdict b {{ font-weight: 500; }}
.db-verdict-ok b {{ color: {UP}; }} .db-verdict-warn b, .db-verdict-fail b {{ color: {AMBER}; }}
.db-verdict-fail {{ background: {AMBER_TINT}; }}
.db-verdict-muted b {{ color: {SECONDARY}; }}
.db-checks {{ font-size: 13px; color: {TEXT}; }}
.db-checks div {{ display: flex; justify-content: space-between; gap: 8px; padding: 6px 0;
  border-bottom: 1px solid {DIVIDER}; }}

/* ---- tables ---- */
.db-table {{ width: 100%; border-collapse: collapse; font-size: 13px; color: {TEXT}; }}
.db-table th {{ text-align: left; font-weight: 500; color: {META}; font-size: {SIZE_META}px;
  border-bottom: 1px solid {DIVIDER}; padding: 10px 8px; }}
.db-table td {{ padding: 10px 8px; border-bottom: 1px solid {DIVIDER}; vertical-align: top; }}
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
.db-empty {{ color: {SECONDARY}; font-size: 13px; padding: 10px 0; }}

/* ---- yield curve ---- */
.db-curve {{ color: {ACCENT}; }}
.db-curve text {{ font-size: 11px; fill: {META}; font-family: {SANS}; }}
.db-curve .db-curve-grid {{ stroke: {DIVIDER}; }}
.db-curve .db-curve-now {{ stroke: {ACCENT}; stroke-width: 1.8; fill: none; }}
.db-curve .db-curve-then, .db-curve .db-curve-old {{ stroke: {SECONDARY}; stroke-width: 1.2;
  fill: none; stroke-dasharray: 4 3; }}
.db-curve .db-curve-old {{ stroke-dasharray: 1.5 3; opacity: .8; }}
.db-tenors {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 6px;
  margin-top: 8px; }}
.db-tenor {{ font-size: 13px; font-weight: 500; font-variant-numeric: tabular-nums; }}
.db-tenor .db-fig {{ display: block; text-align: left; }}
.db-curve-legend {{ display: flex; gap: 16px; color: {META}; font-size: {SIZE_META}px; }}

/* ---- Macro page (T54; restyled by T59) ---- */
.db-mx-words {{ color: {TEXT}; font-size: 13.5px; margin: 4px 0 8px 0; }}
.db-mx-strip {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
  gap: 8px; margin: 6px 0 8px 0; }}
.db-mx-cell {{ font-size: {SIZE_BODY}px; font-weight: 500; font-variant-numeric: tabular-nums; }}
.db-mx-cell .db-fig {{ display: block; text-align: left; }}
.db-mx-table td, .db-mx-heat td, .db-mx-heat th {{ white-space: nowrap; }}
.db-mx-scroll {{ overflow-x: auto; }}
.db-mx-heat {{ font-size: 12.5px; }}
.db-mx-heat td {{ text-align: center; }}
.db-mx-heat .db-fig {{ text-align: center; }}
.db-mx-diag {{ color: {META}; }}
.db-mx-cot-name {{ color: {TEXT}; font-weight: 500; font-size: 15px; }}
.db-short {{ background: {RAISED}; border-radius: 8px; padding: 10px 12px; margin: 4px 0; }}
</style>
"""


def apply() -> None:
    """Inject the stylesheet once per run."""
    st.markdown(CSS, unsafe_allow_html=True)
