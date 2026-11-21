"""Macro page (T54): each view's presence, provenance on every figure, figure-free charts,
hosted-mode filtering on the same store, and the plain short-history message.

SYNTHETIC FIXTURE: ``brief_synth`` (Monday 16 Nov 2026, 07:00 ET) plus ``macro_synth`` (seeded
random walks ending Friday 13 Nov, weekly CFTC rows as of Tuesdays). No R2, no network.
"""
# ruff: noqa: RUF001  (true minus sign in expected output)

from __future__ import annotations

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
    """TradingView charts drawn: each ``tvchart.chart`` ends with its attribution line."""
    from app.components import tvchart

    return page(at).count(tvchart.attribution_html())


def row(at: AppTest, name: str) -> str:
    """The markup of one table row (T59: a grid row under its in-session button)."""
    for m in at.markdown:
        if m.value.startswith('<div class="db-pickbody">') and (
            f'<span class="db-mx-name">{name}</span>' in m.value
        ):
            return str(m.value)
    raise AssertionError(f"no row named {name!r}")


def mx(where: str, iid: str) -> str:
    """The key of a Macro row's in-session button (built, so it never reads like a secret)."""
    return "-".join(("mx", where, iid))


def tvchart_href() -> str:
    from app.components import tvchart

    return f'href="{tvchart.ATTRIBUTION_URL}"'


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
    assert '<span class="db-brand">Macro</span>' in text  # T57: the router's nav marks the page
    assert re.search(r"Contango|Backwardation|Flat", text)  # the VIX shape in words
    assert "Over the month the curve" in text
    # T59: names open the instrument on Markets through in-session buttons, never a link
    assert at.button(key=mx("vix", "VIX3M")).label == "Open 3M · VIX3M on Markets"
    assert at.button(key=mx("curve", "UST_10Y")).label == "Open 10Y on Markets"
    assert at.button(key=mx("heat", "UST_10Y"))
    assert "href=" not in text.replace(tvchart_href(), "")
    assert charts(at) == 6  # 2s10s, gold, real yield, three CFTC histories


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
    assert "TIPS real yield (bottom, percent)" in joined  # two charts, each on its own scale


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
    at = run_macro("local", rows=_tier1_only_spy())
    text = page(at)
    assert "alpaca" in text
    assert 'data-tier="1"' in text
    assert 'data-tier="1"' in row(at, "<b>SPY</b>")
    assert at.button(key=mx("heat", "SPY"))


@pytest.mark.parametrize("mode", ["hosted", None, "LIVE!"])
def test_hosted_and_unknown_modes_never_read_tier1(run_macro: RunMacro, mode: str | None) -> None:
    at = run_macro(mode, rows=_tier1_only_spy())
    assert not at.exception
    text = page(at)
    assert "alpaca" not in text
    assert 'data-tier="1"' not in text
    spy = row(at, "<b>SPY</b>")  # SPY's one tier-3 close (brief_synth) keeps its row ...
    assert "db-fig" not in spy  # ... but one day gives no correlation to print


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
# review round 1: chart provenance, aligned changes, two-year window, fixed values
# ---------------------------------------------------------------------------


