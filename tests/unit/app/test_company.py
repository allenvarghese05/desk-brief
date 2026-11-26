# ruff: noqa: RUF001  (a true minus sign is intentional output)
"""The Company section for single stocks (P-024, card T67).

SYNTHETIC FIXTURE: every filing, trade and holding below is invented. "Example Corp (EXMP)",
its CIK 0009999999, "Jane Example" and "Invented Asset Management" are fictional; the URL is a
made-up EDGAR path. No real company's filings appear anywhere.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from test_home import Run, page

from brief_synth import MON, MON_0700, World
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.query import company_filings_from_frame
from deskbrief.schemas import to_frame
from deskbrief.store.datasets import Datasets

EXMP_CIK = "0009999999"
URL = "https://www.sec.gov/Archives/edgar/data/9999999/000999999926000001/"
INGESTED = datetime(2026, 11, 16, 11, 0, tzinfo=UTC)  # Monday 06:00 ET


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "instrument_id": "EXMP", "cik": EXMP_CIK, "accession": "0009999999-26-000001",
        "seq": 0, "row_kind": "filing", "form": "10-Q", "filed_date": date(2026, 11, 4),
        "period": date(2026, 9, 30), "url": URL, "plan_10b5_1": False, "details_json": "{}",
        "source": "edgar", "dataset": "submissions", "fetch_id": "F1", "run_id": "R1",
        "ingested_at": INGESTED,
    }  # fmt: skip
    row.update(over)
    return row


def _trade(seq: int, side: str, shares: float, plan: bool) -> dict[str, Any]:
    return _row(
        accession=f"0009999999-26-0000{10 + seq}", seq=1, row_kind="insider_trade", form="4",
        filed_date=date(2026, 10, 20), period=None, party="Jane Example",
        party_cik="0008888888", party_role="Officer", trade_date=date(2026, 10, 16),
        trade_code="S" if side == "sell" else "P", side=side, shares=shares, price=100.0,
        value_usd=shares * 100.0, plan_10b5_1=plan, dataset="form4",
    )  # fmt: skip


def _holder(party: str, cik: str, change: float) -> dict[str, Any]:
    return _row(
        accession=f"{cik}-26-000003", seq=1, row_kind="holder_change", form="13F-HR",
        filed_date=date(2026, 8, 14), period=date(2026, 6, 30), party=party, party_cik=cik,
        shares=50000.0, shares_change=change, value_usd=None, dataset="13f",
    )  # fmt: skip


def filings() -> pd.DataFrame:
    rows = [
        _row(),
        _trade(1, "sell", 1000.0, True),
        _trade(2, "sell", 500.0, False),
        _trade(3, "buy", 200.0, False),
        _holder("Invented Asset Management", "0007777777", -12000.0),
        _holder("Example Pension Fund", "0006666666", 3000.0),
    ]
    return to_frame("filings", rows)


def test_the_section_shows_each_fact_with_its_source_and_date() -> None:
    from app.components import company

    co = company_filings_from_frame(filings(), "EXMP", MON_0700, 90)
    html = company.section_html(co, 90)
    assert '<div class="db-label db-co-label">Company<span>SEC EDGAR filings</span></div>' in html
    assert "10-Q, 4 Nov 2026" in html
    assert "SEC EDGAR · filed 4 Nov 2026 · period 30 Sep 2026" in html
    assert f'href="{URL}" target="_blank" rel="noopener noreferrer"' in html
    assert '<span class="db-down">2 sells</span> · <span class="db-up">1 buys</span>' in html
    assert "Form 4 · net −$130.0K · 1 of 3 under 10b5-1 plans · filed since 18 Aug 2026" in html
    assert "1 added · 1 cut" in html
    assert "13F · as of 30 Jun 2026, 13F runs about 45 days behind" in html
    assert "Invented Asset Management" in html
    assert " title=" not in html


def test_an_empty_filings_table_says_no_filings_stored_yet() -> None:
    from app.components import company

    co = company_filings_from_frame(to_frame("filings", []), "EXMP", MON_0700, 90)
    assert "No filings stored yet" in company.section_html(co, 90)
    assert "No filings stored yet" in company.section_html(None, 90)


def test_marker_is_the_latest_filing_date() -> None:
    from app.components import company

    co = company_filings_from_frame(filings(), "EXMP", MON_0700, 90)
    assert company.marker(co) == (date(2026, 11, 4), "10-Q filed")
    assert company.marker(None) is None


def test_the_data_layer_reads_recent_filings_from_the_store(tmp_path: Path) -> None:
    from app.components import data

    w = World(tmp_path)
    ctx = RunContext(
        run_id="R1", job=JobKind.MORNING, trigger=Trigger.MANUAL, target_date=MON,
        now_utc=INGESTED, git_sha="x", config_hash="x", config=w.config, store=w.store,
    )  # fmt: skip
    Datasets(w.store).append("filings", filings(), ctx, MON)
    config = w.config.model_copy(
        update={"storage": w.config.storage.model_copy(update={"local_root": str(w.store.root)})}
    )
    co = data.build_company(config, {}, MON_0700, "EXMP")
    assert co.latest_filing is not None
    assert co.latest_filing.form == "10-Q"
    later = data.build_company(config, {}, datetime(2026, 12, 31, 12, tzinfo=UTC), "EXMP")
    assert later.latest_filing is None  # older than FILINGS_READ_DAYS of fetches: not read


def test_an_etf_shows_no_company_section(run_home: Run) -> None:
    at = run_home("hosted")
    at.query_params["id"] = "SPY"
    text = page(at.run())
    assert '<span class="db-ch-name">SPY</span>' in text
    assert "db-co-label" not in text


def test_a_stock_with_nothing_stored_says_so(run_home: Run) -> None:
    from brief_synth import FRI, daily

    rows = [daily("AAPL", FRI, 200.0, source="yahoo", dataset="history", tier=2)]
    at = run_home("hosted", rows=rows)
    at.query_params["id"] = "AAPL"
    text = page(at.run())
    assert "db-co-label" in text
    assert "No filings stored yet" in text


def test_the_filing_marker_snaps_to_the_first_bar_on_or_after_the_filed_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.components import data
    from app.views import markets

    from deskbrief.report.model import Row

    co = company_filings_from_frame(filings(), "EXMP", MON_0700, 90)
    monkeypatch.setattr(data, "load_company", lambda config, iid: co)
    monkeypatch.setattr(markets, "_is_stock", lambda config, iid: True)
    row = Row.__new__(Row)
    object.__setattr__(row, "instrument_id", "EXMP")
    view = pd.DataFrame({"d": pd.to_datetime(["2026-11-03", "2026-11-05", "2026-11-06"])})
    assert markets._filing_marker(None, row, view) == [  # type: ignore[arg-type]
        {"time": "2026-11-05", "text": "10-Q filed"}
    ]
    early = pd.DataFrame({"d": pd.to_datetime(["2026-10-01", "2026-10-02"])})
    assert markets._filing_marker(None, row, early) == []  # type: ignore[arg-type]


def test_tvchart_payload_carries_markers_only_when_given() -> None:
    from app.components import tvchart

    base = tvchart.spec([{"kind": "line", "points": [], "meta": []}])
    assert "markers" not in base
    marked = tvchart.spec(
        [{"kind": "line", "points": [], "meta": []}],
        markers=[{"time": "2026-11-05", "text": "10-Q filed"}],
    )
    assert marked["markers"] == [{"time": "2026-11-05", "text": "10-Q filed"}]
    assert marked["sig"] != base["sig"]
    js = (Path(tvchart.__file__).parent / "tvchart_frontend" / "tvchart.js").read_text("utf-8")
    assert "LWC.createSeriesMarkers(drawn[0].api" in js


def test_company_stylesheet_is_flat(flat_css: Any) -> None:
    from app.components import company

    assert flat_css(company.COMPANY_CSS) == []
    assert not re.search(r"title=", company.COMPANY_CSS)
