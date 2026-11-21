"""The Macro page (T54, P-021): the views a global-macro desk reads first.

* VIX term structure (9D, 30D, 3M, 6M) now, a week and a month ago, its shape in words,
* the Treasury curve's change by tenor against 1W, 1M and 1Y, and the 2s10s history,
* gold against the 10-year real (TIPS) yield,
* a 90-day correlation heatmap of daily changes,
* CFTC net speculative positioning with its weekly date and week-on-week change.

Provenance (spec 10.2, P-009, H9): every figure is printed through ``values.value_cell`` or
``change_cell`` (compact, P-022) with its source, tier, date and what it is. Inline SVG curves
are gridlines, lines and points only (T36's yield-curve rule). Time series are TradingView
charts (T59, P-022) built by :func:`tv_line`: every point carries field, tier and source for
the chart's tooltip, and each chart has the "Charts by TradingView" attribution and a caption
naming the series' sources, tiers and date span. The correlation heatmap stays an HTML table.
Bars come from :func:`app.components.data.load_series`, which never reads a hidden tier, so
hosted mode stays tier-1 free.

Navigation (T59): a table is one grid row per instrument (:class:`Table`), so the page can put
each row under an in-session button that opens the instrument on Markets
(``st.switch_page`` with ``query_params``); there is no ``<a target="_self">`` and no
``title`` attribute anywhere in this module's markup.

Short history: until the owner runs the two-year history load (T53), the store may hold only
days or weeks. A line needs :data:`MIN_DAILY` daily points (:data:`MIN_WEEKLY` weekly ones);
with fewer, the panel says how much is stored and draws nothing, rather than a misleading line.
A comparison whose date is before the first stored day says what it needs.
"""
# ruff: noqa: RUF001  (true minus sign is intentional output)

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from html import escape
from itertools import pairwise

import pandas as pd
from app import theme
from app.components import tvchart
from app.components.chart import tier_text
from app.components.data import CURVE_TENORS
from app.components.panels import SHORT, provenance_of
from app.components.values import (
    TIER_GLYPH,
    TIER_NAME,
    Provenance,
    change_cell,
    tier_glyph,
    value_cell,
)

from deskbrief.report.model import Row
from deskbrief.timeutil import to_et

_MINUS = "−"
#: fewest points before a history line is drawn
MIN_DAILY, MIN_WEEKLY = 20, 4
#: fewest common daily changes before a correlation is printed
MIN_CORR = 20
CORR_DAYS = 90
#: a comparison point may sit this many days before its target (weekends, holidays)
MAX_GAP_DAYS = 7
#: calendar days of bars the page reads: the two-year history load (T53) plus a margin
HISTORY_DAYS = 800
#: the page's chart range control: calendar days drawn (None: everything stored)
RANGES: dict[str, int | None] = {"3M": 92, "6M": 183, "1Y": 366, "2Y": 731, "All": None}
DEFAULT_RANGE = "2Y"
#: Markets keeps the selected instrument under this plain session-state key (``KEEP_ID`` in
#: ``app/views/markets.py``), so a name opened from Macro is still selected after a later visit
MARKETS_KEEP_ID = "db-keep-id"

#: VIX term structure: (instrument, tenor label, calendar days)
VIX_TERM = (("VIX9D", "9D", 9), ("VIX", "30D", 30), ("VIX3M", "3M", 91), ("VIX6M", "6M", 182))
_TENOR_YEARS = {"UST_3M": 0.25, "UST_2Y": 2, "UST_10Y": 10, "UST_20Y": 20, "UST_30Y": 30}
VIX_BACK = (("1W", 7), ("1M", 30))
CURVE_BACK = (("1W", 7), ("1M", 30), ("1Y", 365))
GOLD_IDS = ("GOLD_F", "GLD")  # futures first; the ETF when no futures history is stored
REAL_ID = "UST_10Y_REAL"
#: the correlation heatmap's instruments and short names (gold resolved like GOLD_IDS)
CORR_SET = (
    ("SPY", "SPY"),
    ("QQQ", "QQQ"),
    ("GOLD", "Gold"),
    ("UST_10Y", "10Y"),
    (REAL_ID, "10Y real"),
    ("USD_BROAD", "Dollar"),
    ("VIX", "VIX"),
    ("WTI", "WTI"),
    ("HY_OAS", "HY OAS"),
)
#: CFTC positioning: (net position id, open interest id, market)
COT = (
    ("COT_GC_NET", "COT_GC_OI", "Gold (COMEX)"),
    ("COT_ES_NET", "COT_ES_OI", "S&P 500 e-mini (CME)"),
    ("COT_ZN_NET", "COT_ZN_OI", "10-year note (CBOT)"),
)
#: every instrument the page reads bars for
ALL_IDS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [i for i, _, _ in VIX_TERM]
        + list(CURVE_TENORS)
        + [*GOLD_IDS, REAL_ID]
        + [i for i, _ in CORR_SET if i != "GOLD"]
        + [i for i, _, _ in COT]
    )
)
_WHAT = {"close": "close", "yield": "yield", "level": "level", "rate": "rate",
         "spread": "10Y minus 2Y yield spread, bp",
         "net_position": "net speculative position", "open_interest": "open interest"}  # fmt: skip
