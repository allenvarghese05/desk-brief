"""Configuration contract: pydantic models for ``config.yaml`` (spec §4).

CONTRACT FILE. Changing model shapes needs a labeled ``contract:`` PR. Changing *values* in
``config.yaml`` (thresholds, instruments) is a normal PR.

Validate from the command line (used by the CI ``config`` job)::

    python -m deskbrief.config [path/to/config.yaml]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from deskbrief.timeutil import parse_hhmm

KNOWN_SOURCES: frozenset[str] = frozenset(
    {
        "treasury",
        "fred",
        "ecb",
        "stooq",
        "cboe",
        "alpaca",
        "twelvedata",
        "yahoo",
        "cftc",
        "fiscaldata",  # P-024 / T64: Treasury upcoming auctions (keyless)
        "edgar",  # P-024 / T64: SEC EDGAR filings, Form 4, 13F
    }
)

#: SEC EDGAR fair-access limit: 10 requests per second, so at most 600 per minute (P-024).
SEC_MAX_RATE_PER_MIN: float = 600.0

Tier = Literal[1, 2, 3]
AssetClass = Literal["equity", "rates", "fx", "commodity", "index", "vol", "macro"]
Kind = Literal["etf", "stock", "future", "index", "yield", "fx", "macro"]
Unit = Literal["USD", "pct", "fx_rate", "index_pts", "index", "contracts"]
Relation = Literal["same", "return", "inverse_duration", "level"]
CalendarName = Literal["XNYS", "SIFMA_US", "TARGET"]
#: Units of an overnight change (``deskbrief.query.ChangeRow.change_unit``).
ChangeUnit = Literal["pct", "bp", "pts", "pp"]

_ENV_NAME = r"^[A-Z][A-Z0-9_]*$"
_CIK = r"^\d{10}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceSettings(_Model):
    enabled: bool = True
    rate_per_min: float = Field(gt=0)
    timeout_s: float = Field(gt=0)
    retries: int = Field(ge=0, le=10)
    daily_budget: int | None = Field(default=None, gt=0)
    user_agent: str | None = None
    #: Name of the environment variable that holds the User-Agent (P-024: SEC EDGAR reads its
    #: contact User-Agent from the secret ``DESKBRIEF_SEC_USER_AGENT``, so no address is
    #: committed). The value is a secret: never logged, never stored. Exclusive with
    #: ``user_agent``.
    user_agent_env: str | None = Field(default=None, pattern=_ENV_NAME)

    @model_validator(mode="after")
    def _one_user_agent(self) -> Self:
        if self.user_agent is not None and self.user_agent_env is not None:
            raise ValueError("set user_agent or user_agent_env, not both")
        return self


class StorageSettings(_Model):
    backend: Literal["r2", "local"] = "local"
    bucket: str = "deskbrief-data"
    local_root: str = "./.data"


class Feed(_Model):
    """One way of observing an instrument: a (source, dataset, symbol) at a trust tier."""

    source: str
    dataset: str
    symbol: str
    tier: Tier
    scale: float = Field(default=1.0, gt=0)

    @field_validator("tier", mode="before")
    @classmethod
    def _strict_tier(cls, v: object) -> object:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("tier must be the integer 1, 2 or 3")
        return v


class Instrument(_Model):
    id: str = Field(pattern=r"^[A-Z0-9_]+$")
    name: str
    asset_class: AssetClass
    kind: Kind
    unit: Unit
    is_proxy: bool = False
    label: str | None = None
    feeds: tuple[Feed, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        keys = [(f.source, f.dataset, f.symbol) for f in self.feeds]
        if len(keys) != len(set(keys)):
            raise ValueError(f"instrument {self.id}: duplicate feed")
        if self.is_proxy and not (self.label and "proxy" in self.label.lower()):
            raise ValueError(f"instrument {self.id}: is_proxy needs a label mentioning 'proxy'")
        expected_unit = {"fx": "fx_rate", "yield": "pct"}.get(self.kind)
        if expected_unit and self.unit != expected_unit:
            raise ValueError(
                f"instrument {self.id}: kind {self.kind} must use unit {expected_unit}"
            )
        if self.kind in ("etf", "stock") and self.unit != "USD":
            raise ValueError(f"instrument {self.id}: {self.kind} must use unit USD")
        return self


class Tolerance(_Model):
    """Thresholds in basis points.

    Units of the compared difference depend on the relation: ``level`` / ``same`` on yields
    and ``official_pair`` compare absolute levels (1 bp = 0.01 percentage point); on prices,
    FX and indices they compare relative difference (1 bp = 0.01%); ``return`` and
    ``inverse_duration`` compare daily returns (1 bp = 0.01% return).
    """

    warn_bps: float = Field(ge=0)
    fail_bps: float = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.fail_bps < self.warn_bps:
            raise ValueError("fail_bps must be >= warn_bps")
        return self


class Group(_Model):
    """A quantity seen through several instruments/tiers, used by cross-checks (spec §3)."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    label: str
    proxy: str | None = None
    exact: str | None = None
    official: str | None = None
    relation: Relation
    duration: float | None = Field(default=None, gt=0)
    tolerance: Tolerance

    @model_validator(mode="after")
    def _shape(self) -> Self:
        members = [m for m in (self.proxy, self.exact, self.official) if m]
        if not members:
            raise ValueError(f"group {self.id}: needs at least one of proxy/exact/official")
        if self.relation == "inverse_duration":
            if self.duration is None:
                raise ValueError(f"group {self.id}: inverse_duration needs duration")
            if self.proxy is None or (self.exact is None and self.official is None):
                raise ValueError(f"group {self.id}: inverse_duration needs proxy and a yield")
        if self.relation == "return" and len(set(members)) < 2:
            raise ValueError(f"group {self.id}: return relation needs two distinct instruments")
        return self

    def members(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(m for m in (self.proxy, self.exact, self.official) if m))


