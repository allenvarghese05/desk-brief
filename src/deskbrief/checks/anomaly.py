"""Anomaly: z-score of the latest daily move against the previous window (spec §8.1).

Per daily series (instrument, source, dataset, field): the move is the relative return for
prices and the change in basis points for yields/rates (unit ``pct``). ``z = (|move| - mean) /
std`` of ``|move|`` over the ``window_days`` moves before it. ``warn`` at ``z_warn``, ``fail`` at
``z_fail`` (config: 3 and 5; ``fail`` means "verify", not "wrong"). ``skip`` when there are fewer
than ``MIN_MOVES`` earlier moves, or the window has zero variance.

P-009: reads ``obs_pit(now)`` and ignores ET dates on or after today (the unfinished day) and
after the target date, so a run is reproducible.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from deskbrief.checks.base import ET_DATE_SQL, make_result, query, register
from deskbrief.models import CheckType, DQResult, DQStatus, RunContext
from deskbrief.store.duck import DAILY_CLOSE_FIELDS
from deskbrief.timeutil import et_date

MIN_MOVES = 10  # earlier moves needed before a z-score means anything
_SERIES = ["instrument_id", "source", "dataset", "field", "unit"]


def daily_moves(values: np.ndarray, unit: str) -> np.ndarray:
    """Absolute daily moves: |delta| in bp for yields/rates (``pct``), |return| otherwise."""
    if unit == "pct":
        return np.abs(np.diff(values) * 100.0)
    prev = values[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.abs(np.where(prev != 0, values[1:] / prev - 1.0, np.nan))


class AnomalyCheck:
    name = "anomaly"
    check_type = CheckType.ANOMALY

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        cfg = ctx.config.checks.anomaly
        cutoff = min(ctx.target_date, et_date(ctx.now_utc) - timedelta(days=1))
        fields = ", ".join(f"'{f}'" for f in DAILY_CLOSE_FIELDS)
        df: pd.DataFrame = query(
            duck,
            f"SELECT instrument_id, source, dataset, field, unit, {ET_DATE_SQL} AS d, value "  # noqa: S608
            f"FROM obs_pit(?) WHERE interval = '1d' AND field IN ({fields}) "
            f"AND {ET_DATE_SQL} <= ? ORDER BY instrument_id, source, dataset, field, d",
            [ctx.now_utc, cutoff],
        )
        out: list[DQResult] = []
        for (inst, source, dataset, field, unit), g in df.groupby(_SERIES, sort=True):
            moves = daily_moves(g["value"].to_numpy(dtype=float), str(unit))
            hist = moves[-(cfg.window_days + 1) : -1]
            hist = hist[np.isfinite(hist)]
            last = float(moves[-1]) if len(moves) else float("nan")
            info: dict[str, Any] = {
                "dataset": str(dataset),
                "field": str(field),
                "as_of_day": str(g["d"].iloc[-1])[:10],
                "move_unit": "bp" if unit == "pct" else "return",
                "move": last,
                "history_moves": len(hist),
            }
            std = float(hist.std(ddof=1)) if len(hist) > 1 else 0.0
            who: dict[str, Any] = {"source": str(source), "instrument_id": str(inst)}
            if len(hist) < MIN_MOVES or not np.isfinite(last):
                out.append(
                    make_result(ctx, self, DQStatus.SKIP, reason="history too short", **who, **info)
                )
            elif std < 1e-12:
                out.append(
                    make_result(
                        ctx, self, DQStatus.SKIP, reason="zero variance in window", **who, **info
                    )
                )
            else:
                z = (last - float(hist.mean())) / std
                status = (
                    DQStatus.FAIL
                    if z >= cfg.z_fail
                    else DQStatus.WARN
                    if z >= cfg.z_warn
                    else DQStatus.PASS
                )
                thr = cfg.z_fail if status is DQStatus.FAIL else cfg.z_warn
                out.append(make_result(ctx, self, status, observed=z, threshold=thr, **who, **info))
        return out


register(AnomalyCheck())
