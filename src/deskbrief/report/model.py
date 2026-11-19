"""BriefModel: the pure, serialisable content of the morning report (spec §9).

``BriefModel.from_store(duck, config, report_date, report_time)`` reads through
``deskbrief.query`` (point-in-time at ``report_time``) and returns a frozen tree of dataclasses:
headline, sections by asset class, notable items, data health, known limits and lineage. It is
serialised to ``model.json`` so every renderer (HTML, Excel, dashboard) stays dumb: all text a
reader sees (labels, signed changes, tier glyphs, "data unavailable") is decided here.

Labelling rules (P-009): a value is labelled by its ``field``/``unit``. "since previous NYSE
close" is used only for price-family ``close`` instruments; yields, ECB reference rates and macro
series say "since previous official value". Nothing outside ``field = close`` is called a close.
Weekly (``1w``) values show their date without a time and say "week-on-week, since previous
weekly value (date)"; they get no anomaly scoring, which a known limit states.

Per-row qualifiers (fallback source, IEX single exchange, delayed quote, derived) sit on each
``Row``, so a value never loses its caveat when a renderer drops the known-limits block.
Cross-check items are labelled with the data date they compared (``details.ref_date``), never
the run's ``target_date``. The 2s10s spread is derived from two *official* yields of one ET
date (never a tick) and says so.

Empty or partial stores are normal (checks and sources land at different times): missing data
becomes a "data unavailable" row plus a known limit, never an exception. The model is
deterministic: no wall-clock reads, stable ordering, ``to_json`` output is byte-stable.
"""
# ruff: noqa: RUF001, RUF002, RUF003  (true minus sign and sigma are intentional output)

from __future__ import annotations

import json
import types
from collections.abc import Collection, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime
from typing import Any, Self, Union, get_args, get_origin, get_type_hints

import duckdb
import numpy as np

from deskbrief.checks.anomaly import MIN_MOVES, daily_moves
from deskbrief.config import Config, Instrument, config_hash
from deskbrief.query import (
    BASIS_OFFICIAL,
    CHANGE_DECIMALS,
    OFFICIAL_INTERVALS,
    ChangeRow,
    CheckItem,
    HealthSummary,
    Lineage,
    ValueRow,
    field_label,
    health_summary,
    instrument_flags,
    lineages,
    overnight_changes,
    series,
    series_all,
    value_label,
)
from deskbrief.timeutil import ensure_utc, et_date, et_to_utc, to_et

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
SPREAD_ID = "UST_2S10S"
_FLAG_ORDER = ("none", "pass", "warn", "fail")  # worst last
SPREAD_LOOKBACK_DAYS = 14  # enough official days to find the latest date both legs share

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
    qualifiers: tuple[str, ...] = ()  # per-row caveats: fallback, IEX, delayed, derived
    # typed numbers behind the text (None when unavailable); writers use these, never the text
    value: float | None = None
    value_unit: str | None = None  # unit of ``value``: pct, USD, fx_rate, index_pts, ..., bp
    change_value: float | None = None
    change_value_unit: str | None = None  # pct | bp | pts | pp
    # worst check or cross-check status for this instrument on the report date:
    # fail > warn > pass; skip is no result; none when there is none
    dq_flag: str = "none"
    # the instant behind ``as_of_text``: the value's own timestamp (a daily official value is
    # midnight ET of its date, not a publish time); the spread's is the midnight-ET stamp of the
    # date both legs share. None when unavailable.
    as_of: datetime | None = None


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
    ref_label: str | None = None  # crosscheck: the data date compared, e.g. "13 Nov"


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
        *,
        exclude_tiers: Collection[int] = (),
    ) -> Self:
        """Build the brief for ET ``report_date`` as known at ``report_time`` (aware UTC).

        ``exclude_tiers`` is passed to the ``query.py`` reads that accept it
        (``overnight_changes``): an instrument whose latest value is an excluded tier falls back
        to its best remaining tier, as a hosted deployment that may not show tier 1 needs. The
        default ``()`` excludes nothing. Health, the derived 2s10s spread (official tier 3
        yields only) and the anomaly history read no tier-1 value and take no filter.
        """
        report_time = ensure_utc(report_time)
        changes = {
            c.last.instrument_id: c
            for c in overnight_changes(duck, report_date, report_time, exclude_tiers=exclude_tiers)
        }
        snapshot = tuple(c.last for c in changes.values())  # ordered by instrument_id
        baselines = tuple(c.previous for c in changes.values() if c.previous is not None)
        enabled = [s for s, st in config.sources.items() if st.enabled]
        summary = health_summary(duck, report_date, expected_sources=enabled)
        flags = instrument_flags(duck, report_date)

        rows = {
            i.id: replace(
                _row(i, changes.get(i.id), report_date, report_time),
                dq_flag=flags.get(i.id, "none"),
            )
            for i in config.instruments
        }
        spread = _spread_row(duck, config, report_time)
        if spread:  # derived: as bad as its worst leg
            legs = [flags.get(i, "none") for i in ("UST_10Y", "UST_2Y")]
            spread = replace(spread, dq_flag=max(legs, key=_FLAG_ORDER.index))
        sections = tuple(
            Section(
                ac,
                title,
                tuple(rows[i.id] for i in config.instruments if i.asset_class == ac)
                + ((spread,) if spread and ac == "rates" else ()),
            )
            for ac, title in SECTIONS
            if any(i.asset_class == ac for i in config.instruments)
        )
        if spread:
            rows[SPREAD_ID] = spread
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
            lineage=lineages(duck, snapshot) + lineages(duck, baselines, role="baseline"),
        )

    # -- serialisation ------------------------------------------------------
    def to_json(self) -> str:
        """Deterministic JSON (stable key order, UTC ISO timestamps, UTF-8 characters kept)."""
        return json.dumps(_encode(self), indent=2, ensure_ascii=False, allow_nan=False) + "\n"

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
    if row.unit == "contracts":
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def _as_of_text(row: ValueRow, report_date: date) -> str:
    """Daily, weekly and monthly values show their date; intraday ones show the time, plus the
    date unless today."""
    et = to_et(row.as_of)
    if row.interval in OFFICIAL_INTERVALS:
        return f"{et.day} {et:%b}"
    if et.date() == report_date:
        return f"{et:%H:%M}"
    return f"{et.day} {et:%b} {et:%H:%M}"