class DatasetSchedule(_Model):
    """When a dataset is expected to be published; drives freshness checks (spec §8.1)."""

    source: str
    dataset: str
    expected_by_et: str
    lag_bdays: int = Field(ge=0, le=10)
    calendar: CalendarName
    grace_minutes: int = Field(default=60, gt=0)
    #: Weekday of the release (0 = Monday .. 6 = Sunday) for ``1w`` datasets (P-021); absent
    #: for daily and monthly datasets. Read by the freshness check (card T45).
    release_weekday: int | None = Field(default=None, ge=0, le=6, strict=True)

    @field_validator("expected_by_et")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parse_hhmm(v)
        return v


class IntradaySchedule(_Model):
    """A dataset polled during the session; its newest tick must stay fresh (spec §8.1)."""

    source: str
    dataset: str
    calendar: CalendarName = "XNYS"
    grace_minutes: int = Field(default=30, gt=0)


class AnomalySettings(_Model):
    window_days: int = Field(ge=5)
    z_warn: float = Field(gt=0)
    z_fail: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.z_fail < self.z_warn:
            raise ValueError("z_fail must be >= z_warn")
        return self


class ChecksSettings(_Model):
    anomaly: AnomalySettings
    validity: dict[Unit, tuple[float, float]]

    @field_validator("validity")
    @classmethod
    def _ranges(cls, v: dict[Unit, tuple[float, float]]) -> dict[Unit, tuple[float, float]]:
        for unit, (lo, hi) in v.items():
            if lo >= hi:
                raise ValueError(f"validity range for {unit} must have low < high")
        return v


class IexVsSipSettings(Tolerance):
    symbols: tuple[str, ...] = Field(min_length=1)
    max_fail_minutes_pct: float = Field(ge=0, le=100)


class FxVsEcbSettings(Tolerance):
    pairs: tuple[str, ...] = Field(min_length=1)
    fix_time_cet: str

    @field_validator("fix_time_cet")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parse_hhmm(v)
        return v


class OfficialPairSettings(Tolerance):
    pairs: tuple[tuple[str, str], ...] = Field(min_length=1)
    """(FRED series id, Treasury curve column) pairs."""


class CrosscheckSettings(_Model):
    iex_vs_sip: IexVsSipSettings
    fx_vs_ecb: FxVsEcbSettings
    official_pair: OfficialPairSettings
    yahoo_vs_primary: Tolerance
    close_vs_official: Tolerance


class ReportSettings(_Model):
    recipients_env: str = "REPORT_RECIPIENTS"
    headline_instruments: tuple[str, ...] = Field(min_length=1)


class DashboardSettings(_Model):
    mode_env: str = "DESKBRIEF_DISPLAY_MODE"
    refresh_seconds: int = Field(default=60, ge=15)
    hosted_hidden_tiers: tuple[Tier, ...] = (1,)


