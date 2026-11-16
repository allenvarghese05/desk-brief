"""yahoo_vs_primary: Yahoo last vs Alpaca SIP (equities) / Twelve Data (FX), same minute +-1.

SYNTHETIC FIXTURE: all data is made up. Friday 2026-11-13 20:59:40Z is 15:59:40 ET.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from xc_fakes import (
    CLOSE_FRI,
    EARLY_NOW,
    NOW,
    duck_for,
    make_ctx,
    minutes,
    put,
    sip_bar,
    td_bar,
    yahoo_tick,
)

from deskbrief.config import Config
from deskbrief.crosschecks.yahoo_primary import YahooVsPrimary
from deskbrief.models import DQStatus, XResult
from deskbrief.store.local import LocalBackend

TICK = datetime(2026, 11, 13, 20, 59, 40, tzinfo=UTC)
MIN = datetime(2026, 11, 13, 20, 59, tzinfo=UTC)  # the tick's minute


def run(store: LocalBackend, config: Config, **kw: object) -> dict[str, XResult]:
    ctx = make_ctx(config, store, **kw)  # type: ignore[arg-type]
    return {r.left_instrument: r for r in YahooVsPrimary().evaluate(ctx, duck_for(store, config))}


# config thresholds: warn 10 bp, fail 50 bp (relative, vs the SIP / Twelve Data reference)
@pytest.mark.parametrize(
    ("yahoo", "diff_bps", "status"),
    [
        (100.00, 0.0, DQStatus.PASS),
        (100.10, 10.0, DQStatus.PASS),  # exactly warn
        (100.11, 11.0, DQStatus.WARN),
        (100.50, 50.0, DQStatus.WARN),  # exactly fail
        (100.51, 51.0, DQStatus.FAIL),
        (99.40, -60.0, DQStatus.FAIL),
    ],
)
def test_equity_boundaries(
    store: LocalBackend, config: Config, yahoo: float, diff_bps: float, status: DQStatus
) -> None:
    put(store, config, [yahoo_tick("SPY", "SPY", TICK, yahoo), sip_bar("SPY", MIN, 100.0)])
    r = run(store, config)["SPY"]
    assert r.status is status
    assert r.diff_bps == pytest.approx(diff_bps, abs=1e-6)
    assert (r.warn_bps, r.fail_bps) == (10.0, 50.0)
    assert (r.left_source, r.right_source) == ("yahoo", "alpaca")
    assert r.left_value == pytest.approx(yahoo)
    assert r.right_value == pytest.approx(100.0)
    assert r.at_time == TICK
    assert r.relation == "same"
    assert r.details["basis"] == "relative"


def test_details_carry_both_sides(store: LocalBackend, config: Config) -> None:
    put(store, config, [yahoo_tick("QQQ", "QQQ", TICK, 50.0), sip_bar("QQQ", MIN, 50.0)])
    d = run(store, config)["QQQ"].details
    assert (d["left"]["source"], d["left"]["dataset"]) == ("yahoo", "quote")
    assert (d["left"]["field"], d["left"]["unit"]) == ("last", "USD")
    assert d["left"]["as_of"] == TICK.isoformat()
    assert (d["right"]["source"], d["right"]["dataset"]) == ("alpaca", "sip_bars_1m")
    assert (d["right"]["field"], d["right"]["unit"]) == ("close", "USD")
    assert d["right"]["as_of"] == MIN.isoformat()
    assert d["minute_offset"] == 0
    assert d["kind"] == "equity"


@pytest.mark.parametrize(
    ("bar_minutes", "offset"),
    [
        ([0], 0),
        ([0, -1, 1], 0),  # same minute wins over neighbours
        ([-1], -1),
        ([1], 1),
        ([-1, 1], -1),  # earlier neighbour tried first
    ],
)
def test_minute_alignment_within_one_minute(
    store: LocalBackend, config: Config, bar_minutes: list[int], offset: int
) -> None:
    bars = [sip_bar("SPY", minutes(MIN, m), 100.0 + m) for m in bar_minutes]
    put(store, config, [yahoo_tick("SPY", "SPY", TICK, 100.0), *bars])
    r = run(store, config)["SPY"]
    assert r.details["minute_offset"] == offset
    assert r.right_value == pytest.approx(100.0 + offset)


@pytest.mark.parametrize("bar_minutes", [[-2], [2], [-5, 5]])
def test_bar_two_minutes_away_is_skip(
    store: LocalBackend, config: Config, bar_minutes: list[int]
) -> None:
    bars = [sip_bar("SPY", minutes(MIN, m), 100.0) for m in bar_minutes]
    put(store, config, [yahoo_tick("SPY", "SPY", TICK, 100.0), *bars])
    r = run(store, config)["SPY"]
    assert r.status is DQStatus.SKIP
    assert "within 1 minute" in r.details["reason"]
    assert r.diff_bps is None


def test_latest_yahoo_tick_of_the_session_is_used(store: LocalBackend, config: Config) -> None:
    early = datetime(2026, 11, 13, 15, 0, 5, tzinfo=UTC)
    put(
        store,
        config,
        [
            yahoo_tick("SPY", "SPY", early, 90.0),
            yahoo_tick("SPY", "SPY", TICK, 100.0),
            sip_bar("SPY", MIN, 100.0),
            sip_bar("SPY", datetime(2026, 11, 13, 15, 0, tzinfo=UTC), 90.0),
        ],
    )
    r = run(store, config)["SPY"]
    assert r.left_value == pytest.approx(100.0)
    assert r.status is DQStatus.PASS


def test_missing_alpaca_side_is_skip_with_reason(store: LocalBackend, config: Config) -> None:
    # Alpaca has not been ingested at all (source card not merged / budget exhausted)
    put(store, config, [yahoo_tick("SPY", "SPY", TICK, 100.0)])
    r = run(store, config)["SPY"]
    assert r.status is DQStatus.SKIP
    assert "missing side" in r.details["reason"]
    assert r.left_value == pytest.approx(100.0)
    assert r.right_value is None
    assert r.details["right"]["source"] == "alpaca"


def test_missing_yahoo_side_is_skip_with_reason(store: LocalBackend, config: Config) -> None:
    put(store, config, [sip_bar("SPY", MIN, 100.0)])
    r = run(store, config)["SPY"]
    assert r.status is DQStatus.SKIP
    assert "no yahoo quote tick" in r.details["reason"]


def test_every_equity_and_fx_member_gets_a_row_even_with_no_data(
    store: LocalBackend, config: Config
) -> None:
    out = run(store, config)
    # equities are restricted to crosschecks.iex_vs_sip.symbols (T14 fetches SIP bars only there)
    assert config.crosschecks.iex_vs_sip.symbols == ("SPY", "QQQ", "IWM", "GLD", "TLT")
    assert set(out) == {"SPY", "QQQ", "IWM", "GLD", "TLT", "EURUSD", "GBPUSD", "USDJPY"}
    assert {r.status for r in out.values()} == {DQStatus.SKIP}
    assert all("reason" in r.details for r in out.values())


def test_equities_outside_the_sip_symbol_list_are_not_compared(
    store: LocalBackend, config: Config
) -> None:
    # AAPL has both a yahoo and an alpaca sip_bars_1m feed but T14 never fetches its SIP bars
    put(store, config, [yahoo_tick("AAPL", "AAPL", TICK, 100.0), sip_bar("AAPL", MIN, 100.0)])
    assert "AAPL" not in run(store, config)


def test_ticks_after_the_session_close_are_ignored(store: LocalBackend, config: Config) -> None:
    after_hours = datetime(2026, 11, 13, 21, 5, tzinfo=UTC)  # 16:05 ET, closing prints
    put(
        store,
        config,
        [
            yahoo_tick("SPY", "SPY", TICK, 100.0),
            yahoo_tick("SPY", "SPY", after_hours, 150.0),
            sip_bar("SPY", MIN, 100.0),
            sip_bar("SPY", minutes(after_hours, 0), 150.0),
        ],
    )
    r = run(store, config)["SPY"]
    assert r.left_value == pytest.approx(100.0)
    assert r.status is DQStatus.PASS
    assert r.details["session_close_utc"] == CLOSE_FRI.isoformat()


def test_early_close_session_ignores_ticks_after_13_00_et(
    store: LocalBackend, config: Config
) -> None:
    before = datetime(2026, 11, 27, 17, 59, 30, tzinfo=UTC)  # 12:59:30 ET
    after = datetime(2026, 11, 27, 20, 59, 40, tzinfo=UTC)  # 15:59:40 ET, after the early close
    put(
        store,
        config,
        [
            yahoo_tick("SPY", "SPY", before, 100.0),
            yahoo_tick("SPY", "SPY", after, 200.0),
            sip_bar("SPY", datetime(2026, 11, 27, 17, 59, tzinfo=UTC), 100.0),
            sip_bar("SPY", datetime(2026, 11, 27, 20, 59, tzinfo=UTC), 200.0),
        ],
    )
    r = run(store, config, target=EARLY_NOW.date(), now=EARLY_NOW)["SPY"]
    assert r.left_value == pytest.approx(100.0)
    assert r.status is DQStatus.PASS
    assert r.details["ref_date"] == "2026-11-27"


# -- FX vs Twelve Data ---------------------------------------------------------------------


def test_fx_vs_twelve_data_boundaries_use_relative_bp(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        [
            yahoo_tick("EURUSD", "EURUSD=X", TICK, 1.0810, unit="fx_rate"),
            td_bar("EURUSD", "EUR/USD", MIN, 1.0800),
        ],
    )
    r = run(store, config)["EURUSD"]
    assert r.diff_bps == pytest.approx((1.0810 - 1.0800) / 1.0800 * 1e4)  # ~9.26 bp
    assert r.status is DQStatus.PASS
    assert (r.right_source, r.details["right"]["dataset"]) == ("twelvedata", "fx_series_1m")
    assert r.details["right"]["unit"] == "fx_rate"
    assert r.details["kind"] == "fx"
    assert r.relation == "level"


def test_fx_warn_and_fail(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        [
            yahoo_tick("EURUSD", "EURUSD=X", TICK, 1.0815, unit="fx_rate"),  # +13.9 bp
            td_bar("EURUSD", "EUR/USD", MIN, 1.0800),
            yahoo_tick("GBPUSD", "GBPUSD=X", TICK, 1.2700, unit="fx_rate"),  # +78 bp
            td_bar("GBPUSD", "GBP/USD", MIN, 1.2601),
        ],
    )
    out = run(store, config)
    assert out["EURUSD"].status is DQStatus.WARN
    assert out["GBPUSD"].status is DQStatus.FAIL


def test_fx_uses_twelve_data_close_field(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        [
            yahoo_tick("EURUSD", "EURUSD=X", TICK, 1.0800, unit="fx_rate"),
            td_bar("EURUSD", "EUR/USD", MIN, 1.0800),
        ],
    )
    r = run(store, config)["EURUSD"]
    assert r.details["right"]["field"] == "close"
    assert r.status is DQStatus.PASS


def test_fx_missing_twelve_data_is_skip(store: LocalBackend, config: Config) -> None:
    put(store, config, [yahoo_tick("USDJPY", "JPY=X", TICK, 150.0, unit="fx_rate")])
    r = run(store, config)["USDJPY"]
    assert r.status is DQStatus.SKIP
    assert "no twelvedata/fx_series_1m bars" in r.details["reason"]


# Realistic layout: Twelve Data bars only in the ECB-fix window (14:15 CET = 13:15Z in winter,
# bars 13:05-13:25Z), Yahoo ticks spread through the day.
FIX_BARS = [datetime(2026, 11, 13, 13, m, tzinfo=UTC) for m in range(5, 26)]


def _spread_ticks(extra: list[datetime]) -> list[dict[str, object]]:
    base = [datetime(2026, 11, 13, h, mi, 12, tzinfo=UTC) for h in range(6, 21) for mi in (0, 30)]
    return [
        yahoo_tick("EURUSD", "EURUSD=X", t, 1.0800 + i * 1e-5, unit="fx_rate")
        for i, t in enumerate(sorted({*base, *extra}))
    ]


def test_fx_anchors_on_twelve_data_bars_matched(store: LocalBackend, config: Config) -> None:
    near_fix = datetime(2026, 11, 13, 13, 15, 40, tzinfo=UTC)  # inside the bar window
    put(
        store,
        config,
        [
            *_spread_ticks([near_fix]),
            *[td_bar("EURUSD", "EUR/USD", b, 1.0800) for b in FIX_BARS],
        ],
    )
    r = run(store, config)["EURUSD"]
    assert r.status is not DQStatus.SKIP
    assert r.at_time == near_fix
    assert r.details["right"]["as_of"] == datetime(2026, 11, 13, 13, 15, tzinfo=UTC).isoformat()
    assert r.details["minute_offset"] == 0
    assert r.details["n_twelvedata_bars"] == len(FIX_BARS)
    assert r.details["n_yahoo_ticks"] > 20  # the latest tick of the day is NOT what is compared


def test_fx_anchors_on_twelve_data_bars_unmatched(store: LocalBackend, config: Config) -> None:
    # ticks at hh:00:12 / hh:30:12 only: the nearest to the 13:05-13:25 bars is 13:00:12 (5 min off)
    put(
        store,
        config,
        [*_spread_ticks([]), *[td_bar("EURUSD", "EUR/USD", b, 1.0800) for b in FIX_BARS]],
    )
    r = run(store, config)["EURUSD"]
    assert r.status is DQStatus.SKIP
    assert r.details["reason"] == "no Yahoo tick within ±1m of the ECB-fix bars"
    assert r.diff_bps is None
    assert r.details["n_twelvedata_bars"] == len(FIX_BARS)


def test_fx_adjacent_minute_tick_matches_with_offset(store: LocalBackend, config: Config) -> None:
    tick = datetime(2026, 11, 13, 13, 4, 50, tzinfo=UTC)  # minute 13:04; first bar starts 13:05
    put(
        store,
        config,
        [
            yahoo_tick("EURUSD", "EURUSD=X", tick, 1.0800, unit="fx_rate"),
            *[td_bar("EURUSD", "EUR/USD", b, 1.0800) for b in FIX_BARS],
        ],
    )
    r = run(store, config)["EURUSD"]
    assert r.status is DQStatus.PASS
    assert r.details["minute_offset"] == 1


def test_current_unfinished_date_is_excluded(store: LocalBackend, config: Config) -> None:
    # Today (Mon, ET) has a wildly different pair; the comparison uses Friday's session.
    mon_tick = datetime(2026, 11, 16, 11, 59, 40, tzinfo=UTC)  # 06:59 ET, before NOW
    mon_min = datetime(2026, 11, 16, 11, 59, tzinfo=UTC)
    put(
        store,
        config,
        [
            yahoo_tick("SPY", "SPY", TICK, 100.0),
            sip_bar("SPY", MIN, 100.0),
            yahoo_tick("SPY", "SPY", mon_tick, 150.0),
            sip_bar("SPY", mon_min, 100.0),
        ],
    )
    r = run(store, config)["SPY"]
    assert r.left_value == pytest.approx(100.0)
    assert r.status is DQStatus.PASS
    assert r.details["ref_date"] == "2026-11-13"
    assert NOW > mon_tick > CLOSE_FRI
