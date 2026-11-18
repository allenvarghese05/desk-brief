"""Shared helpers for the dashboard pages 2-6 (spec 10.3).

Lives in ``app/components`` (not ``app/pages``) so Streamlit does not list it as a page.

* :func:`context`: config, display mode, hidden tiers, read-only store; stops the page with a
  message when the store cannot be read.
* serving files (spec 6) through a cache: :func:`serving_frame`, :func:`serving_json`;
  partitioned tables for drill-down: :func:`recent_table`.
* hosted rule: :func:`exposed_sources` have a feed in a hidden tier; rows that may carry their
  values are dropped and counted (:func:`keep_rows`), never shown.
* small inline-SVG charts (dataviz skill: thin marks, one axis, direct labels, a table view next
  to every chart, secondary encoding by dash and marker as well as colour).
"""

from __future__ import annotations

import io
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app import theme  # noqa: E402
from app.components import data  # noqa: E402
from app.components.values import TIER_GLYPH, TIER_NAME  # noqa: E402

from deskbrief.config import Config, load_config  # noqa: E402
from deskbrief.store.backend import StorageBackend, StorageError, serving_key, table_prefix  # noqa: E402
from deskbrief.store.local import LocalBackend  # noqa: E402
from deskbrief.store.r2 import R2Backend  # noqa: E402
from deskbrief.timeutil import et_date, to_et  # noqa: E402

#: categorical slots of the reference palette (blue, orange, aqua), one per tier.
#: Tier is also told by the glyph, the dash pattern and the label, never by colour alone.
TIER_COLOUR = {3: "#2a78d6", 2: "#eb6834", 1: "#1baf7a"}
TIER_DASH = {3: "", 2: "5 3", 1: "1.5 3"}
STATUS_MARK = {"pass": "✓", "warn": "!", "fail": "✕", "skip": "-"}
STATUS_COLOUR = {"pass": theme.UP, "warn": theme.WARN, "fail": theme.FAIL, "skip": theme.MUTED}

PAGE_CSS = f"""
<style>
.db-strip-row {{ display: flex; gap: 3px; align-items: center; }}
.db-sq {{ width: 20px; height: 20px; display: inline-flex; align-items: center;
  justify-content: center; font-size: .72rem; color: #fff; border-radius: 2px; }}
.db-sq-label {{ color: {theme.MUTED}; font-size: .72rem; margin-left: .5rem; }}
.db-legend {{ display: flex; gap: 1.2rem; font-size: .8rem; color: {theme.MUTED};
  margin: .2rem 0 .4rem 0; flex-wrap: wrap; }}
.db-chart text {{ font-size: 11px; fill: {theme.MUTED}; }}
.db-n {{ color: {theme.MUTED}; font-size: .85rem; }}
</style>
"""


@dataclass(frozen=True)
class Ctx:
    config: Config
    mode: str
    hidden: frozenset[int]
    store: StorageBackend
    now: datetime

    @property
    def today(self) -> date:
        return et_date(self.now)


def open_store(config: Config, env: dict[str, str] | os._Environ[str]) -> StorageBackend:
    """Read-only store: ``dashboard-reader`` keys on R2, the local backend for development."""
    if config.storage.backend == "r2":
        return R2Backend.from_env(config.storage.bucket, data.reader_env(env))
    return LocalBackend(config.storage.local_root)


@st.cache_resource
def _config(path: str) -> Config:
    return load_config(path)


def context(title: str) -> Ctx:
    """Common page start: set up the page, read mode and store, or stop with a message."""
    st.set_page_config(page_title=f"{title} · Desk Brief", layout="wide")
    theme.apply()
    st.markdown(PAGE_CSS, unsafe_allow_html=True)
    path = os.environ.get("DESKBRIEF_CONFIG") or str(ROOT / "config.yaml")
    config = _config(path)
    mode = data.resolve_mode(os.environ.get(config.dashboard.mode_env))
    try:
        store = open_store(config, os.environ)
    except StorageError as exc:  # names the missing variables, never their values
        st.error(f"Cannot read the data store: {exc}")
        st.stop()
    st.markdown(
        f'<div class="db-top"><span class="db-brand">{escape(title)}</span>'
        f'<span class="db-mode">{escape(mode)}</span></div><hr class="db-rule">',
        unsafe_allow_html=True,
    )
    return Ctx(config, mode, data.hidden_tiers(mode, config), store, data.current_time())


def where_key(ctx_config: Config) -> str:
    s = ctx_config.storage
    return s.bucket if s.backend == "r2" else s.local_root


