"""Helpers for pipeline tests (importable as ``pipeline_fakes``).

SYNTHETIC FIXTURE: every source, payload and price here is made up for tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from config_support import enable_all_sources
from deskbrief.config import Config, load_config
from deskbrief.models import FetchRequest, FetchStatus, JobKind, RawResponse, RunContext
from deskbrief.pipeline.jobs import JobSteps
from deskbrief.pipeline.runner import RunEnv
from deskbrief.schemas import empty_frame, to_frame
from deskbrief.sources.base import HttpClient, ParseError
from deskbrief.store.local import LocalBackend

REPO_ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 11, 16, 11, 20, tzinfo=UTC)  # Monday 06:20 ET


def load_test_config() -> Config:
    return enable_all_sources(load_config(REPO_ROOT / "config.yaml"))


class FakeClock:
    """Deterministic clock: each call advances one second."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class NoHttp:
    """HttpClient that fails the test if used (fake sources never call it)."""

    def get(self, req: FetchRequest, url: str, **kw: object) -> RawResponse:  # pragma: no cover
        raise AssertionError("unexpected HTTP call")


def obs_frame(
    instrument: str, source: str, dataset: str, as_of: datetime, value: float
) -> pd.DataFrame:
    """SYNTHETIC FIXTURE: one parsed (pre-lineage) observation row."""
    return to_frame(
        "parsed_observations",
        [
            {
                "instrument_id": instrument,
                "source": source,
                "dataset": dataset,
                "symbol": instrument,
                "tier": 3,
                "field": "close",
                "interval": "1d",
                "as_of": as_of,
                "value": value,
                "unit": "USD",
                "is_derived": False,
                "is_fallback": False,
            }
        ],
    )


class FakeSource:
    """SYNTHETIC FIXTURE: a source driven by a payload; the body is JSON ``[[iso, value], ...]``.

    ``mode`` selects the fault: ``ok``, ``raise_fetch``, ``raise_plan``, ``http_429``,
    ``http_500``, ``corrupt`` (unparseable body), ``bad_schema`` (valid JSON, NaN value breaks
    the schema), ``empty`` (200, empty body), ``budget``, ``parser_bug`` (parse raises KeyError).
    """

    parser_version = "fake-1"

    def __init__(
        self,
        name: str,
        *,
        mode: str = "ok",
        points: list[tuple[datetime, float]] | None = None,
        instrument: str = "SPY",
        dataset: str = "daily",
        requests: int = 1,
        windowed: bool = False,
    ) -> None:
        self.name = name
        self.mode = mode
        self.instrument = instrument
        self.dataset = dataset
        self.points = points or [(datetime(2026, 11, 13, 5, 0, tzinfo=UTC), 450.0)]
        self.n_requests = requests
        self.windowed = windowed  # parse() honours request.target_date, like the Cboe parser
        self.plans: list[tuple[JobKind, date]] = []

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        self.plans.append((job, ctx.target_date))
        if self.mode == "raise_plan":
            raise RuntimeError("plan exploded")
        return [
            FetchRequest(
                self.name,
                self.dataset,
                params={"n": str(i)},
                symbols=(self.instrument,),
                target_date=ctx.target_date,
            )
            for i in range(self.n_requests)
        ]

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        if self.mode == "raise_fetch":
            raise ConnectionError("socket on fire")
        t = datetime.now(UTC)
        body = json.dumps([[ts.isoformat(), v] for ts, v in self.points]).encode()
        status: FetchStatus | None = None
        http_status: int | None = 200
        error: str | None = None
        content_type = "application/json"
        if self.mode == "corrupt":
            body = b'[["2026-11-13T05:00:00+00:00", 450.0'  # truncated JSON
        elif self.mode == "bad_schema":
            body = json.dumps([[self.points[0][0].isoformat(), float("nan")]]).encode()
        elif self.mode == "http_429":
            http_status, body, error = 429, b"", "HTTP 429 Too Many Requests (after 3 attempts)"
        elif self.mode == "http_500":
            http_status, body, error = 500, b"upstream broke", "HTTP 500"
        elif self.mode == "empty":
            body = b""
        elif self.mode == "budget":
            http_status, body, status = None, b"", FetchStatus.BUDGET_EXHAUSTED
            error = "daily budget exhausted"
        return RawResponse(
            request=req,
            fetch_id=f"{self.name}-{req.params.get('n', '0')}-{t.timestamp():.6f}",
            requested_at=t,
            responded_at=t,
            http_status=http_status,
            content_type=content_type,
            body=body,
            error=error,
            status=status,
            url_redacted=f"https://example.invalid/{self.name}",
        )

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        if self.mode == "parser_bug":
            raise KeyError("column that does not exist")
        try:
            rows = json.loads(raw.body)
        except json.JSONDecodeError as exc:
            raise ParseError(f"corrupted JSON: {exc}") from exc
        if not rows:
            return empty_frame("parsed_observations")
        if self.windowed and raw.request.target_date is not None:
            lo = raw.request.target_date - timedelta(days=3)
            hi = raw.request.target_date
            rows = [r for r in rows if lo <= datetime.fromisoformat(r[0]).date() <= hi]
        if not rows:
            return empty_frame("parsed_observations")
        frames = [
            obs_frame(self.instrument, self.name, self.dataset, datetime.fromisoformat(ts), v)
            for ts, v in rows
        ]
        return pd.concat(frames, ignore_index=True)


def make_env(
    tmp_path: Path,
    sources: list[FakeSource],
    *,
    clock: Callable[[], datetime] | None = None,
    env: dict[str, str] | None = None,
    config: Config | None = None,
    real_steps: bool = False,
) -> RunEnv:
    """``real_steps=False`` (default) runs no check/serving hooks, so runner tests stay about the
    runner; wiring tests pass ``real_steps=True`` to get ``jobs.steps_for``."""
    env_ = RunEnv(
        config=config or load_test_config(),
        store=LocalBackend(tmp_path / "data"),
        http=NoHttp(),
        sources=sources,
        clock=clock or FakeClock(),
        git_sha="deadbeefcafe",
        env=env or {},
    )
    if not real_steps:
        env_.steps_for = lambda job: JobSteps()
    return env_
