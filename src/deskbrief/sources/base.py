"""Provider interface (spec §5.1).

CONTRACT FILE. Changing anything here needs a labeled ``contract:`` PR.

Every source implements three steps:

* ``plan(job, ctx)``  -> which requests this job needs (pure, uses config + calendars)
* ``fetch(req, http)`` -> IO only; returns the exact bytes in a ``RawResponse``
* ``parse(raw)``     -> pure function of bytes; returns a DataFrame with ``OBS_COLUMNS``
  (except the lineage columns, which the runner fills in: see ``RUNNER_FILLED_COLUMNS``)

Because ``parse`` is pure, any raw file can be replayed through a fixed parser.

Rules every source follows:

* Construction: ``SourceClass(config)``. The registry (card T03) builds sources this way, so
  ``plan`` and ``parse`` resolve symbols to instruments via
  ``config.feed_index(source, dataset)`` -> ``{symbol: (Instrument, Feed)}``; ``tier``,
  ``unit`` and ``scale`` come from there, never hard-coded.
* ``parse`` returns rows only for configured instruments (extra provider rows, e.g. raw ECB
  EUR/GBP used to derive GBPUSD, stay in the raw file). Derived values set ``is_derived``.
* A legitimately empty response (holiday, no new data) -> ``empty_frame("parsed_observations")``.
* A malformed/unexpected payload -> raise :class:`ParseError`; the runner quarantines the fetch
  and records a DQ failure. Never return partial garbage.
* Apply ``Feed.scale`` before returning values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import pandas as pd

from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext

if TYPE_CHECKING:
    from deskbrief.config import Config

#: Columns the runner adds after parse (lineage); parsers must not set them.
#: ``ingested_at`` = ``RawResponse.responded_at``.
RUNNER_FILLED_COLUMNS: tuple[str, ...] = ("fetch_id", "run_id", "ingested_at", "parser_version")


class ParseError(Exception):
    """Raised by ``Source.parse`` when a payload is malformed or not what was expected."""


@runtime_checkable
class HttpClient(Protocol):
    """Shared HTTP client (implemented in ``sources/http.py``, card T03).

    Secrets go in ``secret_params`` / ``secret_headers`` and are never logged or stored;
    the URL recorded in ``RawResponse.url_redacted`` has them masked.
    Transport/HTTP failures are returned as ``RawResponse.error``, not raised.

    ``units`` is the number of daily-budget units one request costs (default 1; for example a
    batch quote that the provider counts once per symbol). Implementations that track a budget
    reserve that many units before the call.
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
        units: int = 1,
    ) -> RawResponse: ...


@runtime_checkable
class Source(Protocol):
    name: str
    parser_version: str  # bump on any change to parse()

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]: ...

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse: ...

    def parse(self, raw: RawResponse) -> pd.DataFrame: ...


#: How the registry constructs a source.
SourceFactory = Callable[["Config"], Source]
