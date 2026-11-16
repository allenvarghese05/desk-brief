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
    assert redact_text("token=abc&x=1") == f"token={MASK}&x=1"
    assert redact_value(["key_id=zzz", 5]) == [f"key_id={MASK}", 5]
    assert redact_value(("a",)) == ("a",)
    assert redact_value({"password": ""}, key=None) == {"password": ""}
    assert redact_value(None, key="token") is None


def test_level_filtering() -> None:
    buf = io.StringIO()
    configure_logging(level="WARNING", stream=buf)
    get_logger().info("hidden")
    get_logger().warning("shown")
    assert "hidden" not in buf.getvalue()
    assert "shown" in buf.getvalue()
