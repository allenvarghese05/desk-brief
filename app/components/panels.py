"""Overview panels (T36 redesign): top bar, headline tiles, biggest moves, section tables, yield
curve, search filter and the provenance drawer.

All text comes from ``BriefModel`` rows (labels, signed changes, as-of text); every number is
printed through ``values.value_cell`` / ``change_cell``. On the Overview they use the compact
tag (tier glyph and as-of on screen; field and source on hover and for screen readers); the
provenance drawer and the Instrument page print the full line.
"""
# ruff: noqa: RUF001  (the sigma sign is intentional output)

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from html import escape

import streamlit as st
from app.components.data import CURVE_TENORS, Overview
from app.components.values import (
    TIER_NAME,
    Provenance,
    change_cell,
    direction,
    tier_glyph,
    value_cell,
)

from deskbrief.query import Lineage, field_label
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
INSTRUMENT_PAGE = "Instrument"


def short(row: Row) -> str:
    return SHORT.get(row.instrument_id, row.instrument_id)


def _link(iid: str) -> str:
    return f"{INSTRUMENT_PAGE}?id={escape(iid)}"


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
    return Provenance(last.source, row.tier, row.as_of_text, field_label(last), hover)


def _has_change(row: Row) -> bool:
    return row.change is not None and row.change_text not in ("n/a", "")


def _change(row: Row, p: Provenance) -> str:
    if not _has_change(row):
        return f'<span class="db-flat" title="no earlier value to compare with">{NO_CHANGE}</span>'
    return change_cell(row.change_text, p, row.basis, compact=True)


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
    """Pipeline health as one quiet link to Data health; the counts live in the tooltip."""
    s = ov.health.status
    text = escape(ov.health.text)
    return (
        '<div class="db-status">'
        f'<a class="db-health db-health-{s}" href="Data_health" target="_self" title="{text}">'
        f'<span class="db-dot db-dot-{s}"></span>{_BADGE[s]}'
        f'<span class="db-sr"> · {text}</span></a>'
        f'<span class="db-mode" title="display mode and the data tiers it shows">'
        f"{escape(ov.mode_label)}</span></div>"
    )


# ---------------------------------------------------------------------------
# headline tiles and biggest moves
# ---------------------------------------------------------------------------


