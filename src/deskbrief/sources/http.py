"""Shared HTTP client (spec §5.1, §5.3, §11; card T03).

``DefaultHttpClient`` implements :class:`deskbrief.sources.base.HttpClient`:

* one token-bucket rate limiter per source, built from ``SourceSettings.rate_per_min``
* retry with exponential backoff + jitter on timeouts, connection errors, 5xx and 429;
  ``Retry-After`` (seconds or HTTP-date) is honoured; 401/403/404 and other 4xx are never retried
* failures come back as ``RawResponse.error`` (never raised); ``attempts`` records tries
* secrets (``secret_params`` / ``secret_headers``) go on the wire only: they never reach logs,
  the ``FetchRequest`` or the ``RawResponse``. ``url_redacted`` is built from public params only
* an optional daily request budget (Twelve Data); once spent, no HTTP call is made and the
  response has ``status=FetchStatus.BUDGET_EXHAUSTED``

Disabled sources (``enabled: false``): no ``FetchStatus`` describes "disabled", so the registry
(``sources/registry.py``) never builds them and no request is ever planned. As a backstop the
client refuses to send for a disabled source and returns an ``ERROR`` response saying so.

Redirects are never followed (``follow_redirects=False`` on every request, injected clients
included): a redirect target could receive secret query params.

Budget persistence (wired by the runner, card T19)
--------------------------------------------------
This module only knows the ``BudgetStore`` protocol. The runner supplies ``load``/``save``
callables over the ``StorageBackend``, keyed by ``deskbrief.store.backend.budget_key``::

    def load(source, day):  # key: logs/budget/<source>/date=YYYY-MM-DD.json
        try:
            return json.loads(store.get_bytes(budget_key(source, day)))["used"]
        except KeyNotFoundError:
            return 0

    def save(source, day, used):
        data = json.dumps({"used": used}).encode()
        store.put_bytes(budget_key(source, day), data, overwrite=True)

and builds ``BudgetTracker.from_settings(CallableBudgetStore(load, save), config.sources)``.
``day`` is the UTC date, since provider quotas reset at UTC midnight. Every HTTP attempt,
retries included, consumes one unit. Weighting batch requests per symbol is left to the
source card.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import quote, quote_plus, urlencode

import httpx

from deskbrief.config import SourceSettings
from deskbrief.ids import new_id
from deskbrief.logging import MASK, get_logger, redact
from deskbrief.models import FetchRequest, FetchStatus, RawResponse
from deskbrief.timeutil import ensure_utc, utc_now

DEFAULT_USER_AGENT = "desk-brief/0.1 (+https://github.com/allenvarghese05/desk-brief)"
#: 429 plus the 5xx family. 401/403/404 and every other 4xx are final.
RETRY_STATUSES = frozenset({429, *range(500, 600)})

# ---------------------------------------------------------------------------------------------
# Daily budget
# ---------------------------------------------------------------------------------------------


class BudgetStore(Protocol):
    """Persistence for the per-source, per-day request count (implemented by the runner)."""

    def load(self, source: str, day: date) -> int: ...

    def save(self, source: str, day: date, used: int) -> None: ...


class InMemoryBudgetStore:
    """Process-local store, for tests and for runs without persistence."""

    def __init__(self) -> None:
        self._used: dict[tuple[str, date], int] = {}

    def load(self, source: str, day: date) -> int:
        return self._used.get((source, day), 0)

    def save(self, source: str, day: date, used: int) -> None:
        self._used[(source, day)] = used


class CallableBudgetStore:
    """Adapts plain ``load``/``save`` callables to :class:`BudgetStore`."""

    def __init__(
        self,
        load: Callable[[str, date], int],
        save: Callable[[str, date, int], None],
    ) -> None:
        self._load, self._save = load, save

    def load(self, source: str, day: date) -> int:
        return self._load(source, day)

    def save(self, source: str, day: date, used: int) -> None:
        self._save(source, day, used)


class BudgetTracker:
    """Counts requests per (source, UTC day) against ``limits``; sources without a limit pass."""

    def __init__(self, store: BudgetStore, limits: Mapping[str, int]) -> None:
        self._store = store
        self._limits = dict(limits)
        self._lock = threading.Lock()

    @classmethod
    def from_settings(
        cls, store: BudgetStore, sources: Mapping[str, SourceSettings]
    ) -> BudgetTracker:
        limits = {n: s.daily_budget for n, s in sources.items() if s.daily_budget is not None}
        return cls(store, limits)

    def used(self, source: str, day: date) -> int:
        return self._store.load(source, day)

    def try_consume(self, source: str, day: date) -> bool:
        """Reserve one request. False (and nothing saved) when the day's budget is spent."""
        limit = self._limits.get(source)
        if limit is None:
            return True
        with self._lock:
            used = self._store.load(source, day)
            if used >= limit:
                return False
            self._store.save(source, day, used + 1)
            return True


