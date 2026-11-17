import io
import json

import pytest

from deskbrief.logging import (
    MASK,
    configure_logging,
    get_logger,
    redact_text,
    redact_value,
)


def _log_line(**fields: object) -> dict[str, object]:
    buf = io.StringIO()
    configure_logging(stream=buf)
    get_logger(run_id="r1").info("fetch.done", **fields)
    out = buf.getvalue().strip().splitlines()[-1]
    parsed = json.loads(out)
    assert isinstance(parsed, dict)
    return parsed


def test_json_line_with_bindings() -> None:
    line = _log_line(source="fred")
    assert line["event"] == "fetch.done"
    assert line["run_id"] == "r1"
    assert line["level"] == "info"
    assert "timestamp" in line


def test_query_string_secret_never_logged() -> None:
    url = "https://api.twelvedata.com/quote?symbol=EUR/USD&apikey=SECRET123456&format=JSON"
    line = _log_line(url=url)
    assert "SECRET123456" not in json.dumps(line)
    assert f"apikey={MASK}" in str(line["url"])
    assert "symbol=EUR/USD" in str(line["url"])


def test_secret_keys_masked_recursively() -> None:
    line = _log_line(headers={"APCA-API-SECRET-KEY": "abcdef123", "Accept": "json"})
    headers = line["headers"]
    assert isinstance(headers, dict)
    assert headers["APCA-API-SECRET-KEY"] == MASK
    assert headers["Accept"] == "json"


def test_env_secret_values_masked_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "fredkey0123456789")  # gitleaks:allow (fake test value)
    line = _log_line(error="401 for key fredkey0123456789 in body")
    assert "fredkey0123456789" not in json.dumps(line)


def test_short_env_values_not_used_as_masks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "ab")
    line = _log_line(note="about a cab")
    assert line["note"] == "about a cab"


def test_redact_helpers() -> None:
    assert redact_text("api_key: XYZ987", []) == f"api_key: {MASK}"
    assert redact_text("token=abc12345XYZ&x=1") == f"token={MASK}&x=1"
    assert redact_value(["key_id=zzz", 5]) == [f"key_id={MASK}", 5]
    assert redact_value(("a",)) == ["a"]
    assert redact_value({"password": ""}, key=None) == {"password": ""}
    assert redact_value(None, key="token") is None


def test_level_filtering() -> None:
    buf = io.StringIO()
    configure_logging(level="WARNING", stream=buf)
    get_logger().info("hidden")
    get_logger().warning("shown")
    assert "hidden" not in buf.getvalue()
    assert "shown" in buf.getvalue()


# --- review round 1: redaction bypasses found by probing ---------------------------------
import logging as stdlib_logging  # noqa: E402
from urllib.parse import quote_plus  # noqa: E402


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("Authorization: Bearer abcDEF123456", "abcDEF123456"),
        ("bearer abcDEF123456", "abcDEF123456"),
        ("Cookie: session=abcdef; other=1", "session=abcdef"),
        ("https://user:hunter2pass@example.com/x", "hunter2pass"),
        ('{"apikey": "JSONSECRET1"}', "JSONSECRET1"),
        ("https://x?api%5Fkey=ENCODED123", "ENCODED123"),
        ("https://x?q=apikey%3DENCODED456", "ENCODED456"),
    ],
)
def test_redaction_bypasses_closed(text: str, leak: str) -> None:
    assert leak not in redact_text(text)


def test_url_encoded_env_secret_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "s3cr3t/with+chars="  # gitleaks:allow (fake test value)
    monkeypatch.setenv("TWELVEDATA_API_KEY", secret)
    line = _log_line(note=f"x?k={quote_plus(secret)}")
    assert quote_plus(secret) not in json.dumps(line)
    assert secret not in json.dumps(line)


def test_non_string_values_redacted() -> None:
    exc = RuntimeError("GET https://api.twelvedata.com/q?apikey=EXCSECRET9 failed")
    line = _log_line(err=exc, raw=b"token=BYTESECRET9", s={"apikey=SETSECRET9"})
    dumped = json.dumps(line)
    for leak in ("EXCSECRET9", "BYTESECRET9", "SETSECRET9"):
        assert leak not in dumped


def test_dataclass_values_redacted() -> None:
    from dataclasses import dataclass

    @dataclass
    class Thing:
        url: str

    line = _log_line(obj=Thing(url="https://x?token=DCSECRET99"))
    assert "DCSECRET99" not in json.dumps(line)