Bars = Mapping[str, pd.DataFrame]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def signed(x: float, decimals: int, suffix: str = "") -> str:
    """``+1.25`` or ``-0.40`` (printed with a true minus); zero has no sign."""
    r = round(x, decimals)
    sign = "+" if r > 0 else _MINUS if r < 0 else ""
    return f"{sign}{abs(r):,.{decimals}f}{suffix}"


@dataclass(frozen=True)
class Point:
    """One stored daily (or weekly) value: the bar's main value and where it came from."""

    d: date
    value: float
    tier: int
    source: str
    field: str
    proxy: bool = False  # the instrument is a proxy (e.g. SPY for the S&P 500, GLD for gold)


def point(bars: pd.DataFrame, on_or_before: date | None = None) -> Point | None:
    """The last bar (on or before a date). ``None`` when the store starts after that date or
    the nearest bar is more than :data:`MAX_GAP_DAYS` older: never a stand-in from far away."""
    if bars.empty:
        return None
    sel = bars
    if on_or_before is not None:
        sel = bars[bars["d"].dt.date <= on_or_before]
        if sel.empty:  # the store starts after that date
            return None
    r = sel.iloc[-1]
    p = Point(
        r["d"].date(), float(r["close"]), int(r["tier"]), str(r["source"]), str(r["field"]),
        _is_proxy(bars),
    )  # fmt: skip
    if on_or_before is not None and (on_or_before - p.d).days > MAX_GAP_DAYS:
        return None
    return p


def mark_proxies(bars: Bars, proxies: frozenset[str]) -> dict[str, pd.DataFrame]:
    """``bars`` with a ``proxy`` column on each proxy instrument's frame (copies; the cached
    frames are not changed), so every figure built from them says "proxy" (P-022)."""
    return {
        iid: f.assign(proxy=True) if iid in proxies and not f.empty else f
        for iid, f in bars.items()
    }


def _is_proxy(frame: pd.DataFrame) -> bool:
    return "proxy" in frame and not frame.empty and bool(frame["proxy"].any())


def _flags(proxy: bool) -> tuple[str, ...]:
    return ("proxy",) if proxy else ()


def _prov(p: Point, what: str | None = None) -> Provenance:
    return Provenance(
        p.source, p.tier, f"{p.d:%d %b %Y}", what or _WHAT.get(p.field, p.field),
        flags=_flags(p.proxy),
    )  # fmt: skip


def _pair_prov(now: Point, then: Point, what: str) -> Provenance:
    """A change between two stored values: both sources, the more live tier."""
    src = now.source if now.source == then.source else f"{now.source} vs {then.source}"
    return Provenance(
        src, min(now.tier, then.tier), f"{now.d:%d %b %Y}", what,
        flags=_flags(now.proxy or then.proxy),
    )  # fmt: skip


def _frames_prov(
    frames: Sequence[pd.DataFrame], what: str, *, end_only: bool = False
) -> Provenance:
    """A statistic over several series: every source, the most live tier, the date span.

    ``end_only`` shows only the last date as the as-of (a heatmap cell) and moves the span
    into the field text, which the hover and screen readers carry."""
    used = [f for f in frames if not f.empty]
    tiers = {int(t) for f in used for t in f["tier"]}
    sources = sorted({str(s) for f in used for s in f["source"]})
    a = min(f["d"].min() for f in used)
    b = max(f["d"].max() for f in used)
    if len(tiers) > 1:
        what += "; mixed tiers: " + ", ".join(TIER_NAME[t] for t in sorted(tiers, reverse=True))
    span = f"{a:%d %b} – {b:%d %b %Y}"
    flags = _flags(any(_is_proxy(f) for f in used))
    if end_only:
        return Provenance(
            ", ".join(sources), min(tiers), f"{b:%d %b}", f"{what}, {span}", flags=flags
        )
    return Provenance(", ".join(sources), min(tiers), span, what, flags=flags)


def _since(p: Point, q: Point, text: str, noun: str, unit: str) -> str:
    """A compact change cell: ``text`` is ``p`` minus ``q``, measured in ``unit``."""
    basis = f"{noun} of {q.d:%d %b %Y}"
    prov = _pair_prov(p, q, f"change in {unit} since the {basis}")
    return change_cell(text, prov, basis)


def _gap(text: str, why: str) -> str:
    """A missing figure: the short text on screen, the reason for screen readers (no
    ``title`` attribute, P-022)."""
    return (
        f'<span class="db-unavailable">{escape(text)}'
        f'<span class="db-sr"> ({escape(why)})</span></span>'
    )


def _need(label: str, days: int, bars: pd.DataFrame) -> str:
    held = 0 if bars.empty else (bars["d"].iloc[-1] - bars["d"].iloc[0]).days
    return _gap(f"needs {label}", f"needs {days} days of history, the store holds {held}")