def _basis_text(change: ChangeRow) -> str:
    """``previous NYSE close (13 Nov)``: the baseline always carries its own date."""
    text = change.basis
    if change.previous is not None:
        prev = to_et(change.previous.as_of)
        text += f" ({prev.day} {prev:%b})"
    if change.note:
        text += f"; {change.note}"
    return text


def _title(report_time: datetime) -> str:
    et = to_et(report_time)
    return f"Desk Brief — {et:%a} {et.day} {et:%b %Y}, {et:%H:%M} ET"


def _short(instrument_id: str) -> str:
    return SHORT_LABELS.get(instrument_id, instrument_id)


# ---------------------------------------------------------------------------
# rows, headline, health
# ---------------------------------------------------------------------------


def _qualifiers(last: ValueRow, report_time: datetime) -> tuple[str, ...]:
    """Caveats that belong to this one value (not to the instrument in general)."""
    out: list[str] = []
    if last.is_fallback:
        out.append(f"fallback source: {last.source}")
    if last.dataset.startswith("iex"):
        out.append("IEX: single exchange, not the consolidated tape")
    if last.tier == 2 and last.interval in ("tick", "1m"):
        age = (report_time - last.as_of).total_seconds()
        if age < 0:  # a timestamp after the report time is a bad clock, not a fresh quote
            out.append(
                f"delayed quote dated {max(1, int(-age // 60))} min in the future: "
                "check the source's timestamp"
            )
        else:
            out.append(f"delayed quote, {int(age // 60)} min old")
    return tuple(out)


def _tail(change: ChangeRow) -> str:
    if change.status == "ok":
        wow = " week-on-week," if change.last.interval == "1w" else ""
        return f"{change_text(change)}{wow} since {_basis_text(change)}"
    if change.status == "no_move_yet":
        return f"no move to report since {change.basis}"
    if change.previous is not None:  # found, but a percentage of zero is undefined
        prev = to_et(change.previous.as_of)
        return (
            f"change unavailable ({change.basis} of {value_text(change.previous)} on "
            f"{prev.day} {prev:%b} was found and is zero)"
        )
    return f"change unavailable ({change.basis} not found)"


