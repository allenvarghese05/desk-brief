"""Shared fixtures for storage tests.

SYNTHETIC FIXTURE: every row, response and credential used by these tests is made up.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from store_fakes import FakeS3, make_ctx

from deskbrief.config import Config, load_config
from deskbrief.models import RunContext
from deskbrief.store.local import LocalBackend

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def fake_s3() -> FakeS3:
    return FakeS3()


@pytest.fixture(scope="session")
def config() -> Config:
    return load_config(REPO_ROOT / "config.yaml")


@pytest.fixture
def local(tmp_path: Path) -> LocalBackend:
    return LocalBackend(tmp_path / "data")


@pytest.fixture
def ctx(config: Config, local: LocalBackend) -> RunContext:
    return make_ctx(config, local)