def short_history(
    what: str, frames: Sequence[pd.DataFrame], need: int, unit: str, thing: str = "line"
) -> str:
    """The plain message drawn instead of a line when too little history is stored."""
    used = [f for f in frames if not f.empty]
    n = max((len(f) for f in used), default=0)
    span = ""
    if used:
        a = min(f["d"].min() for f in used)
        b = max(f["d"].max() for f in used)
        span = f" ({a:%d %b %Y} to {b:%d %b %Y})"
    return (
        f'<div class="db-empty db-short">{escape(what)}: the store holds {n} {unit}{span}. '
        f"A {thing} needs at least {need} {unit}, so none is drawn. The two-year history load "
        "fills this once it has run.</div>"
    )


def caption(what: str, frames: Sequence[pd.DataFrame]) -> str:
    """Chart caption: what is drawn, tiers, sources and the date span (spec 10.2)."""
    used = [f for f in frames if not f.empty]
    if not used:
        return ""
    tiers = sorted({int(t) for f in used for t in f["tier"]})
    sources = ", ".join(sorted({str(s) for f in used for s in f["source"]}))
    a = min(f["d"].min() for f in used)
    b = max(f["d"].max() for f in used)
    glyphs = " ".join(f"{TIER_GLYPH[t]} {TIER_NAME[t]}" for t in tiers)
    return f"{what} · {glyphs} · {sources} · {a.day} {a:%b %Y} to {b.day} {b:%b %Y}."


@dataclass(frozen=True)
class Table:
    """A table as grid rows, so each row can sit under its own in-session button.

    ``head`` is the header row's html; each row is ``(instrument id, name, html)``, with an
    empty id for a row that opens nothing (no data stored). ``cols`` is the number of figure
    columns after the name, which sets the grid."""

    cols: int
    head: str
    rows: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class Panel:
    """A Macro block: html above the table (words, curve, legend), the table, html below it
    (a figure strip or a note) and the chart caption."""

    top: str
    table: Table | None = None
    bottom: str = ""
    caption: str = ""


#: the Macro page's own rules (grid rows under in-session buttons, T59); tokens from theme
MACRO_CSS = f"""
<style>
.db-mx-grid {{ display: grid; align-items: center; gap: 8px;
  grid-template-columns: minmax(96px, 1.3fr) repeat(var(--db-cols), minmax(58px, 1fr));
  padding: 8px 6px; border-bottom: 1px solid {theme.DIVIDER}; font-size: 13px;
  color: {theme.TEXT}; }}
.db-mx-head {{ color: {theme.META}; font-size: {theme.SIZE_META}px; font-weight: 500;
  padding-top: 14px; }}
.db-mx-c {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
.db-mx-name {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.db-mx-name b {{ font-weight: 500; }}
.db-mx-c .db-fig {{ text-align: right; }}
.db-mx-heatc {{ display: block; border-radius: 4px; padding: 2px 4px; text-align: center; }}
/* a proxy's caveat word sits on its own line inside the tinted cell, never spilling out */
.db-mx-heatc .db-flags {{ display: block; line-height: 1.2; }}
.db-mx-heatc .db-flag {{ margin-left: 0; }}
div[class*="st-key-db-pick-mx"]:hover .db-mx-row {{ background: {theme.RAISED}; }}
div[class*="st-key-db-mx-tbl-"] {{ gap: 0 !important; }}
/* phone: only the heatmap scrolls sideways; the other tables fit, with each figure's as-of
   in its tooltip (P-022 no longer asks for the full line under every figure) */
.st-key-db-mx-tbl-heat .db-mx-grid {{ grid-template-columns: minmax(84px, 1.2fr)
  repeat(var(--db-cols), minmax(58px, 1fr)); }}
@media (max-width: 900px) {{
  .st-key-db-mx-tbl-heat {{ overflow-x: auto; }}
  .st-key-db-mx-tbl-heat > div {{ min-width: 720px; }}
}}
@media (max-width: 640px) {{
  .db-mx-grid {{ grid-template-columns: minmax(64px, 1.1fr) repeat(var(--db-cols),
    minmax(0, 1fr)); gap: 4px; font-size: 12px; padding: 8px 0; }}
  .db-mx-c .db-asofline {{ display: none; }}
  .db-mx-c .db-prov {{ margin-left: 2px; }}
}}
</style>
"""


def grid_row(name: str, cells: Sequence[str], cols: int, *, head: bool = False) -> str:
    """One row: the name, then ``cols`` cells (a cell may be ``""``)."""
    tag = "db-mx-head" if head else "db-mx-row"
    body = "".join(f'<span class="db-mx-c">{c}</span>' for c in cells)
    return (
        f'<div class="db-mx-grid {tag}" style="--db-cols:{cols}">'
        f'<span class="db-mx-name">{name}</span>{body}</div>'
    )


def strip(items: Sequence[tuple[str, str]]) -> str:
    """Figures under a chart: ``(label, html)`` cells, each html a value cell or a gap."""
    cells = "".join(
        f'<div class="db-mx-cell"><span class="db-hz-label">{escape(label)}</span>{html}</div>'
        for label, html in items
    )
    return f'<div class="db-mx-strip">{cells}</div>'


Line = tuple[str, Sequence[float | None]]