def exposed_sources(config: Config, hidden: frozenset[int]) -> frozenset[str]:
    """Sources with any feed in a hidden tier. Rows naming them may carry tier-1 values (a
    cross-check, a validity result, a revision), so hosted mode withholds those rows whole."""
    return frozenset(f.source for i in config.instruments for f in i.feeds if f.tier in hidden)


def keep_rows(df: pd.DataFrame, cols: tuple[str, ...], exposed: frozenset[str]) -> pd.DataFrame:
    """``df`` without rows whose ``cols`` name an exposed source."""
    if df.empty or not exposed:
        return df
    mask = pd.Series(False, index=df.index)
    for c in cols:
        mask |= df[c].isin(exposed)
    return df[~mask]


# ---------------------------------------------------------------------------
# reading (cached 60 s; the leading-underscore arguments are not hashed)
# ---------------------------------------------------------------------------


def _table(raw: bytes) -> pd.DataFrame:
    return pq.read_table(io.BytesIO(raw)).to_pandas()


@st.cache_data(ttl=data.CACHE_TTL_SECONDS, show_spinner=False)
def _serving_frame(_store: StorageBackend, where: str, name: str) -> pd.DataFrame:
    try:
        return _table(_store.get_bytes(serving_key(name)))
    except StorageError:
        return pd.DataFrame()


def serving_frame(ctx: Ctx, name: str) -> pd.DataFrame:
    """A ``serving/*.parquet`` file; empty frame when it is not there yet."""
    return _serving_frame(ctx.store, where_key(ctx.config), name)


@st.cache_data(ttl=data.CACHE_TTL_SECONDS, show_spinner=False)
def _serving_json(_store: StorageBackend, where: str, name: str) -> dict[str, object]:
    try:
        return dict(json.loads(_store.get_bytes(serving_key(name))))
    except (StorageError, ValueError):
        return {}


def serving_json(ctx: Ctx, name: str) -> dict[str, object]:
    return _serving_json(ctx.store, where_key(ctx.config), name)


@st.cache_data(ttl=data.CACHE_TTL_SECONDS, show_spinner=False)
def _recent_table(_store: StorageBackend, where: str, table: str, since: date) -> pd.DataFrame:
    frames = []
    for key in sorted(_store.list(table_prefix(table))):
        if not key.endswith(".parquet"):
            continue
        part = next((p[5:15] for p in key.split("/") if p.startswith("date=")), "")
        if part and date.fromisoformat(part) >= since:
            frames.append(_table(_store.get_bytes(key)))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def recent_table(ctx: Ctx, table: str, days: int) -> pd.DataFrame:
    """Partitions of a daily table (revisions, quarantine) of the last ``days`` days."""
    return _recent_table(ctx.store, where_key(ctx.config), table, ctx.today - timedelta(days=days))


def withheld_note(n: int, mode: str) -> None:
    if n:
        st.caption(
            f"{n} row(s) withheld in {mode} mode: they carry live (tier 1) data, which is not "
            "shown here until provider display terms are verified."
        )


def et_text(ts: object) -> str:
    """ET text of a timestamp, or ``n/a``."""
    if ts is None or pd.isna(ts):
        return "n/a"
    return f"{to_et(pd.Timestamp(ts).to_pydatetime()):%d %b %H:%M} ET"


def day_text(d: object) -> str:
    return "n/a" if d is None or pd.isna(d) else f"{pd.Timestamp(d):%d %b}"


# ---------------------------------------------------------------------------
# marks
# ---------------------------------------------------------------------------


def status_strip(items: list[tuple[str, str, str]]) -> str:
    """Squares ``(label, status, hover)`` oldest first: glyph and colour, never colour alone."""
    sq = "".join(
        f'<span class="db-sq" style="background:{STATUS_COLOUR.get(s, theme.MUTED)}" '
        f'title="{escape(hover)}" aria-label="{escape(lab)} {escape(s)}">'
        f"{STATUS_MARK.get(s, '-')}</span>"
        for lab, s, hover in items
    )
    return f'<span class="db-strip-row">{sq}</span>'


def status_legend() -> str:
    parts = "".join(
        f'<span><span class="db-sq" style="background:{STATUS_COLOUR[s]}">{STATUS_MARK[s]}</span>'
        f" {s}</span>"
        for s in ("pass", "warn", "fail", "skip")
    )
    return f'<div class="db-legend">{parts}</div>'


