"""Raw layer writer: provider bytes stored exactly as received, plus a metadata sidecar.

Spec §2.2: raw is write-once. The key holds the ingest timestamp and a sha256 prefix, and both
objects are written with ``overwrite=False``, so a second write of the same response raises
``KeyExistsError`` instead of replacing evidence.

Order: body first, then sidecar. If the sidecar write fails the body stays (it is still the
true provider response); the error propagates so the run records it.

``ingested_at`` of a raw object is ``RawResponse.responded_at`` (the same instant every parsed
observation later carries), and its partition date is the UTC date of that instant (P-006).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from deskbrief.models import RawResponse, RunContext
from deskbrief.store.backend import StorageBackend, raw_key, raw_meta_key


def raw_extension(content_type: str) -> str:
    """File extension from a Content-Type header: json, csv, xml, txt, default bin."""
    media = content_type.split(";", 1)[0].strip().lower()
    subtype = media.rpartition("/")[2]
    if media == "text/csv" or subtype == "csv":
        return "csv"
    if subtype == "json" or subtype.endswith("+json"):
        return "json"
    if subtype == "xml" or subtype.endswith("+xml"):
        return "xml"
    if media.startswith("text/"):
        return "txt"
    return "bin"


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


class RawWriter:
    """Writes ``RawResponse`` bodies and ``.meta.json`` sidecars through a ``StorageBackend``."""

    def __init__(self, store: StorageBackend) -> None:
        self._store = store

    def write(self, raw: RawResponse, ctx: RunContext) -> str:
        """Store ``raw`` and its sidecar; return the raw key. Raises ``KeyExistsError`` on repeat.

        Callers decide whether responses with an empty body (budget exhausted, calendar skip)
        belong in raw/; this writer stores whatever it is given.
        """
        sha = hashlib.sha256(raw.body).hexdigest()
        key = raw_key(
            source=raw.request.source,
            dataset=raw.request.dataset,
            ingested_at=raw.responded_at,
            sha256=sha,
            ext=raw_extension(raw.content_type),
        )
        self._store.put_bytes(key, raw.body)
        self._store.put_bytes(raw_meta_key(key), self._sidecar(raw, ctx, sha))
        return key

    @staticmethod
    def _sidecar(raw: RawResponse, ctx: RunContext, sha: str) -> bytes:
        meta: dict[str, Any] = {
            "fetch_id": raw.fetch_id,
            "run_id": ctx.run_id,
            "source": raw.request.source,
            "dataset": raw.request.dataset,
            "symbols": list(raw.request.symbols),
            "params": dict(raw.request.params),
            "url_redacted": raw.url_redacted,
            "requested_at": _iso(raw.requested_at),
            "responded_at": _iso(raw.responded_at),
            "latency_ms": raw.latency_ms,
            "http_status": raw.http_status,
            "fetch_status": raw.fetch_status.value,
            "error": raw.error,
            "sha256": sha,
            "bytes": len(raw.body),
            "attempts": raw.attempts,
        }
        return json.dumps(meta, sort_keys=True, indent=2).encode("utf-8")
