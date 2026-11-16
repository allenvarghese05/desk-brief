"""Live R2 round trip. Marked ``integration``: never runs in PR CI.

Needs ``R2_ACCOUNT_ID``, ``R2_ACCESS_KEY_ID``, ``R2_SECRET_ACCESS_KEY`` and
``R2_TEST_BUCKET`` (a bucket the writer token may use). Skips when any is missing.
Everything is written under a unique ``_it/`` prefix and deleted afterwards. This test uses
a throwaway probe object, not production data.

Run locally (the default addopts disable sockets, so override them)::

    uv run pytest tests/integration -m integration -o addopts="" --no-cov
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from deskbrief.store.backend import KeyExistsError, KeyNotFoundError
from deskbrief.store.r2 import R2Backend

pytestmark = pytest.mark.integration

_REQUIRED = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_TEST_BUCKET")


@pytest.fixture
def backend() -> R2Backend:
    missing = [n for n in _REQUIRED if not os.environ.get(n)]
    if missing:
        pytest.skip(f"R2 integration env not set: {', '.join(missing)}")
    return R2Backend.from_env(os.environ["R2_TEST_BUCKET"])


@pytest.fixture
def prefix(backend: R2Backend) -> Iterator[str]:
    pfx = f"_it/{uuid.uuid4().hex}/"
    yield pfx
    for key in backend.list(pfx):
        backend.delete(key)


def test_write_once_round_trip_list_delete(backend: R2Backend, prefix: str) -> None:
    key = f"{prefix}probe.txt"
    assert not backend.exists(key)
    backend.put_bytes(key, b"first")
    assert backend.exists(key)
    assert backend.get_bytes(key) == b"first"
    with pytest.raises(KeyExistsError):  # conditional put (If-None-Match: *) enforced by R2
        backend.put_bytes(key, b"second")
    assert backend.get_bytes(key) == b"first"
    backend.put_bytes(key, b"third", overwrite=True)
    assert backend.get_bytes(key) == b"third"
    assert backend.list(prefix) == [key]
    backend.delete(key)
    with pytest.raises(KeyNotFoundError):
        backend.get_bytes(key)
