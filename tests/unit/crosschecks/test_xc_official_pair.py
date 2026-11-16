"""official_pair: Treasury par curve vs FRED DGS* (absolute bp, publication lag).

SYNTHETIC FIXTURE: all data is made up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from xc_fakes import DAY_FRI, MON, duck_for, fred, make_ctx, put, treasury

from deskbrief.config import Config
from deskbrief.crosschecks.official_pair import OfficialPair
from deskbrief.models import DQStatus, XResult
from deskbrief.store.local import LocalBackend


def run(store: LocalBackend, config: Config) -> dict[str, XResult]:
    ctx = make_ctx(config, store)
    results = OfficialPair().evaluate(ctx, duck_for(store, config))
    return {r.left_instrument: r for r in results}


# config thresholds: warn 0.5 bp, fail 2 bp (absolute, 1 bp = 0.01 percentage point)
@pytest.mark.parametrize(
    ("tsy", "frd", "diff_bps", "status"),
    [
        (4.250, 4.250, 0.0, DQStatus.PASS),
        (4.255, 4.250, 0.5, DQStatus.PASS),  # exactly warn
        (4.256, 4.250, 0.6, DQStatus.WARN),
        (4.270, 4.250, 2.0, DQStatus.WARN),  # exactly fail
        (4.271, 4.250, 2.1, DQStatus.FAIL),
        (4.230, 4.250, -2.0, DQStatus.WARN),  # sign does not matter
        (4.229, 4.250, -2.1, DQStatus.FAIL),
    ],
)
def test_boundaries(
    store: LocalBackend, config: Config, tsy: float, frd: float, diff_bps: float, status: DQStatus
) -> None:
    put(store, config, [treasury("UST_10Y", "10 Yr", tsy), fred("UST_10Y", "DGS10", frd)])
    r = run(store, config)["UST_10Y"]
    assert r.status is status
    assert r.diff_bps == pytest.approx(diff_bps, abs=1e-6)
    assert (r.warn_bps, r.fail_bps) == (0.5, 2.0)
    assert r.left_source == "treasury"
    assert r.right_source == "fred"
    assert r.left_value == pytest.approx(tsy)
    assert r.right_value == pytest.approx(frd)
    assert r.relation == "level"
    assert r.group_id == "ust_10y"
    assert r.target_date == MON
    assert r.at_time == DAY_FRI


def test_yield_diff_is_absolute_not_relative(store: LocalBackend, config: Config) -> None:
    # 0.01 pct-pt on a 4.25 yield is exactly 1 bp absolute (relative would be 23.5 bp)
    put(store, config, [treasury("UST_2Y", "2 Yr", 4.26), fred("UST_2Y", "DGS2", 4.25)])
    r = run(store, config)["UST_2Y"]
    assert r.diff_bps == pytest.approx(1.0)
    assert r.status is DQStatus.WARN
    assert r.details["basis"] == "absolute_pct_points"


def test_details_carry_both_sides_field_unit_and_lag(store: LocalBackend, config: Config) -> None:
    fred_in = datetime(2026, 11, 14, 22, 0, tzinfo=UTC)  # a day after Treasury's 2026-11-13 22:00Z
    put(
        store,
        config,
        [treasury("UST_20Y", "20 Yr", 4.6), fred("UST_20Y", "DGS20", 4.6, ingested_at=fred_in)],
    )
    r = run(store, config)["UST_20Y"]
    assert r.status is DQStatus.PASS
    left, right = r.details["left"], r.details["right"]
    for side, source, dataset in ((left, "treasury", "par_curve"), (right, "fred", "series")):
        assert side["source"] == source
        assert side["dataset"] == dataset
        assert side["field"] == "yield"
        assert side["unit"] == "pct"
        assert side["as_of"] == DAY_FRI.isoformat()
    assert r.details["publication_lag_s"] == 86400.0  # FRED arrived 24h after Treasury
    assert r.details["ref_date"] == "2026-11-13"
    assert r.details["treasury_column"] == "20 Yr"
    assert r.details["fred_series"] == "DGS20"
    json.loads(r.details_json())  # serialisable


@pytest.mark.parametrize(
    ("rows", "missing"),
    [
        (lambda: [treasury("UST_30Y", "30 Yr", 4.7)], "fred"),
        (lambda: [fred("UST_30Y", "DGS30", 4.7)], "treasury"),
        (lambda: [], "UST_30Y/treasury/par_curve, UST_30Y/fred/series"),  # both sides named
    ],
)
def test_missing_side_is_skip_with_reason(
    store: LocalBackend, config: Config, rows: Any, missing: str
) -> None:
    put(store, config, [*rows(), treasury("UST_2Y", "2 Yr", 4.0), fred("UST_2Y", "DGS2", 4.0)])
    r = run(store, config)["UST_30Y"]
    assert r.status is DQStatus.SKIP
    assert r.diff_bps is None
    assert "missing side" in r.details["reason"]
    assert missing in r.details["reason"]
    assert r.details["left"]["unit"] == "pct"  # labels survive even when a side is absent
    assert r.details["right"]["source"] == "fred"


def test_fred_not_yet_published_records_pending_age(store: LocalBackend, config: Config) -> None:
    put(store, config, [treasury("UST_30Y", "30 Yr", 4.7)])
    r = run(store, config)["UST_30Y"]
    assert r.status is DQStatus.SKIP
    assert r.details["publication_lag_s"] is None
    # Treasury ingested 2026-11-13 22:00Z, run at 2026-11-16 12:00Z
    assert r.details["pending_age_s"] == pytest.approx(2 * 86400 + 14 * 3600)


def test_current_unfinished_date_is_excluded(store: LocalBackend, config: Config) -> None:
    # Monday's rows (today, ET) disagree wildly; they must not be used. Friday's agree.
    mon = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
    put(
        store,
        config,
        [
            treasury("UST_10Y", "10 Yr", 4.25),
            fred("UST_10Y", "DGS10", 4.25),
            treasury("UST_10Y", "10 Yr", 9.0, as_of=mon),
            fred("UST_10Y", "DGS10", 1.0, as_of=mon),
        ],
    )
    r = run(store, config)["UST_10Y"]
    assert r.status is DQStatus.PASS
    assert r.left_value == pytest.approx(4.25)
    assert r.details["ref_date"] == "2026-11-13"


def test_one_result_per_configured_pair(store: LocalBackend, config: Config) -> None:
    out = OfficialPair().evaluate(make_ctx(config, store), duck_for(store, config))
    assert [r.left_instrument for r in out] == ["UST_2Y", "UST_10Y", "UST_20Y", "UST_30Y"]
    assert {r.status for r in out} == {DQStatus.SKIP}
