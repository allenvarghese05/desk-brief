"""Instrument tearsheet (T36 phase 1, T58 v2): what an analyst needs about one instrument.

* performance over horizons (1D from the report; 1W, 1M, 3M, YTD, 1Y from daily closes),
  a weekly series (``1w``) shows its week-on-week change first and says that daily-move
  statistics need daily values; index levels and positions change in points, never in %,
* risk and range (realised volatility, today's move in standard deviations, drawdown, 52-week
  range), each saying how many days it needs when the store holds fewer,
* every value we hold for the instrument: one line per source, dataset, field and interval,
  with when it was true (as of) and when we fetched it,
* source agreement over time (cross-checks naming the instrument) and corrections (revisions).

Statistics respect data gaps (T58): a horizon refuses a base close older than the horizon
allows and says "no data"; realised volatility, sigma and drawdown skip every move across a
gap of more than :data:`GAP_BDAYS` business days and say so; a date span names the year when it
crosses one.

Every figure is printed through ``values.value_cell`` with a :class:`Provenance` naming its
source, tier, date and what it is. Derived statistics name the closes they were computed from.
Hosted mode never reads a hidden tier (SQL excludes them; cross-checks naming a source with a
hidden-tier feed are withheld whole). Caches are keyed on ``data.data_version()`` (T58). No
markup here carries a ``title`` attribute (P-022).
"""
# ruff: noqa: RUF001  (minus, sigma and en dash are intentional output)

from __future__ import annotations

import itertools
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from typing import Any

import numpy as np
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
#: a move across more than this many business days without a close is a gap: volatility, sigma
#: and drawdown skip it (T58)
GAP_BDAYS = 5
#: how much older than a horizon's start its base close may be (calendar days): a long weekend
#: or a holiday is fine, a hole in the data is not (T58)
BASE_SLACK_DAYS = 5
BASE_SLACK_DAYS_WEEKLY = 10
DRAWDOWN_DAYS = 365


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
    span = date_span(used["d"].iloc[0], used["d"].iloc[-1])
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
    t = pd.Timestamp(ts)  # type: ignore[arg-type]
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.to_pydatetime()


def _records(df: pd.DataFrame) -> Iterator[Any]:
    """Rows of a query result as named tuples (typed loosely: DuckDB decides the types)."""
    return iter(df.itertuples())


def _need(n_need: int, n_have: int) -> str:
    """Too little history, said on screen and in full for screen readers (no ``title``)."""
    return (
        f'<span class="db-unavailable">needs {n_need}d<span class="db-sr"> (needs {n_need} '
        f"days of history, the store holds {n_have})</span></span>"
    )


def date_span(a: object, b: object) -> str:
    """Two dates joined by an en dash; both carry the year when the span crosses one (T58)."""
    x, y = pd.Timestamp(a), pd.Timestamp(b)  # type: ignore[arg-type]
    if x.date() == y.date():
        return f"{y:%d %b}"
    if x.year != y.year:
        return f"{x:%d %b %Y} – {y:%d %b %Y}"
    return f"{x:%d %b} – {y:%d %b}"


@dataclass(frozen=True)
class Gap:
    """A hole in the daily history: the last close before it and the first after it."""

    before: date
    after: date
    bdays: int  # business days between the two closes, the second one included

    @property
    def text(self) -> str:
        return f"{date_span(self.before, self.after)} ({self.bdays} business days)"


def gaps(bars: pd.DataFrame) -> list[Gap]:
    """Every step between consecutive closes longer than :data:`GAP_BDAYS` business days.

    A weekly series steps a week at a time by design, so it has no gaps by this rule."""
    if len(bars) < 2 or _is_weekly(bars):
        return []
    days = [d.date() for d in pd.to_datetime(bars["d"])]
    out = []
    for a, b in itertools.pairwise(days):
        n = int(np.busday_count(a, b))
        if n > GAP_BDAYS:
            out.append(Gap(a, b, n))
    return out


def _gap_note(skipped: list[Gap]) -> str:
    if not skipped:
        return ""
    noun = "gap" if len(skipped) == 1 else "gaps"
    where = "; ".join(g.text for g in skipped)
    return f"skips the {noun} in the data longer than {GAP_BDAYS} business days: {where}"


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
    slack = BASE_SLACK_DAYS_WEEKLY if _is_weekly(bars) else BASE_SLACK_DAYS
    for label, days in HORIZONS:
        start = date(day.year - 1, 12, 31) if days is None else day - timedelta(days=days)
        before = bars[bars["d"].dt.date <= start]
        if first > start or before.empty:
            need = (day - start).days
            out.append(Horizon(label, _need(need, (day - first).days)))
            continue
        base = before.iloc[-1]
        base_day = base["d"].date()
        if (start - base_day).days > slack:  # a hole in the data: never a longer change
            out.append(Horizon(label, _no_base(label, base_day, start)))
            continue
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


