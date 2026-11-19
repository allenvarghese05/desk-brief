"""Instrument tearsheet (T36 phase 1): what an analyst needs about one instrument on one screen.

* performance over horizons (1D from the report; 1W, 1M, 3M, YTD, 1Y from daily closes),
  a weekly series (``1w``) shows its week-on-week change first and says that daily-move
  statistics need daily values; index levels and positions change in points, never in %,
* risk and range (realised volatility, today's move in standard deviations, drawdown, 52-week
  range), each saying how many days it needs when the store holds fewer,
* every value we hold for the instrument: one line per source, dataset, field and interval,
  with when it was true (as of) and when we fetched it,
* source agreement over time (cross-checks naming the instrument) and corrections (revisions).

Every figure is printed through ``values.value_cell`` with a :class:`Provenance` naming its
source, tier, date and what it is. Derived statistics name the closes they were computed from.
Hosted mode never reads a hidden tier (SQL excludes them; cross-checks naming a source with a
hidden-tier feed are withheld whole).
"""
# ruff: noqa: RUF001  (minus, sigma and en dash are intentional output)

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape

import pandas as pd
import streamlit as st
from app.components import data
from app.components.instrument import exposed_sources
from app.components.values import TIER_GLYPH, TIER_NAME, Provenance, value_cell

from deskbrief.config import Config
from deskbrief.query import LEVEL_FIELDS
from deskbrief.report.model import Row
from deskbrief.timeutil import et_date, to_et

_MINUS = "−"
VOL_DAYS = 20  # realised volatility window (trading days)
SIGMA_MIN_DAYS = 20  # fewest daily moves for a standard deviation
HORIZONS: tuple[tuple[str, int | None], ...] = (
    ("1W", 7),
    ("1M", 30),
    ("3M", 91),
    ("YTD", None),
    ("1Y", 365),
)
XCHECK_DAYS = 14
REVISION_DAYS = 30


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _signed(x: float, unit: str) -> str:
    sign = "+" if x > 0 else _MINUS if x < 0 else ""
    decimals = 1 if unit == "%" else 2 if unit == " pts" and abs(x) < 10 else 0
    return f"{sign}{abs(x):,.{decimals}f}{unit}"


def _is_yield(bars: pd.DataFrame) -> bool:
    return not bars.empty and str(bars["unit"].iloc[-1]) == "pct"


def _is_weekly(bars: pd.DataFrame) -> bool:
    """Bars from :func:`data.build_bars` say their interval; a frame without one is daily."""
    return not bars.empty and "interval" in bars and str(bars["interval"].iloc[-1]) == "1w"


def _mode(bars: pd.DataFrame) -> str:
    """Unit of a move: bp for yields, points (or contracts) for levels and positions, which
    may be zero or negative (NFCI), else %."""
    if _is_yield(bars):
        return "bp"
    if not bars.empty and str(bars["field"].iloc[-1]) in LEVEL_FIELDS:
        return " contracts" if str(bars["unit"].iloc[-1]) == "contracts" else " pts"
    return "%"


def _latest(row: Row, bars: pd.DataFrame) -> tuple[float, date] | None:
    """The latest value and its ET date: the report's (may be intraday), else the last close."""
    if row.change is not None:
        return row.change.last.value, et_date(row.change.last.as_of)
    if bars.empty:
        return None
    return float(bars["close"].iloc[-1]), bars["d"].iloc[-1].date()


def _bar_prov(
    bars: pd.DataFrame, what: str, since: date | None = None, latest: Row | None = None
) -> Provenance:
    """Provenance of a statistic over daily bars (plus the report's latest value, if used).

    The glyph shows the lowest (most live) tier involved, so a tier-1 input is never hidden
    behind an "official" glyph; a mix of tiers is said in words."""
    used = bars if since is None else bars[bars["d"].dt.date >= since]
    used = used if not used.empty else bars
    tiers = {int(t) for t in used["tier"]}
    sources = {str(s) for s in used["source"]}
    if latest is not None and latest.change is not None:
        tiers.add(latest.change.last.tier)
        sources.add(latest.change.last.source)
    a, b = used["d"].iloc[0], used["d"].iloc[-1]
    span = f"{b:%d %b}" if a == b else f"{a:%d %b} – {b:%d %b}"
    if len(tiers) > 1:
        what += "; mixed tiers: " + ", ".join(TIER_NAME[t] for t in sorted(tiers, reverse=True))
    return Provenance(", ".join(sorted(sources)), min(tiers), span, what)