def _spark(values: Sequence[float], cls: str) -> str:
    """Small sparkline of the last official daily values (no axis, no numbers)."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    w, h = 72, 22
    step = w / (len(values) - 1)
    pts = " ".join(
        f"{i * step:.1f},{h - 2 - (v - lo) / span * (h - 4):.1f}" for i, v in enumerate(values)
    )
    return (
        f'<svg class="db-spark {cls}" width="{w}" height="{h}" aria-hidden="true">'
        f'<polyline points="{pts}"/></svg>'
    )


def tiles(ov: Overview) -> str:
    items = []
    for label, row in ov.headline:
        if not row.available:
            body = (
                '<div class="db-tile-value db-unavailable" title="data unavailable">no data</div>'
            )
        else:
            p = provenance_of(row, ov.as_of)
            cls = direction(row.change_text) if _has_change(row) else "db-flat"
            body = (
                f'<div class="db-tile-value">{value_cell(row.value_text, p, compact=True)}</div>'
                f'<div class="db-tile-row"><span class="db-tile-chg">{_change(row, p)}</span>'
                f"{_spark(ov.sparks.get(row.instrument_id, ()), cls)}</div>"
            )
        items.append(
            f'<a class="db-tile" href="{_link(row.instrument_id)}" target="_self" '
            f'title="{escape(row.name)}">'
            f'<div class="db-tile-label">{escape(label)}</div>{body}</a>'
        )
    return f'<div class="db-tiles">{"".join(items)}</div>'


_SIGMA = re.compile(r"is (\d+(?:\.\d+)?)σ")


def moves(ov: Overview, limit: int = 5) -> str:
    """The day's unusual moves, ranked by size against each instrument's own recent moves."""
    rows = {r.instrument_id: r for s in ov.sections for r in s.rows}
    items = []
    for n in ov.moves[:limit]:
        row = rows.get(n.instrument_id or "")
        if row is None or not row.available or not _has_change(row):
            continue
        p = provenance_of(row, ov.as_of)
        m = _SIGMA.search(n.text)
        sigma = f' <span class="db-note" title="{escape(n.text)}">{m.group(1)}σ</span>' if m else ""
        items.append(
            f'<span class="db-move"><span class="db-name"><a href="{_link(row.instrument_id)}" '
            f'target="_self">{escape(short(row))}</a></span> '
            f"{change_cell(row.change_text, p, row.basis, compact=True)}{sigma}</span>"
        )
    body = "".join(items) or (
        '<span class="db-note">Nothing unusual: every move is within its normal range.</span>'
    )
    return (
        '<div class="db-moves" title="moves at least the anomaly threshold away from each '
        "instrument's recent daily moves\">"
        f'<span class="db-moves-label">Biggest moves</span>{body}</div>'
    )


# ---------------------------------------------------------------------------
# search and section tables
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


def _row_html(row: Row, now: datetime) -> str:
    tag = '<span class="db-tag">proxy</span>' if row.is_proxy else ""
    hover = "; ".join((row.label, *row.qualifiers))
    name_cls = "db-full db-name-proxy" if row.is_proxy else "db-full"
    name = (
        f'<td class="db-name"><a href="{_link(row.instrument_id)}" target="_self">'
        f"{escape(short(row))}</a>{tag}"
        f'<span class="{name_cls}" title="{escape(hover)}">{escape(row.name)}</span></td>'
    )
    if not row.available:
        gap = '<td colspan="2" class="db-unavailable num" title="data unavailable">no data</td>'
        return f'<tr class="db-row">{name}{gap}</tr>'
    p = provenance_of(row, now)
    return (
        f'<tr class="db-row">{name}'
        f'<td class="num">{value_cell(row.value_text, p, compact=True)}</td>'
        f'<td class="num db-c">{_change(row, p)}</td></tr>'
    )


def _table(rows: Sequence[Row], now: datetime) -> str:
    body = "".join(_row_html(r, now) for r in rows)
    return f'<table class="db-table"><tbody>{body}</tbody></table>'


def section(sec: Section, now: datetime, *, fold_proxies: bool = True) -> str:
    """A section title and its table; proxies fold behind one line unless searching."""
    real = [r for r in sec.rows if not r.is_proxy] if fold_proxies else list(sec.rows)
    proxies = [r for r in sec.rows if r.is_proxy] if fold_proxies else []
    html = f'<div class="db-sec"><div class="db-sec-title">{escape(sec.title)}</div>'
    html += _table(real, now) if real else ""
    if proxies:
        noun = "proxy" if len(proxies) == 1 else "proxies"
        html += (
            f'<details class="db-more"><summary>Show {len(proxies)} ETF {noun}</summary>'
            f"{_table(proxies, now)}</details>"
        )
    return html + "</div>"


# ---------------------------------------------------------------------------
# yield curve
# ---------------------------------------------------------------------------


def _nearest(points: Sequence[tuple[date, float]], target: date) -> tuple[date, float] | None:
    """The last point on or before ``target``."""
    before = [p for p in points if p[0] <= target]
    return before[-1] if before else None


def yield_curve(ov: Overview, *, back_days: int = 28) -> str:
    """Official Treasury curve: latest values against about a month earlier."""
    tenors = [t for t in CURVE_TENORS if ov.curve.get(t)]
    if len(tenors) < 2:
        return (
            '<div class="db-sec"><div class="db-sec-title">Yield curve</div>'
            '<div class="db-empty">Not enough official Treasury yields yet to draw the curve.'
            "</div></div>"
        )
    latest = max(ov.curve[t][-1][0] for t in tenors)
    now = {t: ov.curve[t][-1] for t in tenors}
    then = {t: _nearest(ov.curve[t], latest - timedelta(days=back_days)) for t in tenors}
    have_then = all(then[t] is not None for t in tenors)
    values = [v for _, v in now.values()] + [p[1] for p in then.values() if p is not None]
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
    for i in range(4):
        v = lo + (hi - lo) * i / 3
        grid += (
            f'<line x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" '
            f'stroke="#eeebe5"/><text x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end">'
            f"{v:.2f}%</text>"
        )
    ticks = "".join(
        f'<text x="{x(t):.1f}" y="{h - 10}" text-anchor="middle">{SHORT[t]}</text>' for t in tenors
    )
    line_now = " ".join(f"{x(t):.1f},{y(now[t][1]):.1f}" for t in tenors)
    marks = "".join(
        f'<circle cx="{x(t):.1f}" cy="{y(now[t][1]):.1f}" r="2.6" fill="currentColor"/>'
        f'<text x="{x(t):.1f}" y="{y(now[t][1]) - 8:.1f}" text-anchor="middle">'
        f"{now[t][1]:.2f}</text>"
        for t in tenors
    )
    then_svg, legend_then = "", ""
    if have_then:
        pts = " ".join(f"{x(t):.1f},{y(then[t][1]):.1f}" for t in tenors)  # type: ignore[index]
        then_svg = f'<polyline class="db-curve-then" points="{pts}"/>'
        d = min(p[0] for p in then.values() if p is not None)
        legend_then = f"<span>- - - {d.day} {d:%b}</span>"
    desc = ", ".join(f"{SHORT[t]} {now[t][1]:.2f}%" for t in tenors)
    svg = (
        f'<svg class="db-curve" viewBox="0 0 {w} {h}" width="100%" role="img" '
        f'aria-label="Treasury yield curve on {latest.day} {latest:%b}: {desc}" '
        f'style="color:#1f2421">{grid}{then_svg}'
        f'<polyline class="db-curve-now" points="{line_now}"/>{marks}{ticks}</svg>'
    )
    return (
        '<div class="db-sec"><div class="db-sec-title">Yield curve · official</div>'
        f'{svg}<div class="db-curve-legend"><span>— {latest.day} {latest:%b}</span>'
        f"{legend_then}<span>{tier_glyph(3)} treasury</span></div></div>"
    )


# ---------------------------------------------------------------------------
# provenance drawer
# ---------------------------------------------------------------------------


def provenance_drawer(ov: Overview) -> None:
    """Pick an instrument to see its value, tier and the fetch behind it (spec 10.3 page 2)."""
    rows = {r.instrument_id: r for s in ov.sections for r in s.rows if r.available}
    with st.expander("Trace a value: source, fetch and raw file"):
        if not rows:
            st.markdown("No values to trace.")
            return
        pick = st.selectbox("Instrument", list(rows), format_func=lambda i: f"{i} · {rows[i].name}")
        row = rows[pick]
        p = provenance_of(row, ov.as_of)
        lg: Lineage | None = ov.lineage.get(pick)
        facts = [
            ("Value", value_cell(row.value_text, p)),
            ("Tier", f"{tier_glyph(p.tier)} {TIER_NAME[p.tier]}"),
            ("Basis", escape(row.basis or "n/a")),
            ("Caveats", escape("; ".join(row.qualifiers) or "none")),
        ]
        if lg is None:
            facts.append(("Fetch", "no single fetch: derived value"))
        else:
            v = lg.value
            facts += [
                ("Source", escape(f"{v.source} / {v.dataset} / {v.symbol}")),
                ("Fetched", escape(f"{to_et(v.ingested_at):%d %b %H:%M} ET, fetch {v.fetch_id}")),
                ("Latency", escape(f"{lg.latency_ms or 'n/a'} ms, HTTP {lg.http_status or 'n/a'}")),
                ("Raw object", escape(f"{lg.raw_key or 'n/a'} · sha256 {lg.sha256 or 'n/a'}")),
                ("Parser", escape(v.parser_version)),
            ]
        body = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in facts)
        st.markdown(f'<table class="db-table">{body}</table>', unsafe_allow_html=True)
