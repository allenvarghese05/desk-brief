"""T25c: completeness expectations that failed on every run (live run 3, 2026-11-18).

SYNTHETIC FIXTURE: all observations are made up.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from checks_support import ET_NOW, as_of_1d, ctx_at, duck_for, find, obs, write_obs

from deskbrief.calendars import add_business_days, prev_business_day
from deskbrief.checks.completeness import NOT_PLANNED, CompletenessCheck
from deskbrief.models import DQStatus, JobKind
from deskbrief.sources.alpaca import bar_symbols
from deskbrief.sources.fred import OIL_LAG_BDAYS

S = DQStatus
WED = date(2026, 11, 18)


def test_minute_completeness_skips_bar_feeds_the_plan_never_fetches(store, config) -> None:  # type: ignore[no-untyped-def]
    """AAPL, MSFT, ... have ``sip_bars_1m`` feeds in config, but the Alpaca plan fetches bars only
    for ``crosschecks.iex_vs_sip.symbols``: those feeds were 7 of run 3's Alpaca fails."""
    ctx = ctx_at(config, store, datetime(2026, 12, 1, 12, tzinfo=UTC), target=date(2026, 11, 17))
    res = find(CompletenessCheck().evaluate(ctx, duck_for(store, config)), source="alpaca")
    minute = [r for r in res if r.details.get("dataset") in ("iex_bars_1m", "sip_bars_1m")]
    unplanned = {
        (r.instrument_id, r.details["dataset"])
        for r in minute
        if r.details.get("reason") == NOT_PLANNED
    }
    assert ("AAPL", "sip_bars_1m") in unplanned
    assert all(r.status is S.SKIP for r in minute if r.details.get("reason") == NOT_PLANNED)
    judged = {(r.instrument_id, r.details["dataset"]) for r in minute if r.status is not S.SKIP}
    expected = {(s, d) for d in ("iex_bars_1m", "sip_bars_1m") for s in bar_symbols(config, d)}
    assert judged == expected  # every planned feed is still judged (no data here -> fail)
    assert {r.status for r in minute if r.status is not S.SKIP} == {S.FAIL}
    assert len(judged) + len(unplanned) == len(minute)


def test_unplanned_bar_feeds_stay_skipped_on_closed_days(store, config) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_at(config, store, datetime(2026, 12, 1, 12, tzinfo=UTC), target=date(2026, 11, 26))
    res = find(
        CompletenessCheck().evaluate(ctx, duck_for(store, config)),
        source="alpaca",
        instrument_id="AAPL",
        dataset="sip_bars_1m",
    )
    assert [r.details["reason"] for r in res] == [NOT_PLANNED]


def fred_row(inst: str, symbol: str, day: date) -> dict[str, object]:
    return obs(
        instrument_id=inst,
        source="fred",
        dataset="series",
        symbol=symbol,
        field="close" if symbol.startswith("DCOIL") else "yield",
        as_of=as_of_1d(day),
        ingested_at=datetime(2026, 11, 18, 0, tzinfo=UTC),
    )


def test_fred_oil_series_are_judged_on_the_weekly_eia_lag(store, config) -> None:  # type: ignore[no-untyped-def]
    """Replay of run 3: a manual morning run at 21:24 ET on Tue 2026-11-17. DGS10 has Friday's
    value; the oil series' newest value is a week older (EIA publishes weekly)."""
    now = datetime(2026, 11, 18, 2, 24, tzinfo=UTC)
    target = date(2026, 11, 17)
    pub = prev_business_day("SIFMA_US", target)
    oil_day = add_business_days("SIFMA_US", pub, -OIL_LAG_BDAYS)
    write_obs(
        store,
        config,
        [
            fred_row("UST_10Y", "DGS10", date(2026, 11, 13)),
            fred_row("WTI", "DCOILWTICO", oil_day),
            fred_row("BRENT", "DCOILBRENTEU", oil_day),
        ],
    )
    ctx = ctx_at(config, store, now, target=target, job=JobKind.MORNING)
    res = {
        r.instrument_id: r
        for r in find(CompletenessCheck().evaluate(ctx, duck_for(store, config)), source="fred")
    }
    assert res["UST_10Y"].status is S.PASS
    assert res["UST_10Y"].details["data_day"] == "2026-11-13"
    for inst in ("WTI", "BRENT"):
        assert res[inst].status is S.PASS
        assert res[inst].details["data_day"] == oil_day.isoformat()
    assert res["UST_2Y"].status is S.FAIL  # the other daily series keep the 1-day lag


def test_fred_oil_series_still_fail_when_their_lagged_day_is_missing(store, config) -> None:  # type: ignore[no-untyped-def]
    write_obs(store, config, [fred_row("WTI", "DCOILWTICO", date(2026, 10, 1))])  # far too old
    ctx = ctx_at(config, store, ET_NOW(WED, "23:00"))
    res = find(CompletenessCheck().evaluate(ctx, duck_for(store, config)), source="fred")
    oil = {r.instrument_id: r.status for r in res if r.instrument_id in ("WTI", "BRENT")}
    assert oil == {"WTI": S.FAIL, "BRENT": S.FAIL}


@pytest.mark.parametrize("symbol", ["DGS2", "DGS10", "DGS20", "DGS30", "DFF"])
def test_other_daily_fred_series_have_no_own_lag(symbol: str) -> None:
    from deskbrief.sources.fred import SERIES_SPECS

    assert SERIES_SPECS[symbol].lag_bdays is None
