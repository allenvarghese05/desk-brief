"""The registry and catalog build the T65 sources (card T65). SYNTHETIC config only."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fakes_t65 import ROOT, t65_config

from deskbrief.config import load_config
from deskbrief.sources import catalog, registry
from deskbrief.sources.edgar import EdgarSource
from deskbrief.sources.fiscaldata import FiscalDataSource
from deskbrief.sources.tables import (
    EDGAR_COMPANY,
    EDGAR_HOLDERS,
    FISCALDATA_AUCTIONS,
    FRED_RELEASE_DATES,
    planned_datasets,
)


@pytest.fixture
def fresh_registry() -> Iterator[None]:
    saved = dict(registry._FACTORIES)
    registry.clear()
    yield
    registry.clear()
    registry._FACTORIES.update(saved)


def test_catalog_lists_the_table_source_modules() -> None:
    assert catalog.TABLE_SOURCE_MODULES == (
        "deskbrief.sources.fiscaldata",
        "deskbrief.sources.edgar",
    )
    assert not set(catalog.TABLE_SOURCE_MODULES) & set(catalog.KNOWN_SOURCE_MODULES)
    defaults = catalog.load_all.__defaults__
    assert defaults is not None
    assert set(catalog.TABLE_SOURCE_MODULES) <= set(defaults[0])  # loaded by default


@pytest.mark.usefixtures("fresh_registry")
def test_registry_builds_fiscaldata_and_edgar_when_enabled() -> None:
    registry.register("fiscaldata", FiscalDataSource)  # what each module does on import
    registry.register("edgar", EdgarSource)
    built = {s.name: s for s in registry.get_sources(t65_config())}
    assert isinstance(built["fiscaldata"], FiscalDataSource)
    assert isinstance(built["edgar"], EdgarSource)


@pytest.mark.usefixtures("fresh_registry")
def test_registry_skips_them_while_disabled_in_config_yaml() -> None:
    registry.register("fiscaldata", FiscalDataSource)
    registry.register("edgar", EdgarSource)
    cfg = load_config(ROOT / "config.yaml")
    assert not cfg.sources["fiscaldata"].enabled
    assert not cfg.sources["edgar"].enabled
    assert registry.get_sources(cfg) == []


def test_planned_datasets_follow_config() -> None:
    assert planned_datasets(load_config(ROOT / "config.yaml")) == []
    assert planned_datasets(t65_config()) == [
        FISCALDATA_AUCTIONS,
        FRED_RELEASE_DATES,
        EDGAR_COMPANY,
        EDGAR_HOLDERS,
    ]
    assert planned_datasets(t65_config(fred_releases=False, holders=False)) == [
        FISCALDATA_AUCTIONS,
        EDGAR_COMPANY,
    ]
    assert planned_datasets(t65_config(companies=False)) == [
        FISCALDATA_AUCTIONS,
        FRED_RELEASE_DATES,
    ]
