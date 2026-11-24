# ruff: noqa: RUF001  (the sigma sign is intentional output)
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

from brief_synth import FRI, MON_0700, daily, monday_rows

REPO = Path(__file__).resolve().parents[3]
HOME = REPO / "app" / "Home.py"
Run = Callable[..., AppTest]


def page(at: AppTest, *, limits: bool = True) -> str:
    """All rendered markdown and captions; ``limits=False`` drops the known-limits bullets."""
    parts = [
        m.value
        for m in at.markdown
        if (limits or not m.value.startswith("- ")) and not m.value.lstrip().startswith("<style>")
    ]
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
    assert "Last run Mon" in text  # T60: the status line names the latest run's day
    assert "2 checks ok" in text
    assert "2/2 checks pass" in text
    assert "hosted" in text
    assert "delayed+official" in text
    for title in ("Rates", "FX", "Commodities", "Equities"):
        assert f'class="db-grp-chev">▾</span>{title}<' in text  # T60: groups start open
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
    foot = text.split('<div class="db-foot">', 1)[1]
    assert "withheld" not in foot  # SPY fell back to tier 3: nothing is withheld (T58: footer)


def test_footer_names_withheld_instruments() -> None:
    """The withheld caption moved to the page footer (T58)."""
    from types import SimpleNamespace

    from app.components import panels

    ov = SimpleNamespace(withheld=2, mode="hosted", mode_label="hosted · delayed+official")
    foot = panels.footer(ov, "")  # type: ignore[arg-type]
    assert "2 instrument(s) withheld in hosted mode" in foot
    assert "withheld" not in panels.footer(SimpleNamespace(**{**vars(ov), "withheld": 0}), "")  # type: ignore[arg-type]


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


def test_strip_items_select_the_instrument(run_home: Run) -> None:
    at = run_home("hosted")
    text = page(at)
    assert '<a class="db-sx' in text
    assert 'data-db-id="UST_10Y"' in text
    assert at.button(
        key="-".join(("pick", "strip", "UST_10Y"))
    )  # T57: a click selects it in-session


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


def verdict_card(text: str) -> str:
    """The "Do the sources agree?" block under the chart (T58's right rail card)."""
    return text.split('<div class="db-label">Do the sources agree?</div>', 1)[1].split(
        '<div class="db-label">Risk and range</div>', 1
    )[0]


def test_ladder_uses_p009_labels(run_home: Run) -> None:
    ladder = verdict_card(page(select(run_home("local"), "UST_10Y")))
    assert 'db-field">yield<' in ladder
    assert 'db-field">close<' not in ladder  # a yield is not a close
    eur = verdict_card(page(select(run_home("local"), "EURUSD")))
    assert "ECB reference rate" in eur


def test_hosted_details_never_show_tier1(run_home: Run) -> None:
    text = page(select(run_home("hosted"), "SPY"), limits=False)
    assert "alpaca" not in text
    assert 'data-tier="1"' not in text
    assert "600.00" not in text


def test_local_details_show_every_tier_and_checks(run_home: Run) -> None:
    text = page(select(run_home("local"), "SPY"))
    ladder = verdict_card(text)
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
    # T59 retired the Altair price_chart and compare_chart: no page drew them any more
    assert not hasattr(chart, "price_chart")
    assert not hasattr(chart, "compare_chart")
    assert chart.tv_series(bare, style=chart.CANDLES)[0]["kind"] == "line"
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
    for label in ("1W", "1M", "3M", "YTD", "1Y"):
        assert f">{label}<" in strip
    assert ">1D<" not in strip  # T60: the 1D change is in the hero pill
    assert "needs" in strip  # the synthetic store holds days, not a year


def test_risk_panel_needs_twenty_days_before_volatility(run_home: Run) -> None:
    text = page(select(run_home("hosted"), "UST_10Y"))
    risk = text.split("Risk and range", 1)[1]
    assert "Realised vol, 20 days" in risk
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
    corr = text.split("Corrections, last 30 days", 1)[1]
    assert "2 versions" in corr
    assert "4.100%" in corr
    assert "4.180%" in corr


def test_verdict_card_answers_do_the_sources_agree(run_home: Run) -> None:
    """T58: one verdict in the right rail, with a mark that does not rely on colour, then the
    sources it compared (each tier's value with its provenance)."""
    card = verdict_card(page(select(run_home("local"), "SPY")))
    verdict = re.search(r'<div class="db-verdict db-verdict-(\w+)">(.*?)</div>', card, flags=re.S)
    assert verdict is not None
    tone = verdict.group(1)
    assert tone in ("ok", "warn", "fail")
    # T60: the answer is a word (Agree, Not comparable, No); only warn shows the warn mark
    answer = {"ok": "Agree", "warn": "Not comparable", "fail": "No"}[tone]
    assert f"<b>{answer}</b>" in verdict.group(2)
    assert ("db-mark-warn" in verdict.group(2)) == (tone == "warn")
    assert card.count('class="db-src-row"') >= 2  # one row per visible tier with a value
    one = verdict_card(page(select(run_home("hosted"), "UST_2Y")))
    assert "One source only" in one


