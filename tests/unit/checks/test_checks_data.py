"""Validity, uniqueness, anomaly, revision checks and the runner hook (spec §8.1).

SYNTHETIC FIXTURE: all observations are made up.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest
from checks_support import ET_NOW, as_of_1d, ctx_at, duck_for, find, obs, write_obs

from deskbrief.checks import registered_checks, run_checks
from deskbrief.checks.anomaly import AnomalyCheck, daily_moves
from deskbrief.checks.base import Check, make_result, register
from deskbrief.checks.revision import RevisionCheck
from deskbrief.checks.uniqueness import UniquenessCheck
from deskbrief.checks.validity import ValidityCheck, find_violations
from deskbrief.models import CheckType, DQStatus
from deskbrief.schemas import validate
from deskbrief.store.datasets import Datasets
from deskbrief.timeutil import et_date

S = DQStatus
RANGES = {"pct": (-2.0, 25.0), "USD": (0.01, 1_000_000.0)}


def frame(**over: Any) -> pd.DataFrame:
    rows = over.pop("rows", None) or [over]
    return pd.DataFrame([obs(**r) for r in rows])


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ({"unit": "pct", "field": "yield", "value": 4.25}, None),
        ({"unit": "pct", "field": "yield", "value": 25.0}, None),  # inclusive bounds
        ({"unit": "pct", "field": "yield", "value": -2.0}, None),
        ({"unit": "pct", "field": "yield", "value": 25.01}, "out_of_range"),
        ({"unit": "pct", "field": "yield", "value": -2.01}, "out_of_range"),
        ({"unit": "pct", "field": "yield", "value": 42.5}, "out_of_range"),  # x10 flip
        ({"value": 0.01}, None),
        ({"value": 0.009}, "out_of_range"),
        ({"value": float("nan")}, "non_finite"),
        ({"value": float("inf")}, "non_finite"),
        ({"field": "volume", "unit": "shares", "value": 0.0}, None),
        ({"field": "volume", "unit": "shares", "value": -1.0}, "negative_volume"),
        ({"field": "volume", "unit": "shares", "value": 5e9}, None),  # no range on volume
    ],
)
def test_find_violations_table(row: dict[str, Any], reason: str | None) -> None:
    bad = find_violations(frame(**row), RANGES)
    assert list(bad["reason"]) == ([reason] if reason else [])


def test_high_below_low() -> None:
    base = {"interval": "1m", "as_of": datetime(2026, 11, 17, 15, 0, tzinfo=UTC)}
    ok = frame(
        rows=[{**base, "field": "high", "value": 10.0}, {**base, "field": "low", "value": 9.0}]
    )
    assert find_violations(ok, RANGES).empty
    bad = frame(
        rows=[{**base, "field": "high", "value": 9.0}, {**base, "field": "low", "value": 10.0}]
    )
    assert list(find_violations(bad, RANGES)["reason"]) == ["high_below_low"]


def test_validity_check_flags_unit_flip_and_passes_clean(store, config) -> None:  # type: ignore[no-untyped-def]
    d = date(2026, 11, 17)
    rows = [
        obs(
            instrument_id="UST_10Y",
            source="treasury",
            dataset="par_curve",
            symbol="10 Yr",
            field="yield",
            unit="pct",
            value=42.5,
            as_of=as_of_1d(d),
        ),
        obs(instrument_id="SPY", as_of=as_of_1d(d)),
        obs(instrument_id="QQQ", symbol="qqq.us", as_of=as_of_1d(d), run_id="OTHER"),
    ]
    write_obs(store, config, rows)
    ctx = ctx_at(config, store, ET_NOW(d, "21:00"))
    res = ValidityCheck().evaluate(ctx, duck_for(store, config))
    by = {r.instrument_id: r.status for r in res}
    assert by == {"UST_10Y": S.FAIL, "SPY": S.PASS}  # other run's rows are out of scope
    bad = find(res, instrument_id="UST_10Y")[0]
    assert bad.observed == 1.0
    assert bad.details["reasons"] == {"out_of_range": 1}


def test_validity_and_uniqueness_skip_without_rows(store, config) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_at(config, store, ET_NOW(date(2026, 11, 17), "21:00"))
    duck = duck_for(store, config)
    assert [r.status for r in ValidityCheck().evaluate(ctx, duck)] == [S.SKIP]
    assert [r.status for r in UniquenessCheck().evaluate(ctx, duck)] == [S.SKIP]


def test_uniqueness_duplicate_within_fetch_fails_but_new_fetch_is_a_revision(store, config) -> None:  # type: ignore[no-untyped-def]
    d = date(2026, 11, 17)
    dup = [obs(as_of=as_of_1d(d)), obs(as_of=as_of_1d(d), value=101.0)]  # same key, same fetch
    clean = obs(instrument_id="QQQ", symbol="qqq.us", as_of=as_of_1d(d))
    write_obs(store, config, [*dup, clean])
    ctx = ctx_at(config, store, ET_NOW(d, "21:00"))
    res = UniquenessCheck().evaluate(ctx, duck_for(store, config))
    assert [(r.source, r.status, r.observed) for r in res] == [("stooq", S.FAIL, 1.0)]

    ok = ctx_at(config, store, ET_NOW(d, "22:00"), run_id="R2")
    write_obs(
        store,
        config,
        [
            obs(
                as_of=as_of_1d(d),
                run_id="R2",
                fetch_id="F2",
                value=102.0,
                ingested_at=ET_NOW(d, "21:30"),
            )
        ],
    )
    res2 = UniquenessCheck().evaluate(ok, duck_for(store, config))
    assert [r.status for r in res2] == [S.PASS]  # different fetch: a revision, not a duplicate


# ---------------------------------------------------------------------------
# anomaly
# ---------------------------------------------------------------------------

HIST = [0.004, 0.006, 0.005, 0.007, 0.003, 0.008, 0.004, 0.006, 0.005, 0.007] * 3  # 30 moves


def series_rows(
    last_k: float | None,
    *,
    unit: str = "USD",
    n_hist: int = 30,
    inst: str = "SPY",
    flip: float = 1.0,
) -> tuple[list[dict[str, Any]], date]:
    """Prices whose last |move| sits ``last_k`` standard deviations above the history mean."""
    hist = np.array(HIST[:n_hist])
    sign = np.where(np.arange(len(hist)) % 2 == 0, 1.0, -1.0)
    moves = list(hist * sign)
    if last_k is not None:
        moves.append(hist.mean() + last_k * hist.std(ddof=1))
    start = date(2026, 9, 1)
    value, rows = 100.0, []
    for i in range(len(moves) + 1):
        if i:
            value *= 1 + moves[i - 1]
        d = start + timedelta(days=i)
        v = value * (flip if (last_k is not None and i == len(moves)) else 1.0)
        rows.append(
            obs(
                instrument_id=inst,
                as_of=as_of_1d(d),
                value=v,
                unit=unit,
                ingested_at=datetime(2026, 9, 1, tzinfo=UTC),
                fetch_id="F1",
            )
        )
    return rows, start + timedelta(days=len(moves))


@pytest.mark.parametrize(
    ("k", "status"), [(0.0, S.PASS), (2.9, S.PASS), (3.1, S.WARN), (4.9, S.WARN), (5.1, S.FAIL)]
)
def test_anomaly_z_boundaries(store, config, k, status) -> None:  # type: ignore[no-untyped-def]
    rows, last = series_rows(k)
    write_obs(store, config, rows)
    ctx = ctx_at(config, store, ET_NOW(last + timedelta(days=1), "07:00"))
    res = AnomalyCheck().evaluate(ctx, duck_for(store, config))
    assert [r.status for r in res] == [status]
    assert res[0].observed == pytest.approx(k, abs=0.01)
    assert res[0].details["move_unit"] == "return"


def test_anomaly_skips_short_history_and_zero_variance(store, config) -> None:  # type: ignore[no-untyped-def]
    rows, _ = series_rows(6.0, n_hist=8)
    flat = [
        obs(
            instrument_id="QQQ",
            symbol="qqq.us",
            as_of=as_of_1d(date(2026, 10, 1) + timedelta(days=i)),
            value=50.0,
        )
        for i in range(20)
    ]
    write_obs(store, config, [*rows, *flat])
    ctx = ctx_at(config, store, ET_NOW(date(2026, 11, 20), "07:00"))
    by = {r.instrument_id: r for r in AnomalyCheck().evaluate(ctx, duck_for(store, config))}
    assert by["SPY"].status is S.SKIP
    assert by["SPY"].details["reason"] == "history too short"
    assert by["QQQ"].status is S.SKIP
    assert by["QQQ"].details["reason"] == "zero variance in window"


def test_anomaly_yield_uses_bp_and_x10_flip_fails(store, config) -> None:  # type: ignore[no-untyped-def]
    # a yield random-walking by ~0.5 bp/day then a x10 flip of the last print
    rows = []
    start = date(2026, 9, 1)
    level = 4.2
    steps = [0.01, -0.02, 0.015, -0.01, 0.02, -0.015, 0.01, -0.005] * 4
    for i, step in enumerate(steps):
        level += step
        rows.append(
            obs(
                instrument_id="UST_10Y",
                source="treasury",
                dataset="par_curve",
                symbol="10 Yr",
                field="yield",
                unit="pct",
                value=level,
                as_of=as_of_1d(start + timedelta(days=i)),
                ingested_at=datetime(2026, 9, 1, tzinfo=UTC),
            )
        )
    flipped = rows[-1] | {
        "value": level * 10,
        "as_of": as_of_1d(start + timedelta(days=len(steps))),
    }
    write_obs(store, config, [*rows, flipped])
    ctx = ctx_at(config, store, ET_NOW(start + timedelta(days=len(steps) + 1), "07:00"))
    res = AnomalyCheck().evaluate(ctx, duck_for(store, config))
    assert [r.status for r in res] == [S.FAIL]
    assert res[0].details["move_unit"] == "bp"
    assert res[0].details["move"] == pytest.approx((level * 10 - level) * 100)


def test_anomaly_excludes_current_unfinished_et_date(store, config) -> None:  # type: ignore[no-untyped-def]
    rows, last = series_rows(0.5)
    today = last + timedelta(days=1)
    wild = obs(as_of=as_of_1d(today), value=1000.0)  # today's value must be ignored (P-009)
    write_obs(store, config, [*rows, wild])
    ctx = ctx_at(config, store, ET_NOW(today, "21:00"), target=today)
    res = AnomalyCheck().evaluate(ctx, duck_for(store, config))
    assert [r.status for r in res] == [S.PASS]
    assert res[0].details["as_of_day"] == last.isoformat()


def test_daily_moves() -> None:
    np.testing.assert_allclose(daily_moves(np.array([100.0, 110.0, 99.0]), "USD"), [0.1, 0.1])
    np.testing.assert_allclose(daily_moves(np.array([4.0, 4.05, 4.0]), "pct"), [5.0, 5.0])
    assert np.isnan(daily_moves(np.array([0.0, 1.0]), "USD")[0])


# ---------------------------------------------------------------------------
# revision
# ---------------------------------------------------------------------------


def fred_row(value: float, run: str, fetch: str, ingested: datetime) -> dict[str, Any]:
    return obs(
        instrument_id="UNRATE",
        source="fred",
        dataset="series",
        symbol="UNRATE",
        field="rate",
        interval="1mo",
        unit="pct",
        value=value,
        run_id=run,
        fetch_id=fetch,
        as_of=as_of_1d(date(2026, 10, 1)),
        ingested_at=ingested,
    )


def test_revision_creates_row_and_warn(store, config) -> None:  # type: ignore[no-untyped-def]
    first = fred_row(3.1, "R1", "F1", datetime(2026, 11, 6, 15, tzinfo=UTC))
    second = fred_row(3.2, "R2", "F2", datetime(2026, 11, 20, 15, tzinfo=UTC))
    write_obs(store, config, [first, second])
    now = datetime(2026, 11, 20, 16, tzinfo=UTC)
    ctx = ctx_at(config, store, now, run_id="R2")
    results, revs = RevisionCheck().scan(ctx, duck_for(store, config))
    assert [(r.status, r.instrument_id) for r in results] == [(S.WARN, "UNRATE")]
    assert results[0].details["old_value"] == 3.1
    assert results[0].details["new_value"] == 3.2
    assert len(revs) == 1
    row = revs.iloc[0]
    assert (row["old_value"], row["new_value"]) == (3.1, 3.2)
    assert (row["old_fetch_id"], row["new_fetch_id"]) == ("F1", "F2")
    assert row["dataset"] == "series"
    validate("revisions", revs)
    # the original run sees no revision (its version has no predecessor)
    r1 = ctx_at(config, store, datetime(2026, 11, 20, 16, tzinfo=UTC), run_id="R1")
    res1, revs1 = RevisionCheck().scan(r1, duck_for(store, config))
    assert [r.status for r in res1] == [S.PASS]
    assert revs1.empty
    # a re-ingest with the same value is not a revision
    same = fred_row(3.2, "R3", "F3", datetime(2026, 11, 21, 15, tzinfo=UTC))
    write_obs(store, config, [same])
    r3 = ctx_at(config, store, datetime(2026, 11, 21, 16, tzinfo=UTC), run_id="R3")
    assert [r.status for r in RevisionCheck().evaluate(r3, duck_for(store, config))] == [S.PASS]
    # versions after `now` are invisible
    early = ctx_at(config, store, datetime(2026, 11, 10, tzinfo=UTC), run_id="R2")
    assert RevisionCheck().scan(early, duck_for(store, config))[1].empty


# ---------------------------------------------------------------------------
# registry and runner hook
# ---------------------------------------------------------------------------


def test_registry_has_the_seven_check_types() -> None:
    types = {c.check_type for c in registered_checks()}
    assert types == {
        CheckType.FRESHNESS,
        CheckType.COMPLETENESS,
        CheckType.CALENDAR,
        CheckType.VALIDITY,
        CheckType.UNIQUENESS,
        CheckType.ANOMALY,
        CheckType.REVISION,
    }
    names = [c.name for c in registered_checks()]
    assert len(names) == len(set(names))
    with pytest.raises(ValueError, match="already registered"):
        register(registered_checks()[0])


class _Boom:
    name = "boom"
    check_type = CheckType.VALIDITY

    def evaluate(self, ctx: Any, duck: Any) -> list[Any]:
        raise RuntimeError("kaput")


class _Fixed:
    name = "fixed"
    check_type = CheckType.CALENDAR

    def evaluate(self, ctx: Any, duck: Any) -> list[Any]:
        return [make_result(ctx, self, S.PASS)]


def test_run_checks_isolates_a_failing_check(store, config) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_at(config, store, ET_NOW(date(2026, 11, 17), "21:00"))
    checks: tuple[Check, ...] = (_Boom(), _Fixed())
    dq, revs = run_checks(ctx, duck_for(store, config), checks)
    assert list(dq["check_name"]) == ["boom", "fixed"]
    assert list(dq["status"]) == ["fail", "pass"]
    assert "RuntimeError: kaput" in dq.loc[0, "details_json"]
    assert revs.empty


def test_run_checks_frames_flush_once_per_table_and_date(store, config) -> None:  # type: ignore[no-untyped-def]
    d = date(2026, 11, 17)
    rows = [
        fred_row(3.1, "R1", "F1", datetime(2026, 11, 6, 15, tzinfo=UTC)),
        fred_row(3.2, "R2", "F2", datetime(2026, 11, 17, 15, tzinfo=UTC)),
    ]
    write_obs(store, config, rows)
    ctx = ctx_at(config, store, ET_NOW(d, "21:00"), target=d, run_id="R2")
    dq, revs = run_checks(ctx, duck_for(store, config))
    validate("dq_results", dq)
    assert set(dq["check_type"]) >= {
        "freshness",
        "calendar",
        "completeness",
        "validity",
        "uniqueness",
        "revision",
    }
    assert (dq["target_date"] == d).all()
    datasets = Datasets(store)
    datasets.append("dq_results", dq, ctx, ctx.target_date)
    datasets.append("revisions", revs, ctx, et_date(ctx.now_utc))
    assert len(datasets.read("dq_results")) == len(dq)
    assert len(datasets.read("revisions")) == 1
    assert len(pd.concat([dq]).query("status == 'warn'")) >= 1
