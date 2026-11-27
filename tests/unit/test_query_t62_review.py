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


def test_on_a_saturday_fridays_close_is_new_after_a_friday_premarket_print(tmp_path: Path) -> None:
    # PR #102 review round 2 N2: Friday's brief showed a Friday 05:00 print; Friday's close,
    # stored after that brief, is still new on Saturday and is ranked close to close
    sat = date(2026, 11, 14)
    w = World(tmp_path)
    w.observations(
        [
            daily("ES_F", THU, 5950.0, unit="index_pts", **YH),
            tick("ES_F", et(FRI, "05:00"), 5955.0, unit="index_pts"),  # in Friday's brief
            daily("ES_F", FRI, 6000.0, unit="index_pts", **YH),
            tick("ES_F", et(FRI, "15:00"), 6030.0, unit="index_pts"),
        ]
    )
    ch = change(w, "ES_F", sat, "10:00")
    assert (ch.last.interval, ch.last.value) == ("1d", 6000.0)
    assert ch.previous is not None
    assert ch.previous.value == 5950.0
    assert (ch.rankable, ch.unranked_reason) == (True, None)


def test_on_an_xnys_holiday_the_previous_close_is_new_after_a_premarket_print(
    tmp_path: Path,
) -> None:
    # PR #102 review round 2 N2: Thanksgiving (XNYS closed, the morning cron still runs).
    # Wednesday's brief showed a Wednesday pre-market print; Wednesday's close, stored on
    # Thursday morning, supersedes the Wednesday afternoon quote and is new
    tue, wed, thu = date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 26)
    w = World(tmp_path)
    w.observations(
        [
            daily("ES_F", tue, 5950.0, unit="index_pts", **YH),
            tick("ES_F", et(wed, "05:00"), 5955.0, unit="index_pts"),  # in Wednesday's brief
            tick("ES_F", et(wed, "15:00"), 6030.0, unit="index_pts"),
            daily("ES_F", wed, 6000.0, unit="index_pts", ingested_at=et(thu, "05:00"), **YH),
        ]
    )
    ch = change(w, "ES_F", thu, "06:15")
    assert (ch.last.interval, ch.last.value) == ("1d", 6000.0)
    assert ch.previous is not None
    assert ch.previous.value == 5950.0
    assert (ch.rankable, ch.unranked_reason) == (True, None)


def test_a_close_the_previous_brief_already_had_is_not_new_again(tmp_path: Path) -> None:
    # the other side of N2: Monday's close was stored before Tuesday's brief (which showed it),
    # so with nothing newer on Wednesday it is not ranked a second time
    wed = date(2026, 11, 18)
    w = World(tmp_path)
    w.observations(
        [
            daily("UST_10Y", FRI, 4.05, unit="pct", **YH),
            tick("UST_10Y", et(MON, "14:50"), 4.14, unit="pct"),
            daily("UST_10Y", MON, 4.15, unit="pct", ingested_at=et(TUE, "05:00"), **YH),
            # review round 3: Yahoo's ten-day window stores Monday's close again on Tuesday's
            # EOD run, so its latest copy is newer than Tuesday's brief; it is still not new
            daily("UST_10Y", MON, 4.15, unit="pct", ingested_at=et(TUE, "18:00"), **YH),
        ]
    )
    assert change(w, "UST_10Y", TUE, "06:15").rankable
    ch = change(w, "UST_10Y", wed, "06:15")
    assert not ch.rankable
    assert ch.unranked_reason is not None
    assert "not new since the previous brief" in ch.unranked_reason