def _no_base(label: str, base: date, start: date) -> str:
    """The closest earlier close is too old for this horizon (a gap in the data)."""
    return (
        '<span class="db-unavailable">no data<span class="db-sr"> (no close near '
        f"{start:%d %b %Y}: the closest earlier one, {base:%d %b %Y}, is older than the "
        f"{escape(label)} horizon allows)</span></span>"
    )


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
    """Daily moves (bp for yields, points for levels, log returns in % otherwise), indexed by
    the later bar's position. A move across a gap (see :func:`gaps`) is left out (T58)."""
    closes = bars["close"].astype(float).reset_index(drop=True)
    if _is_yield(bars):
        moves = closes.diff() * 100  # bp
    elif _mode(bars) != "%":
        moves = closes.diff()  # points: a level may be zero or negative
    else:
        moves = closes.apply(math.log).diff() * 100  # log return in %
    days = pd.to_datetime(bars["d"]).dt.date.reset_index(drop=True)
    if not _is_weekly(bars):
        for i in range(1, len(days)):
            if int(np.busday_count(days[i - 1], days[i])) > GAP_BDAYS:
                moves.iloc[i] = float("nan")
    return moves.dropna()


def _skipped(bars: pd.DataFrame, since: object) -> list[Gap]:
    """The gaps that fall inside the window a statistic used."""
    cut = pd.Timestamp(since).date()  # type: ignore[arg-type]
    return [g for g in gaps(bars) if g.after >= cut]


def _segments(bars: pd.DataFrame) -> list[pd.DataFrame]:
    """The bars cut at every gap, so a drawdown never runs across one."""
    cuts = {g.after for g in gaps(bars)}
    out, start = [], 0
    days = [d.date() for d in pd.to_datetime(bars["d"])]
    for i, d in enumerate(days):
        if d in cuts and i > start:
            out.append(bars.iloc[start:i])
            start = i
    out.append(bars.iloc[start:])
    return [x for x in out if not x.empty]


def _num(v: float, unit: str) -> str:
    if unit == "pct":
        return f"{v:.2f}%"
    return f"{v:,.2f}" if abs(v) >= 10 else f"{v:,.4f}"


def _fact(label: str, value: str) -> str:
    return f'<div class="db-fact"><span>{escape(label)}</span><b>{value}</b></div>'


