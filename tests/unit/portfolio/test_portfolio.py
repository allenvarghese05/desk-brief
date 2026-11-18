"""T41 paper portfolio: loading, valuation, reconciliation (spec §7.2, §8.1 check 9).

SYNTHETIC FIXTURE: every holding, price and timestamp below is made up. The portfolio is a
HYPOTHETICAL paper portfolio; no real account is involved.
"""
# ruff: noqa: PT018

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time
from pathlib import Path

import pandas as pd
import pytest

from deskbrief.config import Config, load_config
from deskbrief.models import CheckType, DQStatus, JobKind, RunContext, Trigger
from deskbrief.portfolio import (
    HYPOTHETICAL,
    POSITIONS_VALUED_COLUMNS,
    PortfolioError,
    ReconciliationCheck,
    load_broker_statement,
    load_holdings,
    reconcile,
    value_positions,
)
from deskbrief.schemas import to_frame
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import ET, et_date

REPO_ROOT = Path(__file__).resolve().parents[3]
D = date(2026, 11, 13)
PREV = date(2026, 11, 12)
HEADER = "account,instrument_id,quantity,cost_basis,currency\n"


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(REPO_ROOT / "config.yaml")


def _csv(tmp_path: Path, name: str, body: str, comment: bool = True) -> Path:
    p = tmp_path / name
    p.write_text(("# HYPOTHETICAL synthetic test fixture\n" if comment else "") + HEADER + body)
    return p


def _obs(
    inst: str, d: date, value: float, tier: int = 3, source: str = "stooq"
) -> dict[str, object]:
    return {
        "instrument_id": inst, "source": source, "dataset": "daily", "symbol": inst.lower(),
        "tier": tier, "field": "close", "interval": "1d",
        "as_of": datetime.combine(d, time(0), tzinfo=ET).astimezone(UTC), "value": value,
        "unit": "USD", "is_derived": False, "is_fallback": False, "fetch_id": f"F{inst}{d}",
        "run_id": "R1", "ingested_at": datetime(2026, 11, 13, 22, 0, tzinfo=UTC),
        "parser_version": "t1",
    }  # fmt: skip


def _ctx(config: Config, store: LocalBackend) -> RunContext:
    return RunContext(
        run_id="R1", job=JobKind.EOD, trigger=Trigger.MANUAL, target_date=D,
        now_utc=datetime(2026, 11, 13, 22, 30, tzinfo=UTC), git_sha="x", config_hash="y",
        config=config, store=store,
    )  # fmt: skip


@pytest.fixture
def duck_ctx(config: Config, tmp_path: Path):  # type: ignore[no-untyped-def]
    store = LocalBackend(tmp_path / "data")
    ctx = _ctx(config, store)
    rows = [
        _obs("AAPL", PREV, 100.0), _obs("AAPL", D, 110.0),
        _obs("MSFT", PREV, 200.0), _obs("MSFT", D, 198.0),
        _obs("NVDA", PREV, 50.0, tier=1, source="alpaca"),  # prev close from tier 1 only
        # NVDA has no close on D -> missing price
    ]  # fmt: skip
    for part in (PREV, D):
        chunk = [r for r in rows if et_date(r["as_of"]) == part]  # type: ignore[arg-type]
        Datasets(store).append("observations", to_frame("observations", chunk), replace(ctx), part)
    return ctx, connect(store, config)


# --- loading -------------------------------------------------------------------------------


def test_shipped_csvs_are_hypothetical_and_load() -> None:
    for path in ("holdings.csv", "broker_statement.csv"):
        first = (REPO_ROOT / path).read_text().splitlines()[0]
        assert first.startswith("#") and "HYPOTHETICAL" in first
    h = load_holdings(REPO_ROOT / "holdings.csv")
    b = load_broker_statement(REPO_ROOT / "broker_statement.csv")
    assert set(h["instrument_id"]) == {"AAPL", "MSFT", "NVDA"}
    assert list(h.columns) == ["account", "instrument_id", "quantity", "cost_basis", "currency"]
    assert h["account"].str.contains(HYPOTHETICAL).all()
    assert len(b) == len(h)


def test_shipped_files_reconcile_by_default() -> None:
    h = load_holdings(REPO_ROOT / "holdings.csv")
    b = load_broker_statement(REPO_ROOT / "broker_statement.csv")
    assert (h.drop(columns="account").values == b.drop(columns="account").values).all()


def test_load_requires_hypothetical_marker(tmp_path: Path) -> None:
    p = _csv(tmp_path, "h.csv", "PAPER,AAPL,1,100,USD\n", comment=False)
    with pytest.raises(PortfolioError, match="HYPOTHETICAL"):
        load_holdings(p)


