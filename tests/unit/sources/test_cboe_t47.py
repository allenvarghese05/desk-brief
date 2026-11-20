# ruff: noqa: PT018
"""Cboe volatility indices (card T47, P-021b): VIX9D, VIX3M, VIX6M, VXN, GVZ and SKEW.

No network. Fixtures under tests/fixtures/synthetic/cboe are SYNTHETIC FIXTURES: the header and
column layout are the ones the live probe saw on 2026-11-19, the values are invented. Store rows
below come from those fixtures through the real parser.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import respx

from deskbrief.checks.freshness import FreshnessCheck
from deskbrief.checks.validity import find_violations
from deskbrief.config import Config, load_config
from deskbrief.models import DQStatus, FetchRequest, JobKind, RawResponse, RunContext, Trigger
from deskbrief.report.model import BriefModel
from deskbrief.schemas import validate
from deskbrief.sources.base import ParseError, supports_range
from deskbrief.sources.cboe import (
    DATASET,
    SYMBOL_SPECS,
    URL,
    CboeSource,
    url_for,
)
from deskbrief.sources.http import DefaultHttpClient
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_to_utc
from weekly_synth import ctx_at, write

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "tests" / "fixtures" / "synthetic" / "cboe"
NEW = ("VIX9D", "VIX3M", "VIX6M", "VXN", "GVZ", "SKEW")
#: layout seen at the live probe (2026-11-19): header and the column emitted as close
LAYOUT = {
    "VIX9D": ("DATE,OPEN,HIGH,LOW,CLOSE", "CLOSE"),
    "VIX3M": ("DATE,OPEN,HIGH,LOW,CLOSE", "CLOSE"),
    "VIX6M": ("DATE,OPEN,HIGH,LOW,CLOSE", "CLOSE"),
    "VXN": ("DATE,OPEN,HIGH,LOW,CLOSE", "CLOSE"),
    "GVZ": ("DATE,GVZ", "GVZ"),
    "SKEW": ("DATE,SKEW", "SKEW"),
}
#: newest close in each synthetic fixture (11/18/2026)
NEWEST = {"VIX9D": 23.1, "VIX3M": 23.4, "VIX6M": 23.8, "VXN": 32.2, "GVZ": 27.1, "SKEW": 141.8}
TARGET = date(2026, 11, 18)
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def src(config: Config) -> CboeSource:
    return CboeSource(config)


def ctx_for(config: Config, job: JobKind = JobKind.EOD) -> RunContext:
    return RunContext(
        run_id="r1", job=job, trigger=Trigger.SCHEDULE, target_date=TARGET, now_utc=NOW,
        git_sha="abc", config_hash="h", config=config, store=object(),  # type: ignore[arg-type]
    )  # fmt: skip


def raw_for(symbol: str, body: bytes, params: dict[str, str] | None = None) -> RawResponse:
    req = FetchRequest(
        "cboe", DATASET, params=params if params is not None else {"lookback_days": "10"},
        symbols=(symbol,), target_date=TARGET,
    )  # fmt: skip
    return RawResponse(
        request=req, fetch_id="F", requested_at=NOW, responded_at=NOW, http_status=200,
        content_type="text/csv", body=body, error=None,
    )  # fmt: skip


def fixture(symbol: str) -> bytes:
    return (FIX / f"{symbol}_History.csv").read_bytes()


# --- config and layouts ---------------------------------------------------------------------


@pytest.mark.parametrize("symbol", NEW)
def test_config_matches_the_card(config: Config, symbol: str) -> None:
    inst, feed = config.feed_index("cboe", DATASET)[symbol]
    assert inst.id == symbol
    assert (inst.asset_class, inst.unit, feed.tier) == ("vol", "index_pts", 3)
    # T55 (planner-approved): a tier-2 Yahoo quote gives each its overnight move
    assert {(f.source, f.dataset, f.symbol, f.tier) for f in inst.feeds} == {
        ("cboe", DATASET, symbol, 3),
        ("yahoo", "quote", f"^{symbol}", 2),
    }
    assert len(inst.feeds) == 2


@pytest.mark.parametrize("symbol", NEW)
def test_layout_matches_the_probe(symbol: str) -> None:
    header, column = LAYOUT[symbol]
    spec = SYMBOL_SPECS[symbol]
    assert ",".join(spec.header) == header
    assert spec.value_column == column
    first_lines = fixture(symbol).decode().splitlines()
    assert first_lines[0].startswith("# SYNTHETIC FIXTURE")
    assert first_lines[1] == header


def test_every_configured_symbol_has_a_layout(config: Config) -> None:
    assert set(config.feed_index("cboe", DATASET)) == set(SYMBOL_SPECS)


def test_vix_url_unchanged_and_urls_follow_the_symbol() -> None:
    assert url_for("VIX") == URL
    assert url_for("SKEW") == (
        "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/SKEW_History.csv"
    )


# --- plan and plan_range --------------------------------------------------------------------


def test_plan_is_one_request_per_symbol_with_vix_unchanged(src: CboeSource, config: Config) -> None:
    reqs = src.plan(JobKind.EOD, ctx_for(config))
    assert [r.symbols for r in reqs] == [(s,) for s in ("VIX", *NEW)]
    assert all(r.params == {"lookback_days": "10"} and r.target_date == TARGET for r in reqs)
    assert src.plan(JobKind.INTRADAY, ctx_for(config)) == []


def test_plan_skips_a_configured_symbol_without_a_layout(config: Config) -> None:
    data = config.model_dump()
    inst = data["instruments"][-1]
    inst["feeds"] = [
        *inst["feeds"],
        {"source": "cboe", "dataset": DATASET, "symbol": "OVX", "tier": 3},
    ]
    reqs = CboeSource(Config.model_validate(data)).plan(JobKind.EOD, ctx_for(config))
    assert "OVX" not in {r.symbols[0] for r in reqs}
    assert len(reqs) == 7


def test_cboe_has_range_support(src: CboeSource) -> None:
    assert supports_range(src)


def test_plan_range_two_years_is_one_request_per_symbol(src: CboeSource, config: Config) -> None:
    start, end = date(2024, 11, 18), date(2026, 11, 18)
    reqs = src.plan_range(start, end, ctx_for(config, JobKind.HISTORY))
    assert len(reqs) == 7
    for r in reqs:
        assert r.params == {"start": "2024-11-18", "end": "2026-11-18"}
        assert r.target_date == end


def test_plan_range_rejects_end_before_start(src: CboeSource, config: Config) -> None:
    with pytest.raises(ValueError, match="before start"):
        src.plan_range(date(2026, 11, 2), date(2026, 11, 1), ctx_for(config))


def _months_body(header: str, ncols: int) -> bytes:
    """Invented rows on the 15th of each month, 2026-01 .. 2026-11, value = month number."""
    lines = [header]
    for m in range(1, 12):
        vals = ",".join([f"{100 + m}.000000"] * (ncols - 1))
        lines.append(f"{m:02d}/15/2026,{vals}")
    return ("\n".join(lines) + "\n").encode()


@pytest.mark.parametrize("symbol", ["VIX3M", "SKEW"])
def test_range_request_parses_a_multi_month_window(src: CboeSource, symbol: str) -> None:
    header = LAYOUT[symbol][0]
    body = _months_body(header, header.count(",") + 1)
    df = src.parse(raw_for(symbol, body, {"start": "2026-03-01", "end": "2026-08-31"}))
    validate("parsed_observations", df)
    days = sorted(ts.tz_convert("America/New_York").date() for ts in df["as_of"])
    assert days == [date(2026, m, 15) for m in range(3, 9)]
    assert sorted(df["value"]) == [103.0, 104.0, 105.0, 106.0, 107.0, 108.0]


@pytest.mark.parametrize(
    "params",
    [{"start": "2026-03-01"}, {"end": "2026-03-01"}, {"start": "March", "end": "2026-08-31"}],
)
def test_bad_range_params_raise(src: CboeSource, params: dict[str, str]) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for("SKEW", fixture("SKEW"), params))


# --- parse ----------------------------------------------------------------------------------


@pytest.mark.parametrize("symbol", NEW)
def test_each_fixture_parses_with_the_card_labels(
    src: CboeSource, config: Config, symbol: str
) -> None:
    df = src.parse(raw_for(symbol, fixture(symbol)))
    validate("parsed_observations", df)
    assert len(df) == 5  # the old first row is outside the 10-day window
    assert set(df["instrument_id"]) == {symbol} and set(df["symbol"]) == {symbol}
    assert set(df["field"]) == {"close"} and set(df["interval"]) == {"1d"}
    assert set(df["unit"]) == {"index_pts"} and set(df["tier"]) == {3}
    newest = df.sort_values("as_of").iloc[-1]
    assert newest["value"] == pytest.approx(NEWEST[symbol])
    assert newest["as_of"] == pd.Timestamp("2026-11-18 05:00", tz="UTC")  # 00:00 EST (P-006)
    assert find_violations(df, config.checks.validity).empty


@pytest.mark.parametrize("symbol", NEW)
def test_backfill_without_window_reads_every_row(src: CboeSource, symbol: str) -> None:
    assert len(src.parse(raw_for(symbol, fixture(symbol), {}))) == 6


@pytest.mark.parametrize(
    ("symbol", "other"), [("SKEW", "VIX9D"), ("GVZ", "SKEW"), ("VXN", "GVZ"), ("VIX", "SKEW")]
)
def test_a_file_with_another_symbols_layout_raises(
    src: CboeSource, symbol: str, other: str
) -> None:
    with pytest.raises(ParseError, match=f"not {symbol}_History.csv"):
        src.parse(raw_for(symbol, fixture(other)))


@pytest.mark.parametrize(
    "body",
    [
        b"DATE,SKEW\n11/18/2026,n/a\n",
        b"DATE,SKEW\n11/18/2026,inf\n",
        b"DATE,SKEW\n11/18/2026,141.8,1\n",
        b"DATE,SKEW\n11/17/2026,144.0\n11/18/2026,141.",
        b"<!DOCTYPE html><html><body>Access Denied</body></html>",
    ],
)
def test_malformed_skew_raises(src: CboeSource, body: bytes) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for("SKEW", body))


def test_unknown_symbol_in_request_raises(src: CboeSource) -> None:
    with pytest.raises(ParseError, match="no layout"):
        src.parse(raw_for("OVX", b"DATE,OVX\n11/18/2026,30.0\n"))


def test_fetch_uses_each_symbols_file(src: CboeSource, config: Config) -> None:
    reqs = src.plan(JobKind.EOD, ctx_for(config))
    with respx.mock(assert_all_called=True) as mock:
        for symbol in ("VIX", *NEW):
            body = fixture(symbol) if symbol in NEW else (FIX / "VIX_History.csv").read_bytes()
            mock.get(url_for(symbol)).respond(200, content=body)
        with DefaultHttpClient(config.sources) as http:
            raws = [src.fetch(r, http) for r in reqs]
    assert all(r.ok for r in raws)
    by_symbol = {r.request.symbols[0]: src.parse(r) for r in raws}
    assert set(by_symbol) == {"VIX", *NEW}
    assert by_symbol["SKEW"].sort_values("as_of")["value"].iloc[-1] == pytest.approx(141.8)


# --- end to end: store -> freshness, report model and dashboard -----------------------------

THU = date(2026, 11, 19)
AT = et_to_utc(THU, "08:00")  # morning, before today's close is published


def _obs(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Each close becomes visible at 20:30 ET on its own date (invented, after the 20:00 due)."""
    out = []
    for r in df.to_dict("records"):
        day = r["as_of"].tz_convert("America/New_York").date()
        out.append(
            {**r, "run_id": "R1", "ingested_at": et_to_utc(day, "20:30"), "parser_version": "2",
             "fetch_id": f"F-{r['symbol']}-{day:%Y%m%d}"}
        )  # fmt: skip
    return out