def risk_html(row: Row, bars: pd.DataFrame) -> str:
    """Realised vol, today's move in sigmas, max drawdown and the 52-week range, as label and
    value rows and a labelled range bar. Moves across a gap are skipped and the card says so."""
    if bars.empty:
        return '<div class="db-empty">No daily history yet.</div>'
    yields = _is_yield(bars)
    mode = _mode(bars)
    moves = _moves(bars)
    n = len(moves)
    facts: list[tuple[str, str]] = []
    skipped: list[Gap] = []
    weekly = _is_weekly(bars)
    # 20 weekly moves are not "20-day" volatility, and a % volatility of a level that may cross
    # zero means nothing: both say so instead of printing a mislabelled number
    skip = (
        "weekly series: needs daily values" if weekly
        else None if mode in ("%", "bp") else "not computed for a level series"
    )  # fmt: skip
    vol_label = f"Realised vol, {VOL_DAYS} days"
    if skip:
        note = f'<span class="db-unavailable">{skip}</span>'
        facts += [(vol_label, note), ("Latest move", note)]
    elif n >= VOL_DAYS:
        used = moves.tail(VOL_DAYS)
        vol = float(used.std()) * math.sqrt(252)
        text = f"{vol:,.0f} bp" if yields else f"{vol:.1f}%"
        window = bars.iloc[max(0, int(used.index[0]) - 1) :]
        gone = _skipped(bars, window["d"].iloc[0])
        skipped += gone
        what = f"{VOL_DAYS}-day realised volatility, annualised"
        p = _bar_prov(window, what + (f"; {_gap_note(gone)}" if gone else ""))
        facts.append((vol_label, value_cell(text, p, compact=True, show_as_of=False)))
    else:
        facts.append((vol_label, _need(VOL_DAYS + 1, n + 1)))
    latest = _latest(row, bars)
    if skip is None and latest is not None and n >= SIGMA_MIN_DAYS:
        value, day = latest
        last_close = float(bars["close"].iloc[-1])
        last_day = bars["d"].iloc[-1].date()
        intraday = day > last_day
        across = (
            int(np.busday_count(last_day, day)) > GAP_BDAYS
            if intraday
            else (len(bars) - 1) not in moves.index  # the last move was across a gap
        )
        today = _move(value, last_close, mode)[0] if intraday else float(moves.iloc[-1])
        hist = moves if intraday else moves.iloc[:-1]
        sd = float(hist.tail(60).std())
        if across:
            facts.append(
                ("Latest move", '<span class="db-unavailable">no data<span class="db-sr"> '
                 "(the latest move spans a gap in the data)</span></span>")
            )  # fmt: skip
        elif sd > 0:
            window = bars.iloc[max(0, int(hist.tail(60).index[0]) - 1) :]
            gone = _skipped(bars, window["d"].iloc[0])
            skipped += gone
            what = f"latest move in standard deviations of {len(hist.tail(60))} daily moves"
            p = _bar_prov(
                window,
                what + (f"; {_gap_note(gone)}" if gone else ""),
                latest=row if intraday else None,
            )
            text = f"{today / sd:+.1f}σ".replace("-", _MINUS)
            facts.append(("Latest move", value_cell(text, p, compact=True, show_as_of=False)))
    elif skip is None:
        facts.append(("Latest move", _need(SIGMA_MIN_DAYS + 1, n + 1)))
    if mode == "%" and len(bars) >= 2:
        recent = bars[bars["d"] >= bars["d"].iloc[-1] - pd.Timedelta(days=DRAWDOWN_DAYS)]
        dd = 0.0
        for seg in _segments(recent):
            closes = seg["close"].astype(float)
            dd = min(dd, float((closes / closes.cummax() - 1).min()) * 100)
        gone = _skipped(recent, recent["d"].iloc[0])
        skipped += gone
        what = "largest fall from a high, daily closes"
        p = _bar_prov(recent, what + (f"; {_gap_note(gone)}" if gone else ""))
        span = (recent["d"].iloc[-1] - recent["d"].iloc[0]).days
        label = "Max drawdown, 1Y" if span >= 360 else f"Max drawdown, {len(recent)} days"
        facts.append((label, value_cell(_signed(dd, "%"), p, compact=True, show_as_of=False)))
    body = "".join(_fact(k, v) for k, v in facts)
    note = ""
    if skipped:
        uniq = list(dict.fromkeys(skipped))
        note = f'<div class="db-note db-gapnote">Vol, sigma and drawdown {_gap_note(uniq)}.</div>'
    return f'<div class="db-facts">{body}</div>{note}{range_html(row, bars)}'


def range_html(row: Row, bars: pd.DataFrame) -> str:
    """The 52-week range as a labelled bar: label and latest value, the bar with a marker at
    the latest value, and the low and high at its ends, each with provenance."""
    unit = str(bars["unit"].iloc[-1])
    weekly = _is_weekly(bars)
    year = bars[bars["d"] >= bars["d"].iloc[-1] - pd.Timedelta(days=365)]
    hi, lo = float(year["close"].max()), float(year["close"].min())
    days = (year["d"].iloc[-1] - year["d"].iloc[0]).days
    span, step = ("weekly", "weeks") if weekly else ("daily", "days")
    label = "52-week range" if days >= 360 else f"Range, {len(year)} {step}"
    field = year["field"].iloc[-1]
    lo_row = year.loc[year["close"].astype(float).idxmin()]
    hi_row = year.loc[year["close"].astype(float).idxmax()]
    p_lo = _bar_prov(year[year["d"] == lo_row["d"]], f"lowest {span} {field} of the range")
    p_hi = _bar_prov(year[year["d"] == hi_row["d"]], f"highest {span} {field} of the range")
    latest = _latest(row, bars)
    head = f'<span class="db-range-label">{escape(label)}</span>'
    if latest is not None:
        if row.change is not None:
            last = row.change.last
            p_now = Provenance(
                last.source, last.tier, f"{to_et(last.as_of):%d %b %H:%M}", "latest value"
            )
        else:
            p_now = _bar_prov(bars.tail(1), f"latest {span} {field}")
        head += f"<b>{value_cell(_num(latest[0], unit), p_now, compact=True, show_as_of=False)}</b>"
    bar = ""
    if latest is not None and hi > lo:
        pos = min(1.0, max(0.0, (latest[0] - lo) / (hi - lo)))
        bar = (
            f'<div class="db-range" role="img" aria-label="{escape(label)}: latest value at '
            f'{pos:.0%} of the way from the low to the high">'
            f'<span class="db-range-fill" style="width:{pos * 100:.1f}%"></span>'
            f'<span class="db-range-mk" style="left:calc({pos * 100:.1f}% - 1px)"></span></div>'
        )
    ends = (
        f'<div class="db-range-ends"><span>{value_cell(_num(lo, unit), p_lo, compact=True)}'
        f"</span><span>{value_cell(_num(hi, unit), p_hi, compact=True)}</span></div>"
    )
    return f'<div class="db-rangebox"><div class="db-range-head">{head}</div>{bar}{ends}</div>'


