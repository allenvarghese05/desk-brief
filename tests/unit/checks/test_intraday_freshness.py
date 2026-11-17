"""Intraday tick-age freshness (T07B, spec §8.1).

SYNTHETIC FIXTURE: every tick is made up; times are placed around the configured XNYS session
(09:30-16:00 ET, 13:00 close on 2026-11-27) and the 30 minute grace of ``intraday:`` entries.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from checks_support import ET_NOW, ctx_at, duck_for, find, obs, write_obs

from deskbrief.checks import run_checks
from deskbrief.checks.base import registered_checks
from deskbrief.checks.freshness import IntradayFreshnessCheck
from deskbrief.config import IntradaySchedule
from deskbrief.models import DQStatus, JobKind

S = DQStatus
WED = date(2026, 11, 18)  # ordinary XNYS day, session 09:30-16:00 ET
EARLY = date(2026, 11, 27)  # day after Thanksgiving, XNYS closes 13:00 ET
THANKSGIVING = date(2026, 11, 26)
SATURDAY = date(2026, 11, 21)
FEED = {"source": "alpaca", "dataset": "iex_latest"}


def tick(at: datetime, **over: object) -> dict[str, object]:
    """SYNTHETIC FIXTURE: one made-up Alpaca IEX last-trade tick observed at ``at``."""
    base = {"interval": "tick", "field": "last", "as_of": at, "ingested_at": at, **FEED}
    base.update(over)
    return obs(**base)


def run(store, config, day, hhmm, rows=(), job=JobKind.INTRADAY):  # type: ignore[no-untyped-def]
    write_obs(store, config, list(rows))
    ctx = ctx_at(config, store, ET_NOW(day, hhmm), job=job)
    out = IntradayFreshnessCheck().evaluate(ctx, duck_for(store, config))
    return find(out, **FEED), out


@pytest.mark.parametrize(
    ("age_s", "status", "state"),
    [
        (60, S.PASS, "fresh"),
        (30 * 60, S.PASS, "fresh"),  # inclusive at grace, like within_grace
        (30 * 60 + 1, S.WARN, "stale"),
        (60 * 60, S.WARN, "stale"),  # exactly 2 x grace is still a warn
        (60 * 60 + 1, S.FAIL, "stale"),
    ],
)
def test_tick_age_ladder(store, config, age_s, status, state) -> None:  # type: ignore[no-untyped-def]
    now = ET_NOW(WED, "11:00")
    res, _ = run(store, config, WED, "11:00", [tick(now - timedelta(seconds=age_s))])
    assert [r.status for r in res] == [status]
    assert res[0].details["state"] == state
    assert res[0].observed == pytest.approx(age_s / 60)
    assert res[0].threshold == 30.0


def test_newest_tick_wins_and_other_feeds_do_not_count(store, config) -> None:  # type: ignore[no-untyped-def]
    now = ET_NOW(WED, "11:00")
    rows = [
        tick(now - timedelta(minutes=50)),
        tick(now - timedelta(minutes=5), instrument_id="QQQ"),  # newest across instruments
        tick(now - timedelta(minutes=1), source="yahoo", dataset="quote"),  # another entry
        tick(now - timedelta(minutes=1), ingested_at=now + timedelta(minutes=1)),  # after now
    ]
    res, out = run(store, config, WED, "11:00", rows)
    assert [r.status for r in res] == [S.PASS]
    assert res[0].observed == pytest.approx(5.0)
    assert len(out) == len(config.intraday) == 3  # one result per configured entry


def test_no_ticks_in_session_fails(store, config) -> None:  # type: ignore[no-untyped-def]
    res, _ = run(store, config, WED, "11:00")
    assert [r.status for r in res] == [S.FAIL]
    assert res[0].details["state"] == "no_ticks"
    assert res[0].observed is None


def test_only_yesterdays_tick_is_stale_not_fresh(store, config) -> None:  # type: ignore[no-untyped-def]
    old = ET_NOW(WED - timedelta(days=1), "15:59")
    res, _ = run(store, config, WED, "10:30", [tick(old)])
    assert [r.status for r in res] == [S.FAIL]


@pytest.mark.parametrize(
    ("day", "hhmm", "reason"),
    [
        (WED, "09:29", "pre-open"),
        (WED, "09:59", "warm-up"),  # open + grace is 10:00
        (WED, "16:01", "after close"),
        (EARLY, "13:01", "after close"),  # early close (P-011): 13:00, not 16:00
        (EARLY, "09:45", "warm-up"),
        (THANKSGIVING, "11:00", "holiday"),
        (SATURDAY, "11:00", "weekend"),
    ],
)
def test_outside_evaluation_window_skips(store, config, day, hhmm, reason) -> None:  # type: ignore[no-untyped-def]
    now = ET_NOW(day, hhmm)
    res, out = run(store, config, day, hhmm, [tick(now - timedelta(minutes=1))])
    assert [r.status for r in res] == [S.SKIP]
    assert reason in str(res[0].details["reason"])
    assert {r.status for r in out} == {S.SKIP}


@pytest.mark.parametrize(("day", "hhmm"), [(WED, "10:00"), (WED, "16:00"), (EARLY, "13:00")])
def test_window_edges_are_evaluated(store, config, day, hhmm) -> None:  # type: ignore[no-untyped-def]
    now = ET_NOW(day, hhmm)
    res, _ = run(store, config, day, hhmm, [tick(now - timedelta(minutes=1))])
    assert [r.status for r in res] == [S.PASS]


def test_early_close_day_before_the_early_close_is_evaluated(store, config) -> None:  # type: ignore[no-untyped-def]
    res, _ = run(store, config, EARLY, "12:59")
    assert [r.status for r in res] == [S.FAIL]
    assert res[0].details["early_close"] is True


@pytest.mark.parametrize("job", [JobKind.MORNING, JobKind.EOD, JobKind.BACKFILL, JobKind.REBUILD])
def test_only_the_intraday_job_runs_it(store, config, job) -> None:  # type: ignore[no-untyped-def]
    _, out = run(store, config, WED, "11:00", job=job)
    assert out == []


def test_unmodelled_calendar_skips_with_reason(store, config) -> None:  # type: ignore[no-untyped-def]
    entry = IntradaySchedule(**FEED, calendar="TARGET")
    cfg = config.model_copy(update={"intraday": (entry,)})
    ctx = ctx_at(cfg, store, ET_NOW(WED, "11:00"), job=JobKind.INTRADAY)
    (res,) = IntradayFreshnessCheck().evaluate(ctx, duck_for(store, cfg))
    assert res.status is S.SKIP
    assert "TARGET" in str(res.details["reason"])


def test_registered_and_run_by_run_checks(store, config) -> None:  # type: ignore[no-untyped-def]
    assert "freshness_intraday" in {c.name for c in registered_checks()}
    ctx = ctx_at(config, store, ET_NOW(WED, "11:00"), job=JobKind.INTRADAY)
    dq, _ = run_checks(ctx, duck_for(store, config))
    assert "freshness_intraday" in set(dq["check_name"])


def test_future_dated_tick_is_ignored_by_the_as_of_guard(store, config) -> None:  # type: ignore[no-untyped-def]
    """``as_of <= now`` in ``newest_tick``: ``ingested_at`` alone would let this tick through."""
    now = ET_NOW(WED, "11:00")
    future = tick(now + timedelta(minutes=5), ingested_at=now - timedelta(minutes=1))
    res, _ = run(store, config, WED, "11:00", [future])
    assert [r.status for r in res] == [S.FAIL]
    assert res[0].details["state"] == "no_ticks"


def test_future_dated_tick_does_not_hide_an_older_one(store, config) -> None:  # type: ignore[no-untyped-def]
    now = ET_NOW(WED, "11:00")
    future = tick(now + timedelta(minutes=5), ingested_at=now - timedelta(minutes=1))
    older = tick(now - timedelta(minutes=10), ingested_at=now - timedelta(minutes=1))
    res, _ = run(store, config, WED, "11:00", [future, older])
    assert [r.status for r in res] == [S.PASS]
    assert res[0].observed == pytest.approx(10.0)