# ---------------------------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------------------------


class TokenBucket:
    """Token bucket: ``rate_per_min`` tokens/minute, at most ``burst`` banked.

    Thread-safe. ``acquire`` reserves a token under the lock and sleeps outside it, so
    concurrent callers queue up in order.
    """

    def __init__(
        self,
        rate_per_min: float,
        burst: float,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._rate = rate_per_min / 60.0
        self._capacity = max(1.0, burst)
        self._tokens = self._capacity
        self._mono = monotonic
        self._sleep = sleep
        self._last = monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a token is available. Returns seconds waited."""
        with self._lock:
            now = self._mono()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            self._tokens -= 1.0
            wait = -self._tokens / self._rate if self._tokens < 0 else 0.0
        if wait > 0:
            self._sleep(wait)
        return wait


# ---------------------------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------------------------


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """``Retry-After`` as seconds from ``now``: delta-seconds or an HTTP-date. None if invalid."""
    if value is None:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = ensure_utc(parsedate_to_datetime(text))
        except (TypeError, ValueError):
            return None
        return max(0.0, (when - now).total_seconds())
    return seconds if 0 <= seconds < float("inf") else None


MIN_SECRET_LEN = 3  # shorter values would mangle ordinary text; real keys are far longer


def secret_forms(value: str) -> list[str]:
    """Every textual shape a secret value can take inside an error message.

    Raw, stripped (pasted secrets often carry a newline), ``repr()``-escaped (h11 prints
    ``b'abc\\n'``; the inner text has literal backslash-n), as bytes repr, URL-quoted and
    quote_plus. Longest first so a long form is masked before its prefix.
    """
    forms: set[str] = set()
    for v in {value, value.strip()}:
        if len(v) < MIN_SECRET_LEN:
            continue
        forms |= {
            v,
            repr(v)[1:-1],
            repr(v.encode())[2:-1],
            quote(v, safe=""),
            quote(v),
            quote_plus(v),
        }
    return sorted((f for f in forms if f), key=len, reverse=True)


def mask_secrets(text: str, secrets: Iterable[str]) -> str:
    """Replace every form of every secret value in ``text`` with ``***``, then apply redact()."""
    for secret in secrets:
        for form in secret_forms(secret):
            text = text.replace(form, MASK)
    return redact(text)


@dataclass(frozen=True)
class _Outcome:
    """What the last attempt produced (becomes the RawResponse fields)."""

    http_status: int | None = None
    content_type: str = ""
    body: bytes = b""
    error: str | None = None


class DefaultHttpClient:
    """httpx-based :class:`~deskbrief.sources.base.HttpClient`. See the module docstring."""

    def __init__(
        self,
        sources: Mapping[str, SourceSettings],
        *,
        budget: BudgetTracker | None = None,
        client: httpx.Client | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = utc_now,
        rng: Callable[[], float] = random.random,
        burst: float = 1.0,
        base_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        max_retry_after_s: float = 120.0,
    ) -> None:
        self._settings = dict(sources)
        self._budget = budget
        self._client = client or httpx.Client(follow_redirects=False)
        self._sleep, self._now, self._rng = sleep, now, rng
        self._base = base_backoff_s
        self._max_backoff = max_backoff_s
        self._max_retry_after = max_retry_after_s
        self._buckets = {
            name: TokenBucket(s.rate_per_min, burst, monotonic, sleep)
            for name, s in self._settings.items()
        }

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DefaultHttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- helpers --------------------------------------------------------------------------

    def _backoff(self, retry_number: int) -> float:
        """Exponential backoff with up to 25% jitter; the cap applies after jitter."""
        raw = self._base * 2.0 ** (retry_number - 1) * (1.0 + 0.25 * self._rng())
        return min(self._max_backoff, raw)

    @staticmethod
    def _url_redacted(url: str, params: Mapping[str, str] | None) -> str:
        """Public params only, then masked (a secret smuggled into ``url`` is still masked)."""
        full = f"{url}{'&' if '?' in url else '?'}{urlencode(dict(params))}" if params else url
        return redact(full)

    # -- HttpClient -----------------------------------------------------------------------

    def get(
        self,
        req: FetchRequest,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        secret_params: Mapping[str, str] | None = None,
        secret_headers: Mapping[str, str] | None = None,
    ) -> RawResponse:
        log = get_logger(source=req.source, dataset=req.dataset)
        secrets = [*(secret_params or {}).values(), *(secret_headers or {}).values()]
        url_redacted = mask_secrets(self._url_redacted(url, params), secrets)
        requested_at = self._now()

        def error_text(exc: Exception) -> str:
            """Error message with every secret value masked (before outcome, logs, response)."""
            return mask_secrets(f"{type(exc).__name__}: {exc}", secrets)

        def finish(attempts: int, out: _Outcome, status: FetchStatus | None = None) -> RawResponse:
            """``attempts`` counts HTTP attempts, except that it is at least 1 (model invariant):
            a budget-exhausted or disabled/unknown-source response with no HTTP call reports 1."""
            raw = RawResponse(
                request=req,
                fetch_id=new_id(),
                requested_at=requested_at,
                responded_at=self._now(),
                http_status=out.http_status,
                content_type=out.content_type,
                body=out.body,
                error=out.error,
                attempts=attempts,
                url_redacted=url_redacted,
                status=status,
            )
            log.info(
                "http.done",
                fetch_id=raw.fetch_id,
                url=url_redacted,
                status=str(raw.fetch_status),
                http_status=raw.http_status,
                attempts=attempts,
                latency_ms=raw.latency_ms,
                error=raw.error,
            )
            return raw

        settings = self._settings.get(req.source)
        if settings is None:
            return finish(1, _Outcome(error=f"no source settings for {req.source!r}"))
        if not settings.enabled:
            return finish(1, _Outcome(error=f"source {req.source!r} is disabled; no request made"))

        send_headers = {"User-Agent": settings.user_agent or DEFAULT_USER_AGENT}
        send_headers.update(headers or {})
        send_headers.update(secret_headers or {})
        send_params = {**(params or {}), **(secret_params or {})}
        bucket = self._buckets[req.source]
        max_attempts = settings.retries + 1

        def reserve() -> bool:
            """Take one unit of the daily budget (UTC day of now). True if no budget applies."""
            if self._budget is None:
                return True
            return self._budget.try_consume(req.source, ensure_utc(self._now()).date())

        def exhausted(made: int, last: _Outcome) -> RawResponse:
            log.warning("http.budget_exhausted", url=url_redacted, attempts_made=made)
            detail = "no request made" if made == 0 else "gave up retrying"
            return finish(
                max(1, made),  # attempts >= 1 by contract; 0 HTTP calls is told by `error`
                _Outcome(
                    last.http_status,
                    last.content_type,
                    last.body,
                    f"daily request budget exhausted; {detail}",
                ),
                FetchStatus.BUDGET_EXHAUSTED,
            )

        attempt = 0
        out = _Outcome()
        if not reserve():
            return exhausted(0, out)
        while True:
            attempt += 1
            bucket.acquire()
            started = self._now()
            retry_after: float | None = None
            try:
                resp = self._client.get(
                    url,
                    params=send_params or None,
                    headers=send_headers,
                    timeout=settings.timeout_s,
                    follow_redirects=False,  # also for an injected client: secrets must not follow
                )
            except (httpx.UnsupportedProtocol, httpx.LocalProtocolError) as exc:
                # deterministic (bad scheme, illegal header value): retrying cannot help
                out = _Outcome(error=error_text(exc))
                log.warning("http.attempt", attempt=attempt, url=url_redacted, error=out.error)
                return finish(attempt, out)
            except httpx.TransportError as exc:  # timeouts, connect/read errors: retryable
                out = _Outcome(error=error_text(exc))
            except (httpx.HTTPError, httpx.InvalidURL) as exc:  # malformed request: final
                out = _Outcome(error=error_text(exc))
                log.warning("http.attempt", attempt=attempt, url=url_redacted, error=out.error)
                return finish(attempt, out)
            else:
                code = resp.status_code
                out = _Outcome(
                    code,
                    resp.headers.get("content-type", ""),
                    resp.content,
                    None if resp.is_success else f"HTTP {code}",
                )
                if resp.is_success or code not in RETRY_STATUSES:
                    (log.info if resp.is_success else log.warning)(
                        "http.attempt",
                        attempt=attempt,
                        url=url_redacted,
                        http_status=code,
                        latency_ms=int((self._now() - started).total_seconds() * 1000),
                    )
                    return finish(attempt, out)
                retry_after = parse_retry_after(resp.headers.get("retry-after"), self._now())

            will_retry = attempt < max_attempts
            if retry_after is not None and retry_after > self._max_retry_after:
                will_retry = False  # server asks for longer than we are willing to wait
            delay = retry_after if retry_after is not None else self._backoff(attempt)
            log.warning(
                "http.attempt",
                attempt=attempt,
                url=url_redacted,
                http_status=out.http_status,
                error=out.error,
                retry_in_s=round(delay, 3) if will_retry else None,
            )
            if not will_retry:
                return finish(attempt, out)
            if not reserve():  # check the budget before sleeping, not after
                return exhausted(attempt, out)
            self._sleep(delay)
