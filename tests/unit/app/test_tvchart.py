"""The TradingView chart component (T57B, P-022).

Checks the vendored bundle against ``SHA256SUMS``, the payload the frontend receives (business
days, sorted, deduplicated, no NaN, provenance on every point), the attribution on every
chart, the Altair fallback, and the static folder's rules: no network URLs and no ``title``
attributes.

SYNTHETIC FIXTURE: every bar below is made up.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from conftest import REPO

FRONT = REPO / "app" / "components" / "tvchart_frontend"
LIB = FRONT / "lightweight-charts.standalone.production.js"
T57B_FILES = [
    FRONT / "index.html",
    FRONT / "tvchart.js",
    REPO / "app" / "components" / "tvchart.py",
    REPO / "app" / "components" / "chart.py",
]
_TITLE_ATTR = re.compile(r"""\btitle\s*=\s*["'{]|setAttribute\(\s*["']title["']|\.title\s*=""")


def bars(days: list[date], **cols: list[Any]) -> pd.DataFrame:
    n = len(days)
    base: dict[str, Any] = {
        "d": pd.to_datetime(days),
        "open": [100.0 + i for i in range(n)],
        "high": [102.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "close": [101.0 + i for i in range(n)],
        "volume": [1_000.0 * (i + 1) for i in range(n)],
        "tier": [2] * n,
        "source": ["yahoo"] * n,
        "field": ["close"] * n,
        "unit": ["usd"] * n,
    }
    base.update(cols)
    return pd.DataFrame(base)


# A Thursday, Friday, Monday and Tuesday: a weekend sits between the second and third bar.
DAYS = [date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17)]


# ---- the vendored bundle -----------------------------------------------------------------
def test_vendored_bundle_matches_sha256sums() -> None:
    from app.components import tvchart

    sums = (FRONT / "SHA256SUMS").read_text(encoding="utf-8")
    want = next(r.split()[0] for r in sums.splitlines() if r.endswith(LIB.name))
    assert hashlib.sha256(LIB.read_bytes()).hexdigest() == want
    assert tvchart.library_ok()


def test_license_and_notice_present_and_name_the_pinned_version() -> None:
    from app.components import tvchart

    lic = (FRONT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in lic
    assert "Version 2.0" in lic
    notice = (FRONT / "NOTICE").read_text(encoding="utf-8")
    sha = hashlib.sha256(LIB.read_bytes()).hexdigest()
    assert tvchart.LIBRARY_VERSION in notice
    assert f"lightweight-charts-{tvchart.LIBRARY_VERSION}.tgz" in notice
    assert "sha512-" in notice  # the npm integrity value the tarball was checked against
    assert sha in notice
    assert "TradingView" in notice  # the library's own NOTICE text is carried


def test_bundle_is_the_pinned_version() -> None:
    from app.components import tvchart

    head = LIB.read_text(encoding="utf-8", errors="replace")[:600]
    assert f"v{tvchart.LIBRARY_VERSION}" in head


def test_a_wrong_checksum_disables_the_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.components import tvchart

    (tmp_path / LIB.name).write_bytes(LIB.read_bytes() + b"\n// tampered")
    (tmp_path / "SHA256SUMS").write_text((FRONT / "SHA256SUMS").read_text(), encoding="utf-8")
    monkeypatch.setattr(tvchart, "FRONTEND", tmp_path)
    tvchart.library_ok.cache_clear()
    tvchart._component.cache_clear()
    try:
        assert not tvchart.library_ok()
        assert tvchart._component() is None
    finally:
        monkeypatch.undo()
        tvchart.library_ok.cache_clear()
        tvchart._component.cache_clear()


# ---- the static folder -------------------------------------------------------------------
@pytest.mark.parametrize("name", ["index.html", "tvchart.js"])
def test_no_network_urls_in_the_frontend(name: str) -> None:
    text = (FRONT / name).read_text(encoding="utf-8")
    assert not re.search(r"(?i)\b(?:https?|wss?|ftp):", text)
    assert not re.search(r"""(?:src|href)\s*=\s*["']//""", text)  # protocol-relative
    srcs = re.findall(r"""<script[^>]*\bsrc\s*=\s*["']([^"']+)["']""", text)
    assert all("/" not in s for s in srcs)  # only files of this folder


def test_index_loads_the_bundle_and_glue_from_this_folder() -> None:
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    srcs = re.findall(r"""<script[^>]*\bsrc\s*=\s*["']([^"']+)["']""", html)
    assert srcs == [LIB.name, "tvchart.js"]


