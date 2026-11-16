"""Cross-check framework: bps math, status thresholds, reference date, registry, runner.

SYNTHETIC FIXTURE: all data is made up.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import duckdb
import pytest
from xc_fakes import (
    DAY_FRI,
    INGESTED,
    NOW,
    duck_for,
    fred,
    make_ctx,
    put,
    treasury,
)

from deskbrief.config import Config
from deskbrief.crosschecks import base
from deskbrief.crosschecks.base import (
    abs_level_bps,
    level_diff_bps,
    reference_date,
    rel_bps,
    return_diff_bps,
    run_crosschecks,
    simple_return,
    status_for,
)
from deskbrief.models import CheckType, DQStatus, RunContext, XResult
from deskbrief.schemas import validate
from deskbrief.store.datasets import Datasets
from deskbrief.store.local import LocalBackend

# ---------------------------------------------------------------------------
# bps math
# ---------------------------------------------------------------------------


def test_yield_level_is_absolute_bp() -> None:
    assert abs_level_bps(4.26, 4.25) == pytest.approx(1.0)
    assert abs_level_bps(4.25, 4.26) == pytest.approx(-1.0)


def test_price_is_relative_bp() -> None:
    assert rel_bps(100.05, 100.0) == pytest.approx(5.0)
    assert rel_bps(99.0, 100.0) == pytest.approx(-100.0)


def test_same_numbers_differ_by_unit_basis() -> None:
    # a 0.01 move: 1 bp as a yield level, but ~23.5 bp relative on a 4.25 price-like value
    y, y_basis = level_diff_bps(4.26, 4.25, "pct")
    p, p_basis = level_diff_bps(4.26, 4.25, "USD")
    assert y == pytest.approx(1.0)
    assert p == pytest.approx(0.01 / 4.25 * 1e4)
    assert (y_basis, p_basis) == ("absolute_pct_points", "relative")
    # FX and index points are relative too
    assert level_diff_bps(1.0850, 1.0845, "fx_rate")[1] == "relative"
    assert level_diff_bps(15.1, 15.0, "index_pts")[1] == "relative"


def test_relative_bp_rejects_zero_reference() -> None:
    with pytest.raises(ZeroDivisionError):
        rel_bps(1.0, 0.0)


def test_return_helpers() -> None:
    assert simple_return(100.0, 101.0) == pytest.approx(0.01)
    assert return_diff_bps(0.0010, 0.0004) == pytest.approx(6.0)
    with pytest.raises(ZeroDivisionError):
        simple_return(0.0, 1.0)


@pytest.mark.parametrize(
    ("diff", "expected"),
    [
        (0.0, DQStatus.PASS),
        (5.0, DQStatus.PASS),  # exactly warn
        (-5.0, DQStatus.PASS),
        (5.000000001, DQStatus.WARN),
        (-5.5, DQStatus.WARN),
        (20.0, DQStatus.WARN),  # exactly fail
        (20.1, DQStatus.FAIL),
        (-20.1, DQStatus.FAIL),
        (5.0 + 1e-12, DQStatus.PASS),  # float noise does not breach
    ],
)
def test_status_boundaries(diff: float, expected: DQStatus) -> None:
    assert status_for(diff, 5.0, 20.0) is expected


# ---------------------------------------------------------------------------
# reference date: the current unfinished ET date is excluded (P-009)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "target", "cal", "expected"),
    [
        # Monday 07:00 ET: today excluded -> Friday
        (NOW, date(2026, 11, 16), "XNYS", date(2026, 11, 13)),
        # Monday 22:00 ET (03:00Z Tuesday): still Monday in ET -> Friday
        (datetime(2026, 11, 17, 3, 0, tzinfo=UTC), date(2026, 11, 16), "XNYS", date(2026, 11, 13)),
        # Tuesday 00:30 ET (05:30Z): Monday is now finished
        (datetime(2026, 11, 17, 5, 30, tzinfo=UTC), date(2026, 11, 17), "XNYS", date(2026, 11, 16)),
        # backfill of an old target date is not moved forward
        (NOW, date(2026, 11, 10), "XNYS", date(2026, 11, 10)),
        # target on a weekend rolls back to the last business day
        (NOW, date(2026, 11, 15), "XNYS", date(2026, 11, 13)),
        # US holiday (Thanksgiving 2026-11-26): XNYS and SIFMA roll back, no calendar does not
        (datetime(2026, 11, 27, 12, 0, tzinfo=UTC), date(2026, 11, 27), "XNYS", date(2026, 11, 25)),
        (datetime(2026, 11, 27, 12, 0, tzinfo=UTC), date(2026, 11, 27), None, date(2026, 11, 26)),
        # no calendar: weekends roll back to Friday
        (NOW, date(2026, 11, 16), None, date(2026, 11, 13)),
    ],
)
def test_reference_date(
    store: LocalBackend, config: Config, now: datetime, target: date, cal: Any, expected: date
) -> None:
    ctx = make_ctx(config, store, target=target, now=now)
    assert reference_date(ctx, cal) == expected


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_builtin_checks_are_registered() -> None:
    assert {"official_pair", "yahoo_vs_primary", "close_vs_official"} <= set(base.registered())


def test_duplicate_registration_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    class Dummy:
        name = "official_pair"

        def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]:
            return []

    base.registered()
    with pytest.raises(ValueError, match="already registered"):
        base.register(Dummy())


def test_unknown_check_name_rejected(store: LocalBackend, config: Config) -> None:
    with pytest.raises(KeyError, match="nope"):
        run_crosschecks(make_ctx(config, store), duck_for(store, config), only=["nope"])


# ---------------------------------------------------------------------------
# runner: frames, mirrored dq rows, isolation
# ---------------------------------------------------------------------------


def test_runner_returns_valid_frames_and_mirrors_dq_rows(
    store: LocalBackend, config: Config
) -> None:
    put(
        store,
        config,
        [
            treasury("UST_10Y", "10 Yr", 4.30),
            fred("UST_10Y", "DGS10", 4.25),  # +5 bp: warn (0.5 / 2)... fail
        ],
    )
    ctx = make_ctx(config, store)
    xdf, ddf = run_crosschecks(ctx, duck_for(store, config), only=["official_pair"])
    validate("crosscheck_results", xdf)
    validate("dq_results", ddf)
    assert len(xdf) == len(ddf) == 4  # one row per configured pair, skips included
    assert set(ddf["check_type"]) == {CheckType.CONSISTENCY.value}
    # same ids link the mirrored rows to their cross-check rows, same status, same instrument
    import json

    by_x = xdf.set_index("result_id")
    for _, d in ddf.iterrows():
        x = by_x.loc[json.loads(d["details_json"])["crosscheck_result_id"]]
        assert d["status"] == x["status"]
        assert d["check_name"] == x["check_name"] == "official_pair"
        assert d["instrument_id"] == x["left_instrument"]
        assert d["target_date"] == x["target_date"] == ctx.target_date
        assert d["run_id"] == ctx.run_id
    fail = ddf[ddf["status"] == "fail"].iloc[0]
    assert fail["observed"] == pytest.approx(5.0)
    assert fail["threshold"] == pytest.approx(2.0)  # fail_bps on a fail row
    skip = ddf[ddf["status"] == "skip"].iloc[0]
    assert skip["observed"] != skip["observed"]  # NaN: no diff on a skip row
    assert skip["threshold"] == pytest.approx(0.5)  # warn_bps otherwise


def test_runner_frames_can_be_flushed_once_for_the_run(store: LocalBackend, config: Config) -> None:
    put(store, config, [treasury("UST_2Y", "2 Yr", 4.0), fred("UST_2Y", "DGS2", 4.0)])
    ctx = make_ctx(config, store)
    xdf, ddf = run_crosschecks(ctx, duck_for(store, config))
    ds = Datasets(store)
    ds.append("crosscheck_results", xdf, ctx, ctx.target_date)
    ds.append("dq_results", ddf, ctx, ctx.target_date)
    assert len(ds.read("crosscheck_results", part_date=ctx.target_date)) == len(xdf)
    assert len(ds.read("dq_results", part_date=ctx.target_date)) == len(ddf)


def test_runner_with_nothing_registered_returns_empty_valid_frames(
    store: LocalBackend, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})
    monkeypatch.setattr(base, "load_builtin_checks", lambda: None)
    xdf, ddf = run_crosschecks(make_ctx(config, store), duck_for(store, config))
    assert xdf.empty
    assert ddf.empty
    validate("crosscheck_results", xdf)
    validate("dq_results", ddf)


def test_a_crashing_check_is_isolated_as_a_skip_row(
    store: LocalBackend, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Boom:
        name = "t21_boom"

        def evaluate(self, ctx: RunContext, duck: duckdb.DuckDBPyConnection) -> list[XResult]:
            raise RuntimeError("synthetic failure")

    monkeypatch.setitem(base._REGISTRY, "t21_boom", Boom())
    put(store, config, [treasury("UST_2Y", "2 Yr", 4.0), fred("UST_2Y", "DGS2", 4.0)])
    xdf, ddf = run_crosschecks(
        make_ctx(config, store), duck_for(store, config), only=["t21_boom", "official_pair"]
    )
    boom = xdf[xdf["check_name"] == "t21_boom"]
    assert len(boom) == 1
    assert boom.iloc[0]["status"] == "skip"
    assert "RuntimeError: synthetic failure" in boom.iloc[0]["details_json"]
    assert (xdf["check_name"] == "official_pair").sum() == 4  # the other check still ran
    validate("crosscheck_results", xdf)
    validate("dq_results", ddf)


def test_obs_pit_ignores_versions_ingested_after_run_time(
    store: LocalBackend, config: Config
) -> None:
    # SYNTHETIC: a FRED revision ingested after ctx.now_utc must not change the result
    late = datetime(2026, 11, 16, 13, 0, tzinfo=UTC)
    put(store, config, [treasury("UST_10Y", "10 Yr", 4.25), fred("UST_10Y", "DGS10", 4.25)])
    put(store, config, [fred("UST_10Y", "DGS10", 9.99, ingested_at=late)])
    xdf, _ = run_crosschecks(
        make_ctx(config, store), duck_for(store, config), only=["official_pair"]
    )
    row = xdf[xdf["left_instrument"] == "UST_10Y"].iloc[0]
    assert row["status"] == "pass"
    assert row["right_value"] == pytest.approx(4.25)
    assert DAY_FRI < INGESTED < late  # sanity of the fixture timeline