def test_every_chart_point_carries_provenance(
    run_macro: RunMacro, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T59: every TradingView series names field, tier and source for each point (its
    tooltip shows them with the as-of date), and hosted mode draws no tier-1 point."""
    from app.components import tvchart

    real = tvchart.chart
    drawn: list[list[Any]] = []

    def spy(series: Any, **kw: Any) -> bool:
        drawn.append(list(series))
        return real(series, **kw)

    monkeypatch.setattr(tvchart, "chart", spy)
    at = run_macro("local")
    assert not at.exception
    assert len(drawn) == 6
    for series in drawn:
        for s in series:
            assert s["points"]
            assert all(0 <= p["m"] < len(s["meta"]) for p in s["points"])
            for field, tier, source in s["meta"]:
                assert field
                assert source
                assert re.match(r"[●◐○] (live|delayed|official)$", tier)


def test_tv_line_names_derived_fields() -> None:
    from app.components import macro

    spread = macro.spread_2s10s(
        {"UST_10Y": _bars([4.3, 4.4], source="treasury", field="yield", unit="pct"),
         "UST_2Y": _bars([4.0, 4.1], source="treasury", field="yield", unit="pct")}
    )  # fmt: skip
    s = macro.tv_line(spread, "2s10s", field="10Y minus 2Y yield spread, bp")
    assert s["meta"] == [["10Y minus 2Y yield spread, bp", "○ official", "treasury"]]
    assert [p["value"] for p in s["points"]] == pytest.approx([30.0, 30.0])


def test_chart_range_windows_every_series() -> None:
    from app.components import macro

    bars = _bars([1.0] * 400)
    assert len(macro.window(bars, "3M")) == 92
    assert len(macro.window(bars, "All")) == 400
    assert macro.DEFAULT_RANGE in macro.RANGES


def test_rebased_compare_rows_keep_provenance_columns() -> None:
    """The compare payload of Markets (T36 rule, now TradingView) keeps its provenance."""
    from app.components import chart

    days = pd.to_datetime([date(2026, 11, 2) + timedelta(days=i) for i in range(5)])
    bars = pd.DataFrame(
        {"d": days, "close": [10.5, 11.5, 12.5, 13.5, 14.5], "tier": 2, "source": "yahoo",
         "field": "close"}
    )  # fmt: skip
    long = chart.rebased(bars, bars.assign(source="stooq", tier=3), "A", "B")
    assert {"field_text", "tier_text", "source"} <= set(long.columns)
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
    at = run_macro("hosted", rows=_fixed_rows(), base=False)
    text = page(at)
    row10, row2 = row(at, "10Y"), row(at, "2Y")
    assert '<span class="db-val db-up">+5bp</span>' in row10  # vs 1W: 5 days x 1bp
    assert '<span class="db-val db-up">+22bp</span>' in row10  # vs 1M: 22 days
    assert '<span class="db-val db-flat">0bp</span>' in row2
    gold = next(c for c in FIG.findall(text) if "since the close of 14 Oct 2026" in c)
    assert '<span class="db-val db-up">+5.0%</span>' in gold
    cells = FIG.findall(text)
    spy_qqq = next(c for c in cells if "SPY vs QQQ" in c)
    assert '<span class="db-val">+1.00</span>' in spy_qqq
    spy_hy = next(c for c in cells if "SPY vs HY OAS" in c)
    assert re.search(r'<span class="db-val">−0\.9\d|<span class="db-val">−1\.00', spy_hy)


# ---------------------------------------------------------------------------
# T59: in-session navigation, caveat words once, no full-reload links
# ---------------------------------------------------------------------------


def _qp(at: AppTest, key: str) -> str:
    value = at.query_params[key]
    return value[0] if isinstance(value, list) else value


def _chart_name(at: AppTest) -> str:
    m = re.search(r'<span class="db-ch-name">([^<]*)</span>', page(at))
    assert m is not None
    return m.group(1)


def test_a_name_opens_the_instrument_on_markets_in_session(
    run_home: Callable[..., AppTest],
) -> None:
    """Carry-over 1: no ``<a target="_self">``; a click selects the instrument on Markets in
    the same session, and Markets' kept selection brings it back after another page."""
    at = run_home("hosted", rows=monday_rows() + macro_rows(60, 12))
    at.session_state["probe"] = "still here"  # gone if the click started a new session
    at.switch_page("pages/2_Macro.py").run()
    assert not at.exception
    assert 'target="_self"' not in page(at)
    at.button(key=mx("curve", "UST_2Y")).click().run()
    assert not at.exception
    assert _qp(at, "id") == "UST_2Y"
    assert _chart_name(at) == "2Y"
    assert at.session_state["probe"] == "still here"
    assert at.session_state["db-keep-id"] == "UST_2Y"
    at.switch_page("pages/2_Macro.py").run()  # Markets, then Macro, then Markets again
    at.switch_page("Home.py").run()  # AppTest names the default page by the router
    assert not at.exception
    assert _chart_name(at) == "2Y"


def test_macro_uses_markets_keep_key() -> None:
    from app.components import macro

    src = (REPO / "app" / "views" / "markets.py").read_text(encoding="utf-8")
    assert f'KEEP_ID, KEEP_RNG, KEEP_STYLE = "{macro.MARKETS_KEEP_ID}"' in src
    macro_src = (REPO / "app" / "pages" / "2_Macro.py").read_text(encoding="utf-8")
    assert "st.switch_page(Home.pages()[0]" in macro_src  # the router's Markets page object


def test_chart_range_is_kept_across_a_visit(run_home: Callable[..., AppTest]) -> None:
    at = run_home("hosted", rows=monday_rows() + macro_rows(60, 12))
    at.switch_page("pages/2_Macro.py").run()
    at.segmented_control(key="mx-rng").set_value("3M").run()
    at.switch_page("Home.py").run()  # AppTest names the default page by the router
    at.switch_page("pages/2_Macro.py").run()
    assert not at.exception
    assert at.segmented_control(key="mx-rng").value == "3M"


def _cot_row(iid: str, flags: tuple[str, ...]) -> Any:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    now = datetime(2026, 11, 10, 20, tzinfo=UTC)
    quals = tuple(f"{f}: synthetic" for f in flags if f != "proxy")
    return SimpleNamespace(
        instrument_id=iid, tier=3, as_of_text="10 Nov", available=True,
        is_proxy="proxy" in flags, qualifiers=quals, value_text="240,000 contracts",
        change_text="+12,000 contracts", basis="previous weekly value",
        change=SimpleNamespace(last=SimpleNamespace(source="cftc", as_of=now, field="close",
                                                    dataset="cot")),
    )  # fmt: skip


def _visible(html: str) -> str:
    html = re.sub(r'<span class="db-sr[^"]*"[^>]*>.*?</span>', "", html, flags=re.S)
    html = re.sub(r'<span class="db-tip"[^>]*>.*?</span></span>', "</span>", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def test_cftc_strip_shows_each_caveat_word_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Carry-over 2: the strip printed its caveat words beside the value and the change. Each
    word is now visible once, and the later figures still name it in their accessible label."""
    from datetime import UTC, datetime

    from app.components import macro, panels

    monkeypatch.setattr(panels, "field_label", lambda last: "net speculative position")
    net = _cot_row("COT_GC_NET", ("fallback",))
    oi = _cot_row("COT_GC_OI", ("fallback",))
    html = macro.cot_card(net, oi, "Gold (COMEX)", datetime(2026, 11, 16, 12, tzinfo=UTC))
    shown = _visible(html)
    assert len(re.findall(r"\bfallback\b", shown)) == 1
    labels = re.findall(r'class="db-prov[^"]*"[^>]*aria-label="([^"]*)"', html)
    assert len(labels) == 3  # value, change and open interest each keep their glyph
    assert all("fallback" in label for label in labels)
