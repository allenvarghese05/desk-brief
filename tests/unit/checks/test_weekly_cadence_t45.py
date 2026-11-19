"""Weekly cadence in the checks (card T45, P-021): freshness and completeness of ``1w`` datasets,
validity of ``index`` and ``contracts`` rows, and the anomaly check on daily ``level`` rows.

SYNTHETIC FIXTURE: every observation is made up (see ``weekly_synth``). Dates: the CFTC-like
dataset releases on Fridays at 15:30 ET (grace 60 min) with data as of the Tuesday; Thanksgiving
(Thu 26 Nov 2026) delays the 27 Nov release to Monday 30 Nov.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest
from checks_support import ET_NOW, find

from deskbrief.calendars import add_business_days, is_business_day
from deskbrief.checks.anomaly import AnomalyCheck, daily_moves
from deskbrief.checks.completeness import CompletenessCheck
from deskbrief.checks.freshness import DELAYED, FreshnessCheck, weekly_expected
from deskbrief.checks.validity import ValidityCheck
from deskbrief.config import Config
from deskbrief.models import DQStatus
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from weekly_synth import broad, ctx_at, net, nfci, oi, weekly_config, write

S = DQStatus
FRI = date(2026, 11, 20)  # an ordinary release Friday
TUE = date(2026, 11, 17)  # its reference date
PREV_TUE = date(2026, 11, 10)  # last week's reference date
TG_FRI = date(2026, 11, 27)  # release week with Thanksgiving
TG_MON = date(2026, 11, 30)  # the delayed release day
TG_TUE = date(2026, 11, 24)


@pytest.fixture(scope="module")
def wconfig() -> Config:
    return weekly_config()


def cot_rows(d: date, ingested_day: date) -> list[dict[str, object]]:
    at = ET_NOW(ingested_day, "15:35")
    return [net(d, -125_000.0, at), oi(d, 480_000.0, at)]


def fresh(store: LocalBackend, config: Config, day: date, hhmm: str):  # type: ignore[no-untyped-def]
    ctx = ctx_at(config, store, ET_NOW(day, hhmm))
    res = find(FreshnessCheck().evaluate(ctx, connect(store, config)), source="cftc")
    assert len(res) == 1
    return res[0]


def complete(store: LocalBackend, config: Config, day: date, hhmm: str):  # type: ignore[no-untyped-def]
    ctx = ctx_at(config, store, ET_NOW(day, hhmm))
    return find(CompletenessCheck().evaluate(ctx, connect(store, config)), source="cftc")


# --- which release is judged -----------------------------------------------------------------


def test_weekly_expected_on_an_ordinary_friday(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    sched = next(s for s in wconfig.datasets if s.source == "cftc")
    after = weekly_expected(ctx_at(wconfig, store, ET_NOW(FRI, "16:00")), sched)
    assert (after.exp.publication_day, after.exp.data_day) == (FRI, TUE)
    assert after.data_from > PREV_TUE  # last week's reference date never counts
    assert after.holidays == 0
    assert after.exp.skip_reason is None
    before = weekly_expected(ctx_at(wconfig, store, ET_NOW(FRI, "10:00")), sched)
    assert before.scheduled_day == FRI - timedelta(days=7)  # this week's is not due yet
    # Veterans Day (Wed 11 Nov, SIFMA closed) delays that release to Monday 16 Nov
    assert (before.holidays, before.exp.publication_day) == (1, date(2026, 11, 16))
    sunday = weekly_expected(
        ctx_at(wconfig, store, ET_NOW(FRI + timedelta(days=2), "12:00")), sched
    )
    assert sunday.exp.publication_day == FRI


def test_weekly_expected_shifts_a_holiday_week(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    sched = next(s for s in wconfig.datasets if s.source == "cftc")
    w = weekly_expected(ctx_at(wconfig, store, ET_NOW(TG_FRI, "18:00")), sched)
    assert w.holidays == 1
    assert w.scheduled_day == TG_FRI
    assert w.exp.publication_day == add_business_days("SIFMA_US", TG_FRI, 1) == TG_MON
    assert w.exp.skip_reason == DELAYED


# --- freshness -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hhmm", "have", "status", "word"),
    [
        ("16:00", TUE, S.PASS, "fresh"),
        ("16:00", None, S.SKIP, "waiting"),  # 30 min late, inside grace
        ("17:00", PREV_TUE, S.WARN, "late"),  # 90 min: last week's row does not count
        ("18:00", PREV_TUE, S.FAIL, "late"),  # 150 min > 2 x grace
        ("10:00", PREV_TUE, S.PASS, "fresh"),  # before 15:30: last week's release is judged
    ],
)
def test_weekly_freshness_ladder(store, wconfig, hhmm, have, status, word) -> None:  # type: ignore[no-untyped-def]
    if have:
        write(store, wconfig, cot_rows(have, have + timedelta(days=3)))
    r = fresh(store, wconfig, FRI, hhmm)
    assert r.status is status
    assert r.details.get("state", r.details.get("reason")) == word
    assert r.details["interval"] == "1w"


def test_weekly_freshness_ignores_daily_rows(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    """A ``1d`` row of the weekly dataset is not a weekly release."""
    stray = {**net(TUE, 1.0, ET_NOW(FRI, "15:35")), "interval": "1d"}
    write(store, wconfig, [stray])
    assert fresh(store, wconfig, FRI, "18:00").status is S.FAIL


def test_holiday_delayed_release_is_skip_not_fail(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    r = fresh(store, wconfig, TG_FRI, "18:00")
    assert r.status is S.SKIP
    assert r.details["reason"] == DELAYED
    assert r.details["release_day"] == TG_MON.isoformat()
    sat = fresh(store, wconfig, TG_FRI + timedelta(days=1), "12:00")
    assert sat.status is S.SKIP  # a week without its release on time is never a fail


def test_holiday_delayed_release_is_judged_after_its_delayed_due(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    assert fresh(store, wconfig, TG_MON, "16:00").details["reason"] == "waiting"
    assert fresh(store, wconfig, TG_MON, "18:00").status is S.FAIL
    write(store, wconfig, cot_rows(TG_TUE, TG_FRI))  # published on the usual Friday after all
    assert fresh(store, wconfig, TG_FRI, "18:00").status is S.PASS  # present: pass, not skip
    assert fresh(store, wconfig, TG_MON, "18:00").status is S.PASS


def test_weekly_wednesday_release(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    """NFCI-like: Wednesday 08:30 release of the Friday before (``release_weekday`` 2)."""
    wed, fri = date(2026, 11, 18), date(2026, 11, 13)
    ctx = ctx_at(wconfig, store, ET_NOW(wed, "11:00"))
    res = find(FreshnessCheck().evaluate(ctx, connect(store, wconfig)), dataset="series_weekly")
    assert [r.status for r in res] == [S.FAIL]
    write(store, wconfig, [nfci(fri, -0.52, ET_NOW(wed, "08:35"))])
    res = find(FreshnessCheck().evaluate(ctx, connect(store, wconfig)), dataset="series_weekly")
    assert [r.status for r in res] == [S.PASS]
    assert res[0].details["data_day"] == fri.isoformat()


# --- completeness ----------------------------------------------------------------------------


def test_weekly_completeness_counts_the_week_per_feed(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    write(store, wconfig, [net(TUE, -125_000.0, ET_NOW(FRI, "15:35"))])  # no open interest
    res = {r.instrument_id: r for r in complete(store, wconfig, FRI, "18:00")}
    assert res["GC_NET"].status is S.PASS
    assert res["GC_NET"].details["weeks"] == 1
    assert res["GC_OI"].status is S.FAIL
    assert res["GC_OI"].details["weeks"] == 0
    assert res["GC_OI"].details["data_from"] == (TUE - timedelta(days=6)).isoformat()


def test_weekly_completeness_last_week_is_not_this_week(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    write(store, wconfig, cot_rows(PREV_TUE, PREV_TUE + timedelta(days=3)))
    assert {r.status for r in complete(store, wconfig, FRI, "18:00")} == {S.FAIL}


@pytest.mark.parametrize(
    ("day", "hhmm", "reason"),
    [(TG_FRI, "18:00", DELAYED), (FRI, "16:00", "waiting"), (FRI, "10:00", None)],
)
def test_weekly_completeness_skips_like_freshness(store, wconfig, day, hhmm, reason) -> None:  # type: ignore[no-untyped-def]
    write(store, wconfig, cot_rows(PREV_TUE, PREV_TUE + timedelta(days=3)))
    res = complete(store, wconfig, day, hhmm)
    assert len(res) == 2
    if reason is None:  # before Friday's due time last week's release is judged, and present
        assert {r.status for r in res} == {S.PASS}
    else:
        assert {(r.status, r.details["reason"]) for r in res} == {(S.SKIP, reason)}


# --- validity on the new units (index may be negative) ------------------------------------------


def test_validity_on_index_and_contracts_rows(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    at = ET_NOW(FRI, "15:35")
    rows = [
        nfci(date(2026, 11, 13), -0.52, at),  # negative NFCI: inside [-100, 100000]
        net(TUE, -125_000.0, at),  # short net position: inside [-1e7, 1e8]
        oi(TUE, 480_000.0, at),
    ]
    write(store, wconfig, rows)
    ctx = ctx_at(wconfig, store, ET_NOW(FRI, "18:00"))
    res = ValidityCheck().evaluate(ctx, connect(store, wconfig))
    by = {(r.source, r.instrument_id): r.status for r in res}
    assert by[("fred", "NFCI")] is S.PASS
    assert by[("cftc", "GC_NET")] is S.PASS
    assert by[("cftc", "GC_OI")] is S.PASS


def test_validity_rejects_out_of_range_index_and_contracts(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    """INJECTED FAULT: an NFCI below -100 and an open interest above 1e8 contracts."""
    at = ET_NOW(FRI, "15:35")
    write(store, wconfig, [nfci(date(2026, 11, 13), -150.0, at), oi(TUE, 2.5e8, at)])
    ctx = ctx_at(wconfig, store, ET_NOW(FRI, "18:00"))
    res = ValidityCheck().evaluate(ctx, connect(store, wconfig))
    by = {(r.source, r.instrument_id): r for r in res}
    assert by[("fred", "NFCI")].status is S.FAIL
    assert by[("cftc", "GC_OI")].status is S.FAIL


# --- anomaly: daily level yes, weekly no -------------------------------------------------------


def test_anomaly_covers_daily_level_and_skips_weekly(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    """``level`` joined ``DAILY_CLOSE_FIELDS`` (T45), so a daily broad-dollar series is scored;
    weekly rows are never read by the anomaly check (``1d`` only)."""
    span = (date(2026, 10, 1) + timedelta(days=n) for n in range(60))
    days = [x for x in span if is_business_day("SIFMA_US", x)][:30]
    values = [120.0 + (0.1 if i % 2 else -0.1) for i in range(len(days))]
    values[-1] = values[-2] * 1.05  # INJECTED FAULT: a 5% jump in a dollar index
    rows = [broad(x, v, ET_NOW(x, "17:00")) for x, v in zip(days, values, strict=True)]
    rows += [nfci(x, -0.5 + i * 0.3, ET_NOW(x, "17:00")) for i, x in enumerate(days[::5])]
    write(store, wconfig, rows)
    last = days[-1]
    ctx = ctx_at(wconfig, store, ET_NOW(add_business_days("SIFMA_US", last, 1), "18:00"))
    res = AnomalyCheck().evaluate(ctx, connect(store, wconfig))
    level = [r for r in res if r.instrument_id == "USD_BROAD"]
    assert len(level) == 1
    assert level[0].details["field"] == "level"
    assert level[0].status in (S.WARN, S.FAIL)
    assert not [r for r in res if r.instrument_id == "NFCI"]


def test_daily_moves_never_take_a_return_from_a_base_at_or_below_zero() -> None:
    moves = daily_moves(np.array([-0.5, -0.4, 0.0, 0.2, 0.3]), "index")
    assert np.isnan(moves[:3]).all()  # bases -0.5, -0.4 and 0: no relative return
    np.testing.assert_allclose(moves[3], 0.5)
    np.testing.assert_allclose(daily_moves(np.array([100.0, 110.0]), "index_pts"), [0.1])


def test_anomaly_skips_a_daily_level_whose_latest_base_is_not_positive(store, wconfig) -> None:  # type: ignore[no-untyped-def]
    """A level that crosses zero is never scored on a relative return (skip, not a z-score)."""
    span = (date(2026, 10, 1) + timedelta(days=n) for n in range(60))
    days = [x for x in span if is_business_day("SIFMA_US", x)][:30]
    values = [1.0 + (0.1 if i % 2 else -0.1) for i in range(len(days))]
    values[-2], values[-1] = -0.2, 5.0  # INJECTED FAULT: base below zero, then a large jump
    rows = [broad(x, v, ET_NOW(x, "17:00")) for x, v in zip(days, values, strict=True)]
    write(store, wconfig, rows)
    ctx = ctx_at(wconfig, store, ET_NOW(add_business_days("SIFMA_US", days[-1], 1), "18:00"))
    res = [
        r
        for r in AnomalyCheck().evaluate(ctx, connect(store, wconfig))
        if r.instrument_id == "USD_BROAD"
    ]
    assert [r.status for r in res] == [S.SKIP]
    assert res[0].details["reason"] == "history too short"
