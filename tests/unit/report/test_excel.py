"""Excel writer: cell values, number formats, labels, empty tables, Brief and About sheets.

SYNTHETIC FIXTURE: all data comes from ``brief_synth`` (made-up values); workbooks are written
under pytest's ``tmp_path``. Names and headers are pinned in ``tests/contract/test_excel_tables``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import xlsx_read
from brief_synth import FRI, MON, MON_0700, THU, World, daily, et, monday_rows, tick
from deskbrief.report.excel import write_workbook
from deskbrief.report.model import BriefModel
from xlsx_read import Cell

YIELD, BP, PRICE, FX = '0.00"%"', "+0;-0", "#,##0.00", "0.0000"


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path / "w")
    w.observations(monday_rows())
    return w


def write(w: World, tmp_path: Path) -> xlsx_read.Workbook:
    model = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    return xlsx_read.Workbook(write_workbook(model, tmp_path / "brief.xlsx"))


@pytest.fixture
def book(world: World, tmp_path: Path) -> xlsx_read.Workbook:
    world.dq(MON, [("fresh", "warn", "yahoo", "ES_F")])
    world.fetches(MON, [("yahoo", "ok"), ("stooq", "error")])
    world.log_fetch("F-LIN", MON_0700, "raw/syn/1.json")
    return write(world, tmp_path)


def find(book: xlsx_read.Workbook, table: str, iid: str) -> dict[str, Cell]:
    return next(r for r in book.table_rows(table) if r["instrument_id"].value == iid)


def test_yield_value_and_change_formats(book: xlsx_read.Workbook) -> None:
    r = find(book, "tbl_rates", "UST_10Y")
    assert (r["value"].value, r["value"].num_format) == (4.37, YIELD)
    assert (r["change"].value, r["change"].num_format) == (pytest.approx(-3.0), BP)
    assert r["change_unit"].value == "bp"
    assert r["unit"].value == "pct"
    assert r["tier_name"].value == "delayed"
    assert "official treasury baseline" in str(r["description"].value)


def test_fx_price_and_percent_formats(book: xlsx_read.Workbook) -> None:
    assert find(book, "tbl_fx", "EURUSD")["value"].num_format == FX
    es = find(book, "tbl_equity", "ES_F")
    assert (es["value"].value, es["value"].num_format) == (5976.0, PRICE)
    assert es["change_unit"].value == "pct"
    assert es["change"].num_format == '+0.0"%";-0.0"%"'
    assert es["qualifiers"].value == "delayed quote, 1 min old"


def test_labels_name_the_field_and_never_call_a_fix_or_yield_a_close(
    book: xlsx_read.Workbook,
) -> None:
    eur = find(book, "tbl_fx", "EURUSD")
    assert eur["field"].value == "rate"
    assert "ECB reference rate" in str(eur["label"].value)
    assert find(book, "tbl_rates", "UST_2Y")["basis"].value == "previous official value"
    for name in ("tbl_fx", "tbl_rates"):
        for row in book.table_rows(name):
            for col in ("label", "field", "basis", "description"):
                assert "close" not in str(row[col].value).lower(), (name, col)
    assert find(book, "tbl_equity", "SPY")["basis"].value == "previous NYSE close"


def test_spread_row_is_numeric_in_bp_and_labelled_derived(book: xlsx_read.Workbook) -> None:
    r = find(book, "tbl_rates", "UST_2S10S")
    assert (r["value"].value, r["value"].num_format) == (22.0, BP)  # 4.40 - 4.18 on Fri 13 Nov
    assert (r["change"].value, r["change_unit"].value) == (-3.0, "bp")
    assert r["unit"].value == "bp"
    assert r["field"].value == "spread"
    assert "derived" in str(r["label"].value)
    assert r["as_of"].value == "13 Nov"


def test_unavailable_row_has_blank_value_and_says_so(book: xlsx_read.Workbook) -> None:
    r = find(book, "tbl_equity", "QQQ")
    assert r["value"].value is None
    assert r["status"].value == "unavailable"
    assert "data unavailable" in str(r["description"].value)


def test_proxy_column_and_global_holds_index_vol_and_macro(book: xlsx_read.Workbook) -> None:
    assert find(book, "tbl_equity", "SPY")["proxy"].value == "proxy"
    assert find(book, "tbl_equity", "ES_F")["proxy"].value is None
    classes = {r["asset_class"].value for r in book.table_rows("tbl_global")}
    assert classes == {"index", "vol", "macro"}
    assert find(book, "tbl_global", "VIX")["value"].value == 16.2
    assert {r["asset_class"].value for r in book.table_rows("tbl_commod")} == {"commodity"}


def test_checks_sources_and_lineage_tables(book: xlsx_read.Workbook) -> None:
    (check,) = book.table_rows("tbl_checks")
    assert (check["label"].value, check["kind"].value, check["status"].value) == (
        "fresh",
        "dq",
        "warn",
    )
    assert (check["observed"].value, check["threshold"].value) == (1.5, 1.0)
    sources = {r["label"].value: r["status"].value for r in book.table_rows("tbl_sources")}
    assert sources["yahoo"] == "ok"
    assert sources["stooq"] == "failed"
    lin = [r for r in book.table_rows("tbl_lineage") if r["instrument_id"].value == "ES_F"]
    assert [(r["role"].value, r["value"].value) for r in lin] == [
        ("value", 5976.0),
        ("baseline", 6000.0),
    ]
    assert lin[0]["as_of_et"].num_format == "yyyy-mm-dd hh:mm:ss"
    assert lin[0]["field"].value == "last"
    assert lin[1]["field"].value == "close"


def test_empty_store_keeps_every_table_with_one_placeholder_row(
    tmp_path: Path,
) -> None:
    book = write(World(tmp_path / "empty"), tmp_path)
    assert len(book.table_rows("tbl_checks")) == 1
    assert str(book.table_rows("tbl_checks")[0]["label"].value).startswith("none")
    assert len(book.table_rows("tbl_lineage")) == 1
    assert book.table_rows("tbl_rates")[0]["status"].value == "unavailable"


def test_brief_sheet_has_title_headline_health_and_index_links(book: xlsx_read.Workbook) -> None:
    text = {c.value for c in book.cells("Brief").values()}
    assert "Desk Brief — Mon 16 Nov 2026, 07:00 ET" in text
    assert any(str(t).startswith("Overnight: ") for t in text)
    assert any("check" in str(t) for t in text)
    assert {"Rates", "FX", "Commodities", "Equities", "Global", "Checks", "Sources"} <= text
    assert {"Lineage", "About"} <= text


def test_about_explains_close_vs_yield_vs_reference_rate_and_tiers(
    book: xlsx_read.Workbook,
) -> None:
    text = "\n".join(str(c.value) for c in book.cells("About").values())
    for needle in ("close", "yield", "reference rate", "last", "derived", "●", "◐", "○"):
        assert needle in text, needle
    assert "proxy" in text
    assert "Not investment advice" in text
    assert "Proxies, not the underlying" in text  # known limits come from the model


def test_as_of_utc_is_iso_text_and_dq_flag_is_the_models(book: xlsx_read.Workbook) -> None:
    es = find(book, "tbl_equity", "ES_F")
    assert es["as_of_utc"].value == "2026-11-16T11:59:00Z"  # 06:59 ET tick
    assert es["as_of_utc"].num_format == "General"  # text, so VBA reads it the same everywhere
    assert es["dq_flag"].value == "warn"
    assert find(book, "tbl_rates", "UST_10Y")["dq_flag"].value == "none"
    assert find(book, "tbl_rates", "UST_10Y")["as_of_utc"].value == "2026-11-16T11:58:00Z"


def test_unavailable_row_has_blank_as_of_utc_and_none_flag(book: xlsx_read.Workbook) -> None:
    r = find(book, "tbl_equity", "QQQ")
    assert (r["as_of_utc"].value, r["dq_flag"].value) == (None, "none")


def test_negative_spread_is_typed_negative_bp(tmp_path: Path) -> None:
    """Inverted curve: the typed value is -18, never parsed from the minus-sign text."""
    tsy = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
    w = World(tmp_path / "w")
    w.observations(
        [
            daily("UST_10Y", THU, 4.10, **tsy),
            daily("UST_10Y", FRI, 4.00, **tsy),
            daily("UST_2Y", THU, 4.20, **tsy),
            daily("UST_2Y", FRI, 4.18, **tsy),
        ]
    )
    r = find(write(w, tmp_path), "tbl_rates", "UST_2S10S")
    assert (r["value"].value, r["value"].num_format) == (-18.0, BP)  # 4.00 - 4.18
    assert (r["change"].value, r["change"].num_format) == (pytest.approx(-8.0), BP)  # from -10
    assert (r["unit"].value, r["change_unit"].value) == ("bp", "bp")


def test_qualifiers_iex_and_fallback_and_lineage_is_fallback(tmp_path: Path) -> None:
    w = World(tmp_path / "w")
    w.observations(monday_rows())
    w.observations(
        [tick("BRENT", et(MON, "06:58"), 75.0, source="stooq", dataset="daily", is_fallback=True)]
    )
    book = write(w, tmp_path)
    assert find(book, "tbl_equity", "SPY")["qualifiers"].value == (
        "IEX: single exchange, not the consolidated tape"
    )
    brent = find(book, "tbl_commod", "BRENT")
    assert "fallback source: stooq" in str(brent["qualifiers"].value)
    flags = {
        r["instrument_id"].value: r["is_fallback"].value for r in book.table_rows("tbl_lineage")
    }
    assert flags["BRENT"] == "fallback"
    assert flags["SPY"] is None


def test_notable_lines_reach_the_brief_sheet(tmp_path: Path) -> None:
    w = World(tmp_path / "w")
    w.observations(monday_rows())
    w.xchecks(MON, [("spy_vs_es", "fail", 40.0)], mirror=True)
    model = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    assert any(n.kind == "crosscheck" for n in model.notable)
    book = xlsx_read.Workbook(write_workbook(model, tmp_path / "brief.xlsx"))
    text = [str(c.value) for c in book.cells("Brief").values()]
    assert "Notable" in text
    for n in model.notable:
        assert n.text in text
