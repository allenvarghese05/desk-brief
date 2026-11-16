"""Structured JSON logging with secret redaction.

Every log line is one JSON object (structlog). Before rendering, a redaction processor masks:

* values of keys that look secret (``api_key``, ``token``, ``secret``, ``password``, ...)
* ``name=value`` secrets inside strings (URLs, query strings, headers)
* the literal values of known secret environment variables, wherever they appear

Usage::

    from deskbrief.logging import configure_logging, get_logger
    configure_logging()
    log = get_logger(run_id=ctx.run_id, source="fred")
    log.info("fetch.done", status="ok", url=raw.url_redacted)
"""

from __future__ import annotations

import logging as _stdlib_logging
import os
import re
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any, TextIO
from urllib.parse import quote, quote_plus, unquote_plus

import structlog

MASK = "***"

SECRET_ENV_VARS: tuple[str, ...] = (
    "ALPACA_KEY_ID",
    "ALPACA_SECRET_KEY",
    "TWELVEDATA_API_KEY",
    "FRED_API_KEY",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_READER_ACCESS_KEY_ID",
    "R2_READER_SECRET_ACCESS_KEY",
    "SMTP_APP_PASSWORD",
    "HEALTHCHECK_URL_MORNING",
    "HEALTHCHECK_URL_EOD",
)

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|token|secret|password|passwd|authorization|cookie|key[_-]?id"
    r"|access[_-]?key)",
    re.IGNORECASE,
)
_NAMES = (
    r"api[_-]?key|apikey|token|secret|password|passwd|key[_-]?id|access[_-]?key|key|auth"
    r"|signature|sig|x-amz-signature|x-amz-credential|x-amz-security-token"
    r"|APCA-API-KEY-ID|APCA-API-SECRET-KEY"
)
_INLINE_RE = re.compile(
    rf"(?<![A-Za-z0-9_\-])(?P<name>{_NAMES})(?P<q1>[\"']?)(?P<sep>\s*[=:]\s*)(?P<q2>[\"']?)(?P<val>[^&\s,;\"'}}]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(
    r"\b(bearer)\s+[A-Za-z0-9._~+/=\-]{6,}|\b(basic)\s+[A-Za-z0-9+/]{12,}={0,2}", re.IGNORECASE
)
_HEADER_LINE_RE = re.compile(r"\b(authorization|cookie|set-cookie)(\s*:\s*)[^\r\n]+", re.I)
_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^/\s@]+@", re.IGNORECASE)


def _env_secrets(names: Iterable[str]) -> list[str]:
    vals = [os.environ.get(n, "") for n in names]
    # ignore very short values to avoid masking ordinary words
    return sorted({v for v in vals if len(v) >= 6}, key=len, reverse=True)


def redact_text(text: str, env_secrets: Iterable[str] = ()) -> str:
    """Mask secrets in free text.

    Covers ``name=value`` / ``"name": "value"`` pairs (also URL-encoded), ``Bearer``/``Basic``
    tokens, ``Authorization``/``Cookie`` header lines, ``user:pass@`` in URLs, and the literal
    (or URL-encoded) values of known secret env vars. If anything inside a URL-encoded form was
    masked, the decoded, masked form is returned (safety over fidelity).
    """
    secrets = list(env_secrets)

    def _mask(t: str) -> str:
        t = _USERINFO_RE.sub(lambda m: f"{m.group(1)}{MASK}@", t)
        t = _HEADER_LINE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", t)
        t = _BEARER_RE.sub(lambda m: f"{m.group(1) or m.group(2)} {MASK}", t)
        t = _INLINE_RE.sub(
            lambda m: f"{m.group('name')}{m.group('q1')}{m.group('sep')}{m.group('q2')}{MASK}", t
        )
        for secret in secrets:
            for form in (secret, quote(secret, safe=""), quote_plus(secret)):
                t = t.replace(form, MASK)
        return t

    out = _mask(text)
    if "%" in out:
        decoded = unquote_plus(out)
        masked_decoded = _mask(decoded)
        if masked_decoded != decoded:
            return masked_decoded
    return out


def redact_value(value: Any, env_secrets: Iterable[str] = (), key: str | None = None) -> Any:
    """Recursively redact a log value. Secret-looking keys have their whole value masked.

    Non-JSON-native values (exceptions, bytes, sets, dataclasses, ...) are converted to text
    and redacted, so the JSON renderer never falls back to an unredacted ``repr()``.
    """
    secrets = list(env_secrets)
    if value is None or isinstance(value, bool | int | float):
        return value  # numbers are never secrets (e.g. requests_per_token=1)
    if key is not None and _SECRET_KEY_RE.search(key) and value != "":
        return MASK
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, bytes | bytearray):
        return redact_text(bytes(value).decode("utf-8", errors="replace"), secrets)
    if isinstance(value, Mapping):
        return {str(k): redact_value(v, secrets, key=str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_value(v, secrets) for v in value]
    return redact_text(repr(value) if not isinstance(value, BaseException) else str(value), secrets)


def redact(text: str) -> str:
    """Redact free text using the inline rules plus current secret env values."""
    return redact_text(text, _env_secrets(SECRET_ENV_VARS))


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact every field of the event."""
    secrets = _env_secrets(SECRET_ENV_VARS)
    for k in list(event_dict):
        event_dict[k] = redact_value(event_dict[k], secrets, key=None if k == "event" else k)
    return event_dict


class RedactingFilter(_stdlib_logging.Filter):
    """stdlib logging filter: redacts the fully formatted message of third-party loggers."""

    def filter(self, record: _stdlib_logging.LogRecord) -> bool:
        msg = redact_text(record.getMessage(), _env_secrets(SECRET_ENV_VARS))
        record.msg, record.args = msg, None
        if record.exc_info:
            record.exc_text = redact_text(
                _stdlib_logging.Formatter().formatException(record.exc_info),
                _env_secrets(SECRET_ENV_VARS),
            )
            record.exc_info = None
        return True


#: Third-party loggers that print full request URLs at INFO/DEBUG.
NOISY_LOGGERS: tuple[str, ...] = ("httpx", "httpcore", "urllib3", "botocore", "yfinance")


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Configure structlog for JSON output to ``stream`` (default stderr). Idempotent."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_processor,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            _stdlib_logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )
    # stdlib/third-party logs: WARNING+ only for noisy libraries, and always redacted
    root = _stdlib_logging.getLogger()
    existing = [h for h in root.handlers if isinstance(h, _RedactedHandler)]
    if existing:
        existing[0].setStream(stream or sys.stderr)
    else:
        handler = _RedactedHandler(stream or sys.stderr)
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
    root.setLevel(level.upper())
    for name in NOISY_LOGGERS:
        _stdlib_logging.getLogger(name).setLevel(_stdlib_logging.WARNING)


class _RedactedHandler(_stdlib_logging.StreamHandler):  # type: ignore[type-arg]
    """Root handler installed by ``configure_logging`` (marker class for idempotency)."""


def get_logger(**bindings: Any) -> Any:
    """A bound structlog logger. Bind ``run_id``, ``job``, ``source``, ``fetch_id`` as known."""
    return structlog.get_logger().bind(**bindings)
