# ruff: noqa: PT018
"""FRED macro-desk series (card T46, P-021a): DFII10, T10YIE, DTWEXBGS, BAMLH0A0HYM2, NFCI.

No network. Fixtures under tests/fixtures/synthetic/fred are SYNTHETIC FIXTURES (schema-faithful
to FRED's ``series/observations`` JSON, invented values). Store rows below are built from those
fixtures by the real parser, or invented in the test (each says so). Dates: November 2026;
Veterans Day (Wed 11 Nov) is a SIFMA holiday.
"""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from deskbrief.checks.anomaly import AnomalyCheck
from deskbrief.checks.completeness import CompletenessCheck
from deskbrief.checks.freshness import FreshnessCheck
from deskbrief.checks.validity import find_violations
from deskbrief.config import Config, load_config
from deskbrief.logging import configure_logging
from deskbrief.models import (
    DQResult,
    DQStatus,
    FetchRequest,
    JobKind,
    RawResponse,
    RunContext,
    Trigger,
)
from deskbrief.report.model import BriefModel, Row
from deskbrief.schemas import validate
from deskbrief.sources.base import supports_range
from deskbrief.sources.fred import (
    DOLLAR_LAG_BDAYS,
    RANGE_LIMIT,
    SERIES_SPECS,
    FredSource,
    SeriesSpec,
)
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_to_utc
from weekly_synth import ctx_at, write

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "tests" / "fixtures" / "synthetic" / "fred"

#: card T46 table: FRED id -> (instrument, field, unit, interval, asset class, dataset)
TABLE = {
    "DFII10": ("UST_10Y_REAL", "yield", "pct", "1d", "rates", "series"),
    "T10YIE": ("BEI_10Y", "yield", "pct", "1d", "rates", "series"),
    "DTWEXBGS": ("USD_BROAD", "level", "index_pts", "1d", "fx", "series"),
    "BAMLH0A0HYM2": ("HY_OAS", "rate", "pct", "1d", "macro", "series"),
    "NFCI": ("NFCI", "level", "index", "1w", "macro", "series_weekly"),
}
FIXTURE = {s: f"{s.lower()}.json" for s in TABLE}


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def src(config: Config) -> FredSource:
    return FredSource(config)


def ctx_for(config: Config, job: JobKind = JobKind.MORNING) -> RunContext:
    return RunContext(
        run_id="r1", job=job, trigger=Trigger.SCHEDULE, target_date=date(2026, 11, 19),
        now_utc=datetime(2026, 11, 19, 12, 0, tzinfo=UTC), git_sha="abc", config_hash="h",
        config=config, store=object(),  # type: ignore[arg-type]
    )  # fmt: skip


def raw_for(series: str, body: bytes, dataset: str = "series") -> RawResponse:
    req = FetchRequest(
        source="fred", dataset=dataset, params={"series_id": series}, symbols=(series,)
    )
    t = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    return RawResponse(
        request=req, fetch_id="f1", requested_at=t, responded_at=t, http_status=200,
        content_type="application/json", body=body,
    )  # fmt: skip


def parsed(src: FredSource, series: str) -> pd.DataFrame:
    dataset = TABLE[series][5]
    return src.parse(raw_for(series, (FIX / FIXTURE[series]).read_bytes(), dataset))


def payload(rows: list[tuple[str, str]]) -> bytes:
    return json.dumps({"observations": [{"date": d, "value": v} for d, v in rows]}).encode()


# --- config and specs -----------------------------------------------------------------------


@pytest.mark.parametrize("series", list(TABLE))
def test_config_and_spec_match_the_card(config: Config, series: str) -> None:
    inst_id, field, unit, interval, asset_class, dataset = TABLE[series]
    inst, feed = config.feed_index("fred", dataset)[series]
    assert inst.id == inst_id and inst.unit == unit and inst.asset_class == asset_class
    assert feed.tier == 3  # official, shown in hosted mode
    spec = SERIES_SPECS[series]
    assert (spec.field, spec.interval) == (field, interval)


def test_broad_dollar_is_kind_macro_not_fx(config: Config) -> None:
    """Kind fx requires unit fx_rate; an index of the dollar is not a currency pair."""
    inst, _ = config.feed_index("fred", "series")["DTWEXBGS"]
    assert inst.kind == "macro"


def test_nfci_weekly_schedule(config: Config) -> None:
    """Chicago Fed: Wednesdays 08:30 ET, data through the previous Friday (docs/sources/fred.md)."""
    sched = next(d for d in config.datasets if (d.source, d.dataset) == ("fred", "series_weekly"))
    assert sched.release_weekday == 2  # Wednesday
    assert sched.lag_bdays == 3  # Wednesday -> the Friday before
    assert sched.expected_by_et == "09:30" and sched.grace_minutes == 90
    assert sched.calendar == "SIFMA_US"


