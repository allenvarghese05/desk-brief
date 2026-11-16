"""Read-only query API: snapshot, overnight changes, series, lineage, health.

SYNTHETIC FIXTURE: all data comes from ``brief_synth`` (made-up values, labelled there).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from brief_synth import FRI, MON, MON_0700, THU, WED, World, daily, et, monday_rows, tick
from deskbrief import query


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
