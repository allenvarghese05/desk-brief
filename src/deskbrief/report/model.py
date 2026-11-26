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
from deskbrief.config import BriefSettings, Config, Instrument, RelatedPair, config_hash
from deskbrief.query import (
    BASIS_OFFICIAL,
    CHANGE_DECIMALS,
    OFFICIAL_INTERVALS,
    ChangeRow,
    CheckItem,
    HealthSummary,
    Lineage,
    SeriesKey,
    ValueRow,
    field_label,
    health_summary,
    instrument_flags,
    lineages,
    overnight_changes,
    previous_brief_time,
    series,
    series_all,
    series_by_feed,
    series_key,
    source_name,
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
#: what a trader calls each instrument (the headline, the movers, the dashboard); anything not
#: listed shows its id (SPY, AAPL, EURUSD)
SHORT_LABELS = {
    "ES_F": "S&P fut",
    "UST_3M": "3M",
    "UST_2Y": "2Y",
    "UST_10Y": "10Y",
    "UST_20Y": "20Y",
    "UST_30Y": "30Y",
    "UST_2S10S": "2s10s",
    "GOLD_F": "Gold",
    "BRENT": "Brent",
    "N225": "Nikkei",
    "UNRATE": "Unemployment",
    "DFF": "Fed funds",
    "UST_10Y_REAL": "10Y real",
    "BEI_10Y": "10Y BEI",
    "USD_BROAD": "Broad USD",
    "HY_OAS": "HY OAS",
    "COT_GC_NET": "Gold COT net",
    "COT_ES_NET": "ES COT net",
    "COT_ZN_NET": "10Y note COT net",
    "COT_GC_OI": "Gold COT OI",
    "COT_ES_OI": "ES COT OI",
    "COT_ZN_OI": "10Y note COT OI",
}
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
    # P-024 (T62): the move's size in sigma against its own series' recent daily moves, for a
    # rankable change only; ``sigma_note`` says why a rankable change has no sigma
    sigma: float | None = None
    sigma_note: str | None = None


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
class Mover:
    """One market that moved more than usual (P-024): a rankable change whose sigma is at or
    above ``checks.anomaly.z_warn``. ``BriefModel.movers`` is the one ranking every surface
    reads (the email, the strip, the watchlist and the brief)."""

    instrument_id: str
    short: str  # what a trader calls it ("HY OAS")
    name: str
    sigma: float  # rounded to 0.1
    n_moves: int  # the earlier daily moves sigma was measured against
    value_text: str
    change_text: str
    change_value: float
    change_unit: str  # pct | bp | pts | pp
    direction: str  # up | down
    source: str
    tier: int
    as_of: datetime
    as_of_text: str
    basis: str  # the baseline with its date ("previous NYSE close (13 Nov)")
    check: bool = False  # a Check before using item names this market
    text: str = ""  # one sentence: "HY OAS +27bp is 3.6σ vs prior 30 daily moves"


@dataclass(frozen=True)
class RuleResult:
    """One configured regime rule and whether today's moves meet it."""

    regime: str  # the rule set's label ("Risk-off")
    label: str  # the rule's own label ("VIX up")
    market: str
    met: bool


@dataclass(frozen=True)
class Read:
    """The read (P-024): one regime label from the fixed rules in ``brief.regime_rules``.

    ``kind`` is ``regime`` (a rule set's label), ``mixed`` (rules met for more than one label),
    ``unclear`` (moves ranked, no label met far enough) or ``quiet`` (nothing ranked).
    ``regime`` names the rule set listed under "Why" (None for mixed and quiet). ``tone`` is the
    colour of the label: a regime takes its first rule's direction (Risk-off's equity futures
    down: ``down``); every other kind is ``flat``. Claude never picks the label."""

    label: str
    kind: str
    tone: str  # up | down | flat
    regime: str | None
    rules: tuple[RuleResult, ...]  # every configured rule, met or not, in config order
    n_ranked: int  # markets that moved more than usual (``len(movers)``)
    text: str  # the read as one line: "Risk-off. Equity futures down, VIX up."

    @property
    def met(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.rules if r.met)


@dataclass(frozen=True)
class Caveat:
    """A "Check before using" item (P-024): something that touches today's read and needs a
    second look. ``kind``: ``crosscheck`` (a failed or warning cross-check on a ranked market),
    ``basis`` (a basis note on a ranked move) or ``pair`` (two related markets moving further
    apart than ``brief.related_pairs`` allows)."""

    kind: str
    title: str  # "Brent vs WTI", "HY OAS"
    text: str
    instrument_ids: tuple[str, ...]


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
    # P-024 (T62): the ranking, the read and Check before using, shared by every surface
    movers: tuple[Mover, ...] = ()
    read: Read | None = None
    caveats: tuple[Caveat, ...] = ()
    previous_brief: datetime | None = None  # the brief "new since" compares with (T61)

    @classmethod
    def from_store(
        cls,
        duck: duckdb.DuckDBPyConnection,
        config: Config,
        report_date: date,
        report_time: datetime,
        *,
        exclude_tiers: Collection[int] = (),
        health_date: date | None = None,
    ) -> Self:
        """Build the brief for ET ``report_date`` as known at ``report_time`` (aware UTC).

        ``exclude_tiers`` is passed to the ``query.py`` reads that accept it
        (``overnight_changes``): an instrument whose latest value is an excluded tier falls back
        to its best remaining tier, as a hosted deployment that may not show tier 1 needs. The
        default ``()`` excludes nothing. Health, the derived 2s10s spread (official tier 3
        yields only) and the anomaly history read no tier-1 value and take no filter.

        ``health_date`` (keyword-only) is the run date whose checks the health block, the check
        flags and the check items read, for example ``query.latest_check_run(duck, now)``'s date
        on a weekend. When given, those reads are bounded by ``report_time`` (a re-run stored
        later is ignored). The default ``None`` reads ``report_date`` without that bound, exactly
        as before, so the morning email is unchanged.
        """
        report_time = ensure_utc(report_time)
        changes = {
            c.last.instrument_id: c
            for c in overnight_changes(duck, report_date, report_time, exclude_tiers=exclude_tiers)
        }
        snapshot = tuple(c.last for c in changes.values())  # ordered by instrument_id
        baselines = tuple(c.previous for c in changes.values() if c.previous is not None)
        enabled = [s for s, st in config.sources.items() if st.enabled]
        checks_date = report_date if health_date is None else health_date
        checks_as_of = None if health_date is None else report_time
        summary = health_summary(duck, checks_date, expected_sources=enabled, as_of=checks_as_of)
        flags = instrument_flags(duck, checks_date, as_of=checks_as_of)

        rows = {
            i.id: replace(
                _row(i, changes.get(i.id), report_date, report_time),
                dq_flag=flags.get(i.id, "none"),
            )
            for i in config.instruments
        }
        hidden = frozenset(exclude_tiers)
        by_feed = series_by_feed(duck, config.checks.anomaly.window_days * 2, report_time)
        best = series_all(duck, config.checks.anomaly.window_days * 2, report_time)
        scores = _scores(config, list(changes.values()), by_feed, best, hidden)
        for iid, sc in scores.items():
            rows[iid] = replace(rows[iid], sigma=sc.sigma, sigma_note=sc.note)
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
        movers = _movers(config, rows, scores)
        caveats = _caveats(config, rows, movers, summary, by_feed)
        flagged = {i for c in caveats for i in c.instrument_ids}
        movers = tuple(replace(m, check=m.instrument_id in flagged) for m in movers)
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
            notable=_notable(movers, summary),
            health=health,
            known_limits=_known_limits(config, rows, snapshot, summary),
            lineage=lineages(duck, snapshot) + lineages(duck, baselines, role="baseline"),
            movers=movers,
            read=_read(config.brief, rows, len(movers)),
            caveats=caveats,
            previous_brief=previous_brief_time(report_date, report_time),
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


def signed(x: float, decimals: int, *, grouped: bool = False) -> str:
    """``+0.4`` / ``−0.4`` (true minus sign); values that round to zero print unsigned.
    ``grouped`` adds thousands separators (``−10,000``)."""
    text = f"{x:+,.{decimals}f}" if grouped else f"{x:+.{decimals}f}"
    if float(text.replace(",", "")) == 0:
        return f"{0:.{decimals}f}"
    return text.replace("-", _MINUS)


def change_text(change: ChangeRow) -> str:
    if change.change is None:
        return "n/a"
    if change.last.unit == "contracts":
        # whole contracts, like the value ("240,000"); ``change_unit`` stays "pts" because the
        # Excel writer has no contracts format (follow-up in the T45 PR)
        return signed(change.change, 0, grouped=True) + " contracts"
    decimals = _UNIT_DECIMALS[change.change_unit]
    if change.last.unit == "index" and change.change_unit == "pts":
        decimals = 2  # an index such as NFCI moves by hundredths: "+0.04 pts", not "0.0 pts"
    return signed(change.change, decimals) + _UNIT_SUFFIX[change.change_unit]


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
    return SHORT_LABELS.get(instrument_id, instrument_id.replace("_", " "))


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
    note = f"; {change.note}" if change.note else ""
    return f"change unavailable ({change.basis} not found{note})"


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


@dataclass(frozen=True)
class _Score:
    sigma: float | None
    n_moves: int
    note: str | None  # why a rankable change has no sigma


def _sigma_history(
    ch: ChangeRow,
    by_feed: dict[SeriesKey, tuple[ValueRow, ...]],
    best: dict[str, tuple[ValueRow, ...]],
    hidden: frozenset[int],
) -> tuple[tuple[ValueRow, ...], str | None]:
    """The daily history a move is scored against, and why there is none (T61 review).

    The baseline's own daily series first, so the move and its history share one basis (a
    Yahoo ``^IRX`` quote against Yahoo's ``^IRX`` closes, Brent's future against its own
    closes). Failing that, the best-tier daily history of the instrument (an FX pair's 17:00
    quote has no daily series of its own), except for a quoted yield: the official par yield
    is another basis, so it is left without sigma and says why.
    """
    assert ch.previous is not None
    own = by_feed.get(series_key(ch.previous), ()) if ch.previous.interval == "1d" else ()
    own = tuple(r for r in own if r.tier not in hidden)
    if len(own) > MIN_MOVES:
        return own, None
    last = ch.last
    if ch.family == "yield" and last.interval not in OFFICIAL_INTERVALS:
        name = f"{source_name(ch.previous.source)} {ch.previous.dataset}"
        return (), (
            f"no sigma: fewer than {MIN_MOVES + 1} daily closes of {name} stored, and the "
            "official yield history measures another basis"
        )
    return tuple(r for r in best.get(last.instrument_id, ()) if r.tier not in hidden), None


def _scores(
    config: Config,
    changes: Sequence[ChangeRow],
    by_feed: dict[SeriesKey, tuple[ValueRow, ...]],
    best: dict[str, tuple[ValueRow, ...]],
    hidden: frozenset[int],
) -> dict[str, _Score]:
    """Sigma of every rankable change: the T20 anomaly check's own scoring, |move| against the
    mean and sample std of the |daily moves| before it, in the unit the check uses (bp for
    yields, return otherwise). P-024: only honest moves are scored (``ChangeRow.rankable``)."""
    window = config.checks.anomaly.window_days
    out: dict[str, _Score] = {}
    for ch in changes:
        if not ch.rankable or ch.previous is None or ch.change is None:
            continue
        iid = ch.last.instrument_id
        past, why = _sigma_history(ch, by_feed, best, hidden)
        if why is not None:
            out[iid] = _Score(None, 0, why)
            continue
        if not past:
            out[iid] = _Score(None, 0, "no sigma: no daily history stored")
            continue
        unit = past[-1].unit
        series_moves = daily_moves(np.array([p.value for p in past], dtype=float), unit)
        if past[-1].as_of == ch.last.as_of:
            # the scored move is the series' own last move (official-last yields, rates, FX
            # fixes): leave it out of its baseline, like T20's moves[-(window+1):-1]
            series_moves = series_moves[:-1]
        prior = series_moves[-window:]
        prior = prior[np.isfinite(prior)]
        last = float(daily_moves(np.array([ch.previous.value, ch.last.value]), unit)[0])
        std = float(prior.std(ddof=1)) if len(prior) > 1 else 0.0
        if len(prior) < MIN_MOVES or not np.isfinite(last) or std < 1e-12:
            out[iid] = _Score(
                None, len(prior), f"no sigma: fewer than {MIN_MOVES} earlier daily moves stored"
            )
            continue
        z = (last - float(prior.mean())) / std
        out[iid] = _Score(round(z, 2), len(prior), None)
    return out


def _direction(x: float) -> str:
    return "up" if x > 0 else "down" if x < 0 else "flat"


def _movers(config: Config, rows: dict[str, Row], scores: dict[str, _Score]) -> tuple[Mover, ...]:
    """Rankable changes with sigma at or above the anomaly warn level, largest first."""
    out: list[Mover] = []
    for iid, sc in scores.items():
        row = rows.get(iid)
        if sc.sigma is None or sc.sigma < config.checks.anomaly.z_warn or row is None:
            continue
        ch = row.change
        assert ch is not None
        assert ch.change is not None
        assert row.tier is not None
        assert row.as_of is not None
        text = (
            f"{_short(iid)} {row.change_text} is {sc.sigma:.1f}σ vs prior {sc.n_moves} daily moves"
        )
        out.append(
            Mover(
                instrument_id=iid,
                short=_short(iid),
                name=row.name,
                sigma=round(sc.sigma, 1),
                n_moves=sc.n_moves,
                value_text=row.value_text,
                change_text=row.change_text,
                change_value=ch.change,
                change_unit=ch.change_unit,
                direction=_direction(ch.change),
                source=source_name(ch.last.source),
                tier=row.tier,
                as_of=row.as_of,
                as_of_text=row.as_of_text,
                basis=_basis_text(ch),
                text=text,
            )
        )
    return tuple(
        sorted(out, key=lambda m: (-(scores[m.instrument_id].sigma or 0.0), m.instrument_id))
    )


def _notable(movers: Sequence[Mover], summary: HealthSummary) -> tuple[Notable, ...]:
    """The email's notable list: the movers' sentences, then failed or warning cross-checks."""
    crosschecks = [
        Notable("crosscheck", _crosscheck_text(i), i.instrument_id, _ref_label(i))
        for i in summary.issues
        if i.kind == "crosscheck"
    ]
    return tuple([Notable("move", m.text, m.instrument_id) for m in movers] + crosschecks)


# ---------------------------------------------------------------------------
# the read and Check before using (P-024, T62)
# ---------------------------------------------------------------------------


def _rule_met(row: Row | None, direction: str, min_sigma: float | None,
              min_change: float | None, change_unit: str | None) -> bool:  # fmt: skip
    """A regime rule over one market's rankable move (``config.RegimeRule``)."""
    ch = row.change if row is not None else None
    if row is None or ch is None or not ch.rankable or ch.change is None:
        return False
    if _direction(ch.change) != direction:
        return False
    if min_sigma is not None:
        return row.sigma is not None and row.sigma >= min_sigma
    assert min_change is not None
    return ch.change_unit == change_unit and abs(ch.change) >= min_change


def _read(brief: BriefSettings, rows: dict[str, Row], n_ranked: int) -> Read:
    results: list[RuleResult] = []
    met: dict[str, int] = {}
    for rs in brief.regime_rules:
        for rule in rs.rules:
            ok = _rule_met(
                rows.get(rule.market), rule.direction, rule.min_sigma, rule.min_change,
                rule.change_unit,
            )  # fmt: skip
            results.append(RuleResult(rs.label, rule.label, rule.market, ok))
            met[rs.label] = met.get(rs.label, 0) + int(ok)
    label = brief.read_label(n_ranked, met)
    hits = [r for r in results if r.met]
    if label == brief.quiet_label:
        kind, regime = "quiet", None
        text = f"{label}. Nothing moved more than usual."
    elif label == brief.mixed_label:
        kind, regime = "mixed", None
        parts = [
            f"{rs.label} ({', '.join(r.label for r in hits if r.regime == rs.label)})"
            for rs in brief.regime_rules
            if met.get(rs.label)
        ]
        text = f"{label}. Rules met for " + " and ".join(parts) + "."
    elif label == brief.unclear_label:
        kind = "unclear"
        regime = (
            max(brief.regime_rules, key=lambda rs: met.get(rs.label, 0)).label if hits else None
        )
        noun = "market" if n_ranked == 1 else "markets"
        text = f"{label}. {n_ranked} {noun} moved more than usual"
        if hits:
            need = next(rs.min_met for rs in brief.regime_rules if rs.label == regime)
            text += f"; {len(hits)} of the {need} {regime} rules needed are met"
        text += "."
    else:
        kind, regime = "regime", label
        text = f"{label}. " + _sentence([r.label for r in hits if r.regime == label]) + "."
    tone = "flat"
    if kind == "regime":
        first = next(rs for rs in brief.regime_rules if rs.label == label).rules[0]
        tone = first.direction
    return Read(label, kind, tone, regime, tuple(results), n_ranked, text)


def _sentence(labels: Sequence[str]) -> str:
    """ "Equity futures down, VIX up and high-yield spreads wider" (first word kept, the rest
    lower-cased only where they are plain words)."""
    parts = [labels[0], *(_lower_first(x) for x in labels[1:])] if labels else []
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _lower_first(text: str) -> str:
    """Lower-case the first word unless it is an acronym or ticker ("VIX up" stays)."""
    word, _, rest = text.partition(" ")
    if word[:1].isupper() and word[1:].islower():
        word = word.lower()
    return f"{word} {rest}".strip()


def _windows_match(a: ChangeRow, b: ChangeRow) -> bool:
    """Both moves span the same window: the same baseline day and last values of the same ET
    day within two hours of each other (gold futures quoted at 06:55 against GLD's close the
    evening before do not match)."""
    assert a.previous is not None
    assert b.previous is not None
    if et_date(a.previous.as_of) != et_date(b.previous.as_of):
        return False
    ta, tb = ensure_utc(a.last.as_of), ensure_utc(b.last.as_of)
    return et_date(ta) == et_date(tb) and abs((ta - tb).total_seconds()) <= 2 * 3600


def _close_window(
    a: ChangeRow, b: ChangeRow, by_feed: dict[SeriesKey, tuple[ValueRow, ...]]
) -> tuple[float, float, date, date] | None:
    """Both legs' % moves between the two latest ET dates on which both baseline series have a
    daily close, and those dates; None when they share fewer than two."""
    assert a.previous is not None
    assert b.previous is not None
    sa = {et_date(r.as_of): r.value for r in by_feed.get(series_key(a.previous), ())}
    sb = {et_date(r.as_of): r.value for r in by_feed.get(series_key(b.previous), ())}
    common = sorted(sa.keys() & sb.keys())
    if len(common) < 2 or not sa[common[-2]] or not sb[common[-2]]:
        return None
    d0, d1 = common[-2], common[-1]
    return (sa[d1] / sa[d0] - 1) * 100, (sb[d1] / sb[d0] - 1) * 100, d0, d1


def _pair_caveat(
    pair: RelatedPair, rows: dict[str, Row], by_feed: dict[SeriesKey, tuple[ValueRow, ...]]
) -> Caveat | None:
    """Two related markets whose moves differ by more than ``pair.limit``, compared over one
    window (``config.RelatedPair``): today's moves when their windows match, else their daily
    closes over the latest two dates both have; skipped when neither works."""
    ra, rb = rows.get(pair.pair[0]), rows.get(pair.pair[1])
    if ra is None or rb is None or ra.change is None or rb.change is None:
        return None
    a, b = ra.change, rb.change
    if not (a.rankable and b.rankable) or a.change is None or b.change is None:
        return None
    if a.change_unit != pair.unit or b.change_unit != pair.unit:
        return None
    na, nb = _short(pair.pair[0]), _short(pair.pair[1])
    if _windows_match(a, b):
        ma, mb = a.change, b.change
        assert a.previous is not None
        p = to_et(a.previous.as_of)
        window = f"since the {p:%a} {p.day} {p:%b} close"
    else:
        closes = _close_window(a, b, by_feed)
        if closes is None:
            return None
        ma, mb, d0, d1 = closes
        window = f"daily closes {d0:%a} {d0.day} {d0:%b} to {d1:%a} {d1.day} {d1:%b}"
    gap = abs(ma - mb)
    if gap <= pair.limit:
        return None
    sfx = _UNIT_SUFFIX[pair.unit]
    dec = _UNIT_DECIMALS[pair.unit]
    text = (
        f"{signed(gap, dec).lstrip('+')}{sfx} apart ({na} {signed(ma, dec)}{sfx}, "
        f"{nb} {signed(mb, dec)}{sfx}, {window}); they usually move within "
        f"{pair.limit:g}{sfx}."
    )
    return Caveat("pair", f"{na} vs {nb}", text, pair.pair)


def _caveats(
    config: Config,
    rows: dict[str, Row],
    movers: Sequence[Mover],
    summary: HealthSummary,
    by_feed: dict[SeriesKey, tuple[ValueRow, ...]],
) -> tuple[Caveat, ...]:
    """Check before using (P-024): only items touching what is ranked; the full list of limits
    and warnings stays on Data health."""
    ranked = {m.instrument_id for m in movers}
    members = {g.id: {m for m in (g.proxy, g.exact, g.official) if m} for g in config.groups}
    out: list[Caveat] = []
    for i in summary.issues:
        if i.kind != "crosscheck":
            continue
        touched = ({i.instrument_id} if i.instrument_id else set()) | members.get(
            i.group_id or "", set()
        )
        hit = sorted(touched & ranked)
        if hit:
            out.append(Caveat("crosscheck", ", ".join(_short(x) for x in hit),
                              _crosscheck_text(i), tuple(hit)))  # fmt: skip
    for m in movers:
        ch = rows[m.instrument_id].change
        if ch is not None and ch.note:
            out.append(Caveat("basis", m.short, f"{ch.note[:1].upper()}{ch.note[1:]}.",
                              (m.instrument_id,)))  # fmt: skip
    for pair in config.brief.related_pairs:
        c = _pair_caveat(pair, rows, by_feed)
        if c is not None:
            out.append(c)
    return tuple(out)


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
        # a field added later (with a default) may be missing from an older model.json
        return tp(
            **{f.name: _decode(hints[f.name], value[f.name]) for f in fields(tp) if f.name in value}
        )
    if tp is datetime:
        return datetime.fromisoformat(value)
    if tp is date:
        return date.fromisoformat(value)
    return value


__all__ = [
    "BriefModel",
    "Caveat",
    "Headline",
    "HeadlineItem",
    "HealthBlock",
    "KnownLimit",
    "Mover",
    "Notable",
    "Read",
    "Row",
    "RuleResult",
    "Section",
    "field_label",
]