def test_broad_dollar_has_its_own_lag() -> None:
    assert SERIES_SPECS["DTWEXBGS"].lag_bdays == DOLLAR_LAG_BDAYS == 6
    for s in ("DFII10", "T10YIE", "BAMLH0A0HYM2"):
        assert SERIES_SPECS[s].lag_bdays is None  # the dataset's 1-day lag applies


# --- plan and plan_range --------------------------------------------------------------------


def test_plan_puts_nfci_on_series_weekly(src: FredSource, config: Config) -> None:
    reqs = {r.symbols[0]: r for r in src.plan(JobKind.EOD, ctx_for(config, JobKind.EOD))}
    for series, row in TABLE.items():
        assert reqs[series].dataset == row[5]
        assert reqs[series].params["limit"] == "10"


def test_plan_skips_a_series_on_the_wrong_dataset(config: Config) -> None:
    """A weekly spec on the daily dataset (or the reverse) is a config bug: logged, not fetched."""
    stream = io.StringIO()
    configure_logging(stream=stream)
    src = FredSource(config)
    src._specs["DFII10"] = SeriesSpec("yield", "1w")
    reqs = src.plan(JobKind.MORNING, ctx_for(config))
    assert "DFII10" not in [r.symbols[0] for r in reqs]
    logged = [json.loads(ln) for ln in stream.getvalue().splitlines() if ln.strip()]
    skips = [r for r in logged if r.get("event") == "fred.plan.skip"]
    assert [s["series"] for s in skips] == ["DFII10"] and skips[0]["level"] == "error"
    assert "series_weekly" in skips[0]["reason"]


def test_fred_has_range_support(src: FredSource) -> None:
    assert supports_range(src)


def test_plan_range_two_years_is_one_request_per_series(src: FredSource, config: Config) -> None:
    start, end = date(2024, 11, 19), date(2026, 11, 18)
    reqs = src.plan_range(start, end, ctx_for(config))
    by_series = {r.symbols[0]: r for r in reqs}
    assert len(reqs) == len(by_series) == len(set(SERIES_SPECS))  # one per configured series
    for r in reqs:
        assert r.params["observation_start"] == "2024-11-19"
        assert r.params["observation_end"] == "2026-11-18"
        assert r.params["limit"] == str(RANGE_LIMIT) == "100000"  # FRED's maximum
        assert r.params["sort_order"] == "asc" and r.params["file_type"] == "json"
        assert r.target_date == end
        assert not any("key" in k.lower() for k in r.params)
    assert by_series["NFCI"].dataset == "series_weekly"
    assert by_series["DTWEXBGS"].dataset == "series"
    # two years of daily values (~520) fit far inside one response
    assert (end - start).days < RANGE_LIMIT


def test_plan_range_single_day_and_invalid(src: FredSource, config: Config) -> None:
    d = date(2026, 11, 18)
    reqs = src.plan_range(d, d, ctx_for(config))
    assert reqs and all(r.params["observation_start"] == r.params["observation_end"] for r in reqs)
    with pytest.raises(ValueError, match="before start"):
        src.plan_range(d, d - timedelta(days=1), ctx_for(config))


# --- parse ----------------------------------------------------------------------------------


@pytest.mark.parametrize("series", list(TABLE))
def test_each_fixture_parses_with_the_card_labels(src: FredSource, series: str) -> None:
    inst_id, field, unit, interval, _, dataset = TABLE[series]
    df = parsed(src, series)
    validate("parsed_observations", df)
    assert not df.empty and df["value"].notna().all()
    assert set(df["instrument_id"]) == {inst_id} and set(df["field"]) == {field}
    assert set(df["unit"]) == {unit} and set(df["interval"]) == {interval}
    assert set(df["dataset"]) == {dataset} and set(df["tier"]) == {3}


def test_nfci_is_weekly_dated_friday_at_et_midnight(src: FredSource) -> None:
    df = parsed(src, "NFCI")
    assert len(df) == 4
    days = sorted(ts.tz_convert("America/New_York").date() for ts in df["as_of"])
    assert all(d.weekday() == 4 for d in days)  # FRED dates the NFCI week by its Friday
    newest = df[df["as_of"] == df["as_of"].max()].iloc[0]
    assert newest["as_of"] == pd.Timestamp("2026-11-13T05:00:00Z")  # 00:00 EST, P-021
    assert newest["value"] == -0.52


def test_tips_yield_skips_missing_and_keeps_negative_values(src: FredSource) -> None:
    df = parsed(src, "DFII10")
    assert len(df) == 3  # the "." row is skipped
    assert sorted(df["value"]) == [-1.98, -0.42, 0.15]


