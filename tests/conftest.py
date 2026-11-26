"""Shared pytest configuration.

Network access is disabled for every test by default (pytest-socket, see pyproject addopts).
Tests that need real services must be marked ``@pytest.mark.integration``;
they are excluded from PR CI.
Synthetic data lives only under ``tests/`` and must carry a "SYNTHETIC FIXTURE" or
"INJECTED FAULT" header.

Sandbox notes (T18). The suite runs from the repo root with a bare ``pytest -m "not integration"``:

- ``.env*`` files are never collected or stat'ed (``--ignore-glob`` in pyproject addopts);
- ``config.yaml`` comes from the ``repo_root`` fixture / ``REPO_ROOT``, never the cwd;
- httpx clients built in unit tests do not load the system CA store (no real TLS happens).
"""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute repo root, independent of the working directory."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def repo_config_path() -> Path:
    """Absolute path of the repo's ``config.yaml``."""
    return REPO_ROOT / "config.yaml"


def _bare_ssl_context(*_args: object, **_kwargs: object) -> ssl.SSLContext:
    # No CA bundle: unit tests block sockets, so no TLS handshake can happen.
    return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


@pytest.fixture(autouse=True)
def _no_system_ca_store(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("integration") is not None:
        yield
        return
    # The transport module binds create_ssl_context at import time (sync and async share it).
    mp = pytest.MonkeyPatch()
    mp.setattr("httpx._transports.default.create_ssl_context", _bare_ssl_context)
    try:
        yield
    finally:
        mp.undo()


class NoAnthropicInTestsError(RuntimeError):
    """Raised if a unit test ever tries to build the real Anthropic client."""


def _refuse_sdk_client(*_args: object, **_kwargs: object) -> object:
    raise NoAnthropicInTestsError("unit tests never build the real Anthropic client (T66)")


@pytest.fixture(autouse=True)
def _no_anthropic_api(request: pytest.FixtureRequest) -> Iterator[None]:
    """PR #102 review: no unit test may call the Anthropic API, whatever the developer's shell
    exports. The key is removed from the environment and the SDK client factory refuses to be
    built, so a test that forgets a fake gets "No note today", never a billed call."""
    if request.node.get_closest_marker("integration") is not None:
        yield
        return
    mp = pytest.MonkeyPatch()
    mp.delenv("ANTHROPIC_API_KEY", raising=False)
    mp.setattr("deskbrief.report.note._sdk_client", _refuse_sdk_client)
    try:
        yield
    finally:
        mp.undo()
