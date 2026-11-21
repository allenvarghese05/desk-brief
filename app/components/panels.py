"""Markets panels (T36 layout, T57 v2 look, T58 Home v2): top bar, headline strip, watchlist
with sigma tags, chart header, yield curve, the page footer and the Home stylesheet.

All text comes from ``BriefModel`` rows (labels, signed changes, as-of text); every number is
printed through ``values.value_cell`` / ``change_cell`` with compact provenance (a focusable
tier glyph with a CSS tooltip, P-022). The caveat words (proxy, IEX, fallback, HYPOTHETICAL)
are printed once beside each figure, never twice in one place.

Selection is in-session (T57): each selectable item is returned as ``(instrument id, html)``
and the page draws it under a transparent button whose callback sets ``st.query_params``
(``app/views/markets.py``). The markup itself holds no link, no ``target`` and no ``title``.

:data:`HOME_CSS` styles the T58 blocks (watchlist rows, verdict, risk facts, range bar, tabs,
footer) from the ``app/theme.py`` tokens; the page injects it after ``theme.apply()``.
"""
# ruff: noqa: RUF001  (the sigma sign is intentional output)

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape

import numpy as np
from app.components.data import Overview
from app.components.values import (
    TIER_NAME,
    Provenance,
    change_cell,
    direction,
    tier_glyph,
    value_cell,
)

from deskbrief.query import field_label
from deskbrief.report.model import SPREAD_ID, Row, Section
from deskbrief.timeutil import to_et

_BADGE = {"ok": "Healthy", "warn": "Warnings", "fail": "Failing checks", "unknown": "No checks yet"}

#: what a trader calls each instrument; anything not listed shows its id (SPY, AAPL, EURUSD)
SHORT = {
    "ES_F": "S&P fut",
    "UST_3M": "3M",
    "UST_2Y": "2Y",
    "UST_10Y": "10Y",
    "UST_20Y": "20Y",
    "UST_30Y": "30Y",
    SPREAD_ID: "2s10s",
    "GOLD_F": "Gold",
    "BRENT": "Brent",
    "N225": "Nikkei",
    "UNRATE": "Unemployment",
    "DFF": "Fed funds",
    "UST_10Y_REAL": "10Y real",
    "BEI_10Y": "10Y BEI",
    "USD_BROAD": "Broad USD",
    "HY_OAS": "HY OAS",
    "COT_GC_NET": "Gold COT net",
    "COT_ES_NET": "ES COT net",
    "COT_ZN_NET": "10Y note COT net",
    "COT_GC_OI": "Gold COT OI",
    "COT_ES_OI": "ES COT OI",
    "COT_ZN_OI": "10Y note COT OI",
}
_TENOR_YEARS = {"UST_3M": 0.25, "UST_2Y": 2, "UST_10Y": 10, "UST_20Y": 20, "UST_30Y": 30}
NO_CHANGE = "—"
NO_DATA = 'no data<span class="db-sr"> (data unavailable)</span>'
#: the instrument selected when the URL names none
DEFAULT_ID = "ES_F"


def short(row: Row) -> str:
    """The trader's name (10Y, Gold); a ticker (SPY, EURUSD) as it is; never a raw id with an
    underscore."""
    iid = row.instrument_id
    return SHORT.get(iid, iid.replace("_", " "))


