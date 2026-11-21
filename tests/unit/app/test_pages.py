"""Dashboard pages 2-6 (T35; the P-022 template since T59): AppTest smoke per page, hosted
rule, P-009 labels, ref_date labels, and the T59 template, markup and cache rules. The
Instrument page moved into Home in T36; its tests are in test_home. Macro's own views are in
test_macro.

SYNTHETIC FIXTURE: a store built from ``brief_synth`` (made-up values, Monday 16 Nov 2026 07:00
ET) plus serving files built by ``deskbrief.pipeline.serving``. SPY is the only tier-1 instrument
and ``alpaca`` the only source with a tier-1 feed. No R2, no network.
"""

from __future__ import annotations

import itertools
import json
import re
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
    "macro": "2_Macro.py",
    "xchecks": "3_Cross_checks.py",
    "health": "4_Data_health.py",
    "sources": "5_Sources.py",
    "runs": "6_Runs.py",
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
                [daily("SPY", THU, 590.25, tier=1, source="alpaca", dataset="iex_latest"),
                 # T59 review: the fetch behind the fallback correction below
                 daily("SPY", THU, 590.10, tier=2, source="yahoo", dataset="iex_delayed",
                       is_fallback=True, fetch_id="F6")]
            )  # fmt: skip
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
            w._append("runs", [{
                "run_id": "RUNLATE", "job": "intraday", "trigger": "schedule", "target_date": MON,
                "scheduled_for": et(MON, "06:50"), "started_at": et(MON, "06:55"),
                "finished_at": et(MON, "06:58"), "status": "ok", "git_sha": "deadbeef",
                "config_hash": "cafe", "n_fetch": 1, "n_fetch_failed": 0, "n_obs": 1,
                "n_dq_fail": 0, "n_dq_warn": 0,
            }], lambda r: MON)  # fmt: skip
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
            }, {
                "instrument_id": "SPY", "source": "yahoo", "dataset": "iex_delayed",
                "field": "close", "interval": "1d", "as_of": et(THU), "old_value": 590.05,
                "new_value": 590.10, "old_fetch_id": "F5", "new_fetch_id": "F6",
                "detected_at": et(MON, "06:32"), "run_id": "RUN1",
            }], lambda r: MON)  # fmt: skip
            w._append("quarantine", [{
                "fetch_id": "F9", "run_id": "RUN1", "source": "yahoo", "dataset": "quote",
                "reason": "negative_price", "detail": "synthetic", "raw_key": None,
                "detected_at": et(MON, "06:40"),
            }, {
                "fetch_id": "F10", "run_id": "RUN1", "source": "alpaca", "dataset": "iex_latest",
                "reason": "tier1_bad_tick", "detail": "synthetic", "raw_key": None,
                "detected_at": et(MON, "06:41"),
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


# --- page 4: data health --------------------------------------------------------------------


def test_data_health_groups_by_type_and_lists_revisions_and_quarantine(run_page: RunPage) -> None:
    t = text(run_page("health", "local"))
    assert "freshness" in t
    assert "validity" in t
    assert "negative_price" in t  # quarantine list
    assert "UST_10Y" in t  # revisions log
    assert "fresh_b" in t


def test_data_health_hosted_withholds_tier1_rows(run_page: RunPage) -> None:
    t = text(run_page("health", "hosted"))
    assert "range_spy" not in t
    assert "alpaca" not in t
    assert "fresh_b" in t
    assert "withheld" in t


def test_data_health_freshness_timeline_per_dataset(run_page: RunPage) -> None:
    t = text(run_page("health", "hosted"))
    assert 'aria-label="12 Nov fail: ' in t  # Thursday's late arrival (T57: detail follows)
    assert "minutes late" in t


# --- page 5: sources ------------------------------------------------------------------------


def test_sources_show_measured_numbers_with_days_of_data(run_page: RunPage) -> None:
    at = run_page("sources", "hosted")
    t = text(at)
    assert "days of data" in t
    df = at.dataframe[0].value
    assert set(df["Source"]) >= {"yahoo", "treasury"}
    row = df[df["Source"] == "yahoo"].iloc[0]
    assert row["Days of data"] == 2
    assert row["Uptime"] == "83.3%"  # fetch-weighted: (1 ok of 2 + 4 ok of 4) / 6


def test_sources_empty_says_so(run_page: RunPage) -> None:
    t = text(run_page("sources", empty=True))
    assert "No scorecard data yet" in t


# --- page 6: runs ---------------------------------------------------------------------------


def test_runs_table_start_delay_and_links(run_page: RunPage) -> None:
    at = run_page("runs", "hosted")
    t = text(at)
    assert "degraded" in t
    assert "ok" in t
    assert "morning" in t
    assert "5 min" in t  # scheduled 06:50, started 06:55
    assert "github.com/allenvarghese05/desk-brief/actions" in t
    # the template block inside the code fence is not an incident
    assert "YYYY-MM-DD" not in t


def test_parse_incidents_skips_the_template_fence() -> None:
    import sys

    sys.path.insert(0, str(REPO))
    from app.components import incidents as _incidents

    md = (
        "# Incidents\n\n```\n## 2026-01-01 — Template example\n- Detected by:\n```\n\n"
        "## 2026-11-20 — Yahoo outage\n- Detected by: freshness\n- Impact: none\n\n"
        "## 2026-11-18 — Late FRED\n- Detected by: sla\n"
    )
    items = _incidents.parse(md)
    assert [i.title for i in items] == ["Yahoo outage", "Late FRED"]
    assert items[0].date == "2026-11-20"
    assert "freshness" in items[0].body
    assert _incidents.parse("# Incidents\n\nnone yet") == []


def test_skipped_crosscheck_has_no_nan_in_strip_or_chart(run_page: RunPage) -> None:
    at = run_page("xchecks", "local")
    at = at.selectbox(key="check").select("sp500 · skipped_check").run()
    t = text(at)
    assert not at.exception
    assert "nan" not in t.lower().replace("financial", "")
    assert "no data in this window" in t
    assert "no gap computed" in text(run_page("xchecks", "local"))


def test_data_health_quarantine_hosted_withholds_tier1_source(run_page: RunPage) -> None:
    hosted = text(run_page("health", "hosted"))
    assert "tier1_bad_tick" not in hosted
    assert "negative_price" in hosted
    assert "withheld" in hosted
    assert "tier1_bad_tick" in text(run_page("health", "local"))


# --- T36 phase 1: headline numbers first ------------------------------------------------------


def test_every_system_page_has_top_bar_navigation(run_page: RunPage) -> None:
    # T57: the page list is st.navigation(position="top") in app/Home.py, which marks the
    # current page itself (aria-current); test_navigation.py drives it
    from app.Home import PAGES as ROUTES

    routed = {file.rsplit("/", 1)[-1] for file, _, _ in ROUTES}
    for page in PAGE_FILES:
        t = text(run_page(page, "hosted"))
        assert PAGE_FILES[page] in routed, page
        assert '<span class="db-brand">' in t, page


def test_crosschecks_put_problems_first_and_fold_skipped(run_page: RunPage) -> None:
    at = run_page("xchecks", "local")
    t = text(at)
    assert "SPY vs ES" in t  # readable name, raw id kept beside it
    # T59: skipped checks are one tab away (depth in tabs), not a <details> fold
    assert [tab.label for tab in at.tabs][:2] == ["Compared (2)", "Skipped (1)"]
    assert "1 skipped check: nothing to compare all week" in t
    assert "<details" not in t
    assert t.index("spy_vs_es") < t.index("es_vs_yahoo")  # warn sorts before pass


def test_data_health_leads_with_failing_count(run_page: RunPage) -> None:
    t = text(run_page("health", "local"))
    assert "Failing" in t
    assert t.index("Failing") < t.index("Freshness, last 7 days")
    assert "Needs attention" in t


def test_sources_leave_out_switched_off_sources(run_page: RunPage) -> None:
    at = run_page("sources", "hosted")
    df = at.dataframe[0].value
    assert "stooq" not in set(df["Source"])
    assert "Median data age" in df.columns


# --- T59: the P-022 template on every page -------------------------------------------------

T59_FILES = sorted((REPO / "app" / "pages").glob("*.py")) + [
    REPO / "app" / "components" / name for name in ("macro.py", "incidents.py", "kit.py")
]
#: kit's serving-file caches keep a 60 s TTL: the serving files are not covered by
#: data_version(), so a 15-minute TTL would show them stale (planner ruling for T59)
SERVING_CACHES = {"_serving_frame", "_serving_json"}
_TITLE_ATTR = re.compile(r"""<[^>]*\stitle\s*=""", flags=re.I)


def markup(at: AppTest) -> str:
    """Every markdown block the page generated (the stylesheets left out)."""
    return "\n".join(m.value for m in at.markdown if "<style>" not in m.value)


def rendered_blocks(markup_text: str) -> int:
    """Blocks the page rendered: the headline row (``db-kpis``), each card (its ``db-ct``
    title; a card's tab notes are ``db-ct-note``), and the page footer (``db-pfoot``)."""
    cards = len(re.findall(r'<div class="db-ct">', markup_text))
    return (
        cards
        + markup_text.count('<div class="db-kpis">')
        + markup_text.count('<div class="db-pfoot">')
    )


@pytest.mark.parametrize("page", list(PAGE_FILES))
def test_every_page_follows_the_card_template(run_page: RunPage, page: str) -> None:
    at = run_page(page, "local")
    assert not at.exception
    t = markup(at)
    # title on the left of the heading, cards keyed db-card-*, the footer last
    assert t.index('<span class="db-brand">') < t.index('class="db-ct"')
    assert t.rindex('class="db-ct"') < t.index('<div class="db-pfoot">')
    footer = t.split('<div class="db-pfoot">', 1)[1]
    assert 'class="db-glyphs"' in footer  # the glyph legend on every page (P-022)
    assert "delayed" in footer
    assert 4 <= rendered_blocks(t) <= 6, (page, rendered_blocks(t))


@pytest.mark.parametrize("page", list(PAGE_FILES))
def test_no_full_reload_link_and_no_title_attribute(run_page: RunPage, page: str) -> None:
    """Acceptance: no ``<a target="_self">`` and no ``title`` attribute in generated markup
    (Streamlit's own component iframe ``title`` is not markdown, so it is not in here)."""
    for mode in ("hosted", "local"):
        t = markup(run_page(page, mode))
        assert 'target="_self"' not in t, (page, mode)
        assert not _TITLE_ATTR.search(t), (page, mode, _TITLE_ATTR.search(t))


@pytest.mark.parametrize("path", T59_FILES, ids=lambda p: p.name)
def test_no_title_attribute_or_self_target_in_t59_sources(path: Path) -> None:
    """Every string literal that is not a docstring (docstrings may name the rule)."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.FunctionDef | ast.ClassDef)
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    strings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
    ]
    assert strings
    for text in strings:
        assert not re.search(r"""\btitle\s*=\s*["'\\{]""", text), text
        assert "_self" not in text, text


@pytest.mark.parametrize("page", list(PAGE_FILES))
def test_hosted_never_shows_tier1_on_any_page(run_page: RunPage, page: str) -> None:
    hosted, local = markup(run_page(page, "hosted")), markup(run_page(page, "local"))
    assert 'data-tier="1"' not in hosted
    legend = hosted.split('class="db-glyphs"', 1)[1].split("</div>", 1)[0]
    assert ">live<" not in legend
    assert ">live<" in local.split('class="db-glyphs"', 1)[1].split("</div>", 1)[0]
    # the footer's mode line names the tiers the page shows (review round 1, blocking 2)
    mode_line = re.compile(r'<div class="db-pfoot"><span class="db-mode">([^<]*)</span>')
    hosted_mode, local_mode = mode_line.search(hosted), mode_line.search(local)
    assert hosted_mode is not None
    assert local_mode is not None
    assert hosted_mode.group(1).startswith("hosted mode · shows ")
    assert "live (tier 1)" not in hosted_mode.group(1)
    assert "delayed (tier 2)" in hosted_mode.group(1)
    assert "live (tier 1)" in local_mode.group(1)


@pytest.mark.parametrize("path", T59_FILES, ids=lambda p: p.name)
def test_every_store_cache_is_keyed_on_the_data_version(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    assert "ttl=60" not in src
    found = re.findall(r"@st\.cache_data\(([^)]*)\)\ndef (\w+)\(([^)]*)\)", src, flags=re.S)
    for ttl, name, args in found:
        assert re.search(r"\bver: str\b", args), (name, args)  # data.data_version()
        if name in SERVING_CACHES:  # the one narrow exemption, by name
            assert path.name == "kit.py"
            assert "data.CACHE_TTL_SECONDS" in ttl
            continue
        assert "DATA_CACHE_TTL_SECONDS" in ttl, (name, ttl)
    if path.name == "4_Data_health.py":
        assert [n for _, n, _ in found] == ["_dq", "_fallback_fetches"]
        assert "open_connection" not in src  # the shared connection (T56)


def _visible(html: str) -> str:
    html = re.sub(r'<span class="db-sr[^"]*"[^>]*>.*?</span>', "", html, flags=re.S)
    html = re.sub(r'<span class="db-tip"[^>]*>.*?</span></span>', "</span>", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def _correction_rows(markup_text: str) -> dict[str, str]:
    """Corrections table rows keyed ``instrument · source``."""
    table = markup_text.split("Instrument, field and source", 1)[1].split("</table>", 1)[0]
    out = {}
    for row in re.findall(r"<tr>.*?</tr>", table, flags=re.S):
        m = re.search(r"<b>([^<]+)</b><span class='db-note'> · [^·]+ · ([^<]+)</span>", row)
        assert m, row
        out[f"{m.group(1)} · {m.group(2)}"] = row
    return out


def test_corrected_values_carry_compact_provenance(run_page: RunPage) -> None:
    """Every corrected value is a compact figure with source, tier, as-of and field, and the
    caveat words by the tear sheet's rule (T59 review round 1, blocking 1)."""
    local = _correction_rows(markup(run_page("health", "local")))
    hosted = _correction_rows(markup(run_page("health", "hosted")))
    assert set(local) == {"UST_10Y · treasury", "SPY · alpaca", "SPY · yahoo"}
    assert set(hosted) == {"UST_10Y · treasury", "SPY · yahoo"}  # tier-1 alpaca withheld
    want = {
        "UST_10Y · treasury": (),
        "SPY · alpaca": ("proxy", "IEX"),  # iex_latest, tier 1 (local only)
        "SPY · yahoo": ("proxy", "IEX", "fallback"),  # iex_delayed from a fallback fetch
    }
    for rows in (local, hosted):
        for key, row in rows.items():
            figs = re.findall(r"<td class='num'>(.*?)</td>", row, flags=re.S)
            assert len(figs) == 2, key  # was and now, each cell one whole figure
            for f in figs:
                assert 'class="db-prov"' in f  # compact: glyph, tooltip and label
                for part in ("db-src", "db-asof", "db-field"):
                    assert part in f
                label = re.search(r'aria-label="([^"]*)"', f).group(1)  # type: ignore[union-attr]
                for word in ("proxy", "IEX", "fallback"):
                    on = word in want[key]
                    assert (re.search(rf"\b{word}\b", _visible(f)) is not None) == on, (key, word)
                    assert (word in label) == on, (key, word, label)
    assert '<span class="db-val">4.39</span>' in local["UST_10Y · treasury"]
    assert 'data-tier="1"' in local["SPY · alpaca"]


def test_crosscheck_gap_is_a_compact_figure(run_page: RunPage) -> None:
    t = markup(run_page("xchecks", "local"))
    gap = next(c for c in re.findall(r'<span class="db-fig">.*?</span></span>', t, flags=re.S)
               if "+6.5 bp" in c)  # fmt: skip
    assert "alpaca vs yahoo" in gap
    assert 'data-tier="1"' in gap  # the most live tier of the two sources compared
    assert "13 Nov" in gap  # the date compared, not the run date


def test_runs_job_filter_narrows_the_table(run_page: RunPage) -> None:
    at = run_page("runs", "hosted")
    assert set(at.dataframe[0].value["Job"]) >= {"morning", "intraday"}
    at = at.segmented_control(key="job").set_value("intraday").run()
    assert not at.exception
    assert set(at.dataframe[0].value["Job"]) == {"intraday"}
    assert "Runs, 14 days" in text(at)  # the headline counts still cover every job


def test_compact_is_the_default_and_keeps_caveat_words() -> None:
    from app.components.values import Provenance, change_cell, value_cell

    p = Provenance("alpaca", 2, "10:27", "last", flags=("proxy", "IEX", "fallback"))
    for html in (value_cell("1.00", p), change_cell("+1.0%", p, "previous close")):
        assert 'class="db-prov' in html  # compact without asking (T59)
        shown = re.sub(r'<span class="db-tip".*?</span></span>', "", html, flags=re.S)
        for word in ("proxy", "IEX", "fallback"):
            assert f'class="db-flag">{word}<' in shown
    assert "db-meta" in value_cell("1.00", p, compact=False)