# ---------------------------------------------------------------------------
# every value we hold
# ---------------------------------------------------------------------------


def _where(config: Config) -> str:
    s = config.storage
    return s.bucket if s.backend == "r2" else s.local_root


def _tiers_sql(hidden: frozenset[int]) -> str:
    return ", ".join(str(int(t)) for t in (1, 2, 3) if t not in hidden) or "0"


@st.cache_data(ttl=data.DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _all_values(
    _config: Config, where: str, iid: str, hidden: frozenset[int], ver: str, _now: datetime
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
    df = _all_values(config, _where(config), iid, hidden, data.data_version(config), now)
    if df.empty:
        return '<div class="db-empty">No values stored for this instrument.</div>', 0
    rows = []
    for r in _records(df):
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


@st.cache_data(ttl=data.DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _xchecks(_config: Config, where: str, iid: str, today: date, ver: str) -> pd.DataFrame:
    with data.shared_connection(_config) as conn:
        return conn.execute(
            "SELECT check_name, target_date, status, diff_bps, warn_bps, fail_bps, "
            "left_instrument, left_source, right_instrument, right_source, details_json "
            "FROM xchecks WHERE (left_instrument = ? OR right_instrument = ?) "
            "AND target_date > ? ORDER BY target_date, at_time",
            [iid, iid, today - timedelta(days=XCHECK_DAYS)],
        ).fetchdf()


_STATUS_MARK = {"pass": "✓", "warn": "!", "fail": "✕", "skip": "–"}


def _visible_xchecks(config: Config, iid: str, hidden: frozenset[int], today: date) -> pd.DataFrame:
    """Cross-checks naming the instrument, without skips and without any naming a source that
    has a hidden-tier feed (hosted mode withholds those whole)."""
    df = _xchecks(config, _where(config), iid, today, data.data_version(config))
    exposed = exposed_sources(config, hidden)
    if not df.empty and exposed:
        df = df[~(df["left_source"].isin(exposed) | df["right_source"].isin(exposed))]
    return df[df["status"] != "skip"] if not df.empty else df


def xcheck_summary(config: Config, iid: str, hidden: frozenset[int], today: date) -> str:
    """One sentence for the verdict card: how the last 14 days of cross-checks went."""
    df = _visible_xchecks(config, iid, hidden, today)
    if df.empty:
        return f"No cross-checks in the last {XCHECK_DAYS} days."
    n, ok = len(df), int((df["status"] == "pass").sum())
    pairs = ", ".join(
        dict.fromkeys(f"{r.left_source} vs {r.right_source}" for r in df.itertuples())
    )
    fails = int((df["status"] == "fail").sum())
    tail = f"{ok} of {n} passed" + (f", {fails} failed" if fails else "")
    return f"Cross-checks, last {XCHECK_DAYS} days ({pairs}): {tail}."


def agreement_html(config: Config, iid: str, hidden: frozenset[int], today: date) -> str:
    """Each cross-check naming the instrument: the last 14 days as marks, the latest gap."""
    df = _visible_xchecks(config, iid, hidden, today)
    if df.empty:
        return f'<div class="db-empty">No cross-checks in the last {XCHECK_DAYS} days.</div>'
    lines = []
    for name, g in df.groupby("check_name", sort=False):
        marks = "".join(
            f'<span class="db-st-{escape(str(s))}" aria-hidden="true">'
            f"{_STATUS_MARK.get(str(s), '?')}</span>"
            f'<span class="db-sr">{r.target_date:%d %b %Y}: {escape(str(s))}; </span>'
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


@st.cache_data(ttl=data.DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _revisions(
    _config: Config, where: str, iid: str, hidden: frozenset[int], ver: str, _now: datetime
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
    df = _revisions(config, _where(config), iid, hidden, data.data_version(config), now)
    if df.empty:
        return f'<div class="db-empty">No corrections in the last {REVISION_DAYS} days.</div>'
    lines = []
    for r in _records(df):
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