def curve_svg(labels: Sequence[str], xs: Sequence[float], lines: Sequence[Line], aria: str) -> str:
    """Curves over tenors: gridlines, lines and points only, never a figure (T36 rule).

    ``xs`` are positions (e.g. log years); each line is ``(css class, values per tenor)``,
    ``None`` where a tenor has no value."""
    values = [v for _, vals in lines for v in vals if v is not None]
    lo, hi = min(values), max(values)
    pad = max((hi - lo) * 0.15, 0.05)
    lo, hi = lo - pad, hi + pad
    w, h, left, right, top, bottom = 420, 200, 22, 22, 12, 28
    x0, x1 = min(xs), max(xs)

    def x(v: float) -> float:
        return left + (v - x0) / ((x1 - x0) or 1) * (w - left - right)

    def y(v: float) -> float:
        return top + (hi - v) / ((hi - lo) or 1) * (h - top - bottom)

    grid = "".join(
        f'<line x1="{left}" x2="{w - right}" y1="{y(lo + (hi - lo) * i / 3):.1f}" '
        f'y2="{y(lo + (hi - lo) * i / 3):.1f}" class="db-curve-grid"/>'
        for i in range(4)
    )
    ticks = "".join(
        f'<text x="{x(xv):.1f}" y="{h - 9}" text-anchor="middle">{escape(lab)}</text>'
        for lab, xv in zip(labels, xs, strict=True)
    )
    body = ""
    for cls, vals in lines:
        pts = [(x(xv), y(v)) for xv, v in zip(xs, vals, strict=True) if v is not None]
        if len(pts) < 2:
            continue
        body += f'<polyline class="{cls}" points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in pts)}"/>'
        if cls == "db-curve-now":
            body += "".join(
                f'<circle cx="{a:.1f}" cy="{b:.1f}" r="2.6" fill="currentColor"/>' for a, b in pts
            )
    return (
        f'<svg class="db-curve" viewBox="0 0 {w} {h}" width="100%" role="img" '
        f'aria-label="{escape(aria)}" style="color:{theme.ACCENT}">{grid}{body}{ticks}</svg>'
    )


_LEGEND = {"db-curve-now": "—", "db-curve-then": "- - -", "db-curve-old": "···"}


def legend(items: Sequence[tuple[str, str]], tiers: set[int], sources: set[str]) -> str:
    spans = "".join(f"<span>{_LEGEND[cls]} {escape(text)}</span>" for cls, text in items)
    tier = " ".join(f"{tier_glyph(t)} {TIER_NAME[t]}" for t in sorted(tiers))
    return (
        f'<div class="db-curve-legend">{spans}<span>{tier} · '
        f"{escape(', '.join(sorted(sources)))}</span></div>"
    )


def _long(df: pd.DataFrame, field: str | None = None) -> pd.DataFrame:
    """Chart rows with the provenance columns a TradingView point needs; ``field`` names a
    derived series (the 2s10s spread), else each row's stored field is used."""
    out = df[["d", "close", "tier", "source", "field"]].rename(columns={"close": "value"}).copy()
    out["tier_text"] = out["tier"].map(tier_text)
    out["field_text"] = field or out["field"].map(lambda f: _WHAT.get(str(f), str(f)))
    if _is_proxy(df):  # the chart tooltip names the caveat too
        out["field_text"] = out["field_text"] + " (proxy)"
    return out


def window(df: pd.DataFrame, rng: str) -> pd.DataFrame:
    """The bars inside the page's chosen range, counted back from the last bar."""
    days = RANGES.get(rng)
    if df.empty or days is None:
        return df
    out: pd.DataFrame = df[df["d"] > df["d"].max() - timedelta(days=days)]
    return out


def tv_line(
    df: pd.DataFrame, name: str, *, field: str | None = None, color: str | None = None
) -> tvchart.Series:
    """One TradingView line: every point names its field, tier and source (P-009)."""
    pts, meta = tvchart.points(_long(df, field), "line", meta=("field_text", "tier_text", "source"))
    return {"kind": "line", "name": name, "color": color or theme.ACCENT, "points": pts,
            "meta": meta}  # fmt: skip


# ---------------------------------------------------------------------------
# VIX term structure
# ---------------------------------------------------------------------------


def vix_shape(levels: Mapping[str, float]) -> str:
    """The term structure in words (no figures: those are in the table)."""
    spot, three = levels.get("VIX"), levels.get("VIX3M")
    if spot is None or three is None:
        return "Shape needs both the 30-day VIX and the 3-month VIX3M."
    if three > spot:
        words = "Contango: 3-month implied volatility is above 30-day, the usual calm shape."
    elif three < spot:
        words = (
            "Backwardation: 30-day implied volatility is above 3-month, so the market pays "
            "more for near-term protection (a stress signal)."
        )
    else:
        words = "Flat: 30-day and 3-month implied volatility are level."
    nine = levels.get("VIX9D")
    if nine is not None and nine > spot:
        words += " The front end is inverted: 9-day is above 30-day."
    tenors = [levels[i] for i, _, _ in VIX_TERM if i in levels]
    if len(tenors) == len(VIX_TERM) and all(a < b for a, b in pairwise(tenors)):
        words += " Upward sloping across all four tenors."
    return words


