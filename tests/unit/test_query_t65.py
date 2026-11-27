"""Read helpers for ``events`` and ``filings`` (card T65, for T62/T67).

SYNTHETIC FIXTURE: every frame below is built in code. The company is the invented
"Example Corp (EXMP)" (CIK 0009999999) mapped onto the instrument id ``AAPL`` only; the
insiders, holder, accessions, prices and share counts are made up.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from deskbrief.config import load_config
from deskbrief.models import JobKind, RunContext, Trigger
from deskbrief.query import (
    company_filings,
    company_filings_from_frame,
    current_events,
    events_today,
    holder_period_is_stale,
    latest_amendments_only,
)
from deskbrief.schemas import to_frame
from deskbrief.store.datasets import Datasets
from deskbrief.store.local import LocalBackend

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]
CIK = "0009999999"
R1 = datetime(2026, 11, 25, 11, 20, tzinfo=UTC)  # Wed 06:20 ET
R2 = datetime(2026, 11, 26, 11, 20, tzinfo=UTC)  # Thu 06:20 ET
NOW = datetime(2026, 11, 26, 14, 0, tzinfo=UTC)  # Thu 09:00 ET


def _event(day: int, run: str, at: datetime, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": f"release:10:2026-11-{day:02d}",
        "source": "fred",
        "dataset": "release_dates",
        "kind": "release",
        "title": "Consumer Price Index",
        "event_date": date(2026, 11, day) if day <= 30 else date(2026, 12, day - 30),
        "scheduled_at": datetime(2026, 11, min(day, 30), 8, 30, tzinfo=ET) if day <= 30 else None,
        "details_json": '{"release_id":10}',
        "fetch_id": f"F-{run}",
        "run_id": run,
        "ingested_at": at,
    }
    row.update(kw)
    return row


def _auction(day: int, run: str, at: datetime) -> dict[str, Any]:
    return _event(
        day,
        run,
        at,
        event_id=f"auction:2026-11-{day:02d}:EXMPB0011",
        source="fiscaldata",
        dataset="upcoming_auctions",
        kind="auction",
        title="4-Week Bill auction",
        scheduled_at=None,
        fetch_id=f"A-{run}",
    )


def _log(run: str, at: datetime, source: str, dataset: str, status: str = "ok") -> dict[str, Any]:
    return {
        "fetch_id": f"L-{run}-{source}",
        "run_id": run,
        "source": source,
        "dataset": dataset,
        "symbols": "",
        "request_redacted": "{}",
        "requested_at": at,
        "responded_at": at,
        "latency_ms": 1,
        "http_status": 200,
        "status": status,
        "bytes": 1,
        "rows_parsed": 0,
        "rows_quarantined": 0,
        "attempt": 1,
    }


# --------------------------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------------------------


def test_current_events_sorted_in_window_and_scope_replaced() -> None:
    events = to_frame(
        "events",
        [
            _event(26, "R1", R1),  # replaced by R2's CPI scope
            _event(27, "R2", R2),
            _event(26, "R2", R2, event_id="release:50:2026-11-26", title="Employment Situation",
                   scheduled_at=datetime(2026, 11, 26, 10, 0, tzinfo=ET)),
            _auction(26, "R1", R1),  # fiscaldata not refetched: R1 stays current
            _event(25, "R2", R2, event_id="release:10:2026-11-25"),  # before today
            _event(40, "R2", R2, event_id="release:10:2026-12-10"),  # beyond the horizon
        ],
    )  # fmt: skip
    log = to_frame(
        "source_log",
        [_log("R1", R1, "fred", "release_dates"), _log("R2", R2, "fred", "release_dates")],
    )
    items = current_events(events, log, NOW, horizon_days=7)
    assert [(e.event_date, e.title, e.source) for e in items] == [
        (date(2026, 11, 26), "Employment Situation", "fred"),
        (date(2026, 11, 26), "4-Week Bill auction", "fiscaldata"),  # no time: after timed ones
        (date(2026, 11, 27), "Consumer Price Index", "fred"),
    ]  # R1's CPI on the 26th is gone: R2 replaced the whole (fred, release_dates) scope
    assert items[0].scheduled_at == datetime(2026, 11, 26, 15, 0, tzinfo=UTC)
    assert items[1].scheduled_at is None
    assert items[2].details == {"release_id": 10}


def test_current_events_point_in_time_and_genuine_empty() -> None:
    events = to_frame("events", [_event(27, "R1", R1), _event(28, "R2", R2)])
    # before R2 responded only R1's rows are known
    before = current_events(events, None, R1 + timedelta(hours=1), horizon_days=7)
    assert [e.event_date for e in before] == [date(2026, 11, 27)]
    later = R2 + timedelta(days=1)
    empty = to_frame("source_log", [_log("R3", later, "fred", "release_dates", "empty")])
    assert current_events(events, empty, later + timedelta(hours=1), horizon_days=7) == []


def test_events_today_reads_the_store(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path)
    ds = Datasets(store)
    ctx = RunContext("R2", JobKind.MORNING, Trigger.MANUAL, date(2026, 11, 26), R2, "t", "t",
                     load_config(ROOT / "config.yaml"), store)  # fmt: skip
    ds.append("events", to_frame("events", [_event(27, "R2", R2)]), ctx, date(2026, 11, 26))
    items = events_today(ds, NOW, 7)
    assert [e.title for e in items] == ["Consumer Price Index"]


# --------------------------------------------------------------------------------------------
# filings
# --------------------------------------------------------------------------------------------


def _filing(acc: str, seq: int, kind: str, form: str, filed: date, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "instrument_id": "AAPL",
        "cik": CIK,
        "accession": acc,
        "seq": seq,
        "row_kind": kind,
        "form": form,
        "filed_date": filed,
        "period": None,
        "url": f"https://www.sec.gov/Archives/edgar/data/9999999/{acc.replace('-', '')}/",
        "plan_10b5_1": False,
        "details_json": "{}",
        "source": "edgar",
        "dataset": "company",
        "fetch_id": "F1",
        "run_id": "R2",
        "ingested_at": R2,
    }
    row.update(kw)
    return row


def _trade(acc: str, seq: int, form: str, filed: date, side: str, shares: float,
           price: float | None, plan: bool = False, **kw: Any) -> dict[str, Any]:  # fmt: skip
    code = {"buy": "P", "sell": "S"}.get(side, "A")
    return _filing(
        acc, seq, "insider_trade", form, filed,
        party="Example Jane", party_cik="0008888888", party_role="Officer: CFO",
        trade_date=filed - timedelta(days=2), trade_code=code, side=side, shares=shares,
        price=price, value_usd=shares * price if price is not None else None,
        plan_10b5_1=plan, **kw,
    )  # fmt: skip


def _holder(acc: str, period: date, filed: date, shares: float, change: float,
            party: str = "Invented Asset Management", cik: str = "0007777777",
            **kw: Any) -> list[dict[str, Any]]:  # fmt: skip
    common = {"period": period, "party": party, "party_cik": cik, "dataset": "holders"}
    return [
        _filing(acc, 0, "filing", kw.pop("form", "13F-HR"), filed, **common, **kw),
        _filing(acc, 1, "holder_change", "13F-HR" if "amends_accession" not in kw else "13F-HR/A",
                filed, shares=shares, shares_change=change, value_usd=shares * 50.0, **common),
    ]  # fmt: skip


def _company_frame() -> pd.DataFrame:
    rows = [
        _filing("0009999999-26-000002", 0, "filing", "10-K", date(2026, 2, 20),
                period=date(2025, 12, 31)),
        _filing("0009999999-26-000007", 0, "filing", "10-Q", date(2026, 11, 5),
                period=date(2026, 9, 30)),
        # a sale under a 10b5-1 plan, a tax withholding (other), a grant with no price
        _filing("0009999999-26-000010", 0, "filing", "4", date(2026, 11, 20)),
        _trade("0009999999-26-000010", 1, "4", date(2026, 11, 20), "sell", 1000, 50.0, True),
        _trade("0009999999-26-000010", 2, "4", date(2026, 11, 20), "other", 100, 50.0),
        _filing("0009999999-26-000006", 0, "filing", "4", date(2026, 10, 1)),
        _trade("0009999999-26-000006", 1, "4", date(2026, 10, 1), "other", 5000, None),
        # a purchase, restated twice: only the latest amendment may count
        _filing("0009999999-26-000009", 0, "filing", "4", date(2026, 11, 10)),
        _trade("0009999999-26-000009", 1, "4", date(2026, 11, 10), "buy", 200, 45.0),
        _filing("0009999999-26-000012", 0, "filing", "4/A", date(2026, 11, 12),
                amends_accession="0009999999-26-000009"),
        _trade("0009999999-26-000012", 1, "4/A", date(2026, 11, 12), "buy", 250, 45.0),
        _filing("0009999999-26-000013", 0, "filing", "4/A", date(2026, 11, 14),
                amends_accession="0009999999-26-000009"),
        _trade("0009999999-26-000013", 1, "4/A", date(2026, 11, 14), "buy", 300, 45.0),
        # filed before the 90-day window
        _filing("0009999999-26-000005", 0, "filing", "4", date(2026, 7, 1)),
        _trade("0009999999-26-000005", 1, "4", date(2026, 7, 1), "sell", 700, 40.0),
    ]  # fmt: skip
    rows += _holder("0007777777-26-000002", date(2026, 6, 30), date(2026, 8, 10), 10000, 1000)
    rows += _holder("0007777777-26-000004", date(2026, 9, 30), date(2026, 11, 20), 12500, 2500,
                    form="13F-HR/A", amends_accession="0007777777-26-000003")  # fmt: skip
    rows += _holder("0007777777-26-000003", date(2026, 9, 30), date(2026, 11, 10), 12000, 2000)
    rows += _holder("0006666666-26-000001", date(2026, 9, 30), date(2026, 11, 12), 0, -4000,
                    party="Other Invented Fund", cik="0006666666")  # fmt: skip
    return to_frame("filings", rows)


def test_two_amendments_of_one_original_never_double_count() -> None:
    kept = latest_amendments_only(_company_frame())
    accs = set(kept["accession"])
    assert "0009999999-26-000013" in accs
    assert "0009999999-26-000012" not in accs  # the earlier amendment of the same original
    assert "0009999999-26-000009" in accs  # current_rows, not this helper, drops originals


def test_company_filings_section() -> None:
    cf = company_filings_from_frame(_company_frame(), "AAPL", NOW, insider_days=90)
    assert cf.latest_filing is not None
    assert (cf.latest_filing.form, cf.latest_filing.accession) == ("10-Q", "0009999999-26-000007")
    ins = cf.insiders
    assert ins.since == date(2026, 8, 28)
    purchases = [t for t in ins.trades if t.side == "buy"]
    assert [(t.accession, t.shares) for t in purchases] == [("0009999999-26-000013", 300.0)]
    assert (ins.n_buys, ins.n_sells) == (1, 1)
    assert (ins.buys_usd, ins.sells_usd, ins.net_usd) == (13500.0, 50000.0, -36500.0)
    assert ins.n_10b5_1 == 1
    assert ins.n_unpriced == 0  # the unpriced grant is neither a buy nor a sell
    assert len(ins.trades) == 4  # sale, withholding, grant, the latest restated purchase
    assert cf.holders_period == date(2026, 9, 30)
    assert [(h.party, h.shares, h.shares_change, h.accession) for h in cf.holders] == [
        ("Other Invented Fund", 0.0, -4000.0, "0006666666-26-000001"),
        ("Invented Asset Management", 12500.0, 2500.0, "0007777777-26-000004"),
    ]  # the restated original 000003 is gone; largest change (either way) first


def test_company_filings_point_in_time_and_unknown_instrument() -> None:
    frame = _company_frame()
    assert company_filings_from_frame(frame, "AAPL", R1, 90).latest_filing is None  # not yet
    empty = company_filings_from_frame(frame, "MSFT", NOW, 90)
    assert empty.latest_filing is None
    assert empty.insiders.trades == ()
    assert empty.holders == ()
    assert empty.holders_period is None


def test_company_filings_reads_the_store(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path)
    ds = Datasets(store)
    ctx = RunContext("R2", JobKind.MORNING, Trigger.MANUAL, date(2026, 11, 26), R2, "t", "t",
                     load_config(ROOT / "config.yaml"), store)  # fmt: skip
    ds.append("filings", _company_frame(), ctx, date(2026, 11, 26))
    cf = company_filings(ds, "AAPL", NOW, 90)
    assert cf.latest_filing is not None
    assert cf.holders_period == date(2026, 9, 30)


# --------------------------------------------------------------------------------------------
# PR #102 review batch 4: per-holder 13F periods, unmatched Form 4/A
# --------------------------------------------------------------------------------------------


def test_holders_keep_their_own_latest_quarter_during_the_filing_window() -> None:
    rows: list[dict[str, Any]] = []
    # one holder has filed the new quarter, another not yet: both show, each with its period
    rows += _holder("0007777777-26-000003", date(2026, 9, 30), date(2026, 11, 10), 12000, 2000)
    rows += _holder("0007777777-26-000002", date(2026, 6, 30), date(2026, 8, 10), 10000, 1000)
    rows += _holder("0006666666-26-000001", date(2026, 6, 30), date(2026, 8, 12), 3000, -500,
                    party="Other Invented Fund", cik="0006666666")  # fmt: skip
    # a holder that stopped filing: its last quarter has missed two deadlines, left out
    rows += _holder("0005555555-26-000001", date(2026, 3, 31), date(2026, 5, 12), 900, 100,
                    party="Dormant Invented Fund", cik="0005555555")  # fmt: skip
    cf = company_filings_from_frame(to_frame("filings", rows), "AAPL", NOW, 90)
    assert [(h.party, h.period, h.accession) for h in cf.holders] == [
        ("Invented Asset Management", date(2026, 9, 30), "0007777777-26-000003"),
        ("Other Invented Fund", date(2026, 6, 30), "0006666666-26-000001"),
    ]
    assert cf.holders_period == date(2026, 9, 30)
    assert cf.holders_periods_differ
    assert not company_filings_from_frame(_company_frame(), "AAPL", NOW, 90).holders_periods_differ


def test_holder_staleness_bound() -> None:
    q1 = date(2026, 3, 31)
    # two later quarters (Q2, Q3) are due by 2026-11-14 (Q3 end + 45 days)
    assert not holder_period_is_stale(q1, date(2026, 11, 14))
    assert holder_period_is_stale(q1, date(2026, 11, 15))
    assert not holder_period_is_stale(date(2026, 6, 30), NOW.date())


def test_an_unmatched_form4_amendment_is_counted_alone() -> None:
    rows = [
        # two Form 4s of one owner for one period; a 4/A restates one, the writer cannot tell
        _filing("0009999999-26-000020", 0, "filing", "4", date(2026, 11, 2)),
        _trade("0009999999-26-000020", 1, "4", date(2026, 11, 2), "buy", 100, 40.0),
        _filing("0009999999-26-000021", 0, "filing", "4", date(2026, 11, 2)),
        _trade("0009999999-26-000021", 1, "4", date(2026, 11, 2), "buy", 150, 40.0),
        _filing("0009999999-26-000022", 0, "filing", "4/A", date(2026, 11, 9),
                details_json='{"amendment_unmatched":true,"amends_candidates":'
                             '["0009999999-26-000020","0009999999-26-000021"]}'),
        _trade("0009999999-26-000022", 1, "4/A", date(2026, 11, 9), "buy", 120, 40.0),
    ]  # fmt: skip
    cf = company_filings_from_frame(to_frame("filings", rows), "AAPL", NOW, 90)
    assert [(t.accession, t.shares) for t in cf.insiders.trades] == [
        ("0009999999-26-000022", 120.0)
    ]
    assert cf.insiders.n_buys == 1
    # never counted twice, but a Form 4 nobody amended may be missing: the reader is told
    assert cf.insiders.n_ambiguous == 1