@pytest.mark.parametrize(
    ("body", "msg"),
    [
        ("PAPER,AAPL,-1,100,USD\n", "quantity"),
        ("PAPER,AAPL,abc,100,USD\n", "quantity"),
        ("PAPER,AAPL,1,-5,USD\n", "cost_basis"),
        ("PAPER,AAPL,1,100,EUR\n", "currency"),
        ("PAPER,ZZZZ,1,100,USD\n", "unknown instrument"),
        ("PAPER,AAPL,1,100,USD\nPAPER,AAPL,2,100,USD\n", "duplicate"),
        ("PAPER,,1,100,USD\n", "instrument_id"),
    ],
)
def test_load_rejects_bad_rows(tmp_path: Path, config: Config, body: str, msg: str) -> None:
    with pytest.raises(PortfolioError, match=msg):
        load_holdings(_csv(tmp_path, "h.csv", body), config=config)


def test_load_rejects_wrong_columns(tmp_path: Path) -> None:
    p = tmp_path / "h.csv"
    p.write_text("# HYPOTHETICAL\naccount,instrument_id,qty\nPAPER,AAPL,1\n")
    with pytest.raises(PortfolioError, match="columns"):
        load_holdings(p)


# --- valuation -----------------------------------------------------------------------------


def test_value_positions(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    _, duck = duck_ctx
    h = load_holdings(_csv(tmp_path, "h.csv", "PAPER,AAPL,10,900,USD\nPAPER,MSFT,5,900,USD\n"))
    v = value_positions(h, duck, D)
    assert v.label == HYPOTHETICAL
    assert list(v.rows.columns) == list(POSITIONS_VALUED_COLUMNS)
    a = v.rows.set_index("instrument_id").loc["AAPL"]
    assert (a["price"], a["market_value"], a["day_pnl"]) == (110.0, 1100.0, 100.0)
    assert (a["price_source"], a["price_tier"]) == ("stooq", 3)
    m = v.rows.set_index("instrument_id").loc["MSFT"]
    assert m["market_value"] == 990.0 and m["day_pnl"] == pytest.approx(-10.0)
    assert v.rows["date"].eq(D).all()
    assert v.missing == ()


def test_missing_price_is_null_and_reported(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    _, duck = duck_ctx
    h = load_holdings(_csv(tmp_path, "h.csv", "PAPER,AAPL,10,900,USD\nPAPER,NVDA,3,100,USD\n"))
    v = value_positions(h, duck, D)
    n = v.rows.set_index("instrument_id").loc["NVDA"]
    assert pd.isna(n["price"]) and pd.isna(n["market_value"]) and pd.isna(n["day_pnl"])
    assert pd.isna(n["price_source"]) and pd.isna(n["price_tier"])
    assert n["quantity"] == 3
    assert v.missing == ("NVDA",)
    assert v.total_market_value == 1100.0  # missing excluded, never zero-filled silently


def test_no_prev_close_gives_null_pnl(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    _, duck = duck_ctx
    h = load_holdings(_csv(tmp_path, "h.csv", "PAPER,AAPL,10,900,USD\n"))
    v = value_positions(h, duck, PREV)  # nothing before PREV
    r = v.rows.iloc[0]
    assert r["market_value"] == 1000.0 and pd.isna(r["day_pnl"])


def test_value_aggregates_accounts(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    _, duck = duck_ctx
    h = load_holdings(_csv(tmp_path, "h.csv", "A1,AAPL,10,900,USD\nA2,AAPL,5,400,USD\n"))
    v = value_positions(h, duck, D)
    assert len(v.rows) == 1 and v.rows.iloc[0]["quantity"] == 15


# --- reconciliation ------------------------------------------------------------------------


def _frames(tmp_path: Path, hold: str, broker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    return (
        load_holdings(_csv(tmp_path, "h.csv", hold)),
        load_broker_statement(_csv(tmp_path, "b.csv", broker)),
    )


def _rec(ctx: RunContext, duck, h, b):  # type: ignore[no-untyped-def]
    return reconcile(ctx, h, b, value_positions(h, duck, ctx.target_date))


def test_reconcile_pass(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    ctx, duck = duck_ctx
    body = "PAPER,AAPL,10,900,USD\nPAPER,MSFT,5,900,USD\n"
    res = _rec(ctx, duck, *_frames(tmp_path, body, body))
    assert {r.check_name for r in res} == {"reconcile_quantity", "reconcile_market_value"}
    assert len(res) == 4
    assert all(r.status is DQStatus.PASS for r in res)
    assert all(r.check_type is CheckType.RECONCILIATION for r in res)
    assert all(json.loads(r.details_json())["label"] == HYPOTHETICAL for r in res)
    assert {r.instrument_id for r in res} == {"AAPL", "MSFT"}


def test_reconcile_quantity_mismatch_fails_both(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    ctx, duck = duck_ctx
    res = _rec(
        ctx, duck,
        *_frames(tmp_path, "PAPER,AAPL,10,900,USD\n", "PAPER,AAPL,9,900,USD\n"),
    )  # fmt: skip
    q = next(r for r in res if r.check_name == "reconcile_quantity")
    mv = next(r for r in res if r.check_name == "reconcile_market_value")
    assert q.status is DQStatus.FAIL and q.observed == 1.0 and q.threshold == 0.0
    assert mv.status is DQStatus.FAIL and mv.observed == pytest.approx(110.0)
    d = json.loads(mv.details_json())
    assert d["holdings_market_value"] == 1100.0 and d["broker_market_value"] == 990.0


def test_reconcile_market_value_tolerance(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    ctx, duck = duck_ctx
    h, b = _frames(tmp_path, "PAPER,AAPL,10,900,USD\n", "PAPER,AAPL,10.00001,900,USD\n")
    res = _rec(ctx, duck, h, b)  # 0.0011 USD apart
    q = next(r for r in res if r.check_name == "reconcile_quantity")
    mv = next(r for r in res if r.check_name == "reconcile_market_value")
    assert q.status is DQStatus.FAIL  # quantity is exact
    assert mv.status is DQStatus.PASS  # below one cent


def test_reconcile_missing_on_one_side_fails(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    ctx, duck = duck_ctx
    res = _rec(
        ctx, duck,
        *_frames(
            tmp_path,
            "PAPER,AAPL,10,900,USD\nPAPER,MSFT,5,900,USD\n",
            "PAPER,AAPL,10,900,USD\n",
        ),
    )  # fmt: skip
    m = [r for r in res if r.instrument_id == "MSFT"]
    assert {r.status for r in m} == {DQStatus.FAIL}
    assert json.loads(m[0].details_json())["broker_quantity"] == 0.0


def test_reconcile_missing_price_skips_market_value(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    ctx, duck = duck_ctx
    body = "PAPER,NVDA,3,100,USD\n"
    res = _rec(ctx, duck, *_frames(tmp_path, body, body))
    q = next(r for r in res if r.check_name == "reconcile_quantity")
    mv = next(r for r in res if r.check_name == "reconcile_market_value")
    assert q.status is DQStatus.PASS
    assert mv.status is DQStatus.SKIP
    assert json.loads(mv.details_json())["reason"] == "no price"


def test_reconcile_account_level_mismatch_not_netted(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    ctx, duck = duck_ctx
    # Same instrument total (15) but split differently across accounts: must fail.
    res = _rec(
        ctx, duck,
        *_frames(
            tmp_path,
            "A1,AAPL,10,900,USD\nA2,AAPL,5,400,USD\n",
            "A1,AAPL,5,900,USD\nA2,AAPL,10,400,USD\n",
        ),
    )  # fmt: skip
    assert any(r.status is DQStatus.FAIL for r in res)


def test_reconciliation_check_class(tmp_path: Path, duck_ctx, config: Config) -> None:  # type: ignore[no-untyped-def]
    ctx, duck = duck_ctx
    body = "PAPER,AAPL,10,900,USD\n"
    chk = ReconciliationCheck(_csv(tmp_path, "h.csv", body), _csv(tmp_path, "b.csv", body))
    assert chk.check_type is CheckType.RECONCILIATION
    assert all(r.status is DQStatus.PASS for r in chk.evaluate(ctx, duck))


def test_reconciliation_check_unreadable_files_fail(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    ctx, duck = duck_ctx
    res = ReconciliationCheck(tmp_path / "nope.csv", tmp_path / "nope2.csv").evaluate(ctx, duck)
    assert len(res) == 1 and res[0].status is DQStatus.FAIL
    assert json.loads(res[0].details_json())["label"] == HYPOTHETICAL


def test_summary_lines_are_labelled(tmp_path: Path, duck_ctx) -> None:  # type: ignore[no-untyped-def]
    from deskbrief.portfolio import summary_lines

    _, duck = duck_ctx
    h = load_holdings(_csv(tmp_path, "h.csv", "PAPER,AAPL,10,900,USD\nPAPER,NVDA,3,100,USD\n"))
    lines = list(summary_lines(value_positions(h, duck, D)))
    assert HYPOTHETICAL in lines[0]
    assert lines[1].startswith("AAPL") and "1,100.00 USD" in lines[1]
    assert "no price" in lines[2]