class RegimeRule(_Model):
    """One condition of a regime rule set (P-024 "the read"), over one market's overnight move.

    Met when the market's rankable change (``ChangeRow.rankable``, P-024: like-for-like and new
    since the previous brief) goes in ``direction`` by at least
    ``min_sigma`` (its size in sigma against its own recent daily moves) or, for a size rule, by at
    least ``min_change`` in ``change_unit`` (the unit of ``ChangeRow.change_unit``). Exactly one
    of ``min_sigma`` and ``min_change`` is set.
    """

    market: str  # instrument id
    direction: Literal["up", "down"]
    label: str = Field(min_length=1)  # shown next to the read, e.g. "Equity futures down"
    min_sigma: float | None = Field(default=None, gt=0)
    min_change: float | None = Field(default=None, gt=0)
    change_unit: ChangeUnit | None = None

    @model_validator(mode="after")
    def _one_threshold(self) -> Self:
        if (self.min_sigma is None) == (self.min_change is None):
            raise ValueError(f"rule {self.label!r}: set exactly one of min_sigma, min_change")
        if (self.min_change is None) != (self.change_unit is None):
            raise ValueError(f"rule {self.label!r}: change_unit goes with min_change only")
        return self


class RegimeRuleSet(_Model):
    """The rules behind one regime label (e.g. "Risk-off")."""

    label: str = Field(min_length=1)
    rules: tuple[RegimeRule, ...] = Field(min_length=1)
    min_met: int = Field(ge=1)  # how many rules must be met for this label

    @model_validator(mode="after")
    def _reachable(self) -> Self:
        if self.min_met > len(self.rules):
            raise ValueError(f"regime {self.label!r}: min_met exceeds the number of rules")
        return self


class RelatedPair(_Model):
    """Two markets that usually move together (P-024 "Check before using").

    Flagged when both have a rankable change and the changes differ by more than ``limit`` in
    ``unit``. The two changes must cover matching windows (for example gold futures, which
    trade overnight, against GLD, which does not, are compared over the same span): T62 decides
    how, and skips the pair when it cannot match them. ``limit`` is in
    ``unit`` (for prices, percentage points of the two % changes).
    """

    pair: tuple[str, str]  # instrument ids
    limit: float = Field(gt=0)
    unit: ChangeUnit = "pct"

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.pair[0] == self.pair[1]:
            raise ValueError(f"related pair {self.pair}: the two markets must differ")
        return self