def test_stdlib_and_httpx_logs_redacted_and_capped() -> None:
    buf = io.StringIO()
    configure_logging(stream=buf)
    stdlib_logging.getLogger("thirdparty").warning("calling https://x?apikey=STDSECRET1")
    stdlib_logging.getLogger("httpx").info("HTTP Request: GET https://x?apikey=HTTPXSECRET")
    out = buf.getvalue()
    assert "STDSECRET1" not in out
    assert f"apikey={MASK}" in out
    assert "HTTPXSECRET" not in out  # httpx INFO is suppressed entirely
    configure_logging(stream=buf)  # idempotent: no duplicate handlers
    handlers = [
        h for h in stdlib_logging.getLogger().handlers if type(h).__name__ == "_RedactedHandler"
    ]
    assert len(handlers) == 1


# --- review round 2 ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ["token bucket empty, waiting 2.5s", "basic auth not used", "cache key count", "monkey=3"],
)
def test_ordinary_text_not_over_redacted(text: str) -> None:
    assert redact_text(text) == text


def test_numbers_under_secretish_keys_kept() -> None:
    line = _log_line(requests_per_token=1, key_id_count=2)
    assert line["requests_per_token"] == 1
    assert line["key_id_count"] == 2


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("https://tokVALUE123@host/x", "tokVALUE123"),
        ("https://x/q?key=BAREKEY123", "BAREKEY123"),
        ("https://x/q?auth=AUTHVAL123", "AUTHVAL123"),
        ("https://b.r2.dev/o?X-Amz-Signature=abcdef0123&X-Amz-Credential=AKIAXYZ", "abcdef0123"),
        ("https://b.r2.dev/o?X-Amz-Signature=abcdef0123&X-Amz-Credential=AKIAXYZ", "AKIAXYZ"),
    ],
)
def test_round2_bypasses_closed(text: str, leak: str) -> None:
    assert leak not in redact_text(text)