@pytest.mark.parametrize("path", T57B_FILES[:2], ids=lambda p: p.name)
def test_no_title_attributes_in_the_frontend(path: Path) -> None:
    assert not _TITLE_ATTR.search(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", T57B_FILES[2:], ids=lambda p: p.name)
def test_no_title_attributes_in_generated_markup(path: Path) -> None:
    """Markup strings in the Python modules (Altair's ``title=`` keyword names a tooltip
    field in a Vega spec, not a markup attribute)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "<" in node.value:
            assert not re.search(r"\btitle\s*=", node.value), node.value


def test_glue_speaks_the_component_protocol_and_hard_codes_no_colour() -> None:
    js = (FRONT / "tvchart.js").read_text(encoding="utf-8")
    for msg in ("streamlit:componentReady", "streamlit:render", "streamlit:setFrameHeight"):
        assert msg in js
    assert not re.search(r"#[0-9A-Fa-f]{6}\b", js)  # colours come from app/theme.py


# ---- payload -----------------------------------------------------------------------------
def test_points_are_business_days_sorted_deduplicated_and_finite() -> None:
    from app.components import tvchart

    frame = pd.DataFrame(
        {
            "d": pd.to_datetime(["2026-11-16", "2026-11-12", "2026-11-13", "2026-11-16",
                                 "2026-11-17", "2026-11-18"]),
            "value": [3.0, 1.0, float("nan"), 4.0, float("inf"), 6.0],
            "field": "close", "tier_text": "◐ delayed", "source": "yahoo",
        }
    )  # fmt: skip
    pts, meta = tvchart.points(frame, "line")
    assert [p["time"] for p in pts] == ["2026-11-12", "2026-11-16", "2026-11-18"]
    assert [p["value"] for p in pts] == [1.0, 4.0, 6.0]  # repeated date keeps its last row
    assert meta == [["close", "◐ delayed", "yahoo"]]
    assert all(p["m"] == 0 for p in pts)
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", p["time"]) for p in pts)


def test_business_day_accepts_dates_and_timestamps() -> None:
    from app.components import tvchart

    assert tvchart.business_day(date(2025, 1, 2)) == "2025-01-02"
    assert tvchart.business_day(pd.Timestamp("2025-01-02 16:00")) == "2025-01-02"


def _strict_json(obj: Any) -> str:
    return json.dumps(obj, allow_nan=False)  # raises on NaN or infinity


def test_candle_payload() -> None:
    from app import theme
    from app.components import chart

    days = list(pd.bdate_range("2026-11-02", "2026-11-13").date)  # ten weekdays
    b = bars(days)
    b.loc[3, "low"] = 500.0  # the low sits above the close: not a candle
    b.loc[6, "open"] = float("nan")  # no open: not a candle
    b = pd.concat([b, b.iloc[[9]].assign(close=200.0, high=201.0)], ignore_index=True)  # repeat
    series = chart.tv_series(b, style=chart.CANDLES)
    assert [s["kind"] for s in series] == ["candles", "volume"]
    candles = series[0]
    pts = candles["points"]
    assert [p["time"] for p in pts] == [f"{d:%Y-%m-%d}" for d in days]
    assert [p["ohlc"] for p in pts] == [i not in (3, 6) for i in range(10)]
    bad = pts[3]
    assert bad["open"] == bad["high"] == bad["low"] == bad["close"] == 104.0
    assert bad["color"] == theme.STALE  # a grey mark at the close, never a made-up candle
    assert "color" not in pts[0]
    assert pts[-1]["close"] == 200.0  # a repeated date keeps its last row
    assert candles["meta"] == [["close", "◐ delayed", "yahoo"]]
    vol = series[1]
    assert vol["meta"] == [["volume (close bar)", "◐ delayed", "yahoo"]]
    assert all(isinstance(p["up"], bool) for p in vol["points"])
    _strict_json(series)


def test_line_and_area_payload_when_no_ohlc() -> None:
    from app.components import chart

    b = bars(DAYS, open=[None] * 4, high=[None] * 4, low=[None] * 4, volume=[None] * 4)
    for style, kind in ((chart.CANDLES, "line"), (chart.LINE, "line"), (chart.AREA, "area")):
        series = chart.tv_series(b, style=style)
        assert [s["kind"] for s in series] == [kind]
        assert [p["value"] for p in series[0]["points"]] == [101.0, 102.0, 103.0, 104.0]
        _strict_json(series)


def test_compare_payload_is_rebased_on_common_dates() -> None:
    from app import theme
    from app.components import chart

    a = bars(DAYS)
    b = bars(DAYS[1:], close=[50.0, 55.0, 60.0], source=["stooq"] * 3, tier=[3] * 3)
    series = chart.tv_compare_series(a, b, "SPY", "QQQ")
    assert [s["name"] for s in series] == ["SPY", "QQQ"]
    assert [s["kind"] for s in series] == ["line", "line"]
    assert series[0]["color"] == theme.ACCENT
    assert not series[0]["dashed"]
    assert series[1]["color"] == theme.AMBER
    assert series[1]["dashed"]
    assert [p["time"] for p in series[1]["points"]] == ["2026-11-13", "2026-11-16", "2026-11-17"]
    assert series[1]["points"][0]["value"] == 100.0
    assert series[1]["points"][2]["value"] == pytest.approx(120.0)
    assert series[1]["meta"][0][1:] == ["○ official", "stooq"]
    assert "rebased to 100 on 13 Nov 2026" in series[1]["meta"][0][0]
    _strict_json(series)


def test_spec_carries_theme_tokens_and_a_finite_last() -> None:
    from app import theme
    from app.components import chart, tvchart

    s = tvchart.spec(chart.tv_series(bars(DAYS), style=chart.LINE), last=float("nan"))
    assert s["last"] is None
    t = s["theme"]
    assert (t["page"], t["panel"], t["divider"]) == ("#0B0D10", "#13161B", "#22262E")
    assert (t["text"], t["secondary"], t["meta"]) == ("#E8EAED", "#9AA0A6", "#8A9099")
    assert (t["up"], t["down"], t["accent"]) == (theme.UP, theme.DOWN, theme.ACCENT)
    assert tvchart.spec([], last=104.5)["last"] == 104.5
    _strict_json(s)


def test_two_years_of_weekdays_have_no_weekend_points() -> None:
    from app.components import chart

    days = list(pd.bdate_range("2024-11-19", "2026-11-19").date)
    series = chart.tv_series(bars(days), style=chart.CANDLES)
    times = [p["time"] for p in series[0]["points"]]
    assert len(times) == len(days)
    assert all(pd.Timestamp(t).day_of_week < 5 for t in times)
    assert times == sorted(times)
    assert times[0][:4] == "2024"
    assert times[-1][:4] == "2026"


def test_precision() -> None:
    from app.components import chart

    assert chart.tv_precision(bars(DAYS)) == 2
    assert chart.tv_precision(bars(DAYS, close=[1.08, 1.09, 1.1, 1.1])) == 4
    assert chart.tv_precision(bars(DAYS, unit=["pct"] * 4, close=[4.1] * 4)) == 2


# ---- rendering: attribution and fallback -------------------------------------------------
def _script() -> None:
    import sys
    from datetime import date as _date

    import pandas as _pd

    sys.path.insert(0, _REPO)  # type: ignore[name-defined]  # noqa: F821
    from app.components import chart

    days = list(_pd.bdate_range(_date(2026, 9, 1), _date(2026, 11, 19)).date)
    n = len(days)
    b = _pd.DataFrame(
        {
            "d": _pd.to_datetime(days), "open": [100.0] * n, "high": [101.0] * n,
            "low": [99.0] * n, "close": [100.5] * n, "volume": [1e3] * n, "tier": [2] * n,
            "source": ["yahoo"] * n, "field": ["close"] * n, "unit": ["usd"] * n,
        }
    )  # fmt: skip
    chart.price_panel(b, name="SPY", last=100.7, key="t")


def _run() -> AppTest:
    import inspect
    import textwrap

    body = textwrap.dedent(inspect.getsource(_script)).split("\n", 1)[1]
    code = f"_REPO = {str(REPO)!r}\n" + textwrap.dedent(body)
    at = AppTest.from_string(code, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return at


def _markdown(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_every_chart_carries_the_attribution() -> None:
    from app.components import tvchart

    at = _run()
    md = _markdown(at)
    assert "Charts by <a " in md
    assert f'href="{tvchart.ATTRIBUTION_URL}"' in md
    assert ">TradingView</a>" in md
    assert not _TITLE_ATTR.search(md)
    assert tvchart.NOTE_FALLBACK not in "\n".join(c.value for c in at.caption)


def test_fallback_draws_an_altair_line_with_a_note(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.components import tvchart

    monkeypatch.setattr(tvchart, "_component", lambda: None)  # e.g. the bundle failed its checksum
    at = _run()
    captions = "\n".join(c.value for c in at.caption)
    assert tvchart.NOTE_FALLBACK in captions
    assert at.get("vega_lite_chart"), "no Altair fallback chart drawn"


def test_a_frontend_error_switches_to_the_fallback() -> None:
    from app.components import tvchart

    at = _run()
    at.session_state["t-chart"] = {"status": "error", "message": "boom"}
    at.run()
    assert tvchart.NOTE_FALLBACK in "\n".join(c.value for c in at.caption)


def test_tvchart_chart_is_the_only_way_to_draw_the_component() -> None:
    """Attribution lives inside ``tvchart.chart``, so no module may call the raw component
    (or declare its own) and skip it."""
    for path in (REPO / "app").rglob("*.py"):
        if path.name == "tvchart.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"_component", "declare_component"}, path
