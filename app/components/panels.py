"""Workspace panels (T36 layout, T57 v2 look): top bar, market strip, biggest moves,
watchlist, chart header, overview small multiples and the yield curve.

All text comes from ``BriefModel`` rows (labels, signed changes, as-of text); every number is
printed through ``values.value_cell`` / ``change_cell``. Lists use compact provenance (a
focusable tier glyph with a CSS tooltip, P-022); the chart header prints the full line.

Selection is in-session (T57): each selectable item is returned as ``(instrument id, html)``
and the page draws it under a transparent button whose callback sets ``st.query_params``
(``app/views/markets.py``). The markup itself holds no link, no ``target`` and no ``title``.
"""
# ruff: noqa: RUF001  (the sigma sign is intentional output)

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape

from app.components.data import CURVE_TENORS, Overview
from app.components.values import (
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
}
_TENOR_YEARS = {"UST_3M": 0.25, "UST_2Y": 2, "UST_10Y": 10, "UST_20Y": 20, "UST_30Y": 30}
NO_CHANGE = "—"
NO_DATA = 'no data<span class="db-sr"> (data unavailable)</span>'
#: the instrument selected when the URL names none
DEFAULT_ID = "ES_F"


def short(row: Row) -> str:
    return SHORT.get(row.instrument_id, row.instrument_id)


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
    """Pipeline health as one quiet badge; the counts are in its CSS tooltip and for screen
    readers. The page puts a button over it that opens Data health in the same session."""
    s = ov.health.status
    text = escape(ov.health.text)
    return (
        '<div class="db-status">'
        f'<span class="db-health db-health-{s} db-tipwrap">'
        f'<span class="db-dot db-dot-{s}"></span>{_BADGE[s]}'
        f'<span class="db-sr"> · {text}</span>'
        f'<span class="db-tip" aria-hidden="true">{text}</span></span>'
        f'<span class="db-mode">{escape(ov.mode_label)}'
        '<span class="db-sr"> (display mode and the data tiers it shows)</span></span></div>'
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
MOVES_LABEL = (
    '<span class="db-moves-label">Biggest moves<span class="db-sr"> (moves at least the anomaly '
    "threshold away from each instrument's recent daily moves)</span></span>"
)
NO_MOVES = '<span class="db-note">Nothing unusual: every move is within its normal range.</span>'


def move_items(ov: Overview, limit: int = 6) -> list[Item]:
    """The day's unusual moves, ranked by size against each instrument's own recent moves."""
    rows = {r.instrument_id: r for s in ov.sections for r in s.rows}
    items = []
    for n in ov.moves[:limit]:
        row = rows.get(n.instrument_id or "")
        if row is None or not row.available or not _has_change(row):
            continue
        p = provenance_of(row, ov.as_of)
        m = _SIGMA.search(n.text)
        sigma = (
            f' <span class="db-note">{m.group(1)}σ<span class="db-sr"> ({escape(n.text)})'
            "</span></span>"
            if m
            else ""
        )
        items.append(
            (
                row.instrument_id,
                f'<a class="db-move" data-db-id="{escape(row.instrument_id)}">'
                f"<b>{escape(short(row))}</b>"
                f"{change_cell(row.change_text, p, row.basis, compact=True)}{sigma}</a>",
            )
        )
    return items


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


def wl_row(row: Row, now: datetime, sel: str) -> str:
    """One watchlist row: short name, full name, value and change."""
    tag = '<span class="db-tag">proxy</span>' if row.is_proxy and not row.available else ""
    hover = "; ".join((row.label, *row.qualifiers))
    cls = "db-wl-row db-row is-sel" if row.instrument_id == sel else "db-wl-row db-row"
    name = (
        f'<span class="db-wl-name"><b>{escape(short(row))}{tag}</b>'
        f"<small>{escape(row.name)}</small>"
        f'<span class="db-sr"> ({escape(hover)})</span></span>'
    )
    if not row.available:
        cells = f'<span class="db-wl-gap db-unavailable">{NO_DATA}</span>'
    else:
        p = provenance_of(row, now)
        cells = (
            f'<span class="db-wl-val">{_listed(row.value_text, p)}</span>'
            f'<span class="db-wl-chg">{_change(row, p)}</span>'
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
    sections: Sequence[Section], now: datetime, sel: str = "", *, fold_proxies: bool = True
) -> list[WatchGroup]:
    """Every instrument grouped by section; ETF proxies fold behind one line unless searching."""
    out = []
    for sec in sections:
        real = [r for r in sec.rows if not (fold_proxies and r.is_proxy)]
        proxies = [r for r in sec.rows if fold_proxies and r.is_proxy]
        out.append(
            WatchGroup(
                sec.title,
                [(r.instrument_id, wl_row(r, now, sel)) for r in real],
                [(r.instrument_id, wl_row(r, now, sel)) for r in proxies],
            )
        )
    return out


def group_title(title: str) -> str:
    return f'<div class="db-wl-grp db-sec-title">{escape(title)}</div>'


# ---------------------------------------------------------------------------
# chart header, details and the overview grid
# ---------------------------------------------------------------------------


def chart_header(row: Row, now: datetime) -> str:
    """Name, the latest value with its full provenance line, and the change with its basis."""
    sub = escape(" · ".join(x for x in (row.label, *row.qualifiers) if x))
    head = (
        f'<div class="db-ch"><span class="db-ch-name">{escape(short(row))}</span>'
        f'<span class="db-ch-sub">{escape(row.name)}</span></div>'
    )
    if not row.available:
        return head + f'<div class="db-ch-value db-unavailable">{NO_DATA}</div>'
    p = provenance_of(row, now)
    chg = (
        change_cell(row.change_text, p, row.basis)
        if _has_change(row)
        else _no_change("no change yet")
    )
    return (
        head + '<div class="db-ch" style="margin-top:.3rem">'
        f'<span class="db-ch-value">{value_cell(row.value_text, p)}</span>'
        f'<span class="db-ch-chg">{chg}</span></div>'
        + (f'<div class="db-ch-foot">{sub}</div>' if sub else "")
    )


def overview(ov: Overview, sel: str = "") -> list[tuple[str, list[Item]]]:
    """Small multiples: every instrument's 30-day trend, as ``(section title, cards)``."""
    out = []
    for sec in ov.sections:
        cards = []
        for row in sec.rows:
            if row.is_proxy or not row.available:
                continue
            p = provenance_of(row, ov.as_of)
            cls = "db-card is-sel" if row.instrument_id == sel else "db-card"
            spark = _spark(
                ov.sparks.get(row.instrument_id, ()), _trend_cls(row), 160, 34, area=True
            )
            cards.append(
                (
                    row.instrument_id,
                    f'<a class="{cls}" data-db-id="{escape(row.instrument_id)}">'
                    f'<div class="db-card-top"><b>{escape(short(row))}'
                    f'<span class="db-sr"> · {escape(row.name)}</span></b>'
                    f"{_change(row, p)}</div>"
                    f'<div class="db-card-val">{_listed(row.value_text, p)}</div>'
                    f"{spark}</a>",
                )
            )
        if cards:
            out.append((sec.title, cards))
    return out


def section_title(title: str) -> str:
    return f'<div class="db-sec-title">{escape(title)}</div>'


# ---------------------------------------------------------------------------
# yield curve
# ---------------------------------------------------------------------------


def _nearest(
    points: Sequence[tuple[date, float, str]], target: date
) -> tuple[date, float, str] | None:
    """The last point on or before ``target``."""
    before = [p for p in points if p[0] <= target]
    return before[-1] if before else None


def _curve_prov(point: tuple[date, float, str]) -> Provenance:
    return Provenance(point[2], 3, f"{point[0]:%d %b}", "yield")


def yield_curve(ov: Overview, *, back_days: int = 28) -> str:
    """Official Treasury curve: latest values against about a month earlier."""
    tenors = [t for t in CURVE_TENORS if ov.curve.get(t)]
    if len(tenors) < 2:
        return (
            '<div class="db-empty">Not enough official Treasury yields yet to draw the curve.</div>'
        )
    latest = max(ov.curve[t][-1][0] for t in tenors)
    now = {t: ov.curve[t][-1] for t in tenors}
    then = {t: _nearest(ov.curve[t], latest - timedelta(days=back_days)) for t in tenors}
    have_then = all(then[t] is not None for t in tenors)
    values = [p[1] for p in now.values()] + [p[1] for p in then.values() if p is not None]
    lo, hi = min(values), max(values)
    pad = max((hi - lo) * 0.15, 0.05)
    lo, hi = lo - pad, hi + pad
    w, h, left, right, top, bottom = 420, 210, 40, 14, 14, 30
    logs = {t: math.log(_TENOR_YEARS[t]) for t in tenors}
    x0, x1 = min(logs.values()), max(logs.values())

    def x(t: str) -> float:
        return left + (logs[t] - x0) / ((x1 - x0) or 1) * (w - left - right)

    def y(v: float) -> float:
        return top + (hi - v) / ((hi - lo) or 1) * (h - top - bottom)

    grid = ""
    for i in range(4):  # gridlines only: every yield figure is in the tenor strip below
        v = lo + (hi - lo) * i / 3
        grid += (
            f'<line x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" '
            'class="db-curve-grid"/>'
        )
    ticks = "".join(
        f'<text x="{x(t):.1f}" y="{h - 10}" text-anchor="middle">{SHORT[t]}</text>' for t in tenors
    )
    line_now = " ".join(f"{x(t):.1f},{y(now[t][1]):.1f}" for t in tenors)
    marks = "".join(  # points only: the figures are in the tenor strip, with provenance
        f'<circle cx="{x(t):.1f}" cy="{y(now[t][1]):.1f}" r="2.6" fill="currentColor"/>'
        for t in tenors
    )
    then_svg, legend_then = "", ""
    if have_then:
        pts = " ".join(f"{x(t):.1f},{y(then[t][1]):.1f}" for t in tenors)  # type: ignore[index]
        then_svg = f'<polyline class="db-curve-then" points="{pts}"/>'
        d = min(p[0] for p in then.values() if p is not None)
        legend_then = f"<span>- - - {d.day} {d:%b}</span>"
    strip = "".join(
        f'<div class="db-tenor"><span class="db-hz-label">{SHORT[t]}</span>'
        f"{value_cell(f'{now[t][1]:.2f}%', _curve_prov(now[t]), compact=True)}"
        "</div>"
        for t in tenors
    )
    svg = (
        f'<svg class="db-curve" viewBox="0 0 {w} {h}" width="100%" role="img" '
        f"aria-label=\"Treasury yield curve on {latest.day} {latest:%b}; each tenor's yield is "
        f'listed under the chart">{grid}{then_svg}'
        f'<polyline class="db-curve-now" points="{line_now}"/>{marks}{ticks}</svg>'
    )
    sources = ", ".join(sorted({now[t][2] for t in tenors}))
    return (
        f'{svg}<div class="db-curve-legend"><span>— {latest.day} {latest:%b}</span>'
        f"{legend_then}<span>{tier_glyph(3)} official · {escape(sources)}</span></div>"
        f'<div class="db-tenors">{strip}</div>'
    )
