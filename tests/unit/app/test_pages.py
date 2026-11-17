"""Dashboard pages 2-3 (T35, part 1): AppTest smoke, hosted rule, P-009 labels, ref_date labels.

SYNTHETIC FIXTURE: a store built from ``brief_synth`` (made-up values, Monday 16 Nov 2026 07:00
ET) plus serving files built by ``deskbrief.pipeline.serving``. SPY is the only tier-1 instrument
and ``alpaca`` the only source with a tier-1 feed. No R2, no network.
"""

from __future__ import annotations

import itertools
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # t23_synth lives in tests/unit
from t23_synth import ctx_for, put

from brief_synth import FRI, MON, MON_0700, THU, World, daily, et, monday_rows

REPO = Path(__file__).resolve().parents[3]
PAGES = REPO / "app" / "pages"
PAGE_FILES = {
    "instrument": "2_Instrument.py",
    "xchecks": "3_Cross_checks.py",
}
_ENV = ("DESKBRIEF_DISPLAY_MODE", "R2_ACCOUNT_ID", "R2_READER_ACCESS_KEY_ID")
_ids = itertools.count()

RunPage = Callable[..., AppTest]


def text(at: AppTest) -> str:
    """Everything rendered: markdown (incl. SVG charts), captions, dataframes."""
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [h.value for h in at.subheader] + [e.label for e in at.expander]
    parts += [df.value.to_string() for df in at.dataframe]
    return "\n".join(parts)


def xcheck_rows(
    target: Any, ref: Any, status: str, diff: float | None, left: str, right: str, name: str,
    warn: float = 5.0,
):  # fmt: skip
    return {
        "result_id": f"XC{next(_ids)}", "run_id": "RUN1", "check_name": name,
        "group_id": "sp500", "target_date": target, "at_time": et(target, "07:00"),
        "relation": "return", "left_instrument": "SPY", "left_source": left,
        "right_instrument": "ES_F", "right_source": right, "left_value": 1.0,
        "right_value": 1.0, "expected_value": 1.0, "diff_bps": diff, "warn_bps": warn,
        "fail_bps": 10.0, "status": status,
        "details_json": json.dumps({"ref_date": ref.isoformat()}),
    }  # fmt: skip


