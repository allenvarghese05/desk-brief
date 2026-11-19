"""Macro page (T54): each view's presence, provenance on every figure, figure-free charts,
hosted-mode filtering on the same store, and the plain short-history message.

SYNTHETIC FIXTURE: ``brief_synth`` (Monday 16 Nov 2026, 07:00 ET) plus ``macro_synth`` (seeded
random walks ending Friday 13 Nov, weekly CFTC rows as of Tuesdays). No R2, no network.
"""
# ruff: noqa: RUF001  (true minus sign in expected output)

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # macro_synth sits next to this file
from macro_synth import business_days, macro_rows, tier1_spy

from brief_synth import FRI, MON, MON_0700, World, daily, monday_rows

REPO = Path(__file__).resolve().parents[3]
PAGE = REPO / "app" / "pages" / "2_Macro.py"
_ENV = ("DESKBRIEF_DISPLAY_MODE", "DESKBRIEF_STORAGE_BACKEND", "R2_ACCOUNT_ID",
        "R2_READER_ACCESS_KEY_ID", "R2_READER_SECRET_ACCESS_KEY")  # fmt: skip
KICKERS = (
    "VIX term structure",
    "Treasury curve · change by tenor",
    "2s10s · 10-year minus 2-year yield",
    "Gold vs 10-year real yield",
    "Correlation · 90 days of daily changes",
    "CFTC positioning",
)
FIG = re.compile(r'<span class="db-fig">.*?</span></span>', flags=re.S)
RunMacro = Callable[..., AppTest]


