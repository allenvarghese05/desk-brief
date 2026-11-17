"""Excel workbook for the morning brief (spec §9.2): one writer for ``.xlsx`` and ``.xlsm``.

Renders a ``BriefModel``; it reads nothing else. Sheet names, Excel Table names and column
headers are a contract with the VBA in T33 (``tests/contract/test_excel_tables.py`` pins them),
so they are constants here, not derived from the model.

Every data table has a ``label`` column, and every market table and the lineage table a ``field``
column (P-009): a value is labelled by what it is (close, last, yield, rate, spread), never called
a "close" by default; the About sheet explains the difference. Values are written as numbers with
the spec's formats (yields ``0.00"%"``, bp ``+0;-0``, prices ``#,##0.00``, FX ``0.0000``); an
unavailable value is a blank cell with ``status = unavailable``. A table with nothing to list
(for example no warn/fail checks) keeps one placeholder row, because Excel needs a data row.

Spec §9.2 has no sheet for Volatility or Macro: those rows go to ``Global`` (``tbl_global``), told
apart by the ``asset_class`` column.
"""
# ruff: noqa: RUF001  (true minus sign is intentional input to the parser below)

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import xlsxwriter

from deskbrief.query import value_label
from deskbrief.report.model import SPREAD_ID, TIER_GLYPH, TIER_NAME, BriefModel, Row
from deskbrief.timeutil import to_et

SHEETS = (
    "Brief", "Rates", "FX", "Commodities", "Equities", "Global",
    "Checks", "Sources", "Lineage", "About",
)  # fmt: skip
#: sheet, table, model asset classes shown there
MARKET_TABLES = (
    ("Rates", "tbl_rates", ("rates",)),
    ("FX", "tbl_fx", ("fx",)),
    ("Commodities", "tbl_commod", ("commodity",)),
    ("Equities", "tbl_equity", ("equity",)),
    ("Global", "tbl_global", ("index", "vol", "macro")),
)
MARKET_HEADERS = (
    "instrument_id", "asset_class", "name", "label", "field", "value", "unit", "change",
    "change_unit", "basis", "status", "as_of", "tier", "tier_name", "proxy", "qualifiers",
    "description",
)  # fmt: skip
CHECKS_HEADERS = (
    "label", "kind", "status", "check_type", "source", "instrument_id", "group_id",
    "observed", "threshold", "ref_date",
)  # fmt: skip
SOURCES_HEADERS = ("label", "status", "n_fetches", "n_failed")
LINEAGE_HEADERS = (
    "label", "field", "role", "instrument_id", "value", "unit", "as_of_et", "source",
    "dataset", "symbol", "tier", "is_fallback", "fetch_id", "run_id", "requested_at_et",
    "latency_ms", "http_status", "fetch_status", "raw_key", "rows_parsed",
)  # fmt: skip

YIELD_FMT, BP_FMT, PRICE_FMT, FX_FMT = '0.00"%"', "+0;-0", "#,##0.00", "0.0000"
_CHANGE_FMT = {
    "bp": BP_FMT,
    "pct": '+0.0"%";-0.0"%"',
    "pts": "+0.0;-0.0",
    "pp": '+0.00"pp";-0.00"pp"',
}
_WIDTHS = {"label": 40, "description": 80, "qualifiers": 44, "name": 28, "raw_key": 36}
_INDEX_NOTES = {
    "Brief": "this sheet: headline, health and index",
    "Rates": "yields, 2s10s spread, rate ETF proxies",
    "FX": "foreign exchange",
    "Commodities": "oil, gold and their proxies",
    "Equities": "equity ETFs, futures and single stocks",
    "Global": "world indices, volatility and macro series",
    "Checks": "data-quality and cross-check warnings and failures",
    "Sources": "per-source fetch health",
    "Lineage": "the fetch behind every value and every baseline",
    "About": "how to read the values, tier legend, known limits",
}
_PLACEHOLDER = "none: nothing to list"


def write_workbook(model: BriefModel, path: Path) -> Path:
    """Write ``model`` as an ``.xlsx`` workbook at ``path`` and return the path."""
    wb = xlsxwriter.Workbook(str(path))
    build(model, wb)
    wb.close()
    return path


