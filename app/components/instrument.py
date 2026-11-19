"""Instrument details for the workspace's right-hand panel (spec 10.3 page 2, folded into Home
by T36): the tier ladder, lineage of the shown value and the last fortnight of checks.

Hosted mode never reads a hidden tier: the ladder queries one visible tier at a time and the
check list drops rows naming a source with a hidden-tier feed (they may carry its values).
Every figure goes through ``values.value_cell`` with its full provenance line.
"""

# ruff: noqa: RUF001  (the en dash marks an empty value on purpose)

from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape

import pandas as pd
import streamlit as st
from app.components import data
from app.components.values import TIER_GLYPH, TIER_NAME, Provenance, value_cell

from deskbrief.config import Config
from deskbrief.query import Lineage, ValueRow, field_label, latest_snapshot, lineages
from deskbrief.timeutil import to_et

CHECK_DAYS = 14
_STATUS_MARK = {"pass": "✓", "warn": "!", "fail": "✕", "skip": "–"}


def _where(config: Config) -> str:
    s = config.storage
    return s.bucket if s.backend == "r2" else s.local_root


@st.cache_data(ttl=60, show_spinner=False)
def _ladder(
    _config: Config, where: str, iid: str, hidden: frozenset[int], _now: datetime
) -> list[ValueRow]:
    rows: list[ValueRow] = []
    with data.shared_connection(_config) as conn:
        for tier in (1, 2, 3):
            if tier in hidden:
                continue
            others = {1, 2, 3} - {tier}
            rows += [r for r in latest_snapshot(conn, _now, exclude_tiers=others)
                     if r.instrument_id == iid]  # fmt: skip
    return rows


@st.cache_data(ttl=60, show_spinner=False)
def _lineage(_config: Config, where: str, row: ValueRow) -> Lineage:
    with data.shared_connection(_config) as conn:
        return lineages(conn, [row])[0]


@st.cache_data(ttl=60, show_spinner=False)
def _checks(_config: Config, where: str, iid: str, today: date) -> pd.DataFrame:
    with data.shared_connection(_config) as conn:
        return conn.execute(
            "SELECT target_date, check_name, source, status FROM dq WHERE instrument_id = ? "
            "AND target_date > ? ORDER BY target_date DESC, check_name",
            [iid, today - timedelta(days=CHECK_DAYS)],
        ).fetchdf()


def exposed_sources(config: Config, hidden: frozenset[int]) -> frozenset[str]:
    """Sources with a feed in a hidden tier (their check rows are withheld when hosted)."""
    return frozenset(f.source for i in config.instruments for f in i.feeds if f.tier in hidden)


def ladder(config: Config, iid: str, hidden: frozenset[int], now: datetime) -> list[ValueRow]:
    """Latest value per visible tier, highest tier number (official) first."""
    rows = _ladder(config, _where(config), iid, hidden, now)
    return sorted(rows, key=lambda r: -r.tier)


def _prov(r: ValueRow) -> Provenance:
    return Provenance(r.source, r.tier, f"{to_et(r.as_of):%d %b %H:%M}", field_label(r))


_PLAIN_UNITS = {"USD", "index_pts", "shares"}


def _num(r: ValueRow) -> str:
    """Up to four decimals, at least two; yields as percent; unusual units named."""
    if r.unit == "pct":
        return f"{r.value:.3f}%"
    text = f"{r.value:,.4f}"
    while text.endswith("0") and len(text.split(".")[1]) > 2:
        text = text[:-1]
    return text if r.unit in _PLAIN_UNITS else f"{text} {r.unit}"


