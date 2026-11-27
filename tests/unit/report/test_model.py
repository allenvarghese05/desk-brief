"""BriefModel: labels, baselines, known limits, health, notable moves, JSON round trip.

SYNTHETIC FIXTURE: all data comes from ``brief_synth`` (made-up values, labelled there).
"""
# ruff: noqa: RUF001  (true minus sign and sigma are intentional output)

from __future__ import annotations

import itertools
import json
import os
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

import brief_synth
from brief_synth import FRI, MON, MON_0700, THU, World, daily, et, fixture_config, monday_rows, tick
from deskbrief.config import Config, config_hash
from deskbrief.query import ValueRow, latest_check_run, latest_snapshot
from deskbrief.report.model import BriefModel, Row, _qualifiers, signed

GOLDEN = Path(__file__).parent / "golden" / "brief_model.json"


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.observations(monday_rows())
    return w


def build(w: World) -> BriefModel:
    return BriefModel.from_store(w.connect(), w.config, MON, MON_0700)


def row(m: BriefModel, iid: str) -> Row:
    return next(r for r in m.rows() if r.instrument_id == iid)


def test_signed_uses_true_minus_and_hides_negative_zero() -> None:
    assert signed(-0.43, 1) == "−0.4"
    assert signed(0.43, 1) == "+0.4"
    assert signed(-0.04, 1) == "0.0"
    assert signed(0.0, 0) == "0"


def test_monday_uses_friday_close_as_baseline(world: World) -> None:
    es = row(build(world), "ES_F")
    assert es.basis == "previous NYSE close"
    assert es.change_text == "−0.4%"  # 5976 vs Friday 6000, not Thursday or the decoy
    assert es.tier_name == "delayed"


def test_thanksgiving_gap_skips_the_holiday(tmp_path: Path) -> None:
    w = World(tmp_path)
    wed, fri = date(2026, 11, 25), date(2026, 11, 27)
    w.observations(
        [
            daily("WTI", wed, 70.0, source="yahoo", dataset="history", tier=2),
            daily("WTI", date(2026, 11, 26), 999.0),  # holiday: must never be a baseline
            tick("WTI", et(fri, "06:58"), 71.4),
        ]
    )
    m = BriefModel.from_store(w.connect(), w.config, fri, et(fri, "07:00"))
    assert row(m, "WTI").change_text == "+2.0%"


def test_yield_label_is_basis_points_since_previous_official_value(world: World) -> None:
    r = row(build(world), "UST_10Y")
    assert r.description.startswith(
        "US 10-year Treasury yield, −3bp since previous official value (13 Nov); "
    )
    assert r.value_text == "4.37%"


def test_tick_against_official_baseline_says_so_in_the_row(world: World) -> None:
    r = row(build(world), "UST_10Y")
    assert "yahoo" in r.description
    assert "official treasury baseline" in r.description
    assert "baseline" not in row(build(world), "UST_2Y").description