def build(model: BriefModel, wb: Any) -> None:
    """Fill an open ``xlsxwriter`` workbook (``.xlsm`` callers add their VBA project first)."""
    sheets = {name: wb.add_worksheet(name) for name in SHEETS}
    w = _Book(wb)
    _brief(w, sheets["Brief"], model)
    rows = model.rows()
    for sheet, table, classes in MARKET_TABLES:
        picked = [r for r in rows if r.asset_class in classes]
        w.table(sheets[sheet], table, MARKET_HEADERS, [_market_row(r) for r in picked])
    s = model.health.summary
    w.table(
        sheets["Checks"],
        "tbl_checks",
        CHECKS_HEADERS,
        [
            [
                *(i.name, i.kind, i.status, i.check_type, i.source, i.instrument_id, i.group_id),
                (i.observed, None),
                (i.threshold, None),
                (i.ref_date, "yyyy-mm-dd"),
            ]
            for i in s.issues
        ],
    )
    w.table(
        sheets["Sources"],
        "tbl_sources",
        SOURCES_HEADERS,
        [[h.source, h.status, h.n_fetches, h.n_failed] for h in s.sources],
    )
    w.table(
        sheets["Lineage"], "tbl_lineage", LINEAGE_HEADERS, [_lineage_row(x) for x in model.lineage]
    )
    _about(w, sheets["About"], model)


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------

Cell = Any  # a plain value, or a ``(value, number_format)`` pair


def _parse_bp(text: str) -> float | None:
    """The derived 2s10s row carries no typed value, only ``+22bp`` or minus-3bp text."""
    if not text.endswith("bp"):
        return None
    return float(text.removesuffix("bp").replace("−", "-"))


def _value_fmt(unit: str, value: float) -> str:
    if unit == "pct":
        return YIELD_FMT
    if unit == "bp":
        return BP_FMT
    if unit == "fx_rate" and abs(value) < 20:
        return FX_FMT
    return PRICE_FMT


def _market_row(r: Row) -> list[Cell]:
    value: float | None
    change: float | None
    unit: str | None
    field: str | None
    change_unit: str | None
    if r.instrument_id == SPREAD_ID and r.available:
        value, unit, field = _parse_bp(r.value_text), "bp", "spread"
        change, change_unit = _parse_bp(r.change_text), "bp"
    elif r.change is not None:
        last = r.change.last
        value, unit, field = last.value, last.unit, last.field
        change, change_unit = r.change.change, r.change.change_unit
    else:
        value = unit = field = change = change_unit = None
    status = r.change.status if r.change else ("ok" if r.available else "unavailable")
    return [
        r.instrument_id,
        r.asset_class,
        r.name,
        r.label,
        field,
        (value, _value_fmt(unit or "", value or 0.0)) if value is not None else None,
        unit,
        (change, _CHANGE_FMT[change_unit or "pct"]) if change is not None else None,
        change_unit if change is not None else None,
        r.basis,
        status,
        r.as_of_text if r.available else None,
        r.tier,
        r.tier_name or None,
        "proxy" if r.is_proxy else None,
        "; ".join(r.qualifiers) or None,
        r.description,
    ]


def _et_naive(t: datetime | None) -> datetime | None:
    return to_et(t).replace(tzinfo=None) if t else None


def _lineage_row(x: Any) -> list[Cell]:
    v = x.value
    stamp = "yyyy-mm-dd hh:mm:ss"
    return [
        value_label(v),
        v.field,
        x.role,
        v.instrument_id,
        (v.value, _value_fmt(v.unit, v.value)),
        v.unit,
        (_et_naive(v.as_of), stamp),
        v.source,
        v.dataset,
        v.symbol,
        v.tier,
        "fallback" if v.is_fallback else None,
        v.fetch_id,
        v.run_id,
        (_et_naive(x.requested_at), stamp),
        x.latency_ms,
        x.http_status,
        x.fetch_status,
        x.raw_key,
        x.rows_parsed,
    ]


# ---------------------------------------------------------------------------
# sheets
# ---------------------------------------------------------------------------


