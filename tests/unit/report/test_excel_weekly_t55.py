"""Excel: weekly positions in whole contracts, and the About sheet for the Phase 4B fields (T55).

SYNTHETIC FIXTURE: every row is made up (see ``weekly_synth``). Report: Monday 16 Nov 2026,
07:00 ET. Workbooks are written under pytest's ``tmp_path``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import xlsx_read
from deskbrief.config import Config
from deskbrief.report.excel import write_workbook
from deskbrief.report.model import BriefModel
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_to_utc
from weekly_synth import broad, net, nfci, oi, weekly_config, write
from xlsx_read import Cell

MON = date(2026, 11, 16)
AT = et_to_utc(MON, "07:00")
CONTRACTS_VALUE, CONTRACTS_CHANGE = "#,##0", "+#,##0;-#,##0"


@pytest.fixture(scope="module")
def wconfig() -> Config:
    return weekly_config()


@pytest.fixture
def book(tmp_path: Path, wconfig: Config) -> xlsx_read.Workbook:
    s = LocalBackend(tmp_path / "data")
    thu, fri = date(2026, 11, 12), date(2026, 11, 13)
    tue1, tue2 = date(2026, 11, 3), date(2026, 11, 10)  # CFTC reference days
    write(s, wconfig, [
        broad(thu, 120.0, et_to_utc(thu, "17:00")),
        broad(fri, 121.5, et_to_utc(fri, "17:00")),
        nfci(date(2026, 10, 30), -0.50, et_to_utc(date(2026, 11, 4), "08:35")),
        nfci(date(2026, 11, 6), -0.46, et_to_utc(date(2026, 11, 11), "08:35")),
        net(tue1, 250_000.0, et_to_utc(date(2026, 11, 6), "15:35")),
        net(tue2, 240_000.0, et_to_utc(MON, "06:30")),
        oi(tue1, 500_000.0, et_to_utc(date(2026, 11, 6), "15:35")),
        oi(tue2, 512_000.0, et_to_utc(MON, "06:30")),
    ])  # fmt: skip
    model = BriefModel.from_store(connect(s, wconfig), wconfig, MON, AT)
    return xlsx_read.Workbook(write_workbook(model, tmp_path / "brief.xlsx"))


def find(book: xlsx_read.Workbook, iid: str) -> dict[str, Cell]:
    for table in ("tbl_commod", "tbl_global", "tbl_fx", "tbl_rates", "tbl_equity"):
        for r in book.table_rows(table):
            if r["instrument_id"].value == iid:
                return r
    raise AssertionError(iid)


@pytest.mark.parametrize(
    ("iid", "value", "change"), [("GC_NET", 240_000.0, -10_000.0), ("GC_OI", 512_000.0, 12_000.0)]
)
def test_weekly_position_change_is_whole_contracts_with_separators(
    book: xlsx_read.Workbook, iid: str, value: float, change: float
) -> None:
    r = find(book, iid)
    assert r["unit"].value == "contracts"
    assert (r["value"].value, r["value"].num_format) == (value, CONTRACTS_VALUE)
    assert (r["change"].value, r["change"].num_format) == (pytest.approx(change), CONTRACTS_CHANGE)
    # changing this text is a contract change (P-015); a `contract:` PR may give it "contracts"
    assert r["change_unit"].value == "pts"


def test_index_level_change_keeps_the_points_format(book: xlsx_read.Workbook) -> None:
    r = find(book, "USD_BROAD")
    assert r["change"].num_format == "+0.0;-0.0"
    assert r["change_unit"].value == "pts"


def test_about_explains_the_new_fields_units_and_weekly_dates(book: xlsx_read.Workbook) -> None:
    cells = book.cells("About").values()
    heads = {str(c.value) for c in cells}
    text = "\n".join(str(c.value) for c in cells)
    assert {"level", "net_position", "open_interest", "index", "contracts", "1w"} <= heads
    assert "CFTC" in text
    assert "Tuesday" in text
    assert "reference day" in text
