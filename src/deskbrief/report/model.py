"""BriefModel: the pure, serialisable content of the morning report (spec §9).

``BriefModel.from_store(duck, config, report_date, report_time)`` reads through
``deskbrief.query`` (point-in-time at ``report_time``) and returns a frozen tree of dataclasses:
headline, sections by asset class, notable items, data health, known limits and lineage. It is
serialised to ``model.json`` so every renderer (HTML, Excel, dashboard) stays dumb: all text a
reader sees (labels, signed changes, tier glyphs, "data unavailable") is decided here.

Labelling rules (P-009): a value is labelled by its ``field``/``unit``. "since previous NYSE
close" is used only for price-family ``close`` instruments; yields, ECB reference rates and macro
series say "since previous official value". Nothing outside ``field = close`` is called a close.

Empty or partial stores are normal (checks and sources land at different times): missing data
becomes a "data unavailable" row plus a known limit, never an exception. The model is
deterministic: no wall-clock reads, stable ordering, ``to_json`` output is byte-stable.
"""
# ruff: noqa: RUF001, RUF002, RUF003  (true minus sign and sigma are intentional output)

from __future__ import annotations

import json
import statistics
import types
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from itertools import pairwise
from typing import Any, Self, Union, get_args, get_origin, get_type_hints

import duckdb

from deskbrief.config import Config, Instrument, config_hash
from deskbrief.query import (
    ChangeRow,
    CheckItem,
    HealthSummary,
    Lineage,
    ValueRow,
    field_label,
    health_summary,
    lineages,
    measure,
    overnight_changes,
    series_all,
    value_label,
)
from deskbrief.timeutil import ensure_utc, to_et

SCHEMA_VERSION = 1

#: section order and titles by asset class (spec §9.1; vol and macro are kept separate)
SECTIONS: tuple[tuple[str, str], ...] = (
    ("rates", "Rates"),
    ("fx", "FX"),
    ("commodity", "Commodities"),
    ("equity", "Equities"),
    ("index", "Global"),
    ("vol", "Volatility"),
    ("macro", "Macro"),
)
TIER_GLYPH = {1: "●", 2: "◐", 3: "○"}
TIER_NAME = {1: "live", 2: "delayed", 3: "official"}
#: short names for the one-line headline; any other instrument falls back to its id
SHORT_LABELS = {"ES_F": "S&P fut", "UST_10Y": "10Y", "UST_2Y": "2Y"}
UNAVAILABLE = "data unavailable"
MIN_MOVES_FOR_Z = 10

_MINUS = "−"
_UNIT_SUFFIX = {"pct": "%", "bp": "bp", "pts": " pts", "pp": "pp"}
_UNIT_DECIMALS = {"pct": 1, "bp": 0, "pts": 1, "pp": 2}


# ---------------------------------------------------------------------------
# model types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One instrument line: ready-to-print strings plus the typed change behind them."""

    instrument_id: str
    name: str
    label: str  # what the value is, with "proxy" wording when applicable
    asset_class: str
    is_proxy: bool
    proxy_label: str | None
    available: bool
    value_text: str
    change_text: str
    description: str  # e.g. "US 10-year Treasury yield, −3bp since previous official value"
    basis: str | None
    as_of_text: str
    tier: int | None
    tier_glyph: str
    tier_name: str
    change: ChangeRow | None


@dataclass(frozen=True)
class Section:
    asset_class: str
    title: str
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class HeadlineItem:
    instrument_id: str
    label: str
    value_text: str
    change_text: str | None
    tier: int | None
    available: bool


@dataclass(frozen=True)
class Headline:
    items: tuple[HeadlineItem, ...]
    text: str


@dataclass(frozen=True)
class Notable:
    kind: str  # move | crosscheck
    text: str
    instrument_id: str | None


@dataclass(frozen=True)
class HealthBlock:
    status: str  # ok | warn | fail | unknown
    text: str
    summary: HealthSummary


