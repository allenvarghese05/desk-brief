"""close_vs_official: Alpaca SIP last 1m bar ending at the close vs the Stooq daily close.

SYNTHETIC FIXTURE: all data is made up. Regular close 2026-11-13 is 16:00 ET = 21:00Z, so the
last bar starts 20:59Z. 2026-11-27 (day after Thanksgiving) is an early close: 13:00 ET = 18:00Z,
last bar starts 17:59Z.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from xc_fakes import (
    CLOSE_FRI,
    EARLY_CLOSE,
    EARLY_DAY,
    EARLY_NOW,
    duck_for,
    make_ctx,
    put,
    sip_bar,
    stooq_close,
)

from deskbrief.calendars import is_early_close
from deskbrief.config import Config
from deskbrief.crosschecks.close_official import CloseVsOfficial
from deskbrief.models import DQStatus, XResult
from deskbrief.store.local import LocalBackend

LAST_BAR = datetime(2026, 11, 13, 20, 59, tzinfo=UTC)
EARLY_LAST_BAR = datetime(2026, 11, 27, 17, 59, tzinfo=UTC)
EARLY_DAY_AS_OF = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)


def run(store: LocalBackend, config: Config, **kw: object) -> dict[str, XResult]:
    ctx = make_ctx(config, store, **kw)  # type: ignore[arg-type]
    return {r.left_instrument: r for r in CloseVsOfficial().evaluate(ctx, duck_for(store, config))}


# config thresholds: warn 5 bp, fail 25 bp (relative, vs the Stooq close)
@pytest.mark.parametrize(
    ("sip", "diff_bps", "status"),
    [
        (100.00, 0.0, DQStatus.PASS),
        (100.05, 5.0, DQStatus.PASS),  # exactly warn
        (100.06, 6.0, DQStatus.WARN),
        (100.25, 25.0, DQStatus.WARN),  # exactly fail
        (100.26, 26.0, DQStatus.FAIL),
        (99.70, -30.0, DQStatus.FAIL),
    ],
)
def test_boundaries(
    store: LocalBackend, config: Config, sip: float, diff_bps: float, status: DQStatus
) -> None:
    put(store, config, [sip_bar("SPY", LAST_BAR, sip), stooq_close("SPY", 100.0)])
    r = run(store, config)["SPY"]
    assert r.status is status
    assert r.diff_bps == pytest.approx(diff_bps, abs=1e-6)
    assert (r.warn_bps, r.fail_bps) == (5.0, 25.0)
    assert (r.left_source, r.right_source) == ("alpaca", "stooq")
    assert r.at_time == CLOSE_FRI
    assert r.relation == "same"
    assert r.group_id is not None


def test_details_carry_both_sides(store: LocalBackend, config: Config) -> None:
    put(store, config, [sip_bar("QQQ", LAST_BAR, 50.0), stooq_close("QQQ", 50.0)])
    d = run(store, config)["QQQ"].details
    assert (d["left"]["source"], d["left"]["dataset"]) == ("alpaca", "sip_bars_1m")
    assert (d["left"]["field"], d["left"]["unit"]) == ("close", "USD")
    assert d["left"]["as_of"] == LAST_BAR.isoformat()  # bar start; the bar ends at 16:00 ET
    assert (d["right"]["source"], d["right"]["dataset"]) == ("stooq", "daily")
    assert (d["right"]["field"], d["right"]["unit"]) == ("close", "USD")
    assert d["early_close"] is False
    assert d["session_close_utc"] == CLOSE_FRI.isoformat()
    assert d["expected_bar_start_utc"] == LAST_BAR.isoformat()
    assert d["basis"] == "relative"


def test_only_the_last_bar_before_the_close_counts(store: LocalBackend, config: Config) -> None:
    # a bar a minute earlier, and a stray 13:00 ET bar, must not be used
    put(
        store,
        config,
        [
            sip_bar("SPY", datetime(2026, 11, 13, 20, 58, tzinfo=UTC), 100.0),
            sip_bar("SPY", EARLY_LAST_BAR.replace(day=13), 100.0),
            stooq_close("SPY", 100.0),
        ],
    )
    r = run(store, config)["SPY"]
    assert r.status is DQStatus.SKIP
    assert "missing side" in r.details["reason"]
    assert "SPY/alpaca" in r.details["reason"]


def test_early_close_day_uses_the_13_00_bar(store: LocalBackend, config: Config) -> None:
    assert is_early_close(EARLY_DAY)
    put(
        store,
        config,
        [
            sip_bar("SPY", EARLY_LAST_BAR, 100.04),  # the real last bar (bar ends 13:00 ET)
            sip_bar("SPY", datetime(2026, 11, 27, 20, 59, tzinfo=UTC), 300.0),  # decoy at 16:00
            stooq_close("SPY", 100.0, as_of=EARLY_DAY_AS_OF),
        ],
    )
    r = run(store, config, target=EARLY_NOW.date(), now=EARLY_NOW)["SPY"]
    assert r.status is DQStatus.PASS
    assert r.diff_bps == pytest.approx(4.0)
    assert r.at_time == EARLY_CLOSE
    assert r.details["early_close"] is True
    assert r.details["ref_date"] == "2026-11-27"
    assert r.details["expected_bar_start_utc"] == EARLY_LAST_BAR.isoformat()


def test_regular_day_ignores_a_13_00_bar(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        [
            sip_bar("SPY", LAST_BAR, 100.0),
            sip_bar("SPY", datetime(2026, 11, 13, 17, 59, tzinfo=UTC), 300.0),  # decoy at 13:00
            stooq_close("SPY", 100.0),
        ],
    )
    r = run(store, config)["SPY"]
    assert r.status is DQStatus.PASS
    assert r.details["early_close"] is False


@pytest.mark.parametrize(
    ("rows", "missing"),
    [
        (lambda: [stooq_close("SPY", 100.0)], "SPY/alpaca"),
        (lambda: [sip_bar("SPY", LAST_BAR, 100.0)], "SPY/stooq"),
    ],
)
def test_missing_side_is_skip_with_reason(
    store: LocalBackend, config: Config, rows: object, missing: str
) -> None:
    put(store, config, rows())  # type: ignore[operator]
    r = run(store, config)["SPY"]
    assert r.status is DQStatus.SKIP
    assert r.diff_bps is None
    assert missing in r.details["reason"]
    assert r.details["left"]["unit"] == "USD"


def test_current_unfinished_date_is_excluded(store: LocalBackend, config: Config) -> None:
    mon_last = datetime(2026, 11, 16, 20, 59, tzinfo=UTC)  # Monday (today at run time)
    mon_day = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
    put(
        store,
        config,
        [
            sip_bar("SPY", LAST_BAR, 100.0),
            stooq_close("SPY", 100.0),
            sip_bar("SPY", mon_last, 500.0),
            stooq_close("SPY", 100.0, as_of=mon_day),
        ],
    )
    r = run(store, config)["SPY"]
    assert r.status is DQStatus.PASS
    assert r.details["ref_date"] == "2026-11-13"


def test_every_instrument_with_both_feeds_gets_a_row(store: LocalBackend, config: Config) -> None:
    out = run(store, config)
    # restricted to crosschecks.iex_vs_sip.symbols: T14 fetches SIP bars only for those
    assert set(out) == set(config.crosschecks.iex_vs_sip.symbols)
    assert set(out) == {"SPY", "QQQ", "IWM", "GLD", "TLT"}
    assert {r.status for r in out.values()} == {DQStatus.SKIP}


def test_instruments_outside_the_sip_symbol_list_are_not_compared(
    store: LocalBackend, config: Config
) -> None:
    # AAPL has sip_bars_1m and stooq feeds, but T14 never fetches its SIP bars
    put(store, config, [sip_bar("AAPL", LAST_BAR, 100.0), stooq_close("AAPL", 100.0)])
    assert "AAPL" not in run(store, config)