def _shape_word(levels: Mapping[str, float]) -> str | None:
    spot, three = levels.get("VIX"), levels.get("VIX3M")
    if spot is None or three is None:
        return None
    return "contango" if three > spot else "backwardation" if three < spot else "flat"


def vix_term(bars: Bars) -> Panel:
    """The VIX term-structure panel."""
    frames = [bars.get(i, pd.DataFrame()) for i, _, _ in VIX_TERM]
    now = {i: point(f) for (i, _, _), f in zip(VIX_TERM, frames, strict=True)}
    have = [i for i, _, _ in VIX_TERM if now[i] is not None]
    if len(have) < 2:
        return Panel(
            '<div class="db-empty">Not enough Cboe volatility indices stored yet to draw the '
            "term structure (needs at least two of VIX9D, VIX, VIX3M, VIX6M).</div>"
        )
    anchor = max(now[i].d for i in have)  # type: ignore[union-attr]
    then = {
        (lab, i): point(bars[i], anchor - timedelta(days=days)) if i in have else None
        for lab, days in VIX_BACK
        for i, _, _ in VIX_TERM
    }
    xs = [math.log(days) for _, _, days in VIX_TERM]
    lines: list[tuple[str, list[float | None]]] = [
        ("db-curve-now", [now[i].value if now[i] else None for i, _, _ in VIX_TERM])  # type: ignore[union-attr]
    ]
    leg = [("db-curve-now", f"{anchor.day} {anchor:%b}")]
    for (lab, _days), cls in zip(VIX_BACK, ("db-curve-then", "db-curve-old"), strict=True):
        vals = [then[(lab, i)].value if then[(lab, i)] else None for i, _, _ in VIX_TERM]  # type: ignore[union-attr]
        if sum(v is not None for v in vals) >= 2:
            lines.append((cls, vals))
            leg.append((cls, f"{lab} ago"))
    svg = curve_svg(
        [t for _, t, _ in VIX_TERM],
        xs,
        lines,
        "VIX term structure now and earlier; each tenor's level is in the table below",
    )
    rows: list[tuple[str, str, str]] = []
    for (iid, tenor, _), frame in zip(VIX_TERM, frames, strict=True):
        p = now[iid]
        name = f"{tenor} · {SHORT.get(iid, iid)}"
        if p is None:
            rows.append(("", name, grid_row(escape(name), [_gap("no data", "nothing stored")], 3)))
            continue
        cells = [value_cell(f"{p.value:.2f}", _prov(p))]
        for lab, days in VIX_BACK:
            q = then[(lab, iid)]
            if q is None:
                cells.append(_need(lab, days, frame))
                continue
            cells.append(_since(p, q, signed(p.value - q.value, 2), "close", "points"))
        rows.append((iid, name, grid_row(escape(name), cells, 3)))
    table = Table(3, grid_row("Tenor", ["Level", "vs 1W", "vs 1M"], 3, head=True), tuple(rows))
    levels = {i: now[i].value for i in have}  # type: ignore[union-attr]
    words = vix_shape(levels)
    was = _shape_word({i: then[("1M", i)].value for i in have if then[("1M", i)]})  # type: ignore[union-attr]
    shape = _shape_word(levels)
    if was and shape and was != shape:
        words += f" A month ago it was in {was}."
    extra = ""
    if now["VIX"] and now["VIX3M"]:
        a, b = now["VIX3M"], now["VIX"]
        what = f"VIX3M minus VIX, points ({b.d:%d %b} closes)"
        p = Provenance(
            a.source if a.source == b.source else f"{a.source}, {b.source}",
            min(a.tier, b.tier),
            f"{min(a.d, b.d):%d %b %Y}",
            what,
        )
        extra = strip([("3M − 30D", value_cell(signed(a.value - b.value, 2), p))])
    used = [f for f in frames if not f.empty]
    leg_html = legend(
        leg, {int(t) for f in used for t in f["tier"]}, {str(s) for f in used for s in f["source"]}
    )
    cap = caption(
        f"Cboe implied volatility indices (daily closes), latest {anchor:%d %b %Y} against "
        "about 1 week and 1 month earlier",
        used,
    )
    return Panel(f'<div class="db-mx-words">{escape(words)}</div>{svg}{leg_html}', table, extra,
                 cap)  # fmt: skip


# ---------------------------------------------------------------------------
# Treasury curve change and 2s10s
# ---------------------------------------------------------------------------


def curve_words(chg: Mapping[str, float]) -> str:
    """Bull/bear and steepener/flattener over a month, from 2Y and 10Y changes (in bp)."""
    two, ten = chg.get("UST_2Y"), chg.get("UST_10Y")
    if two is None or ten is None:
        return ""
    slope = "steepened" if ten - two > 0 else "flattened" if ten - two < 0 else "kept its slope"
    level = "bull" if (two + ten) / 2 < 0 else "bear" if (two + ten) / 2 > 0 else ""
    joined = f"{level}-{slope}" if level and slope != "kept its slope" else slope
    lead = "yields fell" if level == "bull" else "yields rose" if level == "bear" else ""
    return f"Over the month the curve {joined}" + (f" ({lead})." if lead else ".")


