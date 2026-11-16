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
    r"(api[_-]?key|apikey|token|secret|password|passwd|authorization|key[_-]?id|access[_-]?key)",
    re.IGNORECASE,
)
_INLINE_RE = re.compile(
    r"(?P<name>api[_-]?key|apikey|token|secret|password|key[_-]?id|access[_-]?key"
    r"|APCA-API-KEY-ID|APCA-API-SECRET-KEY)(?P<sep>\s*[=:]\s*)(?P<val>[^&\s,;\"']+)",
    re.IGNORECASE,
)


def _env_secrets(names: Iterable[str]) -> list[str]:
    vals = [os.environ.get(n, "") for n in names]
    # ignore very short values to avoid masking ordinary words
    return sorted({v for v in vals if len(v) >= 6}, key=len, reverse=True)


def redact_text(text: str, env_secrets: Iterable[str] = ()) -> str:
    """Mask inline ``name=value`` secrets and any literal secret values in ``text``."""
    out = _INLINE_RE.sub(lambda m: f"{m.group('name')}{m.group('sep')}{MASK}", text)
    for secret in env_secrets:
        out = out.replace(secret, MASK)
    return out


def redact_value(value: Any, env_secrets: Iterable[str] = (), key: str | None = None) -> Any:
    """Recursively redact a log value. Secret-looking keys have their whole value masked."""
    secrets = list(env_secrets)
    if key is not None and _SECRET_KEY_RE.search(key) and value not in (None, ""):
        return MASK
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {k: redact_value(v, secrets, key=str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact_value(v, secrets) for v in value)
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact every field of the event."""
    secrets = _env_secrets(SECRET_ENV_VARS)
    for k in list(event_dict):
        event_dict[k] = redact_value(event_dict[k], secrets, key=None if k == "event" else k)
    return event_dict


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


def get_logger(**bindings: Any) -> Any:
    """A bound structlog logger. Bind ``run_id``, ``job``, ``source``, ``fetch_id`` as known."""
    return structlog.get_logger().bind(**bindings)
