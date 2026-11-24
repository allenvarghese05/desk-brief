"""Instrument details (spec 10.3 page 2, folded into Home by T36; v2 by T58): the "Do the
sources agree?" verdict card with each visible tier's value, the lineage of a value and the
last fortnight of data checks.

Hosted mode never reads a hidden tier: the ladder queries one visible tier at a time and the
check list drops rows naming a source with a hidden-tier feed (they may carry its values).
Every figure goes through ``values.value_cell``. Caches are keyed on ``data.data_version()``
(T58). No markup here carries a ``title`` attribute (P-022).
"""

# ruff: noqa: RUF001  (the en dash marks an empty value on purpose)

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape

import pandas as pd
import streamlit as st
from app.components import data
from app.components.values import TIER_GLYPH, TIER_NAME, Provenance, status_marker, value_cell

from deskbrief.config import Config
from deskbrief.query import Lineage, ValueRow, field_label, latest_snapshot, lineages
from deskbrief.timeutil import to_et

CHECK_DAYS = 14
_STATUS_MARK = {"pass": "✓", "warn": "!", "fail": "✕", "skip": "–"}


def _where(config: Config) -> str:
    s = config.storage
    return s.bucket if s.backend == "r2" else s.local_root


@st.cache_data(ttl=data.DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _ladder(
    _config: Config, where: str, iid: str, hidden: frozenset[int], ver: str, _now: datetime
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


@st.cache_data(ttl=data.DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _lineage(_config: Config, where: str, row: ValueRow, ver: str) -> Lineage:
    with data.shared_connection(_config) as conn:
        return lineages(conn, [row])[0]


@st.cache_data(ttl=data.DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _checks(_config: Config, where: str, iid: str, today: date, ver: str) -> pd.DataFrame:
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
    rows = _ladder(config, _where(config), iid, hidden, data.data_version(config), now)
    return sorted(rows, key=lambda r: -r.tier)


def caveats(r: ValueRow) -> tuple[str, ...]:
    """The caveat words that stay visible beside a stored value (P-022): the same rule as the
    report's per-row qualifiers (``panels.flags``): a proxy instrument, an IEX (single-exchange)
    dataset, a fallback source."""
    out = ["proxy"] if r.is_proxy else []
    if r.dataset.startswith("iex"):
        out.append("IEX")
    if r.is_fallback:
        out.append("fallback")
    return tuple(out)


def _prov(r: ValueRow) -> Provenance:
    return Provenance(
        r.source, r.tier, f"{to_et(r.as_of):%d %b %H:%M}", field_label(r), flags=caveats(r)
    )


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


#: gap (bp) at or under which two values count as agreeing
AGREE_BP = 5.0
#: values further apart in time than this are from different moments (a gap beyond AGREE_BP
#: between them is "not comparable", not a disagreement)
LIKE_FOR_LIKE_S = 15 * 60


def _gap_bp(r: ValueRow, best: ValueRow) -> float | None:
    if r.unit == "pct":
        return (r.value - best.value) * 100
    return (r.value - best.value) / best.value * 10_000 if best.value else None


@dataclass(frozen=True)
class Verdict:
    """The answer to "do the sources agree?" for the visible tiers' latest values."""

    tone: str  # ok | warn | fail | muted
    mark: str  # a mark beside the answer; only the warn case shows the warn mark "!"
    headline: str  # the answer: Agree, Not comparable, No, One source only
    detail: str  # one sentence under the answer
    gap: str = ""  # the size of the gap (or the time apart) beside the answer
    note: str = ""  # a neutral note: the values agree but are from different moments


def verdict(rows: list[ValueRow]) -> Verdict | None:
    """Compare each visible tier's latest value with the official (highest) tier's (T60 bug 6).

    * every gap within :data:`AGREE_BP`: "Agree" (ok) with the gap; when the values are more
      than :data:`LIKE_FOR_LIKE_S` apart in time a neutral note says so (two sources 0.8 bp
      apart, a day apart, still agree);
    * a gap beyond :data:`AGREE_BP` between values more than :data:`LIKE_FOR_LIKE_S` apart:
      "Not comparable" (warn), with the time apart: a move between two moments is not a
      disagreement;
    * a gap beyond :data:`AGREE_BP` at the same moment: "No" (fail).
    """
    if not rows:
        return None
    best = rows[0]
    if len(rows) == 1:
        return Verdict("muted", "–", "One source only", "Nothing to cross-check yet.")
    gaps = [abs(g) for r in rows[1:] if (g := _gap_bp(r, best)) is not None]
    worst = max(gaps, default=0.0)
    apart = max(abs((x.as_of - best.as_of).total_seconds()) for x in rows[1:])
    n = len(rows)
    if worst <= AGREE_BP:
        note = f"The values are {_apart(apart)} apart in time." if apart > LIKE_FOR_LIKE_S else ""
        when = "" if note else " at the same moment"
        return Verdict(
            "ok", "✓", "Agree", f"{n} sources within {AGREE_BP:g} bp{when}.",
            f"{worst:.1f} bp apart", note,
        )  # fmt: skip
    if apart > LIKE_FOR_LIKE_S:
        return Verdict(
            "warn", "!", "Not comparable",
            f"{n} sources, {worst:.1f} bp apart, but their values are {_apart(apart)} apart "
            "in time, so the gap may be a move rather than a disagreement.",
            f"{_apart(apart)} apart",
        )  # fmt: skip
    return Verdict(
        "fail", "✕", "No", f"{n} sources disagree at the same moment.", f"{worst:.1f} bp apart"
    )


def _gap_text(r: ValueRow, best: ValueRow) -> str:
    """The gap of one row to the official value, as in :func:`verdict`: a gap beyond
    :data:`AGREE_BP` between different moments is "not comparable", a small one only names the
    time apart."""
    if r is best:
        return "reference"
    bp = _gap_bp(r, best)
    gap = f"{bp:+.1f} bp" if bp is not None else "–"
    apart = abs((r.as_of - best.as_of).total_seconds())
    if apart > LIKE_FOR_LIKE_S:
        too_far = bp is None or abs(bp) > AGREE_BP
        gap += f", {_apart(apart)} apart" + (": not comparable" if too_far else "")
    return gap


def verdict_card_html(rows: list[ValueRow], summary: str = "") -> str:
    """The "Do the sources agree?" block (P-023): its label over a hairline, the answer
    (15 px) with its gap, a sentence, then each visible tier's latest value as a flat row with
    its source, tier and gap to the official value. ``summary`` is a line about the last
    fortnight of cross-checks. Only the warn case shows the warn mark; agreement shows a
    status dot."""
    head = '<div class="db-label">Do the sources agree?</div>'
    v = verdict(rows)
    if v is None:
        return head + '<div class="db-empty">No value in a tier shown in this mode.</div>'
    best = rows[0]
    srcs = "".join(
        f'<div class="db-src-row"><span>{TIER_GLYPH[r.tier]} {escape(r.source)} · '
        f'{TIER_NAME[r.tier]}<span class="db-tagline">{escape(_gap_text(r, best))}</span></span>'
        f"<b>{value_cell(_num(r), _prov(r), compact=True)}</b></div>"
        for r in rows
    )
    if v.tone == "warn":
        mark = status_marker("warn")
    elif v.tone == "fail":
        mark = status_marker("fail")
    else:
        mark = f'<span class="db-dot db-dot-{v.tone}" aria-hidden="true"></span>'
    gap = f'<span class="db-verdict-gap">{escape(v.gap)}</span>' if v.gap else ""
    note = f'<p class="db-verdict-p db-verdict-note">{escape(v.note)}</p>' if v.note else ""
    more = f'<p class="db-verdict-sum">{escape(summary)}</p>' if summary else ""
    return (
        f'{head}<div class="db-verdict db-verdict-{v.tone}">{mark}'
        f"<b>{escape(v.headline)}</b>{gap}</div>"
        f'<p class="db-verdict-p">{escape(v.detail)}</p>{note}{more}'
        f'<div class="db-srcs">{srcs}</div>'
    )


def lineage_html(config: Config, row: ValueRow) -> str:
    lg = _lineage(config, _where(config), row, data.data_version(config))
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
    df = _checks(config, _where(config), iid, today, data.data_version(config))
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
