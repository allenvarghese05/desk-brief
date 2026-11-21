"""Dashboard workspace (T34, T36): AppTest smoke, hosted-mode licence rule, no bare numbers,
selection by ``?id=``, the folded-in Instrument details and the price chart's data rules.

SYNTHETIC FIXTURE: data comes from ``brief_synth`` (Monday 16 Nov 2026, 07:00 ET); SPY is the
only tier-1 instrument in it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from brief_synth import FRI, daily, monday_rows

HOME = Path(__file__).resolve().parents[3] / "app" / "Home.py"
Run = Callable[..., AppTest]


def page(at: AppTest, *, limits: bool = True) -> str:
    """All rendered markdown and captions; ``limits=False`` drops the known-limits bullets."""
    parts = [m.value for m in at.markdown if limits or not m.value.startswith("- ")]
    return "\n".join(parts + [c.value for c in at.caption] + [e.label for e in at.expander])


ROW = r'<a class="db-wl-row db-row.*?</a>'


def nav_titles(at: AppTest) -> list[str]:
    """Titles of the pages the router registered with st.navigation (T57)."""
    return [p["page_name"] for p in at._registered_pages.values()]


def select(at: AppTest, iid: str) -> AppTest:
    """What a click on a watchlist row does: put ``?id=`` in the URL and run again."""
    at.query_params["id"] = iid
    return at.run()


def test_smoke_top_bar_and_overview(run_home: Run) -> None:
    at = run_home("hosted")
    assert not at.exception
    text = page(at)
    assert "Desk Brief" in text
    assert "Mon 16 Nov" in text
    assert "07:00 ET" in text
    assert "Healthy" in text
    assert "2/2 checks pass" in text
    assert "hosted" in text
    assert "delayed+official" in text
    for title in ("Rates", "FX", "Commodities", "Equities"):
        assert f'db-sec-title">{title}</div>' in text
    assert "Cross checks" in nav_titles(at)  # T57: the page list is st.navigation's top bar
    assert at.button(key="-".join(("pick", "health"))).label == (
        "Open Data health: 2/2 checks pass"  # T57: counts readable without hovering
    )
    assert at.text_input[0].label == "Search instruments"


def test_hosted_never_renders_tier1_rows(run_home: Run) -> None:
    at = run_home("hosted")
    text = page(at, limits=False)
    assert "alpaca" not in text
    assert 'data-tier="1"' not in text
    assert "600.00" not in text  # the tier-1 SPY tick value
    assert "withheld" not in " ".join(c.value for c in at.caption)  # SPY fell back, not hidden


def test_hosted_falls_back_to_tier3_when_latest_is_tier1(run_home: Run) -> None:
    """SPY's latest value is a tier-1 tick; hosted shows its tier-3 Friday close instead."""
    at = run_home("hosted")
    text = page(at, limits=False)
    assert "598.50" in text
    assert "600.00" not in text
    assert 'data-db-id="SPY"' in text


def test_hosted_shows_gap_for_instrument_with_only_tier1_data(run_home: Run) -> None:
    only_t1 = [r for r in monday_rows() if not (r["instrument_id"] == "SPY" and r["tier"] != 1)]
    at = run_home("hosted", rows=only_t1)
    assert "600.00" not in page(at, limits=False)
    assert "data unavailable" in page(at, limits=False)  # shown as a gap, never as the tick


def test_unset_or_unknown_mode_fails_closed_to_hosted(run_home: Run) -> None:
    for mode in (None, "", "LIVE!", "prod"):
        text = page(run_home(mode), limits=False)
        assert "600.00" not in text  # the tier-1 SPY tick value
        assert 'data-tier="1"' not in text


def test_local_mode_shows_tier1_with_glyph(run_home: Run) -> None:
    at = run_home("local")
    text = page(at)
    assert "600.00" in text
    assert "alpaca" in text
    assert 'data-tier="1"' in text
    assert "live+delayed+official" in text


def test_every_number_comes_from_the_provenance_component(run_home: Run) -> None:
    at = run_home("local")
    cells = re.findall(r'<span class="db-fig".*?</span></span>', page(at), flags=re.S)
    assert len(cells) >= 20
    for cell in cells:  # each carries source, tier glyph, as-of and field label
        assert re.search(r"[●◐○]", cell)
        assert "db-src" in cell
        assert "db-asof" in cell
        assert "db-field" in cell
    rest = re.sub(r'<span class="db-fig".*?</span></span>', "", page(at), flags=re.S)
    for number in ("4.31%", "4.37%", "5,976.00", "$71.20", "600.00", "1.0842"):
        assert number not in rest  # no value text outside the component


