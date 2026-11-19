"""T53: ``plan_range`` for Treasury and ECB (P-020), each over a multi-month range. No network.

SYNTHETIC FIXTURE: the ECB CSV below is generated in the test with invented rates; the Treasury
CSV is the existing synthetic fixture.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from deskbrief.calendars import business_days
from deskbrief.config import Config, load_config
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext, Trigger
from deskbrief.sources.base import ParseError, supports_range
from deskbrief.sources.ecb import EcbSource
from deskbrief.sources.treasury import TreasurySource
from deskbrief.timeutil import et_date

NOW = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)
TREASURY_FIX = (
    Path(__file__).parents[2] / "fixtures" / "synthetic" / "treasury" / "par_curve_202611.csv"
)


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config()


def ctx_for(config: Config) -> RunContext:
    return RunContext(
        run_id="r1", job=JobKind.HISTORY, trigger=Trigger.MANUAL, target_date=date(2026, 11, 19),
        now_utc=NOW, git_sha="abc", config_hash="h", config=config,
        store=object(),  # type: ignore[arg-type]
    )  # fmt: skip


def raw(req: FetchRequest, body: bytes, content_type: str = "text/csv") -> RawResponse:
    return RawResponse(
        request=req, fetch_id="F", requested_at=NOW, responded_at=NOW, http_status=200,
        content_type=content_type, body=body, error=None,
    )  # fmt: skip


# --- ECB ----------------------------------------------------------------------------------------

_HEADER = "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE"


def ecb_csv(days: list[date]) -> bytes:
    """SYNTHETIC FIXTURE: USD, GBP and JPY rates for each day, values invented."""
    lines = ["# SYNTHETIC FIXTURE: invented ECB rates for a multi-month range", _HEADER]
    for i, d in enumerate(days):
        for ccy, rate in (("USD", 1.10 + i / 10000), ("GBP", 0.85), ("JPY", 160.0 + i / 100)):
            lines.append(f"EXR.D.{ccy}.EUR.SP00.A,D,{ccy},EUR,SP00,A,{d.isoformat()},{rate:.4f}")
    return ("\n".join(lines) + "\n").encode()


def test_ecb_has_range_support(config: Config) -> None:
    assert supports_range(EcbSource(config))


def test_ecb_plan_range_two_years_is_one_request(config: Config) -> None:
    src = EcbSource(config)
    reqs = src.plan_range(date(2024, 11, 18), date(2026, 11, 18), ctx_for(config))
    assert len(reqs) == 1
    (req,) = reqs
    assert req.params == {
        "startPeriod": "2024-11-18",
        "endPeriod": "2026-11-18",
        "format": "csvdata",
    }
    assert req.target_date == date(2026, 11, 18)
    assert set(req.symbols) == {"EURUSD", "GBPUSD", "USDJPY"}


def test_ecb_range_request_parses_a_multi_month_window(config: Config) -> None:
    src = EcbSource(config)
    start, end = date(2026, 8, 3), date(2026, 10, 30)
    (req,) = src.plan_range(start, end, ctx_for(config))
    days = business_days("TARGET", start, end)
    assert len(days) > 60
    df = src.parse(raw(req, ecb_csv(days)))
    assert len(df) == 3 * len(days)
    assert {et_date(t.to_pydatetime()) for t in df["as_of"]} == set(days)
    assert set(df["field"]) == {"rate"}


def test_ecb_range_still_rejects_rows_outside_the_window(config: Config) -> None:
    src = EcbSource(config)
    (req,) = src.plan_range(date(2026, 9, 1), date(2026, 9, 30), ctx_for(config))
    with pytest.raises(ParseError, match="outside requested window"):
        src.parse(raw(req, ecb_csv([date(2026, 9, 1), date(2026, 10, 1)])))


def test_ecb_morning_plan_is_unchanged(config: Config) -> None:
    ctx = ctx_for(config)
    (req,) = EcbSource(config).plan(JobKind.MORNING, ctx)
    assert req.params == {
        "startPeriod": "2026-11-18",
        "endPeriod": "2026-11-18",
        "format": "csvdata",
    }
    assert req.target_date == date(2026, 11, 18)


def test_ecb_plan_range_rejects_end_before_start(config: Config) -> None:
    with pytest.raises(ValueError, match="before start"):
        EcbSource(config).plan_range(date(2026, 9, 2), date(2026, 9, 1), ctx_for(config))


# --- Treasury -----------------------------------------------------------------------------------


def test_treasury_has_range_support_with_the_protocol_signature(config: Config) -> None:
    src = TreasurySource(config)
    assert supports_range(src)
    reqs = src.plan_range(date(2024, 11, 18), date(2026, 11, 18), ctx_for(config))
    assert len(reqs) == 25  # one per month, November to November
    assert reqs[0].params == {"month": "202411"}
    assert reqs[0].target_date == date(2024, 11, 18)
    assert reqs[-1].params == {"month": "202611"}


def test_treasury_range_request_parses_its_month(config: Config) -> None:
    src = TreasurySource(config)
    reqs = src.plan_range(date(2026, 9, 15), date(2026, 11, 30), ctx_for(config))
    assert [r.params["month"] for r in reqs] == ["202609", "202610", "202611"]
    df = src.parse(raw(reqs[-1], TREASURY_FIX.read_bytes()))
    assert not df.empty
    assert {et_date(t.to_pydatetime()).month for t in df["as_of"]} == {11}
    assert set(df["interval"]) == {"1d"}


def test_treasury_months_cover_the_range_without_gaps(config: Config) -> None:
    reqs = TreasurySource(config).plan_range(date(2025, 1, 31), date(2025, 12, 1), ctx_for(config))
    months = [r.params["month"] for r in reqs]
    assert months == [f"2025{m:02d}" for m in range(1, 13)]
    first = date(2025, 1, 31)
    assert all(r.target_date is not None and r.target_date >= first for r in reqs)
    assert reqs[1].target_date == date(2025, 2, 1)
    assert reqs[0].target_date == first
