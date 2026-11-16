"""RawWriter: write-once body + sidecar (spec §2.2, §6).

SYNTHETIC FIXTURE: provider responses are made up.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from store_fakes import make_ctx

from deskbrief.config import Config
from deskbrief.models import FetchRequest, FetchStatus, RawResponse
from deskbrief.store.backend import KeyExistsError, raw_meta_key
from deskbrief.store.local import LocalBackend
from deskbrief.store.raw import RawWriter, raw_extension

REQ_AT = datetime(2026, 11, 16, 12, 0, 0, 100000, tzinfo=UTC)
RESP_AT = datetime(2026, 11, 16, 12, 0, 0, 350000, tzinfo=UTC)


def _raw(
    body: bytes = b'{"price": 1.5}',
    content_type: str = "application/json",
    **kw: object,
) -> RawResponse:
    defaults: dict[str, object] = {
        "request": FetchRequest(
            source="yahoo",
            dataset="quote",
            params={"interval": "1d"},
            symbols=("SPY", "QQQ"),
        ),
        "fetch_id": "01FETCH0000000000000000000",
        "requested_at": REQ_AT,
        "responded_at": RESP_AT,
        "http_status": 200,
        "content_type": content_type,
        "body": body,
        "url_redacted": "https://example.test/q?symbols=SPY,QQQ&apikey=***",
    }
    defaults.update(kw)
    return RawResponse(**defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("content_type", "ext"),
    [
        ("application/json", "json"),
        ("application/json; charset=utf-8", "json"),
        ("APPLICATION/VND.API+JSON", "json"),
        ("text/json", "json"),
        ("text/csv", "csv"),
        ("application/csv", "csv"),
        ("text/csv; charset=utf-8", "csv"),
        ("application/xml", "xml"),
        ("text/xml", "xml"),
        ("application/atom+xml", "xml"),
        ("text/plain", "txt"),
        ("text/html", "txt"),
        ("application/octet-stream", "bin"),
        ("", "bin"),
        ("garbage", "bin"),
    ],
)
def test_extension_from_content_type(content_type: str, ext: str) -> None:
    assert raw_extension(content_type) == ext


def test_writes_body_byte_for_byte_and_sidecar(local: LocalBackend, config: Config) -> None:
    ctx = make_ctx(config, local, run_id="01RUNBBBBBBBBBBBBBBBBBBBBB")
    body = b'{"price": 1.5, "pad":   "  keep  whitespace "}\n'
    key = RawWriter(local).write(_raw(body), ctx)

    sha = hashlib.sha256(body).hexdigest()
    assert key == (
        f"raw/source=yahoo/dataset=quote/date=2026-11-16/20261116T120000350000Z_{sha[:12]}.json"
    )
    assert local.get_bytes(key) == body

    meta = json.loads(local.get_bytes(raw_meta_key(key)))
    assert meta == {
        "fetch_id": "01FETCH0000000000000000000",
        "run_id": "01RUNBBBBBBBBBBBBBBBBBBBBB",
        "source": "yahoo",
        "dataset": "quote",
        "symbols": ["SPY", "QQQ"],
        "params": {"interval": "1d"},
        "url_redacted": "https://example.test/q?symbols=SPY,QQQ&apikey=***",
        "requested_at": "2026-11-16T12:00:00.100000+00:00",
        "responded_at": "2026-11-16T12:00:00.350000+00:00",
        "latency_ms": 250,
        "http_status": 200,
        "fetch_status": "ok",
        "error": None,
        "sha256": sha,
        "bytes": len(body),
        "attempts": 1,
    }


def test_error_response_sidecar_records_failure(local: LocalBackend, config: Config) -> None:
    raw = _raw(
        b"",
        "",
        http_status=503,
        error="upstream said no",
        attempts=3,
    )
    key = RawWriter(local).write(raw, make_ctx(config, local))
    assert key.endswith(".bin")
    meta = json.loads(local.get_bytes(raw_meta_key(key)))
    assert meta["fetch_status"] == FetchStatus.ERROR.value
    assert meta["error"] == "upstream said no"
    assert meta["attempts"] == 3
    assert meta["bytes"] == 0
    assert meta["sha256"] == hashlib.sha256(b"").hexdigest()


def test_secrets_in_error_never_reach_sidecar(local: LocalBackend, config: Config) -> None:
    raw = _raw(http_status=500, error="GET https://x.test/?apikey=SECRET123 failed")
    key = RawWriter(local).write(raw, make_ctx(config, local))
    stored = local.get_bytes(key) + local.get_bytes(raw_meta_key(key))
    assert b"SECRET123" not in stored


def test_write_once_refuses_second_write(local: LocalBackend, config: Config) -> None:
    ctx = make_ctx(config, local)
    writer = RawWriter(local)
    key = writer.write(_raw(), ctx)
    with pytest.raises(KeyExistsError):
        writer.write(_raw(), ctx)
    assert local.get_bytes(key) == b'{"price": 1.5}'


def test_different_bodies_get_different_keys(local: LocalBackend, config: Config) -> None:
    ctx = make_ctx(config, local)
    writer = RawWriter(local)
    k1 = writer.write(_raw(b'{"a":1}'), ctx)
    k2 = writer.write(_raw(b'{"a":2}'), ctx)
    assert k1 != k2
    assert len(local.list("raw/")) == 4  # two bodies + two sidecars
