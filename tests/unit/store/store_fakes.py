"""Helpers for storage tests (importable as ``store_fakes``).

SYNTHETIC FIXTURE: every row, response and credential is made up for tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

from botocore.exceptions import ClientError

from deskbrief.config import Config
from deskbrief.models import JobKind, RunContext, Trigger


class FakeS3:
    """SYNTHETIC FIXTURE: in-memory stand-in for the boto3 S3 client (no network)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_with: str | None = None  # error code to raise from every call

    def _maybe_fail(self, op: str) -> None:
        if self.fail_with:
            raise ClientError({"Error": {"Code": self.fail_with, "Message": "boom"}}, op)

    def put_object(self, **kw: Any) -> None:
        self.calls.append(("put_object", kw))
        self._maybe_fail("PutObject")
        if kw.get("IfNoneMatch") == "*" and kw["Key"] in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, **kw: Any) -> dict[str, Any]:
        self._maybe_fail("GetObject")
        if kw["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        data = self.objects[kw["Key"]]

        class _Body:
            def read(self) -> bytes:
                return data

        return {"Body": _Body()}

    def head_object(self, **kw: Any) -> dict[str, Any]:
        self._maybe_fail("HeadObject")
        if kw["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}

    def delete_object(self, **kw: Any) -> None:
        self._maybe_fail("DeleteObject")
        self.objects.pop(kw["Key"], None)

    def get_paginator(self, name: str) -> Any:
        assert name == "list_objects_v2"
        outer = self

        class _Pager:
            def paginate(self, *, Bucket: str, Prefix: str) -> Iterator[dict[str, Any]]:  # noqa: N803
                outer._maybe_fail("ListObjectsV2")
                keys = sorted(k for k in outer.objects if k.startswith(Prefix))
                half = len(keys) // 2  # two pages, to exercise pagination
                yield {"Contents": [{"Key": k} for k in keys[:half]]}
                yield {"Contents": [{"Key": k} for k in keys[half:]]} if keys[half:] else {}

        return _Pager()


def obs_row(**over: Any) -> dict[str, Any]:
    """SYNTHETIC FIXTURE: one valid ``observations`` row (a made-up SPY close)."""
    row: dict[str, Any] = {
        "instrument_id": "SPY",
        "source": "stooq",
        "dataset": "daily",
        "symbol": "spy.us",
        "tier": 3,
        "field": "close",
        "interval": "1d",
        "as_of": datetime(2026, 11, 13, 5, 0, tzinfo=UTC),  # 00:00 ET on 2026-11-13
        "value": 100.0,
        "unit": "USD",
        "is_derived": False,
        "is_fallback": False,
        "fetch_id": "F1",
        "run_id": "R1",
        "ingested_at": datetime(2026, 11, 13, 22, 0, tzinfo=UTC),
        "parser_version": "t1",
    }
    row.update(over)
    return row


def make_ctx(
    config: Config,
    store: Any,
    *,
    run_id: str = "01RUNAAAAAAAAAAAAAAAAAAAAA",
    target: date = date(2026, 11, 16),
    now: datetime = datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
) -> RunContext:
    return RunContext(
        run_id=run_id,
        job=JobKind.MORNING,
        trigger=Trigger.MANUAL,
        target_date=target,
        now_utc=now,
        git_sha="deadbeef",
        config_hash="cafe",
        config=config,
        store=store,
    )