def curve_change(bars: Bars) -> Panel:
    """The curve-change panel: curve now, 1M and 1Y ago, and bp by tenor."""
    tenors = [t for t in CURVE_TENORS if not bars.get(t, pd.DataFrame()).empty]
    if len(tenors) < 2:
        return Panel(
            '<div class="db-empty">Not enough Treasury yields stored yet to draw the curve.</div>'
        )
    now = {t: point(bars[t]) for t in tenors}
    anchor = max(p.d for p in now.values() if p)
    then = {
        (lab, t): point(bars[t], anchor - timedelta(days=days))
        for lab, days in CURVE_BACK
        for t in tenors
    }
    xs = [math.log(_TENOR_YEARS[t]) for t in tenors]
    lines: list[tuple[str, list[float | None]]] = [
        ("db-curve-now", [now[t].value if now[t] else None for t in tenors])  # type: ignore[union-attr]
    ]
    leg = [("db-curve-now", f"{anchor.day} {anchor:%b}")]
    for lab, cls in (("1M", "db-curve-then"), ("1Y", "db-curve-old")):
        vals = [then[(lab, t)].value if then[(lab, t)] else None for t in tenors]  # type: ignore[union-attr]
        if all(v is not None for v in vals):
            lines.append((cls, vals))
            leg.append((cls, f"{lab} ago"))
    svg = curve_svg(
        [SHORT[t] for t in tenors],
        xs,
        lines,
        "Treasury yield curve now and earlier; each tenor's yield and change is in the table",
    )
    rows: list[tuple[str, str, str]] = []
    month: dict[str, float] = {}
    for t in tenors:
        p = now[t]
        assert p is not None  # tenors hold only non-empty frames
        cells = [value_cell(f"{p.value:.2f}%", _prov(p, "yield"))]
        for lab, days in CURVE_BACK:
            q = then[(lab, t)]
            if q is None:
                cells.append(_need(lab, days, bars[t]))
                continue
            bp = (p.value - q.value) * 100
            if lab == "1M":
                month[t] = bp
            cells.append(_since(p, q, signed(bp, 0, "bp"), "yield", "basis points"))
        rows.append((t, SHORT[t], grid_row(escape(SHORT[t]), cells, 4)))
    head = grid_row("Tenor", ["Yield", *(f"vs {lab}" for lab, _ in CURVE_BACK)], 4, head=True)
    table = Table(4, head, tuple(rows))
    used = [bars[t] for t in tenors]
    leg_html = legend(
        leg, {int(x) for f in used for x in f["tier"]}, {str(s) for f in used for s in f["source"]}
    )
    words = curve_words(month)
    top = (f'<div class="db-mx-words">{escape(words)}</div>' if words else "") + svg + leg_html
    cap = caption(
        f"Treasury yields by tenor (daily), latest {anchor:%d %b %Y} against about 1 week, "
        "1 month and 1 year earlier; changes in basis points",
        used,
    )
    return Panel(top, table, "", cap)


def spread_2s10s(bars: Bars) -> pd.DataFrame:
    """10Y minus 2Y in basis points on the dates both are stored (bar-shaped frame)."""
    ten, two = bars.get("UST_10Y", pd.DataFrame()), bars.get("UST_2Y", pd.DataFrame())
    cols = ["d", "close", "tier", "source", "field"]
    if ten.empty or two.empty:
        return pd.DataFrame(columns=cols)
    m = ten[["d", "close", "tier", "source"]].merge(
        two[["d", "close", "tier", "source"]], on="d", suffixes=("_10", "_2")
    )
    if m.empty:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame(
        {
            "d": m["d"],
            "close": (m["close_10"] - m["close_2"]) * 100,
            "tier": m[["tier_10", "tier_2"]].min(axis=1),
            "source": [
                a if a == b else f"{a} + {b}"
                for a, b in zip(m["source_10"], m["source_2"], strict=True)
            ],
            "field": "spread",
        }
    )
    return out.reset_index(drop=True)


def spread_figures(spread: pd.DataFrame) -> str:
    """Latest 2s10s, its change over 1M and 1Y, and the sign in words."""
    p = point(spread)
    if p is None:
        return '<div class="db-empty">No common 2Y and 10Y dates stored yet.</div>'
    what = "10Y minus 2Y yield, bp"
    items = [("2s10s", value_cell(signed(p.value, 0, "bp"), _prov(p, what)))]
    for lab, days in (("vs 1M", 30), ("vs 1Y", 365)):
        q = point(spread, p.d - timedelta(days=days))
        if q is None:
            items.append((lab, _need(lab.split()[-1], days, spread)))
            continue
        text = signed(p.value - q.value, 0, "bp")
        items.append((lab, _since(p, q, text, "spread", "basis points")))
    state = (
        "Inverted: the 10-year yields less than the 2-year."
        if p.value < 0
        else "Positive: the 10-year yields more than the 2-year."
        if p.value > 0
        else "Flat: the 10-year and 2-year yield the same."
    )
    return f'<div class="db-mx-words">{state}</div>{strip(items)}'


