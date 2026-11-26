"""The T61 review items carried into card T62 (``query.overnight_changes``).

SYNTHETIC FIXTURE: every value is made up (``brief_synth`` builders).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brief_synth import FRI, MON, THU, World, daily, et, tick
from deskbrief.query import ChangeRow, overnight_changes, previous_brief_time
from deskbrief.report.model import BriefModel

TUE = date(2026, 11, 17)
YH = {"source": "yahoo", "dataset": "history", "tier": 2}
TSY = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}


def change(w: World, iid: str, d: date, hhmm: str = "07:00") -> ChangeRow:
    out = overnight_changes(w.connect(), d, et(d, hhmm))
    return next(c for c in out if c.last.instrument_id == iid)


def test_a_stale_quote_gives_way_to_its_sessions_close(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("ES_F", FRI, 6000.0, unit="index_pts", **YH),
            tick("ES_F", et(MON, "06:59"), 6010.0, unit="index_pts"),  # what Monday's brief showed
            daily("ES_F", MON, 6100.0, unit="index_pts", **YH),
            tick(
                "ES_F", et(MON, "15:00"), 6050.0, unit="index_pts"
            ),  # stale: before Monday's close
        ]
    )
    ch = change(w, "ES_F", TUE)
    # PR #102 review B1: the value shown is Monday's close, never the earlier quote with a
    # backwards change; Tuesday 07:00 has no move yet against it
    assert (ch.last.interval, ch.last.value) == ("1d", 6100.0)
    assert ch.status == "no_move_yet"
    assert ch.change is None
    assert not ch.rankable


def test_on_a_saturday_fridays_close_supersedes_fridays_afternoon_quote(tmp_path: Path) -> None:
    sat = date(2026, 11, 14)
    w = World(tmp_path)
    w.observations(
        [
            daily("ES_F", THU, 5950.0, unit="index_pts", **YH),
            daily("ES_F", FRI, 6000.0, unit="index_pts", **YH),
            tick("ES_F", et(FRI, "15:00"), 6030.0, unit="index_pts"),  # before Friday's close
        ]
    )
    ch = change(w, "ES_F", sat, "10:00")
    assert (ch.last.interval, ch.last.value) == ("1d", 6000.0)
    assert ch.previous is not None
    assert ch.previous.value == 5950.0  # Friday's close against Thursday's
    assert ch.basis == "Thu close"


def test_a_quote_after_its_sessions_close_stays_the_value(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("ES_F", MON, 6100.0, unit="index_pts", **YH),
            tick("ES_F", et(MON, "18:30"), 6120.0, unit="index_pts"),  # the evening session
        ]
    )
    ch = change(w, "ES_F", TUE)
    assert (ch.last.interval, ch.last.value) == ("tick", 6120.0)
    assert ch.status == "ok"


def test_since_follows_the_brief_not_the_readers_clock(tmp_path: Path) -> None:
    hy = {"source": "fred", "dataset": "series", "unit": "pct"}
    w = World(tmp_path)
    w.observations(
        [
            daily("HY_OAS", date(2026, 11, 11), 3.50, **hy),
            # Thursday's value, published by FRED Friday 10:00: after Friday's 06:15 brief
            daily("HY_OAS", THU, 3.80, ingested_at=et(FRI, "10:00"), **hy),
        ]
    )
    at_brief = change(w, "HY_OAS", MON, "06:15")
    assert at_brief.rankable
    conn = w.connect()
    afternoon = overnight_changes(conn, MON, et(MON, "15:00"), brief_time=et(MON, "06:15"))
    hy_pm = next(c for c in afternoon if c.last.instrument_id == "HY_OAS")
    assert hy_pm.rankable  # the same ranking as the morning's brief
    drifting = overnight_changes(conn, MON, et(MON, "15:00"))  # the old, clock-anchored rule
    assert not next(c for c in drifting if c.last.instrument_id == "HY_OAS").rankable


def test_a_quote_after_its_baseline_close_still_ranks(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("ES_F", MON, 6100.0, unit="index_pts", **YH),
            tick("ES_F", et(TUE, "06:59"), 6050.0, unit="index_pts"),
        ]
    )
    assert change(w, "ES_F", TUE).rankable


def test_foreign_index_with_daily_bars_only_ranks_against_its_own_previous_session(
    tmp_path: Path,
) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("N225", THU, 38000.0, unit="index_pts", **YH),
            daily("N225", FRI, 38400.0, unit="index_pts", **YH),
            daily("N225", MON, 38400.0, unit="index_pts", **YH),
            # Tokyo closed 01:30 ET; the bar was fetched at 02:00 ET
            daily("N225", TUE, 38016.0, unit="index_pts", ingested_at=et(TUE, "02:00"), **YH),
        ]
    )
    ch = change(w, "N225", TUE)
    assert ch.basis == "previous Tokyo close"
    assert ch.last.value == 38016.0  # Tuesday's Tokyo bar is final by 07:00 ET (review)
    assert ch.previous is not None
    assert ch.previous.value == 38400.0  # Monday, Tokyo's session before Tuesday
    assert ch.change == -1.0
    assert ch.rankable


def test_a_european_bar_of_the_day_is_not_final_before_its_close(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("FTSE", MON, 8000.0, unit="index_pts", **YH),
            # dated today and already stored, but London is still trading at 07:00 ET
            daily("FTSE", TUE, 99999.0, unit="index_pts", ingested_at=et(TUE, "06:00"), **YH),
        ]
    )
    ch = change(w, "FTSE", TUE)
    assert ch.last.value == 8000.0


def test_foreign_index_with_bars_only_over_a_tokyo_holiday_is_not_scored_twice(
    tmp_path: Path,
) -> None:
    # Monday 23 Nov 2026 is a Tokyo holiday and an NYSE session: no bar that day
    w = World(tmp_path)
    w.observations(
        [
            daily("N225", date(2026, 11, 19), 38000.0, unit="index_pts", **YH),
            daily("N225", date(2026, 11, 20), 38400.0, unit="index_pts", **YH),
        ]
    )
    ch = change(w, "N225", date(2026, 11, 24))
    assert ch.basis == "previous Tokyo close"
    assert ch.previous is not None
    assert ch.previous.value == 38000.0
    assert not ch.rankable  # Monday's brief already showed Friday's Tokyo session
    assert ch.unranked_reason is not None
    assert ch.unranked_reason.startswith("Tokyo session of 20 Nov already in the previous brief")


def test_an_official_series_missing_a_day_is_not_ranked_as_a_one_day_move(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations([daily("UST_2Y", THU, 4.20, **TSY), daily("UST_2Y", MON, 4.10, **TSY)])
    ch = change(w, "UST_2Y", TUE)
    assert ch.status == "ok"
    assert ch.change is not None
    assert round(ch.change) == -10
    assert not ch.rankable
    assert ch.unranked_reason == "spans more than one session"
    assert ch.note == "baseline dated 12 Nov, not the previous session (13 Nov)"


def test_an_official_series_without_a_gap_still_ranks(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations([daily("UST_2Y", FRI, 4.20, **TSY), daily("UST_2Y", MON, 4.10, **TSY)])
    assert change(w, "UST_2Y", TUE).rankable


def test_a_future_with_no_close_of_its_own_says_so(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("BRENT", FRI, 74.0, source="fred", dataset="series"),  # spot: never its baseline
            tick("BRENT", et(MON, "06:58"), 73.0),
        ]
    )
    ch = change(w, "BRENT", MON)
    assert ch.status == "no_baseline"
    assert ch.note == (
        "no Yahoo daily close stored for Fri 13 Nov; a future is never compared with another series"
    )
    m = BriefModel.from_store(w.connect(), w.config, MON, et(MON, "07:00"))
    row = next(r for r in m.rows() if r.instrument_id == "BRENT")
    assert "a future is never compared with another series" in row.description


def test_previous_brief_time_is_the_previous_session_at_the_report_clock_time() -> None:
    assert previous_brief_time(MON, et(MON, "07:00")) == et(FRI, "07:00")
    assert previous_brief_time(TUE, et(TUE, "06:20")) == et(MON, "06:20")
