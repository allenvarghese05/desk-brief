"""T07 follow-ups: waiting, oil calendars, stale anomaly series, Yahoo / Twelve Data completeness.

SYNTHETIC FIXTURE: all observations are made up; times sit around the configured publish times
(stooq 20:00 ET, treasury 18:00 ET, fred 16:30 ET, grace 60 min). One table-driven test per
card bullet, each with an early-close case where P-011 applies.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from checks_support import ET_NOW, as_of_1d, ctx_at, duck_for, find, obs, write_obs

from deskbrief.checks.anomaly import AnomalyCheck
from deskbrief.checks.completeness import CompletenessCheck
from deskbrief.checks.freshness import FreshnessCheck
from deskbrief.models import DQStatus, JobKind

S = DQStatus
WED = date(2026, 11, 18)
GOOD_FRIDAY = date(2026, 4, 3)  # XNYS and oil closed; SIFMA open with a recommended early close
EASTER_MONDAY = date(2026, 4, 6)  # TARGET and ICE Brent closed; SIFMA and XNYS open


def _stooq(d: date) -> dict[str, Any]:
    return obs(as_of=as_of_1d(d))


def _treasury(d: date) -> dict[str, Any]:
    return obs(
        as_of=as_of_1d(d), source="treasury", dataset="par_curve", instrument_id="UST_10Y",
        symbol="10 Yr", field="yield", unit="pct", value=4.1,
    )  # fmt: skip


# -- bullet 1: freshness and completeness agree while waiting ---------------------------------


@pytest.mark.parametrize(
    ("source", "day", "hhmm", "rows", "fresh", "comp_present", "comp_other"),
    [
        ("stooq", WED, "20:30", [], S.SKIP, None, S.SKIP),  # due, within grace, no data
        ("stooq", WED, "20:30", [_stooq], S.PASS, S.PASS, S.SKIP),  # data already in
        ("treasury", GOOD_FRIDAY, "18:30", [], S.SKIP, None, S.SKIP),  # early close: same
    ],
)
def test_due_but_within_grace_skips_waiting_in_both(  # type: ignore[no-untyped-def]
    store, config, source, day, hhmm, rows, fresh, comp_present, comp_other
) -> None:
    write_obs(store, config, [r(day) for r in rows])
    ctx = ctx_at(config, store, ET_NOW(day, hhmm))
    duck = duck_for(store, config)
    f = find(FreshnessCheck().evaluate(ctx, duck), source=source)
    c = find(CompletenessCheck().evaluate(ctx, duck), source=source)
    assert [r.status for r in f] == [fresh]
    if fresh is S.SKIP:
        assert f[0].details["reason"] == "waiting"
        assert {r.details["reason"] for r in c} == {"waiting"}
    by = {r.instrument_id: r.status for r in c}
    if comp_present:
        assert by["SPY"] is comp_present
    assert {s for i, s in by.items() if i != "SPY"} == {comp_other}


# -- bullet 4: FRED oil on days SIFMA is open but the oil market is closed --------------------


@pytest.mark.parametrize(
    ("pub_day", "wti", "brent", "ust"),
    [
        (WED, S.FAIL, S.FAIL, S.FAIL),  # ordinary day: missing oil fails like any series
        (date(2026, 4, 6), S.SKIP, S.SKIP, S.WARN),  # data day Good Friday: oil shut, early close
        (date(2026, 4, 7), S.FAIL, S.SKIP, S.FAIL),  # data day Easter Monday: only Brent shut
    ],
)
def test_fred_oil_skipped_when_oil_market_closed(store, config, pub_day, wti, brent, ust) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_at(config, store, ET_NOW(pub_day, "23:00"))
    res = find(CompletenessCheck().evaluate(ctx, duck_for(store, config)), source="fred")
    by = {r.instrument_id: r for r in res}
    assert (by["WTI"].status, by["BRENT"].status, by["UST_10Y"].status) == (wti, brent, ust)
    for oil in (by["WTI"], by["BRENT"]):
        if oil.status is S.SKIP:
            assert oil.details["reason"] == "oil market closed"


# -- bullet 5: anomaly scores only the expected data day ---------------------------------------


def walk(end: date, **over: Any) -> list[dict[str, Any]]:
    """33 daily rows ending on ``end`` whose last move is a clear outlier (z >> 5)."""
    unit = over.get("unit", "USD")
    steps = [0.004, -0.006, 0.005, -0.007, 0.003] * 6 + [0.004, -0.005] + [0.5]
    value, rows = 100.0 if unit == "USD" else 4.0, []
    for i, step in enumerate(steps):
        value = value * (1 + step) if unit == "USD" else value + step
        d = end - timedelta(days=len(steps) - 1 - i)
        rows.append(
            obs(
                as_of=as_of_1d(d), value=value, ingested_at=datetime(2026, 3, 1, tzinfo=UTC), **over
            )
        )
    return rows


UST = {
    "source": "treasury", "dataset": "par_curve", "instrument_id": "UST_10Y", "symbol": "10 Yr",
    "field": "yield", "unit": "pct",
}  # fmt: skip
BRENT = {
    "source": "fred", "dataset": "series", "instrument_id": "BRENT", "symbol": "DCOILBRENTEU",
}  # fmt: skip


@pytest.mark.parametrize(
    ("over", "end", "run_day", "job", "stale"),
    [
        ({}, date(2026, 10, 1), date(2026, 10, 2), JobKind.EOD, False),  # newest day is expected
        ({}, date(2026, 10, 1), date(2026, 10, 5), JobKind.EOD, True),  # stopped updating
        # P-011: Good Friday is a SIFMA early close, so Thursday is the expected Treasury day
        (UST, date(2026, 4, 2), date(2026, 4, 6), JobKind.MORNING, False),
        (UST, date(2026, 4, 1), date(2026, 4, 6), JobKind.MORNING, True),
        # Brent skips Good Friday and Easter Monday: Thursday 2 April is the newest it can have
        (BRENT, date(2026, 4, 2), date(2026, 4, 8), JobKind.MORNING, False),
        (BRENT, date(2026, 4, 1), date(2026, 4, 8), JobKind.MORNING, True),
    ],
)
def test_anomaly_scores_only_the_expected_data_day(  # type: ignore[no-untyped-def]
    store, config, over, end, run_day, job, stale
) -> None:
    write_obs(store, config, walk(end, **over))
    ctx = ctx_at(config, store, ET_NOW(run_day, "07:00"), job=job)
    (res,) = AnomalyCheck().evaluate(ctx, duck_for(store, config))
    if stale:
        assert res.status is S.SKIP
        assert res.details["reason"] == "stale series"
    else:
        assert res.status is S.FAIL  # the outlier is scored: it belongs to the expected day


# -- bullet 3: Yahoo and Twelve Data completeness ----------------------------------------------

DAY = date(2026, 11, 17)  # ordinary Tuesday; session 14:30-21:00 UTC
EARLY = date(2026, 11, 27)  # day after Thanksgiving: XNYS closes 13:00 ET = 18:00 UTC


def _ctx(config, store, target: date):  # type: ignore[no-untyped-def]
    return ctx_at(config, store, datetime(2026, 12, 1, 12, tzinfo=UTC), target=target)


def _tick(source: str, dataset: str, inst: str, symbol: str, at: datetime, **kw: Any) -> Any:
    unit = "fx_rate" if source == "twelvedata" else "USD"
    return obs(
        source=source, dataset=dataset, instrument_id=inst, symbol=symbol, interval="tick",
        field="last", as_of=at, tier=2, unit=unit, ingested_at=datetime(2026, 12, 1, tzinfo=UTC),
        **kw,
    )  # fmt: skip


def test_yahoo_history_needs_a_bar_for_the_session_day(store, config) -> None:  # type: ignore[no-untyped-def]
    bar = obs(
        source="yahoo", dataset="history", instrument_id="ES_F", symbol="ES=F", tier=2,
        unit="index_pts", as_of=as_of_1d(DAY), ingested_at=datetime(2026, 12, 1, tzinfo=UTC),
    )  # fmt: skip
    write_obs(store, config, [bar])
    res = CompletenessCheck().evaluate(_ctx(config, store, DAY), duck_for(store, config))
    by = {r.instrument_id: r for r in find(res, source="yahoo", dataset="history")}
    assert by["ES_F"].status is S.PASS
    assert by["WTI"].status is S.FAIL
    assert by["N225"].status is S.SKIP
    assert by["N225"].details["reason"] == "exchange calendar not modelled"
    holiday = CompletenessCheck().evaluate(
        _ctx(config, store, date(2026, 11, 26)), duck_for(store, config)
    )
    assert {r.status for r in find(holiday, source="yahoo", dataset="history")} == {S.SKIP}


@pytest.mark.parametrize(
    ("day", "tick_utc", "status"),
    [
        (DAY, datetime(2026, 11, 17, 20, 59, tzinfo=UTC), S.PASS),
        (DAY, datetime(2026, 11, 17, 21, 30, tzinfo=UTC), S.FAIL),  # after the close
        (EARLY, datetime(2026, 11, 27, 17, 30, tzinfo=UTC), S.PASS),
        (EARLY, datetime(2026, 11, 27, 19, 0, tzinfo=UTC), S.FAIL),  # after the early close
    ],
)
def test_tick_completeness_window_ends_at_the_early_close(
    store, config, day, tick_utc, status
) -> None:  # type: ignore[no-untyped-def]
    write_obs(store, config, [_tick("yahoo", "quote", "SPY", "SPY", tick_utc)])
    res = CompletenessCheck().evaluate(_ctx(config, store, day), duck_for(store, config))
    by = {r.instrument_id: r for r in find(res, source="yahoo", dataset="quote")}
    assert by["SPY"].status is status
    assert by["QQQ"].status is S.FAIL  # no tick at all
    assert by["N225"].status is S.SKIP
    assert by["SPY"].details["early_close"] is (day == EARLY)


def test_twelvedata_quote_completeness_per_pair(store, config) -> None:  # type: ignore[no-untyped-def]
    at = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
    write_obs(store, config, [_tick("twelvedata", "fx_quote", "EURUSD", "EUR/USD", at)])
    res = CompletenessCheck().evaluate(_ctx(config, store, DAY), duck_for(store, config))
    by = {r.instrument_id: r.status for r in find(res, source="twelvedata", dataset="fx_quote")}
    assert by == {"EURUSD": S.PASS, "GBPUSD": S.FAIL, "USDJPY": S.FAIL}


@pytest.mark.parametrize(
    ("day", "fix_utc", "bars", "status"),
    [
        (DAY, datetime(2026, 11, 17, 13, 5, tzinfo=UTC), 21, S.PASS),  # 14:15 CET = 13:15 UTC
        (DAY, datetime(2026, 11, 17, 13, 5, tzinfo=UTC), 20, S.WARN),  # 95.2%
        (DAY, datetime(2026, 11, 17, 13, 5, tzinfo=UTC), 19, S.FAIL),  # 90.5%
        (EARLY, datetime(2026, 11, 27, 13, 5, tzinfo=UTC), 21, S.PASS),  # XNYS early close: n/a
    ],
)
def test_twelvedata_fix_window_completeness(store, config, day, fix_utc, bars, status) -> None:  # type: ignore[no-untyped-def]
    rows = [
        obs(source="twelvedata", dataset="fx_series_1m", instrument_id="EURUSD", symbol="EUR/USD",
            interval="1m", tier=1, unit="fx_rate", as_of=fix_utc + timedelta(minutes=i),
            ingested_at=datetime(2026, 12, 1, tzinfo=UTC))
        for i in range(bars)
    ]  # fmt: skip
    write_obs(store, config, rows)
    res = CompletenessCheck().evaluate(_ctx(config, store, day), duck_for(store, config))
    by = {r.instrument_id: r for r in find(res, source="twelvedata", dataset="fx_series_1m")}
    assert by["EURUSD"].status is status
    assert by["EURUSD"].details["expected_bars"] == 21
    assert by["GBPUSD"].status is S.FAIL


@pytest.mark.parametrize(
    ("target", "hhmm", "fix_day"),
    [
        (WED, "17:30", "2026-11-18"),  # eod: the window has closed
        (WED, "07:00", "2026-11-17"),  # morning: today's window is still ahead
        (date(2026, 12, 25), "17:30", "2026-12-24"),  # TARGET holiday: last open day
    ],
)
def test_twelvedata_fix_day(store, config, target, hhmm, fix_day) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_at(config, store, ET_NOW(target, hhmm), target=target)
    res = CompletenessCheck().evaluate(ctx, duck_for(store, config))
    got = {r.details["fix_day"] for r in find(res, source="twelvedata", dataset="fx_series_1m")}
    assert got == {fix_day}
