# ruff: noqa: PT018
"""HttpClient tests (card T03). All HTTP is mocked with respx; clock and sleep are fake.

Every payload and key below is a synthetic test value.
"""

from __future__ import annotations

import io
import json
import threading
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, quote_plus

import httpx
import pytest
import respx

from deskbrief.config import SourceSettings
from deskbrief.logging import configure_logging
from deskbrief.models import FetchRequest, FetchStatus
from deskbrief.sources.base import HttpClient
from deskbrief.sources.http import (
    BudgetTracker,
    CallableBudgetStore,
    DefaultHttpClient,
    InMemoryBudgetStore,
    TokenBucket,
    parse_retry_after,
    secret_forms,
)

URL = "https://api.example.test/v1/series"
SECRET = "sup3rS3cretKey99"
START = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


class FakeClock:
    """Deterministic clock: ``sleep`` advances both the monotonic and the wall clock."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def now(self) -> datetime:
        return START + timedelta(seconds=self.t)

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def settings(**kw: object) -> SourceSettings:
    base: dict[str, object] = {"rate_per_min": 600.0, "timeout_s": 5.0, "retries": 3}
    base.update(kw)
    return SourceSettings(**base)  # type: ignore[arg-type]


def make(
    clock: FakeClock,
    s: SourceSettings | None = None,
    budget: BudgetTracker | None = None,
    rng_value: float = 0.0,
    **kw: float,
) -> DefaultHttpClient:
    return DefaultHttpClient(
        {"src": s or settings()},
        budget=budget,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=clock.now,
        rng=lambda: rng_value,
        **kw,
    )


def req() -> FetchRequest:
    return FetchRequest(source="src", dataset="series", params={"series_id": "DGS10"})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def test_satisfies_protocol(clock: FakeClock) -> None:
    assert isinstance(make(clock), HttpClient)


@respx.mock
def test_success_first_try(clock: FakeClock) -> None:
    route = respx.get(URL).respond(200, content=b"a,b\n1,2\n", headers={"content-type": "text/csv"})
    raw = make(clock).get(req(), URL, params={"series_id": "DGS10"})
    assert route.call_count == 1
    assert raw.ok and raw.attempts == 1 and raw.error is None
    assert raw.body == b"a,b\n1,2\n" and raw.content_type == "text/csv"
    assert raw.fetch_status is FetchStatus.OK
    assert raw.url_redacted == f"{URL}?series_id=DGS10"
    assert raw.request == req()
    assert clock.sleeps == []


@respx.mock
def test_units_keyword_is_accepted_and_changes_nothing(clock: FakeClock) -> None:
    """T08A: ``units`` is part of the protocol; until T08B it has no effect on the request."""
    respx.get(URL).respond(200, content=b"a,b\n1,2\n", headers={"content-type": "text/csv"})
    plain = make(clock).get(req(), URL, params={"series_id": "DGS10"})
    weighted = make(clock).get(req(), URL, params={"series_id": "DGS10"}, units=3)
    assert weighted.ok and weighted.attempts == plain.attempts == 1
    assert weighted.body == plain.body and weighted.url_redacted == plain.url_redacted
    assert weighted.fetch_status is plain.fetch_status is FetchStatus.OK


@respx.mock
def test_empty_body_is_empty_status(clock: FakeClock) -> None:
    respx.get(URL).respond(200, content=b"")
    assert make(clock).get(req(), URL).fetch_status is FetchStatus.EMPTY


@respx.mock
@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_retries_5xx_then_succeeds(clock: FakeClock, code: int) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(code), httpx.Response(code), httpx.Response(200, content=b"x")]
    )
    raw = make(clock).get(req(), URL)
    assert route.call_count == 3
    assert raw.ok and raw.attempts == 3
    assert clock.sleeps == [1.0, 2.0]  # exponential, rng=0 -> no jitter


@respx.mock
def test_backoff_has_jitter_and_cap(clock: FakeClock) -> None:
    respx.get(URL).respond(503)
    make(clock, settings(retries=4), rng_value=1.0, max_backoff_s=3.0).get(req(), URL)
    assert clock.sleeps == [1.25, 2.5, 3.0, 3.0]  # cap applies after jitter


@respx.mock
def test_5xx_exhausts_retries_returns_error(clock: FakeClock) -> None:
    route = respx.get(URL).respond(503, content=b"down")
    raw = make(clock, settings(retries=2)).get(req(), URL)
    assert route.call_count == 3
    assert raw.attempts == 3
    assert raw.http_status == 503 and raw.error == "HTTP 503" and raw.body == b"down"
    assert raw.fetch_status is FetchStatus.ERROR and not raw.ok


@respx.mock
def test_zero_retries_makes_one_attempt(clock: FakeClock) -> None:
    route = respx.get(URL).respond(500)
    raw = make(clock, settings(retries=0)).get(req(), URL)
    assert route.call_count == 1 and raw.attempts == 1 and clock.sleeps == []


@respx.mock
@pytest.mark.parametrize("code", [401, 403, 404, 400])
def test_never_retries_client_errors(clock: FakeClock, code: int) -> None:
    route = respx.get(URL).respond(code, content=b"nope")
    raw = make(clock).get(req(), URL)
    assert route.call_count == 1 and raw.attempts == 1
    assert raw.http_status == code and raw.error == f"HTTP {code}"
    assert clock.sleeps == []


@respx.mock
def test_redirect_is_not_followed(clock: FakeClock) -> None:
    route = respx.get(URL).respond(302, headers={"location": "https://evil.test/"})
    raw = make(clock).get(req(), URL)
    assert route.call_count == 1 and raw.error == "HTTP 302"


@respx.mock
@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), httpx.ConnectError("refused")]
)
def test_retries_transport_errors(clock: FakeClock, exc: Exception) -> None:
    route = respx.get(URL).mock(side_effect=[exc, httpx.Response(200, content=b"ok")])
    raw = make(clock).get(req(), URL)
    assert route.call_count == 2 and raw.ok and raw.attempts == 2


@respx.mock
def test_transport_failure_returns_error_not_raise(clock: FakeClock) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))
    raw = make(clock, settings(retries=1)).get(req(), URL)
    assert raw.attempts == 2
    assert raw.http_status is None and raw.body == b""
    assert raw.error is not None and "ConnectError" in raw.error
    assert raw.fetch_status is FetchStatus.ERROR


@respx.mock
def test_timeout_is_passed_from_settings(clock: FakeClock) -> None:
    route = respx.get(URL).respond(200, content=b"x")
    make(clock, settings(timeout_s=7.5)).get(req(), URL)
    assert route.calls[0].request.extensions["timeout"]["read"] == 7.5


@respx.mock
@pytest.mark.parametrize("exc", [httpx.UnsupportedProtocol("bad scheme"), httpx.InvalidURL("bad")])
def test_malformed_request_does_not_raise_or_retry(clock: FakeClock, exc: Exception) -> None:
    route = respx.get(URL).mock(side_effect=exc)
    raw = make(clock).get(req(), URL)
    assert route.call_count == 1 and raw.attempts == 1
    assert raw.error is not None and raw.http_status is None and clock.sleeps == []


@respx.mock
def test_429_honours_retry_after_seconds(clock: FakeClock) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, content=b"x"),
        ]
    )
    raw = make(clock).get(req(), URL)
    assert route.call_count == 2 and raw.ok
    assert clock.sleeps == [7.0]


@respx.mock
def test_429_honours_retry_after_http_date(clock: FakeClock) -> None:
    when = (START + timedelta(seconds=20)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": when}),
            httpx.Response(200, content=b"x"),
        ]
    )
    make(clock).get(req(), URL)
    assert clock.sleeps == [20.0]


@respx.mock
def test_429_without_retry_after_uses_backoff(clock: FakeClock) -> None:
    respx.get(URL).mock(side_effect=[httpx.Response(429), httpx.Response(200, content=b"x")])
    make(clock).get(req(), URL)
    assert clock.sleeps == [1.0]


@respx.mock
def test_429_final_is_rate_limited_status(clock: FakeClock) -> None:
    respx.get(URL).respond(429, headers={"Retry-After": "1"})
    raw = make(clock, settings(retries=1)).get(req(), URL)
    assert raw.attempts == 2 and raw.http_status == 429
    assert raw.fetch_status is FetchStatus.RATE_LIMITED


@respx.mock
def test_retry_after_longer_than_cap_gives_up(clock: FakeClock) -> None:
    route = respx.get(URL).respond(429, headers={"Retry-After": "3600"})
    raw = make(clock).get(req(), URL)
    assert route.call_count == 1 and raw.attempts == 1 and clock.sleeps == []
    assert raw.fetch_status is FetchStatus.RATE_LIMITED


def test_parse_retry_after_forms() -> None:
    assert parse_retry_after("5", START) == 5.0
    assert parse_retry_after(" 2.5 ", START) == 2.5
    assert parse_retry_after("Mon, 16 Nov 2026 12:00:30 GMT", START) == 30.0
    assert parse_retry_after("Mon, 16 Nov 2026 11:00:00 GMT", START) == 0.0  # past -> 0
    for bad in (None, "", "soon", "-3", "inf", "nan"):
        assert parse_retry_after(bad, START) is None


# -- rate limiting ---------------------------------------------------------------------------


def test_token_bucket_spacing(clock: FakeClock) -> None:
    b = TokenBucket(6.0, 1, clock.monotonic, clock.sleep)  # 1 token / 10 s
    assert [b.acquire() for _ in range(3)] == [0.0, 10.0, 10.0]
    assert clock.t == 20.0


def test_token_bucket_refills_and_bursts(clock: FakeClock) -> None:
    b = TokenBucket(60.0, 3, clock.monotonic, clock.sleep)  # 1/s, burst 3
    assert [b.acquire() for _ in range(4)] == [0.0, 0.0, 0.0, 1.0]
    clock.t += 100  # long idle: refill capped at burst
    assert [b.acquire() for _ in range(4)] == [0.0, 0.0, 0.0, 1.0]


@respx.mock
def test_client_rate_limits_per_source(clock: FakeClock) -> None:
    respx.get(URL).respond(200, content=b"x")
    c = DefaultHttpClient(
        {"src": settings(rate_per_min=6), "other": settings(rate_per_min=6000)},
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=clock.now,
        rng=lambda: 0.0,
    )
    for _ in range(3):
        c.get(req(), URL)
    assert clock.sleeps == [10.0, 10.0]
    other = FetchRequest(source="other", dataset="d")
    before = clock.t
    c.get(other, URL)
    c.get(other, URL)
    assert clock.t - before <= 0.02  # independent bucket, fast rate


@respx.mock
def test_retries_are_rate_limited_too(clock: FakeClock) -> None:
    respx.get(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200, content=b"x")])
    make(clock, settings(rate_per_min=6)).get(req(), URL)
    # backoff 1 s elapses first, then the bucket needs the remaining 9 s
    assert clock.sleeps == [1.0, 9.0]


# -- redaction --------------------------------------------------------------------------------


@respx.mock
def test_secrets_on_wire_and_absent_from_request_and_response(clock: FakeClock) -> None:
    route = respx.get(URL).respond(200, content=b"x")
    r = req()
    raw = make(clock).get(
        r,
        URL,
        params={"series_id": "DGS10"},
        headers={"Accept": "text/csv"},
        secret_params={"api_key": SECRET},
        secret_headers={"APCA-API-SECRET-KEY": SECRET},
    )
    sent = route.calls[0].request
    assert sent.url.params["api_key"] == SECRET and sent.url.params["series_id"] == "DGS10"
    assert sent.headers["APCA-API-SECRET-KEY"] == SECRET and sent.headers["Accept"] == "text/csv"
    assert raw.url_redacted == f"{URL}?series_id=DGS10"
    assert SECRET not in repr(raw) and SECRET not in repr(r)
    assert "api_key" not in dict(r.params)


@respx.mock
def test_secret_embedded_in_url_is_masked(clock: FakeClock) -> None:
    respx.get(url__startswith=URL).respond(200, content=b"x")
    raw = make(clock).get(req(), f"{URL}?apikey={SECRET}", params={"a": "1"})
    assert raw.url_redacted is not None
    assert SECRET not in raw.url_redacted and "***" in raw.url_redacted


@respx.mock
def test_error_message_is_redacted(clock: FakeClock) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError(f"failed for api_key={SECRET}"))
    raw = make(clock, settings(retries=0)).get(req(), URL, secret_params={"api_key": SECRET})
    assert raw.error is not None and SECRET not in raw.error


@respx.mock
def test_logs_one_line_per_attempt_and_outcome_without_secrets(clock: FakeClock) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, content=b"x")])
    make(clock).get(
        req(),
        URL,
        params={"series_id": "DGS10"},
        secret_params={"api_key": SECRET},
        secret_headers={"X-Token": SECRET},
    )
    text = stream.getvalue()
    assert SECRET not in text
    lines = [json.loads(line) for line in text.splitlines()]
    events = [line["event"] for line in lines]
    assert events == ["http.attempt", "http.attempt", "http.done"]
    assert lines[0]["attempt"] == 1 and lines[0]["http_status"] == 503
    assert lines[1]["attempt"] == 2 and lines[1]["http_status"] == 200
    done = lines[2]
    assert done["attempts"] == 2 and done["status"] == "ok" and done["source"] == "src"
    assert done["url"] == f"{URL}?series_id=DGS10"


# -- headers / settings -----------------------------------------------------------------------


@respx.mock
def test_user_agent_from_settings_and_default(clock: FakeClock) -> None:
    route = respx.get(URL).respond(200, content=b"x")
    make(clock, settings(user_agent="desk-brief test me@example.test")).get(req(), URL)
    assert route.calls[0].request.headers["user-agent"] == "desk-brief test me@example.test"
    make(clock).get(req(), URL)
    assert route.calls[1].request.headers["user-agent"].startswith("desk-brief/")


@respx.mock
def test_disabled_and_unknown_source_make_no_call(clock: FakeClock) -> None:
    route = respx.get(URL).respond(200, content=b"x")
    off = make(clock, settings(enabled=False)).get(req(), URL)
    assert off.error is not None and "disabled" in off.error and off.http_status is None
    unknown = make(clock).get(FetchRequest(source="nope", dataset="d"), URL)
    assert unknown.error is not None and "nope" in unknown.error
    assert route.call_count == 0


@respx.mock
def test_context_manager_closes(clock: FakeClock) -> None:
    respx.get(URL).respond(200, content=b"x")
    with make(clock) as c:
        assert c.get(req(), URL).ok
    assert c._client.is_closed


# -- budget -----------------------------------------------------------------------------------


@respx.mock
def test_budget_exhausted_makes_no_http_call(clock: FakeClock) -> None:
    route = respx.get(URL).respond(200, content=b"x")
    s = settings(daily_budget=2)
    tracker = BudgetTracker.from_settings(InMemoryBudgetStore(), {"src": s})
    c = make(clock, s, budget=tracker)
    assert c.get(req(), URL).ok and c.get(req(), URL).ok
    raw = c.get(req(), URL)
    assert route.call_count == 2
    assert raw.status is FetchStatus.BUDGET_EXHAUSTED
    assert raw.fetch_status is FetchStatus.BUDGET_EXHAUSTED
    assert raw.http_status is None and raw.body == b"" and not raw.ok
    assert raw.error is not None and "no request made" in raw.error
    assert tracker.used("src", date(2026, 11, 16)) == 2


@respx.mock
def test_budget_resets_next_utc_day(clock: FakeClock) -> None:
    respx.get(URL).respond(200, content=b"x")
    s = settings(daily_budget=1)
    c = make(clock, s, budget=BudgetTracker.from_settings(InMemoryBudgetStore(), {"src": s}))
    assert c.get(req(), URL).ok
    assert c.get(req(), URL).status is FetchStatus.BUDGET_EXHAUSTED
    clock.t += 24 * 3600
    assert c.get(req(), URL).ok


@respx.mock
def test_retries_consume_budget_and_stop_when_spent(clock: FakeClock) -> None:
    route = respx.get(URL).respond(503, content=b"down")
    s = settings(daily_budget=2, retries=5)
    tracker = BudgetTracker.from_settings(InMemoryBudgetStore(), {"src": s})
    raw = make(clock, s, budget=tracker).get(req(), URL)
    assert route.call_count == 2 and raw.attempts == 2
    assert raw.status is FetchStatus.BUDGET_EXHAUSTED
    assert raw.http_status == 503 and raw.body == b"down"


@respx.mock
def test_source_without_budget_is_unlimited(clock: FakeClock) -> None:
    respx.get(URL).respond(200, content=b"x")
    tracker = BudgetTracker(InMemoryBudgetStore(), {"other": 1})
    c = make(clock, budget=tracker)
    assert all(c.get(req(), URL).ok for _ in range(3))
    assert tracker.used("src", date(2026, 11, 16)) == 0


def test_callable_budget_store_persists_through_callables() -> None:
    day = date(2026, 11, 16)
    saved: dict[tuple[str, date], int] = {("twelvedata", day): 4}
    store = CallableBudgetStore(
        load=lambda s, d: saved.get((s, d), 0),
        save=lambda s, d, n: saved.__setitem__((s, d), n),
    )
    tracker = BudgetTracker(store, {"twelvedata": 5})
    assert tracker.try_consume("twelvedata", day) is True
    assert saved[("twelvedata", day)] == 5
    assert tracker.try_consume("twelvedata", day) is False
    assert saved[("twelvedata", day)] == 5  # nothing saved when refused


# -- review round 1 ---------------------------------------------------------------------------

# Secret values that match NO name pattern in deskbrief.logging, so only literal masking helps.
ODD = "zzTopSecret987"


@respx.mock
@pytest.mark.parametrize(
    "message",
    [
        f"Illegal header value {(ODD + chr(10)).encode()!r}",  # h11 style: b'zz...\\n'
        f"bad value {ODD!r}",
        f"raw {ODD} raw",
        f"quoted {quote(ODD, safe='')} {quote_plus(ODD)}",
    ],
)
def test_secret_header_value_never_reaches_error_or_logs(clock: FakeClock, message: str) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    respx.get(URL).mock(side_effect=httpx.ConnectError(message))
    raw = make(clock, settings(retries=1)).get(req(), URL, secret_headers={"X-Custom": ODD + "\n"})
    assert raw.error is not None
    assert ODD not in raw.error
    assert ODD not in repr(raw)
    assert ODD not in stream.getvalue()
    assert "***" in raw.error


@respx.mock
def test_secret_param_value_masked_in_error(clock: FakeClock) -> None:
    value = "a b/c+" + ODD
    respx.get(URL).mock(
        side_effect=httpx.ConnectError(f"url ?token_x={quote(value)}&y={quote_plus(value)} {value}")
    )
    raw = make(clock, settings(retries=0)).get(req(), URL, secret_params={"X-P": value})
    assert raw.error is not None
    assert ODD not in raw.error


@respx.mock
def test_local_protocol_error_is_final_and_masked(clock: FakeClock) -> None:
    """The real shape: a secret with a trailing newline makes h11 reject the header."""
    stream = io.StringIO()
    configure_logging(stream=stream)
    exc = httpx.LocalProtocolError(f"Illegal header value {(ODD + chr(10)).encode()!r}")
    route = respx.get(URL).mock(side_effect=exc)
    raw = make(clock).get(req(), URL, secret_headers={"X-Custom": ODD + "\n"})
    assert route.call_count == 1
    assert raw.attempts == 1
    assert clock.sleeps == []
    assert raw.error is not None
    assert ODD not in raw.error
    assert ODD not in stream.getvalue()


@respx.mock
def test_public_headers_and_url_are_not_masked(clock: FakeClock) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    raw = make(clock, settings(retries=0)).get(req(), URL, secret_headers={"X-Custom": ODD})
    assert raw.error == "ConnectError: boom"


@respx.mock
def test_503_retry_after_header_beats_backoff(clock: FakeClock) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "9"}),
            httpx.Response(200, content=b"x"),
        ]
    )
    make(clock).get(req(), URL)
    assert clock.sleeps == [9.0]


@respx.mock
def test_retry_after_exactly_at_cap_is_honoured(clock: FakeClock) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "120"}),
            httpx.Response(200, content=b"x"),
        ]
    )
    raw = make(clock).get(req(), URL)
    assert route.call_count == 2
    assert raw.ok
    assert clock.sleeps == [120.0]


@respx.mock
def test_retry_after_just_over_cap_gives_up(clock: FakeClock) -> None:
    route = respx.get(URL).respond(429, headers={"Retry-After": "121"})
    make(clock).get(req(), URL)
    assert route.call_count == 1
    assert clock.sleeps == []


@respx.mock
def test_budget_checked_before_sleeping(clock: FakeClock) -> None:
    s = settings(daily_budget=1, retries=3)
    tracker = BudgetTracker.from_settings(InMemoryBudgetStore(), {"src": s})
    respx.get(URL).respond(429, headers={"Retry-After": "100"})
    raw = make(clock, s, budget=tracker).get(req(), URL)
    assert raw.status is FetchStatus.BUDGET_EXHAUSTED
    assert raw.attempts == 1
    assert clock.sleeps == []  # no 100 s nap before discovering there is no budget


@respx.mock
def test_budget_exhausted_before_first_request_reports_attempts_1(clock: FakeClock) -> None:
    """attempts >= 1 is a RawResponse invariant; 'no HTTP call' is carried by error/status."""
    route = respx.get(URL).respond(200, content=b"x")
    s = settings(daily_budget=1)
    store = InMemoryBudgetStore()
    store.save("src", date(2026, 11, 16), 1)
    raw = make(clock, s, budget=BudgetTracker.from_settings(store, {"src": s})).get(req(), URL)
    assert route.call_count == 0
    assert raw.attempts == 1
    assert raw.status is FetchStatus.BUDGET_EXHAUSTED
    assert raw.error is not None
    assert "no request made" in raw.error


@respx.mock
def test_budget_day_is_utc_not_et(clock: FakeClock) -> None:
    respx.get(URL).respond(200, content=b"x")
    clock.t = 16.5 * 3600  # 2026-11-17 04:30Z == 23:30 ET on Nov 16
    store = InMemoryBudgetStore()
    s = settings(daily_budget=5)
    make(clock, s, budget=BudgetTracker.from_settings(store, {"src": s})).get(req(), URL)
    assert store.load("src", date(2026, 11, 17)) == 1
    assert store.load("src", date(2026, 11, 16)) == 0


@respx.mock
def test_budget_rolls_over_at_utc_midnight(clock: FakeClock) -> None:
    respx.get(URL).respond(200, content=b"x")
    s = settings(daily_budget=1)
    c = make(clock, s, budget=BudgetTracker.from_settings(InMemoryBudgetStore(), {"src": s}))
    clock.t = 12 * 3600 - 1  # 23:59:59Z (19:00 ET): still Nov 16
    assert c.get(req(), URL).ok
    assert c.get(req(), URL).status is FetchStatus.BUDGET_EXHAUSTED
    clock.t = 12 * 3600 + 1  # 00:00:01Z (19:00 ET): new UTC day
    assert c.get(req(), URL).ok


@respx.mock
def test_injected_client_that_follows_redirects_is_not_allowed_to(clock: FakeClock) -> None:
    other = respx.get("https://evil.test/").respond(200, content=b"leak")
    respx.get(URL).respond(302, headers={"location": "https://evil.test/"})
    c = DefaultHttpClient(
        {"src": settings()},
        client=httpx.Client(follow_redirects=True),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=clock.now,
        rng=lambda: 0.0,
    )
    raw = c.get(req(), URL, secret_params={"api_key": SECRET})
    assert other.call_count == 0
    assert raw.error == "HTTP 302"


class SlowStore(InMemoryBudgetStore):
    """Widens the load/save race window so a missing lock would be caught."""

    def load(self, source: str, day: date) -> int:
        value = super().load(source, day)
        threading.Event().wait(0.002)
        return value


def test_budget_tracker_is_thread_safe() -> None:
    day = date(2026, 11, 16)
    tracker = BudgetTracker(SlowStore(), {"src": 10})
    results: list[bool] = []

    def worker() -> None:
        results.append(tracker.try_consume("src", day))

    threads = [threading.Thread(target=worker) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 10
    assert tracker.used("src", day) == 10


def test_token_bucket_is_thread_safe_and_ordered() -> None:
    waits: list[float] = []
    b = TokenBucket(6.0, 1, lambda: 0.0, lambda s: None)  # frozen clock: waits 0,10,20,...

    def worker() -> None:
        waits.append(b.acquire())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(waits) == [0.0, 10.0, 20.0, 30.0, 40.0, 50.0]


# -- review round 2 ---------------------------------------------------------------------------

UNI = "sécrétValüe77"


@respx.mock
def test_non_ascii_header_secret_does_not_raise_or_burn_budget(clock: FakeClock) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    route = respx.get(URL).respond(200, content=b"x")
    s = settings(daily_budget=3)
    store = InMemoryBudgetStore()
    c = make(clock, s, budget=BudgetTracker.from_settings(store, {"src": s}))
    raw = c.get(req(), URL, secret_headers={"X-Custom": UNI})
    assert route.call_count == 0
    assert raw.attempts == 1
    assert raw.error is not None
    assert "no request made" in raw.error
    assert raw.fetch_status is FetchStatus.ERROR
    assert store.load("src", date(2026, 11, 16)) == 0  # nothing reserved
    for text in (raw.error, repr(raw), stream.getvalue()):
        assert UNI not in text
        assert "crétV" not in text


@respx.mock
def test_non_ascii_param_secret_is_sent_and_masked(clock: FakeClock) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectError(f"x {UNI} {quote(UNI)} {UNI!a}"), httpx.Response(200)]
    )
    raw = make(clock).get(req(), URL, secret_params={"X-P": UNI})
    assert route.call_count == 2
    assert route.calls[0].request.url.params["X-P"] == UNI
    assert raw.ok
    for frag in (UNI, quote(UNI), "cr\\xe9t"):
        assert frag not in stream.getvalue()


@respx.mock
def test_unicode_error_from_transport_is_final_masked_and_counted(clock: FakeClock) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    exc = UnicodeEncodeError("ascii", UNI, 1, 2, f"ordinal not in range for {UNI}")
    route = respx.get(URL).mock(side_effect=exc)
    s = settings(daily_budget=3)
    store = InMemoryBudgetStore()
    c = make(clock, s, budget=BudgetTracker.from_settings(store, {"src": s}))
    raw = c.get(req(), URL, secret_params={"api_key": UNI})
    assert route.call_count == 1
    assert raw.attempts == 1
    assert clock.sleeps == []
    assert raw.error is not None
    assert "UnicodeEncodeError" in raw.error
    assert "é" not in raw.error
    assert "é" not in stream.getvalue()
    assert store.load("src", date(2026, 11, 16)) == 1  # the attempt was reserved, once


@respx.mock
def test_unexpected_exception_is_returned_not_raised_and_masked(clock: FakeClock) -> None:
    route = respx.get(URL).mock(side_effect=RuntimeError(f"boom {ODD}"))
    raw = make(clock).get(req(), URL, secret_headers={"X-Custom": ODD})
    assert route.call_count == 1
    assert raw.attempts == 1
    assert raw.error is not None
    assert raw.error.startswith("RuntimeError: boom ")
    assert ODD not in raw.error


@respx.mock
def test_short_secrets_do_not_mangle_hostnames_or_status(clock: FakeClock) -> None:
    respx.get(url__startswith="https://demo.example.test").respond(500)
    demo = "https://demo.example.test/v1"
    raw = make(clock, settings(retries=0)).get(
        req(), demo, params={"a": "1"}, secret_params={"apikey": "demo", "n": "500"}
    )
    assert raw.url_redacted == f"{demo}?a=1"
    assert raw.error == "HTTP 500"


def test_secret_forms_floor_and_forms() -> None:
    assert secret_forms("demo") == []
    assert secret_forms("abcde") == []
    assert "abcdef" in secret_forms("abcdef")
    assert "s\\xe9cr" in "".join(secret_forms("sécrét-value"))