def tier_legend(tiers: list[int]) -> str:
    parts = "".join(
        f'<span><svg width="26" height="8" aria-hidden="true"><line x1="0" y1="4" x2="26" y2="4" '
        f'stroke="{TIER_COLOUR[t]}" stroke-width="2" stroke-dasharray="{TIER_DASH[t]}"/></svg> '
        f"{TIER_GLYPH[t]} {TIER_NAME[t]}</span>"
        for t in tiers
    )
    return f'<div class="db-legend">{parts}</div>'


@dataclass(frozen=True)
class Line:
    name: str
    points: tuple[tuple[str, float], ...]  # (x label, y)
    colour: str = theme.INK
    dash: str = ""


def line_chart(
    lines: list[Line],
    unit: str,
    *,
    band: tuple[float, float] | None = None,
    guides: tuple[float, ...] = (),
    width: int = 640,
    height: int = 200,
) -> str:
    """Thin lines on one y axis (never two), the last point labelled directly, hover titles on
    every point. ``band`` shades a tolerance range; ``guides`` draws dashed reference lines."""
    lines = [  # a NaN would turn every coordinate into "nan": drop such points
        Line(ln.name, tuple((x, y) for x, y in ln.points if y == y), ln.colour, ln.dash)
        for ln in lines
    ]
    xs = sorted({x for ln in lines for x, _ in ln.points})
    ys = [y for ln in lines for _, y in ln.points] + list(guides) + list(band or ())
    if not xs or not ys:
        return ""
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    lo, hi = lo - span * 0.08, hi + span * 0.08
    left, right, top, bottom = 52, 90, 8, 22

    def px(x: str) -> float:
        i = xs.index(x)
        return left + (width - left - right) * (i / (len(xs) - 1) if len(xs) > 1 else 0.5)

    def py(y: float) -> float:
        return top + (height - top - bottom) * (1 - (y - lo) / (hi - lo))

    out = [f'<svg class="db-chart" viewBox="0 0 {width} {height}" role="img" width="100%">']
    for g in (lo + (hi - lo) * k / 3 for k in range(4)):  # recessive grid, four ticks
        out.append(
            f'<line x1="{left}" x2="{width - right}" y1="{py(g):.1f}" y2="{py(g):.1f}" '
            f'stroke="{theme.RULE}" stroke-width="1"/>'
            f'<text x="{left - 6}" y="{py(g) + 4:.1f}" text-anchor="end">{g:,.4g}</text>'
        )
    if band:
        out.append(
            f'<rect x="{left}" y="{py(band[1]):.1f}" width="{width - left - right}" '
            f'height="{py(band[0]) - py(band[1]):.1f}" fill="{theme.RULE}" opacity=".55"/>'
        )
    for g in guides:
        out.append(
            f'<line x1="{left}" x2="{width - right}" y1="{py(g):.1f}" y2="{py(g):.1f}" '
            f'stroke="{theme.MUTED}" stroke-width="1" stroke-dasharray="2 3"/>'
            f'<text x="{width - right + 4}" y="{py(g) + 4:.1f}">{g:,.4g}</text>'
        )
    for x in (xs[0], xs[-1]):
        anchor = "start" if x == xs[0] else "end"
        out.append(
            f'<text x="{px(x):.1f}" y="{height - 6}" text-anchor="{anchor}">{escape(x)}</text>'
        )
    for ln in lines:
        pts = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in ln.points)
        dash = f' stroke-dasharray="{ln.dash}"' if ln.dash else ""
        out.append(
            f'<polyline points="{pts}" fill="none" stroke="{ln.colour}" stroke-width="2" '
            f'stroke-linejoin="round"{dash}/>'
        )
        for x, y in ln.points:  # >= 8 px hover targets with a surface ring
            out.append(
                f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="4" fill="{ln.colour}" '
                f'stroke="#fff" stroke-width="2"><title>{escape(ln.name)} · {escape(x)} · '
                f"{y:,.6g} {escape(unit)}</title></circle>"
            )
        if ln.points:
            x, y = ln.points[-1]
            out.append(
                f'<text x="{px(x) + 8:.1f}" y="{py(y) + 4:.1f}" style="fill:{theme.INK}">'
                f"{escape(ln.name)}</text>"
            )
    out.append("</svg>")
    return "".join(out)


def show_chart(html: str, table: pd.DataFrame, label: str) -> None:
    """The chart, with its numbers in a table view beneath it."""
    if not html:
        st.markdown(
            '<span class="db-unavailable">no data in this window</span>', unsafe_allow_html=True
        )
        return
    st.markdown(html, unsafe_allow_html=True)
    with st.expander(f"{label}: table view"):
        st.dataframe(table, hide_index=True, width="stretch")
