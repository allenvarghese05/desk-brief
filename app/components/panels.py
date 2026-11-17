"""Overview panels: top bar, headline strip, instrument tables and the provenance drawer.

All text comes from ``BriefModel`` rows (labels, signed changes, as-of text); every number is
printed through ``values.value_cell`` / ``change_cell``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from html import escape

import streamlit as st
from app.components.data import Overview
from app.components.values import TIER_NAME, Provenance, change_cell, tier_glyph, value_cell

from deskbrief.query import Lineage, field_label
from deskbrief.report.model import SPREAD_ID, Row, Section
from deskbrief.timeutil import to_et

_BADGE = {"ok": "Healthy", "warn": "Warnings", "fail": "Failures", "unknown": "No checks yet"}


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
        assert row.instrument_id == SPREAD_ID
        return Provenance("treasury (derived)", row.tier, row.as_of_text, "derived spread")
    last = row.change.last
    hover = f"{to_et(last.as_of):%d %b %H:%M} ET, {_age((now - last.as_of).total_seconds())}"
    return Provenance(last.source, row.tier, row.as_of_text, field_label(last), hover)


def top_bar(ov: Overview) -> None:
    et = to_et(ov.as_of)
    badge = _BADGE[ov.health.status]
    st.markdown(
        '<div class="db-top"><span class="db-brand">Desk Brief</span>'
        f'<span class="db-meta">{et:%a} {et.day} {et:%b} · {et:%H:%M} ET</span>'
        f'<span class="db-health-{ov.health.status}" title="{escape(ov.health.text)}">'
        f"● {badge} · {escape(ov.health.text)}</span>"
        f'<span class="db-mode">{escape(ov.mode_label)}</span></div>',
        unsafe_allow_html=True,
    )


def _spark(values: Sequence[float]) -> str:
    """Tiny inline sparkline (no axis, no numbers) of the last official daily values."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = 60 / (len(values) - 1)
    pts = " ".join(f"{i * step:.1f},{16 - (v - lo) / span * 14:.1f}" for i, v in enumerate(values))
    return (
        '<svg class="db-spark" width="60" height="18" aria-hidden="true">'
        f'<polyline points="{pts}"/></svg>'
    )


def headline_strip(ov: Overview) -> None:
    items = []
    for label, row in ov.headline:
        if not row.available:
            body = '<span class="db-unavailable">data unavailable</span>'
        else:
            p = provenance_of(row, ov.as_of)
            body = value_cell(row.value_text, p) + (
                change_cell(row.change_text, p, row.basis) if row.change_text != "n/a" else ""
            )
            body += _spark(ov.sparks.get(row.instrument_id, ()))
        items.append(f'<div><span class="db-hl-label">{escape(label)}</span>{body}</div>')
    st.markdown(f'<div class="db-strip">{"".join(items)}</div>', unsafe_allow_html=True)


def _name_cell(row: Row) -> str:
    if row.is_proxy:  # proxies are italic and say so
        name = f'<span class="db-name-proxy">{escape(row.label)}</span>'
        return name + '<span class="db-tag">proxy</span>'
    return escape(row.label)


def _row_html(row: Row, now: datetime) -> str:
    if not row.available:
        gap = '<td colspan="2" class="db-unavailable">data unavailable</td>'
    else:
        p = provenance_of(row, now)
        chg = change_cell(row.change_text, p, row.basis) if row.change_text != "n/a" else "n/a"
        gap = f'<td class="num">{value_cell(row.value_text, p)}</td><td class="num">{chg}</td>'
    quals = "".join(f'<span class="db-note"> · {escape(q)}</span>' for q in row.qualifiers)
    return f'<tr class="db-row"><td>{_name_cell(row)}{quals}</td>{gap}</tr>'


def section_table(section: Section, now: datetime) -> None:
    rows = "".join(_row_html(r, now) for r in section.rows)
    st.markdown(
        '<table class="db-table"><thead><tr><th>Instrument</th>'
        '<th class="num">Last</th><th class="num">Change</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>",
        unsafe_allow_html=True,
    )


def provenance_drawer(ov: Overview) -> None:
    """Pick an instrument to see its value, tier and the fetch behind it (spec 10.3 page 2)."""
    rows = {r.instrument_id: r for s in ov.sections for r in s.rows if r.available}
    with st.expander("Provenance"):
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