# --- T58: Home v2 layout (P-022, the approved mockup) ----------------------------------------

T58_FILES = [
    REPO / "app" / "views" / "markets.py",
    REPO / "app" / "components" / "panels.py",
    REPO / "app" / "components" / "tearsheet.py",
    REPO / "app" / "components" / "instrument.py",
]
_TITLE_ATTR = re.compile(r"""\stitle\s*=\s*["'{\\]""")


def qp(at: AppTest, key: str) -> str:
    value = at.query_params[key]
    return value[0] if isinstance(value, list) else value


def visible(html: str) -> str:
    """Text a sighted user reads: no screen-reader-only spans, no tooltips, no tags."""
    html = re.sub(r'<span class="db-sr[^"]*"[^>]*>.*?</span>', "", html, flags=re.S)
    html = re.sub(r'<span class="db-tip[^"]*"[^>]*>.*?</span></span>', "</span>", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def chart_card(text: str) -> str:
    return text.split('<div class="db-label db-ch">', 1)[1].split('<div class="db-hzs"', 1)[0]


def test_layout_top_bar_has_brand_search_and_one_status_dot(run_home: Run) -> None:
    at = run_home("hosted")
    text = page(at)
    status = re.search(r'<div class="db-status">.*?</div>', text, flags=re.S)
    assert status is not None
    assert status.group(0).count('class="db-dot ') == 1
    assert "db-mode" not in status.group(0)  # the mode chip moved to the footer
    assert at.text_input(key="q").label == "Search instruments"
    assert '<span class="db-brand">Desk Brief</span>' in text


def test_layout_strip_is_one_line_with_the_basis_said_once(run_home: Run) -> None:
    """P-023: five instruments as plain text (no tiles, pills or sparklines); a basis the items
    share is said once at the start, an item with another basis says its own."""
    at = select(run_home("hosted"), "UST_10Y")
    text = page(at)
    items = re.findall(r'<a class="db-sx[^"]*" data-db-id="([^"]+)"', text)
    assert 1 <= len(items) <= 5
    sel = re.findall(r'<a class="db-sx is-sel" data-db-id="([^"]+)"', text)
    assert sel == ["UST_10Y"]
    item = re.search(r'<a class="db-sx is-sel".*?</a>', text, flags=re.S)
    assert item is not None
    assert 'class="db-prov"' in item.group(0)
    strip = "".join(re.findall(r'<a class="db-sx.*?</a>', text, flags=re.S))
    assert "db-spark" not in strip
    assert "db-pill" not in strip
    assert "db-tile" not in text
    assert text.count('<div class="db-sx-lead">') <= 1  # the shared basis, once


def test_layout_watchlist_has_full_names_and_no_raw_ids(run_home: Run) -> None:
    from app.components import panels

    from deskbrief.report.model import Row as _Row

    text = page(run_home("hosted"))
    rows = re.findall(ROW, text, flags=re.S)
    ten = next(r for r in rows if 'data-db-id="UST_10Y"' in r)
    assert "<b>10Y" in ten  # T60: the short id first, never truncated
    assert "<small>US 10-year Treasury yield</small>" in ten  # then the full name
    for r in rows:
        shown = visible(r.split(">", 1)[1])
        assert not re.search(r"\b[A-Z0-9]+_[A-Z0-9_]+\b", shown), shown  # no UST_10Y, ES_F
    for iid in ("UST_10Y_REAL", "BEI_10Y", "USD_BROAD", "HY_OAS", "COT_GC_NET", "SPY", "ES_F"):
        stub = _Row.__new__(_Row)
        object.__setattr__(stub, "instrument_id", iid)
        assert "_" not in panels.short(stub)
    css = panels.HOME_CSS
    rule = css.split(".db-wl-name small {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in rule
    assert "text-overflow: ellipsis" in rule


def test_sigma_tags_mark_unusual_moves_on_watchlist_rows() -> None:
    """Biggest moves (T36 strip) became a sigma tag on the row (P-022)."""
    from types import SimpleNamespace

    from app.components import panels

    from deskbrief.report.model import Notable

    ov = SimpleNamespace(
        sections=[],
        moves=(
            Notable("move", "10Y fell 12bp; the move is 3.4σ of its 60-day moves", "UST_10Y"),
            Notable("move", "no sigma here", "SPY"),
        ),
    )
    sigs = panels.sigmas(ov)  # type: ignore[arg-type]
    assert sigs == {"UST_10Y": ("3.4σ", ov.moves[0].text)}
    tag = panels.sigma_tag(sigs["UST_10Y"])
    assert '<span class="db-sig">3.4σ' in tag
    assert "unusual move: 10Y fell 12bp" in tag  # the report's sentence, for screen readers


def test_layout_chart_card_header_controls_and_horizons(run_home: Run) -> None:
    at = run_home("hosted")
    text = page(at)
    card = chart_card(text)
    assert '<span class="db-ch-name">S&amp;P fut</span>' in card
    assert '<span class="db-ch-value">' in card
    assert 'class="db-prov"' in card
    assert '<div class="db-ch-foot">' in card  # provenance line: field, tier, as-of, source
    rng = at.segmented_control(key="db-rng")
    assert rng.options == ["1M", "3M", "6M", "1Y", "All"]
    assert at.selectbox(key="vs-ES_F").label == "Compare with"  # in the small menu
    assert at.get("download_button")[0].label == "Download CSV of the range shown"
    # P-023: the pill holds the 1D change and its basis; the horizon row starts at 1W
    assert re.search(r'<span class="db-pill db-pill-(up|down|flat)">', card)
    assert '<span class="db-pill-basis">1D · ' in card
    row = text.split('aria-label="performance over horizons"', 1)[1].split("</div></div>", 1)[0]
    labels = re.findall(r'class="db-hz-label">([^<]+)<', row)
    assert labels == ["1W", "1M", "3M", "YTD", "1Y"]
    assert '<div class="db-chfoot">Daily ' in text  # chart and compare captions: the footer


def test_layout_tabs_and_notes_under_the_chart(run_home: Run) -> None:
    at = run_home("local")
    assert [t.label for t in at.tabs] == ["All values", "Lineage", "Checks", "Corrections"]
    text = page(at)
    assert '<div class="db-label">Do the sources agree?</div>' in text
    risk = text.split('<div class="db-label">Risk and range</div>', 1)[1]
    for label in ("Realised vol, 20 days", "Latest move"):
        assert f"<span>{label}</span>" in risk
    assert '<div class="db-rangebox">' in risk
    assert re.search(r'class="db-range-label">(52-week range|Range, \d+ days)<', risk)
    assert at.button(key="-".join(("src", "ES_F"))).label == "Lineage: source, fetch and raw file"


def test_layout_footer_carries_mode_and_glyph_legend(run_home: Run) -> None:
    text = page(run_home("local"))
    foot = text.split('<div class="db-foot">', 1)[1]
    assert "local · live+delayed+official" in foot
    assert 'class="db-glyphs"' in foot


# --- T58: the inventory table, every T36/T54 item at its new place -----------------------------


def tab(at: AppTest, label: str) -> str:
    t = next(t for t in at.tabs if t.label == label)
    return "\n".join(m.value for m in t.markdown)


@pytest.mark.parametrize(
    "item",
    [
        "ladder_and_verdict", "crosscheck_strip", "known_limits", "withheld_status_mode",
        "chart_captions", "every_value", "lineage", "checks", "corrections", "moves",
        "curve_and_small_multiples",
    ],
)  # fmt: skip
def test_inventory_item_lives_at_its_new_place(run_home: Run, item: str) -> None:
    at = select(run_home("local"), "SPY")
    text = page(at)
    if item == "ladder_and_verdict":  # three-tier ladder and the verdict: the right rail
        card = verdict_card(text)
        assert "alpaca" in card
        assert 'class="db-verdict db-verdict-' in card
    elif item == "crosscheck_strip":  # 14-day cross-check strip: Checks tab
        assert "Cross-checks, last 14 days" in tab(at, "Checks")
    elif item == "known_limits":  # Known limits: Checks tab
        limits = [m.value for t in at.tabs if t.label == "Checks" for m in t.markdown]
        assert any(v.startswith("- Dashboard:") for v in limits)
        assert not [e for e in at.expander if e.label == "Known limits"]
    elif item == "withheld_status_mode":  # status dot in the top bar, mode in the footer
        assert 'class="db-dot db-dot-' in text.split('<div class="db-status">', 1)[1]
        assert "local · live+delayed+official" in text.split('<div class="db-foot">', 1)[1]
    elif item == "chart_captions":  # sources, tiers and the date span: the chart footer
        foot = text.split('<div class="db-chfoot">', 1)[1].split("</div>", 1)[0]
        assert re.search(r"\d{4} to \d+ \w{3} \d{4}", foot)
    elif item == "every_value":
        assert "Every value we hold" in tab(at, "All values")
    elif item == "lineage":
        assert "Parser" in tab(at, "Lineage")
    elif item == "checks":
        assert "Data checks, last 14 days" in tab(at, "Checks")
    elif item == "corrections":
        assert "Corrections, last 30 days" in tab(at, "Corrections")
    elif item == "moves":  # biggest moves: sigma tags on the rows (no strip)
        assert "db-moves-label" not in text
        assert "db-move" not in text
    elif item == "curve_and_small_multiples":  # Macro, linked from Home
        assert "db-curve" not in text
        assert "db-ovgrid" not in text
        link = next(p for p in at.page_link if "Macro" in p.label)
        assert link.proto.page.endswith("Macro") or "2_Macro" in link.proto.page


def test_hosted_withholds_the_known_tier1_rows_in_every_tab(run_home: Run) -> None:
    at = select(run_home("hosted"), "SPY")
    for t in at.tabs:
        body = "\n".join(m.value for m in t.markdown if not m.value.startswith("- "))
        assert "alpaca" not in body  # (the known-limits bullets name every source)
        assert 'data-tier="1"' not in body


# --- T58: review-round extras --------------------------------------------------------------


def test_every_home_chart_shows_the_tradingview_attribution(run_home: Run) -> None:
    from app.components import tvchart

    for mode in ("hosted", "local"):
        text = page(run_home(mode))
        assert text.count(tvchart.attribution_html()) == 1  # one chart, one attribution
        assert tvchart.NOTE_FALLBACK not in text


def test_attribution_follows_the_compare_chart_too(run_home: Run) -> None:
    from app.components import tvchart

    at = run_home("hosted")
    at.selectbox(key="vs-ES_F").set_value("UST_10Y").run()
    assert not at.exception
    assert page(at).count(tvchart.attribution_html()) == 1


def test_markets_draws_tvcharts_only_through_tvchart_chart() -> None:
    """``tvchart.chart`` always ends with ``tvchart.attribution()``; Home has no other path."""
    src = (REPO / "app" / "views" / "markets.py").read_text(encoding="utf-8")
    assert "tvchart.chart(" in src
    assert "altair_chart" not in src
    assert "price_chart" not in src
    assert "compare_chart" not in src


@pytest.mark.parametrize("path", T58_FILES, ids=lambda p: p.name)
def test_no_sixty_second_caches_remain(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    assert "ttl=60" not in src
    for block in re.findall(r"@st\.cache_data\(([^)]*)\)\ndef \w+\(([^)]*)\)", src, flags=re.S):
        assert "DATA_CACHE_TTL_SECONDS" in block[0]
        assert re.search(r"\bver: str\b", block[1]), block[1]  # keyed on data.data_version()


def test_caches_are_keyed_on_the_data_version(
    run_home: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.components import data, instrument

    run_home("hosted")  # store and config
    seen: list[str] = []
    real = instrument._ladder

    def spy(*args: object) -> object:
        seen.append(str(args[4]))
        return real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(instrument, "_ladder", spy)
    monkeypatch.setattr(data, "data_version", lambda config: "v-test")
    import os

    from deskbrief.config import load_config

    cfg = load_config(os.environ["DESKBRIEF_CONFIG"])
    instrument.ladder(cfg, "UST_10Y", frozenset({1}), MON_0700)
    assert seen == ["v-test"]


def test_selection_and_range_survive_a_visit_to_macro(run_home: Run) -> None:
    """Markets, then Macro, then Markets: the instrument and range come back, although
    Streamlit drops the widget's state and the browser's page change drops ``?id=``/``?r=``."""
    at = run_home("hosted")
    at.button(key="-".join(("pick", "wl", "UST_10Y"))).click().run()
    at.segmented_control(key="db-rng").set_value("1Y").run()
    assert qp(at, "r") == "1Y"
    at.switch_page("pages/2_Macro.py").run()
    assert not at.exception
    assert "db-rng" not in at.session_state  # Streamlit dropped the widget's own state
    for key in list(at.query_params):  # what the browser's page change does to the URL
        del at.query_params[key]
    at.switch_page("Home.py").run()
    assert not at.exception
    assert '<span class="db-ch-name">10Y</span>' in page(at)
    assert at.segmented_control(key="db-rng").value == "1Y"
    assert qp(at, "id") == "UST_10Y"
    assert qp(at, "r") == "1Y"


def test_a_deep_link_range_opens_that_range(run_home: Run) -> None:
    run_home("hosted")
    at = AppTest.from_file(str(HOME), default_timeout=60)
    at.query_params["r"] = "6M"
    at.run()
    assert at.segmented_control(key="db-rng").value == "6M"


def test_chart_header_shows_each_caveat_word_once(run_home: Run) -> None:
    """Review round: "$151.58 proxy … +22.0% proxy" showed the caveat twice. Once, visibly."""
    from app.components.values import VISIBLE_FLAGS

    at = select(run_home("hosted"), "SPY")  # an ETF proxy
    shown = visible(chart_card(page(at)))
    assert len(re.findall(r"\bproxy\b", shown)) == 1
    for word in VISIBLE_FLAGS:
        assert len(re.findall(rf"\b{word}\b", shown)) <= 1, word
    assert 'class="db-flag">proxy</span>' in chart_card(page(at))  # still visible as a tag


@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_no_title_attribute_in_generated_markup(run_home: Run, mode: str) -> None:
    """P-022: tooltips are CSS. Streamlit's own ``title`` on the component's ``<iframe>`` is
    framework markup and is not part of what this page generates."""
    at = select(run_home(mode), "SPY")
    everything = page(at) + "\n".join(m.value for t in at.tabs for m in t.markdown)
    assert not _TITLE_ATTR.search(everything)


@pytest.mark.parametrize("path", T58_FILES, ids=lambda p: p.name)
def test_no_title_attribute_in_t58_sources(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    assert not re.search(r"""\btitle=["'\\]""", src)


def test_health_tooltip_shows_on_hover_through_the_button() -> None:
    """T57 carry-over: the transparent button covers the badge, so hovering the badge's
    container (not the badge) must show the counts."""
    from app import theme

    assert ".st-key-db-pick-health:hover .db-tip" in theme.CSS
    rule = theme.CSS.split(".st-key-db-pick-health:hover .db-tip", 1)[1].split("}", 1)[0]
    assert "display: block" in rule


# --- T58 round 1: caveat words beside every figure, sigma tags on Home ----------------------

#: SYNTHETIC: a delayed SPY tick from an IEX dataset served by a fallback source, so hosted
#: mode (no tier 1) still shows a proxy figure with IEX and fallback caveats
SPY_IEX_FALLBACK = dict(tier=2, source="yahoo", dataset="iex_delayed", is_fallback=True)


def _caveat_rows() -> list:  # type: ignore[type-arg]
    from brief_synth import MON, et, tick

    return [*monday_rows(), tick("SPY", et(MON, "06:59"), 599.10, **SPY_IEX_FALLBACK)]


def _row_source(row: str) -> str:
    """The source a verdict row names: ``<span>{glyph} {source} · {tier}``."""
    m = re.match(r'<div class="db-src-row"><span>\S+ ([^ <]+) · ', row)
    assert m, row
    return m.group(1)


def _aria(html: str) -> list[str]:
    return re.findall(r'class="db-prov"[^>]*aria-label="([^"]*)"', html)


@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_range_head_figure_keeps_its_caveat_words(run_home: Run, mode: str) -> None:
    """Review round 1, blocking: the latest value in the 52-week head showed no proxy, IEX or
    fallback. They are visible beside it and in its accessible label."""
    text = page(select(run_home(mode, rows=_caveat_rows()), "SPY"))
    head = text.split('<div class="db-range-head">', 1)[1].split("</div>", 1)[0]
    shown = visible(head)
    want = ("proxy", "IEX", "fallback") if mode == "hosted" else ("proxy", "IEX")
    for word in want:
        assert re.search(rf"\b{word}\b", shown), (word, shown)
    (label,) = _aria(head)
    for word in want:
        assert word in label
    ends = text.split('<div class="db-range-ends">', 1)[1].split("</div>", 1)[0]
    assert visible(ends).count("proxy") == 2  # low and high: closes of the proxy


@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_every_verdict_row_keeps_its_caveat_words(run_home: Run, mode: str) -> None:
    """Review round 1, blocking: the verdict card's source rows had no caveat words."""
    card = verdict_card(page(select(run_home(mode, rows=_caveat_rows()), "SPY")))
    rows = re.findall(r'<div class="db-src-row">.*?</b></div>', card, flags=re.S)
    assert len(rows) >= 2
    for r in rows:
        assert re.search(r"\bproxy\b", visible(r)), r  # SPY is an ETF proxy: every row
        (label,) = _aria(r)
        assert "proxy" in label
    # T59: the IEX rows are picked by the feed behind them (the fixture's two IEX datasets),
    # not by whether "IEX" already appears in the markup, which would let a row that lost its
    # word go unchecked
    iex_feeds = {"alpaca": "iex_latest", SPY_IEX_FALLBACK["source"]: SPY_IEX_FALLBACK["dataset"]}
    by_source = {_row_source(r): r for r in rows}
    want = {"yahoo"} if mode == "hosted" else {"alpaca", "yahoo"}  # hosted never shows tier 1
    assert {s for s in by_source if s in iex_feeds} == want
    for source in want:
        r = by_source[source]
        assert re.search(r"\bIEX\b", visible(r)), (source, iex_feeds[source], r)
        assert "IEX" in _aria(r)[0]
    fallback = [r for r in rows if "yahoo" in r]
    for r in fallback:
        assert re.search(r"\bfallback\b", visible(r))
        assert "fallback" in _aria(r)[0]


@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_every_figure_of_a_proxy_shows_proxy(run_home: Run, mode: str) -> None:
    """The sweep: every compact figure on a proxy's page (strip, header, horizons, risk, range,
    verdict, all values, corrections) carries "proxy" visibly and in its label."""
    at = select(run_home(mode, rows=_caveat_rows()), "SPY")
    hero = page(at).split('<div class="db-label db-ch">', 1)[1]
    card = hero.split('<div class="db-label">Watchlist', 1)[0]  # the hero column, then tabs
    card += "\n".join(m.value for t in at.tabs for m in t.markdown if not m.value.startswith("- "))
    figs = re.findall(r'<span class="db-fig">(?:(?!<span class="db-fig">).)*', card, flags=re.S)
    figs = [f for f in figs if 'class="db-prov' in f]  # with a visible glyph
    assert len(figs) >= 8
    for f in figs:
        assert 'class="db-flag">proxy<' in f, f
        assert "proxy" in _aria(f)[0]


def _with_move(monkeypatch: pytest.MonkeyPatch, iid: str, text: str) -> None:
    """Inject one notable move into the overview (the synthetic store is too short for the
    report's own anomaly scoring)."""
    import dataclasses

    from app.components import data

    from deskbrief.report.model import Notable

    real = data.overview

    def overview(*args: object, **kw: object) -> object:
        ov = real(*args, **kw)  # type: ignore[arg-type]
        return dataclasses.replace(ov, moves=(Notable("move", text, iid), *ov.moves))

    monkeypatch.setattr(data, "overview", overview)


def test_home_shows_a_sigma_tag_on_the_watchlist_row(
    run_home: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_move(monkeypatch, "UST_10Y", "UST_10Y fell 3bp; the move is 3.4σ of its usual moves")
    text = page(run_home("hosted"))
    row = next(r for r in re.findall(ROW, text, flags=re.S) if 'data-db-id="UST_10Y"' in r)
    assert '<span class="db-sig">3.4σ' in row
    sr = row.split('class="db-sig"', 1)[1]
    assert "10Y fell 3bp" in sr
    assert "UST_10Y" not in sr  # the display name, never the raw id
    others = [r for r in re.findall(ROW, text, flags=re.S) if 'data-db-id="UST_10Y"' not in r]
    assert not any("db-sig" in r for r in others)


def test_no_sigma_tag_for_a_move_across_a_gap() -> None:
    """Review round 1: the tag must not contradict the risk card's "no data" (same rule)."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.components import panels

    from deskbrief.report.model import Notable

    def row(iid: str, prev: datetime) -> SimpleNamespace:
        last = SimpleNamespace(as_of=datetime(2026, 11, 16, 15, tzinfo=UTC))
        return SimpleNamespace(
            instrument_id=iid, change=SimpleNamespace(last=last, previous=SimpleNamespace(
                as_of=prev))
        )  # fmt: skip

    gapped = row("UST_10Y", datetime(2026, 10, 1, 15, tzinfo=UTC))  # 32 business days earlier
    normal = row("UST_2Y", datetime(2026, 11, 13, 15, tzinfo=UTC))  # the Friday before
    ov = SimpleNamespace(
        sections=[SimpleNamespace(rows=(gapped, normal))],
        moves=(
            Notable("move", "UST_10Y is 4.0σ", "UST_10Y"),
            Notable("move", "UST_2Y is 3.1σ", "UST_2Y"),
        ),
    )
    sigs = panels.sigmas(ov)  # type: ignore[arg-type]
    assert "UST_10Y" not in sigs
    assert sigs["UST_2Y"] == ("3.1σ", "2Y is 3.1σ")


def test_known_limits_describe_the_v2_layout() -> None:
    from app.components.data import DASHBOARD_LIMITS

    text = " ".join(DASHBOARD_LIMITS)
    assert "Biggest moves" not in text
    assert "sigma tag on their watchlist row" in text
    assert "yield curve (on Macro)" in text


# --- T60: Home v3, no containers (P-023) ---------------------------------------------------


def _sheets() -> dict[str, str]:
    from app import theme
    from app.components import kit, panels

    return {
        "theme.CSS": theme.CSS,
        "kit.PAGE_CSS": kit.PAGE_CSS,
        "panels.HOME_CSS": panels.HOME_CSS,
    }


def _rule(css: str, selector: str) -> str:
    """The declarations of the first rule whose selector list is exactly ``selector``."""
    m = re.search(r"(?:^|[}\n])\s*" + re.escape(selector) + r"\s*\{\{?([^}]*)\}", css)
    assert m is not None, selector
    return m.group(1)


@pytest.mark.parametrize("name", ["theme.CSS", "kit.PAGE_CSS", "panels.HOME_CSS"])
def test_shared_stylesheets_are_flat(name: str, flat_css: Callable[[str], list[str]]) -> None:
    assert flat_css(_sheets()[name]) == []


def test_flat_checker_is_default_deny(flat_css: Callable[[str], list[str]]) -> None:
    """The checker fails on the T59 KPI panel and on any selector it has never heard of."""
    assert flat_css(".db-kpi { background: #13161B; border-radius: 12px; padding: 18px; }")
    assert flat_css(".brand-new-thing { box-shadow: 0 8px 24px rgba(0,0,0,.4); }")
    assert flat_css(".x { border: 1px solid #22262E; }")  # a box, not a hairline
    assert flat_css(".x { border-left: 1px solid #22262E; }")
    assert flat_css(".x { border-top: 1px solid #222; border-bottom: 1px solid #222; }")
    assert flat_css("@media (max-width: 640px) { .x { background: #13161B; } }")
    assert flat_css(".db-kpi .db-x, .db-tip { background: #20252D; }")  # one denied selector
    assert not flat_css(".x { border-top: 1px solid #22262E; background: transparent; }")
    assert not flat_css(".db-tip { background: #20252D; border-radius: 6px; }")  # exempt


def test_native_expander_tab_panel_and_dataframe_are_flat() -> None:
    from app import theme

    css = theme.CSS
    for sel in (
        '[data-testid="stExpander"] details',
        '[data-testid="stExpanderDetails"]',
        '[data-testid="stTabs"] [data-baseweb="tab-panel"]',
        '[data-testid="stDataFrame"]',
    ):
        rule = _rule(css, sel)
        assert "border: 0" in rule, sel
        assert "border-radius: 0" in rule, sel
    for sel in ('[data-testid="stExpander"] details', '[data-testid="stTabs"] [data-baseweb='
                '"tab-panel"]'):  # fmt: skip
        assert "background: transparent" in _rule(css, sel)


def test_four_type_sizes() -> None:
    """P-023: 42 px hero (36 px on a phone), 15, 13 and 11.5 px; nothing else."""
    from app import theme

    allowed = {f"{s:g}px" for s in theme.TYPE_SIZES}
    for name, css in _sheets().items():
        sizes = set(re.findall(r"font-size:\s*([\d.]+(?:px|rem|em))", css))
        sizes |= set(re.findall(r"font:\s*\d+\s+([\d.]+px)", css))
        assert sizes <= allowed, (name, sizes - allowed)


def test_home_blocks_sit_in_their_p023_places(run_home: Run) -> None:
    """Top bar, strip, then the main row (hero, then the watchlist), then the tabs, footer."""
    text = page(run_home("local"))
    order = [
        '<span class="db-brand">',
        '<div class="db-status">',
        '<a class="db-sx',
        '<div class="db-label db-ch">',
        'class="db-pill',
        '<div class="db-ch-foot">',
        'aria-label="performance over horizons"',
        '<div class="db-label">Do the sources agree?</div>',
        '<div class="db-label">Risk and range</div>',
        '<div class="db-label">Watchlist',
        "Every value we hold",
        '<div class="db-foot">',
    ]
    at = [text.find(s) for s in order]
    assert -1 not in at, [s for s, i in zip(order, at, strict=True) if i < 0]
    assert at == sorted(at)


def test_strip_says_a_shared_basis_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.components import panels
    from app.components.values import Provenance

    labels = {"A": "Fri", "B": "Fri", "C": "Fri", "D": "vs official"}
    rows = [
        SimpleNamespace(instrument_id=i, name=i, available=True, value_text="1", change_text="+1%")
        for i in labels
    ]
    ov = SimpleNamespace(headline=[(r.instrument_id, r) for r in rows], as_of=MON_0700)
    monkeypatch.setattr(panels, "move_label", lambda r: labels[r.instrument_id])
    monkeypatch.setattr(panels, "provenance_of", lambda r, now: Provenance("s", 3, "x", "close"))
    monkeypatch.setattr(panels, "_change", lambda r, p: "+1%")
    lead, items = panels.strip_items(ov)  # type: ignore[arg-type]
    assert lead == "Fri close"
    bodies = dict(items)
    assert all("db-sx-basis" not in bodies[i] for i in "ABC")
    assert 'db-sx-basis">vs official<' in bodies["D"]


def test_watchlist_runs_full_length_and_the_horizon_row_holds_the_attribution(
    run_home: Run,
) -> None:
    from app.components import tvchart

    src = (REPO / "app" / "views" / "markets.py").read_text(encoding="utf-8")
    assert 'st.container(key="db-watch")' in src  # no height=: never an inner scroller
    assert "WATCH_HEIGHT" not in src
    under = page(run_home("local")).split('<div class="db-under">', 1)[1]
    _, rest = under.split('aria-label="performance over horizons"', 1)[1].split("</div></div>", 1)
    assert rest.startswith(tvchart.attribution_html())  # at the right of the horizons


def test_attribution_only_when_the_tradingview_chart_drew(
    run_home: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.components import tvchart

    monkeypatch.setattr(tvchart, "_component", lambda: None)  # the Altair fallback draws
    at = run_home("local")
    assert tvchart.NOTE_FALLBACK in page(at)
    assert tvchart.attribution_html() not in page(at)


def test_a_folded_group_keeps_the_selected_row(run_home: Run) -> None:
    at = select(run_home("hosted"), "UST_10Y")
    at.button(key="fold-rates").click().run()
    assert not at.exception
    keys = {b.key for b in at.button}
    assert "pick-wl-UST_10Y" in keys  # the selection never folds away
    assert "pick-wl-UST_2Y" not in keys  # the rest of the group did
    assert "pick-wl-EURUSD" in keys  # other groups stay open
    assert at.button(key="fold-rates").label == "Unfold Rates"
    at.button(key="fold-rates").click().run()
    assert "pick-wl-UST_2Y" in {b.key for b in at.button}


# --- T60 bugs 1-3: CSS regression checks -----------------------------------------------------


def test_bug1_verdict_wraps_and_links_never_overlap_the_range() -> None:
    from app.components import panels

    css = panels.HOME_CSS
    src = _rule(css, ".db-src-row > span")
    assert "min-width: 0" in src
    assert "overflow-wrap: anywhere" in src
    assert "grid-template-columns: minmax(0, 1fr) auto" in _rule(css, ".db-src-row")
    assert "overflow-wrap: anywhere" in _rule(css, ".db-verdict-p, .db-verdict-sum")
    links = _rule(css, ".st-key-db-links")
    assert "margin-top: 16px" in links
    assert "border-top: 1px solid" in links
    assert "position: relative" in links
    assert "position: static" in _rule(css, ".db-range-ends > span")
    assert not re.search(r"margin(-top)?:\s*-", _rule(css, ".db-rangebox"))
    # the notes columns never squeeze: each takes half the hero, both wrap
    assert "min-width: 0" in _rule(css, ".st-key-db-detail, .st-key-db-risk")


def test_bug2_row_hover_content_is_fixed_and_opens_on_focus(run_home: Run) -> None:
    from app.components import panels

    css = panels.HOME_CSS
    assert "position: fixed" in _rule(css, ".db-tip.db-wl-tip")
    wl = 'div[class*="st-key-db-pick-wl-"]'
    assert f"{wl}:hover .db-wl-tip, {wl}:has(button:focus-visible) .db-wl-tip" in css
    text = page(select(run_home("local"), "SPY"))
    for r in re.findall(ROW, text, flags=re.S):
        tip = re.search(r'<span class="db-tip db-wl-tip"[^>]*>(.*?)</span></span>', r, flags=re.S)
        assert tip is not None, r
        if "no data" in tip.group(1):
            continue
        for part in ('class="db-field">', 'class="db-src">', 'class="db-asof">'):
            assert part in tip.group(1)  # field, source and as-of, with the name and tier
        assert re.search(r"(live|delayed|official)", tip.group(1))
        assert "title=" not in r


def test_bug3_names_never_break_one_letter_per_line() -> None:
    from app.components import panels

    css = panels.HOME_CSS
    b = _rule(css, ".db-wl-name b")
    assert "white-space: nowrap" in b
    small = _rule(css, ".db-wl-name small")
    for decl in ("white-space: nowrap", "overflow: hidden", "text-overflow: ellipsis"):
        assert decl in small
    assert "min-width: 0" in _rule(css, ".db-wl-name")
    # the watchlist keeps about 352 px beside the hero and goes full width below 960 px
    watch = '[data-testid="stColumn"]:has(.st-key-db-watch)'
    assert "flex: 0 0 352px" in _rule(css, watch)


def test_all_values_source_folds_into_the_date_cell_on_a_phone(run_home: Run) -> None:
    from app.components import panels

    at = select(run_home("local"), "UST_10Y")
    body = tab(at, "All values")
    assert "<th class='db-c-src'>Source</th>" in body
    assert "<span class='db-tagline db-src-m'>treasury · official</span>" in body
    assert (
        ".db-values .db-c-src {{ display: none; }}".replace("{{", "{").replace("}}", "}")
        in panels.HOME_CSS
    )
