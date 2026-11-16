"""R2Backend against an in-memory fake S3 client (no network)."""

from __future__ import annotations

import pytest
from store_fakes import FakeS3

from deskbrief.store.backend import KeyExistsError, KeyNotFoundError, StorageBackend, StorageError
from deskbrief.store.r2 import R2Backend, r2_credentials_from_env, r2_endpoint


@pytest.fixture
def backend(fake_s3: FakeS3) -> R2Backend:
    return R2Backend("bkt", fake_s3)


def test_satisfies_protocol(backend: R2Backend) -> None:
    assert isinstance(backend, StorageBackend)


def test_round_trip(backend: R2Backend) -> None:
    assert not backend.exists("a/b")
    backend.put_bytes("a/b", b"hello")
    assert backend.exists("a/b")
    assert backend.get_bytes("a/b") == b"hello"


def test_write_once_uses_conditional_put(backend: R2Backend, fake_s3: FakeS3) -> None:
    backend.put_bytes("k", b"first")
    with pytest.raises(KeyExistsError):
        backend.put_bytes("k", b"second")
    assert fake_s3.objects["k"] == b"first"
    assert all(kw.get("IfNoneMatch") == "*" for _, kw in fake_s3.calls)


def test_overwrite_is_unconditional(backend: R2Backend, fake_s3: FakeS3) -> None:
    backend.put_bytes("k", b"one")
    backend.put_bytes("k", b"two", overwrite=True)
    assert fake_s3.objects["k"] == b"two"
    assert "IfNoneMatch" not in fake_s3.calls[-1][1]


def test_get_missing_raises(backend: R2Backend) -> None:
    with pytest.raises(KeyNotFoundError):
        backend.get_bytes("missing")


def test_delete_missing_is_noop(backend: R2Backend, fake_s3: FakeS3) -> None:
    backend.delete("missing")
    backend.put_bytes("k", b"v")
    backend.delete("k")
    assert not backend.exists("k")


def test_list_paginates_and_sorts(backend: R2Backend) -> None:
    for key in ("p/3", "p/1", "p/2", "q/1", "p/4"):
        backend.put_bytes(key, b"x")
    assert backend.list("p/") == ["p/1", "p/2", "p/3", "p/4"]
    assert backend.list("zzz/") == []


@pytest.mark.parametrize(
    "call",
    [
        lambda b: b.put_bytes("k", b"v"),
        lambda b: b.get_bytes("k"),
        lambda b: b.exists("k"),
        lambda b: b.list("k"),
        lambda b: b.delete("k"),
    ],
    ids=["put", "get", "exists", "list", "delete"],
)
def test_other_client_errors_become_storage_error(
    backend: R2Backend, fake_s3: FakeS3, call: object
) -> None:
    # INJECTED FAULT: provider returns an access error.
    fake_s3.fail_with = "AccessDenied"
    with pytest.raises(StorageError, match="AccessDenied"):
        call(backend)  # type: ignore[operator]


def test_endpoint() -> None:
    assert r2_endpoint("abc123") == "https://abc123.r2.cloudflarestorage.com"


def test_credentials_prefer_writer_then_reader() -> None:
    env = {
        "R2_ACCOUNT_ID": "acct",
        "R2_ACCESS_KEY_ID": "w",
        "R2_SECRET_ACCESS_KEY": "ws",
        "R2_READER_ACCESS_KEY_ID": "r",
        "R2_READER_SECRET_ACCESS_KEY": "rs",
    }
    assert r2_credentials_from_env(env).access_key_id == "w"
    del env["R2_ACCESS_KEY_ID"]
    assert r2_credentials_from_env(env).access_key_id == "r"


def test_credentials_missing_names_vars_not_values() -> None:
    with pytest.raises(StorageError, match="R2_ACCOUNT_ID") as ei:
        r2_credentials_from_env({"R2_ACCESS_KEY_ID": "SECRETVALUE"})
    assert "SECRETVALUE" not in str(ei.value)


def test_credentials_repr_hides_secret() -> None:
    creds = r2_credentials_from_env(
        {"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "TOPSECRET"}
    )
    assert "TOPSECRET" not in repr(creds)


def test_from_env_builds_client_for_r2_endpoint() -> None:
    env = {"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s"}
    backend = R2Backend.from_env("deskbrief-data", env)
    assert backend.bucket == "deskbrief-data"
    assert backend._client.meta.endpoint_url == "https://acct.r2.cloudflarestorage.com"
