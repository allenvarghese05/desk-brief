"""fx_vs_ecb: Twelve Data 1m bar and Yahoo tick at the ECB fix vs the ECB reference rate.

SYNTHETIC FIXTURE: all data is made up. The fix is 14:15 CET (Europe/Berlin): Friday 2026-11-13
is CET (UTC+1) so the fix is 13:15Z; Wednesday 2026-07-15 is CEST (UTC+2) so it is 12:15Z.
ECB rows are ``field=rate``, ``interval=1d``, ``as_of`` 00:00 ET of the reference date.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from xc_fakes import DAY_FRI, duck_for, make_ctx, minutes, obs, put, td_bar, yahoo_tick

from deskbrief.config import Config
from deskbrief.crosschecks.base import registered
from deskbrief.crosschecks.fx_ecb import FxVsEcb
from deskbrief.models import DQStatus, XResult
from deskbrief.store.local import LocalBackend

FIX = datetime(2026, 11, 13, 13, 15, tzinfo=UTC)
SUMMER_FIX = datetime(2026, 7, 15, 12, 15, tzinfo=UTC)
SUMMER_DAY = datetime(2026, 7, 15, 4, 0, tzinfo=UTC)  # 00:00 EDT
SUMMER_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
SUMMER_INGESTED = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)


def ecb(inst: str, value: float, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "instrument_id": inst, "source": "ecb", "dataset": "eurofxref", "symbol": inst,
        "tier": 3, "field": "rate", "interval": "1d", "as_of": DAY_FRI, "value": value,
        "unit": "fx_rate", "is_derived": inst != "EURUSD",
    }  # fmt: skip
    return obs(**{**row, **over})


SYMBOLS = {"EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY"}
YAHOO = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X"}


def td(inst: str, start: datetime, value: float, **over: Any) -> dict[str, Any]:
    return td_bar(inst, SYMBOLS[inst], start, value, **over)


def yh(inst: str, at: datetime, value: float, **over: Any) -> dict[str, Any]:
    return yahoo_tick(inst, YAHOO[inst], at, value, unit="fx_rate", **over)


def run(store: LocalBackend, config: Config, **kw: object) -> dict[tuple[str, str], XResult]:
    ctx = make_ctx(config, store, **kw)  # type: ignore[arg-type]
    return {
        (r.left_instrument, r.left_source): r
        for r in FxVsEcb().evaluate(ctx, duck_for(store, config))
    }


# config thresholds: warn 5 bp, fail 15 bp, relative to the ECB reference rate
@pytest.mark.parametrize(
    ("bar", "diff_bps", "status"),
    [
        (1.0000, 0.0, DQStatus.PASS),
        (1.0005, 5.0, DQStatus.PASS),  # exactly warn
        (1.0006, 6.0, DQStatus.WARN),
        (1.0015, 15.0, DQStatus.WARN),  # exactly fail
        (1.0016, 16.0, DQStatus.FAIL),
        (0.9980, -20.0, DQStatus.FAIL),
    ],
)
def test_twelvedata_boundaries(
    store: LocalBackend, config: Config, bar: float, diff_bps: float, status: DQStatus
) -> None:
    put(store, config, [ecb("EURUSD", 1.0), td("EURUSD", FIX, bar)])
    r = run(store, config)[("EURUSD", "twelvedata")]
    assert r.status is status
    assert r.diff_bps == pytest.approx(diff_bps, abs=1e-6)
    assert (r.warn_bps, r.fail_bps) == (5.0, 15.0)
    assert (r.right_instrument, r.right_source) == ("EURUSD", "ecb")
    assert r.expected_value == pytest.approx(1.0)
    assert r.at_time == FIX
    assert r.relation == "level"
    assert r.group_id == "eurusd"


def test_ecb_side_is_a_reference_rate_not_a_close(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        [ecb("GBPUSD", 1.25), td("GBPUSD", FIX, 1.25), ecb("EURUSD", 1.1), td("EURUSD", FIX, 1.1)],
    )
    out = run(store, config)
    gbp, eur = out[("GBPUSD", "twelvedata")].details, out[("EURUSD", "twelvedata")].details
    assert (gbp["right"]["source"], gbp["right"]["dataset"]) == ("ecb", "eurofxref")
    assert (gbp["right"]["field"], gbp["right"]["unit"]) == ("rate", "fx_rate")
    assert gbp["right_label"] == "ECB reference rate"
    assert gbp["right_is_derived"] is True  # cross via EUR
    assert eur["right_is_derived"] is False
    assert (gbp["left"]["field"], gbp["left"]["dataset"]) == ("close", "fx_series_1m")
    assert gbp["basis"] == "relative"
    assert gbp["fix_time_cet"] == "14:15"
    assert gbp["fix_utc"] == FIX.isoformat()
    assert gbp["ref_date"] == "2026-11-13"


def test_only_the_bar_starting_at_the_fix_counts(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        [
            ecb("EURUSD", 1.0),
            td("EURUSD", minutes(FIX, -1), 1.0100),
            td("EURUSD", minutes(FIX, 1), 1.0100),
            td("EURUSD", FIX, 1.0001),
        ],
    )
    r = run(store, config)[("EURUSD", "twelvedata")]
    assert r.left_value == pytest.approx(1.0001)
    assert r.details["n_twelvedata_bars"] == 3


def test_missing_bar_at_the_fix_is_skip(store: LocalBackend, config: Config) -> None:
    put(store, config, [ecb("EURUSD", 1.0), td("EURUSD", minutes(FIX, 1), 1.0)])
    r = run(store, config)[("EURUSD", "twelvedata")]
    assert r.status is DQStatus.SKIP
    assert r.diff_bps is None
    assert "missing side: EURUSD/twelvedata/fx_series_1m" in r.details["reason"]
    assert r.details["n_twelvedata_bars"] == 1


def test_twelvedata_mid_is_not_a_field(store: LocalBackend, config: Config) -> None:
    put(store, config, [ecb("EURUSD", 1.0), td("EURUSD", FIX, 1.0, field="mid")])
    assert run(store, config)[("EURUSD", "twelvedata")].status is DQStatus.SKIP


def test_missing_ecb_is_skip(store: LocalBackend, config: Config) -> None:
    put(store, config, [td("EURUSD", FIX, 1.0)])
    r = run(store, config)[("EURUSD", "twelvedata")]
    assert r.status is DQStatus.SKIP
    assert "EURUSD/ecb/eurofxref" in r.details["reason"]
    assert r.details["right_label"] == "ECB reference rate"


def test_yahoo_tick_nearest_to_the_fix(store: LocalBackend, config: Config) -> None:
    near = datetime(2026, 11, 13, 13, 15, 20, tzinfo=UTC)
    put(
        store,
        config,
        [
            ecb("USDJPY", 150.0),
            yh("USDJPY", near, 150.015),
            yh("USDJPY", minutes(FIX, -1), 151.0),  # 60 s early, farther
            yh("USDJPY", minutes(FIX, 5), 160.0),  # outside the window
        ],
    )
    r = run(store, config)[("USDJPY", "yahoo")]
    assert r.status is DQStatus.PASS
    assert r.diff_bps == pytest.approx(1.0, abs=1e-6)
    assert (r.left_source, r.right_source) == ("yahoo", "ecb")
    assert r.details["left"]["as_of"] == near.isoformat()
    assert r.details["right_is_derived"] is True


def test_yahoo_without_a_tick_near_the_fix_is_skip(store: LocalBackend, config: Config) -> None:
    put(store, config, [ecb("EURUSD", 1.0), yh("EURUSD", minutes(FIX, 5), 1.0)])
    r = run(store, config)[("EURUSD", "yahoo")]
    assert r.status is DQStatus.SKIP
    assert "yahoo/quote" in r.details["reason"]


def test_summer_fix_is_12_15_utc(store: LocalBackend, config: Config) -> None:
    kw: dict[str, Any] = {"ingested_at": SUMMER_INGESTED}
    put(
        store,
        config,
        [
            ecb("EURUSD", 1.0, as_of=SUMMER_DAY, **kw),
            td("EURUSD", SUMMER_FIX, 1.0003, **kw),
            td(
                "EURUSD", datetime(2026, 7, 15, 13, 15, tzinfo=UTC), 2.0, **kw
            ),  # winter-style 13:15Z
        ],
    )
    r = run(store, config, target=SUMMER_NOW.date(), now=SUMMER_NOW)[("EURUSD", "twelvedata")]
    assert r.status is DQStatus.PASS
    assert r.at_time == SUMMER_FIX
    assert r.details["fix_utc"] == SUMMER_FIX.isoformat()
    assert r.diff_bps == pytest.approx(3.0, abs=1e-6)


def test_winter_fix_is_13_15_utc_not_12_15(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        [ecb("EURUSD", 1.0), td("EURUSD", FIX, 1.0), td("EURUSD", minutes(FIX, -60), 2.0)],
    )
    r = run(store, config)[("EURUSD", "twelvedata")]
    assert r.status is DQStatus.PASS
    assert r.at_time == FIX


def test_target_holiday_rolls_back_to_the_last_ecb_day(store: LocalBackend, config: Config) -> None:
    # 2026-04-03 (Good Friday) and 2026-04-06 (Easter Monday) are TARGET holidays
    now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
    fix = datetime(2026, 4, 2, 12, 15, tzinfo=UTC)  # CEST
    kw: dict[str, Any] = {"ingested_at": datetime(2026, 4, 2, 20, 0, tzinfo=UTC)}
    put(
        store,
        config,
        [
            ecb("EURUSD", 1.0, as_of=datetime(2026, 4, 2, 4, 0, tzinfo=UTC), **kw),
            td("EURUSD", fix, 1.0, **kw),
        ],
    )
    r = run(store, config, target=now.date(), now=now)[("EURUSD", "twelvedata")]
    assert r.status is DQStatus.PASS
    assert r.details["ref_date"] == "2026-04-02"


def test_current_unfinished_date_is_excluded(store: LocalBackend, config: Config) -> None:
    mon_fix = datetime(2026, 11, 16, 13, 15, tzinfo=UTC)  # Monday = today at run time
    put(
        store,
        config,
        [
            ecb("EURUSD", 1.0),
            td("EURUSD", FIX, 1.0),
            ecb("EURUSD", 1.0, as_of=datetime(2026, 11, 16, 5, 0, tzinfo=UTC)),
            td("EURUSD", mon_fix, 5.0),
        ],
    )
    r = run(store, config)[("EURUSD", "twelvedata")]
    assert r.details["ref_date"] == "2026-11-13"
    assert r.status is DQStatus.PASS


def test_every_configured_pair_gets_both_rows(store: LocalBackend, config: Config) -> None:
    out = run(store, config)
    assert set(out) == {
        (p, s) for p in config.crosschecks.fx_vs_ecb.pairs for s in ("twelvedata", "yahoo")
    }
    assert {r.status for r in out.values()} == {DQStatus.SKIP}


def test_registered() -> None:
    assert isinstance(registered()["fx_vs_ecb"], FxVsEcb)