def test_zero_baseline_says_it_was_found_and_is_zero(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations([daily("WTI", FRI, 0.0), tick("WTI", et(MON, "06:58"), 5.0)])
    r = row(build(w), "WTI")
    assert r.change_text == "n/a"
    assert "found" in r.description
    assert "is zero" in r.description
    assert "13 Nov" in r.description
    assert "not found" not in r.description


def test_missing_baseline_still_says_not_found(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations([tick("WTI", et(MON, "06:58"), 5.0)])
    assert "not found" in row(build(w), "WTI").description


def test_rows_carry_their_own_qualifiers(tmp_path: Path, world: World) -> None:
    m = build(world)
    assert row(m, "ES_F").qualifiers == ("delayed quote, 1 min old",)
    assert row(m, "SPY").qualifiers == ("IEX: single exchange, not the consolidated tape",)
    assert row(m, "EURUSD").qualifiers == ()
    w = World(tmp_path / "fallback")
    w.observations(
        [
            daily("WTI", FRI, 72.0, source="yahoo", dataset="history", tier=2),
            tick("WTI", et(MON, "06:30"), 71.2, source="stooq", tier=3, is_fallback=True),
        ]
    )
    assert row(build(w), "WTI").qualifiers == ("fallback source: stooq",)
    w2 = World(tmp_path / "stale")
    w2.observations([tick("ES_F", et(MON, "05:00"), 6000.0, unit="index_pts")])
    assert row(build(w2), "ES_F").qualifiers == ("delayed quote, 120 min old",)


def test_crosscheck_rows_are_labelled_with_ref_date(world: World) -> None:
    world.xchecks(MON, [("sp500_spy_es", "warn", 7.0)], ref=FRI)
    m = build(world)
    assert "13 Nov" in m.notable[0].text
    assert "16 Nov" not in m.notable[0].text
    assert m.notable[0].ref_label == "13 Nov"
    limit = next(k.text for k in m.known_limits if k.kind == "crosscheck_warn")
    assert "13 Nov" in limit


def test_mirrored_crosscheck_is_counted_and_listed_once(world: World) -> None:
    world.dq(MON, [("fresh", "pass", "yahoo", None)])
    world.xchecks(MON, [("sp500_spy_es", "warn", 7.0)], mirror=True)
    m = build(world)
    assert m.health.text == "1/2 checks pass (0 fail, 1 warn)"
    assert [k.kind for k in m.known_limits if k.kind.startswith(("dq_", "crosscheck_"))] == [
        "crosscheck_warn"
    ]


def test_2s10s_is_derived_from_two_official_yields_of_one_date(world: World) -> None:
    m = build(world)
    r = row(m, "UST_2S10S")
    assert r.available
    assert r.value_text == "+22bp"  # 4.40 - 4.18 on Fri 13 Nov, not the 4.37 tick
    assert r.change_text == "−3bp"  # vs Thu 12 Nov: 4.45 - 4.20 = 25bp
    assert r.tier_name == "official"
    assert r.as_of_text == "13 Nov"
    assert "derived" in r.label
    assert r.qualifiers == ("derived from official 10Y and 2Y yields of 13 Nov",)
    assert next(s for s in m.sections if s.asset_class == "rates").rows[-1] is r
    assert "UST_2S10S" in {k.kind: k.text for k in m.known_limits}["derived"]


def test_2s10s_rejects_a_tick_leg(tmp_path: Path) -> None:
    tsy = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
    w = World(tmp_path)
    w.observations(
        [
            tick("UST_10Y", et(MON, "06:58"), 4.37, unit="pct"),  # a ^TNX-style tick, not official
            daily("UST_2Y", FRI, 4.18, **tsy),
        ]
    )
    r = row(build(w), "UST_2S10S")
    assert not r.available
    assert r.value_text == "data unavailable"


def test_2s10s_needs_the_same_et_date_on_both_legs(tmp_path: Path) -> None:
    tsy = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
    w = World(tmp_path)
    w.observations([daily("UST_10Y", FRI, 4.40, **tsy), daily("UST_2Y", THU, 4.20, **tsy)])
    assert not row(build(w), "UST_2S10S").available


def test_lineage_lists_baseline_fetches(world: World) -> None:
    m = build(world)
    es = [x for x in m.lineage if x.value.instrument_id == "ES_F"]
    assert [(x.role, x.value.value) for x in es] == [("value", 5976.0), ("baseline", 6000.0)]
    assert {x.role for x in m.lineage} == {"value", "baseline"}


def test_ecb_rate_is_labelled_reference_rate_not_close(world: World) -> None:
    r = row(build(world), "EURUSD")
    assert "ECB reference rate" in r.description
    assert "close" not in r.description.lower()
    assert r.tier_name == "official"
    assert r.as_of_text == "13 Nov"


def test_proxy_is_labelled_and_unavailable_rows_say_so(world: World) -> None:
    m = build(world)
    spy = row(m, "SPY")
    assert spy.is_proxy
    assert spy.proxy_label
    assert "proxy" in spy.label
    qqq = row(m, "QQQ")
    assert not qqq.available
    assert qqq.value_text == "data unavailable"
    assert qqq.change is None


def test_no_move_yet_when_last_value_is_the_close(world: World) -> None:
    vix = row(build(world), "VIX")
    assert vix.change_text == "n/a"
    assert "no move to report" in vix.description


def test_headline_lists_configured_instruments(world: World) -> None:
    m = build(world)
    assert [i.instrument_id for i in m.headline.items] == list(
        world.config.report.headline_instruments
    )
    assert m.headline.text.startswith("Overnight: S&P fut 5,976.00 (−0.4%)")
    assert "10Y 4.37% (−3bp)" in m.headline.text
    assert m.title == "Desk Brief — Mon 16 Nov 2026, 07:00 ET"


def test_known_limits_from_config_and_data(world: World) -> None:
    kinds = {k.kind: k.text for k in build(world).known_limits}
    assert "SPY" in kinds["proxy"]
    assert "single exchange" in kinds["iex"]
    assert "tiers" in kinds
    assert "QQQ" in kinds["unavailable"]
    assert "dq_missing" in kinds
    assert "source" in kinds  # no fetches logged at all


def test_fallback_value_is_a_known_limit(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(
        [
            daily("WTI", FRI, 72.0, source="yahoo", dataset="history", tier=2),
            tick("WTI", et(MON, "06:58"), 71.2, source="stooq", tier=3, is_fallback=True),
        ]
    )
    kinds = {k.kind: k.text for k in build(w).known_limits}
    assert "WTI (stooq)" in kinds["fallback"]


def test_health_block_counts_dq_and_crosschecks(world: World) -> None:
    world.dq(MON, [("fresh", "pass", "yahoo", None), ("range", "fail", "stooq", "WTI")])
    world.xchecks(MON, [("sp500_spy_es", "warn", 7.0)])
    world.fetches(MON, [("yahoo", "ok")])
    m = build(world)
    assert m.health.status == "fail"
    assert m.health.text == "1/3 checks pass (1 fail, 1 warn)"
    kinds = {k.kind for k in m.known_limits}
    assert {"dq_fail", "crosscheck_warn"} <= kinds
    assert "dq_missing" not in kinds
    assert [n.kind for n in m.notable] == ["crosscheck"]
    assert "7.0bp" in m.notable[0].text
    source_text = next(k.text for k in m.known_limits if k.kind == "source")
    assert "yahoo" not in source_text


def test_health_ok_and_unknown(world: World) -> None:
    assert build(world).health.status == "unknown"
    world.dq(MON, [("fresh", "pass", "yahoo", None)])
    m = build(world)
    assert (m.health.status, m.health.text) == ("ok", "1/1 checks pass")


def test_notable_move_flagged_by_zscore(tmp_path: Path) -> None:
    w = World(tmp_path)
    hist = {"source": "yahoo", "dataset": "history", "tier": 2, "unit": "index_pts"}
    rows = []
    d, v, n = date(2026, 10, 1), 6000.0, 0
    while d < FRI:
        if d.weekday() < 5:
            v += 6.0 if n % 2 == 0 else -5.0  # small, slightly noisy moves
            n += 1
            rows.append(daily("ES_F", d, v, **hist))
        d += timedelta(days=1)
    rows.append(daily("ES_F", FRI, v, **hist))
    rows.append(tick("ES_F", et(MON, "06:59"), v * 0.97, unit="index_pts"))
    w.observations(rows)
    moves = [x for x in build(w).notable if x.kind == "move"]
    assert len(moves) == 1
    assert moves[0].instrument_id == "ES_F"
    assert "σ" in moves[0].text
    assert "S&P fut" in moves[0].text


def test_empty_store_still_builds(tmp_path: Path) -> None:
    m = build(World(tmp_path))
    assert not any(r.available for r in m.rows())
    assert m.lineage == ()
    assert m.health.status == "unknown"
    assert m.headline.text.startswith("Overnight: S&P fut n/a")


def test_json_round_trip_and_determinism(world: World) -> None:
    world.dq(MON, [("fresh", "pass", "yahoo", None)])
    m = build(world)
    text = m.to_json()
    assert BriefModel.from_json(text) == m
    assert build(world).to_json() == text
    assert "−" in text  # UTF-8 kept, not escaped


def test_stale_tick_shows_its_date(tmp_path: Path, world: World) -> None:
    assert row(build(world), "ES_F").as_of_text == "06:59"
    w = World(tmp_path / "stale")
    w.observations(
        [
            daily("WTI", FRI, 72.0, source="yahoo", dataset="history", tier=2),
            # after Friday's close: a quote before it would give way to the close (PR #102)
            tick("WTI", et(FRI, "18:30"), 71.0),
        ]
    )
    wti = row(build(w), "WTI")
    assert wti.as_of_text == "13 Nov 18:30"


def test_baseline_date_is_part_of_the_description(world: World) -> None:
    es = row(build(world), "ES_F")
    assert es.description.endswith("since previous NYSE close (13 Nov)")


def test_derived_cross_is_a_known_limit(tmp_path: Path) -> None:
    w = World(tmp_path)
    ecb = {"source": "ecb", "dataset": "eurofxref", "field": "rate", "unit": "fx_rate"}
    w.observations(
        [
            daily("GBPUSD", date(2026, 11, 12), 1.30, is_derived=True, **ecb),
            daily("GBPUSD", FRI, 1.31, is_derived=True, **ecb),
        ]
    )
    m = build(w)
    assert "ECB derived cross rate" in row(m, "GBPUSD").description
    assert "GBPUSD" in {k.kind: k.text for k in m.known_limits}["derived"]


def _golden_text(tmp_path: Path, config: Config) -> str:
    brief_synth._ids = itertools.count(1)  # ids appear in lineage: make them order-independent
    world = World(tmp_path, config)
    world.observations(monday_rows())
    world.dq(MON, [("fresh", "pass", "yahoo", None), ("range", "fail", "stooq", "WTI")])
    world.xchecks(MON, [("sp500_spy_es", "warn", 7.0)], mirror=True)
    world.fetches(MON, [("yahoo", "ok")])
    model = BriefModel.from_store(world.connect(), world.config, MON, MON_0700)
    # config_hash is masked: it changes with any config edit and says nothing about the brief
    return replace(model, config_hash="MASKED").to_json()


def test_golden_json_snapshot(tmp_path: Path) -> None:
    # built from the frozen fixture config, not config.yaml, so unrelated config edits are safe
    text = _golden_text(tmp_path, fixture_config())
    json.loads(text, parse_constant=_reject_constant)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8")


def test_golden_survives_a_config_only_edit(tmp_path: Path) -> None:
    base = fixture_config()
    edited = base.model_copy(
        update={"checks": base.checks.model_copy(update={"anomaly": base.checks.anomaly.model_copy(
            update={"z_fail": 9.0})})}
    )  # fmt: skip
    assert config_hash(edited) != config_hash(base)
    assert _golden_text(tmp_path / "a", edited) == _golden_text(tmp_path / "b", base)
    assert _golden_text(tmp_path / "c", base) == GOLDEN.read_text(encoding="utf-8")


def test_model_records_the_real_config_hash(world: World) -> None:
    assert build(world).config_hash == config_hash(world.config)


def _reject_constant(name: str) -> None:
    raise AssertionError(f"non-finite number in JSON: {name}")


# -- T09B: typed values, dq_flag, latest-run health, anomaly reuse -------------------------

FLAGGED = {"WTI", "VIX", "ES_F", "SPY", "EURUSD", "UST_10Y", "UST_2S10S"}


def query_row(w: World, iid: str) -> ValueRow:
    return next(r for r in latest_snapshot(w.connect(), MON_0700) if r.instrument_id == iid)


def test_derived_row_has_typed_value_and_unit(world: World, tmp_path: Path) -> None:
    m = build(world)
    r = row(m, "UST_2S10S")
    assert r.value == pytest.approx(22.0)  # 10Y 4.40 minus 2Y 4.18 on 13 Nov, in bp
    assert r.value_unit == "bp"
    assert r.change_value == pytest.approx(-3.0)
    assert r.change_value_unit == "bp"
    es = row(m, "ES_F")  # ordinary rows carry the number behind the text too
    assert (es.value, es.value_unit) == (5976.0, "index_pts")
    assert es.change_value == pytest.approx(-0.4, abs=0.01)
    assert es.change_value_unit == "pct"
    gone = row(build(World(tmp_path / "empty")), "WTI")  # no data at all
    assert (gone.value, gone.value_unit, gone.change_value) == (None, None, None)


def test_2s10s_ignores_a_tier_two_daily_yield(tmp_path: Path) -> None:
    tsy = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
    yh = {"source": "yahoo", "dataset": "history", "field": "yield", "unit": "pct", "tier": 2}
    w = World(tmp_path)
    w.observations(
        [
            daily("UST_10Y", THU, 4.45, **tsy),
            daily("UST_2Y", THU, 4.20, **tsy),
            daily("UST_10Y", FRI, 4.90, **yh),  # a ^TNX-style 1d yield: newer, but not official
            daily("UST_2Y", FRI, 4.18, **tsy),
        ]
    )
    r = row(build(w), "UST_2S10S")
    assert r.value == pytest.approx(25.0)  # Thursday's two official legs, not 4.90 - 4.18
    assert r.as_of_text == "12 Nov"
    assert r.change_text == "n/a"
    assert r.change_value is None


def test_future_dated_quote_warns_instead_of_zero_minutes_old(world: World) -> None:
    # the store never returns an as_of after report_time, so this guards the formatter itself
    last = replace(query_row(world, "ES_F"), as_of=MON_0700 + timedelta(minutes=5))
    (q,) = _qualifiers(last, MON_0700)
    assert q == "delayed quote dated 5 min in the future: check the source's timestamp"
    assert "0 min old" not in q
    ahead = replace(last, as_of=MON_0700 + timedelta(seconds=90))
    assert "1 min in the future" in _qualifiers(ahead, MON_0700)[0]  # whole minutes, rounded down
    assert _qualifiers(replace(last, as_of=MON_0700), MON_0700) == ("delayed quote, 0 min old",)


def test_dq_flag_is_the_worst_result_for_the_instrument(world: World) -> None:
    world.dq(
        MON,
        [
            ("range", "fail", "stooq", "WTI"),
            ("fresh", "pass", "stooq", "WTI"),
            ("fresh", "pass", "yahoo", "VIX"),
            ("anom", "warn", None, "VIX"),
            ("fresh", "pass", "yahoo", "ES_F"),
            ("anom", "skip", None, "EURUSD"),
            ("fresh", "fail", "stooq", None),
        ],
    )
    world.xchecks(MON, [("sp500_spy_es", "warn", 7.0)])
    m = build(world)
    assert {r.instrument_id: r.dq_flag for r in m.rows() if r.instrument_id in FLAGGED} == {
        "WTI": "fail",
        "VIX": "warn",
        "ES_F": "warn",  # dq pass, but a cross-check warn
        "SPY": "warn",
        "EURUSD": "none",  # skip is no result
        "UST_10Y": "none",
        "UST_2S10S": "none",  # derived: the worst of its two legs, and neither has a result
    }


def test_dq_flag_of_the_spread_follows_its_worst_leg(world: World) -> None:
    world.dq(MON, [("range", "warn", None, "UST_2Y"), ("fresh", "pass", None, "UST_10Y")])
    m = build(world)
    assert row(m, "UST_2S10S").dq_flag == "warn"
    assert row(m, "UST_10Y").dq_flag == "pass"
    assert row(m, "ES_F").dq_flag == "none"


def test_health_and_crosscheck_items_use_the_latest_run_only(world: World) -> None:
    world.dq(MON, [("fresh", "fail", "yahoo", None)], run_id="R1")
    world.xchecks(MON, [("sp500_spy_es", "warn", 7.0)], run_id="R1")
    world.dq(MON, [("fresh", "pass", "yahoo", None)], at="08:00", run_id="R2")
    world.xchecks(MON, [("sp500_spy_es", "pass", 1.0)], at="08:00", run_id="R2")
    m = build(world)
    assert (m.health.status, m.health.text) == ("ok", "2/2 checks pass")
    assert m.notable == ()
    assert not {k.kind for k in m.known_limits} & {"dq_fail", "crosscheck_warn"}


def _trend_rows(last_move: float) -> list[dict[str, Any]]:
    hist = {"source": "yahoo", "dataset": "history", "tier": 2, "unit": "index_pts"}
    rows, d, v, n = [], date(2026, 10, 1), 6000.0, 0
    while d < FRI:
        if d.weekday() < 5:
            v += (4.0, 5.0, 6.0, 5.0)[n % 4]  # a steady climb of about 5 points a day
            n += 1
            rows.append(daily("ES_F", d, v, **hist))
        d += timedelta(days=1)
    rows.append(daily("ES_F", FRI, v, **hist))
    rows.append(tick("ES_F", et(MON, "06:59"), v + last_move, unit="index_pts"))
    return rows


def test_notable_scores_the_size_of_a_move_like_the_anomaly_check(tmp_path: Path) -> None:
    # T20's z uses |move| against |daily moves|: a drop as big as a usual daily rise is not
    # notable, though it has the opposite sign (a signed z would call it a 13-sigma event)
    w = World(tmp_path / "normal")
    w.observations(_trend_rows(-5.5))
    assert [n for n in build(w).notable if n.kind == "move"] == []
    w2 = World(tmp_path / "big")
    w2.observations(_trend_rows(-60.0))
    (move,) = [n for n in build(w2).notable if n.kind == "move"]
    assert move.instrument_id == "ES_F"
    assert "σ" in move.text


def test_notable_window_excludes_the_scored_official_move(tmp_path: Path) -> None:
    # official-last yield: the 15bp jump is both the scored move and the last daily move of the
    # series; T20 excludes it from its own window (moves[-(window+1):-1]), so must we
    tsy = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
    days: list[date] = []
    d = FRI
    while len(days) < 32:  # 31 earlier moves + the jump
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    v, rows = 4.00, []
    for n, day in enumerate(days[:-1]):
        v += (0.01, 0.02)[n % 2] if n else 0.0  # steady 1-2bp moves
        rows.append(daily("UST_10Y", day, v, **tsy))
    rows.append(daily("UST_10Y", days[-1], v + 0.15, **tsy))  # the 15bp jump on Friday
    w = World(tmp_path)
    w.observations(rows)
    (move,) = [n for n in build(w).notable if n.kind == "move"]
    assert move.text.endswith("vs prior 30 daily moves")
    z = float(move.text.split(" is ")[1].split("σ")[0])
    assert z > 20  # 15bp against ~1.5bp +/- 0.5; with itself in the window it would be ~5


# ---------------------------------------------------------------------------
# T09C: Row.as_of, the one-date spread tail, from_store(exclude_tiers=), limits text
# ---------------------------------------------------------------------------

_TSY = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}


def test_rows_carry_the_timestamp_of_their_value_and_the_spread_its_date_stamp(
    world: World,
) -> None:
    m = build(world)
    assert row(m, "UST_2S10S").as_of == et(FRI)  # shared date of the two legs, midnight ET
    assert row(m, "ES_F").as_of == et(MON, "06:59")
    assert row(m, "QQQ").as_of is None  # unavailable: no value, no stamp
    assert BriefModel.from_json(m.to_json()) == m


def test_spread_with_one_common_date_says_the_change_is_unavailable(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations([daily("UST_10Y", FRI, 4.40, **_TSY), daily("UST_2Y", FRI, 4.18, **_TSY)])
    r = row(build(w), "UST_2S10S")
    assert r.available
    assert r.change_text == "n/a"
    assert r.description.endswith("+22bp on 13 Nov, change unavailable (no earlier common date)")
    assert r.as_of == et(FRI)


def test_from_store_exclude_tiers_falls_back_to_the_next_tier(world: World) -> None:
    default = BriefModel.from_store(world.connect(), world.config, MON, MON_0700)
    assert row(default, "SPY").tier == 1
    assert default == BriefModel.from_store(
        world.connect(), world.config, MON, MON_0700, exclude_tiers=()
    )
    hosted = BriefModel.from_store(world.connect(), world.config, MON, MON_0700, exclude_tiers=(1,))
    spy = row(hosted, "SPY")
    assert (spy.tier, spy.value, spy.as_of) == (3, 598.5, et(FRI))  # the Friday close
    assert row(hosted, "ES_F").tier == 2  # untouched rows keep their value
    assert 1 not in {r.tier for r in hosted.rows()}


def test_from_store_takes_exclude_tiers_by_keyword_only(world: World) -> None:
    with pytest.raises(TypeError):
        BriefModel.from_store(world.connect(), world.config, MON, MON_0700, (1,))  # type: ignore[misc]


def test_known_limits_say_a_partial_rerun_supersedes_both_check_tables(
    world: World, tmp_path: Path
) -> None:
    world.dq(MON, [("fresh", "pass", "yahoo", None)])
    text = next(k.text for k in build(world).known_limits if k.kind == "checks_scope")
    assert "latest run" in text
    assert "partial re-run" in text
    assert "both check tables" in text
    kinds = {k.kind for k in build(World(tmp_path / "none")).known_limits}
    assert "checks_scope" not in kinds  # no results, nothing to scope (dq_missing says so)


# ---------------------------------------------------------------------------
# T60 bugs 4 and 5: a report date that is not an XNYS session, and the latest run's health
# ---------------------------------------------------------------------------

SAT = date(2026, 11, 14)
SAT_1000 = et(SAT, "10:00")


def test_saturday_row_describes_the_move_since_thursday_close(world: World) -> None:
    es = row(BriefModel.from_store(world.connect(), world.config, SAT, SAT_1000), "ES_F")
    # Friday's yahoo close (6000) against Thursday's (6024); the Monday tick is after SAT_1000
    assert es.change_text == "−0.4%"
    assert es.description.endswith("since Thu close (12 Nov)")
    assert es.basis == "Thu close"
    # session days are unchanged: Monday still reads the previous NYSE close (golden unchanged)
    assert row(build(world), "ES_F").description.endswith("since previous NYSE close (13 Nov)")


def test_health_date_shows_the_latest_run_on_a_saturday(world: World) -> None:
    world.dq(FRI, [("fresh", "pass", "yahoo", None), ("range", "warn", "stooq", "WTI")])
    world.xchecks(FRI, [("sp500_spy_es", "pass", 1.0)])
    db = world.connect()
    ref = latest_check_run(db, SAT_1000)
    assert ref is not None
    assert ref.target_date == FRI
    m = BriefModel.from_store(db, world.config, SAT, SAT_1000, health_date=ref.target_date)
    assert (m.health.status, m.health.text) == ("warn", "2/3 checks pass (0 fail, 1 warn)")
    assert m.health.summary.target_date == FRI
    assert row(m, "WTI").dq_flag == "warn"
    without = BriefModel.from_store(db, world.config, SAT, SAT_1000)  # reads Saturday: nothing
    assert without.health.status == "unknown"


def test_health_date_without_any_run_is_unknown(world: World) -> None:
    db = world.connect()
    assert latest_check_run(db, SAT_1000) is None
    m = BriefModel.from_store(db, world.config, SAT, SAT_1000, health_date=SAT)
    assert m.health.status == "unknown"
    assert m.health.text.startswith("No data-quality results")


def test_health_date_of_an_all_skipped_run_says_so_with_its_day(world: World) -> None:
    world.dq(THU, [("fresh", "pass", "yahoo", None)], run_id="R-THU")
    world.dq(FRI, [("fresh", "skip", "yahoo", None), ("anom", "skip", None, "VIX")], run_id="R-FRI")
    db = world.connect()
    ref = latest_check_run(db, SAT_1000)
    assert ref is not None
    assert (ref.target_date, ref.run_id) == (FRI, "R-FRI")
    m = BriefModel.from_store(db, world.config, SAT, SAT_1000, health_date=ref.target_date)
    assert (m.health.status, m.health.text) == (
        "unknown",
        "No data-quality results recorded for 2026-11-13",
    )
    assert (m.health.summary.target_date, m.health.summary.n_skip) == (FRI, 2)


def test_health_date_ignores_a_rerun_stored_after_report_time(world: World) -> None:
    world.dq(MON, [("fresh", "fail", "yahoo", None)], run_id="R1")  # 07:00 == MON_0700
    world.dq(MON, [("fresh", "pass", "yahoo", None)], at="08:00", run_id="R2")
    db = world.connect()
    m = BriefModel.from_store(db, world.config, MON, MON_0700, health_date=MON)
    assert (m.health.status, m.health.text) == ("fail", "0/1 checks pass (1 fail, 0 warn)")
    # without health_date nothing is bounded, as before (the scheduled email's behaviour)
    assert build(world).health.status == "ok"


# ---------------------------------------------------------------------------
# T61 (P-024): the brief scores sigma only for rankable changes
# ---------------------------------------------------------------------------

_TUE24, _MON23, _FRI20 = date(2026, 11, 24), date(2026, 11, 23), date(2026, 11, 20)


def _steady_daily(iid: str, last_day: date, start: float, **kw: Any) -> list[dict[str, Any]]:
    """40 weekdays up to ``last_day`` moving 1-2 hundredths a day (1-2 bp of a pct series)."""
    days: list[date] = []
    d = last_day
    while len(days) < 40:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    v, rows = start, []
    for n, day in enumerate(days):
        v += (0.01, 0.02)[n % 2] if n else 0.0
        rows.append(daily(iid, day, v, **kw))
    return rows


def _tuesday_notable(w: World) -> list[str]:
    model = BriefModel.from_store(w.connect(), w.config, _TUE24, et(_TUE24, "07:00"))
    return [n.instrument_id or "" for n in model.notable if n.kind == "move"]


def test_ust_3m_basis_gap_against_treasury_is_not_scored(tmp_path: Path) -> None:
    # the 24 Nov email scored a Yahoo ^IRX quote against Treasury's par yield as a notable move.
    # A quote against another series is shown with its note but never scored (values invented)
    rows = _steady_daily("UST_3M", _MON23, 4.00, **_TSY)
    official = rows[-1]["value"]
    rows.append(tick("UST_3M", et(_MON23, "14:59"), official - 0.19, unit="pct"))
    w = World(tmp_path)
    w.observations(rows)
    assert "UST_3M" not in _tuesday_notable(w)


_FRED_RATE = {"source": "fred", "dataset": "series", "field": "rate", "unit": "pct"}


def test_fred_move_already_shown_by_the_previous_brief_is_not_scored(tmp_path: Path) -> None:
    stale = _steady_daily("HY_OAS", _FRI20, 3.00, **_FRED_RATE)
    stale[-1]["value"] += 0.30  # a big move, stored Friday evening: Monday's brief showed it
    w = World(tmp_path / "stale")
    w.observations(stale)
    assert _tuesday_notable(w) == []
    # control: the same jump dated Monday (stored after the previous brief) is new and scored
    fresh = _steady_daily("HY_OAS", _MON23, 3.00, **_FRED_RATE)
    fresh[-1]["value"] += 0.30
    w2 = World(tmp_path / "fresh")
    w2.observations(fresh)
    assert _tuesday_notable(w2) == ["HY_OAS"]


def test_late_published_fred_move_is_scored_once(tmp_path: Path) -> None:
    late = _steady_daily("HY_OAS", _FRI20, 3.00, **_FRED_RATE)
    late[-1]["value"] += 0.30
    late[-1]["ingested_at"] = et(_MON23, "10:00")  # Friday's print, first stored on Monday
    w = World(tmp_path)
    w.observations(late)
    assert _tuesday_notable(w) == ["HY_OAS"]
    wed = date(2026, 11, 25)
    model = BriefModel.from_store(w.connect(), w.config, wed, et(wed, "07:00"))
    assert [n for n in model.notable if n.kind == "move"] == []
