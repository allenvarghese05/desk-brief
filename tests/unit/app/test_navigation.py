"""In-session navigation (T57, P-022): the router, instrument selection, deep links.

Every page runs under ``st.navigation`` in ``app/Home.py``; selecting an instrument sets
``st.query_params["id"]`` from a button callback, so the Streamlit session (and its state)
survives the click. ``?id=`` deep links open the instrument.

SYNTHETIC FIXTURE: data comes from ``brief_synth`` (Monday 16 Nov 2026, 07:00 ET).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from conftest import HOME

Run = Callable[..., AppTest]


def pick(*parts: str) -> str:
    """Key of a selection button, e.g. ``pick("wl", "SPY")``. Built, not spelled out:
    gitleaks reads a literal ``key="..."`` as an API key."""
    return "-".join(("pick", *parts))


@pytest.fixture(autouse=True)
def _close_new_connections() -> Iterator[None]:
    """Close the shared DuckDB connections a test's stores opened (T56 keeps one per store for
    the process), so the suite does not run out of native threads."""
    from app.components import data

    before = set(data._SLOTS)
    yield
    for key in set(data._SLOTS) - before:
        slot = data._SLOTS.pop(key)
        if slot.current is not None and not slot.busy:
            slot.current.conn.close()


def page(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def chart_name(at: AppTest) -> str:
    m = re.search(r'<span class="db-ch-name">([^<]*)</span>', page(at))
    assert m is not None
    return m.group(1)


def qp(at: AppTest, key: str) -> str:
    value = at.query_params[key]
    return value[0] if isinstance(value, list) else value


# --- the router ---------------------------------------------------------------------------


def test_router_registers_every_page_in_the_top_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    real = st.navigation

    def spy(pages: list[st.Page], **kwargs: Any) -> Any:
        seen["pages"], seen["kwargs"] = pages, kwargs
        return real(pages, **kwargs)

    monkeypatch.setattr(st, "navigation", spy)
    at = AppTest.from_file(str(HOME), default_timeout=60).run()
    assert seen["kwargs"]["position"] == "top"  # no sidebar page list
    pages = {p["page_name"]: p for p in at._registered_pages.values()}
    assert list(pages) == ["Markets", "Macro", "Cross checks", "Data health", "Sources", "Runs"]
    # the old multipage URLs, so bookmarks keep working
    assert {n: p["url_pathname"] for n, p in pages.items()} == {
        "Markets": "",
        "Macro": "Macro",
        "Cross checks": "Cross_checks",
        "Data health": "Data_health",
        "Sources": "Sources",
        "Runs": "Runs",
    }
    assert pages["Markets"]["script_path"].endswith("app/views/markets.py")
    files = sorted(p["script_path"].rsplit("/", 1)[-1] for p in pages.values())
    assert files == [
        "2_Macro.py",
        "3_Cross_checks.py",
        "4_Data_health.py",
        "5_Sources.py",
        "6_Runs.py",
        "markets.py",
    ]


def test_sidebar_page_list_is_switched_off() -> None:
    from streamlit import config

    config.get_config_options(force_reparse=True)
    assert config.get_option("client.showSidebarNavigation") is False


# --- selecting an instrument ----------------------------------------------------------------


def test_clicking_an_instrument_sets_the_id_in_the_same_session(run_home: Run) -> None:
    at = run_home("hosted")
    assert chart_name(at) == "S&amp;P fut"
    at.session_state["probe"] = "still here"  # gone if the click started a new session
    at.text_input(key="q").input("yield").run()  # session state that must survive the click
    at.button(key=pick("wl", "UST_10Y")).click().run()
    assert not at.exception
    assert qp(at, "id") == "UST_10Y"
    assert chart_name(at) == "10Y"
    assert at.session_state["probe"] == "still here"
    assert at.text_input(key="q").value == "yield"


def by_group(at: AppTest) -> AppTest:
    """Flip the watchlist switch to "By group" (P-024: "Unusual first" folds every group)."""
    return at.segmented_control(key="db-wl-mode").set_value("By group").run()


@pytest.mark.parametrize("where", ["strip", "wl"])  # T60: the strip replaced the tiles
def test_every_selectable_item_selects_in_session(run_home: Run, where: str) -> None:
    at = by_group(run_home("hosted"))  # P-024: "Unusual first" folds the groups
    at.button(key=pick(where, "UST_10Y")).click().run()
    assert qp(at, "id") == "UST_10Y"
    assert chart_name(at) == "10Y"


def test_a_folded_proxy_selects_and_opens_its_fold(run_home: Run) -> None:
    at = by_group(run_home("hosted"))
    at.button(key=pick("wl", "SPY")).click().run()
    assert qp(at, "id") == "SPY"
    folds = [e for e in at.expander if e.label.startswith("Show ")]
    spy = next(e for e in folds if any(b.key == pick("wl", "SPY") for b in e.button))
    assert spy.proto.expanded
    assert not any(e.proto.expanded for e in folds if e is not spy)


def test_every_pick_has_a_readable_button_label(run_home: Run) -> None:
    at = by_group(run_home("hosted"))
    picks = [b for b in at.button if (b.key or "").startswith("pick-wl-")]
    assert len(picks) >= 8
    ten = next(b for b in picks if b.key == pick("wl", "UST_10Y"))
    assert ten.label == "Select 10Y, US 10-year Treasury yield"


def test_deep_link_opens_the_instrument(run_home: Run) -> None:
    run_home("hosted")  # builds the store and points the config at it
    at = AppTest.from_file(str(HOME), default_timeout=60)
    at.query_params["id"] = "EURUSD"
    at.run()
    assert not at.exception
    assert chart_name(at) == "EURUSD"
    assert qp(at, "id") == "EURUSD"


@pytest.mark.parametrize(
    "file", ["pages/2_Macro.py", "pages/3_Cross_checks.py", "pages/4_Data_health.py",
             "pages/5_Sources.py", "pages/6_Runs.py"],
)  # fmt: skip
def test_every_page_opens_through_the_router_with_an_id(run_home: Run, file: str) -> None:
    at = run_home("hosted")
    at.query_params["id"] = "UST_10Y"
    at.switch_page(file).run()
    assert not at.exception
    assert '<span class="db-brand">' in page(at)


def test_health_counts_are_readable_without_hovering(run_home: Run) -> None:
    from app import theme

    at = run_home("hosted")
    assert at.button(key=pick("health")).label == "Open Data health: 2/2 checks pass"
    # keyboard focus on the badge's button shows the same counts as the hover tooltip
    assert ".st-key-db-pick-health:has(button:focus-visible) .db-tip" in theme.CSS


def test_health_badge_opens_data_health_in_session(run_home: Run) -> None:
    at = run_home("hosted")
    at.session_state["probe"] = "still here"
    at.button(key=pick("health")).click().run()
    assert not at.exception
    assert '<span class="db-brand">Data health</span>' in page(at)
    assert at.session_state["probe"] == "still here"


def test_source_details_open_the_lineage_dialog(
    run_home: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AppTest does not render dialog bodies, so the call to the dialog is recorded."""
    from app.components import kit

    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(kit, "details", lambda title, body: opened.append((title, body)))
    at = run_home("local")
    assert opened == []
    at.button(key="-".join(("src", "ES_F"))).click().run()
    assert not at.exception
    assert len(opened) == 1
    title, body = opened[0]
    assert title == "S&P fut · source and lineage"
    assert "Parser" in body  # instrument.lineage_html: source, fetch and raw file


# --- markup -------------------------------------------------------------------------------


def test_no_full_reload_links_anywhere_on_markets(run_home: Run) -> None:
    for mode in ("hosted", "local"):
        text = page(run_home(mode))
        assert 'target="_self"' not in text
        # T58: the only link is the chart's TradingView attribution, which leaves the app
        hrefs = re.findall(r'href="([^"]*)"', text)
        assert set(hrefs) <= {"https://www.tradingview.com/"}


def test_hosted_and_local_markets_agree_on_tier1(run_home: Run) -> None:
    hosted, local = page(run_home("hosted")), page(run_home("local"))
    assert 'data-tier="1"' not in hosted
    assert 'data-tier="1"' in local
    legend = 'class="db-glyphs"'
    assert ">live<" not in hosted.split(legend, 1)[1].split("</div>", 1)[0]
    assert ">live<" in local.split(legend, 1)[1].split("</div>", 1)[0]
