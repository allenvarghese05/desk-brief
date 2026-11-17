"""T07C: ``feed_completeness`` covers ``alpaca/iex_latest`` (presence in the XNYS session).

SYNTHETIC FIXTURE: every tick is made up. Alpaca's ``as_of`` is the last-trade time, so an
illiquid ETF can have an old tick: the rule asks only that the instrument appears inside the
session, never that the tick is recent. Session rules match the Yahoo and Twelve Data tick rules.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from checks_support import ET_NOW, ctx_at, duck_for, find, obs, write_obs

from deskbrief.checks.completeness import CompletenessCheck
from deskbrief.models import DQStatus, JobKind

S = DQStatus
DAY = date(2026, 11, 17)  # ordinary Tuesday; session 14:30-21:00 UTC
EARLY = date(2026, 11, 27)  # XNYS closes 13:00 ET = 18:00 UTC
HOLIDAY = date(2026, 11, 26)  # Thanksgiving
LATER = datetime(2026, 12, 1, tzinfo=UTC)


def _tick(inst: str, at: datetime, **over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "source": "alpaca", "dataset": "iex_latest", "instrument_id": inst, "symbol": inst,
        "interval": "tick", "field": "last", "as_of": at, "tier": 1, "unit": "USD",
        "ingested_at": LATER,
    }  # fmt: skip
    row.update(over)
    return obs(**row)


def _run(store, config, target, rows, now=None, job=JobKind.EOD):  # type: ignore[no-untyped-def]
    write_obs(store, config, rows)
    ctx = ctx_at(
        config, store, now or datetime(2026, 12, 1, 12, tzinfo=UTC), target=target, job=job
    )
    res = CompletenessCheck().evaluate(ctx, duck_for(store, config))
    return {r.instrument_id: r for r in find(res, source="alpaca", dataset="iex_latest")}


def test_every_configured_alpaca_instrument_is_judged(store, config) -> None:  # type: ignore[no-untyped-def]
    by = _run(store, config, DAY, [])
    configured = {i.id for i, _ in config.feeds_for("alpaca", "iex_latest")}
    assert set(by) == configured
    assert len(configured) > 1
    assert {r.status for r in by.values()} == {S.FAIL}


def test_missing_in_session_fails_and_present_passes(store, config) -> None:  # type: ignore[no-untyped-def]
    rows = [_tick("SPY", datetime(2026, 11, 17, 20, 59, tzinfo=UTC))]
    by = _run(store, config, DAY, rows)
    assert by["SPY"].status is S.PASS
    assert by["SPY"].observed == 1.0
    assert by["QQQ"].status is S.FAIL
    assert by["QQQ"].observed == 0.0


def test_old_tick_inside_session_still_passes(store, config) -> None:  # type: ignore[no-untyped-def]
    # Illiquid ETF: its only tick is the session's first minute, hours before the close.
    by = _run(store, config, DAY, [_tick("BNO", datetime(2026, 11, 17, 14, 31, tzinfo=UTC))])
    assert by["BNO"].status is S.PASS


def test_only_ticks_outside_the_session_do_not_count(store, config) -> None:  # type: ignore[no-untyped-def]
    rows = [
        _tick("SPY", datetime(2026, 11, 17, 13, 0, tzinfo=UTC)),  # pre-market
        _tick("QQQ", datetime(2026, 11, 17, 21, 30, tzinfo=UTC)),  # after the close
        _tick("IWM", datetime(2026, 11, 16, 20, 0, tzinfo=UTC)),  # the day before
    ]
    by = _run(store, config, DAY, rows)
    assert {by[i].status for i in ("SPY", "QQQ", "IWM")} == {S.FAIL}


def test_pre_open_run_judges_the_previous_session_not_todays_premarket(store, config) -> None:  # type: ignore[no-untyped-def]
    # 07:00 ET on the target day: the finished session is the day before (P-009).
    now = ET_NOW(DAY, "07:00")
    rows = [
        _tick("SPY", datetime(2026, 11, 16, 20, 0, tzinfo=UTC), ingested_at=now),  # prior session
        _tick("QQQ", datetime(2026, 11, 17, 11, 0, tzinfo=UTC), ingested_at=now),  # pre-market
    ]
    by = _run(store, config, DAY, rows, now=now, job=JobKind.MORNING)
    assert by["SPY"].status is S.PASS
    assert by["QQQ"].status is S.FAIL
    assert by["SPY"].details["session_day"] == "2026-11-16"


@pytest.mark.parametrize(
    ("tick_utc", "status"),
    [
        (datetime(2026, 11, 27, 17, 30, tzinfo=UTC), S.PASS),
        (datetime(2026, 11, 27, 19, 0, tzinfo=UTC), S.FAIL),  # after the early close
    ],
)
def test_early_close_ends_the_window(store, config, tick_utc, status) -> None:  # type: ignore[no-untyped-def]
    by = _run(store, config, EARLY, [_tick("SPY", tick_utc)])
    assert by["SPY"].status is status
    assert by["SPY"].details["early_close"] is True


def test_holiday_skips_with_reason(store, config) -> None:  # type: ignore[no-untyped-def]
    by = _run(store, config, HOLIDAY, [_tick("SPY", datetime(2026, 11, 26, 16, 0, tzinfo=UTC))])
    assert {r.status for r in by.values()} == {S.SKIP}
    assert all(r.details["reason"] == "holiday" for r in by.values())


def test_other_alpaca_datasets_and_sources_do_not_count(store, config) -> None:  # type: ignore[no-untyped-def]
    at = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
    rows = [
        _tick("SPY", at, dataset="iex_bars_1m"),
        _tick("QQQ", at, source="yahoo", dataset="quote"),
    ]
    by = _run(store, config, DAY, rows)
    assert by["SPY"].status is S.FAIL
    assert by["QQQ"].status is S.FAIL
