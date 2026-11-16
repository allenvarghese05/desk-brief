"""Validity: values are finite and in range, high >= low, volume >= 0 (spec §8.1).

Scope: this run's observations (``run_id``) as seen by ``obs_pit(now)``. Ranges per unit come
from ``config.checks.validity`` (inclusive) and apply to every field except ``volume``. One
result per (source, instrument): ``fail`` if any row breaks a rule, else ``pass``;
``observed`` is the number of bad rows, ``threshold`` 0.

A x10 unit flip is only caught by the range when it leaves it (a yield of 42.5 pct is out of
range; a USD price of 1000 is not); the anomaly check covers the rest.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from deskbrief.checks.base import make_result, query, records, register
from deskbrief.models import CheckType, DQResult, DQStatus, RunContext

_HL_KEY = ["instrument_id", "source", "dataset", "interval", "as_of"]


def find_violations(df: pd.DataFrame, ranges: Mapping[Any, tuple[float, float]]) -> pd.DataFrame:
    """Rows of ``df`` that break a rule, with a ``reason`` column (a row can appear twice)."""
    vals = df["value"].astype(float)
    finite = pd.Series(np.isfinite(vals.to_numpy()), index=df.index)
    is_volume = df["field"] == "volume"
    lo = df["unit"].map(lambda u: ranges[u][0] if u in ranges else -np.inf).astype(float)
    hi = df["unit"].map(lambda u: ranges[u][1] if u in ranges else np.inf).astype(float)
    highs = df.loc[df["field"] == "high", [*_HL_KEY, "value"]].reset_index()
    lows = df.loc[df["field"] == "low", [*_HL_KEY, "value"]]
    pairs = highs.merge(lows, on=_HL_KEY, suffixes=("", "_low"))
    inverted = df.index.isin(pairs.loc[pairs["value"] < pairs["value_low"], "index"])
    masks = {
        "non_finite": ~finite,
        "negative_volume": is_volume & finite & (vals < 0),
        "out_of_range": ~is_volume & finite & ((vals < lo) | (vals > hi)),
        "high_below_low": pd.Series(inverted, index=df.index),
    }
    parts = [df[m].assign(reason=reason) for reason, m in masks.items()]
    return pd.concat(parts, ignore_index=True)


class ValidityCheck:
    name = "validity"
    check_type = CheckType.VALIDITY

    def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[DQResult]:
        df = query(duck, "SELECT * FROM obs_pit(?) WHERE run_id = ?", [ctx.now_utc, ctx.run_id])
        if df.empty:
            return [make_result(ctx, self, DQStatus.SKIP, reason="no observations in this run")]
        bad = find_violations(df, ctx.config.checks.validity)
        out: list[DQResult] = []
        for (source, inst), group in df.groupby(["source", "instrument_id"], sort=True):
            mine = bad[(bad["source"] == source) & (bad["instrument_id"] == inst)]
            reasons = {str(k): int(v) for k, v in mine["reason"].value_counts().items()}
            sample = [
                {
                    "field": r["field"],
                    "as_of": str(r["as_of"]),
                    "value": float(r["value"]),
                    "reason": r["reason"],
                }
                for r in records(mine.head(3))
            ]
            out.append(
                make_result(
                    ctx,
                    self,
                    DQStatus.FAIL if len(mine) else DQStatus.PASS,
                    source=str(source),
                    instrument_id=str(inst),
                    observed=float(len(mine)),
                    threshold=0.0,
                    rows=len(group),
                    reasons=reasons,
                    sample=sample,
                )
            )
        return out


register(ValidityCheck())