def _move(now: float, then: float, mode: str) -> tuple[float, str]:
    """A change in basis points for yields, in points for levels (see :func:`_mode`), in
    percent otherwise."""
    if mode == "bp":
        return (now - then) * 100, "bp"
    if mode != "%":
        return now - then, mode
    return ((now / then) - 1) * 100 if then else 0.0, "%"


def _utc(ts: object) -> datetime:
    """A stored timestamp as an aware UTC datetime (DuckDB may hand back naive UTC)."""
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.to_pydatetime()  # type: ignore[no-any-return]


def _need(n_need: int, n_have: int) -> str:
    return (
        f'<span class="db-unavailable" title="needs {n_need} days of history, '
        f'the store holds {n_have}">needs {n_need}d</span>'
    )


# ---------------------------------------------------------------------------
# performance over horizons
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Horizon:
    label: str
    html: str  # a value_cell, or a "needs" note


def horizons(row: Row, bars: pd.DataFrame) -> list[Horizon]:
    """1D from the report (with its basis); longer horizons against the close on or before the
    horizon's start. Never extrapolated: too little history says so."""
    out: list[Horizon] = []
    latest = _latest(row, bars)
    if row.change is not None and row.change.change is not None and row.change_text:
        last = row.change.last
        p = Provenance(
            last.source, last.tier, f"{to_et(last.as_of):%d %b %H:%M}", f"change vs {row.basis}"
        )
        text = row.change_text
        cls = "db-up" if text.startswith("+") else "db-down" if text.startswith(_MINUS) else ""
        first = "WoW" if last.interval == "1w" else "1D"  # a weekly change is week-on-week
        cell = value_cell(text, p, compact=True)
        out.append(Horizon(first, f'<span class="{cls}">{cell}</span>'))
    else:
        out.append(Horizon("1D", '<span class="db-unavailable">no change yet</span>'))
    if latest is None or bars.empty:
        return out + [Horizon(lab, '<span class="db-unavailable">no history</span>')
                      for lab, _ in HORIZONS]  # fmt: skip
    value, day = latest
    mode = _mode(bars)
    first = bars["d"].iloc[0].date()
    for label, days in HORIZONS:
        start = date(day.year - 1, 12, 31) if days is None else day - timedelta(days=days)
        before = bars[bars["d"].dt.date <= start]
        if first > start or before.empty:
            need = (day - start).days
            out.append(Horizon(label, _need(need, (day - first).days)))
            continue
        base = before.iloc[-1]
        chg, unit = _move(value, float(base["close"]), mode)
        base_tier, base_src = int(base["tier"]), str(base["source"])
        if row.change is not None:  # the latest value can be another feed than the base close
            last = row.change.last
            what = (
                f"change of the latest value ({last.source}, {TIER_NAME[last.tier]}) since the "
                f"{base['field']} of {base['d']:%d %b %Y} ({base_src}, {TIER_NAME[base_tier]})"
            )
            src = base_src if last.source == base_src else f"{last.source} vs {base_src}"
            tier = min(last.tier, base_tier)
        else:
            what = f"change since the {base['field']} of {base['d']:%d %b %Y}"
            src, tier = base_src, base_tier
        p = Provenance(src, tier, f"{base['d']:%d %b %Y}", what)
        text = _signed(chg, unit)
        cls = "db-up" if text.startswith("+") else "db-down" if text.startswith(_MINUS) else ""
        out.append(
            Horizon(label, f'<span class="{cls}">{value_cell(text, p, compact=True)}</span>')
        )
    return out


def horizons_html(items: list[Horizon]) -> str:
    cells = "".join(
        f'<div class="db-hz"><span class="db-hz-label">{escape(h.label)}</span>{h.html}</div>'
        for h in items
    )
    return f'<div class="db-hzs" aria-label="performance over horizons">{cells}</div>'


# ---------------------------------------------------------------------------
# risk and range
# ---------------------------------------------------------------------------


def _moves(bars: pd.DataFrame) -> pd.Series:
    closes = bars["close"].astype(float)
    if _is_yield(bars):
        return closes.diff().dropna() * 100  # bp
    if _mode(bars) != "%":
        return closes.diff().dropna()  # points: a level may be zero or negative
    return (closes.apply(math.log).diff().dropna()) * 100  # log return in %