@pytest.fixture
def store(tmp_path: Path, src: CboeSource, config: Config) -> LocalBackend:
    s = LocalBackend(tmp_path / "data")
    rows: list[dict[str, Any]] = []
    for symbol in NEW:
        rows += _obs(src.parse(raw_for(symbol, fixture(symbol))))
    write(s, config, rows)
    return s


def test_report_model_shows_each_index_in_the_vol_section(
    store: LocalBackend, config: Config
) -> None:
    model = BriefModel.from_store(connect(store, config), config, THU, AT)
    where = {r.instrument_id: (s.asset_class, r) for s in model.sections for r in s.rows}
    for symbol in NEW:
        section, row = where[symbol]
        assert section == "vol", symbol
        assert row.available, symbol
        assert row.value == pytest.approx(NEWEST[symbol]), symbol
        # A close dated the previous NYSE day has no overnight move yet (query.py, as for a
        # VIX close with no Yahoo quote); the brief still shows the value and its date.
        assert row.change is not None and row.change.status == "no_move_yet", symbol
        assert row.label.startswith(config.instrument(symbol).name), symbol


@pytest.mark.parametrize("symbol", NEW)
def test_dashboard_bars(store: LocalBackend, config: Config, symbol: str) -> None:
    from app.components import data

    bars = data.build_bars(connect(store, config), AT, symbol, frozenset({1}))
    assert len(bars) == 5
    assert set(bars["interval"]) == {"1d"} and set(bars["field"]) == {"close"}


def test_freshness_passes_once_the_close_is_due(store: LocalBackend, config: Config) -> None:
    ctx = ctx_at(config, store, et_to_utc(TARGET, "21:00"))  # after expected_by_et 20:00
    results = [
        r for r in FreshnessCheck().evaluate(ctx, connect(store, config)) if r.source == "cboe"
    ]
    assert results and all(r.status is DQStatus.PASS for r in results), results