# ---------------------------------------------------------------------------
# gold against the 10-year real yield
# ---------------------------------------------------------------------------


def gold_series(bars: Bars) -> tuple[str, pd.DataFrame]:
    """(instrument id, bars) of gold: futures when stored, else the GLD ETF."""
    for iid in GOLD_IDS:
        f = bars.get(iid, pd.DataFrame())
        if not f.empty:
            return iid, f
    return GOLD_IDS[0], pd.DataFrame()


def _diff(levels: pd.Series, pct_unit: bool) -> pd.Series:
    """Changes between consecutive levels: bp for yields and spreads (unit pct), log returns
    in % for positive prices and levels, plain differences for anything that can be zero or
    below."""
    s = levels.astype(float)
    if pct_unit:
        return s.diff().dropna() * 100
    if (s > 0).all():
        return s.apply(math.log).diff().dropna() * 100
    return s.diff().dropna()


def _pct_unit(bars: pd.DataFrame) -> bool:
    return "unit" in bars and not bars.empty and str(bars["unit"].iloc[-1]) == "pct"


def changes(bars: pd.DataFrame) -> pd.Series:
    """Daily changes of one series, indexed by date (see :func:`_diff`)."""
    if len(bars) < 2:
        return pd.Series(dtype=float)
    return _diff(bars.set_index("d")["close"], _pct_unit(bars))


def correlation(a: pd.DataFrame, b: pd.DataFrame, since: date) -> tuple[float, int] | None:
    """Correlation of daily changes on common dates on or after ``since``; ``None`` below
    :data:`MIN_CORR` common changes or when either side does not move.

    Levels are joined on their common dates first and only then differenced, so a day one
    series lacks never pairs its two-day change with the other's one-day change."""
    if len(a) < 2 or len(b) < 2:  # a single stored day has no change to correlate
        return None
    lv = a[["d", "close"]].merge(b[["d", "close"]], on="d", suffixes=("_a", "_b"))
    lv = lv.sort_values("d").set_index("d")
    if len(lv) < 2:
        return None
    m = pd.concat(
        [_diff(lv["close_a"], _pct_unit(a)), _diff(lv["close_b"], _pct_unit(b))], axis=1
    ).dropna()
    m = m[m.index >= pd.Timestamp(since)]
    if len(m) < MIN_CORR or m.iloc[:, 0].std() == 0 or m.iloc[:, 1].std() == 0:
        return None
    return float(m.iloc[:, 0].corr(m.iloc[:, 1])), len(m)


def _window(frame: pd.DataFrame, since: date) -> pd.DataFrame:
    out: pd.DataFrame = frame[frame["d"] >= pd.Timestamp(since)]
    return out


def gold_real(bars: Bars) -> tuple[str, pd.DataFrame, pd.DataFrame]:
    """(figures html, gold bars, real-yield bars) of the gold vs real-yield panel."""
    gid, gold = gold_series(bars)
    real = bars.get(REAL_ID, pd.DataFrame())
    gname = "Gold futures" if gid == "GOLD_F" else "GLD (gold ETF, proxy)"
    items: list[tuple[str, str]] = []
    pg, pr = point(gold), point(real)
    if pg is not None:
        items.append((gname, value_cell(f"{pg.value:,.2f}", _prov(pg))))
        q = point(gold, pg.d - timedelta(days=30))
        items.append(
            (
                "Gold vs 1M",
                _since(pg, q, signed((pg.value / q.value - 1) * 100, 1, "%"), "close", "percent")
                if q is not None and q.value
                else _need("1M", 30, gold),
            )
        )
    else:
        items.append(("Gold", _gap("no data", "no gold futures or GLD bars stored")))
    if pr is not None:
        items.append(("10Y real", value_cell(f"{pr.value:.2f}%", _prov(pr, "real yield"))))
        q = point(real, pr.d - timedelta(days=30))
        items.append(
            (
                "Real vs 1M",
                _since(pr, q, signed((pr.value - q.value) * 100, 0, "bp"), "real yield", "bp")
                if q is not None
                else _need("1M", 30, real),
            )
        )
    else:
        items.append(("10Y real", _gap("no data", "no DFII10 values stored")))
    words = ""
    if pg is not None and pr is not None:
        since = max(pg.d, pr.d) - timedelta(days=CORR_DAYS)
        c = correlation(gold, real, since)
        if c is None:
            why = f"needs {MIN_CORR} common daily changes"
            items.append(("90d corr", _gap(f"needs {MIN_CORR} days", why)))
        else:
            corr, n = c
            p = _frames_prov(
                [_window(gold, since), _window(real, since)],
                f"correlation of daily gold returns and real-yield changes, {n} common days",
            )
            items.append(("90d corr", value_cell(signed(corr, 2), p)))
            words = (
                "Moving in the usual opposite direction: gold rises when real yields fall."
                if corr <= -0.3
                else "Moving together, against the usual pattern (gold usually falls when "
                "real yields rise)."
                if corr >= 0.3
                else "Little relationship lately between gold and real yields."
            )
    html = (f'<div class="db-mx-words">{escape(words)}</div>' if words else "") + strip(items)
    return html, gold, real


