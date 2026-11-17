"""Freshness, calendar and completeness checks (spec §8.1).

SYNTHETIC FIXTURE: all observations are made up; times are chosen around the configured
publish times (stooq 20:00 ET XNYS, treasury 18:00 ET SIFMA_US, grace 60 min).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from checks_support import ET_NOW, as_of_1d, ctx_at, duck_for, find, obs, write_obs

from deskbrief.checks.calendar_check import CalendarCheck
from deskbrief.checks.completeness import CompletenessCheck
from deskbrief.checks.freshness import FreshnessCheck, late_status
from deskbrief.models import DQStatus, JobKind

WED = date(2026, 11, 18)  # ordinary XNYS/SIFMA day
S = DQStatus


@pytest.mark.parametrize(
    ("late", "expected"),
    [(0, S.PASS), (60, S.PASS), (60.1, S.WARN), (120, S.WARN), (120.1, S.FAIL)],
)
def test_late_status_boundaries(late: float, expected: DQStatus) -> None:
    assert late_status(late, 60) is expected


def stooq(d: date, **kw: object) -> dict[str, object]:
    return obs(as_of=as_of_1d(d), **kw)


@pytest.mark.parametrize(
    ("hhmm", "have_day", "status", "state"),
    [
        ("17:30", None, S.SKIP, None),  # before expected_by_et: "not yet due"
        ("19:59", None, S.SKIP, None),
        ("20:30", WED, S.PASS, "fresh"),
        ("21:30", None, S.WARN, "late"),  # 90 min late
        ("22:30", None, S.FAIL, "late"),  # 150 min late > 2 x grace
        ("22:30", WED - timedelta(days=1), S.FAIL, "late"),  # stale: only yesterday present
    ],
)
def test_freshness_stooq(store, config, hhmm, have_day, status, state) -> None:  # type: ignore[no-untyped-def]
    if have_day:
        write_obs(store, config, [stooq(have_day)])
    ctx = ctx_at(config, store, ET_NOW(WED, hhmm))
    res = find(FreshnessCheck().evaluate(ctx, duck_for(store, config)), source="stooq")
    assert [r.status for r in res] == [status]
    if state:
        assert res[0].details["state"] == state
    else:
        assert res[0].details["reason"] == "not yet due"


def test_freshness_ignores_data_ingested_after_now(store, config) -> None:  # type: ignore[no-untyped-def]
    """obs_pit(now): a row ingested after the evaluation time does not count."""
    late_row = stooq(WED, ingested_at=ET_NOW(WED, "23:00"))
    write_obs(store, config, [late_row])
    ctx = ctx_at(config, store, ET_NOW(WED, "22:30"))
    res = find(FreshnessCheck().evaluate(ctx, duck_for(store, config)), source="stooq")
    assert res[0].status is S.FAIL


def test_freshness_lag_bdays_uses_previous_business_day(store, config) -> None:  # type: ignore[no-untyped-def]
    """FRED (lag 1, due 16:30): on Monday the data day is Friday."""
    mon = date(2026, 11, 16)
    fri = date(2026, 11, 13)
    row = {
        "source": "fred",
        "dataset": "series",
        "instrument_id": "UST_10Y",
        "symbol": "DGS10",
        "field": "yield",
        "unit": "pct",
        "value": 4.1,
    }
    write_obs(store, config, [obs(as_of=as_of_1d(fri), **row)])
    ctx = ctx_at(config, store, ET_NOW(mon, "19:30"))
    res = find(FreshnessCheck().evaluate(ctx, duck_for(store, config)), source="fred")
    assert res[0].status is S.PASS
    assert res[0].details["data_day"] == fri.isoformat()


@pytest.mark.parametrize(
    ("target", "job", "cal_status"),
    [
        (date(2026, 11, 26), JobKind.EOD, S.SKIP),  # Thanksgiving: XNYS closed
        (date(2026, 11, 21), JobKind.EOD, S.SKIP),  # Saturday
    ],
)
def test_closed_days_skip_not_fail(store, config, target, job, cal_status) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_at(config, store, ET_NOW(target + timedelta(days=2), "23:00"), target=target, job=job)
    duck = duck_for(store, config)
    fresh = find(FreshnessCheck().evaluate(ctx, duck), source="stooq")
    comp = find(CompletenessCheck().evaluate(ctx, duck), source="stooq")
    cal = find(CalendarCheck().evaluate(ctx, duck), calendar="XNYS")
    assert [r.status for r in fresh] == [S.SKIP]
    assert fresh[0].details["reason"] in ("holiday", "weekend")
    assert comp
    assert {r.status for r in comp} == {S.SKIP}
    assert [r.status for r in cal] == [cal_status]


def test_calendar_pass_and_flags(store, config) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_at(config, store, ET_NOW(WED, "12:00"))
    res = CalendarCheck().evaluate(ctx, duck_for(store, config))
    assert {r.details["calendar"] for r in res} == {"XNYS", "SIFMA_US", "TARGET"}
    assert {r.status for r in res} == {S.PASS}
    # 2026-11-26: Thanksgiving closes XNYS and SIFMA but not TARGET
    ctx2 = ctx_at(config, store, ET_NOW(WED, "12:00"), target=date(2026, 11, 26))
    by_cal = {
        r.details["calendar"]: r.status
        for r in CalendarCheck().evaluate(ctx2, duck_for(store, config))
    }
    assert by_cal == {"XNYS": S.SKIP, "SIFMA_US": S.SKIP, "TARGET": S.PASS}
    # day after Thanksgiving is an XNYS early close
    ctx3 = ctx_at(config, store, ET_NOW(WED, "12:00"), target=date(2026, 11, 27))
    xnys = find(CalendarCheck().evaluate(ctx3, duck_for(store, config)), calendar="XNYS")
    assert xnys[0].details["early_close"] is True


GOOD_FRIDAY = date(2026, 4, 3)  # XNYS closed; SIFMA recommends an early close (open day)


def test_sifma_early_close_missing_value_is_warn_not_fail(store, config) -> None:  # type: ignore[no-untyped-def]
    now = ET_NOW(GOOD_FRIDAY, "23:30")  # > 2 x grace after 18:00
    ctx = ctx_at(config, store, now)
    duck = duck_for(store, config)
    fresh = find(FreshnessCheck().evaluate(ctx, duck), source="treasury")
    assert [r.status for r in fresh] == [S.WARN]
    assert fresh[0].details["state"] == "late_early_close"
    comp = find(CompletenessCheck().evaluate(ctx, duck), source="treasury")
    assert comp
    assert {r.status for r in comp} == {S.WARN}
    # the day before is a normal day: same lateness fails
    ctx2 = ctx_at(config, store, ET_NOW(date(2026, 4, 2), "23:30"))
    fresh2 = find(FreshnessCheck().evaluate(ctx2, duck), source="treasury")
    assert [r.status for r in fresh2] == [S.FAIL]
    # while XNYS-calendar Stooq is simply skipped on Good Friday
    stooq_res = find(FreshnessCheck().evaluate(ctx, duck), source="stooq")
    assert [r.status for r in stooq_res] == [S.SKIP]


def test_completeness_daily_per_feed(store, config) -> None:  # type: ignore[no-untyped-def]
    write_obs(store, config, [stooq(WED), stooq(WED, instrument_id="QQQ", symbol="qqq.us")])
    ctx = ctx_at(config, store, ET_NOW(WED, "21:30"))  # past due + grace
    res = find(CompletenessCheck().evaluate(ctx, duck_for(store, config)), source="stooq")
    by_inst = {r.instrument_id: r.status for r in res}
    assert by_inst["SPY"] is S.PASS
    assert by_inst["QQQ"] is S.PASS
    assert by_inst["AAPL"] is S.FAIL  # configured feed with no data
    assert len(res) == len(config.feeds_for("stooq", "daily"))


def test_completeness_waits_for_grace_and_skips_monthly(store, config) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_at(config, store, ET_NOW(WED, "20:30"))  # due, but within grace
    duck = duck_for(store, config)
    stooq_res = find(CompletenessCheck().evaluate(ctx, duck), source="stooq")
    assert {r.status for r in stooq_res} == {S.SKIP}
    # monthly FRED series are never daily feeds
    ctx2 = ctx_at(config, store, ET_NOW(WED, "23:00"))
    fred = find(CompletenessCheck().evaluate(ctx2, duck), source="fred")
    assert {r.instrument_id for r in fred}.isdisjoint({"CPI", "UNRATE"})
    assert {"DFF", "UST_10Y"} <= {r.instrument_id for r in fred}


def minute_rows(day: date, n: int, *, start_utc: datetime) -> list[dict[str, object]]:
    return [
        obs(
            source="alpaca",
            dataset="iex_bars_1m",
            symbol="SPY",
            interval="1m",
            tier=1,
            as_of=start_utc + timedelta(minutes=i),
            ingested_at=datetime(2026, 12, 1, tzinfo=UTC),
        )
        for i in range(n)
    ]


@pytest.mark.parametrize(
    ("bars", "status"),
    [(390, S.PASS), (371, S.WARN), (370, S.FAIL), (0, S.FAIL), (300, S.FAIL)],
)
def test_minute_completeness_full_session(store, config, bars, status) -> None:  # type: ignore[no-untyped-def]
    day = date(2026, 11, 17)
    open_utc = datetime(2026, 11, 17, 14, 30, tzinfo=UTC)  # 09:30 EST
    write_obs(store, config, minute_rows(day, bars, start_utc=open_utc))
    ctx = ctx_at(config, store, datetime(2026, 12, 1, 12, 0, tzinfo=UTC), target=day)
    res = find(
        CompletenessCheck().evaluate(ctx, duck_for(store, config)),
        source="alpaca",
        instrument_id="SPY",
        dataset="iex_bars_1m",
    )
    assert [r.status for r in res] == [status]
    assert res[0].details["expected_bars"] == 390


@pytest.mark.parametrize(
    ("bars", "status"), [(210, S.PASS), (200, S.WARN), (199, S.FAIL), (390, S.PASS)]
)
def test_minute_completeness_honours_early_close(store, config, bars, status) -> None:  # type: ignore[no-untyped-def]
    day = date(2026, 11, 27)  # day after Thanksgiving: closes 13:00 ET = 210 minutes
    open_utc = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    write_obs(store, config, minute_rows(day, bars, start_utc=open_utc))
    ctx = ctx_at(config, store, datetime(2026, 12, 1, 12, 0, tzinfo=UTC), target=day)
    res = find(
        CompletenessCheck().evaluate(ctx, duck_for(store, config)),
        source="alpaca",
        instrument_id="SPY",
        dataset="iex_bars_1m",
    )
    assert res[0].details["expected_bars"] == 210
    assert [r.status for r in res] == [status]


def test_minute_completeness_uses_last_finished_session(store, config) -> None:  # type: ignore[no-untyped-def]
    """Monday morning: target is today (unfinished, P-009) so the Friday session is judged."""
    mon = date(2026, 11, 16)
    ctx = ctx_at(config, store, ET_NOW(mon, "07:00"), job=JobKind.MORNING)
    res = find(
        CompletenessCheck().evaluate(ctx, duck_for(store, config)),
        source="alpaca",
        instrument_id="SPY",
        dataset="iex_bars_1m",
    )
    assert res[0].details["session_day"] == "2026-11-13"
    # a holiday target in the past is skipped
    ctx2 = ctx_at(config, store, datetime(2026, 12, 1, 12, tzinfo=UTC), target=date(2026, 11, 26))
    res2 = find(CompletenessCheck().evaluate(ctx2, duck_for(store, config)), source="alpaca")
    assert {r.status for r in res2} == {S.SKIP}


def test_morning_job_judges_previous_business_day(store, config) -> None:  # type: ignore[no-untyped-def]
    mon = date(2026, 11, 16)
    ctx = ctx_at(config, store, ET_NOW(mon, "07:00"), job=JobKind.MORNING)
    res = find(FreshnessCheck().evaluate(ctx, duck_for(store, config)), source="treasury")
    # data day Friday 11-13, due Friday 18:00 (passed); nothing stored -> late by ~13h -> fail
    assert res[0].details["data_day"] == "2026-11-13"
    assert res[0].status is S.FAIL