def _num(v: float, unit: str) -> str:
    if unit == "pct":
        return f"{v:.2f}%"
    return f"{v:,.2f}" if abs(v) >= 10 else f"{v:,.4f}"


def risk_html(row: Row, bars: pd.DataFrame) -> str:
    """Realised vol, today's move in sigmas, max drawdown and the 52-week range, honestly."""
    if bars.empty:
        return '<div class="db-empty">No daily history yet.</div>'
    yields = _is_yield(bars)
    mode = _mode(bars)
    unit = str(bars["unit"].iloc[-1])
    moves = _moves(bars)
    n = len(moves)
    facts: list[tuple[str, str]] = []
    weekly = _is_weekly(bars)
    # 20 weekly moves are not "20-day" volatility, and a % volatility of a level that may cross
    # zero means nothing: both say so instead of printing a mislabelled number
    skip = (
        "weekly series: needs daily values" if weekly
        else None if mode in ("%", "bp") else "not computed for a level series"
    )  # fmt: skip
    if skip:
        note = f'<span class="db-unavailable">{skip}</span>'
        facts += [("Realised vol 20d", note), ("Latest move", note)]
    elif n >= VOL_DAYS:
        vol = float(moves.tail(VOL_DAYS).std()) * math.sqrt(252)
        text = f"{vol:,.0f} bp" if yields else f"{vol:.1f}%"
        p = _bar_prov(bars.tail(VOL_DAYS + 1), f"{VOL_DAYS}-day realised volatility, annualised")
        facts.append(("Realised vol 20d", value_cell(text, p)))
    else:
        facts.append(("Realised vol 20d", _need(VOL_DAYS + 1, n + 1)))
    latest = _latest(row, bars)
    if skip is None and latest is not None and n >= SIGMA_MIN_DAYS:
        value, day = latest
        last_close = float(bars["close"].iloc[-1])
        intraday = day > bars["d"].iloc[-1].date()
        today = _move(value, last_close, mode)[0] if intraday else float(moves.iloc[-1])
        hist = moves if intraday else moves.iloc[:-1]
        sd = float(hist.tail(60).std())
        if sd > 0:
            p = _bar_prov(
                bars.tail(61),
                f"latest move in standard deviations of {len(hist.tail(60))} daily moves",
                latest=row if intraday else None,
            )
            facts.append(("Latest move", value_cell(f"{today / sd:+.1f}σ".replace("-", _MINUS), p)))
    elif skip is None:
        facts.append(("Latest move", _need(SIGMA_MIN_DAYS + 1, n + 1)))
    if mode == "%" and len(bars) >= 2:
        closes = bars["close"].astype(float)
        dd = float((closes / closes.cummax() - 1).min()) * 100
        p = _bar_prov(bars, "largest fall from a high, daily closes")
        facts.append(("Max drawdown", value_cell(_signed(dd, "%"), p)))
    year = bars[bars["d"] >= bars["d"].iloc[-1] - pd.Timedelta(days=365)]
    hi, lo = float(year["close"].max()), float(year["close"].min())
    days = (year["d"].iloc[-1] - year["d"].iloc[0]).days
    span, step = ("weekly", "weeks") if weekly else ("daily", "days")
    label = "52-week range" if days >= 360 else f"Range, {len(year)} {step}"
    p = _bar_prov(year, f"lowest and highest {span} {year['field'].iloc[-1]}")
    facts.append((label, value_cell(f"{_num(lo, unit)} – {_num(hi, unit)}", p)))
    body = "".join(f"<tr><th>{escape(k)}</th><td>{v}</td></tr>" for k, v in facts)
    bar = ""
    if latest is not None and hi > lo:
        pos = min(1.0, max(0.0, (latest[0] - lo) / (hi - lo)))
        bar = (
            f'<div class="db-range" role="img" aria-label="latest value at {pos:.0%} of the '
            f'range"><span style="left:calc({pos * 100:.1f}% - 1px)"></span></div>'
        )
    return f'<table class="db-dl">{body}</table>{bar}'


# ---------------------------------------------------------------------------
# every value we hold
# ---------------------------------------------------------------------------


def _where(config: Config) -> str:
    s = config.storage
    return s.bucket if s.backend == "r2" else s.local_root


def _tiers_sql(hidden: frozenset[int]) -> str:
    return ", ".join(str(int(t)) for t in (1, 2, 3) if t not in hidden) or "0"


