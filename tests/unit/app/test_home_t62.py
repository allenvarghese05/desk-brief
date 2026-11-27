"""Home v4 (P-024, card T62): three panes, the strip's sigma tags and check markers, the
watchlist switch, the brief pane, hosted mode and the dashboard labels from the T61 review.

SYNTHETIC FIXTURE: every value is made up (``brief_synth`` builders, Monday 16 Nov 2026 07:00
ET). Histories move by a repeating 1-5 step cycle, so a final move of 8 steps is a mover.
"""
# ruff: noqa: RUF001  (the sigma sign is intentional output)

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest
from test_home import Run, page

from brief_synth import FRI, MON, MON_0700, THU, World, daily, et, monday_rows, tick
from deskbrief.calendars import is_business_day
from deskbrief.report.model import BriefModel

REPO = Path(__file__).resolve().parents[3]
YH = {"source": "yahoo", "dataset": "history", "tier": 2}
CYCLE = (1, 2, 3, 4, 5)


def _history(iid: str, base: float, step: float, end: date, **kw: Any) -> list[dict[str, Any]]:
    rows, v, d, n = [], base, date(2026, 9, 1), 0
    while d <= end:
        if is_business_day("XNYS", d):
            rows.append(daily(iid, d, round(v, 6), **kw))
            sign = 1 if n % 2 == 0 else -1
            v = v * (1 + sign * CYCLE[n % 5] * step)
            n += 1
        d += timedelta(days=1)
    return rows


def price(iid: str, k: float, base: float, *, unit: str = "USD", **tick_kw: Any) -> list[Any]:
    rows = _history(iid, base, 0.002, FRI, unit=unit, **YH)
    fri = rows[-1]["value"]
    return [*rows, tick(iid, et(MON, "06:58"), fri * (1 + k * 0.002), unit=unit, **tick_kw)]


def desk_rows() -> list[dict[str, Any]]:
    """The standard Monday rows, ES_F as a mover (8 steps down), Brent and WTI apart."""
    base = [r for r in monday_rows() if r["instrument_id"] not in ("ES_F", "WTI")]
    return [
        *base,
        *price("ES_F", -8, 6000.0, unit="index_pts"),
        *price("BRENT", -15, 75.0),
        *price("WTI", -2, 70.0),
    ]


def strip_item(text: str, iid: str) -> str:
    m = re.search(rf'<a class="db-sx[^"]*" data-db-id="{iid}">.*?</a>', text, flags=re.S)
    assert m is not None, iid
    return m.group(0)


def wl_row(text: str, iid: str) -> str:
    rows = re.findall(r'<a class="db-wl-row db-row.*?</a>', text, flags=re.S)
    return next(r for r in rows if f'data-db-id="{iid}"' in r)


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------


def test_three_panes_in_source_order_brief_watchlist_market(run_home: Run) -> None:
    at = run_home("hosted", rows=desk_rows())
    assert not at.exception
    text = page(at)
    marks = ['<div class="db-brief">', '<div class="db-label">Watchlist', 'class="db-label db-ch"']
    at_ = [text.find(m) for m in marks]
    assert -1 not in at_
    assert at_ == sorted(at_)  # the phone order: brief, watchlist, market
    src = (REPO / "app" / "views" / "markets.py").read_text(encoding="utf-8")
    assert "brief_col, watch_col, hero_col = st.columns(3" in src


def test_wide_screen_places_watchlist_market_brief_and_breakpoints_reflow() -> None:
    from app.components import panels
    from test_home import _rule

    css = panels.HOME_CSS
    watch = '[data-testid="stColumn"]:has(.st-key-db-watch)'
    hero = '[data-testid="stColumn"]:has(.st-key-db-hero)'
    brief = '[data-testid="stColumn"]:has(.st-key-db-brief)'
    assert "order: 1" in _rule(css, watch)
    assert "order: 2" in _rule(css, hero)
    assert "order: 3" in _rule(css, brief)
    at_1280 = css.index("@media (max-width: 1280px) {\n  [data-testid")
    at_960 = css.index("@media (max-width: 960px) {\n  [data-testid")
    wide, narrow = css[at_1280:at_960], css[at_960 : at_960 + 400]
    assert f"{brief} {{ flex: 1 1 100% !important" in wide  # the brief moves above the panes
    assert "order: 0" in wide
    assert "order: 0" in narrow  # below 960 px everything stacks in the source order


def test_strip_shows_a_sigma_tag_only_on_ranked_markets(run_home: Run) -> None:
    text = page(run_home("hosted", rows=desk_rows()))
    es = strip_item(text, "ES_F")
    assert '<span class="db-sig">' in es  # ES_F moved more than usual
    for other in ("UST_10Y", "EURUSD", "WTI", "VIX"):
        assert "db-sig" not in strip_item(text, other)


