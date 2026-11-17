"""Dashboard Overview (T34): AppTest smoke, hosted-mode licence rule, no bare numbers.

SYNTHETIC FIXTURE: data comes from ``brief_synth`` (Monday 16 Nov 2026, 07:00 ET); SPY is the
only tier-1 instrument in it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from brief_synth import FRI, daily, monday_rows

HOME = Path(__file__).resolve().parents[3] / "app" / "Home.py"
Run = Callable[..., AppTest]


def page(at: AppTest, *, limits: bool = True) -> str:
    """All rendered markdown; ``limits=False`` drops the known-limits bullets (config metadata)."""
    return "\n".join(m.value for m in at.markdown if limits or not m.value.startswith("- "))


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
        assert any(e.label == title for e in at.expander)


def test_hosted_never_renders_tier1_rows(run_home: Run) -> None:
    at = run_home("hosted")
    text = page(at, limits=False)
    assert "alpaca" not in text
    assert 'title="live"' not in text
    assert "600.00" not in text  # the tier-1 SPY tick value
    assert "withheld" not in " ".join(c.value for c in at.caption)  # SPY fell back, not hidden


def test_hosted_falls_back_to_tier3_when_latest_is_tier1(run_home: Run) -> None:
    """SPY's latest value is a tier-1 tick; hosted shows its tier-3 Friday close instead."""
    at = run_home("hosted")
    text = page(at, limits=False)
    assert "598.50" in text
    assert "600.00" not in text
    assert any(o.startswith("SPY") for sb in at.selectbox for o in sb.options)


def test_hosted_shows_gap_for_instrument_with_only_tier1_data(run_home: Run) -> None:
    only_t1 = [r for r in monday_rows() if not (r["instrument_id"] == "SPY" and r["tier"] != 1)]
    at = run_home("hosted", rows=only_t1)
    assert "600.00" not in page(at, limits=False)
    assert "data unavailable" in page(at, limits=False)  # shown as a gap, never as the tick


def test_unset_or_unknown_mode_fails_closed_to_hosted(run_home: Run) -> None:
    for mode in (None, "", "LIVE!", "prod"):
        text = page(run_home(mode), limits=False)
        assert "600.00" not in text  # the tier-1 SPY tick value
        assert 'title="live"' not in text


def test_local_mode_shows_tier1_with_glyph(run_home: Run) -> None:
    at = run_home("local")
    text = page(at)
    assert "600.00" in text
    assert "alpaca" in text
    assert 'title="live"' in text
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
    rows = re.findall(r'<tr class="db-row".*?</tr>', text, flags=re.S)
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
    text = page(run_home("hosted"))
    for phrase in ("stale", "z", "curve", "polling", "30-day"):
        assert phrase in text.lower()


def test_provenance_drawer_lists_lineage(run_home: Run) -> None:
    at = run_home("local")
    box = at.selectbox[0]
    box.select_index(next(i for i, o in enumerate(box.options) if o.startswith("UST_10Y"))).run()
    text = page(at)
    assert "Parser" in text
    assert "synthetic-1" in text
