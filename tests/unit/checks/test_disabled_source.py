"""T25: a source switched off in ``config.yaml`` (Stooq, owner decision) is not an expectation.

SYNTHETIC FIXTURE: no observations at all; the checks run on an empty store.
Without this, the schedule entry for a disabled source would fail ``freshness`` and
``completeness`` on every run because the source is, by decision, never fetched.
"""

from __future__ import annotations

from datetime import date

from checks_support import ET_NOW, ctx_at, duck_for, find

from deskbrief.checks.completeness import CompletenessCheck
from deskbrief.checks.freshness import FreshnessCheck
from deskbrief.config import Config

WED = date(2026, 11, 18)


def _set(config: Config, source: str, enabled: bool) -> Config:
    sources = dict(config.sources)
    sources[source] = sources[source].model_copy(update={"enabled": enabled})
    return config.model_copy(update={"sources": sources})


def test_enabled_source_with_no_data_still_fails(store, config) -> None:  # type: ignore[no-untyped-def]
    config = _set(config, "stooq", True)
    ctx = ctx_at(config, store, ET_NOW(WED, "22:30"))
    duck = duck_for(store, config)
    assert find(FreshnessCheck().evaluate(ctx, duck), source="stooq")
    assert find(CompletenessCheck().evaluate(ctx, duck), source="stooq")


def test_disabled_source_produces_no_freshness_or_completeness_rows(store, config) -> None:  # type: ignore[no-untyped-def]
    off = _set(config, "stooq", False)
    ctx = ctx_at(off, store, ET_NOW(WED, "22:30"))
    duck = duck_for(store, off)
    assert find(FreshnessCheck().evaluate(ctx, duck), source="stooq") == []
    assert find(CompletenessCheck().evaluate(ctx, duck), source="stooq") == []
    # other sources are untouched
    assert find(FreshnessCheck().evaluate(ctx, duck), source="treasury")


def test_real_config_has_stooq_disabled_and_cboe_enabled() -> None:
    from pathlib import Path

    from deskbrief.config import load_config

    cfg = load_config(Path(__file__).resolve().parents[3] / "config.yaml")
    assert cfg.sources["stooq"].enabled is False
    assert cfg.sources["cboe"].enabled is True
