"""HYPOTHETICAL paper portfolio: load, value, reconcile (spec §7.2, §8.1 check type 9).

The portfolio is invented for the project: ``holdings.csv`` and ``broker_statement.csv`` at the
repo root hold no real account data and each starts with a ``# HYPOTHETICAL`` comment line, which
the loaders insist on. Every output carries the label: :class:`Valuation.label`, the account names
and the ``label`` key in each reconciliation result's ``details``.

Valuation reads the ``daily_close`` view (tier 3 > 2 > 1) for the target ET date only, so a stale
price is never used silently: no close that day means a null price (``Valuation.missing``).

Not wired in yet (needs files this card does not own): see the T41 PR, "Risks and follow-ups".
:class:`ReconciliationCheck` implements the ``Check`` protocol but is deliberately not registered.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from deskbrief.config import Config
from deskbrief.models import CheckType, DQResult, DQStatus, RunContext

HYPOTHETICAL = "HYPOTHETICAL"
HOLDINGS_COLUMNS: tuple[str, ...] = (
    "account",
    "instrument_id",
    "quantity",
    "cost_basis",
    "currency",
)
#: spec §7.2 ``positions_valued`` columns, in order
POSITIONS_VALUED_COLUMNS: tuple[str, ...] = (
    "date", "instrument_id", "quantity", "price", "price_source", "price_tier", "market_value",
    "day_pnl",
)  # fmt: skip
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOLDINGS = REPO_ROOT / "holdings.csv"
DEFAULT_BROKER_STATEMENT = REPO_ROOT / "broker_statement.csv"
#: quantity must match exactly (up to float noise); market value to one cent
QTY_TOLERANCE = 1e-9
MV_TOLERANCE_USD = 0.01
_CURRENCY = "USD"


class PortfolioError(ValueError):
    """A holdings / broker statement file is unreadable or invalid."""


def _load(path: Path, config: Config | None) -> pd.DataFrame:
    text = Path(path).read_text(encoding="utf-8")
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if not (first.startswith("#") and HYPOTHETICAL in first):
        raise PortfolioError(f"{path}: first line must be a '# {HYPOTHETICAL} ...' comment")
    df = pd.read_csv(path, comment="#", dtype=str, keep_default_na=False)
    if tuple(df.columns) != HOLDINGS_COLUMNS:
        raise PortfolioError(
            f"{path}: columns must be {list(HOLDINGS_COLUMNS)}, got {list(df.columns)}"
        )
    for col in ("account", "instrument_id"):
        if (df[col].str.strip() == "").any():
            raise PortfolioError(f"{path}: empty {col}")
    for col in ("quantity", "cost_basis"):
        num = pd.to_numeric(df[col], errors="coerce")
        if num.isna().any() or (num < 0).any():
            raise PortfolioError(f"{path}: {col} must be a non-negative number")
        df[col] = num.astype(float)
    if (df["currency"] != _CURRENCY).any():
        raise PortfolioError(f"{path}: currency must be {_CURRENCY}")
    if df.duplicated(["account", "instrument_id"]).any():
        raise PortfolioError(f"{path}: duplicate (account, instrument_id) rows")
    if config is not None:
        known = {i.id for i in config.instruments}
        unknown = sorted(set(df["instrument_id"]) - known)
        if unknown:
            raise PortfolioError(f"{path}: unknown instrument {unknown}")
    return df.reset_index(drop=True)


def load_holdings(path: Path = DEFAULT_HOLDINGS, *, config: Config | None = None) -> pd.DataFrame:
    """Validated ``holdings.csv`` (columns ``HOLDINGS_COLUMNS``)."""
    return _load(path, config)


def load_broker_statement(
    path: Path = DEFAULT_BROKER_STATEMENT, *, config: Config | None = None
) -> pd.DataFrame:
    """Validated ``broker_statement.csv`` (same shape as holdings)."""
    return _load(path, config)


@dataclass(frozen=True)
class Valuation:
    """``positions_valued`` rows plus what a reader needs to interpret them."""

    rows: pd.DataFrame  # exactly POSITIONS_VALUED_COLUMNS
    missing: tuple[str, ...]  # instruments with no close on the date
    label: str = HYPOTHETICAL

    @property
    def total_market_value(self) -> float:
        """Sum over priced positions only; missing prices are excluded, never zero-filled."""
        return float(self.rows["market_value"].sum(skipna=True))


def value_positions(holdings: pd.DataFrame, duck: duckdb.DuckDBPyConnection, on: date) -> Valuation:
    """Value ``holdings`` (summed across accounts) at ``daily_close`` for ET date ``on``."""
    qty = holdings.groupby("instrument_id", sort=True)["quantity"].sum()
    ids = [str(i) for i in qty.index]
    closes = (
        duck.execute(
            'SELECT instrument_id, "date", close, tier, chosen_source FROM daily_close '
            'WHERE instrument_id = ANY(?) AND "date" <= ?',
            [ids, on],
        ).df()
        if ids
        else pd.DataFrame(columns=["instrument_id", "date", "close", "tier", "chosen_source"])
    )
    closes["date"] = pd.to_datetime(closes["date"]).dt.date
    out: list[dict[str, Any]] = []
    missing: list[str] = []
    for inst in ids:
        q = float(qty[inst])
        hist = closes[closes["instrument_id"] == inst].sort_values("date")
        today = hist[hist["date"] == on]
        prev = hist[hist["date"] < on]
        if today.empty:
            missing.append(inst)
            out.append({"date": on, "instrument_id": inst, "quantity": q, "price": None,
                        "price_source": None, "price_tier": None, "market_value": None,
                        "day_pnl": None})  # fmt: skip
            continue
        px = float(today.iloc[-1]["close"])
        pnl = None if prev.empty else q * (px - float(prev.iloc[-1]["close"]))
        out.append({"date": on, "instrument_id": inst, "quantity": q, "price": px,
                    "price_source": str(today.iloc[-1]["chosen_source"]),
                    "price_tier": int(today.iloc[-1]["tier"]), "market_value": q * px,
                    "day_pnl": pnl})  # fmt: skip
    rows = pd.DataFrame(out, columns=list(POSITIONS_VALUED_COLUMNS))
    for col in ("price", "market_value", "day_pnl", "price_tier"):
        rows[col] = pd.to_numeric(rows[col], errors="coerce")
    return Valuation(rows=rows, missing=tuple(missing))


def _qty_map(df: pd.DataFrame) -> dict[tuple[str, str], float]:
    return {
        (str(a), str(i)): float(q)
        for a, i, q in zip(df["account"], df["instrument_id"], df["quantity"], strict=True)
    }


def reconcile(
    ctx: RunContext, holdings: pd.DataFrame, broker: pd.DataFrame, valuation: Valuation
) -> list[DQResult]:
    """Holdings vs broker statement per (account, instrument), as ``dq_results`` rows.

    ``reconcile_quantity`` fails unless quantities agree; ``reconcile_market_value`` values both
    quantities at the same ``daily_close`` price and fails if they differ by more than a cent
    (``skip`` when there is no price). A position on one side only compares against zero.
    """
    h, b = _qty_map(holdings), _qty_map(broker)
    prices = valuation.rows.set_index("instrument_id")["price"]
    out: list[DQResult] = []

    def emit(name: str, status: DQStatus, acct: str, inst: str, observed: float | None,
             threshold: float, **details: Any) -> None:  # fmt: skip
        out.append(DQResult(
            run_id=ctx.run_id, check_name=name, check_type=CheckType.RECONCILIATION,
            target_date=ctx.target_date, evaluated_at=ctx.now_utc, status=status,
            source="paper_portfolio", instrument_id=inst, observed=observed, threshold=threshold,
            details={"label": HYPOTHETICAL, "account": acct, **details},
        ))  # fmt: skip

    for acct, inst in sorted(set(h) | set(b)):
        hq, bq = h.get((acct, inst), 0.0), b.get((acct, inst), 0.0)
        diff = abs(hq - bq)
        emit("reconcile_quantity", DQStatus.FAIL if diff > QTY_TOLERANCE else DQStatus.PASS,
             acct, inst, diff, 0.0, holdings_quantity=hq, broker_quantity=bq)  # fmt: skip
        px = prices.get(inst)
        if px is None or pd.isna(px):
            emit("reconcile_market_value", DQStatus.SKIP, acct, inst, None, MV_TOLERANCE_USD,
                 reason="no price", holdings_quantity=hq, broker_quantity=bq)  # fmt: skip
            continue
        hv, bv = hq * float(px), bq * float(px)
        mv_diff = abs(hv - bv)
        emit("reconcile_market_value",
             DQStatus.FAIL if mv_diff > MV_TOLERANCE_USD else DQStatus.PASS, acct, inst,
             mv_diff, MV_TOLERANCE_USD, price=float(px), holdings_market_value=hv,
             broker_market_value=bv)  # fmt: skip
    return out


class ReconciliationCheck:
    """Check type 9. Implements the ``Check`` protocol; not registered (see module docstring)."""

    name = "reconcile_paper_portfolio"
    check_type = CheckType.RECONCILIATION

    def __init__(
        self,
        holdings_path: Path = DEFAULT_HOLDINGS,
        broker_path: Path = DEFAULT_BROKER_STATEMENT,
    ) -> None:
        self.holdings_path = holdings_path
        self.broker_path = broker_path

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        try:
            h = load_holdings(self.holdings_path, config=ctx.config)
            b = load_broker_statement(self.broker_path, config=ctx.config)
        except (PortfolioError, OSError) as exc:
            return [
                DQResult(
                    run_id=ctx.run_id, check_name="reconcile_inputs",
                    check_type=self.check_type, target_date=ctx.target_date,
                    evaluated_at=ctx.now_utc, status=DQStatus.FAIL, source="paper_portfolio",
                    details={"label": HYPOTHETICAL, "error": str(exc)},
                )
            ]  # fmt: skip
        return reconcile(ctx, h, b, value_positions(h, duck, ctx.target_date))


def summary_lines(valuation: Valuation) -> Iterable[str]:
    """Human-readable lines for report/dashboard use; always labelled."""
    yield f"{HYPOTHETICAL} paper portfolio (synthetic, not a real account)"
    rows = valuation.rows
    for inst, qty, mv in zip(
        rows["instrument_id"], rows["quantity"], rows["market_value"], strict=True
    ):
        value = "no price" if pd.isna(mv) else f"{float(mv):,.2f} USD"
        yield f"{inst}: {float(qty):g} = {value}"
