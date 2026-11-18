"""T24B: the intraday gate, the ECB-fix window (P-012), Yahoo-only planning and hook safety.

SYNTHETIC FIXTURE: every source and value is made up; gate, checks and serving are the real ones.
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from pipeline_fakes import FakeClock, FakeSource, make_env

from deskbrief.models import FetchRequest, JobKind, RawResponse, RunStatus, Trigger
from deskbrief.pipeline.jobs import Gate, evaluate_gate
from deskbrief.pipeline.manifest import RunManifest
from deskbrief.pipeline.runner import RunEnv, run_job
from deskbrief.sources.base import HttpClient
from deskbrief.store.backend import serving_key
from deskbrief.store.datasets import Datasets

SCHED_12_15 = "15 12 * * 1-5"
SCHED_QUARTERLY = "*/15 13-21 * * 1-5"
YAHOO_ONLY = frozenset({"yahoo"})


def utc(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def gate(now: datetime, scheduled_for: datetime | None = None) -> Gate:
    return evaluate_gate(JobKind.INTRADAY, Trigger.SCHEDULE, now, scheduled_for)


# --- the fix window -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "when"),
    [
        ("CEST weekday 12:15 UTC", utc(2026, 7, 15, 12, 15)),
        ("CET weekday 13:15 UTC", utc(2026, 11, 17, 13, 15)),
        ("Thanksgiving (XNYS closed, TARGET open) 13:15 UTC", utc(2026, 11, 26, 13, 15)),
        ("July 4 observed (Fri 3 Jul 2026), CEST 12:15 UTC", utc(2026, 7, 3, 12, 15)),
        ("edge of the window, +10 min", utc(2026, 7, 15, 12, 25)),
        ("edge of the window, -10 min", utc(2026, 7, 15, 12, 5)),
    ],
)
def test_fix_window_proceeds_yahoo_only_outside_the_session(label: str, when: datetime) -> None:
    g = gate(when)
    assert g.proceed, label
    assert g.sources == YAHOO_ONLY, label


@pytest.mark.parametrize(
    ("label", "when"),
    [
        ("CET weekday at 12:15 UTC (13:15 Berlin)", utc(2026, 11, 17, 12, 15)),
        ("CEST weekday at 13:15 UTC (15:15 Berlin)", utc(2026, 7, 15, 13, 15)),
        ("CET run at 13:45 UTC, outside the session", utc(2026, 11, 17, 13, 45)),
        ("window edge +11 min", utc(2026, 7, 15, 12, 26)),
        ("TARGET holiday, 1 May 2026 (XNYS open), 12:15 UTC", utc(2026, 5, 1, 12, 15)),
        ("TARGET and XNYS holiday, 25 Dec 2026, 13:15 UTC", utc(2026, 12, 25, 13, 15)),
        ("Saturday at the fix", utc(2026, 11, 14, 13, 15)),
    ],
)
def test_other_triggers_outside_the_session_are_skipped(label: str, when: datetime) -> None:
    g = gate(when)
    assert not g.proceed, label
    assert g.sources is None
    assert g.reason


def test_in_session_runs_are_unrestricted() -> None:
    g = gate(utc(2026, 11, 16, 15, 0))
    assert g.proceed
    assert g.sources is None


def test_after_close_is_skipped() -> None:
    assert not gate(utc(2026, 11, 16, 22, 0)).proceed


def test_delayed_run_is_gated_on_scheduled_for() -> None:
    """Scheduled 12:15 UTC (CEST fix), started 12:40: still the fix run."""
    g = gate(utc(2026, 7, 15, 12, 40), scheduled_for=utc(2026, 7, 15, 12, 15))
    assert g.proceed
    assert g.sources == YAHOO_ONLY


def test_wall_clock_is_used_when_scheduled_for_is_unknown() -> None:
    assert gate(utc(2026, 7, 15, 12, 20)).proceed
    assert not gate(utc(2026, 7, 15, 12, 40)).proceed


def test_scheduled_for_outside_the_window_does_not_exempt_a_run_that_started_inside() -> None:
    """scheduled_for beats the wall clock: a run started at 13:16 for a 13:45 fire is skipped."""
    assert not gate(utc(2026, 11, 17, 13, 16), scheduled_for=utc(2026, 11, 17, 13, 45)).proceed


def test_manual_trigger_is_never_gated() -> None:
    g = evaluate_gate(JobKind.INTRADAY, Trigger.MANUAL, utc(2026, 11, 14, 3, 0))
    assert g == Gate(True)
    assert g.sources is None


def test_other_jobs_are_not_affected() -> None:
    assert evaluate_gate(JobKind.EOD, Trigger.SCHEDULE, utc(2026, 11, 17, 22, 30)).sources is None
    assert not evaluate_gate(JobKind.MORNING, Trigger.SCHEDULE, utc(2026, 11, 17, 13, 15)).proceed


# --- Yahoo-only planning through the runner ---------------------------------------------------


PRIOR_CLOSE = [(utc(2026, 11, 12, 21, 0), 450.0)]  # the day before the seed run


class StampedSource(FakeSource):
    """FakeSource whose responses carry the injected clock, not the real one, so what a run
    ingests is visible to point-in-time reads at the test's fake ``now``."""

    clock: FakeClock

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        raw = super().fetch(req, http)
        t = self.clock()
        return replace(raw, requested_at=t, responded_at=t)