def _row(
    inst: Instrument, change: ChangeRow | None, report_date: date, report_time: datetime
) -> Row:
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
        description=f"{label}, {_tail(change)}",
        basis=change.basis,
        as_of_text=_as_of_text(last, report_date),
        tier=last.tier,
        tier_glyph=TIER_GLYPH.get(last.tier, ""),
        tier_name=TIER_NAME.get(last.tier, ""),
        change=change,
        qualifiers=_qualifiers(last, report_time),
        as_of=last.as_of,
        value=last.value,
        value_unit=last.unit,
        change_value=change.change,
        change_value_unit=change.change_unit if change.change is not None else None,
    )


def _official_yields(duck: duckdb.DuckDBPyConnection, iid: str, at: datetime) -> dict[date, float]:
    """Official (tier 3) yields by ET date: a tick or a delayed history row is never a leg."""
    return {
        et_date(r.as_of): r.value
        for r in series(duck, iid, SPREAD_LOOKBACK_DAYS, at)
        if r.tier == 3 and r.field == "yield"
    }


def _spread_row(duck: duckdb.DuckDBPyConnection, config: Config, at: datetime) -> Row | None:
    """2s10s in bp from the official 10Y and 2Y yields of one ET date (spec §9.1 sample).

    None when the config has no 10Y/2Y pair. Both legs must come from the same ET date: the
    row uses the *latest date both legs share* (``common[-1]``), which can be older than the
    report date and older than either leg's own newest value (for example when one leg has
    published a later day than the other). That date (midnight ET) is the row's ``as_of`` and is
    repeated in its qualifier. The change compares it with the previous shared date (with only
    one shared date it says so, rather than leaving a bare ``n/a``). A tier-2 or tick yield is
    never a leg (see ``_official_yields``). No shared date gives a "data unavailable" row.
    ``value``/``change_value`` are typed floats in bp, so writers need not parse the text.
    """
    ids = {i.id for i in config.instruments}
    if not {"UST_10Y", "UST_2Y"} <= ids:
        return None
    ten, two = (_official_yields(duck, i, at) for i in ("UST_10Y", "UST_2Y"))
    common = sorted(ten.keys() & two.keys())
    name = "US 2s10s spread"
    base: dict[str, Any] = {
        "instrument_id": SPREAD_ID,
        "name": name,
        "asset_class": "rates",
        "is_proxy": False,
        "proxy_label": None,
        "tier": 3,
        "tier_glyph": TIER_GLYPH[3],
        "tier_name": TIER_NAME[3],
        "change": None,
    }
    if not common:
        return Row(
            **base,
            label=f"{name} (derived)",
            available=False,
            value_text=UNAVAILABLE,
            change_text="n/a",
            description=f"{name}: {UNAVAILABLE}",
            basis=None,
            as_of_text="n/a",
            qualifiers=(),
        )
    d = common[-1]  # the latest ET date on which both legs have an official yield
    spread = round((ten[d] - two[d]) * 100.0, CHANGE_DECIMALS)
    d_text = f"{d.day} {d:%b}"
    label = f"{name} (derived: 10-year minus 2-year official yield)"
    tail = f"{signed(spread, 0)}bp on {d_text}"
    change = "n/a"
    no_change = "change unavailable (no earlier common date)"
    move: float | None = None
    if len(common) > 1:
        prev = common[-2]
        move = round(spread - (ten[prev] - two[prev]) * 100.0, CHANGE_DECIMALS)
        change = signed(move, 0) + "bp"
        tail += f", {change} since previous official value ({prev.day} {prev:%b})"
    else:  # one shared date: nothing to compare with, and the caption must not repeat the value
        tail += f", {no_change}"
    return Row(
        **base,
        label=label,
        available=True,
        value_text=signed(spread, 0) + "bp",
        change_text=change,
        description=f"{label}, {tail}",
        basis=BASIS_OFFICIAL,
        as_of_text=d_text,
        qualifiers=(f"derived from official 10Y and 2Y yields of {d_text}",),
        as_of=et_to_utc(d, "00:00"),
        value=spread,
        value_unit="bp",
        change_value=move,
        change_value_unit="bp" if move is not None else None,
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
    cfg = config.checks.anomaly
    hist = series_all(duck, cfg.window_days * 2, at)
    moves: list[tuple[float, Notable]] = []
    for ch in changes:
        past = hist.get(ch.last.instrument_id, ())
        if ch.previous is None or ch.change is None or not past:
            continue
        # the T20 anomaly check's own scoring: |move| against the mean and sample std of the
        # |daily moves| before it, in the unit the check uses (bp for yields, return otherwise)
        unit = past[-1].unit
        series_moves = daily_moves(np.array([p.value for p in past], dtype=float), unit)
        if past[-1].as_of == ch.last.as_of:
            # the scored move is the series' own last move (official-last yields, rates, FX
            # fixes): leave it out of its baseline, like T20's moves[-(window+1):-1]
            series_moves = series_moves[:-1]
        prior = series_moves[-cfg.window_days :]
        prior = prior[np.isfinite(prior)]
        last = float(daily_moves(np.array([ch.previous.value, ch.last.value]), unit)[0])
        std = float(prior.std(ddof=1)) if len(prior) > 1 else 0.0
        if len(prior) < MIN_MOVES or not np.isfinite(last) or std < 1e-12:
            continue
        z = (last - float(prior.mean())) / std
        if z >= cfg.z_warn:
            text = (
                f"{_short(ch.last.instrument_id)} {change_text(ch)} is {z:.1f}σ "
                f"vs prior {len(prior)} daily moves"
            )
            moves.append((z, Notable("move", text, ch.last.instrument_id)))
    ordered = [n for _, n in sorted(moves, key=lambda m: (-m[0], m[1].instrument_id or ""))]
    crosschecks = [
        Notable("crosscheck", _crosscheck_text(i), i.instrument_id, _ref_label(i))
        for i in summary.issues
        if i.kind == "crosscheck"
    ]
    return tuple(ordered + crosschecks)


def _ref_label(i: CheckItem) -> str | None:
    """The data date a cross-check compared (``details.ref_date``), never the run's date."""
    return f"{i.ref_date.day} {i.ref_date:%b}" if i.ref_date else None


def _crosscheck_text(i: CheckItem) -> str:
    where = ", ".join(
        p for p in (i.group_id, f"data of {_ref_label(i)}" if i.ref_date else None) if p
    )
    where = f" ({where})" if where else ""
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
                "◐ delayed values are not live; ○ official values are as of "
                "their publication date (shown), not today",
            )
        )
    fallbacks = sorted({f"{r.instrument_id} ({r.source})" for r in snapshot if r.is_fallback})
    if fallbacks:
        out.append(KnownLimit("fallback", "Fallback source standing in: " + ", ".join(fallbacks)))
    derived = sorted(
        {r.instrument_id for r in snapshot if r.is_derived}
        | ({SPREAD_ID} if (sp := rows.get(SPREAD_ID)) and sp.available else set())
    )
    if derived:
        out.append(
            KnownLimit("derived", "Derived values, not published directly: " + ", ".join(derived))
        )
    weekly = sorted({r.instrument_id for r in snapshot if r.interval == "1w"})
    if weekly:
        out.append(
            KnownLimit(
                "weekly",
                "Weekly series get no anomaly check (it reads daily values only): "
                + ", ".join(weekly),
            )
        )
    missing = [r.instrument_id for r in rows.values() if not r.available]
    if missing:
        out.append(KnownLimit("unavailable", f"{UNAVAILABLE.capitalize()}: " + ", ".join(missing)))
    if s.n_checks - s.n_skip == 0:
        out.append(
            KnownLimit("dq_missing", f"No data-quality results recorded for {s.target_date}")
        )
    else:
        out.append(
            KnownLimit(
                "checks_scope",
                f"Check results are those of the latest run for {s.target_date}: a partial "
                "re-run supersedes both check tables (data quality and cross-checks) for the "
                "date, so a check it did not repeat is not counted",
            )
        )
    for i in s.issues:
        where = i.instrument_id or i.source or i.group_id or "all"
        if i.ref_date:
            where += f", data of {_ref_label(i)}"
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
