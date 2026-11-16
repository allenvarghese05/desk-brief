"""LocalBackend: round trips, write-once, atomicity, key safety (spec §2.2, §6)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deskbrief.store.backend import KeyExistsError, KeyNotFoundError, StorageBackend
from deskbrief.store.local import LocalBackend


@pytest.fixture
def backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(tmp_path / "data")


def test_satisfies_protocol(backend: LocalBackend) -> None:
    assert isinstance(backend, StorageBackend)


def test_round_trip_and_exists(backend: LocalBackend) -> None:
    assert not backend.exists("a/b/c.bin")
    backend.put_bytes("a/b/c.bin", b"\x00\x01payload")
    assert backend.exists("a/b/c.bin")
    assert backend.get_bytes("a/b/c.bin") == b"\x00\x01payload"


def test_write_once_by_default(backend: LocalBackend) -> None:
    backend.put_bytes("raw/x.json", b"first")
    with pytest.raises(KeyExistsError):
        backend.put_bytes("raw/x.json", b"second")
    assert backend.get_bytes("raw/x.json") == b"first"


def test_overwrite_replaces(backend: LocalBackend) -> None:
    backend.put_bytes("k", b"one")
    backend.put_bytes("k", b"two", overwrite=True)
    assert backend.get_bytes("k") == b"two"


def test_get_missing_raises(backend: LocalBackend) -> None:
    with pytest.raises(KeyNotFoundError):
        backend.get_bytes("nope/missing")


def test_delete_missing_is_noop_and_delete_removes(backend: LocalBackend) -> None:
    backend.delete("never/existed")
    backend.put_bytes("d/k", b"v")
    backend.delete("d/k")
    assert not backend.exists("d/k")


def test_list_prefix_sorted_and_relative(backend: LocalBackend) -> None:
    for key in ("p/b/2", "p/a/1", "p/a/0", "q/z", "pp/other"):
        backend.put_bytes(key, b"x")
    assert backend.list("p/") == ["p/a/0", "p/a/1", "p/b/2"]
    assert backend.list("p/a/") == ["p/a/0", "p/a/1"]
    # prefix is a string prefix, not a directory: "p" also matches "pp/other"
    assert backend.list("p") == ["p/a/0", "p/a/1", "p/b/2", "pp/other"]
    assert backend.list("nothing/") == []
    assert backend.list("") == ["p/a/0", "p/a/1", "p/b/2", "pp/other", "q/z"]


def test_no_temp_files_left_after_write(backend: LocalBackend, tmp_path: Path) -> None:
    backend.put_bytes("a/k", b"v")
    backend.put_bytes("a/k", b"w", overwrite=True)
    names = sorted(p.name for p in (tmp_path / "data" / "a").iterdir())
    assert names == ["k"]


def test_failed_write_leaves_no_partial_file(
    backend: LocalBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # INJECTED FAULT: the rename/link step fails after the temp file was written.
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(os, "link", boom)
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk went away"):
        backend.put_bytes("a/k", b"v")
    with pytest.raises(OSError, match="disk went away"):
        backend.put_bytes("a/k", b"v", overwrite=True)
    assert not backend.exists("a/k")
    assert list((tmp_path / "data" / "a").iterdir()) == []


def test_list_ignores_in_flight_temp_files(backend: LocalBackend, tmp_path: Path) -> None:
    backend.put_bytes("a/k", b"v")
    (tmp_path / "data" / "a" / ".k.123.deskbrief-tmp").write_bytes(b"partial")
    assert backend.list("a/") == ["a/k"]


@pytest.mark.parametrize("key", ["", "/abs", "a/../../etc/passwd", "a//b", "../x", "a/./b", "a\\b"])
def test_rejects_unsafe_keys(backend: LocalBackend, key: str) -> None:
    with pytest.raises(ValueError, match="invalid storage key"):
        backend.put_bytes(key, b"x")
    with pytest.raises(ValueError, match="invalid storage key"):
        backend.get_bytes(key)


def test_list_rejects_traversal_prefix(backend: LocalBackend) -> None:
    with pytest.raises(ValueError, match="invalid storage prefix"):
        backend.list("../")


def test_root_is_exposed_resolved(backend: LocalBackend, tmp_path: Path) -> None:
    backend.put_bytes("k", b"v")
    assert backend.root == (tmp_path / "data").resolve()
    assert (backend.root / "k").read_bytes() == b"v"
