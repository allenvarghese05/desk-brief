"""BriefModel's P-024 sections (card T62): the ranking, the read and Check before using.

SYNTHETIC FIXTURE: every value below is made up. Histories move by a repeating cycle of 1 to 5
steps, so a final move of ``k`` steps scores about ``(k - 3) / 1.44`` sigma: 8 steps is a mover
(at or above ``checks.anomaly.z_warn`` = 3), 5 steps meets a 1-sigma regime rule, 3 steps is
nothing.
"""
# ruff: noqa: RUF001  (the sigma sign is intentional output)

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from brief_synth import FRI, MON, MON_0700, THU, World, daily, et, tick
from deskbrief.calendars import is_business_day
from deskbrief.query import ChangeRow, ValueRow
from deskbrief.report.model import BriefModel, Row, _caveats

CYCLE = (1, 2, 3, 4, 5)
START = date(2026, 9, 1)


def _history(iid: str, base: float, step: float, end: date, **kw: Any) -> list[dict[str, Any]]:
    """Daily rows from START to ``end`` (XNYS days), moving by the 1-5 step cycle, alternating
    sign. ``step`` is a return for prices, a yield change in percentage points for ``pct``."""
    rows, v, d, n = [], base, START, 0
    pct = kw.get("unit") == "pct"
    while d <= end:
        if is_business_day("XNYS", d):
            rows.append(daily(iid, d, round(v, 6), **kw))
            sign = 1 if n % 2 == 0 else -1
            v = v + sign * CYCLE[n % 5] * step if pct else v * (1 + sign * CYCLE[n % 5] * step)
            n += 1
        d += timedelta(days=1)
    return rows


YH = {"source": "yahoo", "dataset": "history", "tier": 2}
CBOE = {"source": "cboe", "dataset": "vix_history"}
TSY = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
FRED = {"source": "fred", "dataset": "series", "field": "close", "unit": "pct"}