# ---------------------------------------------------------------------------
# correlation heatmap
# ---------------------------------------------------------------------------


def _corr_frames(bars: Bars) -> list[tuple[str, str, pd.DataFrame]]:
    out = []
    for iid, name in CORR_SET:
        if iid == "GOLD":
            gid, frame = gold_series(bars)
            out.append((gid, name if gid == "GOLD_F" else "GLD", frame))  # GLD: say so
        else:
            out.append((iid, name, bars.get(iid, pd.DataFrame())))
    return out


def _corr_bg(c: float) -> str:
    """Diverging fill: blue for positive, red for negative, stronger with |c| (the sign is
    also printed, so colour is never the only cue)."""
    rgb = "110,168,255" if c >= 0 else "240,100,92"
    return f"background: rgba({rgb},{min(abs(c), 1.0) * 0.55:.2f})"


def heatmap(bars: Bars) -> Panel:
    """The 90-day correlation matrix of daily changes, one grid row per instrument."""
    series = [(i, n, f) for i, n, f in _corr_frames(bars) if not f.empty]
    if len(series) < 2:
        return Panel('<div class="db-empty">Not enough daily series stored for correlations.</div>')
    end = max(f["d"].max() for _, _, f in series).date()
    since = end - timedelta(days=CORR_DAYS)
    most = max(len(changes(_window(f, since))) for _, _, f in series)
    if most < MIN_CORR:
        return Panel(
            short_history(
                f"{CORR_DAYS}-day correlations need {MIN_CORR} common daily changes per pair",
                [_window(f, since) for _, _, f in series],
                MIN_CORR + 1,
                "days",
                "heatmap",
            )
        )
    cols = len(series)
    rows: list[tuple[str, str, str]] = []
    for ia, na, fa in series:
        cells = []
        for ib, _nb, fb in series:
            if ia == ib:
                cells.append(
                    '<span class="db-mx-diag">·<span class="db-sr"> same series</span></span>'
                )
                continue
            c = correlation(fa, fb, since)
            if c is None:
                cells.append(_gap("—", f"needs {MIN_CORR} common daily changes"))
                continue
            corr, n = c
            p = _frames_prov(
                [_window(fa, since), _window(fb, since)],
                f"{CORR_DAYS}-day correlation of daily changes, {na} vs {_nb}, {n} common days",
                end_only=True,
            )
            cells.append(
                f'<span class="db-mx-heatc" style="{_corr_bg(corr)}">'
                # every cell ends on the same day: the as-of is in its tooltip and the caption
                f"{value_cell(signed(corr, 2), p, show_as_of=False)}</span>"
            )
        rows.append((ia, na, grid_row(f"<b>{escape(na)}</b>", cells, cols)))
    head = grid_row("", [escape(n) for _, n, _ in series], cols, head=True)
    note = (
        '<div class="db-note">Daily changes: returns for prices and indices, basis-point moves '
        "for yields and spreads. Blue: move together; red: move opposite. Hover, focus or tap a "
        "cell's glyph for its sources and days; pick a row to open that instrument.</div>"
    )
    cap = caption(
        f"{CORR_DAYS}-day correlations of daily changes",
        [_window(f, since) for _, _, f in series],
    )
    return Panel("", Table(cols, head, tuple(rows)), note, cap)


# ---------------------------------------------------------------------------
# CFTC positioning
# ---------------------------------------------------------------------------


def cot_card(net: Row | None, oi: Row | None, market: str, now: datetime) -> str:
    """Latest net speculative position, its week-on-week change (the report's own
    ``change_text``, e.g. "+12,000 contracts") and open interest, with the report date."""
    head = f'<div class="db-mx-cot-name">{escape(market)}</div>'
    if net is None or not net.available:
        return head + '<div class="db-empty">No CFTC report stored yet.</div>'
    p = provenance_of(net, now)
    week = ""
    if net.change is not None:
        d = to_et(net.change.last.as_of)
        week = (
            f'<div class="db-note">Positions as of {d:%a %d %b %Y} '
            "(CFTC Commitments of Traders, released the Friday after)</div>"
        )
    # the caveat words (proxy, IEX, fallback) are printed once per strip, beside the first
    # figure that carries them (T59: they showed twice); a later figure keeps them in its
    # field text, so its tooltip and accessible label still say so
    shown: set[str] = set()

    def once(prov: Provenance) -> Provenance:
        again = [f for f in prov.flags if f in shown]
        shown.update(prov.flags)
        if not again:
            return prov
        rest = tuple(f for f in prov.flags if f not in again)
        return replace(prov, flags=rest, field=f"{prov.field} ({', '.join(again)}, as shown)")

    items = [("Net position", value_cell(net.value_text, once(p)))]
    items.append(
        (
            "Week on week",
            change_cell(net.change_text, once(p), net.basis)
            if net.change is not None and net.change_text not in ("", "n/a")
            else _gap("no earlier week", "only one weekly report stored"),
        )
    )
    if oi is not None and oi.available:
        items.append(("Open interest", value_cell(oi.value_text, once(provenance_of(oi, now)))))
    return head + week + strip(items)