@pytest.fixture
def run_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunPage:
    """``run_page("sources", "hosted")`` runs that page against a fresh synthetic store."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    st.cache_data.clear()
    counter = itertools.count()

    def run(page: str, mode: str | None = "hosted", empty: bool = False, **kw: Any) -> AppTest:
        w = World(tmp_path / f"w{next(counter)}")
        if not empty:
            w.observations(monday_rows())
            w.observations(
                [daily("SPY", THU, 590.25, tier=1, source="alpaca", dataset="iex_latest")]
            )
            w.dq(MON, [("fresh_a", "pass", "treasury", None), ("fresh_b", "warn", "ecb", None)])
            w.dq(MON, [("range_spy", "fail", "alpaca", "SPY")], check_type="validity")
            w.dq(FRI, [("fresh_a", "pass", "treasury", None)])
            w.dq(THU, [("fresh_a", "fail", "treasury", None)])
            w._append("crosscheck_results", [
                xcheck_rows(MON, FRI, "warn", 6.5, "alpaca", "yahoo", "spy_vs_es", warn=7.0),
                xcheck_rows(MON, FRI, "skip", None, "yahoo", "ecb", "skipped_check"),
                xcheck_rows(MON, FRI, "pass", 1.0, "yahoo", "ecb", "es_vs_yahoo"),
                xcheck_rows(FRI, THU, "pass", 0.5, "yahoo", "ecb", "es_vs_yahoo"),
            ], lambda r: r["target_date"])  # fmt: skip
            w.fetches(MON, [("yahoo", "ok"), ("yahoo", "error"), ("treasury", "ok")])
            put(w.store, "scorecard_daily", [{
                "source": "yahoo", "date": FRI, "fetches": 4, "ok_rate": 1.0,
                "median_latency_ms": 300.0, "p95_latency_ms": 900.0, "median_data_age_s": 60.0,
                "error_rate": 0.0, "disagreement_rate": None, "n_crosschecks": 0,
            }], "RUNSC", FRI)  # fmt: skip
            w.run(MON)
            w.run(FRI, status="degraded")
            w._append("revisions", [{
                "instrument_id": "UST_10Y", "source": "treasury", "dataset": "par_curve",
                "field": "yield", "interval": "1d", "as_of": et(FRI), "old_value": 4.39,
                "new_value": 4.40, "old_fetch_id": "F1", "new_fetch_id": "F2",
                "detected_at": et(MON, "06:30"), "run_id": "RUN1",
            }, {
                "instrument_id": "SPY", "source": "alpaca", "dataset": "iex_latest",
                "field": "close", "interval": "1d", "as_of": et(FRI), "old_value": 598.0,
                "new_value": 598.5, "old_fetch_id": "F3", "new_fetch_id": "F4",
                "detected_at": et(MON, "06:31"), "run_id": "RUN1",
            }], lambda r: MON)  # fmt: skip
            w._append("quarantine", [{
                "fetch_id": "F9", "run_id": "RUN1", "source": "yahoo", "dataset": "quote",
                "reason": "negative_price", "detail": "synthetic", "raw_key": None,
                "detected_at": et(MON, "06:40"),
            }], lambda r: MON)  # fmt: skip
            from deskbrief.pipeline import serving

            ctx = replace(ctx_for(w.store, "RUNSL", MON), now_utc=MON_0700, config=w.config)
            serving.serving_step(ctx)
        text_cfg = (REPO / "config.yaml").read_text(encoding="utf-8")
        text_cfg = text_cfg.replace("local_root: ./.data", f"local_root: {w.store.root}")
        cfg = w.store.root.parent / "config.yaml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(text_cfg, encoding="utf-8")
        monkeypatch.setenv("DESKBRIEF_CONFIG", str(cfg))
        if mode is not None:
            monkeypatch.setenv("DESKBRIEF_DISPLAY_MODE", mode)
        from app.components import data

        monkeypatch.setattr(data, "current_time", lambda: MON_0700)
        return AppTest.from_file(str(PAGES / PAGE_FILES[page]), default_timeout=60).run()

    return run


def test_every_page_is_smoke_clean_and_empty_store_does_not_crash(run_page: RunPage) -> None:
    for page in PAGE_FILES:
        at = run_page(page)
        assert not at.exception, page
        empty = run_page(page, empty=True)
        assert not empty.exception, page


# --- page 2: instrument ---------------------------------------------------------------------


def pick(at: AppTest, instrument: str) -> AppTest:
    box = at.selectbox(key="instrument")
    return box.select(instrument).run()


def test_instrument_ladder_uses_p009_labels(run_page: RunPage) -> None:
    at = pick(run_page("instrument", "local"), "UST_10Y")
    t = text(at)
    assert "yield" in t
    assert "close" not in t.replace("previous close", "")  # a yield is not a close
    t = text(pick(run_page("instrument", "local"), "EURUSD"))
    assert "ECB reference rate" in t


def test_instrument_hosted_never_shows_tier1(run_page: RunPage) -> None:
    at = run_page("instrument", "hosted")
    box = at.selectbox(key="instrument")
    assert box.options  # SPY still has delayed/official data
    t = text(pick(at, "SPY"))
    assert "alpaca" not in t
    assert "600.0000" not in t
    assert 'title="live"' not in t


def test_instrument_local_shows_three_tier_ladder_and_lineage(run_page: RunPage) -> None:
    t = text(pick(run_page("instrument", "local"), "SPY"))
    assert "alpaca" in t
    assert "live" in t
    assert "Lineage" in t or "sha256" in t
    assert "Check history" in t


# --- page 3: cross-checks -------------------------------------------------------------------


def test_crosscheck_strip_is_labelled_by_ref_date_never_target_date(run_page: RunPage) -> None:
    t = text(run_page("xchecks", "local"))
    assert "13 Nov" in t  # ref_date of the Monday run (Friday's data)
    assert 'aria-label="16 Nov' not in t  # target_date is never a label
    assert 'aria-label="13 Nov warn' in t


def test_crosscheck_hosted_withholds_rows_naming_a_tier1_source(run_page: RunPage) -> None:
    hosted = text(run_page("xchecks", "hosted"))
    assert "spy_vs_es" not in hosted
    assert "es_vs_yahoo" in hosted
    assert "withheld" in hosted
    assert "spy_vs_es" in text(run_page("xchecks", "local"))


def test_crosscheck_gap_chart_shows_tolerance_band(run_page: RunPage) -> None:
    at = run_page("xchecks", "local")
    at = at.selectbox(key="check").select("sp500 · spy_vs_es").run()
    t = text(at)
    assert "<polyline" in t
    assert "bps" in t
    assert "warn" in t


def test_instrument_hosted_history_and_tolerance_exclude_tier1(run_page: RunPage) -> None:
    hosted = text(pick(run_page("instrument", "hosted"), "SPY"))
    assert "590.25" not in hosted  # the tier-1 daily value in the 30-day window
    assert "warn 7" not in hosted  # tolerance of the check that names alpaca
    assert "warn 5 bp" in hosted
    local = text(pick(run_page("instrument", "local"), "SPY"))
    assert "590.25" in local


def test_skipped_crosscheck_has_no_nan_in_strip_or_chart(run_page: RunPage) -> None:
    at = run_page("xchecks", "local")
    at = at.selectbox(key="check").select("sp500 · skipped_check").run()
    t = text(at)
    assert not at.exception
    assert "nan" not in t.lower().replace("financial", "")
    assert "no data in this window" in t
    assert "no gap computed" in text(run_page("xchecks", "local"))