def price(
    iid: str,
    k: float,
    *,
    base: float = 100.0,
    step: float = 0.002,
    at: str = "06:58",
    unit: str = "USD",
    hist: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """A price history to Friday and a Monday quote ``k`` steps from Friday's close."""
    rows = _history(iid, base, step, FRI, unit=unit, **(hist or YH))
    fri = rows[-1]["value"]
    return [*rows, tick(iid, et(MON, at), fri * (1 + k * step), unit=unit)]


def official(iid: str, k: float, *, base: float = 4.0, step: float = 0.01,
             kw: dict[str, Any] = TSY) -> list[dict[str, Any]]:  # fmt: skip
    """An official daily series to Thursday and Friday's value ``k`` steps from Thursday's."""
    rows = _history(iid, base, step, THU, **kw)
    return [*rows, daily(iid, FRI, round(rows[-1]["value"] + k * step, 6), **kw)]


def build(w: World) -> BriefModel:
    return BriefModel.from_store(w.connect(), w.config, MON, MON_0700)


def world(tmp_path: Path, *series: list[dict[str, Any]]) -> World:
    w = World(tmp_path)
    w.observations([r for s in series for r in s])
    return w


def row(m: BriefModel, iid: str) -> Row:
    return next(r for r in m.rows() if r.instrument_id == iid)


# ---------------------------------------------------------------------------
# the ranking
# ---------------------------------------------------------------------------


def test_movers_are_rankable_changes_at_or_above_z_warn_in_sigma_order(tmp_path: Path) -> None:
    w = world(
        tmp_path,
        price("ES_F", -8, base=6000.0, unit="index_pts"),
        price("WTI", 10),
        price("BRENT", 4),  # about 0.7 sigma: scored, not a mover
        official("UST_2Y", -9),
    )
    m = build(w)
    assert [x.instrument_id for x in m.movers] == ["WTI", "UST_2Y", "ES_F"]
    sigmas = [x.sigma for x in m.movers]
    assert sigmas == sorted(sigmas, reverse=True)
    assert all(x.sigma >= w.config.checks.anomaly.z_warn for x in m.movers)
    assert row(m, "BRENT").sigma is not None
    assert row(m, "BRENT").sigma < w.config.checks.anomaly.z_warn  # type: ignore[operator]
    es = next(x for x in m.movers if x.instrument_id == "ES_F")
    assert es.short == "S&P fut"
    assert es.direction == "down"
    assert es.source == "Yahoo"
    assert es.tier == 2
    assert es.basis.startswith("previous NYSE close (13 Nov)")
    assert es.text.startswith("S&P fut −1.6% is ")
    # the email's notable list is the same ranking, in the same order
    assert [n.instrument_id for n in m.notable if n.kind == "move"] == ["WTI", "UST_2Y", "ES_F"]


def test_an_unrankable_change_is_never_a_mover_however_large(tmp_path: Path) -> None:
    tsy = official("UST_10Y", 0)
    w = world(tmp_path, tsy, [tick("UST_10Y", et(MON, "06:58"), 9.0, unit="pct")])
    m = build(w)
    r = row(m, "UST_10Y")
    assert r.change is not None
    assert r.change.status == "ok"
    assert not r.change.rankable  # a Yahoo quote against Treasury's official yield
    assert r.sigma is None
    assert m.movers == ()


def test_a_quoted_yield_is_scored_against_its_own_closes(tmp_path: Path) -> None:
    own = {"source": "yahoo", "dataset": "history", "field": "close", "tier": 2, "unit": "pct"}
    hist = _history("UST_3M", 4.0, 0.01, FRI, **own)
    w = world(
        tmp_path,
        hist,
        official("UST_3M", 0, step=0.5),  # the official par yield moves far more: never used
        [tick("UST_3M", et(MON, "06:58"), hist[-1]["value"] - 0.08, unit="pct")],
    )
    r = row(build(w), "UST_3M")
    assert r.change is not None
    assert r.change.rankable
    assert r.change.basis == "previous Yahoo close"
    assert r.sigma == pytest.approx((8 - 3) / 1.438, abs=0.05)
    assert r.sigma_note is None


def test_a_quoted_yield_without_its_own_history_is_left_unscored_and_says_why(
    tmp_path: Path,
) -> None:
    own = {"source": "yahoo", "dataset": "history", "field": "close", "tier": 2, "unit": "pct"}
    w = world(
        tmp_path,
        official("UST_3M", 0),
        [
            daily("UST_3M", FRI, 4.0, **own),
            tick("UST_3M", et(MON, "06:58"), 3.5, unit="pct"),
        ],
    )
    m = build(w)
    r = row(m, "UST_3M")
    assert r.change is not None
    assert r.change.rankable
    assert r.sigma is None
    assert r.sigma_note is not None
    assert "official yield history measures another basis" in r.sigma_note
    assert m.movers == ()


def test_hosted_movers_never_hold_a_tier_one_value(tmp_path: Path) -> None:
    alp = {"source": "alpaca", "dataset": "iex_latest", "tier": 1}
    hist = _history("AAPL", 200.0, 0.002, FRI, unit="USD", **YH)
    w = world(tmp_path, hist, [tick("AAPL", et(MON, "06:58"), hist[-1]["value"] * 1.02, **alp)])
    local = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    hosted = BriefModel.from_store(w.connect(), w.config, MON, MON_0700, exclude_tiers=(1,))
    assert [x.instrument_id for x in local.movers] == ["AAPL"]
    assert all(x.tier != 1 for x in hosted.movers)
    assert all(r.tier != 1 for r in hosted.rows() if r.available)


# ---------------------------------------------------------------------------
# the read
# ---------------------------------------------------------------------------


def _read(tmp_path: Path, es: float, vix: float, hy: float = 3, two: float = 3) -> BriefModel:
    w = world(
        tmp_path,
        price("ES_F", es, base=6000.0, unit="index_pts"),
        price("VIX", vix, base=16.0, unit="index_pts", hist=CBOE),
        official("HY_OAS", hy, kw=FRED),
        official("UST_2Y", two),
    )
    return build(w)


def test_read_risk_off_lists_the_rules_met(tmp_path: Path) -> None:
    m = _read(tmp_path, es=-8, vix=5)
    assert m.read is not None
    assert m.read.label == "Risk-off"
    assert m.read.kind == "regime"
    assert m.read.tone == "down"
    assert m.read.regime == "Risk-off"
    assert [r.label for r in m.read.met] == ["Equity futures down", "VIX up"]
    assert m.read.text == "Risk-off. Equity futures down and VIX up."
    assert m.read.n_ranked == len(m.movers) >= 1
    assert len(m.read.rules) == 8  # every configured rule, met or not


def test_read_risk_on(tmp_path: Path) -> None:
    m = _read(tmp_path, es=8, vix=-5)
    assert m.read is not None
    assert (m.read.label, m.read.tone) == ("Risk-on", "up")


def test_read_mixed_when_the_rules_split(tmp_path: Path) -> None:
    m = _read(tmp_path, es=-8, vix=-5)  # equity futures down (risk-off), VIX down (risk-on)
    assert m.read is not None
    assert (m.read.label, m.read.kind, m.read.tone) == ("Mixed", "mixed", "flat")
    assert m.read.regime is None
    assert m.read.text == (
        "Mixed. Rules met for Risk-off (Equity futures down) and Risk-on (VIX down)."
    )


def test_read_quiet_when_nothing_is_ranked(tmp_path: Path) -> None:
    m = _read(tmp_path, es=-5, vix=5)  # rules met at 1 sigma, but nothing moved more than usual
    assert m.movers == ()
    assert m.read is not None
    assert (m.read.label, m.read.kind) == ("Quiet", "quiet")
    assert m.read.text == "Quiet. Nothing moved more than usual."


def test_read_no_clear_read_when_too_few_rules_are_met(tmp_path: Path) -> None:
    m = _read(tmp_path, es=-8, vix=3)
    assert m.read is not None
    assert (m.read.label, m.read.kind) == ("No clear read", "unclear")
    assert m.read.regime == "Risk-off"
    assert m.read.text == (
        "No clear read. 1 market moved more than usual; 1 of the 2 Risk-off rules needed are met."
    )


def test_read_of_an_empty_store_is_quiet(tmp_path: Path) -> None:
    m = build(World(tmp_path))
    assert m.read is not None
    assert m.read.label == "Quiet"
    assert m.caveats == ()


# ---------------------------------------------------------------------------
# Check before using
# ---------------------------------------------------------------------------


def test_crosscheck_on_a_ranked_market_is_a_caveat_and_marks_the_mover(tmp_path: Path) -> None:
    w = world(tmp_path, price("ES_F", -8, base=6000.0, unit="index_pts"))
    w.xchecks(MON, [("sp500_spy_es", "warn", 7.0)])
    m = build(w)
    c = next(c for c in m.caveats if c.kind == "crosscheck")
    assert c.title == "S&P fut"
    assert c.instrument_ids == ("ES_F",)
    assert "Cross-check sp500_spy_es" in c.text
    assert "WARN" in c.text
    assert m.movers[0].check


def test_crosscheck_on_an_unranked_market_is_not_a_caveat(tmp_path: Path) -> None:
    w = world(tmp_path, price("ES_F", -3, base=6000.0, unit="index_pts"))
    w.xchecks(MON, [("sp500_spy_es", "fail", 30.0)])
    assert [c for c in build(w).caveats if c.kind == "crosscheck"] == []


def _value(iid: str, source: str = "yahoo") -> ValueRow:
    return ValueRow(
        iid, iid, "rates", "yield", False, None, 4.0, "pct", "yield", "tick", source, "quote",
        "SYN", 2, MON_0700, False, False, "F1", "R1", MON_0700, "synthetic-1",
    )  # fmt: skip


def test_basis_note_on_a_ranked_move_is_a_caveat(tmp_path: Path) -> None:
    m = build(world(tmp_path, price("ES_F", -8, base=6000.0, unit="index_pts")))
    rows = {r.instrument_id: r for r in m.rows()}
    es = rows["ES_F"]
    assert es.change is not None
    noted: ChangeRow = replace(es.change, note="baseline from stooq daily, not yahoo quote")
    rows["ES_F"] = replace(es, change=noted)
    from deskbrief.query import HealthSummary

    empty = HealthSummary(MON, None, None, 0, 0, 0, 0, 0, 0, 0, (), ())
    caveats = _caveats(World(tmp_path / "c").config, rows, m.movers, empty, {})
    basis = [c for c in caveats if c.kind == "basis"]
    assert [(c.title, c.text) for c in basis] == [
        ("S&P fut", "Baseline from stooq daily, not yahoo quote.")
    ]


def test_related_pair_moving_apart_on_matching_windows_is_a_caveat(tmp_path: Path) -> None:
    w = world(tmp_path, price("BRENT", -15, base=75.0), price("WTI", -2, base=70.0))
    m = build(w)
    c = next(c for c in m.caveats if c.kind == "pair")
    assert c.title == "Brent vs WTI"
    assert c.instrument_ids == ("BRENT", "WTI")
    assert c.text == (
        "2.6% apart (Brent −3.0%, WTI −0.4%, since the Fri 13 Nov close); "
        "they usually move within 1%."
    )
    assert next(x for x in m.movers if x.instrument_id == "BRENT").check


def test_related_pair_within_its_limit_is_not_a_caveat(tmp_path: Path) -> None:
    w = world(tmp_path, price("BRENT", -4, base=75.0), price("WTI", -2, base=70.0))
    assert [c for c in build(w).caveats if c.kind == "pair"] == []


def test_related_pair_on_different_windows_compares_daily_closes(tmp_path: Path) -> None:
    gold = price("GOLD_F", 1, base=2000.0, at="06:55")
    gld = _history("GLD", 185.0, 0.002, THU, unit="USD", **YH)
    thu = gld[-1]["value"]
    gld += [
        daily("GLD", FRI, round(thu * 1.03, 6), unit="USD", **YH),  # +3% Thursday to Friday
        tick("GLD", et(MON, "04:10"), thu * 1.03 * 1.001, unit="USD"),  # pre-market, early
    ]
    m = build(world(tmp_path, gold, gld))
    c = next(c for c in m.caveats if c.kind == "pair")
    assert c.title == "Gold vs GLD"
    assert "daily closes Thu 12 Nov to Fri 13 Nov" in c.text
    assert "GLD +3.0%" in c.text


def test_related_pair_with_an_unranked_leg_is_skipped(tmp_path: Path) -> None:
    gold = price("GOLD_F", 12, base=2000.0, at="06:55")
    gld = _history("GLD", 185.0, 0.002, FRI, unit="USD", **YH)
    gld.append(tick("GLD", et(FRI, "15:00"), gld[-1]["value"], unit="USD"))  # stale, Friday
    assert [c for c in build(world(tmp_path, gold, gld)).caveats if c.kind == "pair"] == []


def test_previous_brief_is_the_last_session_at_the_same_clock_time(tmp_path: Path) -> None:
    assert build(World(tmp_path)).previous_brief == et(FRI, "07:00")


def test_model_json_round_trip_keeps_the_p024_sections(tmp_path: Path) -> None:
    w = world(tmp_path, price("BRENT", -15, base=75.0), price("WTI", -2, base=70.0))
    m = build(w)
    back = BriefModel.from_json(m.to_json())
    assert back == m
    assert back.read is not None
    assert back.movers == m.movers


def test_an_older_model_json_without_the_p024_sections_still_loads(tmp_path: Path) -> None:
    import json

    m = build(World(tmp_path))
    data = json.loads(m.to_json())
    for key in ("movers", "read", "caveats", "previous_brief"):
        data.pop(key)
    for sec in data["sections"]:
        for r in sec["rows"]:
            r.pop("sigma")
            r.pop("sigma_note")
    old = BriefModel.from_json(json.dumps(data))
    assert old.movers == ()
    assert old.read is None
