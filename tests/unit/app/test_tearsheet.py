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

    closes = [100.0 * (1.01 if i % 2 else 0.99) ** (i % 2) for i in range(25)]
    closes = [100.0, 101.0] * 13
    html = tearsheet.risk_html(_row(), _bars(closes))
    moves = pd.Series(closes).apply(math.log).diff().dropna() * 100
    vol = float(moves.tail(20).std()) * math.sqrt(252)
    assert f"{vol:.1f}%" in html
    assert "Max drawdown" in html
    short = tearsheet.risk_html(_row(), _bars(closes[:8]))
    assert "needs 21d" in short


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


def test_yield_curve_figures_carry_provenance(run_home: Run) -> None:
    from brief_synth import FRI

    ust = {"source": "treasury", "dataset": "par_curve", "field": "yield", "unit": "pct"}
    rows = [*monday_rows(), daily("UST_10Y", FRI, 4.4, **ust), daily("UST_30Y", FRI, 4.6, **ust)]
    text = page(run_home("hosted", rows=rows))
    svg = re.search(r'<svg class="db-curve".*?</svg>', text, flags=re.S)
    assert svg is not None
    assert "4.40" not in svg.group(0)  # no figure printed inside the chart
    assert "4.6" not in svg.group(0).split("aria-label", 1)[1].split('"', 2)[1]
    strip = text.split('<div class="db-tenors">', 1)[1].split("</div></div>\n", 1)[0]
    cells = re.findall(r'<span class="db-fig".*?</span></span>', strip, flags=re.S)
    assert cells
    for cell in cells:
        assert "db-src" in cell
        assert 'db-field">yield<' in cell


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
    clock[0] += data.CACHE_TTL_SECONDS + 1
    with data.shared_connection(config) as fresh:
        assert fresh is not first
        assert fresh.execute("SELECT 1").fetchone() == (1,)
    with pytest.raises(Exception):  # noqa: B017, PT011 (duckdb raises its own closed error)
        first.execute("SELECT 1")
