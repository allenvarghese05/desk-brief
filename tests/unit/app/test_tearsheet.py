# ruff: noqa: RUF001  (minus, en dash and sigma are intentional output)
"""Workspace data rules (T36 review round 1): daily bars, tearsheet statistics, the hosted-mode
filters of the instrument panels, the yield curve's provenance and the shared connection.

SYNTHETIC FIXTURE: values come from ``brief_synth`` (Monday 16 Nov 2026, 07:00 ET) plus the
made-up rows written below. Nothing here is market data.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from brief_synth import MON, MON_0700, World, daily, et, monday_rows
from deskbrief.config import load_config
from deskbrief.report.model import Row

Run = Callable[..., AppTest]
YH = {"source": "yahoo", "dataset": "history", "unit": "USD", "tier": 2}


def page(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    return "\n".join(parts + [e.label for e in at.expander])


def select(at: AppTest, iid: str) -> AppTest:
    at.query_params["id"] = iid
    return at.run()


def bars_for(run_home: Run, rows: list, iid: str, hidden: frozenset[int] = frozenset()):  # type: ignore[no-untyped-def,type-arg]
    from app.components import data

    run_home("local", rows=rows)
    config = load_config(os.environ["DESKBRIEF_CONFIG"])
    conn = data.open_connection(config, os.environ)
    try:
        return data.build_bars(conn, MON_0700, iid, hidden)
    finally:
        conn.close()


# --- daily bars --------------------------------------------------------------------------------


def test_bars_leave_out_the_current_et_day(run_home: Run) -> None:
    today = daily("SPY", MON, 777.0, **YH, field="close", ingested_at=MON_0700 - timedelta(hours=1))
    bars = bars_for(run_home, [*monday_rows(), today], "SPY")
    assert 777.0 not in bars["close"].tolist()  # still forming, so never a bar
    assert bars["d"].max().date() < MON


def test_bars_are_dated_in_new_york_not_utc(run_home: Run) -> None:
    late = date(2026, 11, 4)  # 22:00 ET is 03:00 UTC the next day
    row = {**daily("DAX", late, 123.0, **YH, field="close"), "as_of": et(late, "22:00")}
    bars = bars_for(run_home, [*monday_rows(), row], "DAX")
    hit = bars[bars["close"] == 123.0]
    assert hit["d"].dt.date.tolist() == [late]


def test_open_high_low_only_come_from_the_close_source(run_home: Run) -> None:
    d1, d2 = date(2026, 11, 5), date(2026, 11, 6)
    other = {**YH, "source": "stooq", "dataset": "daily"}
    ohl1 = (("open", 90.0), ("high", 110.0), ("low", 80.0))
    ohl2 = (("open", 99.0), ("high", 102.0), ("low", 98.0))
    rows = [
        daily("WTI", d1, 100.0, **YH, field="close"),
        *(daily("WTI", d1, v, **other, field=f) for f, v in ohl1),
        daily("WTI", d2, 101.0, **YH, field="close"),
        *(daily("WTI", d2, v, **YH, field=f) for f, v in ohl2),
    ]  # fmt: skip
    bars = bars_for(run_home, [*monday_rows(), *rows], "WTI").set_index("close")
    assert math.isnan(bars.loc[100.0, "open"])  # stooq's open is not stitched onto yahoo's close
    assert bars.loc[101.0, "open"] == 99.0
    assert bars.loc[101.0, "source"] == "yahoo"


def test_hosted_bars_never_read_a_tier1_only_day(run_home: Run) -> None:
    only_t1 = date(2026, 11, 9)  # no other SPY row on this day
    t1 = daily("SPY", only_t1, 590.25, tier=1, source="alpaca", dataset="iex_latest")
    local = bars_for(run_home, [*monday_rows(), t1], "SPY")
    hosted = bars_for(run_home, [*monday_rows(), t1], "SPY", frozenset({1}))
    assert 590.25 in local["close"].tolist()  # the day exists when tier 1 may be shown
    assert 590.25 not in hosted["close"].tolist()
    assert set(hosted["tier"]) <= {2, 3}


# --- tearsheet statistics ----------------------------------------------------------------------


def _bars(closes: list[float], end: date = date(2026, 11, 13)) -> pd.DataFrame:
    days = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    n = len(closes)
    return pd.DataFrame({
        "d": days, "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1.0] * n, "tier": [2] * n, "source": ["yahoo"] * n,
        "field": ["close"] * n, "unit": ["USD"] * n,
    })  # fmt: skip


def _row() -> Row:
    return Row("X", "Test", "test (close)", "equity", False, None, True, "1.00", "", "", None, "",
               2, "◐", "delayed", None)  # fmt: skip


def test_horizons_measure_from_the_close_on_or_before_the_start() -> None:
    from app.components import tearsheet

    closes = [100.0 + i for i in range(30)]  # 30 business days ending Fri 13 Nov
    items = {h.label: h.html for h in tearsheet.horizons(_row(), _bars(closes))}
    # 1W: last close 129 vs the close on or before 6 Nov (the 25th close, 124)
    assert "+4.0%" in items["1W"]
    assert "needs" in items["3M"]  # history starts in October: never extrapolated
    assert "needs" in items["1Y"]
    assert "db-src" in items["1W"]  # each figure carries its provenance


def test_risk_volatility_matches_the_formula() -> None:
    from app.components import tearsheet

    closes = [100.0, 101.0] * 13
    html = tearsheet.risk_html(_row(), _bars(closes))
    moves = pd.Series(closes).apply(math.log).diff().dropna() * 100
    vol = float(moves.tail(20).std()) * math.sqrt(252)
    assert f"{vol:.1f}%" in html
    assert "Max drawdown" in html
    short = tearsheet.risk_html(_row(), _bars(closes[:8]))
    assert "needs 21d" in short


# --- T58: statistics that respect data gaps ------------------------------------------------


def _gapped(before: list[float], after: list[float], gap_from: date, gap_to: date) -> pd.DataFrame:
    """Business-day closes up to ``gap_from``, nothing in between, then from ``gap_to``."""
    a = pd.bdate_range(end=pd.Timestamp(gap_from), periods=len(before))
    b = pd.bdate_range(start=pd.Timestamp(gap_to), periods=len(after))
    days = list(a) + list(b)
    closes = before + after
    n = len(closes)
    return pd.DataFrame({
        "d": pd.to_datetime(days), "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": [1.0] * n, "tier": [2] * n, "source": ["yahoo"] * n,
        "field": ["close"] * n, "unit": ["USD"] * n,
    })  # fmt: skip


# SYNTHETIC FIXTURE: 40 closes to Thu 1 Oct 2026, a hole of 16 business days, then 18 closes
# from Mon 26 Oct to Wed 18 Nov. The price halves across the hole.
GAP_FROM, GAP_TO = date(2026, 10, 1), date(2026, 10, 26)
BEFORE = [200.0 + (i % 2) for i in range(40)]
AFTER = [100.0 + (i % 3) * 0.5 for i in range(18)]


def test_gaps_are_found_by_business_days() -> None:
    from app.components import tearsheet

    bars = _gapped(BEFORE, AFTER, GAP_FROM, GAP_TO)
    gaps = tearsheet.gaps(bars)
    assert [(g.before, g.after) for g in gaps] == [(GAP_FROM, GAP_TO)]
    assert gaps[0].bdays > tearsheet.GAP_BDAYS
    assert tearsheet.gaps(_bars([1.0] * 30)) == []  # weekends and holidays are not gaps


def test_horizons_refuse_a_base_close_older_than_the_horizon_allows() -> None:
    from app.components import tearsheet

    bars = _gapped(BEFORE, AFTER, GAP_FROM, GAP_TO)
    items = {h.label: h.html for h in tearsheet.horizons(_row(), bars)}
    # 1M from 18 Nov starts on 19 Oct: the closest earlier close (1 Oct) is inside the hole
    assert "no data" in items["1M"]
    assert "%" not in re.sub(r'<span class="db-sr">.*?</span>', "", items["1M"])  # no change
    assert "01 Oct 2026" in items["1M"]  # says which close was too old (for screen readers)
    assert "%" in items["1W"]  # 1W starts on 11 Nov, after the hole: a real change
    assert "%" in items["3M"]  # 3M starts in August, before the hole, with a close that day


def test_volatility_and_sigma_skip_moves_across_a_gap() -> None:
    from app.components import tearsheet

    bars = _gapped(BEFORE, AFTER, GAP_FROM, GAP_TO)
    html = tearsheet.risk_html(_row(), bars)
    closes = pd.Series(BEFORE + AFTER)
    logs = closes.apply(math.log).diff()
    logs.iloc[len(BEFORE)] = float("nan")  # the move across the hole
    want = float(logs.dropna().tail(20).std() * 100) * math.sqrt(252)
    assert f"{want:.1f}%" in html
    naive = float(logs.fillna(math.log(0.5)).tail(20).std() * 100) * math.sqrt(252)
    assert f"{naive:.1f}%" not in html  # the halving across the hole is not a daily move
    assert "skips the gap in the data longer than 5 business days" in html
    assert "01 Oct – 26 Oct" in html


def test_drawdown_does_not_cross_a_gap() -> None:
    from app.components import tearsheet

    bars = _gapped(BEFORE, AFTER, GAP_FROM, GAP_TO)
    html = tearsheet.risk_html(_row(), bars)
    m = re.search(r"Max drawdown[^<]*</span><b>.*?db-val\">([^<]+)<", html, flags=re.S)
    assert m is not None
    assert m.group(1) != "−50.2%"  # never the fall across the hole
    # inside each side: 101 to 100 after the hole is the deepest fall
    assert m.group(1) == "−1.0%"


def test_latest_move_right_after_a_gap_is_no_data() -> None:
    from app.components import tearsheet

    bars = _gapped(BEFORE[:30], [100.0], GAP_FROM, GAP_TO)
    html = tearsheet.risk_html(_row(), bars)
    latest = html.split("<span>Latest move</span>", 1)[1].split("</div>", 1)[0]
    assert "no data" in latest
    assert "σ" not in latest


def test_date_spans_name_the_year_when_they_cross_one() -> None:
    from app.components import tearsheet

    assert tearsheet.date_span(date(2025, 12, 22), date(2026, 1, 9)) == "22 Dec 2025 – 09 Jan 2026"
    assert tearsheet.date_span(date(2026, 10, 1), date(2026, 10, 26)) == "01 Oct – 26 Oct"
    bars = _bars([100.0, 101.0] * 13, end=date(2026, 1, 9))  # 26 closes from mid-December
    html = tearsheet.risk_html(_row(), bars)
    assert re.search(r"\d{2} Dec 2025 – \d{2} Jan 2026", html)


# --- hosted filters of the instrument panels ---------------------------------------------------


def test_hosted_withholds_crosschecks_naming_a_tier1_source(run_home: Run) -> None:
    def extra(w: World) -> None:  # left source alpaca has a tier-1 feed
        w.xchecks(MON, [("spy_vs_es_agree", "pass", 1.0)])

    hosted = page(select(run_home("hosted", extra=extra), "SPY"))
    local = page(select(run_home("local", extra=extra), "SPY"))
    assert "spy vs es agree" not in hosted
    assert "No cross-checks in the last 14 days." in hosted
    assert "spy vs es agree" in local


def test_hosted_withholds_data_checks_naming_a_tier1_source(run_home: Run) -> None:
    def extra(w: World) -> None:
        w.dq(MON, [("range_spy_t1", "fail", "alpaca", "SPY")], check_type="validity")

    panel_row = 'range_spy_t1<span class="db-note">'  # a row of the Data checks panel
    assert panel_row not in page(select(run_home("hosted", extra=extra), "SPY"))
    assert panel_row in page(select(run_home("local", extra=extra), "SPY"))


def test_hosted_corrections_never_read_tier1(run_home: Run) -> None:
    d = date(2026, 11, 10)
    kw = {"tier": 1, "source": "alpaca", "dataset": "iex_latest"}
    rows = [
        daily("SPY", d, 591.0, **kw, ingested_at=MON_0700 - timedelta(hours=40)),
        daily("SPY", d, 592.0, **kw, ingested_at=MON_0700 - timedelta(hours=20)),
    ]
    hosted = page(select(run_home("hosted", rows=[*monday_rows(), *rows]), "SPY"))
    local = page(select(run_home("local", rows=[*monday_rows(), *rows]), "SPY"))
    assert "592.00" not in hosted
    assert "2 versions" not in hosted
    assert "2 versions" in local


# --- yield curve and shared connection ---------------------------------------------------------


def test_yield_curve_moved_to_macro_with_provenance(run_home: Run) -> None:
    """T58 inventory: the yield curve left Home for Macro (linked from Home's right rail)."""
    from brief_synth import FRI

    ust = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
    rows = [*monday_rows(), daily("UST_10Y", FRI, 4.4, **ust), daily("UST_30Y", FRI, 4.6, **ust)]
    at = run_home("hosted", rows=rows)
    assert '<svg class="db-curve"' not in page(at)
    at.switch_page("pages/2_Macro.py").run()
    assert not at.exception
    text = page(at)
    assert "Treasury curve" in text
    cells = re.findall(r'<span class="db-fig".*?</span></span>', text, flags=re.S)
    assert cells
    for cell in cells:
        assert "db-src" in cell


def test_shared_connection_is_reused_then_replaced_and_closed(
    run_home: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.components import data

    run_home("local")
    config = load_config(os.environ["DESKBRIEF_CONFIG"])
    clock = [time.monotonic() + 1e6]  # later than any stamp the app run left
    monkeypatch.setattr(data.time, "monotonic", lambda: clock[0])
    with data.shared_connection(config) as first:
        assert first.execute("SELECT 1").fetchone() == (1,)
    with data.shared_connection(config) as again:
        assert again is first  # within the minute: same connection
    # T56: rebuilt only when the listing changed, by a background check (old one serves)
    World(Path(config.storage.local_root).parent).observations(
        [daily("SPY", date(2026, 11, 2), 1.0, **YH)]
    )
    clock[0] += data.CACHE_TTL_SECONDS + 1
    with data.shared_connection(config) as during:
        assert during is first  # the check that starts here never blocks this query
    assert data.wait_for_refresh(config)
    with data.shared_connection(config) as fresh:
        assert fresh is not first
        assert fresh.execute("SELECT 1").fetchone() == (1,)
    with pytest.raises(Exception):  # noqa: B017, PT011 (duckdb raises its own closed error)
        first.execute("SELECT 1")


def test_shared_connection_skips_one_closed_between_lookup_and_lock(
    run_home: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that looked up a connection just before another replaced and closed it must
    retry and get the new one, never run a query on the closed one."""
    import threading

    from app.components import data

    run_home("local")
    config = load_config(os.environ["DESKBRIEF_CONFIG"])
    stale = data._Shared(data.open_connection(config, os.environ), threading.Lock(), 0.0)
    stale.closed = True
    stale.conn.close()
    good = data._Shared(data.open_connection(config, os.environ), threading.Lock(), 0.0)
    handed = iter([stale, good])
    monkeypatch.setattr(data, "_shared", lambda *_a: next(handed))
    with data.shared_connection(config) as conn:
        assert conn is good.conn
        assert conn.execute("SELECT 1").fetchone() == (1,)
    good.conn.close()
