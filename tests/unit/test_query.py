"""Read-only query API: snapshot, overnight changes, series, lineage, health.

SYNTHETIC FIXTURE: all data comes from ``brief_synth`` (made-up values, labelled there).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from brief_synth import FRI, MON, MON_0700, THU, WED, World, daily, et, monday_rows, tick
from deskbrief import query
from deskbrief.calendars import is_business_day
from deskbrief.timeutil import et_date


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.observations(monday_rows())
    return w


def change_of(changes: tuple[query.ChangeRow, ...], iid: str) -> query.ChangeRow:
    return next(c for c in changes if c.last.instrument_id == iid)


def test_snapshot_rows_carry_provenance_fields(world: World) -> None:
    snap = {r.instrument_id: r for r in query.latest_snapshot(world.connect(), MON_0700)}
    es = snap["ES_F"]
    assert (es.value, es.source, es.tier, es.field, es.unit) == (
        5976.0,
        "yahoo",
        2,
        "last",
        "index_pts",
    )
    assert es.as_of == et(MON, "06:59")
    assert es.is_proxy is False
    assert snap["SPY"].is_proxy is True
    assert snap["SPY"].proxy_label is not None
    assert "proxy" in snap["SPY"].proxy_label
    assert snap["SPY"].tier == 1


def test_snapshot_is_point_in_time_and_ignores_unfinished_day(world: World) -> None:
    # a restated Friday close is only visible once ingested
    world.observations([daily("ES_F", FRI, 5000.0, source="yahoo", dataset="history", tier=2,
                              unit="index_pts", ingested_at=et(MON, "09:00"))])  # fmt: skip
    conn = world.connect()
    before = query.overnight_changes(conn, MON, MON_0700)
    assert change_of(before, "ES_F").previous is not None
    assert change_of(before, "ES_F").previous.value == 6000.0  # type: ignore[union-attr]
    after = query.overnight_changes(conn, MON, et(MON, "10:00"))
    assert change_of(after, "ES_F").previous.value == 5000.0  # type: ignore[union-attr]
    # Monday's own 1d row (55.0, ET date == report date) is never "the last value"
    wti = {r.instrument_id: r for r in query.latest_snapshot(conn, MON_0700)}["WTI"]
    assert wti.value == 71.20


def test_overnight_change_monday_uses_friday_close(world: World) -> None:
    es = change_of(query.overnight_changes(world.connect(), MON, MON_0700), "ES_F")
    assert es.basis == "previous NYSE close"
    assert es.previous is not None
    assert es.previous.as_of == et(FRI)
    assert es.change_unit == "pct"
    assert es.change == pytest.approx(-0.4)
    assert es.family == "price"


def test_overnight_change_yield_is_bp_vs_previous_official_value(world: World) -> None:
    y = change_of(query.overnight_changes(world.connect(), MON, MON_0700), "UST_10Y")
    assert (y.family, y.basis, y.change_unit) == ("yield", "previous official value", "bp")
    assert y.change == pytest.approx(-3.0)
    assert y.previous is not None
    assert y.previous.tier == 3
    assert y.last.tier == 2  # the delayed ^TNX-style tick is the last value


def test_official_only_series_compares_with_the_value_before(world: World) -> None:
    y = change_of(query.overnight_changes(world.connect(), MON, MON_0700), "UST_2Y")
    assert y.last.as_of == et(FRI)
    assert y.previous is not None
    assert y.previous.as_of == et(THU)
    assert y.change == pytest.approx(-2.0)


def test_ecb_rate_is_a_reference_rate_not_a_close(world: World) -> None:
    fx = change_of(query.overnight_changes(world.connect(), MON, MON_0700), "EURUSD")
    assert (fx.last.field, fx.last.source) == ("rate", "ecb")
    assert fx.family == "rate"
    assert fx.basis == "previous official value"
    assert fx.change_unit == "pct"
    assert fx.change == pytest.approx((1.0842 / 1.0800 - 1) * 100)
    assert query.field_label(fx.last) == "ECB reference rate"
    assert "close" not in query.value_label(fx.last).lower()


def test_no_move_yet_when_last_value_is_the_baseline(world: World) -> None:
    vix = change_of(query.overnight_changes(world.connect(), MON, MON_0700), "VIX")
    assert (vix.status, vix.change, vix.change_unit) == ("no_move_yet", None, "pts")


def test_missing_baseline_is_reported_not_guessed(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations([tick("WTI", et(MON, "06:58"), 71.2), daily("WTI", THU, 70.0)])  # no Friday
    wti = change_of(query.overnight_changes(w.connect(), MON, MON_0700), "WTI")
    assert (wti.status, wti.change, wti.previous) == ("no_baseline", None, None)


def test_holiday_gap_uses_previous_nyse_session(tmp_path: Path) -> None:
    thanksgiving_fri = date(2026, 11, 27)  # NYSE closed Thu 26; previous close is Wed 25
    w = World(tmp_path)
    w.observations(
        [
            daily("WTI", date(2026, 11, 24), 50.0),
            daily("WTI", date(2026, 11, 25), 100.0),
            tick("WTI", et(thanksgiving_fri, "06:50"), 98.0),
        ]
    )
    wti = change_of(
        query.overnight_changes(w.connect(), thanksgiving_fri, et(thanksgiving_fri, "07:00")), "WTI"
    )
    assert wti.previous is not None
    assert wti.previous.value == 100.0
    assert wti.change == pytest.approx(-2.0)


def test_measure_units(world: World) -> None:
    snap = {r.instrument_id: r for r in query.latest_snapshot(world.connect(), MON_0700)}
    bp, unit = query.measure(snap["UST_10Y"], 4.40, 4.37)
    assert (bp, unit) == (pytest.approx(-3.0), "bp")
    assert query.measure(snap["ES_F"], 100.0, 101.0) == (pytest.approx(1.0), "pct")
    assert query.measure(snap["VIX"], 16.0, 16.6) == (pytest.approx(0.6), "pts")
    assert query.measure(snap["EURUSD"], 0.0, 1.0) == (None, "pct")  # missing, not 0%


def test_series_one_value_per_day_oldest_first_without_current_day(world: World) -> None:
    rows = query.series(world.connect(), "WTI", 30, MON_0700)
    assert [r.as_of for r in rows] == [et(FRI)]  # Monday's unfinished 1d row excluded
    es = query.series(world.connect(), "ES_F", 30, MON_0700)
    assert [r.value for r in es] == [7000.0, 6024.0, 6000.0]
    assert [r.as_of for r in es] == [et(WED), et(THU), et(FRI)]
    assert query.series(world.connect(), "ES_F", 4, MON_0700) == es[-1:]  # Fri only
    assert query.series(world.connect(), "NOPE", 30, MON_0700) == ()
    assert set(query.series_all(world.connect(), 30, MON_0700)) >= {"ES_F", "UST_10Y", "WTI"}


def test_series_prefers_tier_three_for_the_same_day(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("SPY", FRI, 1.0, tier=2, source="yahoo", dataset="history"),
            daily("SPY", FRI, 2.0, tier=3),
        ]
    )
    [row] = query.series(w.connect(), "SPY", 10, MON_0700)
    assert (row.value, row.tier) == (2.0, 3)


def test_lineage_joins_the_source_log(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations([tick("ES_F", et(MON, "06:59"), 5976.0, unit="index_pts", fetch_id="FETCH-ES")])
    w.log_fetch("FETCH-ES", et(MON, "06:59"), "raw/yahoo/quote/x.json")
    lin = query.lineage(w.connect(), "ES_F", MON_0700)
    assert lin is not None
    assert (lin.value.fetch_id, lin.raw_key, lin.fetch_status) == (
        "FETCH-ES",
        "raw/yahoo/quote/x.json",
        "ok",
    )
    assert (lin.latency_ms, lin.http_status, lin.rows_parsed) == (1000, 200, 1)
    assert lin.value.parser_version == "synthetic-1"
    assert query.lineage(w.connect(), "WTI", MON_0700) is None


def test_lineage_without_source_log_row_has_empty_fetch_fields(world: World) -> None:
    lin = query.lineage(world.connect(), "ES_F", MON_0700)
    assert lin is not None
    assert (lin.raw_key, lin.fetch_status, lin.latency_ms) == (None, None, None)


def test_health_summary_counts_issues_and_sources(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.dq(
        MON,
        [
            ("fresh_a", "pass", "treasury", None),
            ("fresh_b", "pass", "yahoo", None),
            ("range_c", "warn", None, "WTI"),
            ("fresh_d", "fail", "stooq", None),
            ("anom_e", "skip", None, "VIX"),
        ],
    )
    w.xchecks(MON, [("iex_vs_sip", "pass", 0.6), ("sp500_return", "fail", 12.0)])
    w.fetches(
        MON, [("treasury", "ok"), ("yahoo", "ok"), ("ecb", "error"), ("cboe", "skipped_calendar")]
    )
    w.run(MON, "degraded")
    h = query.health_summary(
        w.connect(), MON, expected_sources=["treasury", "ecb", "cboe", "stooq"]
    )
    assert (h.n_pass, h.n_warn, h.n_fail, h.n_skip) == (2, 1, 1, 1)
    assert (h.n_xcheck_pass, h.n_xcheck_warn, h.n_xcheck_fail) == (1, 0, 1)
    assert h.n_checks == 7
    assert [(i.kind, i.status, i.name) for i in h.issues] == [
        ("crosscheck", "fail", "sp500_return"),
        ("dq", "fail", "fresh_d"),
        ("dq", "warn", "range_c"),
    ]
    assert h.run_status == "degraded"
    status = {s.source: s.status for s in h.sources}
    assert status == {
        "treasury": "ok",
        "ecb": "failed",
        "cboe": "skipped",
        "stooq": "missing",
        "yahoo": "ok",
    }


def test_health_summary_counts_a_rerun_check_once(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.dq(MON, [("fresh_a", "fail", "stooq", None)], at="07:00")
    w.dq(MON, [("fresh_a", "pass", "stooq", None)], at="08:00")  # later evaluation, same check
    h = query.health_summary(w.connect(), MON)
    assert (h.n_pass, h.n_fail) == (1, 0)


def test_health_summary_on_empty_store(tmp_path: Path) -> None:
    h = query.health_summary(World(tmp_path).connect(), MON)
    assert h.n_checks == 0
    assert h.issues == ()
    assert h.run_status is None
    assert {s.status for s in h.sources} == {"missing"}  # every configured source


def test_derived_cross_is_not_called_a_reference_rate(tmp_path: Path) -> None:
    w = World(tmp_path)
    ecb = {"source": "ecb", "dataset": "eurofxref", "field": "rate", "unit": "fx_rate"}
    w.observations(
        [
            daily("GBPUSD", THU, 1.30, is_derived=True, **ecb),
            daily("GBPUSD", FRI, 1.31, is_derived=True, **ecb),
            daily("EURUSD", FRI, 1.08, **ecb),
        ]
    )
    snap = {r.instrument_id: r for r in query.latest_snapshot(w.connect(), MON_0700)}
    assert snap["GBPUSD"].is_derived
    assert query.field_label(snap["GBPUSD"]) == "ECB derived cross rate"
    assert query.field_label(snap["EURUSD"]) == "ECB reference rate"


def test_zero_baseline_gives_missing_change(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations([daily("WTI", FRI, 0.0), tick("WTI", et(MON, "06:58"), 5.0)])
    ch = change_of(query.overnight_changes(w.connect(), MON, MON_0700), "WTI")
    assert ch.change is None
    assert ch.status == "no_baseline"


def test_health_summary_counts_a_mirrored_crosscheck_once(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.dq(MON, [("fresh_a", "pass", "treasury", None)])
    w.xchecks(MON, [("sp500_spy_es", "warn", 7.0), ("iex_vs_sip", "pass", 0.6)], mirror=True)
    h = query.health_summary(w.connect(), MON)
    assert (h.n_pass, h.n_warn, h.n_fail, h.n_skip) == (1, 0, 0, 0)  # mirrors left out of dq
    assert (h.n_xcheck_pass, h.n_xcheck_warn, h.n_xcheck_fail) == (1, 1, 0)
    assert h.n_checks == 3
    assert [(i.kind, i.name) for i in h.issues] == [("crosscheck", "sp500_spy_es")]


def test_crosscheck_item_carries_ref_date_not_target_date(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.xchecks(MON, [("a", "warn", 7.0)], ref=THU)
    w.xchecks(MON, [("b", "fail", 9.0)])  # default ref: previous NYSE session
    issues = {i.name: i for i in query.health_summary(w.connect(), MON).issues}
    assert issues["a"].ref_date == THU
    assert issues["b"].ref_date == FRI
    w2 = World(tmp_path / "none")
    w2.dq(MON, [("fresh", "warn", "yahoo", None)])
    assert query.health_summary(w2.connect(), MON).issues[0].ref_date is None


def test_tick_against_official_baseline_is_noted(world: World) -> None:
    changes = query.overnight_changes(world.connect(), MON, MON_0700)
    note = change_of(changes, "UST_10Y").note
    assert note is not None
    assert "yahoo" in note
    assert "treasury" in note
    assert change_of(changes, "UST_2Y").note is None  # official against official: nothing to say


def test_lineage_rows_have_a_role(world: World) -> None:
    conn = world.connect()
    snap = query.latest_snapshot(conn, MON_0700)
    assert {x.role for x in query.lineages(conn, snap)} == {"value"}
    assert {x.role for x in query.lineages(conn, snap, role="baseline")} == {"baseline"}


def test_baseline_from_another_source_is_noted(tmp_path: Path) -> None:
    w = World(tmp_path)
    ecb = {"dataset": "eurofxref", "field": "rate", "unit": "fx_rate"}
    w.observations(
        [
            daily("EURUSD", THU, 1.00, source="ecb", **ecb),
            daily("EURUSD", FRI, 1.10, source="stooq", tier=3, **{**ecb, "dataset": "daily"}),
        ]
    )
    ch = change_of(query.overnight_changes(w.connect(), MON, MON_0700), "EURUSD")
    assert ch.previous is not None
    assert ch.note is not None
    assert "ecb" in ch.note


def test_exclude_tiers_falls_back_to_the_best_remaining_value(world: World) -> None:
    db = world.connect()
    assert {r.instrument_id: r.tier for r in query.latest_snapshot(db, MON_0700)}["SPY"] == 1
    snap = {r.instrument_id: r for r in query.latest_snapshot(db, MON_0700, exclude_tiers=(1,))}
    spy = snap["SPY"]  # hidden tier-1 tick: the Friday official close stands in, not "no data"
    assert (spy.tier, spy.value, spy.interval) == (3, 598.50, "1d")
    assert snap["ES_F"].tier == 2  # tiers that are not excluded are untouched
    rest = {r.instrument_id for r in query.latest_snapshot(db, MON_0700, exclude_tiers=(1, 2))}
    assert "ES_F" not in rest  # every ES_F row is tier 2: absent, as if it had no data
    assert "EURUSD" in rest


def test_exclude_tiers_applies_to_overnight_changes_and_baselines(world: World) -> None:
    db = world.connect()
    spy = change_of(query.overnight_changes(db, MON, MON_0700, exclude_tiers=(1,)), "SPY")
    assert (spy.last.tier, spy.status) == (3, "no_move_yet")  # last value is Friday's close
    ten = change_of(query.overnight_changes(db, MON, MON_0700, exclude_tiers=(3,)), "UST_10Y")
    assert ten.last.tier == 2  # the ^TNX-style tick
    assert (ten.previous, ten.status) == (None, "no_baseline")  # official baselines are tier 3
    assert change_of(query.overnight_changes(db, MON, MON_0700), "UST_10Y").status == "ok"


def test_health_summary_reads_only_the_latest_run(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.dq(MON, [("fresh_a", "fail", "stooq", None), ("old_only", "warn", None, "WTI")], run_id="R1")
    w.dq(MON, [("fresh_a", "pass", "stooq", None)], at="08:00", run_id="R2")
    w.xchecks(MON, [("sp500_return", "fail", 12.0)], run_id="R1")
    w.xchecks(MON, [("sp500_return", "pass", 1.0)], at="08:00", run_id="R2")
    h = query.health_summary(w.connect(), MON)
    # the rows of run R1 stay in the store (P-008) but R1 is superseded: nothing of it counts
    assert (h.n_pass, h.n_warn, h.n_fail, h.n_skip) == (1, 0, 0, 0)
    assert (h.n_xcheck_pass, h.n_xcheck_warn, h.n_xcheck_fail) == (1, 0, 0)
    assert h.issues == ()
    assert query.instrument_flags(w.connect(), MON) == {"SPY": "pass", "ES_F": "pass"}


def test_instrument_flags_take_the_worst_status_and_ignore_skip(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.dq(
        MON,
        [
            ("range", "fail", "stooq", "WTI"),
            ("fresh", "pass", "stooq", "WTI"),
            ("fresh", "pass", "yahoo", "VIX"),
            ("anom", "warn", None, "VIX"),
            ("anom", "skip", None, "EURUSD"),
            ("fresh", "fail", "stooq", None),  # a source-level check belongs to no instrument
        ],
    )
    w.xchecks(MON, [("sp500_return", "warn", 7.0)])  # SPY (left) against ES_F (right)
    assert query.instrument_flags(w.connect(), MON) == {
        "WTI": "fail",
        "VIX": "warn",
        "SPY": "warn",
        "ES_F": "warn",
    }
    assert query.instrument_flags(w.connect(), THU) == {}


def test_dq_and_crosschecks_come_from_one_run(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.dq(MON, [("fresh_a", "fail", "stooq", None)], run_id="R1")
    w.xchecks(MON, [("sp500_return", "warn", 7.0)], run_id="R1")
    # a later run wrote only cross-checks: it is the one run for both tables
    w.xchecks(MON, [("sp500_return", "pass", 1.0)], at="08:00", run_id="R2")
    h = query.health_summary(w.connect(), MON)
    assert (h.n_pass, h.n_warn, h.n_fail) == (0, 0, 0)  # nothing of R1's dq counts
    assert (h.n_xcheck_pass, h.n_xcheck_warn, h.n_xcheck_fail) == (1, 0, 0)
    assert h.issues == ()
    assert query.instrument_flags(w.connect(), MON) == {"SPY": "pass", "ES_F": "pass"}
    # and the other way round: a later dq-only run hides the earlier run's cross-checks
    w.dq(MON, [("fresh_a", "pass", "stooq", None)], at="09:00", run_id="R3")
    h = query.health_summary(w.connect(), MON)
    assert (h.n_pass, h.n_xcheck_pass, h.n_xcheck_warn) == (1, 0, 0)
    assert query.instrument_flags(w.connect(), MON) == {}


# ---------------------------------------------------------------------------
# T60 bug 4: weekends and XNYS holidays (SYNTHETIC values; dates are real calendar days)
# ---------------------------------------------------------------------------

SAT = date(2026, 11, 14)  # after FRI 13 Nov; the last XNYS session before it is FRI
SAT_1000 = et(SAT, "10:00")
YH_HIST = {"source": "yahoo", "dataset": "history", "tier": 2}


@pytest.mark.parametrize("exclude_tiers", [(), (1,)], ids=["local", "hosted"])
def test_saturday_daily_close_compares_friday_with_thursday(
    tmp_path: Path, exclude_tiers: tuple[int, ...]
) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("SPY", WED, 400.0),  # decoy
            daily("SPY", THU, 500.0),
            daily("SPY", FRI, 510.0),
        ]
    )
    db = w.connect()
    spy = change_of(query.overnight_changes(db, SAT, SAT_1000, exclude_tiers=exclude_tiers), "SPY")
    assert (spy.status, spy.basis) == ("ok", "Thu close")
    assert spy.last.as_of == et(FRI)
    assert et_date(spy.last.as_of) == FRI
    assert spy.previous is not None
    assert spy.previous.as_of == et(THU)
    assert spy.change == pytest.approx(2.0)


def _friday_after_close_world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.observations(
        [
            daily("SPY", THU, 500.0),
            daily("SPY", FRI, 510.0),
            tick("SPY", et(FRI, "16:10"), 515.0),  # tier-2 yahoo quote after the close
            tick("SPY", et(FRI, "16:30"), 520.0, tier=1, source="alpaca", dataset="iex_latest"),
        ]
    )
    return w


def test_saturday_friday_ticks_after_the_close_compare_with_thursday_local(
    tmp_path: Path,
) -> None:
    spy = change_of(
        query.overnight_changes(_friday_after_close_world(tmp_path).connect(), SAT, SAT_1000),
        "SPY",
    )
    assert (spy.last.tier, spy.last.value, et_date(spy.last.as_of)) == (1, 520.0, FRI)
    assert (spy.status, spy.basis) == ("ok", "Thu close")
    assert spy.previous is not None
    assert spy.previous.as_of == et(THU)  # not Friday's close: the tick is Friday's move
    assert spy.change == pytest.approx(4.0)


def test_saturday_friday_ticks_after_the_close_compare_with_thursday_hosted(
    tmp_path: Path,
) -> None:
    db = _friday_after_close_world(tmp_path).connect()
    spy = change_of(query.overnight_changes(db, SAT, SAT_1000, exclude_tiers={1}), "SPY")
    assert (spy.last.tier, spy.last.source, spy.last.value) == (2, "yahoo", 515.0)
    assert (spy.status, spy.basis) == ("ok", "Thu close")
    assert spy.previous is not None
    assert spy.previous.as_of == et(THU)
    assert spy.change == pytest.approx(3.0)


def test_mlk_monday_futures_tick_moves_since_friday_close(tmp_path: Path) -> None:
    mlk, thu, fri = date(2026, 1, 19), date(2026, 1, 15), date(2026, 1, 16)
    assert not is_business_day("XNYS", mlk)
    w = World(tmp_path)
    w.observations(
        [
            daily("ES_F", thu, 6000.0, unit="index_pts", **YH_HIST),
            daily("ES_F", fri, 6100.0, unit="index_pts", **YH_HIST),
            tick("ES_F", et(mlk, "06:59"), 6161.0, unit="index_pts"),
        ]
    )
    es = change_of(query.overnight_changes(w.connect(), mlk, et(mlk, "07:00")), "ES_F")
    assert (es.status, es.basis) == ("ok", "Fri close")
    assert es.previous is not None
    assert es.previous.as_of == et(fri)  # L's own close, not the session before it
    assert es.change == pytest.approx(1.0)


def test_thanksgiving_fx_quote_moves_since_wednesday_ny_close(tmp_path: Path) -> None:
    # T61: FX compares with its own quote at the previous New York close (17:00 ET), whatever the
    # NYSE does; a Yahoo daily FX bar is never the baseline (its close is the London-day open)
    tue, wed, thanksgiving = date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 26)
    w = World(tmp_path)
    w.observations(
        [
            daily("EURUSD", wed, 1.20, unit="fx_rate", **YH_HIST),  # decoy: not the NY close
            tick("EURUSD", et(tue, "17:30"), 1.00, unit="fx_rate"),  # Tuesday's close: too old
            tick("EURUSD", et(wed, "16:00"), 1.04, unit="fx_rate"),
            tick("EURUSD", et(wed, "17:30"), 1.05, unit="fx_rate"),  # nearest 17:00 Wednesday
            tick("EURUSD", et(thanksgiving, "06:00"), 1.071, unit="fx_rate"),
        ]
    )
    fx = change_of(
        query.overnight_changes(w.connect(), thanksgiving, et(thanksgiving, "07:00")), "EURUSD"
    )
    assert (fx.family, fx.status) == ("price", "ok")
    assert fx.basis == "previous NY close, 17:30 ET quote"
    assert fx.previous is not None
    assert fx.previous.as_of == et(wed, "17:30")
    assert fx.change == pytest.approx(2.0)


def test_good_friday_n225_close_seen_saturday_moves_since_thursday_close(tmp_path: Path) -> None:
    wed, thu, good_friday, sat = (date(2026, 4, d) for d in (1, 2, 3, 4))
    assert not is_business_day("XNYS", good_friday)
    w = World(tmp_path)
    w.observations(
        [daily("N225", d, v, unit="index_pts") for d, v in ((wed, 30000.0), (thu, 31000.0))]
        + [daily("N225", good_friday, 31620.0, unit="index_pts")]
    )
    n225 = change_of(query.overnight_changes(w.connect(), sat, et(sat, "10:00")), "N225")
    assert et_date(n225.last.as_of) == good_friday
    # T61: Tokyo traded on Good Friday; its previous Tokyo session is Thursday
    assert (n225.status, n225.basis) == ("ok", "previous Tokyo close")
    assert n225.previous is not None
    assert n225.previous.as_of == et(thu)
    assert n225.change == pytest.approx(2.0)


def test_saturday_treasury_yield_keeps_its_official_change(tmp_path: Path) -> None:
    w = World(tmp_path)
    tsy = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
    w.observations([daily("UST_10Y", THU, 4.45, **tsy), daily("UST_10Y", FRI, 4.40, **tsy)])
    y = change_of(query.overnight_changes(w.connect(), SAT, SAT_1000), "UST_10Y")
    assert (y.family, y.basis, y.status) == ("yield", query.BASIS_OFFICIAL, "ok")
    assert y.previous is not None
    assert y.previous.as_of == et(THU)
    assert y.change == pytest.approx(-5.0)


def test_weekday_before_the_morning_run_keeps_no_move_yet(tmp_path: Path) -> None:
    tue = date(2026, 11, 17)
    w = World(tmp_path)
    w.observations([daily("SPY", FRI, 500.0), daily("SPY", MON, 510.0)])
    spy = change_of(query.overnight_changes(w.connect(), tue, et(tue, "06:00")), "SPY")
    assert (spy.status, spy.basis, spy.change) == ("no_move_yet", query.BASIS_NYSE_CLOSE, None)


# ---------------------------------------------------------------------------
# T60 bug 5: the latest run, and health reads bounded by as_of
# ---------------------------------------------------------------------------


def _checks(w: World, target: date, run_id: str, at: datetime, status: str = "pass") -> None:
    """One dq row for ``target`` evaluated at any instant ``at`` (not only on ``target``)."""
    row = {
        "result_id": f"DQ-{run_id}-{at.isoformat()}",
        "run_id": run_id,
        "check_name": "fresh",
        "check_type": "freshness",
        "source": "yahoo",
        "instrument_id": "SPY",
        "target_date": target,
        "evaluated_at": at,
        "status": status,
        "observed": 1.0,
        "threshold": 1.0,
        "details_json": "{}",
    }
    w._append("dq_results", [row], lambda r: r["target_date"])


def _run_row(w: World, target: date, run_id: str, started: datetime, status: str = "ok") -> None:
    row: dict[str, Any] = {
        "run_id": run_id,
        "job": "morning",
        "trigger": "manual",
        "target_date": target,
        "started_at": started,
        "finished_at": started,
        "status": status,
        "git_sha": "deadbeef",
        "config_hash": "cafe",
        "n_fetch": 1,
        "n_fetch_failed": 0,
        "n_obs": 1,
        "n_dq_fail": 0,
        "n_dq_warn": 0,
    }
    w._append("runs", [row], lambda r: r["target_date"])


def test_latest_check_run_on_an_empty_store_is_none(tmp_path: Path) -> None:
    assert query.latest_check_run(World(tmp_path).connect(), SAT_1000) is None


def test_latest_check_run_skips_a_later_backfill_of_an_earlier_date(tmp_path: Path) -> None:
    w = World(tmp_path)
    _run_row(w, FRI, "R-FRI", et(FRI, "06:55"))
    _checks(w, FRI, "R-FRI", et(FRI, "07:00"))
    _run_row(w, THU, "R-BACKFILL", et(FRI, "09:00"))  # started later, for an earlier date
    _checks(w, THU, "R-BACKFILL", et(FRI, "09:05"))
    ref = query.latest_check_run(w.connect(), SAT_1000)
    assert ref == query.RunRef(FRI, "R-FRI", et(FRI, "06:55"))


def test_latest_check_run_ignores_target_dates_after_today(tmp_path: Path) -> None:
    w = World(tmp_path)
    _checks(w, FRI, "R-FRI", et(FRI, "07:00"))
    _checks(w, MON, "R-AHEAD", et(SAT, "09:00"), "fail")  # evaluated before now, but for Monday
    ref = query.latest_check_run(w.connect(), SAT_1000)
    assert ref == query.RunRef(FRI, "R-FRI", None)  # no runs row: started_at unknown


def test_latest_check_run_ignores_rows_evaluated_after_now(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.dq(THU, [("fresh", "pass", "yahoo", None)], run_id="R-THU")
    w.dq(FRI, [("fresh", "pass", "yahoo", None)], run_id="R-FRI")  # 07:00
    w.dq(FRI, [("fresh", "fail", "yahoo", None)], at="09:00", run_id="R-FRI-2")
    db = w.connect()
    assert query.latest_check_run(db, et(FRI, "08:00")) == query.RunRef(FRI, "R-FRI", None)
    assert query.latest_check_run(db, et(FRI, "06:00")) == query.RunRef(THU, "R-THU", None)
    assert query.latest_check_run(db, SAT_1000) == query.RunRef(FRI, "R-FRI-2", None)


def test_latest_check_run_counts_a_run_of_skipped_checks(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.dq(THU, [("fresh", "pass", "yahoo", None)], run_id="R-THU")
    w.dq(FRI, [("fresh", "skip", "yahoo", None)], run_id="R-FRI")
    assert query.latest_check_run(w.connect(), SAT_1000) == query.RunRef(FRI, "R-FRI", None)


def test_cross_checks_count_from_their_run_start_not_their_market_time(tmp_path: Path) -> None:
    """``at_time`` is market time (here Fri 00:00), not when the check ran (review round 2 #1).

    R-FRI starts Fri 06:55 and writes a cross-check fail stamped Fri 00:00. Read at Fri 06:00,
    before R-FRI started, the earlier run is the one shown and the fail is not counted.
    """
    w = World(tmp_path)
    w.dq(THU, [("fresh", "pass", "yahoo", None)], run_id="R-THU")  # Thu 07:00
    _run_row(w, THU, "R-THU", et(THU, "06:55"))
    w.dq(FRI, [("fresh", "pass", "yahoo", None)], run_id="R-FRI")  # Fri 07:00
    w.xchecks(FRI, [("sp500_return", "fail", 12.0)], run_id="R-FRI", at="00:00")
    _run_row(w, FRI, "R-FRI", et(FRI, "06:55"))
    db = w.connect()
    now = et(FRI, "06:00")
    assert query.latest_check_run(db, now) == query.RunRef(THU, "R-THU", et(THU, "06:55"))
    early = query.health_summary(db, FRI, expected_sources=["yahoo"], as_of=now)
    assert (early.n_pass, early.n_xcheck_fail, early.issues) == (0, 0, ())
    assert query.instrument_flags(db, FRI, as_of=now) == {}
    later = et(FRI, "07:30")
    assert query.latest_check_run(db, later) == query.RunRef(FRI, "R-FRI", et(FRI, "06:55"))
    full = query.health_summary(db, FRI, expected_sources=["yahoo"], as_of=later)
    assert (full.n_pass, full.n_xcheck_fail) == (1, 1)


def test_health_reads_with_as_of_ignore_a_rerun_stored_later(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.dq(MON, [("fresh_a", "fail", "stooq", "WTI")], run_id="R1")  # 07:00
    w.dq(MON, [("fresh_a", "pass", "stooq", "WTI")], at="08:00", run_id="R2")
    w.xchecks(MON, [("sp500_return", "warn", 7.0)], run_id="R1")
    w.xchecks(MON, [("sp500_return", "pass", 1.0)], at="08:00", run_id="R2")
    _run_row(w, MON, "R1", et(MON, "06:55"), "degraded")
    _run_row(w, MON, "R2", et(MON, "07:55"), "ok")
    w.fetches(MON, [("yahoo", "ok")])  # requested 06:00
    db = w.connect()
    bounded = query.health_summary(db, MON, expected_sources=["yahoo"], as_of=et(MON, "07:30"))
    assert (bounded.n_pass, bounded.n_fail, bounded.n_xcheck_warn) == (0, 1, 1)
    assert (bounded.run_id, bounded.run_status) == ("R1", "degraded")
    assert {s.source: s.status for s in bounded.sources} == {"yahoo": "ok"}
    assert query.instrument_flags(db, MON, as_of=et(MON, "07:30")) == {
        "WTI": "fail",
        "SPY": "warn",
        "ES_F": "warn",
    }
    early = query.health_summary(db, MON, expected_sources=["yahoo"], as_of=et(MON, "05:00"))
    assert {s.source: s.status for s in early.sources} == {"yahoo": "missing"}
    assert (early.n_checks, early.run_id) == (0, None)
    unbounded = query.health_summary(db, MON, expected_sources=["yahoo"])  # as before
    assert (unbounded.n_pass, unbounded.n_fail, unbounded.run_status) == (1, 0, "ok")
    assert query.instrument_flags(db, MON) == {"WTI": "pass", "SPY": "pass", "ES_F": "pass"}


# ---------------------------------------------------------------------------
# T61: `deskbrief diagnose changes` (read-only) on the synthetic store
# ---------------------------------------------------------------------------


def test_diagnose_changes_prints_last_value_and_baseline_and_writes_nothing(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from deskbrief import cli

    root = world.store.root
    text = (Path(__file__).resolve().parents[2] / "config.yaml").read_text(encoding="utf-8")
    cfg = tmp_path / "diag-config.yaml"
    cfg.write_text(text.replace("local_root: ./.data", f"local_root: {root}"), encoding="utf-8")
    before = sorted(p.relative_to(root) for p in root.rglob("*"))

    assert cli.main(["diagnose", "changes", "--date", "2026-11-16", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before  # wrote nothing
    lines = out.splitlines()
    assert lines[0].startswith("# deskbrief diagnose changes: report date 2026-11-16, cut 07:00 ET")
    es = lines.index(next(x for x in lines if x.startswith("ES_F ")))
    assert 'status=ok basis="previous NYSE close"' in lines[es]
    assert lines[es + 1].startswith("  last      yahoo/quote field=last interval=tick tier=2")
    assert "as_of=2026-11-16 06:59 ET value=5976" in lines[es + 1]
    assert lines[es + 2].startswith("  baseline  yahoo/history field=close interval=1d tier=2")
    assert "as_of=2026-11-13 ET value=6000" in lines[es + 2]
    assert lines[es + 4] == "  rankable  yes"
    assert "yahoo/history close 1d newest=2026-11-13 n=3" in lines[es + 5]
    # an instrument with no value says so, and every configured instrument gets a block
    assert any(x.startswith("N225 [index] no value as of the cut") for x in lines)
    assert "# notable moves the brief scored" in out

    assert (
        cli.main(
            ["diagnose", "changes", "--date", "2026-11-16", "--no-values", "--config", str(cfg)]
        )
        == 0
    )
    hidden = capsys.readouterr().out
    assert "value=" not in hidden
    assert "change=" not in hidden
    assert "as_of=2026-11-13 ET" in hidden


# ---------------------------------------------------------------------------
# T61 (P-024): own-market baselines, like-for-like ranking, values new since the last brief.
# The dates are real calendar days (Tokyo's Labour Thanksgiving Day, the UK summer bank holiday,
# MLK day); every value is invented, none is copied from or derived from a real store or email.
# ---------------------------------------------------------------------------

TUE24, MON23, FRI20, THU19 = (date(2026, 11, d) for d in (24, 23, 20, 19))
WED25 = date(2026, 11, 25)
TUE24_0700 = et(TUE24, "07:00")
FRED = {"source": "fred", "dataset": "series", "tier": 3}


def test_fx_compares_with_its_own_ny_close(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("EURUSD", MON23, 1.30, field="rate", unit="fx_rate", source="ecb",
                  dataset="eurofxref"),  # decoy: the ECB fix is another series
            tick("EURUSD", et(MON23, "16:00"), 1.09, unit="fx_rate"),
            tick("EURUSD", et(MON23, "17:30"), 1.10, unit="fx_rate"),  # nearest 17:00 ET
            tick("EURUSD", et(MON23, "17:00"), 1.40, unit="fx_rate", tier=1,
                 source="twelvedata", dataset="fx_quote"),  # decoy: another source's quote
            tick("EURUSD", et(TUE24, "06:16"), 1.111, unit="fx_rate"),
        ]
    )  # fmt: skip
    fx = change_of(query.overnight_changes(w.connect(), TUE24, TUE24_0700), "EURUSD")
    assert (fx.family, fx.status) == ("price", "ok")
    assert fx.basis == "previous NY close, 17:30 ET quote"
    assert fx.previous is not None
    assert (fx.previous.source, fx.previous.as_of) == ("yahoo", et(MON23, "17:30"))
    assert fx.change == pytest.approx(1.0)
    assert fx.rankable


def test_fx_without_a_quote_near_the_ny_close_has_no_baseline(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            tick("EURUSD", et(MON23, "12:00"), 1.09, unit="fx_rate"),  # midday: not a close
            tick("EURUSD", et(TUE24, "06:16"), 1.111, unit="fx_rate"),
        ]
    )
    fx = change_of(query.overnight_changes(w.connect(), TUE24, TUE24_0700), "EURUSD")
    assert (fx.status, fx.basis, fx.previous) == ("no_baseline", query.BASIS_NY_CLOSE, None)


def test_fx_walking_back_past_a_missing_close_is_shown_but_not_ranked(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            tick("EURUSD", et(FRI20, "17:30"), 1.10, unit="fx_rate"),  # Friday's close only
            tick("EURUSD", et(TUE24, "06:16"), 1.111, unit="fx_rate"),
        ]
    )
    fx = change_of(query.overnight_changes(w.connect(), TUE24, TUE24_0700), "EURUSD")
    assert fx.status == "ok"
    assert fx.previous is not None
    assert fx.previous.as_of == et(FRI20, "17:30")
    assert fx.change == pytest.approx(1.0)
    assert fx.note == "no yahoo quote stored near the Mon 23 Nov NY close"
    assert (fx.rankable, fx.unranked_reason) == (False, "spans more than one FX day")


def test_n225_on_a_tokyo_holiday_uses_tokyos_previous_session(tmp_path: Path) -> None:
    assert is_business_day("XNYS", MON23)
    assert not is_business_day("XTKS", MON23)  # Labour Thanksgiving Day
    w = World(tmp_path)
    w.observations(
        [
            daily("N225", THU19, 1000.0, unit="index_pts", **YH_HIST),  # decoy
            daily("N225", FRI20, 40000.0, unit="index_pts", **YH_HIST),
            tick("N225", et(TUE24, "01:45"), 40400.0, unit="index_pts"),  # after Tokyo's close
        ]
    )
    n225 = change_of(query.overnight_changes(w.connect(), TUE24, TUE24_0700), "N225")
    assert (n225.status, n225.basis) == ("ok", "previous Tokyo close")
    assert n225.previous is not None
    assert n225.previous.as_of == et(FRI20)
    assert n225.change == pytest.approx(1.0)
    assert n225.rankable


def test_ftse_on_a_london_holiday_uses_londons_previous_session(tmp_path: Path) -> None:
    fri, bank_holiday, tue = date(2026, 8, 28), date(2026, 8, 31), date(2026, 9, 1)
    assert is_business_day("XNYS", bank_holiday)
    assert not is_business_day("XLON", bank_holiday)
    w = World(tmp_path)
    w.observations(
        [
            daily("FTSE", date(2026, 8, 27), 1000.0, unit="index_pts", **YH_HIST),  # decoy
            daily("FTSE", fri, 8000.0, unit="index_pts", **YH_HIST),
            tick("FTSE", et(tue, "06:00"), 8080.0, unit="index_pts"),
        ]
    )
    ftse = change_of(query.overnight_changes(w.connect(), tue, et(tue, "07:00")), "FTSE")
    assert (ftse.status, ftse.basis) == ("ok", "previous London close")
    assert ftse.previous is not None
    assert ftse.previous.as_of == et(fri)
    assert ftse.change == pytest.approx(1.0)


def test_dax_after_an_nyse_holiday_uses_frankfurts_own_previous_session(tmp_path: Path) -> None:
    # MLK day: Xetra traded, the NYSE did not. The NYSE basis would span two Xetra sessions
    fri, mlk, tue = date(2026, 1, 16), date(2026, 1, 19), date(2026, 1, 20)
    assert is_business_day("XETR", mlk)
    assert not is_business_day("XNYS", mlk)
    w = World(tmp_path)
    w.observations(
        [
            daily("DAX", fri, 15000.0, unit="index_pts", **YH_HIST),
            daily("DAX", mlk, 16000.0, unit="index_pts", **YH_HIST),
            tick("DAX", et(tue, "06:00"), 16160.0, unit="index_pts"),
        ]
    )
    dax = change_of(query.overnight_changes(w.connect(), tue, et(tue, "07:00")), "DAX")
    assert (dax.status, dax.basis) == ("ok", "previous Frankfurt close")
    assert dax.previous is not None
    assert dax.previous.as_of == et(mlk)
    assert dax.change == pytest.approx(1.0)


def test_foreign_session_the_previous_brief_scored_is_not_ranked_again(tmp_path: Path) -> None:
    # no fresh London quote on Tuesday: the last value is Monday's later quote, against the same
    # Friday close Monday's brief already used. Monday's session is not scored a second time
    mon, tue = date(2026, 11, 16), date(2026, 11, 17)
    w = World(tmp_path)
    w.observations(
        [
            daily("FTSE", FRI, 8000.0, unit="index_pts", **YH_HIST),
            tick("FTSE", et(mon, "06:00"), 8040.0, unit="index_pts"),  # in Monday's brief
            tick("FTSE", et(mon, "11:35"), 8080.0, unit="index_pts"),  # London's close
            daily("FTSE", mon, 8080.0, unit="index_pts", **YH_HIST),  # stored Monday 20:00
        ]
    )
    ftse = change_of(query.overnight_changes(w.connect(), tue, et(tue, "07:00")), "FTSE")
    assert ftse.last.as_of == et(mon, "11:35")
    assert (ftse.status, ftse.basis) == ("ok", "previous London close")
    assert ftse.change == pytest.approx(1.0)
    assert not ftse.rankable
    assert ftse.unranked_reason == (
        "London session of 16 Nov already in the previous brief (16 Nov 07:00 ET)"
    )


_TSY_3M = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}


def test_ust_3m_quote_against_the_treasury_yield_is_shown_but_not_ranked(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("UST_3M", FRI20, 3.80, **_TSY_3M),
            daily("UST_3M", MON23, 3.81, **_TSY_3M),
            tick("UST_3M", et(MON23, "14:59"), 3.62, unit="pct"),  # ^IRX: a discount rate
        ]
    )
    y = change_of(query.overnight_changes(w.connect(), TUE24, TUE24_0700), "UST_3M")
    assert (y.status, y.basis) == ("ok", query.BASIS_OFFICIAL)
    assert y.change == pytest.approx(-19.0)
    assert y.note == "yahoo tick value against an official treasury baseline"
    assert not y.rankable
    assert y.unranked_reason is not None
    assert "another series (treasury par_curve yield)" in y.unranked_reason


def test_ust_3m_quote_ranks_against_yahoos_own_previous_close(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("UST_3M", FRI20, 3.80, **_TSY_3M),
            daily("UST_3M", MON23, 3.81, **_TSY_3M),
            daily("UST_3M", FRI20, 3.55, unit="pct", **YH_HIST),
            daily("UST_3M", MON23, 3.62, unit="pct", **YH_HIST),  # the quote's own day
            tick("UST_3M", et(MON23, "14:59"), 3.62, unit="pct"),
        ]
    )
    y = change_of(query.overnight_changes(w.connect(), TUE24, TUE24_0700), "UST_3M")
    # PR #102 review: the 14:59 quote gives way to Monday's own close of the same series
    assert (y.last.source, y.last.dataset, y.last.as_of) == ("yahoo", "history", et(MON23))
    assert y.status == "ok"
    assert y.previous is not None
    assert (y.previous.source, y.previous.dataset, y.previous.as_of) == (
        "yahoo",
        "history",
        et(FRI20),
    )
    assert y.basis == "previous Yahoo close"
    assert y.change == pytest.approx(7.0)
    assert y.rankable


def test_ust_3m_own_close_with_a_missing_yahoo_close_is_not_ranked(tmp_path: Path) -> None:
    # PR #102 review round 2 N1: the quote gives way to Monday's own Yahoo close, which stays a
    # quoted yield (never the official path): Friday's Yahoo close is missing, so Monday against
    # Thursday spans two sessions and is never ranked as a one-day move
    w = World(tmp_path)
    w.observations(
        [
            daily("UST_3M", FRI20, 3.80, **_TSY_3M),
            daily("UST_3M", MON23, 3.81, **_TSY_3M),
            daily("UST_3M", THU19, 3.55, unit="pct", **YH_HIST),
            daily("UST_3M", MON23, 3.62, unit="pct", **YH_HIST),
            tick("UST_3M", et(MON23, "14:59"), 3.61, unit="pct"),
        ]
    )
    y = change_of(query.overnight_changes(w.connect(), TUE24, TUE24_0700), "UST_3M")
    assert (y.last.source, y.last.dataset, y.last.as_of) == ("yahoo", "history", et(MON23))
    assert (y.status, y.basis) == ("ok", "previous Yahoo close")
    assert y.change == pytest.approx(7.0)
    assert y.note == "baseline close dated 19 Nov, not the previous session (20 Nov)"
    assert (y.rankable, y.unranked_reason) == (False, "spans more than one session")


def test_ust_3m_quote_against_an_older_own_close_is_not_ranked(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("UST_3M", THU19, 3.55, unit="pct", **YH_HIST),  # Friday's close is missing
            tick("UST_3M", et(MON23, "14:59"), 3.62, unit="pct"),
        ]
    )
    y = change_of(query.overnight_changes(w.connect(), TUE24, TUE24_0700), "UST_3M")
    assert (y.status, y.basis) == ("ok", "previous Yahoo close")
    assert y.change == pytest.approx(7.0)
    assert y.note == "baseline close dated 19 Nov, not the previous session (20 Nov)"
    assert (y.rankable, y.unranked_reason) == (False, "spans more than one session")


def test_fred_value_already_shown_by_the_previous_brief_is_not_rankable(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("HY_OAS", THU19, 2.90, field="rate", unit="pct", **FRED),
            daily("HY_OAS", FRI20, 3.05, field="rate", unit="pct", **FRED),  # stored Fri 20:00
            daily("UST_2Y", FRI20, 3.70, **_TSY_3M),
            daily("UST_2Y", MON23, 3.67, **_TSY_3M),  # stored Mon 20:00: new for Tuesday
        ]
    )
    changes = query.overnight_changes(w.connect(), TUE24, TUE24_0700)
    hy = change_of(changes, "HY_OAS")
    assert (hy.status, hy.change) == ("ok", pytest.approx(0.15))  # shown in pp, with its date
    assert not hy.rankable
    assert hy.unranked_reason == (
        "value dated 20 Nov, not new since the previous brief (23 Nov 07:00 ET)"
    )
    assert change_of(changes, "UST_2Y").rankable


def test_late_published_fred_value_is_ranked_once(tmp_path: Path) -> None:
    # FRED publishes a day late: Friday's value first stored on Monday is new for Tuesday's
    # brief (the first after it landed), and no longer new for Wednesday's
    w = World(tmp_path)
    w.observations(
        [
            daily("HY_OAS", THU19, 2.90, field="rate", unit="pct", **FRED),
            daily("HY_OAS", FRI20, 3.05, field="rate", unit="pct",
                  ingested_at=et(MON23, "10:00"), **FRED),
        ]
    )  # fmt: skip
    db = w.connect()
    tue = change_of(query.overnight_changes(db, TUE24, TUE24_0700), "HY_OAS")
    assert tue.last.as_of == et(FRI20)  # the row keeps the value's own date
    assert (tue.status, tue.rankable) == ("ok", True)
    wed = change_of(query.overnight_changes(db, WED25, et(WED25, "07:00")), "HY_OAS")
    assert wed.last.as_of == et(FRI20)
    assert not wed.rankable
    assert wed.unranked_reason == (
        "value dated 20 Nov, not new since the previous brief (24 Nov 07:00 ET)"
    )


def test_tick_older_than_the_previous_brief_gives_way_to_its_close(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("ES_F", MON23, 6000.0, unit="index_pts", **YH_HIST),
            tick("ES_F", et(MON23, "06:30"), 6060.0, unit="index_pts"),  # before Monday's brief
        ]
    )
    es = change_of(query.overnight_changes(w.connect(), TUE24, TUE24_0700), "ES_F")
    # PR #102 review: Monday's close of the same series supersedes Monday's earlier tick, so the
    # stale tick is neither shown nor ranked; Tuesday has no move yet against that close
    assert (es.last.interval, es.last.value) == ("1d", 6000.0)
    assert es.status == "no_move_yet"
    assert not es.rankable


def test_brent_baseline_is_never_fred_spot(tmp_path: Path) -> None:
    oil = {"unit": "USD"}
    spot = daily("BRENT", MON23, 50.0, symbol="DCOILBRENTEU", **FRED, **oil)  # tier 3 ranks first
    quote = tick("BRENT", et(TUE24, "06:06"), 61.20, symbol="BZ=F", **oil)
    w = World(tmp_path / "with_own_close")
    w.observations([spot, quote, daily("BRENT", MON23, 60.0, symbol="BZ=F", **YH_HIST, **oil)])
    brent = change_of(query.overnight_changes(w.connect(), TUE24, TUE24_0700), "BRENT")
    assert brent.previous is not None
    assert (brent.previous.source, brent.previous.dataset) == ("yahoo", "history")
    assert brent.change == pytest.approx(2.0)
    assert brent.rankable
    # no Yahoo close of its own: no baseline at all, never the spot series
    w2 = World(tmp_path / "spot_only")
    w2.observations([spot, quote])
    brent = change_of(query.overnight_changes(w2.connect(), TUE24, TUE24_0700), "BRENT")
    assert (brent.status, brent.previous, brent.change) == ("no_baseline", None, None)


_SECRET = "https://acct.r2.example.com/bucket-name/observations?X-Amz-Credential=AKIDSECRET"


def test_diagnose_failures_print_the_error_type_only(
    world: World,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deskbrief import cli

    # a config path that does not exist: its path is not echoed
    missing = tmp_path / "private-folder" / "config.yaml"
    assert cli.main(["diagnose", "changes", "--date", "2026-11-16", "--config", str(missing)]) == 2
    err = capsys.readouterr().err
    assert err.strip() == "diagnose: config unavailable: FileNotFoundError"
    assert "private-folder" not in err

    # a lazy store read that fails mid-query with an endpoint, bucket and key in its message
    cfg = tmp_path / "config.yaml"
    text = (Path(__file__).resolve().parents[2] / "config.yaml").read_text(encoding="utf-8")
    cfg.write_text(
        text.replace("local_root: ./.data", f"local_root: {world.store.root}"), encoding="utf-8"
    )

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError(f"HTTP 403 reading {_SECRET}")

    monkeypatch.setattr(query, "overnight_changes", boom)
    assert cli.main(["diagnose", "changes", "--date", "2026-11-16", "--config", str(cfg)]) == 1
    out, err = capsys.readouterr()
    assert err.strip() == "diagnose: read failed: OSError"
    for leak in ("r2.example", "bucket-name", "AKIDSECRET", "403"):
        assert leak not in out + err
