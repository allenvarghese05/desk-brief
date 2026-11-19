"""Weekly values and the new fields, end to end (card T45, P-021): store -> query -> BriefModel,
and store -> ``build_bars`` -> tearsheet.

SYNTHETIC FIXTURE: every row is made up (see ``weekly_synth``). Report: Monday 16 Nov 2026,
07:00 ET. Weekly rows are visible from their (made-up) release time, daily rows from 17:00 ET.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from deskbrief.config import Config
from deskbrief.query import BASIS_OFFICIAL, BASIS_WEEKLY, field_label, latest_snapshot
from deskbrief.report.model import BriefModel, Row
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_to_utc
from weekly_synth import broad, net, nfci, oi, weekly_config, write

MON = date(2026, 11, 16)
AT = et_to_utc(MON, "07:00")


def _at(d: date, hhmm: str = "17:00"):  # type: ignore[no-untyped-def]
    return et_to_utc(d, hhmm)


@pytest.fixture(scope="module")
def wconfig() -> Config:
    return weekly_config()


@pytest.fixture
def store(tmp_path: Path, wconfig: Config) -> LocalBackend:
    s = LocalBackend(tmp_path / "data")
    thu, fri = date(2026, 11, 12), date(2026, 11, 13)
    rows = [
        broad(thu, 120.0, _at(thu)),
        broad(fri, 121.5, _at(fri)),
        broad(MON, 999.0, _at(MON, "06:00")),  # dated today: unfinished, never the value
        # NFCI: Fridays 30 Oct and 6 Nov, released the Wednesdays after
        nfci(date(2026, 10, 30), -0.50, _at(date(2026, 11, 4), "08:35")),
        nfci(date(2026, 11, 6), -0.46, _at(date(2026, 11, 11), "08:35")),
        nfci(fri, -0.40, _at(date(2026, 11, 18), "08:35")),  # released after the report
        # CFTC: Tuesdays 3 and 10 Nov, released Friday / Monday (Veterans Day week)
        net(date(2026, 11, 3), 250_000.0, _at(date(2026, 11, 6), "15:35")),
        net(date(2026, 11, 10), 240_000.0, _at(MON, "06:30")),
        oi(date(2026, 11, 3), 500_000.0, _at(date(2026, 11, 6), "15:35")),
        oi(date(2026, 11, 10), 512_000.0, _at(MON, "06:30")),
    ]
    write(s, wconfig, rows)
    return s


@pytest.fixture
def model(store: LocalBackend, wconfig: Config) -> BriefModel:
    return BriefModel.from_store(connect(store, wconfig), wconfig, MON, AT)


def rows_of(model: BriefModel) -> dict[str, Row]:
    return {r.instrument_id: r for r in model.rows()}


def test_daily_level_is_a_value_with_an_official_baseline(model: BriefModel) -> None:
    r = rows_of(model)["USD_BROAD"]
    assert r.available
    assert r.value == 121.5
    assert r.change is not None
    assert r.change.last.field == "level"
    assert r.change_value == pytest.approx(1.5)
    assert r.change_value_unit == "pts"
    assert r.basis == BASIS_OFFICIAL
    assert r.description == "Broad dollar (level), +1.5 pts since previous official value (12 Nov)"
    assert r.as_of_text == "13 Nov"


def test_weekly_level_is_week_on_week_with_a_date_only(model: BriefModel) -> None:
    r = rows_of(model)["NFCI"]
    assert r.value == -0.46  # the 13 Nov value was not yet released at 07:00 on 16 Nov
    assert r.as_of_text == "6 Nov"  # a weekly value has a date, never a time
    assert r.basis == BASIS_WEEKLY
    assert r.change_value == pytest.approx(0.04)
    assert r.change_value_unit == "pts"
    assert "week-on-week, since previous weekly value (30 Oct)" in r.description


def test_positions_read_as_values_with_their_labels(model: BriefModel) -> None:
    rows = rows_of(model)
    gc, oi_row = rows["GC_NET"], rows["GC_OI"]
    assert gc.value_text == "240,000"
    assert gc.label == "Gold futures positioning (net speculative position)"
    assert gc.change_value == pytest.approx(-10_000.0)
    assert "week-on-week, since previous weekly value (3 Nov)" in gc.description
    assert oi_row.label == "Gold futures interest (open interest)"
    assert oi_row.change_value == pytest.approx(12_000.0)


def test_weekly_series_are_named_in_the_known_limits(model: BriefModel) -> None:
    limits = {k.kind: k.text for k in model.known_limits}
    assert "no anomaly check" in limits["weekly"]
    for iid in ("GC_NET", "GC_OI", "NFCI"):
        assert iid in limits["weekly"]
    assert "USD_BROAD" not in limits["weekly"]  # daily: the anomaly check covers it


def test_model_round_trips_through_json(model: BriefModel) -> None:
    assert BriefModel.from_json(model.to_json()) == model


def test_field_labels_of_the_new_fields(store: LocalBackend, wconfig: Config) -> None:
    snap = {r.instrument_id: r for r in latest_snapshot(connect(store, wconfig), AT)}
    assert field_label(snap["GC_NET"]) == "net speculative position"
    assert field_label(snap["GC_OI"]) == "open interest"
    assert field_label(snap["NFCI"]) == "level"


def test_weekly_row_dated_today_is_left_out(tmp_path: Path, wconfig: Config) -> None:
    """Like a 1d row, a 1w row dated on the current ET date is unfinished (P-009)."""
    s = LocalBackend(tmp_path / "d2")
    write(s, wconfig, [nfci(MON, -0.3, _at(MON, "06:00"))])
    assert latest_snapshot(connect(s, wconfig), AT) == ()


# --- dashboard: build_bars and the tearsheet ------------------------------------------------------


def test_build_bars_reads_daily_level_and_weekly_series(
    store: LocalBackend, wconfig: Config
) -> None:
    from app.components import data

    conn = connect(store, wconfig)
    daily = data.build_bars(conn, AT, "USD_BROAD", frozenset({1}))
    assert daily["close"].tolist() == [120.0, 121.5]
    assert set(daily["interval"]) == {"1d"}
    assert set(daily["field"]) == {"level"}
    weekly = data.build_bars(conn, AT, "NFCI", frozenset({1}))
    assert weekly["close"].tolist() == [-0.50, -0.46]  # point-in-time: 13 Nov not yet known
    assert set(weekly["interval"]) == {"1w"}
    gc = data.build_bars(conn, AT, "GC_NET", frozenset({1}))
    assert gc["close"].tolist() == [250_000.0, 240_000.0]
    assert set(gc["field"]) == {"net_position"}


def test_build_bars_hosted_hides_tier1_weekly_rows(tmp_path: Path, wconfig: Config) -> None:
    from app.components import data

    s = LocalBackend(tmp_path / "d3")
    t1 = {**net(date(2026, 11, 10), 1.0, _at(MON, "06:30")), "tier": 1, "fetch_id": "T1"}
    write(s, wconfig, [t1])
    assert data.build_bars(connect(s, wconfig), AT, "GC_NET", frozenset({1})).empty


def test_tearsheet_weekly_negative_series_never_crashes(model: BriefModel, store, wconfig) -> None:  # type: ignore[no-untyped-def]
    """A negative weekly level (NFCI) gets point changes, a WoW change and honest notes instead
    of log returns (which would raise on a negative value) or a mislabelled "20-day" vol."""
    from app.components import data, tearsheet

    row = rows_of(model)["NFCI"]
    bars = data.build_bars(connect(store, wconfig), AT, "NFCI", frozenset({1}))
    html = tearsheet.risk_html(row, bars)
    assert "weekly series: needs daily values" in html
    assert "Max drawdown" not in html  # a % drawdown of a level that crosses zero is meaningless
    assert "Range, 2 weeks" in html
    labels = [h.label for h in tearsheet.horizons(row, bars)]
    assert labels[0] == "WoW"


def test_tearsheet_daily_level_moves_in_points(model: BriefModel, store, wconfig) -> None:  # type: ignore[no-untyped-def]
    from app.components import data, tearsheet

    row = rows_of(model)["USD_BROAD"]
    bars = data.build_bars(connect(store, wconfig), AT, "USD_BROAD", frozenset({1}))
    html = tearsheet.risk_html(row, bars)
    assert "not computed for a level series" in html
    assert tearsheet.horizons(row, bars)[0].label == "1D"