def _brief(w: _Book, ws: Any, m: BriefModel) -> None:
    ws.set_column(0, 0, 18)
    ws.set_column(1, 1, 90)
    ws.write_string(0, 0, m.title, w.fmt(bold=True, font_size=14))
    ws.write_string(1, 0, m.headline.text)
    ws.write_string(2, 0, f"Data health ({m.health.status}): {m.health.text}")
    r = 4
    if m.notable:
        ws.write_string(r, 0, "Notable", w.fmt(bold=True))
        for n in m.notable:
            r += 1
            ws.write_string(r, 0, n.text)
        r += 2
    ws.write_string(r, 0, "Index", w.fmt(bold=True))
    for name in SHEETS:
        r += 1
        ws.write_url(r, 0, f"internal:'{name}'!A1", w.fmt(font_color="blue", underline=1), name)
        ws.write_string(r, 1, _INDEX_NOTES[name])


def _about(w: _Book, ws: Any, m: BriefModel) -> None:
    ws.set_column(0, 0, 22)
    ws.set_column(1, 1, 110)
    lines: list[tuple[str, str]] = [
        ("What the values are", "Each row is labelled by its field; read the label, not the unit."),
        ("close", "Last price of an exchange session. Change is since the previous NYSE close."),
        ("last", "A quote tick, live or delayed. Compared with the previous NYSE close (prices)."),
        ("yield", "A US Treasury par yield, in percent. Change is in bp since the previous "
                  "official value; a Yahoo tick (for example ^TNX) is delayed and not official."),
        ("rate", "An ECB euro foreign exchange reference rate: a daily fix, not a close. Change "
                 "is since the previous official value."),
        ("spread / derived", "Computed here, not published: the 2s10s spread is the official "
                             "10-year minus 2-year yield of one date, in bp."),
        ("tiers", " ".join(f"{TIER_GLYPH[t]} {TIER_NAME[t]}" for t in (1, 3, 2))
                  + ". Tier 1 live or proxy, tier 2 delayed, tier 3 official."),
        ("proxy", "A proxy is an ETF standing in for the underlying, never the index itself."),
        ("empty tables", "A table with nothing to list keeps one placeholder row ('none')."),
        ("report", f"{m.title}; config {m.config_hash[:12]}; model schema {m.schema_version}."),
    ]  # fmt: skip
    lines += [(f"limit: {k.kind}", k.text) for k in m.known_limits]
    lines.append(("disclaimer", "Personal project built on public data. Not investment advice."))
    ws.write_string(0, 0, "About", w.fmt(bold=True, font_size=14))
    for i, (head, text) in enumerate(lines, start=2):
        ws.write_string(i, 0, head, w.fmt(bold=True))
        ws.write_string(i, 1, text, w.fmt(text_wrap=True, valign="top"))


# ---------------------------------------------------------------------------
# xlsxwriter plumbing
# ---------------------------------------------------------------------------


class _Book:
    def __init__(self, wb: Any) -> None:
        self._wb = wb
        self._formats: dict[tuple[tuple[str, Any], ...], Any] = {}

    def fmt(self, num_format: str | None = None, **props: Any) -> Any:
        if num_format:
            props["num_format"] = num_format
        key = tuple(sorted(props.items()))
        if key not in self._formats:
            self._formats[key] = self._wb.add_format(props)
        return self._formats[key]

    def table(
        self, ws: Any, name: str, headers: Sequence[str], rows: Sequence[Sequence[Cell]]
    ) -> None:
        body = list(rows) or [[_PLACEHOLDER] + [None] * (len(headers) - 1)]
        ws.add_table(
            0,
            0,
            len(body),
            len(headers) - 1,
            {"name": name, "columns": [{"header": h} for h in headers]},
        )
        for c, h in enumerate(headers):
            ws.set_column(c, c, _WIDTHS.get(h, 14))
        ws.freeze_panes(1, 0)
        for r, row in enumerate(body, start=1):
            for c, cell in enumerate(row):
                value, num = cell if isinstance(cell, tuple) else (cell, None)
                self._put(ws, r, c, value, num)

    def _put(self, ws: Any, r: int, c: int, value: Any, num: str | None) -> None:
        fmt = self.fmt(num) if num else None
        if value is None:
            return
        if isinstance(value, (datetime, date)):
            ws.write_datetime(r, c, value, fmt)
        elif isinstance(value, str):
            ws.write_string(r, c, value, fmt)
        else:
            ws.write_number(r, c, value, fmt)