@st.cache_data(ttl=60, show_spinner=False)
def _all_values(
    _config: Config, where: str, iid: str, hidden: frozenset[int], _now: datetime
) -> pd.DataFrame:
    with data.shared_connection(_config) as conn:
        return conn.execute(
            "SELECT source, dataset, symbol, field, interval, tier, value, unit, as_of, "  # noqa: S608 (tiers are ints)
            "ingested_at, fetch_id, is_fallback, is_derived FROM obs_pit(?) "
            f"WHERE instrument_id = ? AND tier IN ({_tiers_sql(hidden)}) "
            "QUALIFY row_number() OVER (PARTITION BY source, dataset, field, interval "
            "ORDER BY as_of DESC, ingested_at DESC) = 1 "
            "ORDER BY tier DESC, field, source, interval",
            [_now, iid],
        ).fetchdf()


_FIELD_TEXT = {"last": "last trade", "close": "close", "open": "open", "high": "high",
               "low": "low", "volume": "volume", "yield": "yield", "rate": "rate",
               "level": "level", "net_position": "net speculative position",
               "open_interest": "open interest"}  # fmt: skip
_INTERVAL_TEXT = {"tick": "quote", "1m": "1-minute bar", "1d": "daily", "1w": "weekly",
                  "1mo": "monthly"}  # fmt: skip
#: intervals whose value is a date's (00:00 ET stamp), shown without a time
_DATED = ("1d", "1w", "1mo")


def _age(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    if m < 60:
        return f"{m}m ago"
    if m < 48 * 60:
        return f"{m // 60}h ago"
    return f"{m // 1440}d ago"


def _value_text(v: float, unit: str, field: str) -> str:
    if field == "volume" or unit in ("shares", "contracts"):
        return f"{v:,.0f}"
    if unit == "pct":
        return f"{v:.3f}%"
    return f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 10 else f"{v:,.2f}"


def all_values_html(
    config: Config, iid: str, hidden: frozenset[int], now: datetime
) -> tuple[str, int]:
    """One line per source, dataset, field and interval: value, as of, fetched. (html, count)"""
    df = _all_values(config, _where(config), iid, hidden, now)
    if df.empty:
        return '<div class="db-empty">No values stored for this instrument.</div>', 0
    rows = []
    for r in df.itertuples():
        as_of = to_et(_utc(r.as_of))
        fetched = _utc(r.ingested_at)
        field = _FIELD_TEXT.get(str(r.field), str(r.field))
        daily = str(r.interval) in _DATED  # a day's value has a date, not a time
        when = f"{as_of:%d %b %Y}" if daily else f"{as_of:%d %b %H:%M}"
        p = Provenance(
            str(r.source), int(r.tier), when, field,
            f"as of {when}{'' if daily else ' ET'}; fetched {to_et(fetched):%d %b %H:%M} ET",
        )  # fmt: skip
        text = _value_text(float(r.value), str(r.unit), str(r.field))
        flags = " · fallback" if r.is_fallback else ""
        flags += " · derived" if r.is_derived else ""
        rows.append(
            f"<tr><td>{escape(field)}<span class='db-tagline'>"
            f"{escape(_INTERVAL_TEXT.get(str(r.interval), str(r.interval)))}{flags}</span></td>"
            f"<td class='num'>{value_cell(text, p, compact=True)}</td>"
            f"<td>{escape(str(r.source))}<span class='db-tagline'>{escape(str(r.dataset))}"
            f" · {escape(str(r.symbol))}</span></td>"
            f"<td class='num'>{when}<span class='db-tagline'>"
            f"fetched {_age((now - fetched).total_seconds())}</span></td></tr>"
        )
    head = (
        "<thead><tr><th>Field</th><th class='num'>Value</th><th>Source</th>"
        "<th class='num'>As of (ET)</th></tr></thead>"
    )
    return f'<table class="db-table db-values">{head}<tbody>{"".join(rows)}</tbody></table>', len(
        df
    )


# ---------------------------------------------------------------------------
# source agreement and corrections
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60, show_spinner=False)
def _xchecks(_config: Config, where: str, iid: str, today: date) -> pd.DataFrame:
    with data.shared_connection(_config) as conn:
        return conn.execute(
            "SELECT check_name, target_date, status, diff_bps, warn_bps, fail_bps, "
            "left_instrument, left_source, right_instrument, right_source, details_json "
            "FROM xchecks WHERE (left_instrument = ? OR right_instrument = ?) "
            "AND target_date > ? ORDER BY target_date, at_time",
            [iid, iid, today - timedelta(days=XCHECK_DAYS)],
        ).fetchdf()