# --- validity -------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "bad"), [(-2.0, False), (-1.98, False), (-2.01, True)])
def test_tips_yield_down_to_minus_two_passes_validity(
    src: FredSource, config: Config, value: float, bad: bool
) -> None:
    df = src.parse(raw_for("DFII10", payload([("2026-11-18", str(value))])))
    assert (not find_violations(df, config.checks.validity).empty) is bad


def test_fixtures_pass_validity_including_negative_nfci(src: FredSource, config: Config) -> None:
    for series in TABLE:
        df = parsed(src, series)
        assert find_violations(df, config.checks.validity).empty, series
    assert (parsed(src, "NFCI")["value"] < 0).any()


# --- end to end: store -> checks, report model and dashboard --------------------------------

THU = date(2026, 11, 19)
AT = et_to_utc(THU, "17:30")  # eod


def _obs(df: pd.DataFrame, ingested: datetime, run_id: str) -> list[dict[str, Any]]:
    extra = {"run_id": run_id, "ingested_at": ingested, "parser_version": "1"}
    return [
        {**r, **extra, "fetch_id": f"F-{r['symbol']}-{r['as_of']:%Y%m%d}"}
        for r in df.to_dict("records")
    ]


@pytest.fixture
def store(tmp_path: Path, src: FredSource, config: Config) -> LocalBackend:
    """Fixture rows parsed by the real parser, made visible at plausible (invented) times."""
    s = LocalBackend(tmp_path / "data")
    rows: list[dict[str, Any]] = []
    rows += _obs(parsed(src, "DTWEXBGS"), et_to_utc(date(2026, 11, 16), "16:20"), "R1")
    rows += _obs(parsed(src, "NFCI"), et_to_utc(date(2026, 11, 18), "08:35"), "R2")
    for series in ("DFII10", "T10YIE", "BAMLH0A0HYM2"):
        rows += _obs(parsed(src, series), et_to_utc(THU, "16:20"), "R3")
    write(s, config, rows)
    return s


@pytest.fixture
def model(store: LocalBackend, config: Config) -> BriefModel:
    return BriefModel.from_store(connect(store, config), config, THU, AT)


def rows_of(model: BriefModel) -> dict[str, Row]:
    return {r.instrument_id: r for r in model.rows()}


def test_report_model_shows_every_series_with_its_p009_label(model: BriefModel) -> None:
    rows = rows_of(model)
    expected = {
        "UST_10Y_REAL": ("US 10-year TIPS real yield", -0.42, "bp"),
        "BEI_10Y": ("US 10-year breakeven inflation (nominal minus real yield)", 2.31, "bp"),
        "USD_BROAD": ("Nominal broad US dollar index (level)", 121.4562, "pts"),
        "HY_OAS": ("ICE BofA US high-yield option-adjusted spread (rate)", 3.12, "pp"),
        "NFCI": ("Chicago Fed National Financial Conditions Index (level)", -0.52, "pts"),
    }
    for iid, (label, value, change_unit) in expected.items():
        r = rows[iid]
        assert r.available, iid
        assert r.label == label
        assert r.value == pytest.approx(value), iid
        assert r.change_value_unit == change_unit, iid
    # NFCI: released Wed 18 Nov, so the newest weekly value is Fri 13 Nov, shown as a date only
    assert rows["NFCI"].as_of_text == "13 Nov"
    assert rows["USD_BROAD"].as_of_text == "13 Nov"  # H.10: last Friday's value on a Thursday


def test_report_sections_follow_the_asset_classes(model: BriefModel) -> None:
    where = {r.instrument_id: s.asset_class for s in model.sections for r in s.rows}
    assert where["UST_10Y_REAL"] == where["BEI_10Y"] == "rates"
    assert where["USD_BROAD"] == "fx"
    assert where["HY_OAS"] == where["NFCI"] == "macro"


@pytest.mark.parametrize(
    ("iid", "interval", "field"),
    [
        ("UST_10Y_REAL", "1d", "yield"),
        ("BEI_10Y", "1d", "yield"),
        ("USD_BROAD", "1d", "level"),
        ("HY_OAS", "1d", "rate"),
        ("NFCI", "1w", "level"),
    ],
)
def test_dashboard_bars(
    store: LocalBackend, config: Config, iid: str, interval: str, field: str
) -> None:
    from app.components import data

    bars = data.build_bars(connect(store, config), AT, iid, frozenset({1}))
    assert not bars.empty
    assert set(bars["interval"]) == {interval} and set(bars["field"]) == {field}


def _for(results: list[DQResult], iid: str | None = None, dataset: str | None = None) -> DQResult:
    hits = [
        r for r in results
        if (iid is None or r.instrument_id == iid)
        and (dataset is None or (r.details or {}).get("dataset") == dataset)
        and r.source == "fred"
    ]  # fmt: skip
    assert len(hits) == 1, hits
    return hits[0]


