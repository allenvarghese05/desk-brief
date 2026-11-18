"""T18: the suite must run from the repo root inside a sandbox (no special flags).

Plain pytest, no network. Nothing here is data; no fixture data is involved.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx
import pytest

from deskbrief.config import load_config


def test_collection_ignores_env_files(pytestconfig: pytest.Config) -> None:
    # The sandbox denies stat() on `.env*`; pytest's root-dir scan stats every entry unless
    # an ignore rule matches first (pytest_ignore_collect checks --ignore-glob before is_dir).
    globs = pytestconfig.getoption("ignore_glob")
    assert any(g.endswith("*.env*") for g in globs)


def test_repo_root_config_is_cwd_independent(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert (repo_root / "config.yaml").is_file()
    assert load_config(repo_root / "config.yaml").sources


def test_httpx_client_does_not_load_ca_store(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise PermissionError("CA store is not readable in the sandbox")

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", boom)
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", boom)
    with httpx.Client() as client:
        assert client is not None
