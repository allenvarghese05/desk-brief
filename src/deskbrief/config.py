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
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from deskbrief.timeutil import parse_hhmm

KNOWN_SOURCES: frozenset[str] = frozenset(
    {"treasury", "fred", "ecb", "stooq", "cboe", "alpaca", "twelvedata", "yahoo"}
)

Tier = Literal[1, 2, 3]
AssetClass = Literal["equity", "rates", "fx", "commodity", "index", "vol", "macro"]
Kind = Literal["etf", "stock", "future", "index", "yield", "fx", "macro"]
Unit = Literal["USD", "pct", "fx_rate", "index_pts"]
Relation = Literal["same", "return", "inverse_duration", "level"]
CalendarName = Literal["XNYS", "SIFMA_US", "TARGET"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceSettings(_Model):
    enabled: bool = True
    rate_per_min: float = Field(gt=0)
    timeout_s: float = Field(gt=0)
    retries: int = Field(ge=0, le=10)
    daily_budget: int | None = Field(default=None, gt=0)
    user_agent: str | None = None


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
    def _no_duplicate_feeds(self) -> Self:
        keys = [(f.source, f.dataset, f.symbol) for f in self.feeds]
        if len(keys) != len(set(keys)):
            raise ValueError(f"instrument {self.id}: duplicate feed")
        return self


class Tolerance(_Model):
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

    @field_validator("expected_by_et")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parse_hhmm(v)
        return v


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


class Config(_Model):
    version: Literal[1]
    timezone_report: Literal["America/New_York"] = "America/New_York"
    storage: StorageSettings = StorageSettings()
    sources: dict[str, SourceSettings]
    instruments: tuple[Instrument, ...] = Field(min_length=1)
    groups: tuple[Group, ...] = ()
    datasets: tuple[DatasetSchedule, ...] = ()
    checks: ChecksSettings
    crosschecks: CrosscheckSettings
    report: ReportSettings
    dashboard: DashboardSettings = DashboardSettings()

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
        for inst in self.instruments:
            for feed in inst.feeds:
                if feed.source not in self.sources:
                    errors.append(f"{inst.id}: feed source {feed.source!r} not configured")
        group_ids = [g.id for g in self.groups]
        if len(group_ids) != len(set(group_ids)):
            errors.append("duplicate group ids")
        for g in self.groups:
            for m in g.members():
                if m not in known_ids:
                    errors.append(f"group {g.id}: unknown instrument {m!r}")
        for ds in self.datasets:
            if ds.source not in self.sources:
                errors.append(f"dataset {ds.source}/{ds.dataset}: source not configured")
        for h in self.report.headline_instruments:
            if h not in known_ids:
                errors.append(f"report headline: unknown instrument {h!r}")
        by_id = {i.id: i for i in self.instruments}
        for sym in self.crosschecks.iex_vs_sip.symbols:
            found = by_id.get(sym)
            if found is None or not any(f.source == "alpaca" for f in found.feeds):
                errors.append(f"iex_vs_sip: {sym!r} is not an instrument with alpaca feeds")
        for pair in self.crosschecks.fx_vs_ecb.pairs:
            fx = by_id.get(pair)
            if fx is None or fx.asset_class != "fx":
                errors.append(f"fx_vs_ecb: {pair!r} is not an fx instrument")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    # -- convenience lookups (pure, no IO) --------------------------------
    def instrument(self, instrument_id: str) -> Instrument:
        for inst in self.instruments:
            if inst.id == instrument_id:
                return inst
        raise KeyError(instrument_id)

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


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate config. Raises ``ValueError`` with all problems on invalid config."""
    p = Path(path) if path is not None else default_config_path()
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{p}: top level must be a mapping")
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