def _age(delta_seconds: float) -> str:
    minutes = max(0, int(delta_seconds // 60))
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes}m ago"
    return f"{minutes // 60}h ago" if minutes < 2880 else f"{minutes // 1440}d ago"


def provenance_of(row: Row, now: datetime) -> Provenance:
    """Source, tier, as-of and field label of an available row (never invented)."""
    if row.tier is None:
        raise ValueError(f"{row.instrument_id} has no value, so no provenance")
    if row.change is None:  # only the derived 2s10s spread has no observed value behind it
        if row.instrument_id != SPREAD_ID:
            raise ValueError(f"{row.instrument_id} has a value but no observed change behind it")
        return Provenance("treasury (derived)", row.tier, row.as_of_text, "derived spread")
    last = row.change.last
    hover = f"{to_et(last.as_of):%d %b %H:%M} ET, {_age((now - last.as_of).total_seconds())}"
    return Provenance(last.source, row.tier, row.as_of_text, field_label(last), hover, flags(row))


def flags(row: Row) -> tuple[str, ...]:
    """The caveat words that stay visible beside the row's figures (P-022)."""
    out = ["proxy"] if row.is_proxy else []
    if any(q.startswith("IEX") for q in row.qualifiers):
        out.append("IEX")
    if any(q.startswith("fallback") for q in row.qualifiers):
        out.append("fallback")
    return tuple(out)


def _listed(text: str, p: Provenance) -> str:
    """A value in a Markets list: glyph and caveat words, as-of in the tooltip."""
    return value_cell(text, p, compact=True, show_as_of=False)


def _has_change(row: Row) -> bool:
    return row.change is not None and row.change_text not in ("n/a", "")


def _change(row: Row, p: Provenance) -> str:
    if not _has_change(row):
        return _no_change(NO_CHANGE)
    return change_cell(row.change_text, p, row.basis, compact=True, glyph=False)


def _no_change(text: str) -> str:
    return (
        f'<span class="db-flat">{text}'
        '<span class="db-sr"> (no earlier value to compare with)</span></span>'
    )


# ---------------------------------------------------------------------------
# top bar
# ---------------------------------------------------------------------------


def brand(ov: Overview) -> str:
    et = to_et(ov.as_of)
    return (
        '<div class="db-top"><span class="db-brand">Desk Brief</span>'
        f'<span class="db-when">{et:%a} {et.day} {et:%b} · {et:%H:%M} ET</span></div>'
    )


def status(ov: Overview) -> str:
    """Pipeline health as one status dot and word; the counts are in its CSS tooltip and for
    screen readers. The page puts a button over it that opens Data health in the same session.
    The display mode moved to the page footer (T58)."""
    s = ov.health.status
    text = escape(ov.health.text)
    return (
        '<div class="db-status">'
        f'<span class="db-health db-health-{s} db-tipwrap">'
        f'<span class="db-dot db-dot-{s}"></span>{_BADGE[s]}'
        f'<span class="db-sr"> · {text}</span>'
        f'<span class="db-tip" aria-hidden="true">{text}</span></span></div>'
    )


# ---------------------------------------------------------------------------
# market strip and biggest moves
# ---------------------------------------------------------------------------


def _spark(
    values: Sequence[float], cls: str, w: int = 72, h: int = 22, *, area: bool = False
) -> str:
    """Sparkline of the last official daily values (no axis, no numbers)."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = w / (len(values) - 1)
    pts = [(i * step, h - 2 - (v - lo) / span * (h - 4)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    fill = f'<polygon class="db-spark-area" points="0,{h} {line} {w},{h}"/>' if area else ""
    return (
        f'<svg class="db-spark {cls}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'preserveAspectRatio="none" aria-hidden="true">{fill}<polyline points="{line}"/></svg>'
    )


def _trend_cls(row: Row) -> str:
    return direction(row.change_text) if _has_change(row) else "db-flat"


Item = tuple[str, str]  # (instrument id, html of the selectable item)


def tile_items(ov: Overview, sel: str = "") -> list[Item]:
    """The headline strip: one tile per headline instrument."""
    items = []
    for label, row in ov.headline:
        if not row.available:
            body = f'<div class="db-tile-value db-unavailable">{NO_DATA}</div>'
        else:
            p = provenance_of(row, ov.as_of)
            body = (
                f'<div class="db-tile-value">{_listed(row.value_text, p)}</div>'
                f'<div class="db-tile-row"><span class="db-tile-chg">{_change(row, p)}</span>'
                f"{_spark(ov.sparks.get(row.instrument_id, ()), _trend_cls(row))}</div>"
            )
        cls = "db-tile is-sel" if row.instrument_id == sel else "db-tile"
        items.append(
            (
                row.instrument_id,
                f'<a class="{cls}" data-db-id="{escape(row.instrument_id)}">'
                f'<div class="db-tile-label">{escape(label)}'
                f'<span class="db-sr"> · {escape(row.name)}</span></div>{body}</a>',
            )
        )
    return items


_SIGMA = re.compile(r"is (\d+(?:\.\d+)?)σ")


def _across_gap(row: Row) -> bool:
    """The latest move spans a gap in the data (the tearsheet's rule: more than
    ``GAP_BDAYS`` business days between the two values), so no sigma is shown for it."""
    from app.components.tearsheet import GAP_BDAYS

    if row.change is None or row.change.previous is None:
        return False
    a, b = to_et(row.change.previous.as_of).date(), to_et(row.change.last.as_of).date()
    return int(np.busday_count(a, b)) > GAP_BDAYS


def sigmas(ov: Overview) -> dict[str, tuple[str, str]]:
    """The day's unusual moves as ``{instrument id: (sigma text, sentence for screen readers)}``.

    These are the report's own notable moves (at least the anomaly threshold away from each
    instrument's recent daily moves); P-022 shows them as a tag on the watchlist row. A move
    across a gap in the data gets no tag (the risk card says "no data" for it). The sentence
    names the instrument by its display name, never by its raw id."""
    rows = {r.instrument_id: r for s in ov.sections for r in s.rows}
    out: dict[str, tuple[str, str]] = {}
    for n in ov.moves:
        m = _SIGMA.search(n.text)
        iid = n.instrument_id
        if not iid or not m or iid in out:
            continue
        row = rows.get(iid)
        if row is not None and _across_gap(row):
            continue
        text = n.text.replace(iid, short(row)) if row is not None else n.text
        out[iid] = (f"{m.group(1)}σ", text)
    return out


def sigma_tag(sig: tuple[str, str] | None) -> str:
    if sig is None:
        return ""
    return (
        f'<span class="db-sig">{escape(sig[0])}<span class="db-sr"> (unusual move: '
        f"{escape(sig[1])})</span></span>"
    )


# ---------------------------------------------------------------------------
# search and watchlist
# ---------------------------------------------------------------------------


def matches(row: Row, query: str) -> bool:
    """Every word of ``query`` appears in the row's id, short name, name or label."""
    hay = f"{row.instrument_id} {short(row)} {row.name} {row.label}".lower().replace("_", " ")
    hay += " " + row.instrument_id.lower()
    return all(word in hay for word in query.lower().split())


def search(sections: Iterable[Section], query: str) -> list[Section]:
    """Sections cut down to the rows that match, empty sections dropped."""
    out = []
    for s in sections:
        rows = tuple(r for r in s.rows if matches(r, query))
        if rows:
            out.append(Section(s.asset_class, s.title, rows))
    return out


def wl_row(
    row: Row,
    now: datetime,
    sel: str,
    spark: Sequence[float] = (),
    sig: tuple[str, str] | None = None,
) -> str:
    """One watchlist row (P-022): the full name, the short name with a sigma tag for an unusual
    move, a 30-day sparkline, then the value over its change."""
    tag = '<span class="db-tag">proxy</span>' if row.is_proxy and not row.available else ""
    hover = "; ".join((row.label, *row.qualifiers))
    cls = "db-wl-row db-row is-sel" if row.instrument_id == sel else "db-wl-row db-row"
    name = (
        f'<span class="db-wl-name"><b>{escape(row.name)}</b>'
        f"<small>{escape(short(row))}{tag}{sigma_tag(sig)}</small>"
        f'<span class="db-sr"> ({escape(hover)})</span></span>'
    )
    trend = _spark(spark, _trend_cls(row), 40, 20) if row.available else ""
    if not row.available:
        cells = f'<span class="db-wl-gap db-unavailable">{NO_DATA}</span>'
    else:
        p = provenance_of(row, now)
        cells = (
            f'<span class="db-wl-spark">{trend}</span>'
            f'<span class="db-wl-lv"><span class="db-wl-val">{_listed(row.value_text, p)}</span>'
            f'<span class="db-wl-chg">{_change(row, p)}</span></span>'
        )
    current = ' aria-current="true"' if row.instrument_id == sel else ""
    return f'<a class="{cls}" data-db-id="{escape(row.instrument_id)}"{current}>{name}{cells}</a>'


@dataclass(frozen=True)
class WatchGroup:
    title: str
    rows: list[Item]
    proxies: list[Item]  # folded behind one line; empty while searching

    @property
    def fold_label(self) -> str:
        noun = "proxy" if len(self.proxies) == 1 else "proxies"
        return f"Show {len(self.proxies)} ETF {noun}"


def watchlist(
    sections: Sequence[Section],
    now: datetime,
    sel: str = "",
    *,
    fold_proxies: bool = True,
    sparks: Mapping[str, Sequence[float]] | None = None,
    sigs: Mapping[str, tuple[str, str]] | None = None,
) -> list[WatchGroup]:
    """Every instrument grouped by asset class; ETF proxies fold behind one line unless
    searching. ``sparks`` are 30-day trends and ``sigs`` the sigma tags (:func:`sigmas`)."""
    sparks = sparks or {}
    sigs = sigs or {}

    def item(r: Row) -> Item:
        iid = r.instrument_id
        return iid, wl_row(r, now, sel, sparks.get(iid, ()), sigs.get(iid))

    out = []
    for sec in sections:
        real = [r for r in sec.rows if not (fold_proxies and r.is_proxy)]
        proxies = [r for r in sec.rows if fold_proxies and r.is_proxy]
        out.append(WatchGroup(sec.title, [item(r) for r in real], [item(r) for r in proxies]))
    return out


def group_title(title: str) -> str:
    return f'<div class="db-wl-grp db-sec-title">{escape(title)}</div>'


# ---------------------------------------------------------------------------
# chart header, details and the overview grid
# ---------------------------------------------------------------------------


def _header_notes(row: Row) -> list[str]:
    """What the value is, beyond its name, without repeating a caveat word that the figure
    already shows (P-022 review: "proxy" once, not twice)."""
    out = []
    if row.is_proxy and row.proxy_label:
        out.append("tracks " + row.proxy_label.removeprefix("proxy for ").strip())
    for q in row.qualifiers:
        if q.startswith("IEX"):
            q = q.split(":", 1)[-1].strip()  # "single exchange, ..."; the IEX tag is shown
        elif q.startswith("fallback"):
            continue  # the fallback tag and the source line say it
        out.append(q)
    return out


def chart_header(row: Row, now: datetime) -> str:
    """Name and what it is; the latest value (large) with its glyph and caveat tags, the change
    pill, and one quiet provenance line: field, tier, as-of and source."""
    head = (
        f'<div class="db-ch"><span class="db-ch-name">{escape(short(row))}</span>'
        f'<span class="db-ch-sub">{escape(row.name)}</span></div>'
    )
    if not row.available:
        return head + f'<div class="db-ch-value db-unavailable">{NO_DATA}</div>'
    p = provenance_of(row, now)
    chg = (
        change_cell(row.change_text, p, row.basis, compact=True, glyph=False)
        if _has_change(row)
        else _no_change("no change yet")
    )
    basis = (
        f'<span class="db-ch-basis">vs {escape(row.basis)}</span>'
        if row.basis and _has_change(row)
        else ""
    )
    line = " · ".join(
        [
            f'<span class="db-field">{escape(p.field)}</span>',
            f"{tier_glyph(p.tier)} {TIER_NAME[p.tier]}",
            f'<span class="db-asof">{escape(p.hover or p.as_of)}</span>',
            f'<span class="db-src">{escape(p.source)}</span>',
            *(escape(n) for n in _header_notes(row)),
        ]
    )
    return (
        head + '<div class="db-ch db-ch-price">'
        f'<span class="db-ch-value">{value_cell(row.value_text, p, compact=True, show_as_of=False)}'
        f'</span><span class="db-ch-chg">{chg}</span>{basis}</div>'
        f'<div class="db-ch-foot">{line}</div>'
    )


def section_title(title: str) -> str:
    return f'<div class="db-sec-title">{escape(title)}</div>'


# ---------------------------------------------------------------------------
# page footer and the Home stylesheet (T58)
# ---------------------------------------------------------------------------


def footer(ov: Overview, legend: str) -> str:
    """The quiet last line: display mode and tiers, any withheld instruments, and what the tier
    glyphs mean (``legend`` is ``kit.glyph_legend``)."""
    withheld = (
        f"<span>{ov.withheld} instrument(s) withheld in {escape(ov.mode)} mode: live (tier 1) "
        "data is not shown here until provider display terms are verified.</span>"
        if ov.withheld
        else ""
    )
    return (
        f'<div class="db-foot"><span class="db-mode">{escape(ov.mode_label)}'
        '<span class="db-sr"> (display mode and the data tiers it shows)</span></span>'
        f"{withheld}{legend}</div>"
    )


def _css() -> str:
    from app import theme as t

    pick = 'div[class*="st-key-db-pick-"]'
    return f"""
<style>
/* ---- T58 Home v2 (P-022) ---- */
.st-key-db-risk {{ background: {t.PANEL}; border-radius: {t.RADIUS}px; padding: {t.PAD}px; }}
.st-key-db-detail, .st-key-db-risk {{ gap: 0; }}
.db-card-h {{ color: {t.SECONDARY}; font-size: 13px; font-weight: 500; margin-bottom: 4px; }}
.st-key-db-watch .db-card-h {{ padding: 6px {t.PAD}px 0 {t.PAD}px; }}

/* the status dot's tooltip opens below it, right-aligned: it sits at the top right corner */
.db-status .db-tip {{ left: auto; right: 0; bottom: auto; top: calc(100% + 8px);
  transform: none; }}

/* watchlist rows: full name, short name and sigma tag, sparkline, value over change */
.db-wl-row {{ grid-template-columns: minmax(0, 1fr) 40px auto; }}
.db-wl-name b {{ font-size: 13.5px; line-height: 1.3; }}
.db-wl-name small {{ display: flex; align-items: center; gap: 6px; white-space: normal;
  overflow: visible; text-overflow: clip; }}
.db-wl-name small .db-tag {{ margin-left: 0; }}
.db-sig {{ color: {t.AMBER}; font-size: 11px; font-weight: 500; }}
.db-wl-spark .db-spark {{ width: 40px; height: 20px; opacity: .9; }}
.db-wl-lv {{ text-align: right; }}
.db-wl-lv .db-wl-val, .db-wl-lv .db-wl-chg {{ display: block; }}
.db-wl-lv .db-fig {{ text-align: right; }}
.db-wl-chg .db-val {{ font-size: 11.5px; }}
{pick}:hover .db-wl-row {{ background: {t.HOVER}; }}

/* chart card header */
.db-ch-price {{ align-items: center; margin-top: 6px; }}
.db-ch-value .db-fig {{ display: inline-flex; align-items: center; gap: 6px; }}
.db-ch-value .db-flag {{ font-size: {t.SIZE_META}px; letter-spacing: 0; }}
.db-ch-chg .db-val {{ font-size: 13px; }}
.db-ch-basis {{ color: {t.META}; font-size: {t.SIZE_META}px; }}
.db-ch-foot .db-tier {{ color: {t.META}; }}
.st-key-db-ctrls {{ justify-content: flex-end; align-items: center; gap: 10px !important;
  flex-wrap: wrap; }}
.st-key-db-ctrls > div {{ width: auto !important; flex: 0 0 auto !important; }}
.db-chfoot {{ color: {t.META}; font-size: {t.SIZE_META}px; margin-top: 4px; }}
.db-hzs {{ margin: 0 0 12px 0; }}

/* right rail: the verdict */
.st-key-db-detail .db-verdict {{ display: flex; align-items: center; gap: 10px; margin: 10px 0 6px;
  padding: 0; background: transparent; }}
.db-verdict-ico {{ width: 28px; height: 28px; border-radius: 8px; display: inline-flex;
  align-items: center; justify-content: center; font-weight: 600; background: {t.RAISED};
  color: {t.SECONDARY}; }}
.db-verdict-ok .db-verdict-ico {{ background: {t.UP_TINT}; color: {t.UP}; }}
.db-verdict-warn .db-verdict-ico, .db-verdict-fail .db-verdict-ico {{
  background: {t.AMBER_TINT}; color: {t.AMBER}; }}
.st-key-db-detail .db-verdict b {{ font-size: 17px; font-weight: 500; color: {t.TEXT}; }}
.db-verdict-p, .db-verdict-sum {{ color: {t.SECONDARY}; font-size: 13px; margin: 0 0 4px 0; }}
.db-srcs {{ margin-top: 14px; display: flex; flex-direction: column; gap: 10px; }}
.db-src-row {{ display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }}
.db-src-row > span {{ color: {t.SECONDARY}; }}
.db-src-row b {{ font-weight: 500; color: {t.TEXT}; }}

/* right rail: risk and range */
.db-facts {{ margin: 4px 0 8px 0; }}
.db-fact {{ display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 12px 0; border-top: 1px solid {t.DIVIDER}; }}
.db-fact:first-child {{ border-top: 0; }}
.db-fact > span {{ color: {t.SECONDARY}; font-size: 13px; }}
.db-fact > b {{ font-weight: 500; font-size: {t.SIZE_BODY}px; color: {t.TEXT}; }}
.db-fact .db-fig {{ text-align: right; }}
.db-gapnote {{ margin: 0 0 8px 0; }}
.db-rangebox {{ padding: 4px 0 8px 0; }}
.db-range-head {{ display: flex; justify-content: space-between; font-size: 13px; }}
.db-range-label {{ color: {t.SECONDARY}; }}
.db-range-head b {{ font-weight: 500; font-variant-numeric: tabular-nums; }}
.db-range .db-range-fill {{ top: 0; height: 4px; left: 0; border-radius: 2px;
  background: linear-gradient(90deg, {t.RAISED}, #3A4250); }}
.db-range-ends .db-fig {{ text-align: left; }}
.db-range-ends > span:last-child .db-fig {{ text-align: right; }}
.st-key-db-risk [data-testid="stButton"] button,
.st-key-db-risk [data-testid="stPageLink"] a {{ color: {t.SECONDARY}; font-size: 13px; }}

/* tabs under the chart */
.st-key-db-values [data-baseweb="tab-list"] {{ gap: 24px; }}
.st-key-db-values [data-baseweb="tab"] p {{ font-size: 13.5px; font-weight: 500; }}
.st-key-db-values [data-baseweb="tab-highlight"] {{ background: {t.ACCENT}; }}
.st-key-db-values [data-baseweb="tab-border"] {{ background: {t.DIVIDER}; }}

@media (max-width: 640px) {{
  .st-key-db-watch {{ max-height: 440px !important; }}
  .db-values .db-tagline, .db-values td {{ white-space: normal; overflow-wrap: anywhere; }}
  .st-key-db-values [data-testid="stMarkdownContainer"] {{ overflow-x: auto; }}
}}

/* footer */
.db-foot {{ margin-top: 24px; color: {t.META}; font-size: {t.SIZE_META}px; display: flex;
  gap: 8px 16px; flex-wrap: wrap; }}
.db-foot .db-glyphs {{ gap: 12px; }}
</style>
"""


#: the Home stylesheet (injected by ``app/views/markets.py`` after ``theme.apply()``)
HOME_CSS = _css()