def test_watchlist_default_is_unusual_first_with_the_movers_on_top(run_home: Run) -> None:
    at = run_home("hosted", rows=desk_rows())
    assert at.segmented_control(key="db-wl-mode").value == "Unusual first"
    text = page(at)
    movers_at = text.find("Moved more than usual · ")
    rest_at = text.find("Everything else · ")
    es = wl_row(text, "ES_F")
    assert movers_at < text.find(es) < rest_at
    assert '<span class="db-sig">' in es
    assert "<small>Yahoo · delayed · 06:58</small>" in es  # source, tier, time
    assert "since Fri 06:15 brief" in text  # the previous brief's time, not the reader's clock
    # everything else starts folded: no 2Y row until its group opens
    assert 'data-db-id="UST_2Y"' not in text
    at.button(key="fold-rates").click().run()
    assert 'data-db-id="UST_2Y"' in page(at)


def test_by_group_shows_every_group_open(run_home: Run) -> None:
    at = run_home("hosted", rows=desk_rows())
    at.segmented_control(key="db-wl-mode").set_value("By group").run()
    text = page(at)
    assert "Moved more than usual" not in text
    assert 'data-db-id="UST_2Y"' in text
    assert 'class="db-grp-chev">▾</span>Rates<' in text


def test_check_marker_on_the_strip_and_the_mover_row(run_home: Run) -> None:
    text = page(run_home("hosted", rows=desk_rows()))
    brent = wl_row(text, "BRENT")
    assert 'class="db-mark db-mark-warn db-check">check' in brent
    assert "db-check" not in wl_row(text, "ES_F")
    assert "db-check" in strip_item(text, "WTI")  # WTI is a headline item and half the pair


def test_brief_pane_shows_the_read_rules_checks_and_today(run_home: Run) -> None:
    text = page(run_home("hosted", rows=desk_rows()))
    pane = text.split('<div class="db-brief">', 1)[1]
    # the read is recomputed at the 07:00 render, so it is labelled live; none is archived, so
    # no brief time is claimed; "new since" follows the 06:15 schedule, not the reader's clock
    assert (
        '<div class="db-label">Live read<span>Mon 07:00 ET · no brief archived yet · '
        "new since Fri 06:15</span>"
    ) in pane
    assert 'class="db-read"><span class="db-regime' in pane
    assert '<div class="db-label">Why “' in pane
    assert '<div class="db-label">Check before using<span>1</span></div>' in pane
    assert "<b>Brent vs WTI</b>" in pane
    assert '<div class="db-label">Today<span>ET</span></div>' in pane
    assert "No note today: no brief has been written today yet." in pane


def test_quiet_store_reads_quiet_and_lists_nothing_to_check(run_home: Run) -> None:
    pane = page(run_home("hosted")).split('<div class="db-brief">', 1)[1]
    assert '<span class="db-regime">Quiet.</span> Nothing moved more than usual.' in pane
    assert "Nothing to check in today&#x27;s read." in pane or "Nothing to check" in pane


def test_slash_focuses_search() -> None:
    from app.views import markets as _  # noqa: F401  (imports cleanly)

    src = (REPO / "app" / "views" / "markets.py").read_text(encoding="utf-8")
    assert "st.html(SLASH_JS, unsafe_allow_javascript=True)" in src
    js = src.split('SLASH_JS = """', 1)[1].split('"""', 1)[0]
    assert 'e.key !== "/"' in js
    assert ".st-key-q input" in js  # the search field's widget key is "q"
    assert 'tag === "input"' in js  # typing "/" in a field is left alone
    assert 'key="q"' in src


# ---------------------------------------------------------------------------
# hosted mode never shows tier 1 in the strip, the movers or the brief
# ---------------------------------------------------------------------------


def _tier1_mover() -> list[dict[str, Any]]:
    alp = {"source": "alpaca", "dataset": "iex_latest", "tier": 1}
    hist = _history("AAPL", 200.0, 0.002, FRI, unit="USD", **YH)
    return [*desk_rows(), *hist, tick("AAPL", et(MON, "06:58"), hist[-1]["value"] * 1.02, **alp)]


def test_hosted_strip_movers_and_brief_never_show_a_tier1_value(run_home: Run) -> None:
    local = page(run_home("local", rows=_tier1_mover()))
    assert 'data-db-id="AAPL"' in local.split("Everything else", 1)[0]  # a mover when local
    hosted_at = run_home("hosted", rows=_tier1_mover())
    hosted = page(hosted_at)
    movers = hosted.split("Moved more than usual", 1)[1].split("Everything else", 1)[0]
    assert 'data-db-id="AAPL"' not in movers
    assert 'data-tier="1"' not in hosted
    pane = hosted.split('<div class="db-brief">', 1)[1].split('<div class="db-label">Watchlist')[0]
    assert "alpaca" not in pane


def test_overview_drops_a_mover_about_a_withheld_instrument(tmp_path: Path) -> None:
    from app.components import data

    w = World(tmp_path)
    w.observations(_tier1_mover())
    model = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    assert any(m.instrument_id == "AAPL" for m in model.movers)
    snap = data.Snapshot(model, {})
    ov = data.overview(snap, "hosted", frozenset({1}))
    assert all(m.instrument_id != "AAPL" for m in ov.movers)


# ---------------------------------------------------------------------------
# labels and notes from the T61 review
# ---------------------------------------------------------------------------


