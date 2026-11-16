"""BriefModel: labels, baselines, known limits, health, notable moves, JSON round trip.

SYNTHETIC FIXTURE: all data comes from ``brief_synth`` (made-up values, labelled there).
"""
# ruff: noqa: RUF001  (true minus sign and sigma are intentional output)

from __future__ import annotations

import itertools
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

import brief_synth
from brief_synth import FRI, MON, MON_0700, World, daily, et, monday_rows, tick
from deskbrief.report.model import BriefModel, Row, signed

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
    assert r.description == "US 10-year Treasury yield, −3bp since previous official value (13 Nov)"
    assert r.value_text == "4.37%"


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
            tick("WTI", et(FRI, "15:30"), 71.0),
        ]
    )
    wti = row(build(w), "WTI")
    assert wti.as_of_text == "13 Nov 15:30"


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


def test_golden_json_snapshot(tmp_path: Path) -> None:
    brief_synth._ids = itertools.count(1)  # ids appear in lineage: make them order-independent
    world = World(tmp_path)
    world.observations(monday_rows())
    world.dq(MON, [("fresh", "pass", "yahoo", None), ("range", "fail", "stooq", "WTI")])
    world.xchecks(MON, [("sp500_spy_es", "warn", 7.0)])
    world.fetches(MON, [("yahoo", "ok")])
    text = build(world).to_json()
    json.loads(text, parse_constant=_reject_constant)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8")


def _reject_constant(name: str) -> None:
    raise AssertionError(f"non-finite number in JSON: {name}")
