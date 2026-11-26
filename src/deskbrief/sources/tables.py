"""Sources that write the ``events`` or ``filings`` table instead of observations (card T65).

Not a contract file. ``sources/base.py`` says ``parse()`` returns observation rows, so a
table source keeps that promise by returning an empty ``parsed_observations`` frame from
``parse()`` (it produces no observations; a rebuild that replays its raw files therefore
writes nothing and quarantines nothing) and adds one method:

* ``parse_table(raw)`` -> the rows of its table (``events`` or ``filings``) without the
  runner-filled lineage columns ``fetch_id``, ``run_id`` and ``ingested_at``. Pure, like
  ``parse``: a malformed payload raises :class:`~deskbrief.sources.base.ParseError`.
* ``table_for(req)`` -> which table the rows of that request go to, or ``None`` when the
  request is an ordinary observation fetch (FRED serves both: its ``series`` datasets are
  observations, ``release_dates`` is ``events``).

The runner (``pipeline/runner.py``) routes a request whose ``table_for`` is not ``None``
through :func:`deskbrief.pipeline.clean.clean_table_response`, buffers the rows per replace
scope (``schemas.REPLACE_SCOPE``) and writes a scope only when every fetch that feeds it
succeeded (the writer rules in the ``schemas`` module docstring).

:data:`TABLE_DATASETS` lists the datasets the freshness check for these tables covers
(``checks/table_freshness.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeGuard, runtime_checkable

import pandas as pd

from deskbrief.models import FetchRequest, RawResponse
from deskbrief.schemas import to_frame
from deskbrief.sources.base import Source

if TYPE_CHECKING:
    from deskbrief.config import Config

TableName = Literal["events", "filings"]

#: Columns the runner adds to every table row (``ingested_at`` = ``RawResponse.responded_at``).
TABLE_LINEAGE_COLUMNS: tuple[str, ...] = ("fetch_id", "run_id", "ingested_at")


_PLACEHOLDER_AT = pd.Timestamp("1970-01-01", tz="UTC")


def table_frame(table: TableName, rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Typed rows of ``table`` without the lineage columns: what ``parse_table`` returns.

    Built with ``schemas.to_frame`` (so dtypes match the table) and placeholder lineage values
    that are dropped again; the runner fills the real ones.
    """
    lineage = {"fetch_id": "-", "run_id": "-", "ingested_at": _PLACEHOLDER_AT}
    frame = to_frame(table, [{**r, **lineage} for r in rows])
    return frame.drop(columns=list(TABLE_LINEAGE_COLUMNS))


@runtime_checkable
class TableSource(Source, Protocol):
    """A :class:`~deskbrief.sources.base.Source` whose fetches feed ``events`` or ``filings``."""

    def table_for(self, req: FetchRequest) -> TableName | None: ...

    def parse_table(self, raw: RawResponse) -> pd.DataFrame: ...


def is_table_source(source: Source) -> TypeGuard[TableSource]:
    """True when ``source`` implements callable ``table_for`` and ``parse_table``."""
    return callable(getattr(source, "table_for", None)) and callable(
        getattr(source, "parse_table", None)
    )


def table_of(source: Source, req: FetchRequest) -> TableName | None:
    """The table ``req``'s rows go to, or ``None`` for an observation fetch."""
    if not is_table_source(source):
        return None
    return source.table_for(req)


@dataclass(frozen=True)
class TableDataset:
    """A dataset that feeds ``events`` or ``filings``, for the freshness check."""

    table: TableName
    source: str
    dataset: str


#: Dataset names (also the ``source_log`` and ``events``/``filings`` dataset values).
FISCALDATA_AUCTIONS = TableDataset("events", "fiscaldata", "upcoming_auctions")
FRED_RELEASE_DATES = TableDataset("events", "fred", "release_dates")
EDGAR_COMPANY = TableDataset("filings", "edgar", "company")
EDGAR_HOLDERS = TableDataset("filings", "edgar", "holders")
TABLE_DATASETS: tuple[TableDataset, ...] = (
    FISCALDATA_AUCTIONS,
    FRED_RELEASE_DATES,
    EDGAR_COMPANY,
    EDGAR_HOLDERS,
)


def planned_datasets(config: Config) -> list[TableDataset]:
    """The table datasets this config fetches: the source is configured and enabled, and its
    config section asks for something (FRED release dates need ``events.fred_releases``; EDGAR
    needs ``filings.companies``; its holders dataset needs ``filings.holders`` and a company
    with a ``cusip``, since 13F tables name securities by CUSIP)."""
    wanted = {
        FISCALDATA_AUCTIONS: True,
        FRED_RELEASE_DATES: bool(config.events.fred_releases),
        EDGAR_COMPANY: bool(config.filings.companies),
        EDGAR_HOLDERS: bool(
            config.filings.holders and any(c.cusip for c in config.filings.companies)
        ),
    }
    out: list[TableDataset] = []
    for ds in TABLE_DATASETS:
        settings = config.sources.get(ds.source)
        if settings is not None and settings.enabled and wanted[ds]:
            out.append(ds)
    return out