def _apart(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 120:
        return f"{m} min"
    return f"{m / 60:.1f} h" if m < 2880 else f"{m / 1440:.1f} d"


#: gap (bp) at or under which two same-moment values count as agreeing
AGREE_BP = 5.0
LIKE_FOR_LIKE_S = 15 * 60


def _gap_bp(r: ValueRow, best: ValueRow) -> float | None:
    if r.unit == "pct":
        return (r.value - best.value) * 100
    return (r.value - best.value) / best.value * 10_000 if best.value else None


def verdict_html(rows: list[ValueRow]) -> str:
    """One line answering "do the sources agree?", shown under the price."""
    if not rows:
        return ""
    best = rows[0]
    names = ", ".join(f"{r.source} ({TIER_NAME[r.tier]})" for r in rows)
    if len(rows) == 1:
        tone, text = "muted", f"One source only: {names}. Nothing to cross-check yet."
    else:
        gaps = [abs(g) for r in rows[1:] if (g := _gap_bp(r, best)) is not None]
        worst = max(gaps, default=0.0)
        apart = max(abs((x.as_of - best.as_of).total_seconds()) for x in rows[1:])
        if apart > LIKE_FOR_LIKE_S:
            tone = "warn"
            text = (
                f"{len(rows)} sources, {worst:.1f} bp apart, but their prices are "
                f"{_apart(apart)} apart in time: not like-for-like."
            )
        elif worst <= AGREE_BP:
            tone, text = "ok", f"{len(rows)} sources agree within {worst:.1f} bp."
        else:
            tone, text = (
                "fail",
                f"{len(rows)} sources disagree by {worst:.1f} bp at the same moment.",
            )
        text += f" ({names})"
    return (
        f'<div class="db-verdict db-verdict-{tone}" title="compares each tier\'s latest value">'
        f'<span class="db-dot db-dot-{"unknown" if tone == "muted" else tone}"></span>'
        f"<b>Do the sources agree?</b> {escape(text)}</div>"
    )


def ladder_html(rows: list[ValueRow]) -> str:
    """Each visible tier's latest value and its gap to the official (highest) tier."""
    if not rows:
        return '<div class="db-empty">No value in a tier shown in this mode.</div>'
    best = rows[0]
    body = ""
    for r in rows:
        if r is best:
            gap = "reference"
        elif r.unit == "pct":
            gap = f"{(r.value - best.value) * 100:+.1f} bp"
        elif best.value:
            gap = f"{(r.value - best.value) / best.value * 10_000:+.1f} bp"
        else:
            gap = "–"
        apart = abs((r.as_of - best.as_of).total_seconds())
        if r is not best and apart > 15 * 60:  # a gap between different moments is not a gap
            gap += f", {_apart(apart)} apart: not like-for-like"
        body += (
            f"<tr><th>{TIER_GLYPH[r.tier]} {TIER_NAME[r.tier]}"
            f'<span class="db-tagline">{escape(gap)}</span></th>'
            f"<td>{value_cell(_num(r), _prov(r))}</td></tr>"
        )
    return f'<table class="db-dl">{body}</table>'


def lineage_html(config: Config, row: ValueRow) -> str:
    lg = _lineage(config, _where(config), row)
    v = lg.value
    facts = [
        ("Source", f"{v.source} / {v.dataset} / {v.symbol}"),
        ("Fetched", f"{to_et(v.ingested_at):%d %b %H:%M} ET"),
        ("Fetch id", v.fetch_id),
        ("Latency", f"{lg.latency_ms or '–'} ms, HTTP {lg.http_status or '–'}"),
        ("Raw object", lg.raw_key or "–"),
        ("sha256", (lg.sha256 or "–")[:16] + ("…" if lg.sha256 else "")),
        ("Parser", v.parser_version),
    ]
    body = "".join(f"<tr><th>{escape(k)}</th><td>{escape(str(x))}</td></tr>" for k, x in facts)
    return f'<table class="db-dl">{body}</table>'


def checks_html(config: Config, iid: str, hidden: frozenset[int], today: date) -> str:
    df = _checks(config, _where(config), iid, today)
    exposed = exposed_sources(config, hidden)
    if not df.empty and exposed:
        df = df[~df["source"].isin(exposed)]
    if df.empty:
        return f'<div class="db-empty">No checks recorded in the last {CHECK_DAYS} days.</div>'
    lines = []
    for r in df.head(12).itertuples():
        st_ = str(r.status)
        lines.append(
            f"<div><span>{escape(str(r.check_name))}"
            f'<span class="db-note"> · {r.target_date:%d %b}</span></span>'
            f'<span class="db-st-{escape(st_)}">{_STATUS_MARK.get(st_, "?")} {escape(st_)}</span>'
            "</div>"
        )
    more = f'<div class="db-note">{len(df) - 12} more on Data health</div>' if len(df) > 12 else ""
    return f'<div class="db-checks">{"".join(lines)}{more}</div>'
