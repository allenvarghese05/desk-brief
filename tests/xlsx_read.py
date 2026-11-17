"""Minimal .xlsx reader for tests (importable as ``xlsx_read``); standard library only.

openpyxl is not a dependency, so tests read the OOXML parts directly: sheet names, Excel table
definitions (name, ref, headers) and cell values with their number-format codes.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


@dataclass(frozen=True)
class Cell:
    value: str | float | None
    num_format: str  # "General" when the cell has no custom format


@dataclass(frozen=True)
class Table:
    name: str
    sheet: str
    ref: str
    headers: tuple[str, ...]


class Workbook:
    def __init__(self, path: Path) -> None:
        self._zip = zipfile.ZipFile(path)
        self.sheet_names: list[str] = []
        self._sheet_parts: dict[str, str] = {}
        rels = {
            r.get("Id"): r.get("Target", "")
            for r in self._xml("xl/_rels/workbook.xml.rels").findall("p:Relationship", _NS)
        }
        for s in self._xml("xl/workbook.xml").findall("m:sheets/m:sheet", _NS):
            name = s.get("name", "")
            self.sheet_names.append(name)
            self._sheet_parts[name] = "xl/" + rels[s.get(_R_ID)].removeprefix("/xl/")
        self._strings = [
            "".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t"))
            for si in self._xml("xl/sharedStrings.xml").findall("m:si", _NS)
        ]
        codes = {
            n.get("numFmtId"): n.get("formatCode", "")
            for n in self._xml("xl/styles.xml").findall("m:numFmts/m:numFmt", _NS)
        }
        self._xf_formats = [
            codes.get(xf.get("numFmtId"), "General")
            for xf in self._xml("xl/styles.xml").findall("m:cellXfs/m:xf", _NS)
        ]
        self.tables = self._read_tables()

    def _xml(self, part: str) -> ET.Element:
        return ET.fromstring(self._zip.read(part))

    def _read_tables(self) -> dict[str, Table]:
        out: dict[str, Table] = {}
        for sheet, part in self._sheet_parts.items():
            rels_part = part.replace("worksheets/", "worksheets/_rels/") + ".rels"
            if rels_part not in self._zip.namelist():
                continue
            for rel in self._xml(rels_part).findall("p:Relationship", _NS):
                target = rel.get("Target", "")
                if "/tables/" not in target:
                    continue
                t = self._xml("xl/tables/" + target.rsplit("/", 1)[1])
                name = t.get("name", "")
                assert name == t.get("displayName")
                cols = tuple(
                    c.get("name", "") for c in t.findall("m:tableColumns/m:tableColumn", _NS)
                )
                out[name] = Table(name, sheet, t.get("ref", ""), cols)
        return out

    def cells(self, sheet: str) -> dict[str, Cell]:
        """Cell reference (``"A1"``) to value and number format, empty cells omitted."""
        out: dict[str, Cell] = {}
        for c in self._xml(self._sheet_parts[sheet]).iter(f"{{{_NS['m']}}}c"):
            v = c.find("m:v", _NS)
            fmt = self._xf_formats[int(c.get("s", "0"))]
            if c.get("t") == "s" and v is not None and v.text is not None:
                out[c.get("r", "")] = Cell(self._strings[int(v.text)], fmt)
            elif c.get("t") == "inlineStr":
                out[c.get("r", "")] = Cell(
                    "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t")), fmt
                )
            elif v is not None and v.text is not None:
                out[c.get("r", "")] = Cell(float(v.text), fmt)
        return out

    def table_rows(self, name: str) -> list[dict[str, Cell]]:
        """Data rows of a table as ``{header: Cell}`` (blank cells are ``Cell(None, ...)``)."""
        t = self.tables[name]
        cells = self.cells(t.sheet)
        m = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", t.ref)
        assert m
        first, last = int(m.group(2)), int(m.group(4))
        letters = [_col_letter(_col_index(m.group(1)) + i) for i in range(len(t.headers))]
        return [
            {
                h: cells.get(f"{col}{r}", Cell(None, "General"))
                for h, col in zip(t.headers, letters, strict=True)
            }
            for r in range(first + 1, last + 1)
        ]


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s