def _row(tmp_path: Path, rows: list[dict[str, Any]], iid: str, d: date = MON) -> Any:
    w = World(tmp_path)
    w.observations(rows)
    m = BriefModel.from_store(w.connect(), w.config, d, et(d, "07:00"))
    return next(r for r in m.rows() if r.instrument_id == iid)


def test_fx_label_says_vs_ny_close(tmp_path: Path) -> None:
    from app.components import panels

    td = {"source": "twelvedata", "dataset": "fx_quote", "unit": "fx_rate"}
    rows = [
        tick("EURUSD", et(FRI, "16:58"), 1.0800, **td),
        tick("EURUSD", et(MON, "06:58"), 1.0854, **td),
    ]
    r = _row(tmp_path, rows, "EURUSD")
    assert panels.move_label(r) == "vs NY close"
    assert panels.basis_text(r) == "vs Fri 13 Nov NY close (16:58 ET quote)"


def test_n225_label_says_vs_tokyo_close(tmp_path: Path) -> None:
    from app.components import panels

    rows = [
        daily("N225", THU, 38000.0, unit="index_pts", **YH),
        daily("N225", FRI, 38400.0, unit="index_pts", **YH),
        tick("N225", et(MON, "01:30"), 38016.0, unit="index_pts"),
    ]
    r = _row(tmp_path, rows, "N225")
    assert panels.move_label(r) == "vs Tokyo close"
    assert panels.basis_text(r) == "vs Fri 13 Nov Tokyo close"


def test_hero_says_why_a_future_has_no_change(tmp_path: Path) -> None:
    from app.components import panels

    rows = [
        daily("BRENT", FRI, 74.0, source="fred", dataset="series"),
        tick("BRENT", et(MON, "06:58"), 73.0),
    ]
    head = panels.chart_header(_row(tmp_path, rows, "BRENT"), MON_0700)
    assert "no change yet" in head
    assert "a future is never compared with another series" in head


def test_hero_shows_sigma_or_why_there_is_none(tmp_path: Path) -> None:
    from app.components import panels

    es = _row(tmp_path / "a", price("ES_F", -8, 6000.0, unit="index_pts"), "ES_F")
    assert es.sigma is not None
    assert f"· {es.sigma:.1f}σ</span>" in panels.chart_header(es, MON_0700)
    own = {"source": "yahoo", "dataset": "history", "field": "close", "tier": 2, "unit": "pct"}
    tsy = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
    rows = [
        daily("UST_3M", THU, 4.10, **tsy),
        daily("UST_3M", FRI, 4.0, **own),
        tick("UST_3M", et(MON, "06:58"), 3.5, unit="pct"),
    ]
    three = _row(tmp_path / "b", rows, "UST_3M")
    assert "another series would measure another basis" in panels.chart_header(three, MON_0700)


def test_an_fx_pair_says_why_it_is_not_ranked_on_the_strip_and_watchlist(tmp_path: Path) -> None:
    # PR #102 review round 2: the reason is not only in the hero
    from app.components import data, panels

    from deskbrief.report.model import NO_DAILY_BASIS_NOTE

    td = {"source": "twelvedata", "dataset": "fx_quote", "unit": "fx_rate"}
    w = World(tmp_path)
    w.observations([tick("EURUSD", et(FRI, "16:58"), 1.0800, **td),
                    tick("EURUSD", et(MON, "06:58"), 1.0854, **td)])  # fmt: skip
    model = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    r = next(r for r in model.rows() if r.instrument_id == "EURUSD")
    assert r.sigma_note == NO_DAILY_BASIS_NOTE
    row = panels.wl_row(r, MON_0700, "")
    assert row.count(NO_DAILY_BASIS_NOTE) == 2  # the tooltip and the screen-reader text
    ov = data.overview(data.Snapshot(model, {}), "local", frozenset())
    _, items = panels.strip_items(ov)
    assert NO_DAILY_BASIS_NOTE in dict(items)["EURUSD"]
    assert any("Euro / US dollar" in k.text and k.kind == "fx_unranked" for k in ov.known_limits)


def test_a_proxy_kept_out_of_the_ranking_says_why_beside_its_sigma(tmp_path: Path) -> None:
    from app.components import panels

    alp = {"source": "alpaca", "dataset": "iex_latest", "tier": 1}
    rows = [
        *price("ES_F", -8, 6000.0, unit="index_pts"),
        *price("SPY", -8, 580.0, **alp),
    ]
    w = World(tmp_path)
    w.observations(rows)
    model = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    assert [m.instrument_id for m in model.movers] == ["ES_F"]
    spy = next(r for r in model.rows() if r.instrument_id == "SPY")
    assert spy.sigma is not None
    assert spy.sigma_note == "not ranked: a proxy; its underlying (S&P fut) has a value"
    assert "not ranked: a proxy; its underlying (S&amp;P fut) has a value" in panels.chart_header(
        spy, MON_0700
    )


@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_no_title_attribute_in_v4_markup(run_home: Run, mode: str) -> None:
    at: AppTest = run_home(mode, rows=desk_rows())
    assert " title=" not in page(at)