def test_a_close_is_ranked_once_across_a_sifma_holiday(tmp_path: Path) -> None:
    # review round 3, R3-1: Veterans Day (Wed 11 Nov 2026) is an NYSE session with SIFMA shut,
    # so a quoted yield has no print that day. Tuesday's close (after Tuesday's pre-market print,
    # first stored Wed 06:00, stored again Wed 18:00 and Thu 06:00) is new on Wednesday only
    tue, wed, thu = date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)
    w = World(tmp_path)
    w.observations(
        [
            daily("UST_10Y", date(2026, 11, 9), 4.05, unit="pct", **YH),
            tick("UST_10Y", et(tue, "06:10"), 4.06, unit="pct"),
            tick("UST_10Y", et(tue, "14:50"), 4.14, unit="pct"),
            *(
                daily("UST_10Y", tue, 4.15, unit="pct", ingested_at=at, **YH)
                for at in (et(wed, "06:00"), et(wed, "18:00"), et(thu, "06:00"))
            ),
        ]
    )
    first = change(w, "UST_10Y", wed, "06:15")
    assert (first.last.interval, first.rankable) == ("1d", True)
    again = change(w, "UST_10Y", thu, "06:15")
    assert again.last.interval == "1d"
    assert not again.rankable
    assert again.unranked_reason is not None
    assert "not new since the previous brief (11 Nov 06:15 ET)" in again.unranked_reason


def test_a_quoted_yields_close_after_its_premarket_print_ranks_the_next_morning(
    tmp_path: Path,
) -> None:
    # N2 on a weekday with N1: Monday's 06:10 ^TNX print was in Monday's brief; Monday's 14:50
    # quote gives way to Monday's Yahoo close, which is new on Tuesday, against Friday's close
    w = World(tmp_path)
    w.observations(
        [
            daily("UST_10Y", FRI, 4.05, unit="pct", **YH),
            tick("UST_10Y", et(MON, "06:10"), 4.06, unit="pct"),
            tick("UST_10Y", et(MON, "14:50"), 4.14, unit="pct"),
            daily("UST_10Y", MON, 4.15, unit="pct", **YH),
        ]
    )
    ch = change(w, "UST_10Y", TUE, "06:15")
    assert ch.basis == "previous Yahoo close"
    assert ch.change is not None
    assert abs(ch.change - 10.0) < 1e-6
    assert (ch.rankable, ch.unranked_reason) == (True, None)


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
            daily("N225", MON, 38400.0, unit="index_pts", ingested_at=et(MON, "10:05"), **YH),
            # Yahoo's writer keeps a Tokyo bar only once its date is past in Tokyo (~10:00 ET)
            daily("N225", TUE, 38016.0, unit="index_pts", ingested_at=et(TUE, "10:05"), **YH),
        ]
    )
    morning = change(w, "N225", TUE)
    # PR #102 review round 2: Tokyo closed at 01:30 ET but its bar is not stored by 07:00, so
    # the row shows Monday's session against Friday's and says Tuesday's close is missing
    assert morning.basis == "previous Tokyo close"
    assert (morning.last.value, morning.change) == (38400.0, 0.0)
    assert morning.note == (
        "Tokyo close of Tue 17 Nov not yet stored; this is the Mon 16 Nov session"
    )
    assert morning.rankable  # Monday's brief (06:15) had no Monday bar yet: it is new
    later = change(w, "N225", TUE, "11:00")
    assert later.last.value == 38016.0  # stored at 10:05 ET: the bar of the day counts
    assert later.previous is not None
    assert later.previous.value == 38400.0  # Monday, Tokyo's session before Tuesday
    assert later.change == -1.0
    assert later.note is None
    assert later.rankable


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


def test_a_tokyo_bar_the_previous_brief_showed_is_not_scored_again_after_a_holiday(
    tmp_path: Path,
) -> None:
    """Monday 23 Nov 2026 is a Tokyo holiday. If Friday's brief already had Friday's final Tokyo
    bar (stored before it ran), Monday's brief must not score that session again."""
    fri, mon = date(2026, 11, 20), date(2026, 11, 23)
    w = World(tmp_path)
    w.observations(
        [
            daily("N225", d, 38000.0 + 100 * i, unit="index_pts", ingested_at=et(d, "02:00"), **YH)
            for i, d in enumerate([date(2026, 11, 18), date(2026, 11, 19), fri])
        ]
    )
    assert change(w, "N225", fri).rankable  # Friday's brief scores Friday's Tokyo session
    monday = change(w, "N225", mon)
    assert not monday.rankable
    assert monday.unranked_reason is not None
    assert "already in the previous brief" in monday.unranked_reason