# --- review round 3: one table of everything that must be masked / must stay readable -----
MUST_MASK = [
    ("client_secret=abc123", "abc123"),
    ("access_token=abc123456", "abc123456"),
    ("https://x/cb?access_token=abc123456&state=1", "abc123456"),
    ("refresh-token=rt0987654", "rt0987654"),
    ("my-apikey=MYKEY12345", "MYKEY12345"),
    ("X-Api-Key: abcdef123", "abcdef123"),
    ("x-api-key=abcdef123", "abcdef123"),
    ("APCA-API-SECRET-KEY: s3cr3tvalue", "s3cr3tvalue"),
    ('password: "pw with space"', "with space"),
    ("password='hunter two'", "hunter two"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("https://x/q?key=BAREKEY123", "BAREKEY123"),
    ("https://x/q?sig=abcdefghijklmnopqrst", "abcdefghijklmnopqrst"),
    # T05 (SYNTHETIC FIXTURE): secret_key, session|private|auth token|key, x-auth-token, sessionid
    # split so the gitleaks generic-api-key rule does not flag the (synthetic) probe itself
    ("SECRET_KEY" + ": " + "abcd1234efgh", "abcd1234efgh"),
    ("MY_SECRET_KEY=abc123", "abc123"),
    ("ALPACA_SECRET_KEY: 'abc'", "abc"),  # the value itself, not "abc'"; see exact test below
    ('{"secret_key": "abcd1234"}', "abcd1234"),
    ("x-auth-token: v4lue99", "v4lue99"),
    ("sessionid=abc123def", "abc123def"),
    ("session_token=sess0987", "sess0987"),
    ("private-key: pk0987", "pk0987"),
    ("authtoken=at0987", "at0987"),
]
MUST_KEEP = [
    "missing key: EURUSD",
    "cache key=SPY:1m",
    "dedup natural key: (SPY, yahoo, 1d)",
    "auth: failed with 401",
    "token: expired",
    "primary key: id",
    "Basic authentication is not required",
    "token bucket empty, waiting 2.5s",
    "monkey=3",
    "keyboard=qwerty",
    # T05 (T01 confirmation #3): placeholders after strong names are not credentials
    "password: required",
    "secret: missing",
    "signature: ok",
    "api_key: None",
    "api_key: null",
    "cookie: expired",
    "token_secret: n/a",
    "password: FALSE",
    "secret: true",
    "api_key: ***",
    "key_id: 12",
    "key_id: 007",
    # T05 (T01 confirmation #2): market / cache keys after weak names
    "cache_key=SPY:1m:2026-11-14",
    "key=SPY:2026-11-14T13:30:00Z",
    "key=EURUSD_2026-11-14",
]


@pytest.mark.parametrize(("text", "leak"), MUST_MASK)
def test_must_mask(text: str, leak: str) -> None:
    assert leak not in redact_text(text)


@pytest.mark.parametrize("text", MUST_KEEP)
def test_must_keep(text: str) -> None:
    assert redact_text(text) == text


def test_key_based_masking_is_precise() -> None:
    out = redact_value({"token_state": "fresh", "access_token": "abc", "token": "t0k3n"})
    assert out == {"token_state": "fresh", "access_token": MASK, "token": MASK}


def test_quoted_secret_is_masked_whole_without_the_quote() -> None:
    # nit from review round 1: the exact output, so a partial mask cannot pass
    assert redact_text("ALPACA_SECRET_KEY: 'abc'") == "ALPACA_SECRET_KEY: '***'"


@pytest.mark.parametrize(
    "text",
    ["api_key: s3cr3tValue99", "password: required1", "key_id: 1234", "secret: nonesuch"],
)
def test_near_placeholders_and_real_credentials_still_masked(text: str) -> None:
    assert redact_text(text).endswith(MASK)


@pytest.mark.parametrize(
    "text",
    # built from pieces so gitleaks' generic-api-key rule does not flag these synthetic probes
    ["key" + "=sk_live_abc123def", "key" + "=AbCdEfGh12345678", "token" + "=abc:def:ghi12345"],
)
def test_weak_tier_credentials_with_separators_still_masked(text: str) -> None:
    assert redact_text(text).endswith(MASK)


def test_structured_keys_cover_the_strong_tier_names() -> None:
    fields = {
        "private_key": "PEMDATA123",
        "auth": "abc",
        "sessionid": "s3ss10n99",
        "secret_key": "sk1",
        "session_token": "st1",
        "auth-key": "ak1",
        "x-auth-token": "xt1",
        "private-token": "pt1",
    }
    assert redact_value(fields) == {k: MASK for k in fields}


def test_structured_keys_stay_precise() -> None:
    fields = {
        "token_state": "fresh",
        "author": "bob",
        "auth_state": "ok",
        "session_key_count": "3",
        "private_key_path": "/x",
    }
    assert redact_value(fields) == fields


# --- T08: the two known gaps from the PR #15 review (findings 1-2), both tightened ------------
# Built from pieces so gitleaks' generic-api-key rule does not flag these synthetic probes.
WEAK_UPPERCASE_PROBES = [
    "token" + "=GHP_ABCD1234",
    "token" + "=SK_LIVE_ABC123",
    "key" + "=A1B2C3D4:E5F6G7H8",
    "key" + "=AAAA_BBBB_CCCC_DDDD",
    "token" + "=AB12:CD34",
    "key" + "=1234:5678",
    "key" + "=12:34:56",
    "key" + "=PK12_AB34",
]
SHORT_NUMERIC_PROBES = [
    "password" + "=123",
    "secret" + ": 999",
    "api_key" + "=12",
    "passwd" + "=007",
    "cookie" + ": 123",
    "Authorization" + ": 1234",
    "client_secret" + "=42",
]


@pytest.mark.parametrize("text", WEAK_UPPERCASE_PROBES)
def test_weak_tier_all_uppercase_credentials_are_masked(text: str) -> None:
    out = redact_text(text)
    assert out.endswith(MASK), out


@pytest.mark.parametrize("text", SHORT_NUMERIC_PROBES)
def test_short_numeric_values_after_strong_names_are_masked(text: str) -> None:
    out = redact_text(text)
    assert out.endswith(MASK), out


@pytest.mark.parametrize("text", ["key_id: 12", "key_id=007", "access_key_id: 5"])
def test_short_numeric_ids_stay_readable_for_id_names_only(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "key=SPY:1m:2026-11-14",
        "cache_key=EURUSD_2026-11-14",
        "key=SPY:2026-11-14T13:30:00Z",
        "key=ES_1d",
        "key=SPY:QQQ:IWM:1m:1d",
    ],
)
def test_market_keys_with_a_date_or_interval_stay_readable(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "password" + "=ok",  # a status word, not a credential
        "secret" + ": true",
        "key_id" + ": 12",  # short number after a key-id name
        "token" + "=AB12_1d",  # weak tier: has an interval segment, so it reads as a market key
    ],
)
def test_known_unmasked(text: str) -> None:
    """Documented limits in the ``logging.py`` docstring: kept readable on purpose."""
    assert redact_text(text) == text
