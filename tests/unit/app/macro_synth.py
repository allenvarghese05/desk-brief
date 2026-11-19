"""Synthetic rows for the Macro page tests and the local preview (T54).

SYNTHETIC FIXTURE: every value below is made up (seeded random walks), not provider data.
Daily series end on Friday 13 Nov 2026, the last completed day of the standard scenario in
``brief_synth`` (Monday 16 Nov 2026, 07:00 ET); CFTC reports are weekly, as of Tuesdays.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

from brief_synth import FRI, daily, et, obs

#: a daily series: (instrument, start, step size, tier, source, dataset, field, unit)
_DAILY = (
    ("VIX9D", 14.0, 0.6, 3, "cboe", "vix_history", "close", "index_pts"),
    ("VIX", 16.0, 0.5, 3, "cboe", "vix_history", "close", "index_pts"),
    ("VIX3M", 18.0, 0.35, 3, "cboe", "vix_history", "close", "index_pts"),
    ("VIX6M", 19.5, 0.25, 3, "cboe", "vix_history", "close", "index_pts"),
    ("UST_3M", 4.30, 0.01, 3, "treasury", "par_curve", "yield", "pct"),
    ("UST_2Y", 4.10, 0.03, 3, "treasury", "par_curve", "yield", "pct"),
    ("UST_10Y", 4.30, 0.03, 3, "treasury", "par_curve", "yield", "pct"),
    ("UST_20Y", 4.60, 0.03, 3, "treasury", "par_curve", "yield", "pct"),
    ("UST_30Y", 4.65, 0.03, 3, "treasury", "par_curve", "yield", "pct"),
    ("SPY", 560.0, 4.0, 3, "stooq", "daily", "close", "USD"),
    ("QQQ", 480.0, 4.0, 3, "stooq", "daily", "close", "USD"),
    ("USD_BROAD", 121.0, 0.3, 3, "fred", "series", "level", "index_pts"),
    ("WTI", 72.0, 0.9, 2, "yahoo", "history", "close", "USD"),
    ("HY_OAS", 3.10, 0.03, 3, "fred", "series", "rate", "pct"),
)
COT_NET = {"COT_GC_NET": 240_000, "COT_ES_NET": -60_000, "COT_ZN_NET": -700_000}
COT_OI = {"COT_GC_OI": 520_000, "COT_ES_OI": 2_200_000, "COT_ZN_OI": 4_300_000}


def business_days(n: int, end: date = FRI) -> list[date]:
    """The last ``n`` weekdays up to ``end``, oldest first."""
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def tuesdays(n: int, end: date = FRI) -> list[date]:
    d = end - timedelta(days=(end.weekday() - 1) % 7)
    return [d - timedelta(weeks=k) for k in range(n)][::-1]


def macro_rows(days: int = 280, weeks: int = 30, seed: int = 7) -> list[dict[str, Any]]:
    """Daily macro series over ``days`` weekdays and ``weeks`` CFTC reports."""
    rng = random.Random(seed)
    dates = business_days(days)
    rows: list[dict[str, Any]] = []
    for iid, start, step, tier, source, dataset, field, unit in _DAILY:
        v = start
        for d in dates:
            v = max(v + rng.gauss(0, step), step)  # stays positive
            rows.append(
                daily(
                    iid,
                    d,
                    round(v, 4),
                    tier=tier,
                    source=source,
                    dataset=dataset,
                    field=field,
                    unit=unit,
                )
            )
    gold, real = 2400.0, 1.90
    for d in dates:  # gold moves against the real yield, as it usually does
        shock = rng.gauss(0, 0.03)
        real = real + shock
        gold = gold * (1 - shock * 0.4 + rng.gauss(0, 0.004))
        rows.append(
            daily(
                "UST_10Y_REAL",
                d,
                round(real, 4),
                source="fred",
                dataset="series",
                field="yield",
                unit="pct",
            )
        )
        rows.append(daily("GOLD_F", d, round(gold, 2), tier=2, source="yahoo", dataset="history"))
    for k, d in enumerate(tuesdays(weeks)):
        for iid, base in COT_NET.items():
            rows.append(
                obs(
                    iid,
                    base + 4_000 * min(k, weeks - 2) + (12_000 if k == weeks - 1 else 0),
                    et(d),
                    interval="1w",
                    field="net_position",
                    unit="contracts",
                    source="cftc",
                    dataset="cot",
                )
            )
        for iid, base in COT_OI.items():
            rows.append(
                obs(
                    iid,
                    base + 1_000 * k,
                    et(d),
                    interval="1w",
                    field="open_interest",
                    unit="contracts",
                    source="cftc",
                    dataset="cot",
                )
            )
    return rows


def tier1_spy(days: int = 280) -> list[dict[str, Any]]:
    """Tier-1 daily SPY bars (alpaca), a walk around 777: hosted mode must never read them."""
    rng = random.Random(1)
    out, v = [], 777.0
    for d in business_days(days):
        v += rng.gauss(0, 3.0)
        out.append(daily("SPY", d, round(v, 2), tier=1, source="alpaca", dataset="iex_bars_1m"))
    return out
