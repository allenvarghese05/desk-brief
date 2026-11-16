"""Provider interface (spec §5.1).

CONTRACT FILE. Changing anything here needs a labeled ``contract:`` PR.

Every source implements three steps:

* ``plan(job, ctx)``  -> which requests this job needs (pure, uses config + calendars)
* ``fetch(req, http)`` -> IO only; returns the exact bytes in a ``RawResponse``
* ``parse(raw)``     -> pure function of bytes; returns a DataFrame with ``OBS_COLUMNS``
  (except the lineage columns, which the runner fills in: see ``RUNNER_FILLED_COLUMNS``)

Because ``parse`` is pure, any raw file can be replayed through a fixed parser.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

import pandas as pd

from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext

#: Columns the runner adds after parse (lineage); parsers must not set them.
RUNNER_FILLED_COLUMNS: tuple[str, ...] = ("fetch_id", "run_id", "ingested_at", "parser_version")


@runtime_checkable
class HttpClient(Protocol):
    """Shared HTTP client (implemented in ``sources/http.py``, card T03).

    Secrets go in ``secret_params`` / ``secret_headers`` and are never logged or stored;
    the URL recorded in ``RawResponse.url_redacted`` has them masked.
    Transport/HTTP failures are returned as ``RawResponse.error``, not raised.
    """

    def get(
        self,
        req: FetchRequest,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        secret_params: Mapping[str, str] | None = None,
        secret_headers: Mapping[str, str] | None = None,
    ) -> RawResponse: ...


@runtime_checkable
class Source(Protocol):
    name: str
    parser_version: str  # bump on any change to parse()

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]: ...

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse: ...

    def parse(self, raw: RawResponse) -> pd.DataFrame: ...
