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
