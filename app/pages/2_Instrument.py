"""Instrument drill-down (spec 10.3 page 2): tier ladder, 30-day chart of every tier, lineage and
check history. Hosted mode never reads a hidden tier; the best remaining tier is shown instead.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from app.components import data  # noqa: E402
from app.components.values import TIER_GLYPH, TIER_NAME, Provenance, value_cell  # noqa: E402
from app.pages import _kit  # noqa: E402

from deskbrief.query import ValueRow, field_label, latest_snapshot, lineages  # noqa: E402
from deskbrief.timeutil import et_date, to_et  # noqa: E402

ctx = _kit.context("Instrument")
HISTORY_DAYS = 30
CHECK_DAYS = 14


@st.cache_data(ttl=60, show_spinner=False)
def _ladder(_ctx: _kit.Ctx, where: str, iid: str, hidden: frozenset[int]) -> list[ValueRow]:
    """Latest value per visible tier of ``iid`` (one query per tier, others excluded)."""
    conn = data.open_connection(_ctx.config, os.environ)
    try:
        rows = []
        for tier in (1, 2, 3):
            if tier in hidden:
                continue
            others = {1, 2, 3} - {tier}
            rows += [r for r in latest_snapshot(conn, _ctx.now, exclude_tiers=others)
                     if r.instrument_id == iid]  # fmt: skip
        return rows
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def _history(_ctx: _kit.Ctx, where: str, iid: str, hidden: frozenset[int]) -> pd.DataFrame:
    conn = data.open_connection(_ctx.config, os.environ)
    try:
        marks = ", ".join(str(int(t)) for t in (1, 2, 3) if t not in hidden) or "0"
        return conn.execute(
            "SELECT tier, CAST(timezone('America/New_York', as_of) AS DATE) AS d, field, value, "  # noqa: S608 (the interpolated tiers are ints)
            "unit, source FROM obs_pit(?) WHERE instrument_id = ? AND interval = '1d' "
            "AND field IN ('close', 'yield', 'rate') AND as_of > ? "
            f"AND tier IN ({marks}) "
            "AND CAST(timezone('America/New_York', as_of) AS DATE) < ? "
            "QUALIFY row_number() OVER (PARTITION BY tier, d ORDER BY "
            "CASE field WHEN 'close' THEN 0 WHEN 'yield' THEN 1 ELSE 2 END) = 1 ORDER BY d",
            [_ctx.now, iid, _ctx.now - timedelta(days=HISTORY_DAYS), et_date(_ctx.now)],
        ).fetchdf()
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def _checks(_ctx: _kit.Ctx, where: str, iid: str) -> pd.DataFrame:
    conn = data.open_connection(_ctx.config, os.environ)
    try:
        return conn.execute(
            "SELECT target_date, check_type, check_name, source, status, observed, threshold "
            "FROM dq WHERE instrument_id = ? AND target_date > ? ORDER BY target_date DESC, "
            "check_name",
            [iid, ctx.today - timedelta(days=CHECK_DAYS)],
        ).fetchdf()
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def _lineage(_ctx: _kit.Ctx, where: str, row: ValueRow) -> object:
    conn = data.open_connection(_ctx.config, os.environ)
    try:
        return lineages(conn, [row])[0]
    finally:
        conn.close()


snap = _kit.serving_frame(ctx, "latest_snapshot.parquet")
names = dict(zip(snap["instrument_id"], snap["name"], strict=True)) if not snap.empty else {}
if not names:
    st.markdown("No instruments to show yet.")
    st.stop()
iid = st.selectbox(
    "Instrument", sorted(names), format_func=lambda i: f"{i} · {names[i]}", key="instrument"
)
where = _kit._where(ctx.config)
rows = _ladder(ctx, where, iid, ctx.hidden)
if not rows:
    st.markdown("No value in a tier shown in this mode.")
    st.stop()

# --- the three-tier ladder ---
st.subheader("Tier ladder")
best = max(rows, key=lambda r: r.tier)
cells = []
for r in sorted(rows, key=lambda r: -r.tier):
    p = Provenance(r.source, r.tier, f"{to_et(r.as_of):%d %b %H:%M}", field_label(r))
    gap = (r.value - best.value) / best.value * 10_000 if best.value and r is not best else None
    gap_txt = "reference" if r is best else f"{gap:+.1f} bp vs {TIER_NAME[best.tier]}"
    cells.append(
        f"<tr><td>{TIER_GLYPH[r.tier]} {TIER_NAME[r.tier]}</td>"
        f'<td class="num">{value_cell(f"{r.value:,.4f} {r.unit}", p)}</td>'
        f'<td class="num">{gap_txt}</td></tr>'
    )
tol = _kit.serving_frame(ctx, "crosschecks_7d.parquet")
tol_txt = "no cross-check tolerance for this instrument in the last 7 days"
if not tol.empty:
    t = tol[(tol["left_instrument"] == iid) | (tol["right_instrument"] == iid)]
    t = _kit.keep_rows(
        t, ("left_source", "right_source"), _kit.exposed_sources(ctx.config, ctx.hidden)
    )
    if not t.empty:
        warn, fail = t["warn_bps"].iloc[-1], t["fail_bps"].iloc[-1]
        tol_txt = f"cross-check tolerance: warn {warn:g} bp, fail {fail:g} bp"
st.markdown(
    '<table class="db-table"><thead><tr><th>Tier</th><th class="num">Value</th>'
    f'<th class="num">Gap</th></tr></thead><tbody>{"".join(cells)}</tbody></table>'
    f'<div class="db-n">{tol_txt}. Tiers may differ in unit of time: a live tick against an '
    "official end-of-day value is not a like-for-like gap.</div>",
    unsafe_allow_html=True,
)

# --- 30-day chart, all visible tiers overlaid ---
st.subheader("Last 30 days, official daily values")
hist = _history(ctx, where, iid, ctx.hidden)
lines = []
for tier in (3, 2, 1):
    h = hist[hist["tier"] == tier]
    if not h.empty:
        lines.append(_kit.Line(
            f"{TIER_GLYPH[tier]} {TIER_NAME[tier]}",
            tuple((f"{d:%d %b}", float(v)) for d, v in zip(h["d"], h["value"], strict=True)),
            _kit.TIER_COLOUR[tier], _kit.TIER_DASH[tier],
        ))  # fmt: skip
unit = str(hist["unit"].iloc[0]) if not hist.empty else ""
st.markdown(_kit.tier_legend([t for t in (3, 2, 1) if t not in ctx.hidden]), unsafe_allow_html=True)
table = (
    hist.rename(columns={"d": "date"})[["date", "tier", "field", "value", "unit", "source"]]
    if not hist.empty
    else hist
)
_kit.show_chart(_kit.line_chart(lines, unit), table, "30-day values")

# --- lineage ---
st.subheader("Lineage")
lg = _lineage(ctx, where, best)
v = lg.value  # type: ignore[attr-defined]
facts = [
    ("Source", f"{v.source} / {v.dataset} / {v.symbol}"),
    ("Fetched", f"{to_et(v.ingested_at):%d %b %H:%M} ET, fetch {v.fetch_id}"),
    ("Latency", f"{lg.latency_ms or 'n/a'} ms, HTTP {lg.http_status or 'n/a'}"),  # type: ignore[attr-defined]
    ("Raw object", f"{lg.raw_key or 'n/a'}"),  # type: ignore[attr-defined]
    ("sha256", f"{lg.sha256 or 'n/a'}"),  # type: ignore[attr-defined]
    ("Parser", v.parser_version),
]
st.markdown(
    '<table class="db-table">'
    + "".join(f"<tr><th>{_kit.escape(k)}</th><td>{_kit.escape(x)}</td></tr>" for k, x in facts)
    + "</table>",
    unsafe_allow_html=True,
)

# --- check history ---
st.subheader("Check history")
chk = _checks(ctx, where, iid)
chk = _kit.keep_rows(chk, ("source",), _kit.exposed_sources(ctx.config, ctx.hidden))
if chk.empty:
    st.markdown("No checks recorded for this instrument in the last 14 days.")
else:
    st.dataframe(chk, hide_index=True, width="stretch")
