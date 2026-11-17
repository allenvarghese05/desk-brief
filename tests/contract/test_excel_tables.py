"""Contract: workbook sheet names, Excel Table names and column headers (spec §9.2).

The VBA modules (T33) address these names, so this file pins them as literals: a change here is
a contract change (plan "Contracts" rule), never a refactor. SYNTHETIC FIXTURE: the workbook is
built from ``brief_synth`` (made-up values) under pytest's ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import xlsx_read
from brief_synth import MON, MON_0700, World, load, monday_rows
from deskbrief.report.excel import PLACEHOLDER_LABEL, PLACEHOLDER_STATUS, write_workbook
from deskbrief.report.model import BriefModel

# spec §9.2, verbatim
SHEETS = [
    "Brief", "Rates", "FX", "Commodities", "Equities", "Global",
    "Checks", "Sources", "Lineage", "About",
]  # fmt: skip
TABLE_SHEET = {
    "tbl_rates": "Rates",
    "tbl_fx": "FX",
    "tbl_commod": "Commodities",
    "tbl_equity": "Equities",
    "tbl_global": "Global",
    "tbl_checks": "Checks",
    "tbl_sources": "Sources",
    "tbl_lineage": "Lineage",
}
MARKET_HEADERS = (
    "instrument_id", "asset_class", "name", "label", "field", "value", "unit", "change",
    "change_unit", "basis", "status", "as_of", "tier", "tier_name", "proxy", "qualifiers",
    "description", "dq_flag", "as_of_utc",
)  # fmt: skip
HEADERS = {
    **dict.fromkeys(
        ("tbl_rates", "tbl_fx", "tbl_commod", "tbl_equity", "tbl_global"), MARKET_HEADERS
    ),
    "tbl_checks": (
        "label", "kind", "status", "check_type", "source", "instrument_id", "group_id",
        "observed", "threshold", "ref_date",
    ),
    "tbl_sources": ("label", "status", "n_fetches", "n_failed"),
    "tbl_lineage": (
        "label", "field", "role", "instrument_id", "value", "unit", "as_of_et", "source",
        "dataset", "symbol", "tier", "is_fallback", "fetch_id", "run_id", "requested_at_et",
        "latency_ms", "http_status", "fetch_status", "raw_key", "rows_parsed",
    ),
}  # fmt: skip


@pytest.fixture(params=["populated", "empty"])
def book(request: pytest.FixtureRequest, tmp_path: Path) -> xlsx_read.Workbook:
    """Both a full day and an empty store must keep the contract (empty tables stay tables)."""
    w = World(tmp_path / "w")
    if request.param == "populated":
        w.observations(monday_rows())
        w.dq(MON, [("fresh", "warn", "yahoo", "ES_F")])
        w.fetches(MON, [("yahoo", "ok")])
    model = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    return xlsx_read.Workbook(write_workbook(model, tmp_path / "brief.xlsx"))


def test_sheets_exact_and_in_order(book: xlsx_read.Workbook) -> None:
    assert book.sheet_names == SHEETS


def test_table_names_and_sheets_exact(book: xlsx_read.Workbook) -> None:
    assert {n: t.sheet for n, t in book.tables.items()} == TABLE_SHEET


def test_table_headers_exact(book: xlsx_read.Workbook) -> None:
    assert {n: t.headers for n, t in book.tables.items()} == HEADERS


def test_every_data_table_has_a_label_column_and_market_tables_a_field_column(
    book: xlsx_read.Workbook,
) -> None:
    """P-009: values are always labelled; nothing is implicitly a "close"."""
    for name, headers in HEADERS.items():
        assert "label" in headers, name
    for name in ("tbl_rates", "tbl_fx", "tbl_commod", "tbl_equity", "tbl_global", "tbl_lineage"):
        assert "field" in HEADERS[name], name


MARKET_TABLES = ("tbl_rates", "tbl_fx", "tbl_commod", "tbl_equity", "tbl_global")


def test_new_market_columns_come_last_in_this_order() -> None:
    """P-015: ``dq_flag`` then ``as_of_utc`` appended; every earlier position is unchanged."""
    assert MARKET_HEADERS[-2:] == ("dq_flag", "as_of_utc")
    assert MARKET_HEADERS.index("description") == 16


def test_dq_flag_vocabulary_is_exactly_fail_warn_pass_none(tmp_path: Path) -> None:
    """The VBA filters on these four strings; ``skip`` never appears (it is no result)."""
    w = World(tmp_path / "w")
    w.observations(monday_rows())
    w.dq(
        MON,
        [
            ("a", "pass", "yahoo", "UST_10Y"),
            ("b", "warn", "yahoo", "ES_F"),
            ("c", "fail", "ecb", "EURUSD"),
            ("d", "skip", "yahoo", "WTI"),
        ],
    )
    model = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    book = xlsx_read.Workbook(write_workbook(model, tmp_path / "brief.xlsx"))
    flags = {
        r["instrument_id"].value: r["dq_flag"].value
        for t in MARKET_TABLES
        for r in book.table_rows(t)
    }
    assert flags["UST_10Y"] == "pass"
    assert flags["ES_F"] == "warn"
    assert flags["EURUSD"] == "fail"
    assert flags["WTI"] == "none"  # only a skip: no result
    assert set(flags.values()) == {"fail", "warn", "pass", "none"}


def test_placeholder_label_constant_and_status_empty(tmp_path: Path) -> None:
    """Macros skip the placeholder row by its ``status``; the label is exported, not retyped."""
    assert PLACEHOLDER_LABEL == "none: nothing to list"
    assert PLACEHOLDER_STATUS == "empty"
    model = BriefModel.from_store(World(tmp_path / "e").connect(), load(), MON, MON_0700)
    book = xlsx_read.Workbook(write_workbook(model, tmp_path / "brief.xlsx"))
    for name in ("tbl_checks", "tbl_lineage"):  # tbl_sources lists every enabled source
        (row,) = book.table_rows(name)
        assert row["label"].value == PLACEHOLDER_LABEL, name
        if "status" in row:
            assert row["status"].value == "empty", name