@dataclass(frozen=True)
class KnownLimit:
    kind: str  # proxy, iex, tiers, fallback, unavailable, dq_missing, dq_*, crosscheck_*, source
    text: str


@dataclass(frozen=True)
class BriefModel:
    schema_version: int
    report_date: date
    report_time: datetime
    title: str
    config_hash: str
    headline: Headline
    sections: tuple[Section, ...]
    notable: tuple[Notable, ...]
    health: HealthBlock
    known_limits: tuple[KnownLimit, ...]
    lineage: tuple[Lineage, ...]

    @classmethod
    def from_store(
        cls,
        duck: duckdb.DuckDBPyConnection,
        config: Config,
        report_date: date,
        report_time: datetime,
    ) -> Self:
        """Build the brief for ET ``report_date`` as known at ``report_time`` (aware UTC)."""
        report_time = ensure_utc(report_time)
        changes = {
            c.last.instrument_id: c for c in overnight_changes(duck, report_date, report_time)
        }
        snapshot = tuple(c.last for c in changes.values())  # ordered by instrument_id
        enabled = [s for s, st in config.sources.items() if st.enabled]
        summary = health_summary(duck, report_date, expected_sources=enabled)

        rows = {i.id: _row(i, changes.get(i.id)) for i in config.instruments}
        sections = tuple(
            Section(ac, title, tuple(rows[i.id] for i in config.instruments if i.asset_class == ac))
            for ac, title in SECTIONS
            if any(i.asset_class == ac for i in config.instruments)
        )
        headline = _headline(config, rows)
        health = _health(summary)
        return cls(
            schema_version=SCHEMA_VERSION,
            report_date=report_date,
            report_time=report_time,
            title=_title(report_time),
            config_hash=config_hash(config),
            headline=headline,
            sections=sections,
            notable=_notable(duck, config, list(changes.values()), summary, report_time),
            health=health,
            known_limits=_known_limits(config, rows, snapshot, summary),
            lineage=lineages(duck, snapshot),
        )

    # -- serialisation ------------------------------------------------------
    def to_json(self) -> str:
        """Deterministic JSON (stable key order, UTC ISO timestamps, UTF-8 characters kept)."""
        return json.dumps(_encode(self), indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Self:
        decoded = _decode(cls, json.loads(text))
        assert isinstance(decoded, cls)
        return decoded

    def rows(self) -> tuple[Row, ...]:
        """Every row of every section, in section order."""
        return tuple(r for s in self.sections for r in s.rows)


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------


def signed(x: float, decimals: int) -> str:
    """``+0.4`` / ``−0.4`` (true minus sign); values that round to zero print unsigned."""
    text = f"{x:+.{decimals}f}"
    if float(text) == 0:
        return f"{0:.{decimals}f}"
    return text.replace("-", _MINUS)


def change_text(change: ChangeRow) -> str:
    if change.change is None:
        return "n/a"
    return (
        signed(change.change, _UNIT_DECIMALS[change.change_unit]) + _UNIT_SUFFIX[change.change_unit]
    )


def value_text(row: ValueRow) -> str:
    v = row.value
    if row.unit == "pct":
        return f"{v:.2f}%"
    if row.unit == "USD":
        return f"${v:,.2f}"
    if row.unit == "fx_rate":
        return f"{v:.2f}" if abs(v) >= 20 else f"{v:.4f}"
    return f"{v:,.2f}"


def _as_of_text(row: ValueRow) -> str:
    et = to_et(row.as_of)
    if row.interval in ("1d", "1mo"):
        return f"{et.day} {et:%b}"
    return f"{et:%H:%M}"


def _title(report_time: datetime) -> str:
    et = to_et(report_time)
    return f"Desk Brief — {et:%a} {et.day} {et:%b %Y}, {et:%H:%M} ET"


def _short(instrument_id: str) -> str:
    return SHORT_LABELS.get(instrument_id, instrument_id)


# ---------------------------------------------------------------------------
# rows, headline, health
# ---------------------------------------------------------------------------


def _row(inst: Instrument, change: ChangeRow | None) -> Row:
    if change is None:
        return Row(
            instrument_id=inst.id,
            name=inst.name,
            label=f"{inst.name} — {inst.label}" if inst.is_proxy else inst.name,
            asset_class=inst.asset_class,
            is_proxy=inst.is_proxy,
            proxy_label=inst.label if inst.is_proxy else None,
            available=False,
            value_text=UNAVAILABLE,
            change_text="n/a",
            description=f"{inst.name}: {UNAVAILABLE}",
            basis=None,
            as_of_text="n/a",
            tier=None,
            tier_glyph="",
            tier_name="",
            change=None,
        )
    last = change.last
    label = value_label(last)
    if last.is_proxy and last.proxy_label:
        label = f"{label} — {last.proxy_label}"
    if change.status == "ok":
        tail = f"{change_text(change)} since {change.basis}"
    elif change.status == "no_move_yet":
        tail = f"no move to report since {change.basis}"
    else:
        tail = f"change unavailable ({change.basis} not found)"
    return Row(
        instrument_id=last.instrument_id,
        name=last.name,
        label=label,
        asset_class=last.asset_class,
        is_proxy=last.is_proxy,
        proxy_label=last.proxy_label if last.is_proxy else None,
        available=True,
        value_text=value_text(last),
        change_text=change_text(change),
        description=f"{label}, {tail}",
        basis=change.basis,
        as_of_text=_as_of_text(last),
        tier=last.tier,
        tier_glyph=TIER_GLYPH.get(last.tier, ""),
        tier_name=TIER_NAME.get(last.tier, ""),
        change=change,
    )


def _headline(config: Config, rows: dict[str, Row]) -> Headline:
    items: list[HeadlineItem] = []
    parts: list[str] = []
    for iid in config.report.headline_instruments:
        row = rows[iid]
        label = _short(iid)
        has_change = row.available and row.change is not None and row.change.change is not None
        items.append(
            HeadlineItem(
                iid,
                label,
                row.value_text,
                row.change_text if has_change else None,
                row.tier,
                row.available,
            )
        )
        if not row.available:
            parts.append(f"{label} n/a")
        elif has_change:
            parts.append(f"{label} {row.value_text} ({row.change_text})")
        else:
            parts.append(f"{label} {row.value_text}")
    return Headline(tuple(items), "Overnight: " + " · ".join(parts))


def _health(s: HealthSummary) -> HealthBlock:
    total = s.n_checks - s.n_skip
    if total == 0:
        return HealthBlock("unknown", f"No data-quality results recorded for {s.target_date}", s)
    passed = s.n_pass + s.n_xcheck_pass
    fails = s.n_fail + s.n_xcheck_fail
    warns = s.n_warn + s.n_xcheck_warn
    status = "fail" if fails else "warn" if warns else "ok"
    text = f"{passed}/{total} checks pass"
    if fails or warns:
        text += f" ({fails} fail, {warns} warn)"
    return HealthBlock(status, text, s)


# ---------------------------------------------------------------------------
# notable items
# ---------------------------------------------------------------------------


def _notable(
    duck: duckdb.DuckDBPyConnection,
    config: Config,
    changes: Sequence[ChangeRow],
    summary: HealthSummary,
    at: datetime,
) -> tuple[Notable, ...]:
    window = config.checks.anomaly.window_days
    hist = series_all(duck, window * 2, at)
    moves: list[tuple[float, Notable]] = []
    for ch in changes:
        if ch.change is None:
            continue
        past = hist.get(ch.last.instrument_id, ())
        diffs = [measure(ch.last, a.value, b.value)[0] for a, b in pairwise(past)]
        diffs = diffs[-window:]
        if len(diffs) < MIN_MOVES_FOR_Z or (sd := statistics.stdev(diffs)) == 0:
            continue
        z = (ch.change - statistics.fmean(diffs)) / sd
        if abs(z) >= config.checks.anomaly.z_warn:
            text = (
                f"{_short(ch.last.instrument_id)} {change_text(ch)} is {abs(z):.1f}σ "
                f"vs prior {len(diffs)} daily moves"
            )
            moves.append((abs(z), Notable("move", text, ch.last.instrument_id)))
    ordered = [n for _, n in sorted(moves, key=lambda m: (-m[0], m[1].instrument_id or ""))]
    crosschecks = [
        Notable("crosscheck", _crosscheck_text(i), i.instrument_id)
        for i in summary.issues
        if i.kind == "crosscheck"
    ]
    return tuple(ordered + crosschecks)


def _crosscheck_text(i: CheckItem) -> str:
    where = f" ({i.group_id})" if i.group_id else ""
    gap = f"gap {i.observed:.1f}bp" if i.observed is not None else "no gap recorded"
    limit = f", limit {i.threshold:g}bp" if i.threshold is not None else ""
    return f"Cross-check {i.name}{where} {i.status.upper()}: {gap}{limit}"


# ---------------------------------------------------------------------------
# known limits
# ---------------------------------------------------------------------------


def _known_limits(
    config: Config, rows: dict[str, Row], snapshot: Sequence[ValueRow], s: HealthSummary
) -> tuple[KnownLimit, ...]:
    out: list[KnownLimit] = []
    proxies = [i.id for i in config.instruments if i.is_proxy]
    if proxies:
        out.append(KnownLimit("proxy", "Proxies, not the underlying: " + ", ".join(proxies)))
    if any(f.dataset.startswith("iex") for i in config.instruments for f in i.feeds):
        out.append(KnownLimit("iex", "IEX covers a single exchange, not the consolidated tape"))
    tiers = {r.tier for r in snapshot}
    if tiers & {2, 3}:
        out.append(
            KnownLimit(
                "tiers",
                "◐ delayed values are exact but not live; ○ official values are as of "
                "their publication date (shown), not today",
            )
        )
    fallbacks = sorted({f"{r.instrument_id} ({r.source})" for r in snapshot if r.is_fallback})
    if fallbacks:
        out.append(KnownLimit("fallback", "Fallback source standing in: " + ", ".join(fallbacks)))
    missing = [r.instrument_id for r in rows.values() if not r.available]
    if missing:
        out.append(KnownLimit("unavailable", f"{UNAVAILABLE.capitalize()}: " + ", ".join(missing)))
    if s.n_checks - s.n_skip == 0:
        out.append(
            KnownLimit("dq_missing", f"No data-quality results recorded for {s.target_date}")
        )
    for i in s.issues:
        where = i.instrument_id or i.source or i.group_id or "all"
        out.append(
            KnownLimit(
                f"{'dq' if i.kind == 'dq' else 'crosscheck'}_{i.status}",
                f"{i.kind} {i.status.upper()}: {i.name} ({where})",
            )
        )
    down = [h.source for h in s.sources if h.status in ("missing", "failed")]
    if down:
        out.append(KnownLimit("source", "No successful fetch today from: " + ", ".join(down)))
    return tuple(out)


# ---------------------------------------------------------------------------
# JSON codec (dataclass tree <-> plain data)
# ---------------------------------------------------------------------------


def _encode(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _encode(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, tuple):
        return [_encode(x) for x in obj]
    if isinstance(obj, datetime):
        return ensure_utc(obj).isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def _decode(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        return _decode(args[0], value)
    if origin is tuple:
        return tuple(_decode(get_args(tp)[0], v) for v in value)
    if isinstance(tp, type) and is_dataclass(tp):
        hints = get_type_hints(tp)
        return tp(**{f.name: _decode(hints[f.name], value[f.name]) for f in fields(tp)})
    if tp is datetime:
        return datetime.fromisoformat(value)
    if tp is date:
        return date.fromisoformat(value)
    return value


__all__ = [
    "BriefModel",
    "Headline",
    "HeadlineItem",
    "HealthBlock",
    "KnownLimit",
    "Notable",
    "Row",
    "Section",
    "field_label",
]