@pytest.fixture
def run_macro(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunMacro:
    """``run_macro(mode, rows=...)`` runs the Macro page against a fresh synthetic store."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    st.cache_data.clear()
    n = iter(range(100))

    def run(
        mode: str | None = "hosted",
        rows: list[dict[str, Any]] | None = None,
        *,
        base: bool = True,
    ) -> AppTest:
        """``base=False`` leaves out ``brief_synth.monday_rows`` (fixed-value tests)."""
        w = World(tmp_path / f"w{next(n)}")
        w.observations(
            (monday_rows() if base else []) + (macro_rows(120, 12) if rows is None else rows)
        )
        w.dq(MON, [("fresh_a", "pass", None, None)])
        w.run(MON)
        text = (REPO / "config.yaml").read_text(encoding="utf-8")
        cfg = w.store.root.parent / "config.yaml"
        cfg.write_text(text.replace("local_root: ./.data", f"local_root: {w.store.root}"))
        monkeypatch.setenv("DESKBRIEF_CONFIG", str(cfg))
        if mode is not None:
            monkeypatch.setenv("DESKBRIEF_DISPLAY_MODE", mode)
        from app.components import data

        monkeypatch.setattr(data, "current_time", lambda: MON_0700)
        return AppTest.from_file(str(PAGE), default_timeout=120).run()

    return run


def page(at: AppTest) -> str:
    """Rendered markdown (the stylesheet left out) and captions."""
    parts = [m.value for m in at.markdown if "<style>" not in m.value]
    return "\n".join(parts + [c.value for c in at.caption])


def charts(at: AppTest) -> int:
    return len(at.get("vega_lite_chart"))


def plain(text: str) -> str:
    """The page with every provenance cell, inline SVG and style attribute removed."""
    text = FIG.sub("", text)
    text = re.sub(r"<svg.*?</svg>", "", text, flags=re.S)
    return re.sub(r'style="[^"]*"', "", text)


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


def test_every_view_is_present(run_macro: RunMacro) -> None:
    at = run_macro("hosted")
    assert not at.exception
    text = page(at)
    for kicker in KICKERS:
        assert kicker in text
    assert 'aria-current="page">Macro</a>' in text  # the nav marks this page
    assert re.search(r"Contango|Backwardation|Flat", text)  # the VIX shape in words
    assert "Over the month the curve" in text
    assert 'href="./?id=VIX3M"' in text  # names open the instrument on Markets
    assert 'href="./?id=UST_10Y"' in text
    assert charts(at) == 5  # 2s10s, gold vs real yield, three CFTC histories


def test_every_figure_goes_through_value_cell(run_macro: RunMacro) -> None:
    text = page(run_macro("local"))
    cells = FIG.findall(text)
    assert len(cells) >= 60
    for cell in cells:  # each names its source, tier, as-of and what it is
        assert re.search(r"[●◐○]", cell)
        for part in ("db-src", "db-asof", "db-field"):
            assert part in cell
    rest = plain(text)
    assert not re.search(r"\d\.\d", rest), re.findall(r".{40}\d\.\d.{20}", rest)[:3]
    assert not re.search(r"[+−]\d", rest), re.findall(r".{40}[+−]\d.{20}", rest)[:3]


def test_inline_charts_carry_no_figures(run_macro: RunMacro) -> None:
    svgs = re.findall(r"<svg.*?</svg>", page(run_macro("local")), flags=re.S)
    assert len(svgs) == 2  # VIX term structure and the Treasury curve
    labels = {t for s in svgs for t in re.findall(r"<text[^>]*>([^<]*)</text>", s)}
    assert labels <= {"9D", "30D", "3M", "6M", "2Y", "10Y", "20Y", "30Y"}


def test_chart_captions_state_sources_tiers_and_span(run_macro: RunMacro) -> None:
    caps = [c.value for c in run_macro("hosted").caption]
    assert len(caps) >= 8
    for cap in caps:
        if "withheld" in cap:
            continue
        assert re.search(r"[●◐○] (live|delayed|official)", cap)
        assert re.search(r"\d{1,2} \w{3} \d{4} to \d{1,2} \w{3} \d{4}\.$", cap)
    joined = "\n".join(caps)
    for source in ("cboe", "treasury", "fred", "yahoo", "cftc"):
        assert source in joined
    assert "drawn inverted" in joined  # the real yield axis is flipped and says so


def test_cftc_shows_weekly_date_and_week_on_week_contracts(run_macro: RunMacro) -> None:
    text = page(run_macro("hosted"))
    assert "Positions as of Tue 10 Nov 2026" in text
    wow = [c for c in FIG.findall(text) if "+12,000 contracts" in c]
    assert len(wow) == 3  # gold, ES and ZN, each from the report's own change text
    for cell in wow:
        assert "cftc" in cell
        assert "net speculative position" in cell
        assert "previous weekly value" in cell
    assert "Open interest" in text


def test_curve_change_has_1y_with_long_history(run_macro: RunMacro) -> None:
    text = page(run_macro("hosted", rows=macro_rows(280, 30)))
    assert "needs 1Y" not in text
    assert re.search(r"yield of \d{2} Nov 2025", text)  # the 1Y comparison's basis


# ---------------------------------------------------------------------------
# hosted mode on the same store
# ---------------------------------------------------------------------------


def _tier1_only_spy() -> list[dict[str, Any]]:
    return [r for r in macro_rows(120, 12) if r["instrument_id"] != "SPY"] + tier1_spy(120)


def test_local_mode_shows_tier1_spy_in_the_heatmap(run_macro: RunMacro) -> None:
    text = page(run_macro("local", rows=_tier1_only_spy()))
    assert "alpaca" in text
    assert 'title="live"' in text
    assert '<th class="num"><a href="./?id=SPY"' in text


@pytest.mark.parametrize("mode", ["hosted", None, "LIVE!"])
def test_hosted_and_unknown_modes_never_read_tier1(run_macro: RunMacro, mode: str | None) -> None:
    at = run_macro(mode, rows=_tier1_only_spy())
    assert not at.exception
    text = page(at)
    assert "alpaca" not in text
    assert 'title="live"' not in text
    spy = re.search(r'<tr><th><a href="./\?id=SPY".*?</tr>', text, flags=re.S)
    assert spy is not None  # SPY's one tier-3 close (brief_synth) keeps its row ...
    assert "db-fig" not in spy.group(0)  # ... but one day gives no correlation to print


def test_build_series_hosted_vs_local_on_the_same_store(tmp_path: Path) -> None:
    from app.components import data

    w = World(tmp_path / "w")
    w.observations(_tier1_only_spy())
    conn = w.connect()
    try:
        local = data.build_series(conn, MON_0700, ("SPY", "VIX", "NOPE"), frozenset())
        hosted = data.build_series(conn, MON_0700, ("SPY", "VIX", "NOPE"), frozenset({1}))
    finally:
        conn.close()
    assert set(local["SPY"]["tier"]) == {1}
    assert hosted["SPY"].empty
    assert not hosted["VIX"].empty
    assert local["NOPE"].empty
    assert hosted["NOPE"].empty


# ---------------------------------------------------------------------------
# short history (before the owner's two-year load)
# ---------------------------------------------------------------------------


def test_short_history_says_so_and_draws_no_lines(run_macro: RunMacro) -> None:
    at = run_macro("hosted", rows=macro_rows(5, 2))
    assert not at.exception
    text = page(at)
    assert charts(at) == 0
    assert "2s10s history: the store holds 5 days" in text
    assert "A line needs at least 20 days, so none is drawn" in text
    assert "the store holds 2 weekly reports" in text
    assert "A heatmap needs at least" in text
    assert "needs 1W" in text
    assert "needs 1M" in text
    assert "two-year history load" in text
    for kicker in KICKERS:  # every view is still there, saying what it lacks
        assert kicker in text


def test_empty_store_does_not_raise(run_macro: RunMacro) -> None:
    at = run_macro("hosted", rows=[])
    assert not at.exception
    text = page(at)
    assert "Not enough Cboe volatility indices" in text
    assert "No CFTC report stored yet" in text


# ---------------------------------------------------------------------------
# the pieces
# ---------------------------------------------------------------------------


def _bars(values: list[float], start: date = date(2026, 9, 1), **kw: Any) -> pd.DataFrame:
    days = [start + timedelta(days=i) for i in range(len(values))]
    return pd.DataFrame(
        {
            "d": pd.to_datetime(days),
            "close": values,
            "tier": kw.get("tier", 3),
            "source": kw.get("source", "cboe"),
            "field": kw.get("field", "close"),
            "unit": kw.get("unit", "index_pts"),
        }
    )


def test_vix_shape_in_words() -> None:
    from app.components.macro import vix_shape

    calm = vix_shape({"VIX9D": 12.0, "VIX": 14.0, "VIX3M": 16.0, "VIX6M": 17.0})
    assert calm.startswith("Contango")
    assert "all four tenors" in calm
    stress = vix_shape({"VIX9D": 35.0, "VIX": 30.0, "VIX3M": 25.0})
    assert stress.startswith("Backwardation")
    assert "front end is inverted" in stress
    assert "needs both" in vix_shape({"VIX": 14.0})
    assert not re.search(r"\d\.\d", calm + stress)  # words only; figures are in the table


def test_curve_words() -> None:
    from app.components.macro import curve_words

    assert curve_words({"UST_2Y": -20.0, "UST_10Y": -5.0}) == (
        "Over the month the curve bull-steepened (yields fell)."
    )
    assert curve_words({"UST_2Y": 15.0, "UST_10Y": 5.0}).endswith("bear-flattened (yields rose).")
    assert curve_words({"UST_10Y": 5.0}) == ""


def test_point_never_reaches_far_back() -> None:
    from app.components.macro import point

    bars = _bars([10.0, 11.0, 12.0], start=date(2026, 9, 1))
    assert point(bars) is not None
    assert point(bars).value == 12.0
    assert point(bars, date(2026, 8, 31)) is None  # before the first stored day
    assert point(bars, date(2026, 9, 2)).value == 11.0  # type: ignore[union-attr]
    assert point(bars, date(2026, 9, 20)) is None  # nearest bar is 17 days older


def test_correlation_sign_and_minimum() -> None:
    from app.components.macro import MIN_CORR, correlation

    up = [100 + i + (i % 3) for i in range(40)]
    a, b = _bars(up), _bars([2.0 - v / 1000 for v in up], unit="pct")
    corr = correlation(a, b, date(2026, 9, 1))
    assert corr is not None
    assert corr[0] < -0.99
    assert corr[1] == 39
    short = _bars(up[:MIN_CORR])  # MIN_CORR closes give MIN_CORR - 1 changes
    assert correlation(short, short, date(2026, 9, 1)) is None
    assert correlation(_bars([5.0]), a, date(2026, 9, 1)) is None  # one day: no change at all


def test_spread_2s10s_in_basis_points_on_common_dates() -> None:
    from app.components.macro import spread_2s10s

    ten = _bars([4.30, 4.40, 4.50], unit="pct", source="treasury", field="yield")
    two = _bars([4.10, 4.60], unit="pct", source="fred", field="yield", tier=3)
    s = spread_2s10s({"UST_10Y": ten, "UST_2Y": two})
    assert list(s["close"].round(6)) == [20.0, -20.0]
    assert set(s["source"]) == {"treasury + fred"}
    assert spread_2s10s({"UST_10Y": ten}).empty


def test_signed_uses_a_true_minus() -> None:
    from app.components.macro import signed

    assert signed(-0.404, 2) == "−0.40"
    assert signed(12000, 0, " contracts") == "+12,000 contracts"
    assert signed(0.0001, 2) == "0.00"


def test_macro_rows_cover_the_page() -> None:
    from app.components.macro import ALL_IDS

    stored = {r["instrument_id"] for r in macro_rows(3, 1)}
    assert set(ALL_IDS) - stored == {"GLD"}  # gold comes from futures in the fixture
    assert len(business_days(3)) == 3


# ---------------------------------------------------------------------------
# review round 1: tooltip provenance, aligned changes, two-year window, fixed values
# ---------------------------------------------------------------------------

_PROV_TITLES = {"As of", "Field", "Tier", "Source"}


def _tooltips(node: Any) -> list[list[dict[str, Any]]]:
    """Every tooltip list anywhere in a Vega-Lite spec."""
    found: list[list[dict[str, Any]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "tooltip" and isinstance(value, list):
                found.append(value)
            else:
                found += _tooltips(value)
    elif isinstance(node, list):
        for item in node:
            found += _tooltips(item)
    return found


def _assert_tooltips_carry_provenance(spec: dict[str, Any]) -> int:
    """Each tooltip that shows a number also names its as-of date, field, tier and source."""
    shown = 0
    for tips in _tooltips(spec):
        if not any(t.get("type") == "quantitative" for t in tips):
            continue
        shown += 1
        titles = {t.get("title") for t in tips}
        assert titles >= _PROV_TITLES, titles
    return shown


def test_every_page_tooltip_with_a_value_carries_provenance(run_macro: RunMacro) -> None:
    at = run_macro("local")
    specs = [json.loads(el.proto.spec) for el in at.get("vega_lite_chart")]
    assert len(specs) == 5
    assert sum(_assert_tooltips_carry_provenance(s) for s in specs) >= 6


def test_home_chart_tooltips_carry_provenance() -> None:
    """T36's price, volume and compare charts follow the same rule."""
    from app.components import chart

    days = pd.to_datetime([date(2026, 11, 2) + timedelta(days=i) for i in range(5)])
    bars = pd.DataFrame(
        {
            "d": days,
            "open": [10.0, 11, 12, 13, 14],
            "high": [11.0, 12, 13, 14, 15],
            "low": [9.0, 10, 11, 12, 13],
            "close": [10.5, 11.5, 12.5, 13.5, 14.5],
            "volume": [100.0, 200, 300, 400, 500],
            "tier": 2,
            "source": "yahoo",
            "field": "close",
            "unit": "USD",
            "interval": "1d",
        }
    )
    for style in (chart.CANDLES, chart.LINE):
        spec = chart.price_chart(bars, style=style, last=14.6).to_dict()
        assert _assert_tooltips_carry_provenance(spec) == 2  # price and volume
    long = chart.rebased(bars, bars.assign(source="stooq", tier=3), "A", "B")
    assert {"field_text", "tier_text", "source"} <= set(long.columns)
    assert _assert_tooltips_carry_provenance(chart.compare_chart(long, "A", "B").to_dict()) == 1
    assert "rebased to 100 on 02 Nov 2026" in long["field_text"].iloc[0]


def test_correlation_joins_levels_before_differencing() -> None:
    """A day missing from one series must not pair a two-day change with a one-day one."""
    from app.components.macro import correlation

    n = 45
    a_vals = [100.0 + (5 if i % 2 else 0) + i * 0.01 for i in range(n)]
    a = _bars(a_vals)
    # b tracks a exactly, but every third day is missing from b
    b = _bars(a_vals).iloc[[i for i in range(n) if i % 3 != 1]].reset_index(drop=True)
    corr = correlation(a, b, date(2026, 9, 1))
    assert corr is not None
    assert corr[0] == pytest.approx(1.0)  # differencing first gives about 0.70 here
    assert corr[1] == len(b) - 1


def test_page_reads_the_two_year_history(run_macro: RunMacro) -> None:
    from datetime import datetime

    at = run_macro("hosted", rows=macro_rows(520, 100))
    cap = next(c.value for c in at.caption if c.value.startswith("Daily 2s10s spread"))
    start = re.search(r"(\d{1,2} \w{3} \d{4}) to \d{1,2} \w{3} \d{4}\.$", cap)
    assert start is not None, cap
    first = datetime.strptime(start.group(1), "%d %b %Y").date()
    assert (FRI - first).days > 700  # beyond BAR_DAYS (400): the T53 load shows


def _fixed_rows() -> list[dict[str, Any]]:
    """Known values: 10Y rises 1bp a business day, 2Y is flat, gold steps from 2,000 to 2,100
    after 14 Oct, QQQ is SPY doubled and HY OAS moves against SPY."""
    tsy = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
    rows: list[dict[str, Any]] = []
    for i, d in enumerate(business_days(60)):
        zig = 5.0 if i % 2 else 0.0
        spy = 500.0 + zig + i * 0.5
        rows += [
            daily("UST_10Y", d, round(4.00 + 0.01 * i, 4), **tsy),
            daily("UST_2Y", d, 4.00, **tsy),
            daily(
                "GOLD_F",
                d,
                2000.0 if d <= date(2026, 10, 14) else 2100.0,
                tier=2,
                source="yahoo",
                dataset="history",
            ),
            daily("SPY", d, spy),
            daily("QQQ", d, spy * 2),
            daily(
                "HY_OAS",
                d,
                round(3.00 - zig / 100, 4),
                source="fred",
                dataset="series",
                field="rate",
                unit="pct",
            ),
        ]
    return rows


def test_fixed_values_on_the_page(run_macro: RunMacro) -> None:
    text = page(run_macro("hosted", rows=_fixed_rows(), base=False))
    row10 = re.search(r'<tr><td><a href="./\?id=UST_10Y".*?</tr>', text, flags=re.S)
    row2 = re.search(r'<tr><td><a href="./\?id=UST_2Y".*?</tr>', text, flags=re.S)
    assert row10 is not None
    assert row2 is not None
    assert '<span class="db-val db-up">+5bp</span>' in row10.group(0)  # vs 1W: 5 days x 1bp
    assert '<span class="db-val db-up">+22bp</span>' in row10.group(0)  # vs 1M: 22 days
    assert '<span class="db-val db-flat">0bp</span>' in row2.group(0)
    gold = next(c for c in FIG.findall(text) if "since the close of 14 Oct 2026" in c)
    assert '<span class="db-val db-up">+5.0%</span>' in gold
    cells = FIG.findall(text)
    spy_qqq = next(c for c in cells if "SPY vs QQQ" in c)
    assert '<span class="db-val">+1.00</span>' in spy_qqq
    spy_hy = next(c for c in cells if "SPY vs HY OAS" in c)
    assert re.search(r'<span class="db-val">−0\.9\d|<span class="db-val">−1\.00', spy_hy)