def test_weekly_freshness_and_completeness_pass_after_the_release(
    store: LocalBackend, config: Config
) -> None:
    ctx = ctx_at(config, store, AT)
    duck = connect(store, config)
    fresh = _for(FreshnessCheck().evaluate(ctx, duck), dataset="series_weekly")
    assert fresh.status is DQStatus.PASS
    assert (fresh.details or {})["data_day"] == "2026-11-13"  # Wed 18 Nov release -> Fri 13
    done = _for(CompletenessCheck().evaluate(ctx, duck), "NFCI")
    assert done.status is DQStatus.PASS


def test_weekly_freshness_waits_until_due_on_release_day(config: Config, tmp_path: Path) -> None:
    """Wednesday 08:00 ET: this week's release is not yet due, so last week's is judged."""
    s = LocalBackend(tmp_path / "w")
    nfci = FredSource(config).parse(
        raw_for("NFCI", payload([("2026-11-06", "-0.49")]), "series_weekly")
    )
    write(s, config, _obs(nfci, et_to_utc(date(2026, 11, 12), "08:35"), "R1"))
    ctx = ctx_at(config, s, et_to_utc(date(2026, 11, 18), "08:00"))
    fresh = _for(FreshnessCheck().evaluate(ctx, connect(s, config)), dataset="series_weekly")
    assert fresh.status is DQStatus.PASS
    assert (fresh.details or {})["scheduled_release_day"] == "2026-11-11"


def test_broad_dollar_completeness_uses_the_weekly_h10_lag(
    store: LocalBackend, config: Config
) -> None:
    """Thursday: the newest DTWEXBGS value is last Friday's, which is healthy for H.10."""
    ctx = ctx_at(config, store, AT)
    results = CompletenessCheck().evaluate(ctx, connect(store, config))
    usd = _for(results, "USD_BROAD")
    assert usd.status is DQStatus.PASS
    # Thu 19 Nov less 6 SIFMA business days (Veterans Day skipped) = Tue 10 Nov
    assert (usd.details or {})["data_day"] == "2026-11-10"
    for iid in ("UST_10Y_REAL", "BEI_10Y", "HY_OAS"):
        assert _for(results, iid).status is DQStatus.PASS, iid


# --- anomaly on the daily level series (DTWEXBGS) ------------------------------------------


def _dollar_store(tmp_path: Path, config: Config, last: float) -> LocalBackend:
    """SYNTHETIC: 30 business days of a broad-dollar level near 120 with small moves, then
    ``last`` on Fri 13 Nov; all visible after the Mon 16 Nov H.10-style release."""
    days: list[date] = []
    d = date(2026, 11, 13)
    while len(days) < 31:
        if d.weekday() < 5 and d != date(2026, 11, 11):
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    values = [120.0 + (0.1 if i % 2 else -0.1) + 0.01 * i for i in range(len(days) - 1)] + [last]
    body = payload([(x.isoformat(), f"{v:.4f}") for x, v in zip(days, values, strict=True)])
    df = FredSource(config).parse(raw_for("DTWEXBGS", body))
    s = LocalBackend(tmp_path / f"usd-{last}")
    write(s, config, _obs(df, et_to_utc(date(2026, 11, 16), "16:20"), "R1"))
    return s


def _anomaly(store: LocalBackend, config: Config, now: datetime) -> DQResult:
    ctx = replace(ctx_at(config, store, now), job=JobKind.EOD)
    return _for(AnomalyCheck().evaluate(ctx, connect(store, config)), "USD_BROAD")


def test_dollar_anomaly_scores_relative_moves_after_the_release(
    tmp_path: Path, config: Config
) -> None:
    s = _dollar_store(tmp_path, config, last=123.0)  # ~+2.4% after ~0.2% moves
    res = _anomaly(s, config, et_to_utc(date(2026, 11, 17), "17:30"))  # Tuesday eod
    assert res.status is DQStatus.FAIL  # "verify", not "wrong"
    assert (res.details or {})["move_unit"] == "return"
    calm = _anomaly(_dollar_store(tmp_path, config, last=120.31), config,
                    et_to_utc(date(2026, 11, 17), "17:30"))  # fmt: skip
    assert calm.status is DQStatus.PASS


def test_dollar_anomaly_is_stale_series_skip_late_in_the_week(
    tmp_path: Path, config: Config
) -> None:
    """The anomaly check uses the dataset's 1-day lag, so by Thursday the Friday value is old:
    a harmless ``stale series`` skip, never a re-reported move (docs/sources/fred.md)."""
    s = _dollar_store(tmp_path, config, last=123.0)
    res = _anomaly(s, config, AT)
    assert res.status is DQStatus.SKIP and (res.details or {})["reason"] == "stale series"
