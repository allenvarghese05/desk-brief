"""The Source/HttpClient protocols are implementable and runtime-checkable."""

from collections.abc import Mapping
from datetime import UTC, datetime

import pandas as pd

from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext
from deskbrief.sources.base import HttpClient, Source

T = datetime(2026, 11, 16, tzinfo=UTC)


class _Http:
    def get(
        self,
        req: FetchRequest,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        secret_params: Mapping[str, str] | None = None,
        secret_headers: Mapping[str, str] | None = None,
        units: int = 1,
    ) -> RawResponse:
        return RawResponse(
            request=req,
            fetch_id="F",
            requested_at=T,
            responded_at=T,
            http_status=200,
            content_type="text/plain",
            body=b"ok",
            url_redacted=url,
        )


class _Dummy:
    name = "dummy"
    parser_version = "1"

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        return [FetchRequest(source=self.name, dataset="d")]

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        return http.get(req, "https://example.invalid/d")

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        return pd.DataFrame()


def test_protocols_runtime_checkable() -> None:
    assert isinstance(_Dummy(), Source)
    assert isinstance(_Http(), HttpClient)
    raw = _Dummy().fetch(FetchRequest(source="dummy", dataset="d"), _Http())
    assert raw.ok
    assert raw.url_redacted == "https://example.invalid/d"


def test_http_get_accepts_units_keyword() -> None:
    """T08A: the protocol's optional cost argument (default 1) can be passed by keyword."""
    req = FetchRequest(source="dummy", dataset="d")
    assert _Http().get(req, "https://example.invalid/d", units=3).ok
