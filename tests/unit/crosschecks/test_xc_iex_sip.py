"""iex_vs_sip: IEX 1m close vs SIP 1m close per session minute.

SYNTHETIC FIXTURE: all data is made up. Friday 2026-11-13 regular session is 09:30-16:00 ET =
14:30Z-21:00Z (390 minutes). 2026-11-27 is an early close (13:00 ET = 18:00Z, 210 minutes).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from xc_fakes import (
    EARLY_DAY,
    EARLY_NOW,
    duck_for,
    make_ctx,
    minutes,
    obs,
    put,
    sip_bar,
)

from deskbrief.config import Config
from deskbrief.crosschecks.base import registered, run_crosschecks
from deskbrief.crosschecks.iex_sip import IexVsSip
from deskbrief.models import DQStatus, XResult
from deskbrief.store.local import LocalBackend

OPEN = datetime(2026, 11, 13, 14, 30, tzinfo=UTC)
CLOSE = datetime(2026, 11, 13, 21, 0, tzinfo=UTC)
EARLY_OPEN = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
EARLY_CLOSE = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def iex_bar(inst: str, start: datetime, value: float, **over: Any) -> dict[str, Any]:
    return obs(
        instrument_id=inst, source="alpaca", dataset="iex_bars_1m", symbol=inst, tier=1,
        interval="1m", as_of=start, value=value, **over,
    )  # fmt: skip


def pairs(
    inst: str, first: int, n: int, iex: float, sip: float, base: datetime = OPEN
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for k in range(first, first + n):
        rows += [iex_bar(inst, minutes(base, k), iex), sip_bar(inst, minutes(base, k), sip)]
    return rows


def run(store: LocalBackend, config: Config, **kw: object) -> dict[str, XResult]:
    ctx = make_ctx(config, store, **kw)  # type: ignore[arg-type]
    return {r.left_instrument: r for r in IexVsSip().evaluate(ctx, duck_for(store, config))}


# config: warn 5 bp, fail 20 bp, max_fail_minutes_pct 5; gap = (IEX - SIP) / SIP in bp
@pytest.mark.parametrize(
    ("iex", "median", "status"),
    [
        (100.00, 0.0, DQStatus.PASS),
        (100.05, 5.0, DQStatus.PASS),  # exactly warn: not over it
        (100.06, 6.0, DQStatus.WARN),
        (100.20, 20.0, DQStatus.WARN),  # exactly fail: not over it
        (100.21, 21.0, DQStatus.FAIL),
        (99.70, 30.0, DQStatus.FAIL),
    ],
)
def test_boundaries(
    store: LocalBackend, config: Config, iex: float, median: float, status: DQStatus
) -> None:
    put(store, config, pairs("SPY", 0, 100, iex, 100.0))
    r = run(store, config)["SPY"]
    assert r.status is status
    assert r.diff_bps == pytest.approx(median, abs=1e-6)  # diff_bps = median abs gap
    assert (r.warn_bps, r.fail_bps) == (5.0, 20.0)
    assert (r.left_source, r.right_source) == ("alpaca", "alpaca")
    assert r.relation == "same"
    assert r.at_time == CLOSE


@pytest.mark.parametrize(
    ("n_bad", "bad_iex", "status"),
    [
        (5, 100.30, DQStatus.PASS),  # 5% over fail: not above the 5% limit
        (6, 100.30, DQStatus.FAIL),  # 6% over fail
        (6, 100.10, DQStatus.WARN),  # 6% over warn, none over fail
        (5, 100.10, DQStatus.PASS),
    ],
)
def test_status_comes_from_the_share_of_bad_minutes(
    tmp_path: Path, config: Config, n_bad: int, bad_iex: float, status: DQStatus
) -> None:
    store = LocalBackend(tmp_path / "d")
    rows = pairs("SPY", 0, 100 - n_bad, 100.0, 100.0) + pairs(
        "SPY", 100 - n_bad, n_bad, bad_iex, 100.0
    )
    put(store, config, rows)
    r = run(store, config)["SPY"]
    assert r.status is status
    assert r.details["n_compared"] == 100
    assert r.details["median_abs_gap_bps"] == pytest.approx(0.0)
    assert r.details["pct_minutes_over_warn"] == pytest.approx(float(n_bad))
    assert r.details["max_fail_minutes_pct"] == 5.0


def test_percentiles(store: LocalBackend, config: Config) -> None:
    rows = pairs("SPY", 0, 94, 100.0, 100.0) + pairs("SPY", 94, 6, 100.30, 100.0)
    put(store, config, rows)
    d = run(store, config)["SPY"].details
    assert d["p95_abs_gap_bps"] == pytest.approx(30.0)  # nearest rank: 95th of 100
    assert d["pct_minutes_over_fail"] == pytest.approx(6.0)


def test_p95_is_nearest_rank_with_95_clean_and_5_bad_minutes(
    store: LocalBackend, config: Config
) -> None:
    # nearest rank: ceil(0.95 * 100) = 95th smallest gap; 95 clean minutes, so it is a clean one
    rows = pairs("SPY", 0, 95, 100.0, 100.0) + pairs("SPY", 95, 5, 100.30, 100.0)
    put(store, config, rows)
    r = run(store, config)["SPY"]
    assert r.details["p95_abs_gap_bps"] == pytest.approx(0.0)
    assert r.details["pct_minutes_over_fail"] == pytest.approx(5.0)
    assert r.status is DQStatus.PASS  # exactly 5 % is not above the 5 % limit


def test_n_missing_both_never_goes_negative(store: LocalBackend, config: Config) -> None:
    # bars off the minute grid (30 s past each minute) make the distinct start times exceed the
    # session's minute count; the count is a minute-grid estimate and is clamped at zero
    rows = pairs("SPY", 0, 390, 100.0, 100.0)
    rows += pairs("SPY", 0, 390, 100.0, 100.0, OPEN + timedelta(seconds=30))
    put(store, config, rows)
    d = run(store, config)["SPY"].details
    assert d["n_iex_bars"] == d["n_sip_bars"] == 780
    assert d["n_missing_both"] == 0


def test_missing_minutes_are_excluded_and_counted(store: LocalBackend, config: Config) -> None:
    rows = pairs("SPY", 0, 100, 100.0, 100.0)  # minutes 0..99 on both sides
    rows += [iex_bar("SPY", minutes(OPEN, k), 100.0) for k in range(100, 110)]  # IEX only
    rows += [sip_bar("SPY", minutes(OPEN, k), 300.0) for k in range(110, 125)]  # SIP only
    rows += [iex_bar("SPY", minutes(OPEN, -5), 900.0), sip_bar("SPY", minutes(OPEN, -5), 1.0)]
    rows += [iex_bar("SPY", CLOSE, 900.0), sip_bar("SPY", CLOSE, 1.0)]  # outside the session
    put(store, config, rows)
    r = run(store, config)["SPY"]
    d = r.details
    assert r.status is DQStatus.PASS
    assert d["n_session_minutes"] == 390
    assert d["n_compared"] == 100
    assert d["n_missing_sip"] == 10  # IEX bar without a SIP bar
    assert d["n_missing_iex"] == 15  # SIP bar without an IEX bar
    assert d["n_missing_both"] == 390 - 100 - 10 - 15
    assert d["n_iex_bars"] == 110
    assert d["n_sip_bars"] == 115


def test_details_label_both_sides(store: LocalBackend, config: Config) -> None:
    put(store, config, pairs("QQQ", 0, 10, 50.0, 50.0))
    d = run(store, config)["QQQ"].details
    assert (d["left"]["source"], d["left"]["dataset"]) == ("alpaca", "iex_bars_1m")
    assert (d["left"]["field"], d["left"]["unit"]) == ("close", "USD")
    assert (d["right"]["source"], d["right"]["dataset"]) == ("alpaca", "sip_bars_1m")
    assert (d["right"]["field"], d["right"]["unit"]) == ("close", "USD")
    assert d["left_label"] == "IEX 1m close (single venue)"
    assert d["right_label"] == "SIP 1m close (consolidated, all venues)"
    assert d["basis"] == "relative"
    assert d["ref_date"] == "2026-11-13"
    assert d["session_open_utc"] == OPEN.isoformat()
    assert d["session_close_utc"] == CLOSE.isoformat()


def test_no_overlap_or_no_data_is_skip(store: LocalBackend, config: Config) -> None:
    put(store, config, [iex_bar("SPY", OPEN, 100.0), sip_bar("QQQ", OPEN, 50.0)])
    out = run(store, config)
    assert out["SPY"].status is DQStatus.SKIP
    assert out["SPY"].diff_bps is None
    assert "no minute with both" in out["SPY"].details["reason"]
    assert out["SPY"].details["n_iex_bars"] == 1
    assert out["QQQ"].status is DQStatus.SKIP
    assert out["IWM"].status is DQStatus.SKIP  # no data at all


def test_zero_sip_close_is_not_a_reference(store: LocalBackend, config: Config) -> None:
    put(store, config, pairs("SPY", 0, 10, 100.0, 100.0) + pairs("SPY", 10, 2, 100.0, 0.0))
    r = run(store, config)["SPY"]
    assert r.status is DQStatus.PASS
    assert r.details["n_compared"] == 10
    assert r.details["n_zero_reference"] == 2


def test_early_close_session_has_210_minutes(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        [
            *pairs("SPY", 0, 20, 100.0, 100.0, EARLY_OPEN),
            iex_bar("SPY", EARLY_CLOSE, 500.0),  # at/after 13:00: outside the session
            sip_bar("SPY", EARLY_CLOSE, 1.0),
        ],
    )
    r = run(store, config, target=EARLY_NOW.date(), now=EARLY_NOW)["SPY"]
    assert r.status is DQStatus.PASS
    assert r.details["ref_date"] == EARLY_DAY.isoformat()
    assert r.details["n_session_minutes"] == 210
    assert r.at_time == EARLY_CLOSE


def test_current_unfinished_date_is_excluded(store: LocalBackend, config: Config) -> None:
    mon_open = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)  # Monday = today at run time
    put(
        store,
        config,
        pairs("SPY", 0, 10, 100.0, 100.0) + pairs("SPY", 0, 10, 200.0, 100.0, mon_open),
    )
    r = run(store, config)["SPY"]
    assert r.details["ref_date"] == "2026-11-13"
    assert r.status is DQStatus.PASS


def test_only_configured_symbols(store: LocalBackend, config: Config) -> None:
    put(store, config, pairs("AAPL", 0, 10, 100.0, 100.0))
    out = run(store, config)
    assert set(out) == set(config.crosschecks.iex_vs_sip.symbols)
    assert "AAPL" not in out


def test_registered_and_dq_rows_mirror(store: LocalBackend, config: Config) -> None:
    assert isinstance(registered()["iex_vs_sip"], IexVsSip)
    put(store, config, pairs("SPY", 0, 10, 100.0, 100.0))
    ctx = make_ctx(config, store)
    xdf, ddf = run_crosschecks(ctx, duck_for(store, config), only=["iex_vs_sip"])
    assert len(xdf) == len(ddf) == 5
    assert set(ddf["check_type"]) == {"consistency"}