CLOCK = FakeClock()


def three_sources(points: list[tuple[datetime, float]] = PRIOR_CLOSE) -> list[FakeSource]:
    out = [
        StampedSource("alpaca", dataset="iex_latest", instrument="SPY", points=points),
        StampedSource("twelvedata", dataset="fx_quote", instrument="EURUSD", points=points),
        StampedSource("yahoo", dataset="quote", instrument="ES_F", points=points),
    ]
    for s in out:
        s.clock = lambda: CLOCK.now
    return out


def sched(
    env: RunEnv, now: datetime, cron: str = SCHED_QUARTERLY, *, dry_run: bool = False
) -> RunManifest:
    env.clock = CLOCK
    CLOCK.now = now - timedelta(seconds=1)
    env.env = {"GITHUB_EVENT_NAME": "schedule", "GITHUB_EVENT_SCHEDULE": cron}
    return run_job(env, JobKind.INTRADAY, dry_run=dry_run)


@pytest.mark.parametrize(
    ("when", "cron"),
    [
        (utc(2026, 7, 15, 12, 15), SCHED_12_15),  # CEST
        (utc(2026, 11, 17, 13, 15), SCHED_QUARTERLY),  # CET
        (utc(2026, 11, 26, 13, 15), SCHED_QUARTERLY),  # Thanksgiving
        (utc(2026, 7, 15, 12, 40), SCHED_12_15),  # delayed 25 min
    ],
)
def test_exempted_runs_plan_yahoo_requests_only(tmp_path: Path, when: datetime, cron: str) -> None:
    env = make_env(tmp_path, three_sources())
    m = sched(env, when, cron, dry_run=True)
    assert m.planned == ["yahoo/quote (ES_F)"]


def test_in_session_run_plans_every_source(tmp_path: Path) -> None:
    env = make_env(tmp_path, three_sources())
    m = sched(env, utc(2026, 11, 16, 15, 0), dry_run=True)
    assert {p.split("/")[0] for p in m.planned} == {"alpaca", "twelvedata", "yahoo"}


def test_exempted_run_fetches_only_yahoo(tmp_path: Path) -> None:
    sources = three_sources()
    env = make_env(tmp_path, sources)
    m = sched(env, utc(2026, 11, 17, 13, 15))
    assert m.status is RunStatus.OK
    assert sources[0].plans == []
    assert sources[1].plans == []
    assert len(sources[2].plans) == 1
    assert set(Datasets(env.store).read("source_log")["source"]) == {"yahoo"}


def test_skipped_run_plans_nothing(tmp_path: Path) -> None:
    sources = three_sources()
    env = make_env(tmp_path, sources)
    m = sched(env, utc(2026, 11, 17, 12, 15), SCHED_12_15)  # winter: 13:15 Berlin
    assert m.skipped_reason
    assert m.status is None
    assert all(s.plans == [] for s in sources)


def test_manual_dispatch_runs_all_sources_outside_the_session(tmp_path: Path) -> None:
    sources = three_sources()
    env = make_env(tmp_path, sources, clock=FakeClock(utc(2026, 11, 14, 3, 0)))
    run_job(env, JobKind.INTRADAY, trigger=Trigger.MANUAL)
    assert all(len(s.plans) == 1 for s in sources)


# --- hook safety: freshness and serving ---------------------------------------------------------


def snapshot(env: RunEnv) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(env.store.get_bytes(serving_key("latest_snapshot.parquet"))))


@pytest.mark.parametrize(
    ("fix_time", "cron"),
    [
        (utc(2026, 11, 26, 13, 15), SCHED_QUARTERLY),  # Thanksgiving: XNYS holiday
        (utc(2026, 11, 17, 13, 15), SCHED_QUARTERLY),  # CET weekday, before the open
        (utc(2026, 7, 15, 12, 15), SCHED_12_15),  # CEST weekday, before the open
    ],
)
def test_exempted_run_keeps_other_sources_freshness_and_the_serving_view(
    tmp_path: Path, fix_time: datetime, cron: str
) -> None:
    seed_day = (fix_time - timedelta(days=1)).replace(hour=15, minute=0)  # in-session, all sources
    sources = three_sources([(seed_day - timedelta(hours=18), 450.0)])
    env = make_env(tmp_path, sources, real_steps=True)
    assert sched(env, seed_day).status is RunStatus.OK
    before = snapshot(env)
    assert {"SPY", "EURUSD", "ES_F"} <= set(before["instrument_id"])

    m = sched(env, fix_time, cron)
    assert m.status in (RunStatus.OK, RunStatus.DEGRADED)

    dq = Datasets(env.store).read("dq_results")
    mine = dq[dq["run_id"] == m.run_id]
    other = mine[
        mine["source"].isin(["alpaca", "twelvedata"]) & (mine["check_type"] == "freshness")
    ]
    assert not other.empty  # the check did speak about them ...
    assert set(other["status"]) == {"skip"}  # ... and only to say "not judged now"

    after = snapshot(env)
    assert {"SPY", "EURUSD", "ES_F"} <= set(after["instrument_id"])  # not a Yahoo-only view
    assert len(after) >= len(before)
