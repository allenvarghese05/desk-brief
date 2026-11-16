"""Cloudflare R2 storage backend (S3-compatible, via boto3).

Endpoint: ``https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com``. Credentials come from the
environment (spec §11); the CI writer uses ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY``,
the dashboard uses the read-only ``R2_READER_*`` pair. Secrets are never logged or put in
exception messages.

Write-once: ``put_bytes(overwrite=False)`` sends ``PutObject`` with ``If-None-Match: *``, which
R2 enforces atomically (HTTP 412 -> ``KeyExistsError``). There is no check-then-write race.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from deskbrief.store.backend import KeyExistsError, KeyNotFoundError, StorageError

_NOT_FOUND = {"NoSuchKey", "404", "NotFound"}
_PRECONDITION_FAILED = {"PreconditionFailed", "412"}


def r2_endpoint(account_id: str) -> str:
    return f"https://{account_id}.r2.cloudflarestorage.com"


@dataclass(frozen=True)
class R2Credentials:
    account_id: str
    access_key_id: str
    secret_access_key: str = field(repr=False)  # never printed


def r2_credentials_from_env(env: Mapping[str, str]) -> R2Credentials:
    """Writer keys if present, else the read-only reader keys. Names only in errors."""
    account = env.get("R2_ACCOUNT_ID", "")
    pairs = (
        ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"),
        ("R2_READER_ACCESS_KEY_ID", "R2_READER_SECRET_ACCESS_KEY"),
    )
    for key_name, secret_name in pairs:
        key, secret = env.get(key_name, ""), env.get(secret_name, "")
        if account and key and secret:
            return R2Credentials(account, key, secret)
    missing = [n for n in ("R2_ACCOUNT_ID", *pairs[0]) if not env.get(n)]
    raise StorageError(f"R2 credentials not configured; missing env vars: {', '.join(missing)}")


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class R2Backend:
    """``StorageBackend`` on an R2 bucket. Keys map 1:1 to object keys."""

    def __init__(self, bucket: str, client: Any) -> None:
        self.bucket = bucket
        self._client = client

    @classmethod
    def from_credentials(cls, bucket: str, creds: R2Credentials) -> R2Backend:
        client = boto3.client(
            "s3",
            endpoint_url=r2_endpoint(creds.account_id),
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key,
            region_name="auto",
            config=BotoConfig(retries={"max_attempts": 5, "mode": "standard"}),
        )
        return cls(bucket, client)

    @classmethod
    def from_env(cls, bucket: str, env: Mapping[str, str] | None = None) -> R2Backend:
        return cls.from_credentials(
            bucket, r2_credentials_from_env(os.environ if env is None else env)
        )

    def put_bytes(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Body": data}
        if not overwrite:
            kwargs["IfNoneMatch"] = "*"
        try:
            self._client.put_object(**kwargs)
        except ClientError as exc:
            if _code(exc) in _PRECONDITION_FAILED:
                raise KeyExistsError(key) from None
            raise StorageError(f"put {key!r} failed: {_code(exc) or 'client error'}") from exc

    def get_bytes(self, key: str) -> bytes:
        try:
            body: bytes = self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND:
                raise KeyNotFoundError(key) from None
            raise StorageError(f"get {key!r} failed: {_code(exc) or 'client error'}") from exc
        return body

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND:
                return False
            raise StorageError(f"head {key!r} failed: {_code(exc) or 'client error'}") from exc
        return True

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        try:
            pages = self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix
            )
            for page in pages:
                keys.extend(obj["Key"] for obj in page.get("Contents", []))
        except ClientError as exc:
            raise StorageError(f"list {prefix!r} failed: {_code(exc) or 'client error'}") from exc
        return sorted(keys)

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND:
                return
            raise StorageError(f"delete {key!r} failed: {_code(exc) or 'client error'}") from exc
