from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from deskbrief.config import load_config
from deskbrief.models import (
    CheckType,
    DQResult,
    DQStatus,
    FetchRequest,
    JobKind,
    RawResponse,
    RunContext,
    Trigger,
    XResult,
)

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _req() -> FetchRequest:
    return FetchRequest(source="fred", dataset="series", params={"series_id": "DGS10"})


def test_fetch_request_params_are_immutable() -> None:
    req = _req()
    with pytest.raises(TypeError):
        req.params["x"] = "y"  # type: ignore[index]


def test_fetch_request_window_validation() -> None:
    ok = FetchRequest(source="a", dataset="b", window=(T0, T0 + timedelta(minutes=1)))
    assert ok.window is not None
    with pytest.raises(ValueError, match="after start"):
        FetchRequest(source="a", dataset="b", window=(T0, T0))
    with pytest.raises(ValueError, match="naive"):
        FetchRequest(source="a", dataset="b", window=(T0.replace(tzinfo=None), T0))


def test_raw_response_ok_and_latency() -> None:
    raw = RawResponse(
        request=_req(),
        fetch_id="f1",
        requested_at=T0,
        responded_at=T0 + timedelta(milliseconds=250),
        http_status=200,
        content_type="application/json",
        body=b"{}",
    )
    assert raw.ok
    assert raw.latency_ms == 250
    err = RawResponse(
        request=_req(),
        fetch_id="f2",
        requested_at=T0,
        responded_at=T0,
        http_status=None,
        content_type="",
        body=b"",
        error="timeout",
    )
    assert not err.ok
    not_2xx = RawResponse(
        request=_req(),
        fetch_id="f3",
        requested_at=T0,
        responded_at=T0,
        http_status=503,
        content_type="",
        body=b"",
    )
    assert not not_2xx.ok


def test_raw_response_rejects_bad_attempts() -> None:
    with pytest.raises(ValueError, match="attempts"):
        RawResponse(
            request=_req(),
            fetch_id="f",
            requested_at=T0,
            responded_at=T0,
            http_status=200,
            content_type="",
            body=b"",
            attempts=0,
        )


def test_run_context_normalizes_times() -> None:
    from deskbrief.timeutil import ET

    ctx = RunContext(
        run_id="r",
        job=JobKind.MORNING,
        trigger=Trigger.SCHEDULE,
        target_date=date(2026, 11, 16),
        now_utc=datetime(2026, 11, 16, 7, 0, tzinfo=ET),
        git_sha="abc",
        config_hash="h",
        config=load_config(ROOT / "config.yaml"),
        store=object(),  # type: ignore[arg-type]
        scheduled_for=datetime(2026, 11, 16, 6, 15, tzinfo=ET),
    )
    assert ctx.now_utc.tzinfo == UTC
    assert ctx.scheduled_for is not None
    assert ctx.scheduled_for.hour == 11


def test_results_validate() -> None:
    r = DQResult(
        run_id="r",
        check_name="freshness.fred",
        check_type=CheckType.FRESHNESS,
        target_date=date(2026, 11, 16),
        evaluated_at=T0,
        status=DQStatus.PASS,
    )
    assert r.status == "pass"
    with pytest.raises(ValueError, match="fail_bps"):
        XResult(
            run_id="r",
            check_name="x",
            group_id=None,
            target_date=date(2026, 11, 16),
            at_time=T0,
            relation="level",
            left_instrument="A",
            left_source="s",
            left_value=1.0,
            right_instrument="A",
            right_source="t",
            right_value=1.0,
            expected_value=None,
            diff_bps=0.0,
            warn_bps=5,
            fail_bps=1,
            status=DQStatus.PASS,
        )


def test_enums_are_strings() -> None:
    assert JobKind("eod") is JobKind.EOD
    assert str(DQStatus.FAIL) == "fail"
    assert {c.value for c in CheckType} >= {"freshness", "consistency", "reconciliation"}


# --- review round 1 additions ------------------------------------------------------------
from deskbrief.models import FetchStatus  # noqa: E402


@pytest.mark.parametrize("name", ["apikey", "api_key", "token", "APCA-API-SECRET-KEY", "auth"])
def test_fetch_request_rejects_secret_params(name: str) -> None:
    with pytest.raises(ValueError, match="secret"):
        FetchRequest(source="x", dataset="y", params={name: "v"})


def test_fetch_request_target_date_must_be_date() -> None:
    with pytest.raises(TypeError, match="date"):
        FetchRequest(source="x", dataset="y", target_date=T0)  # type: ignore[arg-type]


def _resp(**kw: object) -> RawResponse:
    base: dict[str, object] = {
        "request": _req(),
        "fetch_id": "f",
        "requested_at": T0,
        "responded_at": T0,
        "http_status": 200,
        "content_type": "text/csv",
        "body": b"x",
    }
    base.update(kw)
    return RawResponse(**base)  # type: ignore[arg-type]


def test_fetch_status_derivation() -> None:
    assert _resp().fetch_status is FetchStatus.OK
    assert _resp(body=b"").fetch_status is FetchStatus.EMPTY
    assert _resp(http_status=429).fetch_status is FetchStatus.RATE_LIMITED
    assert _resp(http_status=500).fetch_status is FetchStatus.ERROR
    assert _resp(http_status=None, error="timeout").fetch_status is FetchStatus.ERROR
    skipped = _resp(http_status=None, body=b"", status=FetchStatus.BUDGET_EXHAUSTED)
    assert skipped.fetch_status is FetchStatus.BUDGET_EXHAUSTED
    assert not skipped.ok
    provider_err = _resp(status=FetchStatus.ERROR, error="status=error in 200 body")
    assert not provider_err.ok


def test_clock_skew_is_clamped_not_raised() -> None:
    r = _resp(responded_at=T0 - timedelta(seconds=2))
    assert r.latency_ms == 0


def test_result_ids_generated_and_details_json() -> None:
    a = DQResult(
        run_id="r",
        check_name="c",
        check_type=CheckType.VALIDITY,
        target_date=date(2026, 11, 16),
        evaluated_at=T0,
        status=DQStatus.FAIL,
        details={"b": 1, "a": T0},
    )
    b = DQResult(
        run_id="r",
        check_name="c",
        check_type=CheckType.VALIDITY,
        target_date=date(2026, 11, 16),
        evaluated_at=T0,
        status=DQStatus.FAIL,
    )
    assert a.result_id
    assert b.result_id
    assert a.result_id != b.result_id
    assert a.details_json().startswith('{"a":"2026-11-16')
    with pytest.raises(TypeError, match="date"):
        DQResult(
            run_id="r",
            check_name="c",
            check_type=CheckType.VALIDITY,
            target_date=T0,
            evaluated_at=T0,
            status=DQStatus.FAIL,  # type: ignore[arg-type]
        )


def test_raw_response_persisted_strings_are_redacted() -> None:
    r = _resp(
        http_status=None,
        error="GET https://api.twelvedata.com/q?apikey=PERSISTED1 timed out",
        url_redacted="https://api.stlouisfed.org/fred?api_key=PERSISTED2&series_id=DGS10",
    )
    assert r.error is not None
    assert "PERSISTED1" not in r.error
    assert r.url_redacted is not None
    assert "PERSISTED2" not in r.url_redacted
    assert "series_id=DGS10" in r.url_redacted