class NoteSettings(_Model):
    """The morning note written by Claude (P-024, card T66). Inert without the API key."""

    enabled: bool = True
    model: str = "claude-sonnet-5-5"
    api_key_env: str = Field(default="ANTHROPIC_API_KEY", pattern=_ENV_NAME)
    max_sentences: int = Field(default=5, ge=1, le=10)
    timeout_s: float = Field(default=30.0, gt=0, le=300)
    #: Claude's effort level; low keeps adaptive thinking short for a 4-5 sentence note.
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    #: The reply's token ceiling. Thinking tokens count against it too, so it is sized far above
    #: the visible note (about 200 tokens) to keep thinking from crowding out the text; a reply
    #: that stops at this ceiling is treated as no note (T66).
    max_output_tokens: int = Field(default=8000, ge=2000, le=64000)
    #: Monthly spend cap in USD; when the month's estimated spend reaches it, no call is made.
    monthly_cap_usd: float = Field(default=5.0, gt=0)
    #: Prices used to estimate spend from the reply's token usage, USD per million tokens.
    price_input_usd_per_mtok: float = Field(default=2.0, gt=0)
    price_output_usd_per_mtok: float = Field(default=10.0, gt=0)
    #: A sentence containing any of these (lower case, matched as whole words) is dropped.
    forbidden_words: tuple[str, ...] = ()

    @field_validator("forbidden_words")
    @classmethod
    def _words(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for w in v:
            if not w.strip() or w != w.strip().lower():
                raise ValueError(f"forbidden word {w!r} must be non-empty, trimmed lower case")
        if len(v) != len(set(v)):
            raise ValueError("forbidden_words has duplicates")
        return v


class BriefSettings(_Model):
    """The brief shared by Home and the email (P-024): the read, Check before using, the note.

    The read (:meth:`read_label`, P-024 review ruling 1):

    * nothing ranked: ``quiet_label`` ("Quiet");
    * rules met for more than one label (the rules split): ``mixed_label`` ("Mixed");
    * one label has at least its ``min_met`` rules met and no other label has any: that label;
    * otherwise (moves ranked, but no label's rules met far enough): ``unclear_label``
      ("No clear read").
    """

    regime_rules: tuple[RegimeRuleSet, ...] = ()
    quiet_label: str = Field(default="Quiet", min_length=1)
    mixed_label: str = Field(default="Mixed", min_length=1)
    unclear_label: str = Field(default="No clear read", min_length=1)
    related_pairs: tuple[RelatedPair, ...] = ()
    note: NoteSettings = NoteSettings()

    @model_validator(mode="after")
    def _labels(self) -> Self:
        labels = [s.label for s in self.regime_rules]
        fixed = {self.quiet_label, self.mixed_label, self.unclear_label}
        if len(fixed) != 3:
            raise ValueError("quiet_label, mixed_label and unclear_label must differ")
        if len(labels) != len(set(labels)):
            raise ValueError("regime rule sets have duplicate labels")
        clash = sorted(set(labels) & fixed)
        if clash:
            raise ValueError(f"regime labels {clash} are reserved for quiet/mixed/unclear")
        pairs = [frozenset(p.pair) for p in self.related_pairs]
        if len(pairs) != len(set(pairs)):
            raise ValueError("related_pairs has a duplicate pair")
        return self

    def read_label(self, n_ranked: int, met: Mapping[str, int]) -> str:
        """The regime label from how many moves are ranked and, per rule-set label, how many of
        its rules are met (labels not in ``met`` count as 0). Pure; T62 supplies the counts."""
        if n_ranked <= 0:
            return self.quiet_label
        with_any = [s for s in self.regime_rules if met.get(s.label, 0) > 0]
        if len(with_any) > 1:
            return self.mixed_label
        if with_any and met[with_any[0].label] >= with_any[0].min_met:
            return with_any[0].label
        return self.unclear_label


class FredRelease(_Model):
    """A FRED release shown in Today (P-024, card T65). FRED gives dates only, so the time is
    configured here."""

    release_id: int = Field(gt=0, strict=True)
    title: str = Field(min_length=1)  # e.g. "Consumer Price Index"
    time_et: str  # HH:MM, America/New_York

    @field_validator("time_et")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parse_hhmm(v)
        return v


class EventsSettings(_Model):
    """Today's calendar (``events`` table): Treasury auctions and FRED release dates."""

    horizon_days: int = Field(default=7, ge=1, le=31)  # fetch events this many days ahead
    fred_releases: tuple[FredRelease, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [r.release_id for r in self.fred_releases]
        if len(ids) != len(set(ids)):
            raise ValueError("events.fred_releases has a duplicate release_id")
        return self


class FilingsCompany(_Model):
    """A single stock whose SEC filings are fetched (P-024 Company section). Two instruments may
    share an issuer ``cik`` (share classes); instrument ids are unique."""

    instrument: str  # instrument id, kind stock
    cik: str = Field(pattern=_CIK)  # issuer CIK, 10 digits, zero-padded
    #: Matches the stock in 13F information tables, which identify securities by CUSIP.
    cusip: str | None = Field(default=None, pattern=r"^[0-9A-Z]{8}[0-9]$")


class FilingsHolder(_Model):
    """A 13F filer whose quarter-on-quarter position changes are shown."""

    cik: str = Field(pattern=_CIK)
    name: str = Field(min_length=1)


class FilingsSettings(_Model):
    """SEC EDGAR (``filings`` table, card T65): which stocks, how far back, which holders."""

    companies: tuple[FilingsCompany, ...] = ()
    insider_days: int = Field(default=90, ge=1, le=365)  # Form 4 look-back
    holders: tuple[FilingsHolder, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> Self:
        for what, keys in (
            ("instrument", [c.instrument for c in self.companies]),
            ("holder cik", [h.cik for h in self.holders]),
        ):
            if len(keys) != len(set(keys)):
                raise ValueError(f"filings: duplicate {what}")
        return self


class Config(_Model):
    version: Literal[1]
    timezone_report: Literal["America/New_York"] = "America/New_York"
    storage: StorageSettings = StorageSettings()
    sources: dict[str, SourceSettings]
    instruments: tuple[Instrument, ...] = Field(min_length=1)
    groups: tuple[Group, ...] = ()
    datasets: tuple[DatasetSchedule, ...] = ()
    intraday: tuple[IntradaySchedule, ...] = ()
    checks: ChecksSettings
    crosschecks: CrosscheckSettings
    report: ReportSettings
    dashboard: DashboardSettings = DashboardSettings()
    brief: BriefSettings = BriefSettings()
    events: EventsSettings = EventsSettings()
    filings: FilingsSettings = FilingsSettings()

    @model_validator(mode="after")
    def _cross_references(self) -> Self:
        errors: list[str] = []
        unknown = set(self.sources) - KNOWN_SOURCES
        if unknown:
            errors.append(f"unknown sources in 'sources': {sorted(unknown)}")
        ids = [i.id for i in self.instruments]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            errors.append(f"duplicate instrument ids: {dupes}")
        known_ids = set(ids)
        seen_feeds: dict[tuple[str, str, str], str] = {}
        for inst in self.instruments:
            for feed in inst.feeds:
                if feed.source not in self.sources:
                    errors.append(f"{inst.id}: feed source {feed.source!r} not configured")
                key = (feed.source, feed.dataset, feed.symbol)
                if key in seen_feeds:
                    errors.append(f"feed {key} used by both {seen_feeds[key]} and {inst.id}")
                seen_feeds[key] = inst.id
        used_units = {i.unit for i in self.instruments}
        missing_ranges = used_units - set(self.checks.validity)
        if missing_ranges:
            errors.append(f"checks.validity has no range for units {sorted(missing_ranges)}")
        group_ids = [g.id for g in self.groups]
        if len(group_ids) != len(set(group_ids)):
            errors.append("duplicate group ids")
        by_id = {i.id: i for i in self.instruments}
        for g in self.groups:
            for m in g.members():
                if m not in known_ids:
                    errors.append(f"group {g.id}: unknown instrument {m!r}")
            off = by_id.get(g.official or "")
            if g.official and off and not any(f.tier == 3 for f in off.feeds):
                errors.append(f"group {g.id}: official {g.official} has no tier-3 feed")
            if g.relation == "inverse_duration":
                for y in (g.exact, g.official):
                    yi = by_id.get(y or "")
                    if y and yi and yi.kind != "yield":
                        errors.append(f"group {g.id}: {y} must be a yield for inverse_duration")
        all_feeds = {(f.source, f.dataset) for i in self.instruments for f in i.feeds}
        ds_keys = [(d.source, d.dataset) for d in self.datasets]
        if len(ds_keys) != len(set(ds_keys)):
            errors.append("duplicate datasets entries")
        for ds in self.datasets:
            if ds.source not in self.sources:
                errors.append(f"dataset {ds.source}/{ds.dataset}: source not configured")
            elif (ds.source, ds.dataset) not in all_feeds:
                errors.append(f"dataset {ds.source}/{ds.dataset}: no instrument feed uses it")
        intraday_keys = [(s.source, s.dataset) for s in self.intraday]
        if len(intraday_keys) != len(set(intraday_keys)):
            errors.append("duplicate intraday entries")
        for sched in self.intraday:
            if sched.source not in self.sources:
                errors.append(f"intraday {sched.source}/{sched.dataset}: source not configured")
            elif (sched.source, sched.dataset) not in all_feeds:
                errors.append(
                    f"intraday {sched.source}/{sched.dataset}: no instrument feed uses it"
                )
        heads = self.report.headline_instruments
        if len(heads) != len(set(heads)):
            errors.append("report headline: duplicate instruments")
        for h in heads:
            if h not in known_ids:
                errors.append(f"report headline: unknown instrument {h!r}")

        def has_feed(inst_id: str, source: str, dataset: str) -> bool:
            inst = by_id.get(inst_id)
            return inst is not None and any(
                f.source == source and f.dataset == dataset for f in inst.feeds
            )

        for sym in self.crosschecks.iex_vs_sip.symbols:
            if not (
                has_feed(sym, "alpaca", "iex_bars_1m") and has_feed(sym, "alpaca", "sip_bars_1m")
            ):
                errors.append(f"iex_vs_sip: {sym!r} needs alpaca iex_bars_1m and sip_bars_1m feeds")
        for pair in self.crosschecks.fx_vs_ecb.pairs:
            fx = by_id.get(pair)
            if fx is None or fx.asset_class != "fx":
                errors.append(f"fx_vs_ecb: {pair!r} is not an fx instrument")
            elif not any(f.source == "ecb" for f in fx.feeds):
                errors.append(f"fx_vs_ecb: {pair!r} has no ecb feed")
        fred_syms = {f.symbol for i in self.instruments for f in i.feeds if f.source == "fred"}
        tsy_syms = {f.symbol for i in self.instruments for f in i.feeds if f.source == "treasury"}
        for fred_id, tsy_col in self.crosschecks.official_pair.pairs:
            if fred_id not in fred_syms:
                errors.append(f"official_pair: {fred_id!r} is not a configured fred feed")
            if tsy_col not in tsy_syms:
                errors.append(f"official_pair: {tsy_col!r} is not a configured treasury feed")
        errors.extend(self._p024_errors(by_id))
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def _p024_errors(self, by_id: dict[str, Instrument]) -> list[str]:
        """Cross-references of the P-024 sections: brief, events, filings, edgar."""
        errors: list[str] = []
        for rule_set in self.brief.regime_rules:
            for rule in rule_set.rules:
                if rule.market not in by_id:
                    errors.append(f"regime {rule_set.label!r}: unknown market {rule.market!r}")
        for rp in self.brief.related_pairs:
            for m in rp.pair:
                if m not in by_id:
                    errors.append(f"related pair {rp.pair}: unknown market {m!r}")
        if self.events.fred_releases and "fred" not in self.sources:
            errors.append("events.fred_releases needs the fred source")
        for company in self.filings.companies:
            inst = by_id.get(company.instrument)
            if inst is None:
                errors.append(f"filings: unknown instrument {company.instrument!r}")
            elif inst.kind != "stock":
                errors.append(f"filings: {company.instrument} is a {inst.kind}, not a stock")
        if (self.filings.companies or self.filings.holders) and "edgar" not in self.sources:
            errors.append("filings needs the edgar source")
        edgar = self.sources.get("edgar")
        if edgar is not None:
            if edgar.user_agent_env is None:
                errors.append("sources.edgar: user_agent_env is required (SEC contact User-Agent)")
            if edgar.rate_per_min > SEC_MAX_RATE_PER_MIN:
                errors.append(
                    f"sources.edgar: rate_per_min {edgar.rate_per_min:g} exceeds SEC's "
                    f"{SEC_MAX_RATE_PER_MIN:g} (10 per second)"
                )
        return errors

    # -- convenience lookups (pure, no IO) --------------------------------
    def instrument(self, instrument_id: str) -> Instrument:
        for inst in self.instruments:
            if inst.id == instrument_id:
                return inst
        raise KeyError(instrument_id)

    def feed_index(self, source: str, dataset: str) -> dict[str, tuple[Instrument, Feed]]:
        """``{provider symbol: (instrument, feed)}`` for one dataset (symbols are unique)."""
        return {f.symbol: (i, f) for i, f in self.feeds_for(source, dataset)}

    def feeds_for(self, source: str, dataset: str | None = None) -> list[tuple[Instrument, Feed]]:
        """All (instrument, feed) pairs served by ``source`` (optionally one dataset)."""
        return [
            (inst, feed)
            for inst in self.instruments
            for feed in inst.feeds
            if feed.source == source and (dataset is None or feed.dataset == dataset)
        ]


def default_config_path() -> Path:
    """``$DESKBRIEF_CONFIG`` if set, else ``./config.yaml``."""
    return Path(os.environ.get("DESKBRIEF_CONFIG", "config.yaml"))


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently keeping the last."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[object, object]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


#: Environment variables that override config values (so CI can switch to R2).
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "DESKBRIEF_STORAGE_BACKEND": ("storage", "backend"),
    "DESKBRIEF_STORAGE_BUCKET": ("storage", "bucket"),
}


def _apply_env_overrides(data: dict[str, object]) -> None:
    for env, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(env)
        if value:
            sect = data.setdefault(section, {})
            if isinstance(sect, dict):
                sect[key] = value


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate config. Raises ``ValueError`` with all problems on invalid config.

    ``DESKBRIEF_STORAGE_BACKEND`` / ``DESKBRIEF_STORAGE_BUCKET`` override the file.
    """
    p = Path(path) if path is not None else default_config_path()
    with p.open(encoding="utf-8") as fh:
        try:
            data = yaml.load(fh, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
        except yaml.YAMLError as exc:
            raise ValueError(f"{p}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{p}: top level must be a mapping")
    _apply_env_overrides(data)
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"{p}: invalid config:\n{exc}") from exc


def config_hash(config: Config) -> str:
    """Stable sha256 of the validated config; recorded on every run for lineage."""
    canonical = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else default_config_path()
    try:
        cfg = load_config(path)
    except (ValueError, OSError) as exc:
        print(f"config: INVALID\n{exc}", file=sys.stderr)
        return 1
    print(
        f"config: ok ({path}) instruments={len(cfg.instruments)} groups={len(cfg.groups)} "
        f"sources={len(cfg.sources)} hash={config_hash(cfg)[:12]}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
