"""Shared fixtures for cross-check tests.

SYNTHETIC FIXTURE: all data in these tests is made up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deskbrief.config import Config, load_config
from deskbrief.store.local import LocalBackend

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def config() -> Config:
    return load_config(REPO_ROOT / "config.yaml")


@pytest.fixture
def store(tmp_path: Path) -> LocalBackend:
    return LocalBackend(tmp_path / "data")