_STATUS_MARK = {"pass": "✓", "warn": "!", "fail": "✕", "skip": "–"}


def agreement_html(config: Config, iid: str, hidden: frozenset[int], today: date) -> str:
    """Each cross-check naming the instrument: the last 14 days as marks, the latest gap."""
    df = _xchecks(config, _where(config), iid, today)
    exposed = exposed_sources(config, hidden)
    if not df.empty and exposed:
        df = df[~(df["left_source"].isin(exposed) | df["right_source"].isin(exposed))]
    df = df[df["status"] != "skip"] if not df.empty else df
    if df.empty:
        return f'<div class="db-empty">No cross-checks in the last {XCHECK_DAYS} days.</div>'
    lines = []
    for name, g in df.groupby("check_name", sort=False):
        marks = "".join(
            f'<span class="db-st-{escape(str(s))}" title="{r.target_date:%d %b}: {escape(str(s))}">'
            f"{_STATUS_MARK.get(str(s), '?')}</span>"
            for r, s in zip(g.itertuples(), g["status"], strict=True)
        )
        last = g.iloc[-1]
        gap = (
            f"{last['diff_bps']:+.1f} bp (warn {last['warn_bps']:g})"
            if pd.notna(last["diff_bps"])
            else "no gap"
        )
        pair = f"{last['left_source']} vs {last['right_source']}"
        lines.append(
            f"<div><span>{escape(str(name).replace('_', ' '))}"
            f'<span class="db-note"> · {escape(pair)}</span></span>'
            f'<span class="db-marks">{marks}</span></div>'
            f'<div class="db-note db-gapline">latest gap {escape(gap)}</div>'
        )
    return f'<div class="db-checks">{"".join(lines)}</div>'


@st.cache_data(ttl=60, show_spinner=False)
def _revisions(
    _config: Config, where: str, iid: str, hidden: frozenset[int], _now: datetime
) -> pd.DataFrame:
    with data.shared_connection(_config) as conn:
        return conn.execute(
            "SELECT source, field, interval, as_of, count(*) AS versions, "  # noqa: S608 (tiers are ints)
            "arg_min(value, ingested_at) AS first_value, arg_max(value, ingested_at) AS "
            "last_value, max(ingested_at) AS revised_at, max(unit) AS unit, max(tier) AS tier "
            "FROM obs_all "
            f"WHERE instrument_id = ? AND tier IN ({_tiers_sql(hidden)}) AND as_of > ? "
            "AND ingested_at <= ? GROUP BY source, dataset, field, interval, as_of "
            "HAVING count(DISTINCT value) > 1 ORDER BY revised_at DESC LIMIT 20",
            [iid, _now - timedelta(days=REVISION_DAYS), _now],
        ).fetchdf()


def revisions_html(config: Config, iid: str, hidden: frozenset[int], now: datetime) -> str:
    df = _revisions(config, _where(config), iid, hidden, now)
    if df.empty:
        return f'<div class="db-empty">No corrections in the last {REVISION_DAYS} days.</div>'
    lines = []
    for r in df.itertuples():
        as_of = to_et(_utc(r.as_of))
        at = to_et(_utc(r.revised_at))
        unit, field = str(r.unit), str(r.field)
        p_old = Provenance(str(r.source), int(r.tier), f"{as_of:%d %b}", f"first reported {field}")
        p_new = Provenance(
            str(r.source), int(r.tier), f"{as_of:%d %b}", f"{field} as revised {at:%d %b %H:%M}"
        )
        old = value_cell(_value_text(float(r.first_value), unit, field), p_old, compact=True)
        new = value_cell(_value_text(float(r.last_value), unit, field), p_new, compact=True)
        lines.append(
            f"<div><span>{escape(field)} of {as_of:%d %b}"
            f'<span class="db-note"> · {escape(str(r.source))} · {r.versions} versions'
            f"</span></span><span>{old} → {new}</span></div>"
        )
    return f'<div class="db-checks">{"".join(lines)}</div>'


def tier_names(bars: pd.DataFrame) -> str:
    return " ".join(
        f"{TIER_GLYPH[int(t)]} {TIER_NAME[int(t)]}" for t in sorted(bars["tier"].unique())
    )
