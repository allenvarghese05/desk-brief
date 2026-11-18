"""Fixtures for check tests. SYNTHETIC FIXTURE: all data is made up."""

from __future__ import annotations

from pathlib import Path

import pytest

from config_support import enable_all_sources
from deskbrief.config import Config, load_config
from deskbrief.store.local import LocalBackend

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def config() -> Config:
    return enable_all_sources(load_config(REPO_ROOT / "config.yaml"))


@pytest.fixture
def store(tmp_path: Path) -> LocalBackend:
    return LocalBackend(tmp_path / "data")