def test_p009_labels_next_to_tier_and_asof(run_home: Run) -> None:
    text = page(run_home("hosted"))
    rows = re.findall(ROW, text, flags=re.S)
    ten = next(c for c in rows if "4.37%" in c)  # 10Y tick (tier 2): labelled a yield, not a close
    assert 'db-field">yield<' in ten
    assert "delayed" in ten
    two = next(c for c in rows if "4.18%" in c)  # 2Y official
    assert 'db-field">yield<' in two
    assert "official" in two
    eur = next(c for c in rows if "1.0842" in c)
    assert "ECB reference rate" in eur
    assert "close" not in eur  # an ECB fix is never called a close


def test_empty_store_does_not_raise(run_home: Run) -> None:
    only = daily(
        "UST_2Y", FRI, 4.18, source="treasury", dataset="par_curve", field="yield", unit="pct"
    )
    at = run_home("hosted", rows=[only])
    assert not at.exception
    assert "data unavailable" in page(at)


def test_unreachable_r2_shows_error_not_traceback(
    run_home: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DESKBRIEF_STORAGE_BACKEND", "r2")
    at = run_home("hosted")
    assert not at.exception
    assert at.error
    assert "R2_READER_ACCESS_KEY_ID" in at.error[0].value
    assert "SECRET" not in at.error[0].value.replace("R2_READER_SECRET_ACCESS_KEY", "")


def test_unreadable_store_shows_error_without_message(
    run_home: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    import duckdb
    from app.components import data

    def boom(*_a: object, **_k: object) -> None:
        raise duckdb.IOException("secret-token-123 connection refused")

    run_home("hosted")  # builds the store and config
    st.cache_data.clear()
    monkeypatch.setattr(data, "build_snapshot", boom)
    at = AppTest.from_file(str(HOME), default_timeout=60).run()
    assert not at.exception
    assert at.error
    assert "Cannot read the data store" in at.error[0].value
    assert "secret-token-123" not in at.error[0].value


def test_oserror_shows_error_without_message(
    run_home: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.components import data

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("/private/path/leak")

    run_home("hosted")
    st.cache_data.clear()
    monkeypatch.setattr(data, "build_snapshot", boom)
    at = AppTest.from_file(str(HOME), default_timeout=60).run()
    assert at.error
    assert "Cannot read the data store" in at.error[0].value
    assert "/private/path/leak" not in at.error[0].value


def test_known_limits_list_dashboard_limits(run_home: Run) -> None:
    from app.components.data import DASHBOARD_LIMITS

    assert len(DASHBOARD_LIMITS) == 5
    assert not any("no yield-curve panel" in limit for limit in DASHBOARD_LIMITS)
    text = page(run_home("hosted"))
    for limit in DASHBOARD_LIMITS:
        assert f"- {limit}" in text


def test_details_list_lineage_of_the_selection(run_home: Run) -> None:
    text = page(select(run_home("local"), "UST_10Y"))
    assert "Lineage: source, fetch and raw file" in text
    assert "Parser" in text
    assert "synthetic-1" in text


# --- T36 redesign: compact tags, search, folded proxies, tiles -----------------------------


def test_overview_numbers_use_the_compact_tag(run_home: Run) -> None:
    """Glyph and as-of are on screen; field and source stay in the markup (hover, readers)."""
    text = page(run_home("hosted"))
    rows = re.findall(ROW, text, flags=re.S)
    ten = next(c for c in rows if "4.37%" in c)
    assert 'class="db-prov"' in ten  # T57: the compact glyph with its CSS tooltip
    assert re.search(r'<span class="db-tip"[^>]*><span class="db-field">yield</span>', ten)
    assert re.search(
        r' · ◐ delayed · <span class="db-asof">[^<]+</span> · <span class="db-src">', ten
    )
    assert ten.count('class="db-prov"') == 1  # the change does not repeat the tag


def test_headline_tiles_link_to_the_instrument_page(run_home: Run) -> None:
    at = run_home("hosted")
    text = page(at)
    assert '<a class="db-tile' in text
    assert 'data-db-id="UST_10Y"' in text
    assert at.button(
        key="-".join(("pick", "tile", "UST_10Y"))
    )  # T57: a click selects it in-session
    assert "Biggest moves" in text


def test_proxies_fold_behind_one_line(run_home: Run) -> None:
    at = run_home("hosted")  # T57: the fold is an st.expander, so its rows can be buttons
    fold = [e for e in at.expander if re.fullmatch(r"Show \d+ ETF prox(y|ies)", e.label)]
    assert fold
    folded = ["\n".join(m.value for m in e.markdown) for e in fold]
    assert any('data-db-id="SPY"' in f for f in folded)


def test_search_filters_sections_and_unfolds_proxies(run_home: Run) -> None:
    at = run_home("hosted")
    at.text_input[0].input("yield").run()
    text = page(at)
    assert not at.exception
    assert "matches for" in text
    assert "<details" not in text  # nothing folded while searching
    assert not [e for e in at.expander if e.label.startswith("Show ")]  # T57: fold is an expander
    rows = re.findall(ROW, text, flags=re.S)
    assert not any("1.0842" in r for r in rows)  # EURUSD does not match "yield"
    assert any("4.37%" in r for r in rows)


def test_search_with_no_match_says_so(run_home: Run) -> None:
    at = run_home("hosted")
    at.text_input[0].input("zzzz").run()
    assert 'No instrument matches "zzzz"' in page(at)


# --- T36 workspace: selection, details, chart -------------------------------------------------


def test_default_selection_is_the_s_and_p_future_and_is_highlighted(run_home: Run) -> None:
    at = run_home("hosted")
    assert at.query_params["id"] == ["ES_F"] or at.query_params["id"] == "ES_F"
    sel = re.findall(r'<a class="db-wl-row db-row is-sel".*?</a>', page(at), flags=re.S)
    assert len(sel) == 1
    assert "5,976.00" in sel[0]


def test_query_param_selects_an_instrument(run_home: Run) -> None:
    at = select(run_home("hosted"), "UST_10Y")
    text = page(at)
    assert not at.exception
    sel = re.findall(r'<a class="db-wl-row db-row is-sel".*?</a>', text, flags=re.S)
    assert len(sel) == 1
    assert "4.37%" in sel[0]
    assert '<span class="db-ch-name">10Y</span>' in text
    assert "Do the sources agree?" in text


def test_unknown_query_param_falls_back_to_the_default(run_home: Run) -> None:
    at = select(run_home("hosted"), "NOT_AN_ID")
    assert not at.exception
    assert '<span class="db-ch-name">S&amp;P fut</span>' in page(at)


def test_single_search_match_selects_it(run_home: Run) -> None:
    at = run_home("hosted")
    at.text_input[0].input("eurusd").run()
    text = page(at)
    assert "1 match for" in text
    assert '<span class="db-ch-name">EURUSD</span>' in text


def test_ladder_uses_p009_labels(run_home: Run) -> None:
    text = page(select(run_home("local"), "UST_10Y"))
    ladder = text.split('db-kicker">Do the sources agree?</div>', 1)[1].split("Cross-checks", 1)[0]
    assert 'db-field">yield<' in ladder
    assert 'db-field">close<' not in ladder  # a yield is not a close
    eur = page(select(run_home("local"), "EURUSD")).split(
        'db-kicker">Do the sources agree?</div>', 1
    )[1]
    assert "ECB reference rate" in eur


def test_hosted_details_never_show_tier1(run_home: Run) -> None:
    text = page(select(run_home("hosted"), "SPY"), limits=False)
    assert "alpaca" not in text
    assert 'data-tier="1"' not in text
    assert "600.00" not in text


def test_local_details_show_every_tier_and_checks(run_home: Run) -> None:
    text = page(select(run_home("local"), "SPY"))
    ladder = text.split('db-kicker">Do the sources agree?</div>', 1)[1].split("Cross-checks", 1)[0]
    assert "alpaca" in ladder
    assert "live" in ladder
    assert "Data checks, last 14 days" in text


def test_candles_need_a_consistent_open_high_low() -> None:
    import pandas as pd
    from app.components import chart

    days = pd.to_datetime(["2026-11-10", "2026-11-11", "2026-11-12"])
    base = {"d": days, "volume": [1.0, 1.0, 1.0], "tier": [2, 2, 2], "source": ["yahoo"] * 3,
            "field": ["close"] * 3, "unit": ["USD"] * 3}  # fmt: skip
    good = pd.DataFrame(
        {
            **base,
            "open": [1.0, 2.0, 3.0],
            "high": [2.5, 3.5, 4.5],
            "low": [0.5, 1.5, 2.5],
            "close": [2.0, 3.0, 4.0],
        }
    )
    assert chart.has_ohlc(good)
    assert chart.candle_ok(good).all()
    bad = good.assign(close=[2.0, 3.0, 9.0])  # a close above its own high is not a candle
    assert chart.candle_ok(bad).tolist() == [True, True, False]
    bare = good.assign(open=float("nan"), high=float("nan"), low=float("nan"))
    assert not chart.has_ohlc(bare)  # close only: a line, never a made-up candle
    spec = chart.price_chart(bare, style=chart.CANDLES, last=4.1).to_dict()
    assert "vconcat" in spec or "layer" in spec
    assert len(chart.window(good, "1M")) == 3


# --- T36 phase 1: tearsheet -------------------------------------------------------------------


def test_tearsheet_lists_every_value_we_hold_with_as_of_and_fetch_age(run_home: Run) -> None:
    text = page(select(run_home("local"), "UST_10Y"))
    assert "Every value we hold" in text
    table = text.split("Every value we hold", 1)[1].split("</table>", 1)[0]
    assert "treasury" in table  # the official yield
    assert "yahoo" in table  # and the delayed quote
    assert "fetched" in table
    for cell in re.findall(r'<span class="db-fig".*?</span></span>', table, flags=re.S):
        assert "db-src" in cell
        assert "db-field" in cell


def test_hosted_tearsheet_never_shows_tier1_values(run_home: Run) -> None:
    text = page(select(run_home("hosted"), "SPY"), limits=False)
    table = text.split("Every value we hold", 1)[1].split("</table>", 1)[0]
    assert "alpaca" not in table
    assert "600.00" not in table


def test_horizons_say_how_much_history_they_need(run_home: Run) -> None:
    text = page(select(run_home("hosted"), "UST_10Y"))
    strip = text.split('aria-label="performance over horizons"', 1)[1].split("</div></div>", 1)[0]
    for label in ("1D", "1W", "1M", "3M", "YTD", "1Y"):
        assert f">{label}<" in strip
    assert "needs" in strip  # the synthetic store holds days, not a year


def test_risk_panel_needs_twenty_days_before_volatility(run_home: Run) -> None:
    text = page(select(run_home("hosted"), "UST_10Y"))
    risk = text.split("Risk and range", 1)[1]
    assert "Realised vol 20d" in risk
    assert "needs 21d" in risk


def test_rebased_comparison_starts_both_series_at_100() -> None:
    import pandas as pd
    from app.components import chart

    days = pd.to_datetime(["2026-11-10", "2026-11-11", "2026-11-12"])
    meta = {"tier": 3, "source": "stooq", "field": "close"}  # bar columns (provenance)
    a = pd.DataFrame({"d": days, "close": [50.0, 55.0, 60.0], **meta})
    b = pd.DataFrame({"d": days[1:], "close": [200.0, 180.0], **meta})
    long = chart.rebased(a, b, "A", "B")
    first = long[long["d"] == days[1]].set_index("series")["index"]
    assert first["A"] == 100.0
    assert first["B"] == 100.0
    assert long[(long["series"] == "B") & (long["d"] == days[2])]["index"].iloc[0] == 90.0
    assert chart.rebased(a, b.head(0), "A", "B").empty


def test_corrections_show_both_versions(run_home: Run) -> None:
    from brief_synth import FRI, MON_0700

    old = daily("UST_2Y", FRI, 4.10, source="treasury", dataset="par_curve", field="yield",
                unit="pct", ingested_at=MON_0700 - timedelta(hours=30))  # fmt: skip
    new = daily("UST_2Y", FRI, 4.18, source="treasury", dataset="par_curve", field="yield",
                unit="pct", ingested_at=MON_0700 - timedelta(hours=2))  # fmt: skip
    rows = [
        r
        for r in monday_rows()
        if not (r["instrument_id"] == "UST_2Y" and r["as_of"] == old["as_of"])
    ]
    at = select(run_home("hosted", rows=[*rows, old, new]), "UST_2Y")
    text = page(at)
    corr = text.split("Corrections, last 30 days", 1)[0]
    assert "2 versions" in corr
    assert "4.100%" in corr
    assert "4.180%" in corr


def test_verdict_under_the_price_answers_do_the_sources_agree(run_home: Run) -> None:
    text = page(select(run_home("local"), "SPY"))
    verdict = re.search(r'<div class="db-verdict db-verdict-(\w+)".*?</div>', text, flags=re.S)
    assert verdict is not None
    assert "Do the sources agree?" in verdict.group(0)
    assert verdict.start() < text.index('<div class="db-hzs"')
    one = page(select(run_home("hosted"), "UST_2Y"))
    assert "One source only" in one or "sources" in one
