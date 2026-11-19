# ruff: noqa: PT018
"""CFTC Commitments of Traders (card T49, P-021c): gold, E-mini S&P 500 and 10-year note.

No network. ``tests/fixtures/synthetic/cftc/cot_legacy.json`` is a SYNTHETIC FIXTURE: its
columns and date format are the ones the live probe saw on 2026-11-19, the values are invented.
Store rows below come from that fixture through the real parser.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest
import respx

from deskbrief.checks.completeness import CompletenessCheck
from deskbrief.checks.freshness import FreshnessCheck
from deskbrief.checks.validity import find_violations
from deskbrief.config import Config, load_config
from deskbrief.models import (
    DQResult,
    DQStatus,
    FetchRequest,
    JobKind,
    RawResponse,
    RunContext,
    Trigger,
)
from deskbrief.report.model import BriefModel
from deskbrief.schemas import validate
from deskbrief.sources import catalog
from deskbrief.sources.base import ParseError, supports_range
from deskbrief.sources.cftc import (
    DATASET,
    LOOKBACK_DAYS,
    MARKETS,
    ROW_LIMIT,
    URL,
    CftcSource,
    split_symbol,
)
from deskbrief.sources.http import DefaultHttpClient
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_to_utc
from weekly_synth import ctx_at, write

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "tests" / "fixtures" / "synthetic" / "cftc" / "cot_legacy.json"
#: card T49: instrument id -> (market code, field)
CARD = {
    "COT_GC_NET": ("088691", "net_position"),
    "COT_ES_NET": ("13874A", "net_position"),
    "COT_ZN_NET": ("043602", "net_position"),
    "COT_GC_OI": ("088691", "open_interest"),
    "COT_ES_OI": ("13874A", "open_interest"),
    "COT_ZN_OI": ("043602", "open_interest"),
}
#: newest week in the fixture (Tue 2026-11-10, i = 3): long/short/OI = base +100i / -50i / +1000i
NEWEST = {
    "COT_GC_NET": (220_300 - 39_850), "COT_GC_OI": 403_000,
    "COT_ES_NET": (210_300 - 349_850), "COT_ES_OI": 2_003_000,
    "COT_ZN_NET": (700_300 - 1_649_850), "COT_ZN_OI": 6_003_000,
}  # fmt: skip
TARGET = date(2026, 11, 19)
NOW = et_to_utc(TARGET, "17:30")


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def src(config: Config) -> CftcSource:
    return CftcSource(config)


def ctx_for(config: Config, job: JobKind = JobKind.EOD) -> RunContext:
    return RunContext(
        run_id="r1", job=job, trigger=Trigger.SCHEDULE, target_date=TARGET, now_utc=NOW,
        git_sha="abc", config_hash="h", config=config, store=object(),  # type: ignore[arg-type]
    )  # fmt: skip


def rows() -> list[dict[str, Any]]:
    doc = json.loads(FIX.read_text())
    assert doc["_synthetic"].startswith("SYNTHETIC FIXTURE")
    out: list[dict[str, Any]] = doc["rows"]
    return out


def body(items: Any = None) -> bytes:
    return json.dumps(rows() if items is None else items).encode()


def raw_for(content: bytes, start: str = "2026-10-15", end: str = "2026-11-19") -> RawResponse:
    req = FetchRequest(
        "cftc", DATASET, params={"start": start, "end": end},
        symbols=tuple(MARKETS), target_date=date.fromisoformat(end),
    )  # fmt: skip
    return RawResponse(
        request=req, fetch_id="F", requested_at=NOW, responded_at=NOW, http_status=200,
        content_type="application/json", body=content, error=None,
    )  # fmt: skip


# --- config ---------------------------------------------------------------------------------


@pytest.mark.parametrize("iid", CARD)
def test_config_matches_the_card(config: Config, iid: str) -> None:
    code, field = CARD[iid]
    inst = config.instrument(iid)
    assert (inst.asset_class, inst.kind, inst.unit) == ("macro", "macro", "contracts")
    (feed,) = inst.feeds
    assert (feed.source, feed.dataset, feed.symbol, feed.tier) == (
        "cftc", DATASET, f"{code}:{field}", 3,
    )  # fmt: skip
    assert split_symbol(feed.symbol) == (code, field)


def test_source_settings_set_user_agent_and_rate_limit(config: Config) -> None:
    s = config.sources["cftc"]
    assert s.enabled and s.rate_per_min <= 10
    assert s.user_agent and "desk-brief" in s.user_agent


def test_freshness_entry_is_weekly_on_friday(config: Config) -> None:
    (sched,) = [d for d in config.datasets if (d.source, d.dataset) == ("cftc", DATASET)]
    assert sched.release_weekday == 4  # Friday
    assert (sched.expected_by_et, sched.lag_bdays, sched.calendar) == ("15:30", 3, "SIFMA_US")


def test_catalog_loads_the_module() -> None:
    assert "deskbrief.sources.cftc" in catalog.KNOWN_SOURCE_MODULES


@pytest.mark.parametrize("symbol", ["088691", "088691:close", "999999:net_position", "x:y:z"])
def test_split_symbol_rejects_other_shapes(symbol: str) -> None:
    assert split_symbol(symbol) is None


# --- plan and plan_range --------------------------------------------------------------------


def test_plan_is_one_request_for_the_three_markets(src: CftcSource, config: Config) -> None:
    (req,) = src.plan(JobKind.EOD, ctx_for(config))
    assert set(req.symbols) == set(MARKETS) and len(req.symbols) == 3
    start = date(2026, 11, 19).toordinal() - LOOKBACK_DAYS
    assert req.params["start"] == date.fromordinal(start).isoformat()
    assert req.params["end"] == "2026-11-19" and req.target_date == TARGET
    for code in MARKETS:
        assert f"'{code}'" in req.params["$where"]
    assert req.params["$limit"] == str(ROW_LIMIT)


@pytest.mark.parametrize("job", [JobKind.INTRADAY, JobKind.REBUILD])
def test_plan_nothing_for_intraday_and_rebuild(
    src: CftcSource, config: Config, job: JobKind
) -> None:
    assert src.plan(job, ctx_for(config, job)) == []


def test_plan_skips_a_symbol_of_another_shape(config: Config) -> None:
    data = config.model_dump()
    inst = next(i for i in data["instruments"] if i["id"] == "COT_GC_OI")
    inst["feeds"] = [
        *inst["feeds"],
        {"source": "cftc", "dataset": DATASET, "symbol": "GC", "tier": 3},
    ]
    (req,) = CftcSource(Config.model_validate(data)).plan(JobKind.EOD, ctx_for(config))
    assert set(req.symbols) == set(MARKETS)


def test_plan_without_feeds_is_empty(config: Config) -> None:
    data = config.model_dump()
    data["instruments"] = [i for i in data["instruments"] if not i["id"].startswith("COT_")]
    data["datasets"] = [d for d in data["datasets"] if d["source"] != "cftc"]
    assert CftcSource(Config.model_validate(data)).plan(JobKind.EOD, ctx_for(config)) == []


def test_cftc_has_range_support(src: CftcSource) -> None:
    assert supports_range(src)


def test_plan_range_two_years_is_one_request(src: CftcSource, config: Config) -> None:
    start, end = date(2024, 11, 19), date(2026, 11, 19)
    (req,) = src.plan_range(start, end, ctx_for(config, JobKind.HISTORY))
    assert (req.params["start"], req.params["end"]) == ("2024-11-19", "2026-11-19")
    assert "between '2024-11-19T00:00:00' and '2026-11-19T00:00:00'" in req.params["$where"]
    assert req.target_date == end
    assert ROW_LIMIT > 3 * 105  # two years of three markets fits one answer


def test_plan_range_rejects_end_before_start(src: CftcSource, config: Config) -> None:
    with pytest.raises(ValueError, match="before start"):
        src.plan_range(date(2026, 11, 2), date(2026, 11, 1), ctx_for(config))


def test_range_request_parses_a_multi_month_window(src: CftcSource) -> None:
    """Invented weekly gold reports, Jan..Nov 2026; a March..August window keeps those weeks."""
    items = []
    day = date(2026, 1, 6)
    while day <= date(2026, 11, 10):
        items.append({
            "cftc_contract_market_code": "088691",
            "report_date_as_yyyy_mm_dd": f"{day.isoformat()}T00:00:00.000",
            "open_interest_all": str(day.month * 1000),
            "noncomm_positions_long_all": "500", "noncomm_positions_short_all": "200",
        })  # fmt: skip
        day = date.fromordinal(day.toordinal() + 7)
    df = src.parse(raw_for(body(items), "2026-03-01", "2026-08-31"))
    validate("parsed_observations", df)
    oi = df[df["field"] == "open_interest"]
    days = [ts.tz_convert("America/New_York").date() for ts in oi["as_of"]]
    assert min(days) == date(2026, 3, 3) and max(days) == date(2026, 8, 25)
    assert len(days) == 26 and {d.weekday() for d in days} == {1}
    assert sorted(set(oi["value"])) == [m * 1000.0 for m in range(3, 9)]


# --- parse ----------------------------------------------------------------------------------


def test_fixture_parses_with_the_card_labels(src: CftcSource, config: Config) -> None:
    df = src.parse(raw_for(body()))
    validate("parsed_observations", df)
    assert len(df) == 4 * 6
    assert set(df["instrument_id"]) == set(CARD)
    assert set(df["interval"]) == {"1w"} and set(df["unit"]) == {"contracts"}
    assert set(df["tier"]) == {3} and set(df["source"]) == {"cftc"}
    for iid, (code, field) in CARD.items():
        sub = df[df["instrument_id"] == iid].sort_values("as_of")
        assert set(sub["field"]) == {field} and set(sub["symbol"]) == {f"{code}:{field}"}
        assert sub["value"].iloc[-1] == pytest.approx(NEWEST[iid]), iid
        assert set(sub["is_derived"]) == {field == "net_position"}
    assert find_violations(df, config.checks.validity).empty


def test_as_of_is_the_position_tuesday_at_et_midnight(src: CftcSource) -> None:
    df = src.parse(raw_for(body()))
    newest = df["as_of"].max()
    assert newest == pd.Timestamp("2026-11-10 05:00", tz="UTC")  # 00:00 EST, not the release
    assert {ts.tz_convert("America/New_York").weekday() for ts in df["as_of"]} == {1}


def test_window_drops_rows_outside_the_request(src: CftcSource) -> None:
    df = src.parse(raw_for(body(), "2026-11-01", "2026-11-19"))
    days = {ts.tz_convert("America/New_York").date() for ts in df["as_of"]}
    assert days == {date(2026, 11, 3), date(2026, 11, 10)}


def test_unknown_market_rows_are_ignored(src: CftcSource) -> None:
    other = {**rows()[0], "cftc_contract_market_code": "099741"}  # EUR, not configured
    df = src.parse(raw_for(body([other])))
    assert df.empty


def test_empty_list_is_the_legitimate_empty_case(src: CftcSource) -> None:
    df = src.parse(raw_for(b"[]"))
    assert df.empty
    validate("parsed_observations", df)


def _with(**change: Any) -> bytes:
    return body([{**rows()[0], **change}])


@pytest.mark.parametrize(
    "content",
    [
        b'{"code": "query.compiler.malformed", "error": true, "message": "bad query"}',
        b"<!DOCTYPE html><html><body>Access Denied</body></html>",
        b"[1, 2]",
        _with(open_interest_all="n/a"),
        _with(open_interest_all="-5"),
        _with(noncomm_positions_long_all="12.5"),
        _with(noncomm_positions_short_all="inf"),
        _with(noncomm_positions_short_all=None),
        _with(report_date_as_yyyy_mm_dd="10 Nov 2026"),
        _with(cftc_contract_market_code=None),
        body([rows()[0], rows()[0]]),  # the same market and date twice
    ],
)
def test_malformed_bodies_raise(src: CftcSource, content: bytes) -> None:
    with pytest.raises(ParseError):
        src.parse(raw_for(content))


def test_a_body_at_the_row_limit_raises(src: CftcSource) -> None:
    with pytest.raises(ParseError, match="row limit"):
        src.parse(raw_for(body([{}] * ROW_LIMIT)))


def test_failed_fetch_and_missing_window_raise(src: CftcSource) -> None:
    raw = raw_for(body())
    with pytest.raises(ParseError, match="failed fetch"):
        src.parse(RawResponse(**{**raw.__dict__, "http_status": 503}))
    bare = FetchRequest("cftc", DATASET, params={}, symbols=tuple(MARKETS), target_date=TARGET)
    with pytest.raises(ParseError, match="start and end"):
        src.parse(RawResponse(**{**raw.__dict__, "request": bare}))


def test_fetch_sends_the_soda_query_with_the_user_agent(src: CftcSource, config: Config) -> None:
    (req,) = src.plan(JobKind.EOD, ctx_for(config))
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(url__startswith=URL).respond(200, content=body())
        with DefaultHttpClient(config.sources) as http:
            raw = src.fetch(req, http)
    assert raw.ok
    sent = route.calls.last.request
    assert "desk-brief" in sent.headers["User-Agent"]
    query = parse_qs(urlsplit(str(sent.url)).query)
    assert query["$where"] == [req.params["$where"]]
    assert "start" not in query and "end" not in query  # parse-side window only
    assert len(src.parse(raw)) == 4 * 6  # 15 Oct .. 19 Nov holds all four fixture weeks


# --- end to end: store -> checks, report model and dashboard --------------------------------


def _obs(df: pd.DataFrame, run_id: str = "R1") -> list[dict[str, Any]]:
    """Each report becomes visible Monday after its Tuesday (invented; after the release)."""
    out = []
    for r in df.to_dict("records"):
        tue = r["as_of"].tz_convert("America/New_York").date()
        seen = et_to_utc(date.fromordinal(tue.toordinal() + 6), "09:00")
        out.append(
            {**r, "run_id": run_id, "ingested_at": seen, "parser_version": "1",
             "fetch_id": f"F-{r['symbol']}-{tue:%Y%m%d}"}
        )  # fmt: skip
    return out


@pytest.fixture
def store(tmp_path: Path, src: CftcSource, config: Config) -> LocalBackend:
    s = LocalBackend(tmp_path / "data")
    write(s, config, _obs(src.parse(raw_for(body()))))
    return s


def _cftc(results: list[DQResult]) -> list[DQResult]:
    return [r for r in results if r.source == "cftc"]


def test_freshness_passes_on_the_probe_day(store: LocalBackend, config: Config) -> None:
    """Thu 19 Nov: Veterans Day moved the 13 Nov release to Mon 16 Nov; it holds Tue 10 Nov."""
    ctx = ctx_at(config, store, NOW)
    (fresh,) = _cftc(FreshnessCheck().evaluate(ctx, connect(store, config)))
    assert fresh.status is DQStatus.PASS
    details = fresh.details or {}
    assert details["release_day"] == "2026-11-16"
    assert details["data_day"] == "2026-11-10"


def test_thanksgiving_delay_is_skip_not_fail(store: LocalBackend, config: Config) -> None:
    """Fri 27 Nov 18:00 ET, no 24 Nov report: Thanksgiving moves the release to Mon 30 Nov."""
    s = store
    tue17 = [{**r, "report_date_as_yyyy_mm_dd": "2026-11-17T00:00:00.000"} for r in rows()[-3:]]
    write(s, config, _obs(CftcSource(config).parse(raw_for(body(tue17))), "R2"))
    ctx = ctx_at(config, s, et_to_utc(date(2026, 11, 27), "18:00"))
    (fresh,) = _cftc(FreshnessCheck().evaluate(ctx, connect(s, config)))
    assert fresh.status is DQStatus.SKIP
    assert (fresh.details or {})["release_day"] == "2026-11-30"


def test_completeness_counts_each_instrument(store: LocalBackend, config: Config) -> None:
    ctx = ctx_at(config, store, NOW)
    done = _cftc(CompletenessCheck().evaluate(ctx, connect(store, config)))
    assert {r.instrument_id for r in done} == set(CARD)
    assert all(r.status is DQStatus.PASS for r in done), done


def test_report_model_shows_positions_with_week_on_week_change(
    store: LocalBackend, config: Config
) -> None:
    model = BriefModel.from_store(connect(store, config), config, TARGET, NOW)
    where = {r.instrument_id: (s.asset_class, r) for s in model.sections for r in s.rows}
    for iid in CARD:
        section, row = where[iid]
        assert section == "macro", iid
        assert row.available and row.value == pytest.approx(NEWEST[iid]), iid
        assert row.as_of_text == "10 Nov", iid
    gc = where["COT_GC_NET"][1]
    assert gc.change is not None and gc.change.status == "ok"
    assert gc.change.change == pytest.approx(150.0)  # week on week: +100 long, -50 short


@pytest.mark.parametrize("iid", CARD)
def test_dashboard_bars(store: LocalBackend, config: Config, iid: str) -> None:
    from app.components import data

    bars = data.build_bars(connect(store, config), NOW, iid, frozenset({1}))
    assert len(bars) == 4
    assert set(bars["interval"]) == {"1w"} and set(bars["field"]) == {CARD[iid][1]}
