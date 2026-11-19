"""proxy_vs_exact: daily return of a proxy ETF vs the exact instrument / yield-implied move.

SYNTHETIC FIXTURE: all data is made up. Reference session Friday 2026-11-13, previous session
Thursday 2026-11-12 (00:00 ET = 05:00Z for the ``1d`` as_of). Run at Monday 2026-11-16 07:00 ET.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from xc_fakes import DAY_FRI, duck_for, fred, make_ctx, obs, put, stooq_close

from deskbrief.calendars import is_business_day
from deskbrief.config import Config, Group, Tolerance
from deskbrief.crosschecks.base import registered, run_crosschecks
from deskbrief.crosschecks.proxy_exact import ProxyVsExact, in_roll_window
from deskbrief.models import DQStatus, XResult
from deskbrief.store.local import LocalBackend

THU = datetime(2026, 11, 12, 5, 0, tzinfo=UTC)


def closes(
    inst: str, prev: float, cur: float, days: tuple[datetime, datetime] = (THU, DAY_FRI)
) -> list[dict[str, Any]]:
    return [stooq_close(inst, prev, as_of=days[0]), stooq_close(inst, cur, as_of=days[1])]


def history(
    inst: str,
    symbol: str,
    prev: float,
    cur: float,
    unit: str,
    days: tuple[datetime, datetime] = (THU, DAY_FRI),
) -> list[dict[str, Any]]:
    def row(as_of: datetime, v: float) -> dict[str, Any]:
        return obs(
            instrument_id=inst, source="yahoo", dataset="history", symbol=symbol, tier=2,
            field="close", interval="1d", as_of=as_of, value=v, unit=unit,
        )  # fmt: skip

    return [row(days[0], prev), row(days[1], cur)]


def yields(inst: str, series: str, prev: float, cur: float) -> list[dict[str, Any]]:
    return [fred(inst, series, prev, as_of=THU), fred(inst, series, cur, as_of=DAY_FRI)]


def run(store: LocalBackend, config: Config, **kw: Any) -> dict[str, XResult]:
    ctx = make_ctx(config, store, **kw)
    return {r.group_id or "": r for r in ProxyVsExact().evaluate(ctx, duck_for(store, config))}


def sp500(store: LocalBackend, config: Config, proxy_cur: float, es_cur: float) -> XResult:
    put(
        store,
        config,
        closes("SPY", 100.0, proxy_cur) + history("ES_F", "ES=F", 5000.0, es_cur, "index_pts"),
    )
    return run(store, config)["sp500"]


# --- return relation (sp500: warn 30 bp, fail 100 bp on the difference of daily returns) -------


@pytest.mark.parametrize(
    ("proxy_cur", "diff_bps", "status"),
    [
        (101.00, 0.0, DQStatus.PASS),  # both +1.00%
        (101.30, 30.0, DQStatus.PASS),  # exactly warn
        (101.31, 31.0, DQStatus.WARN),
        (102.00, 100.0, DQStatus.WARN),  # exactly fail
        (102.01, 101.0, DQStatus.FAIL),
        (100.00, -100.0, DQStatus.WARN),  # proxy flat: |move| < 25 bp, no direction rule
    ],
)
def test_return_boundaries(
    store: LocalBackend, config: Config, proxy_cur: float, diff_bps: float, status: DQStatus
) -> None:
    r = sp500(store, config, proxy_cur, 5050.0)  # exact +1.00%
    assert r.status is status
    assert r.diff_bps == pytest.approx(diff_bps, abs=1e-6)
    assert (r.warn_bps, r.fail_bps) == (30.0, 100.0)
    assert (r.left_instrument, r.right_instrument) == ("SPY", "ES_F")
    assert (r.left_source, r.right_source) == ("stooq", "yahoo")
    assert r.relation == "return"
    assert r.left_value == pytest.approx(proxy_cur / 100 - 1)
    assert r.expected_value == pytest.approx(0.01)
    assert r.at_time == DAY_FRI


def test_return_details_label_both_sides(store: LocalBackend, config: Config) -> None:
    d = sp500(store, config, 101.0, 5050.0).details
    assert d["ref_date"] == "2026-11-13"
    assert d["prev_date"] == "2026-11-12"
    assert (d["left"]["source"], d["left"]["dataset"]) == ("stooq", "daily")
    assert (d["left"]["field"], d["left"]["unit"]) == ("close", "USD")
    assert (d["left"]["prev_value"], d["left"]["value"]) == (100.0, 101.0)
    assert (d["right"]["source"], d["right"]["dataset"]) == ("yahoo", "history")
    assert (d["right"]["field"], d["right"]["unit"]) == ("close", "index_pts")
    assert d["right"]["return"] == pytest.approx(0.01)
    assert d["basis"] == "daily_return_diff"
    assert d["roll_window"] is False


@pytest.mark.parametrize(
    ("proxy_cur", "es_cur", "status", "direction"),
    [
        (100.30, 4985.0, DQStatus.FAIL, True),  # +30 bp vs -30 bp: both > 25 bp, opposite sign
        (99.70, 5015.0, DQStatus.FAIL, True),
        (100.25, 4980.0, DQStatus.WARN, False),  # +25 bp is not > 25 bp (diff 65 bp -> warn)
        (100.30, 4990.0, DQStatus.WARN, False),  # -20 bp is not > 25 bp (diff 50 bp -> warn)
        (100.30, 5015.0, DQStatus.PASS, False),  # same direction, diff 0
    ],
)
def test_direction_disagreement_rule(
    store: LocalBackend,
    config: Config,
    proxy_cur: float,
    es_cur: float,
    status: DQStatus,
    direction: bool,
) -> None:
    r = sp500(store, config, proxy_cur, es_cur)
    assert r.status is status
    assert r.details["direction_disagreement"] is direction


def test_direction_rule_overrides_small_difference_thresholds(
    store: LocalBackend, config: Config
) -> None:
    # +30 bp vs -26 bp: |diff| = 56 bp is only a warn by threshold, direction rule makes it a fail
    r = sp500(store, config, 100.30, 5000.0 * (1 - 0.0026))
    assert r.diff_bps == pytest.approx(56.0, abs=1e-6)
    assert r.status is DQStatus.FAIL


def spot_wti(prev: float, cur: float) -> list[dict[str, Any]]:
    """FRED spot WTI (tier 3): present in config, but not the exact leg of spec §3."""

    def row(as_of: datetime, v: float) -> dict[str, Any]:
        return obs(
            instrument_id="WTI", source="fred", dataset="series", symbol="DCOILWTICO", tier=3,
            field="close", interval="1d", as_of=as_of, value=v, unit="USD",
        )  # fmt: skip

    return [row(THU, prev), row(DAY_FRI, cur)]


def test_wti_roll_window_is_not_modelled(store: LocalBackend, config: Config) -> None:
    # oil_wti: warn 150 bp, fail 400 bp. No WTI roll calendar yet, so a fail is never softened
    put(
        store,
        config,
        closes("USO", 100.0, 100.0) + history("WTI", "CL=F", 60.0, 66.0, "USD"),  # 0% vs +10%
    )
    r = run(store, config)["oil_wti"]
    assert r.diff_bps == pytest.approx(-1000.0)
    assert r.status is DQStatus.FAIL
    assert r.details["roll_window"] == "not modelled"
    assert r.details["capped_at_warn"] is False


def test_wti_direction_disagreement_still_fails(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        closes("USO", 100.0, 100.5) + history("WTI", "CL=F", 60.0, 59.7, "USD"),  # +50 vs -50 bp
    )
    r = run(store, config)["oil_wti"]
    assert r.details["direction_disagreement"] is True
    assert r.status is DQStatus.FAIL


def test_gold_roll_window_is_not_modelled(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        closes("GLD", 100.0, 100.0) + history("GOLD_F", "GC=F", 2000.0, 2100.0, "USD"),
    )
    r = run(store, config)["gold"]
    assert r.status is DQStatus.FAIL
    assert r.details["roll_window"] == "not modelled"
    assert r.details["capped_at_warn"] is False


def test_exact_leg_is_the_yahoo_future_not_spot(store: LocalBackend, config: Config) -> None:
    # only FRED spot exists for WTI: skip with a reason, never a silent switch to spot
    put(store, config, closes("USO", 100.0, 100.0) + spot_wti(60.0, 66.0))
    r = run(store, config)["oil_wti"]
    assert r.status is DQStatus.SKIP
    assert "WTI/close from yahoo" in r.details["reason"]
    # with both present the (lower-tier) Yahoo future wins over the tier-3 spot
    put(store, config, history("WTI", "CL=F", 60.0, 60.3, "USD"))
    r = run(store, config)["oil_wti"]
    assert r.right_source == "yahoo"
    assert r.right_value == pytest.approx(0.005)


# Dec 2026: ES_F third Friday is the 18th, window 10-19 Dec. Fri 11 Dec vs Thu 10 Dec.
ES_DAYS = (datetime(2026, 12, 10, 5, 0, tzinfo=UTC), datetime(2026, 12, 11, 5, 0, tzinfo=UTC))
ES_NOW = datetime(2026, 12, 14, 12, 0, tzinfo=UTC)
# Nov 2026: BRENT window is the last 3 sessions (25, 27, 30 Nov). Wed 25 Nov vs Tue 24 Nov.
BR_DAYS = (datetime(2026, 11, 24, 5, 0, tzinfo=UTC), datetime(2026, 11, 25, 5, 0, tzinfo=UTC))
BR_NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def test_direction_fail_inside_an_es_roll_window_is_capped_at_warn(
    store: LocalBackend, config: Config
) -> None:
    # spec §8.2: roll windows cap at warn, and that includes the direction-disagreement fail
    put(
        store,
        config,
        closes("SPY", 100.0, 100.30, ES_DAYS)  # +30 bp
        + history("ES_F", "ES=F", 5000.0, 4985.0, "index_pts", ES_DAYS),  # -30 bp
    )
    r = run(store, config, target=ES_NOW.date(), now=ES_NOW)["sp500"]
    assert r.details["ref_date"] == "2026-12-11"
    assert r.details["direction_disagreement"] is True
    assert r.details["roll_window"] is True
    assert r.details["capped_at_warn"] is True
    assert r.status is DQStatus.WARN


def test_direction_fail_inside_a_brent_roll_window_is_capped_at_warn(
    store: LocalBackend, config: Config
) -> None:
    put(
        store,
        config,
        closes("BNO", 100.0, 100.5, BR_DAYS)  # +50 bp
        + history("BRENT", "BZ=F", 80.0, 79.6, "USD", BR_DAYS),  # -50 bp
    )
    r = run(store, config, target=BR_NOW.date(), now=BR_NOW)["oil_brent"]
    assert r.details["ref_date"] == "2026-11-25"
    assert r.details["direction_disagreement"] is True
    assert r.details["roll_window"] is True
    assert r.status is DQStatus.WARN


@pytest.mark.parametrize(
    ("inst", "day", "expected"),
    [
        ("ES_F", date(2026, 12, 9), False),  # third Friday of Dec 2026 is the 18th
        ("ES_F", date(2026, 12, 10), True),  # 8 days before
        ("ES_F", date(2026, 12, 19), True),  # 1 day after
        ("ES_F", date(2026, 12, 20), False),
        ("ES_F", date(2026, 11, 13), False),  # not a quarterly month
        ("BRENT", date(2026, 11, 24), False),  # last sessions: 25, 27, 30 Nov
        ("BRENT", date(2026, 11, 25), True),
        ("BRENT", date(2026, 12, 1), True),  # first session of the next month
        ("BRENT", date(2026, 12, 2), False),
        ("WTI", date(2026, 11, 13), False),  # not modelled
        ("GOLD_F", date(2026, 11, 30), False),
        ("SPY", date(2026, 11, 30), False),
    ],
)
def test_in_roll_window(inst: str, day: date, expected: bool) -> None:
    assert in_roll_window(inst, day) is expected


@pytest.mark.parametrize(("inst", "sessions"), [("ES_F", 27), ("BRENT", 48), ("WTI", 0)])
def test_roll_window_length_over_2026(inst: str, sessions: int) -> None:
    # APPROXIMATION pending a roll-calendar card: these are the counts the current rules give,
    # out of 251 XNYS sessions in 2026. Pinned so a rule change that widens a window (and so
    # disables fails on more days) shows up in review.
    days = [date(2026, 1, 1) + timedelta(days=n) for n in range(365)]
    n = sum(is_business_day("XNYS", d) and in_roll_window(inst, d) for d in days)
    assert n == sessions


# --- inverse_duration relation (TLT: D = 16.5, warn 40 bp, fail 100 bp) -------------------------


def tlt(
    store: LocalBackend, config: Config, tlt_cur: float, y_prev: float, y_cur: float
) -> XResult:
    put(store, config, closes("TLT", 100.0, tlt_cur) + yields("UST_20Y", "DGS20", y_prev, y_cur))
    return run(store, config)["ust_long"]


def test_inverse_duration_expected_move(store: LocalBackend, config: Config) -> None:
    # D = 16.5, yield +10 bp -> expected -1.65%; actual -1.65% -> no gap
    r = tlt(store, config, 98.35, 4.00, 4.10)
    assert r.status is DQStatus.PASS
    assert r.expected_value == pytest.approx(-0.0165)
    assert r.left_value == pytest.approx(-0.0165)
    assert r.diff_bps == pytest.approx(0.0, abs=1e-6)
    assert r.relation == "inverse_duration"
    assert (r.left_instrument, r.right_instrument) == ("TLT", "UST_20Y")
    assert (r.left_source, r.right_source) == ("stooq", "fred")
    d = r.details
    assert d["duration"] == 16.5
    assert d["delta_yield_bp"] == pytest.approx(10.0)
    assert d["expected_return"] == pytest.approx(-0.0165)
    assert (d["right"]["field"], d["right"]["unit"]) == ("yield", "pct")
    assert d["basis"] == "daily_return_diff"


@pytest.mark.parametrize(
    ("tlt_cur", "diff_bps", "status"),
    [
        (98.75, 40.0, DQStatus.PASS),  # actual -1.25% vs expected -1.65%: exactly warn
        (98.76, 41.0, DQStatus.WARN),
        (99.35, 100.0, DQStatus.WARN),  # exactly fail
        (99.36, 101.0, DQStatus.FAIL),
        (97.00, -135.0, DQStatus.FAIL),
    ],
)
def test_inverse_duration_boundaries(
    store: LocalBackend, config: Config, tlt_cur: float, diff_bps: float, status: DQStatus
) -> None:
    r = tlt(store, config, tlt_cur, 4.00, 4.10)
    assert r.status is status
    assert r.diff_bps == pytest.approx(diff_bps, abs=1e-6)
    assert (r.warn_bps, r.fail_bps) == (40.0, 100.0)


def test_inverse_duration_yield_falls_means_price_rises(
    store: LocalBackend, config: Config
) -> None:
    r = tlt(store, config, 101.65, 4.10, 4.00)  # yield -10 bp -> +1.65%
    assert r.expected_value == pytest.approx(0.0165)
    assert r.status is DQStatus.PASS


def test_inverse_duration_prefers_the_official_yield(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        closes("IEF", 100.0, 99.65)  # D = 7.0, +5 bp -> -0.35%
        + yields("UST_10Y", "DGS10", 4.00, 4.05)
        + history("UST_10Y", "^TNX", 4.00, 4.50, "pct"),  # tier 2 must not win
    )
    r = run(store, config)["ust_10y"]
    assert r.status is DQStatus.PASS
    assert r.right_source == "fred"
    assert r.details["delta_yield_bp"] == pytest.approx(5.0)


def test_inverse_duration_falls_back_to_the_exact_yield_source(
    store: LocalBackend, config: Config
) -> None:
    put(
        store,
        config,
        closes("IEF", 100.0, 99.65) + history("UST_10Y", "^TNX", 4.00, 4.05, "pct"),
    )
    r = run(store, config)["ust_10y"]
    assert r.right_source == "yahoo"
    assert r.status is DQStatus.PASS


def test_official_yield_takes_precedence_over_exact_when_they_differ(
    store: LocalBackend, config: Config
) -> None:
    # synthetic group: official = UST_2Y, exact = UST_10Y (config's real groups use one id for both)
    g = Group(
        id="prec", label="precedence", proxy="SHY", exact="UST_10Y", official="UST_2Y",
        relation="inverse_duration", duration=1.9, tolerance=Tolerance(warn_bps=10, fail_bps=30),
    )  # fmt: skip
    cfg = config.model_copy(update={"groups": (g,)})
    both = closes("SHY", 100.0, 99.81) + yields("UST_2Y", "DGS2", 4.00, 4.10)  # -0.19%
    both += yields("UST_10Y", "DGS10", 4.00, 4.50)
    put(store, config, both)
    r = ProxyVsExact().evaluate(make_ctx(cfg, store), duck_for(store, cfg))[0]
    assert (r.right_instrument, r.status) == ("UST_2Y", DQStatus.PASS)
    assert r.details["delta_yield_bp"] == pytest.approx(10.0)


def test_exact_yield_is_used_only_when_official_has_no_data(
    store: LocalBackend, config: Config
) -> None:
    g = Group(
        id="prec", label="precedence", proxy="SHY", exact="UST_10Y", official="UST_2Y",
        relation="inverse_duration", duration=1.9, tolerance=Tolerance(warn_bps=10, fail_bps=30),
    )  # fmt: skip
    cfg = config.model_copy(update={"groups": (g,)})
    put(store, config, closes("SHY", 100.0, 99.905) + yields("UST_10Y", "DGS10", 4.00, 4.05))
    r = ProxyVsExact().evaluate(make_ctx(cfg, store), duck_for(store, cfg))[0]
    assert r.right_instrument == "UST_10Y"
    assert r.status is DQStatus.PASS


def test_one_source_must_have_both_days(store: LocalBackend, config: Config) -> None:
    # previous day only from FRED, reference day only from Yahoo: never mixed
    put(
        store,
        config,
        [
            *closes("IEF", 100.0, 99.65),
            fred("UST_10Y", "DGS10", 4.00, as_of=THU),
            *history("UST_10Y", "^TNX", 4.05, 4.05, "pct")[1:],
        ],
    )
    r = run(store, config)["ust_10y"]
    assert r.status is DQStatus.SKIP
    assert "missing side" in r.details["reason"]


# --- skips, scope, registration -----------------------------------------------------------------


def test_missing_sides_are_skip_with_reason(store: LocalBackend, config: Config) -> None:
    put(store, config, closes("SPY", 100.0, 101.0))  # no ES_F
    r = run(store, config)["sp500"]
    assert r.status is DQStatus.SKIP
    assert r.diff_bps is None
    assert "ES_F" in r.details["reason"]
    assert r.details["left"]["value"] == 101.0
    put(store, config, history("ES_F", "ES=F", 5000.0, 5050.0, "index_pts")[1:])  # one day only
    assert run(store, config)["sp500"].status is DQStatus.SKIP


def test_zero_previous_value_is_skip(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        closes("SPY", 0.0, 101.0) + history("ES_F", "ES=F", 5000.0, 5050.0, "index_pts"),
    )
    r = run(store, config)["sp500"]
    assert r.status is DQStatus.SKIP
    assert "zero" in r.details["reason"]


def test_only_return_and_inverse_duration_groups_with_a_proxy(
    store: LocalBackend, config: Config
) -> None:
    out = run(store, config)
    assert set(out) == {
        "sp500",
        "oil_wti",
        "oil_brent",
        "gold",
        "ust_2y",
        "ust_10y",
        "ust_long",
    }  # same/level groups (nasdaq, eurusd, ...) belong to other checks
    assert {r.status for r in out.values()} == {DQStatus.SKIP}


def test_current_unfinished_date_is_excluded(store: LocalBackend, config: Config) -> None:
    mon = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)  # Monday = today at run time
    put(
        store,
        config,
        closes("SPY", 100.0, 101.0)
        + history("ES_F", "ES=F", 5000.0, 5050.0, "index_pts")
        + [stooq_close("SPY", 300.0, as_of=mon)],
    )
    r = run(store, config)["sp500"]
    assert r.details["ref_date"] == "2026-11-13"
    assert r.status is DQStatus.PASS


def test_registered_and_dq_rows_mirror(store: LocalBackend, config: Config) -> None:
    assert isinstance(registered()["proxy_vs_exact"], ProxyVsExact)
    ctx = make_ctx(config, store)
    xdf, ddf = run_crosschecks(ctx, duck_for(store, config), only=["proxy_vs_exact"])
    assert len(xdf) == len(ddf) == 7
    assert set(ddf["check_type"]) == {"consistency"}


# --- T48 (P-019): the proxy ETF leg from Yahoo history; provider label ------------------------


def yahoo_closes(inst: str, prev: float, cur: float) -> list[dict[str, Any]]:
    return history(inst, inst, prev, cur, "USD")


def test_proxy_leg_from_yahoo_history_compares(store: LocalBackend, config: Config) -> None:
    put(
        store,
        config,
        yahoo_closes("SPY", 100.0, 101.0) + history("ES_F", "ES=F", 5000.0, 5050.0, "index_pts"),
    )
    r = run(store, config)["sp500"]
    assert r.status is DQStatus.PASS
    assert r.diff_bps == pytest.approx(0.0, abs=1e-6)
    assert (r.left_source, r.right_source) == ("yahoo", "yahoo")
    assert (r.details["left"]["dataset"], r.details["right"]["dataset"]) == ("history", "history")
    assert r.details["provider"] == "same provider"


def test_return_group_with_stooq_proxy_says_different_providers(
    store: LocalBackend, config: Config
) -> None:
    assert sp500(store, config, 101.0, 5050.0).details["provider"] == "different providers"


def test_inverse_duration_with_yahoo_proxy_and_fred_yield(
    store: LocalBackend, config: Config
) -> None:
    put(store, config, yahoo_closes("TLT", 100.0, 98.35) + yields("UST_20Y", "DGS20", 4.0, 4.1))
    r = run(store, config)["ust_long"]
    assert r.status is DQStatus.PASS
    assert (r.left_source, r.right_source) == ("yahoo", "fred")
    assert r.details["provider"] == "different providers"


def test_provider_label_is_absent_when_a_leg_is_missing(
    store: LocalBackend, config: Config
) -> None:
    put(store, config, yahoo_closes("SPY", 100.0, 101.0))
    r = run(store, config)["sp500"]
    assert r.status is DQStatus.SKIP
    assert "provider" not in r.details


def test_same_provider_never_changes_the_status(store: LocalBackend, config: Config) -> None:
    # opposite directions above 25 bp still fail with both legs on Yahoo
    put(
        store,
        config,
        yahoo_closes("GLD", 100.0, 101.0) + history("GOLD_F", "GC=F", 2000.0, 1980.0, "USD"),
    )
    r = run(store, config)["gold"]
    assert r.status is DQStatus.FAIL
    assert r.details["direction_disagreement"] is True
    assert r.details["provider"] == "same provider"


def test_every_proxy_etf_has_a_yahoo_history_feed(config: Config) -> None:
    hist = {i.id for i, _ in config.feeds_for("yahoo", "history")}
    paired = ("return", "inverse_duration")
    proxies = {g.proxy for g in config.groups if g.proxy and g.relation in paired}
    assert proxies <= hist
