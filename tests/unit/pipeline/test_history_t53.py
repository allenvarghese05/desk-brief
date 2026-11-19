"""T53: the one-time history load (P-020). No network.

SYNTHETIC FIXTURE: every source, payload and price here is made up (``pipeline_fakes``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from pipeline_fakes import REPO_ROOT, FakeSource, load_test_config, make_env

from deskbrief import cli
from deskbrief.models import FetchRequest, JobKind, RunContext, RunStatus, Trigger
from deskbrief.pipeline import history
from deskbrief.pipeline.history import build_plan, default_range, run_history
from deskbrief.pipeline.jobs import JobSteps
from deskbrief.pipeline.runner import make_context
from deskbrief.sources import catalog
from deskbrief.sources.base import supports_range
from deskbrief.sources.registry import get_sources
from deskbrief.store import duck
from deskbrief.store.datasets import Datasets

START, END = date(2026, 9, 1), date(2026, 9, 30)
NOW = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)


def et_midnight(d: date) -> datetime:
    """00:00 America/New_York in September (EDT) as UTC."""
    return datetime(d.year, d.month, d.day, 4, 0, tzinfo=UTC)


def weekdays(start: date, end: date) -> list[date]:
    n = (end - start).days + 1
    return [d for d in (start + timedelta(days=i) for i in range(n)) if d.weekday() < 5]


class RangeSource(FakeSource):
    """SYNTHETIC FIXTURE: a ``FakeSource`` with ``plan_range``; ``plan`` must never be called."""

    def __init__(self, name: str, days: list[date], *, chunks: int = 1, **kw: object) -> None:
        super().__init__(name, **kw)  # type: ignore[arg-type]
        self.days = days
        self.chunks = chunks
        self.ranges: list[tuple[date, date]] = []

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        raise AssertionError("the history job must call plan_range, never plan")

    def plan_range(self, start: date, end: date, ctx: RunContext) -> list[FetchRequest]:
        self.ranges.append((start, end))
        return [
            FetchRequest(
                self.name,
                self.dataset,
                params={"n": str(i)},
                symbols=(self.instrument,),
                target_date=end,
            )
            for i in range(self.chunks)
        ]

    def fetch(self, req: FetchRequest, http: object) -> object:  # type: ignore[override]
        # each chunk returns its share of the days; chunk 0 also repeats chunk 1's first day
        i = int(req.params["n"])
        mine = self.days[i :: self.chunks]
        if i == 0 and self.chunks > 1 and len(self.days) > 1:
            mine = [*mine, self.days[1]]
        self.points = [(et_midnight(d), 100.0 + d.day) for d in mine]
        return super().fetch(req, http)  # type: ignore[arg-type]


def obs_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "data").glob("clean/*/observations/date=*/run=*.parquet"))


def no_steps(job: JobKind) -> JobSteps:
    raise AssertionError("the history job runs no checks, scorecard, serving or report")


def env_for(tmp_path: Path, sources: list[FakeSource]):  # type: ignore[no-untyped-def]
    env = make_env(tmp_path, sources)
    env.steps_for = no_steps
    return env


# --- plan -----------------------------------------------------------------------------------


def test_default_range_is_two_years_up_to_yesterday() -> None:
    assert default_range(date(2026, 11, 19)) == (date(2024, 11, 18), date(2026, 11, 18))
    assert default_range(date(2028, 3, 1)) == (date(2026, 2, 28), date(2028, 2, 29))


def test_real_sources_are_planned_with_plan_range_never_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-020: FRED's ``plan`` is the 10-observation request; history must use ``plan_range``."""
    config = load_test_config()
    catalog.load_all()
    sources = get_sources(config)

    def boom(*_a: object, **_k: object) -> list[FetchRequest]:
        raise AssertionError("plan() called by the history job")

    for s in sources:
        monkeypatch.setattr(s, "plan", boom)
    env = make_env(tmp_path, [])
    ctx = make_context(env, JobKind.HISTORY, Trigger.MANUAL, END, NOW)
    plan = build_plan(config, sources, date(2024, 11, 18), date(2026, 11, 18), ctx)
    by_source = {sp.source: sp for sp in plan.sources}
    assert {"fred", "treasury", "ecb", "yahoo", "cboe", "cftc"} <= set(by_source)
    assert all(sp.error is None for sp in plan.sources)
    for req in by_source["fred"].requests:
        assert req.params["observation_start"] == "2024-11-18"
        assert req.params["observation_end"] == "2026-11-18"
        assert req.params["limit"] != "10"
    assert len(by_source["treasury"].requests) == 25
    assert len(by_source["ecb"].requests) == 1
    assert {r.dataset for r in by_source["yahoo"].requests} == {"history"}
    skipped = {s.source: s for s in plan.skipped}
    assert skipped["alpaca"].reason == "no range support"
    assert "fx_series_1m" in skipped["twelvedata"].datasets
    assert skipped["twelvedata"].reason == "no range support"
    assert skipped["yahoo"].datasets == ("quote",)


def test_disabled_sources_are_listed() -> None:
    config = load_test_config()
    disabled = config.model_copy(
        update={
            "sources": {
                **config.sources,
                "stooq": config.sources["stooq"].model_copy(update={"enabled": False}),
            }
        }
    )
    env = make_env(Path("/nonexistent"), [])
    ctx = make_context(env, JobKind.HISTORY, Trigger.MANUAL, END, NOW)
    plan = build_plan(disabled, [], START, END, ctx)
    assert [(s.source, s.datasets, s.reason) for s in plan.skipped] == [
        ("stooq", ("daily",), "disabled")
    ]


def test_dry_run_prints_the_plan_and_fetches_and_writes_nothing(tmp_path: Path) -> None:
    src = RangeSource("fred", weekdays(START, END), chunks=2, instrument="DGS10", dataset="series")

    def no_fetch(*_a: object, **_k: object) -> None:
        raise AssertionError("dry run must not fetch")

    src.fetch = no_fetch  # type: ignore[method-assign]
    env = env_for(tmp_path, [src])
    result = run_history(env, start=START, end=END, dry_run=True)
    assert result.manifest.status is RunStatus.OK
    assert not (tmp_path / "data").exists() or not any((tmp_path / "data").rglob("*.*"))
    text = result.to_markdown()
    assert "history plan 2026-09-01 .. 2026-09-30" in text
    assert "| fred | series | 2 | 2 |" in text
    assert "Estimated observation partitions:" in text
    assert "Dry run: nothing fetched, nothing written." in text
    assert src.ranges == [(START, END)]


# --- run ------------------------------------------------------------------------------------


def test_one_observations_file_per_touched_date(tmp_path: Path) -> None:
    days = weekdays(START, END)
    src = RangeSource("fred", days, chunks=3, instrument="DGS10", dataset="series")
    env = env_for(tmp_path, [src])
    result = run_history(env, start=START, end=END)
    assert result.manifest.status is RunStatus.OK
    files = obs_files(tmp_path)
    by_date = {f.parent.name for f in files}
    assert len(files) == len(days) == len(by_date)  # N dates -> N files, one per partition
    assert by_date == {f"date={d.isoformat()}" for d in days}
    stored = Datasets(env.store).read("observations")
    assert len(stored) == len(days)  # the repeated day was written once
    assert result.already_stored == 1
    assert result.manifest.job is JobKind.HISTORY
    runs = Datasets(env.store).read("runs")
    assert list(runs["job"]) == ["history"]
    # isolation: no checks wrote anything
    assert not list((tmp_path / "data").glob("**/dq_results/**/*.parquet"))
    assert not list((tmp_path / "data").glob("**/scorecard*/**/*.parquet"))


def test_a_rerun_writes_nothing_twice(tmp_path: Path) -> None:
    days = weekdays(START, END)
    env = env_for(tmp_path, [RangeSource("fred", days, instrument="DGS10", dataset="series")])
    run_history(env, start=START, end=END)
    before = obs_files(tmp_path)
    again = run_history(env, start=START, end=END)
    assert again.manifest.status is RunStatus.OK
    assert again.manifest.n_obs == 0
    assert again.already_stored == len(days)
    assert obs_files(tmp_path) == before
    assert len(Datasets(env.store).read("observations")) == len(days)


def test_an_interrupted_load_is_completed_by_a_rerun(tmp_path: Path) -> None:
    days = weekdays(START, END)
    first = RangeSource("fred", days[:7], instrument="DGS10", dataset="series")
    env = env_for(tmp_path, [first])
    run_history(env, start=START, end=END)
    env.sources = [RangeSource("fred", days, instrument="DGS10", dataset="series")]
    result = run_history(env, start=START, end=END)
    assert result.manifest.n_obs == len(days) - 7
    assert result.already_stored == 7
    assert len(obs_files(tmp_path)) == len(days)
    stored = Datasets(env.store).read("observations")
    assert not stored.duplicated(["instrument_id", "as_of"]).any()


def test_rows_outside_the_range_are_dropped(tmp_path: Path) -> None:
    days = weekdays(date(2026, 8, 24), date(2026, 10, 9))  # a month plus the edges around it
    env = env_for(tmp_path, [RangeSource("treasury", days, instrument="UST10Y", dataset="x")])
    result = run_history(env, start=START, end=END)
    inside = [d for d in days if START <= d <= END]
    assert result.manifest.n_obs == len(inside)
    assert result.outside_range == len(days) - len(inside)


def test_a_failing_source_costs_only_its_own_rows(tmp_path: Path) -> None:
    days = weekdays(START, END)
    good = RangeSource("fred", days, instrument="DGS10", dataset="series")
    bad = RangeSource("cboe", days, instrument="VIX", dataset="vix_history", mode="http_500")
    env = env_for(tmp_path, [bad, good])
    result = run_history(env, start=START, end=END)
    assert result.manifest.status is RunStatus.DEGRADED
    assert result.exit_code == 0
    assert result.manifest.n_obs == len(days)
    assert result.manifest.sources["cboe"].failed == 1
    log = Datasets(env.store).read("source_log")
    assert set(log["source"]) == {"cboe", "fred"}


def test_a_source_whose_plan_range_raises_is_isolated(tmp_path: Path) -> None:
    good = RangeSource("fred", weekdays(START, END), instrument="DGS10", dataset="series")
    broken = RangeSource("ecb", [], instrument="EURUSD", dataset="eurofxref")

    def explode(*_a: object, **_k: object) -> list[FetchRequest]:
        raise RuntimeError("range exploded")

    broken.plan_range = explode  # type: ignore[method-assign]
    env = env_for(tmp_path, [broken, good])
    result = run_history(env, start=START, end=END)
    assert result.manifest.status is RunStatus.DEGRADED
    assert "plan_range failed" in result.manifest.sources["ecb"].errors[0]
    assert result.manifest.n_obs == len(weekdays(START, END))


def test_sources_without_range_support_are_not_fetched(tmp_path: Path) -> None:
    plain = FakeSource("alpaca", instrument="SPY")
    assert not supports_range(plain)
    good = RangeSource("fred", weekdays(START, END), instrument="DGS10", dataset="series")
    env = env_for(tmp_path, [plain, good])
    result = run_history(env, start=START, end=END)
    assert plain.plans == []  # plan() never called
    assert "alpaca" not in result.manifest.sources
    assert ("alpaca", "no range support") in {(s.source, s.reason) for s in result.plan.skipped}


def test_rows_keep_their_real_ingested_at_so_pit_reads_before_the_load_miss_them(
    tmp_path: Path,
) -> None:
    days = weekdays(START, END)
    env = env_for(tmp_path, [RangeSource("fred", days, instrument="DGS10", dataset="series")])
    before_load = datetime.now(UTC) - timedelta(seconds=1)
    run_history(env, start=START, end=END)
    stored = Datasets(env.store).read("observations")
    assert (pd.to_datetime(stored["ingested_at"], utc=True) > before_load).all()
    con = duck.connect(env.store, env.config)
    seen_before = con.execute(
        "SELECT count(*) FROM obs_pit(?::TIMESTAMPTZ)", [before_load]
    ).fetchone()
    seen_after = con.execute(
        "SELECT count(*) FROM obs_pit(?::TIMESTAMPTZ)", [datetime.now(UTC)]
    ).fetchone()
    assert seen_before == (0,)
    assert seen_after == (len(days),)


def test_progress_is_logged_per_request_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class Rec:
        def __init__(self, **bound: object) -> None:
            self.bound = bound

        def info(self, event: str, **kw: object) -> None:
            events.append((event, {**self.bound, **kw}))

        error = warning = info

    monkeypatch.setattr(history, "get_logger", lambda **kw: Rec(**kw))
    src = RangeSource("fred", weekdays(START, END), chunks=2, instrument="DGS10", dataset="series")
    run_history(env_for(tmp_path, [src]), start=START, end=END)
    names = [e for e, _ in events]
    assert names.count("history.request") == 2
    assert names.count("history.request_done") == 2
    assert names.count("history.source_done") == 1
    assert names[0] == "history.plan"
    assert names[-1] == "history.finished"


def test_step_summary_holds_the_plan_and_the_outcome(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    env = env_for(tmp_path, [RangeSource("fred", weekdays(START, END), instrument="DGS10")])
    env.env = {"GITHUB_STEP_SUMMARY": str(summary)}
    run_history(env, start=START, end=END)
    text = summary.read_text()
    assert "history plan 2026-09-01 .. 2026-09-30" in text
    assert "`history`" in text


# --- CLI ------------------------------------------------------------------------------------


@pytest.fixture
def local_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DESKBRIEF_STORAGE_BACKEND", "local")
    monkeypatch.setenv("DESKBRIEF_CONFIG", str(REPO_ROOT / "config.yaml"))
    monkeypatch.chdir(tmp_path)  # config's local_root is relative: ./.data
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)
    # configure_logging() would keep a handler on a stream pytest closes between tests
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    return tmp_path


def test_cli_dry_run_defaults_to_two_years(
    local_cli: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.main(["history", "--dry-run", "--sources", "ecb,cftc"]) == 0
    out = capsys.readouterr().out
    assert "history plan 2024-11-18 .. 2026-11-18" in out
    assert "ecb/eurofxref" in out
    assert "cftc/cot" in out
    assert "Dry run: nothing fetched, nothing written." in out


def test_cli_explicit_range(
    local_cli: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = ["history", "--dry-run", "--sources", "ecb", "--start", "2026-01-05"]
    assert cli.main([*args, "--end", "2026-03-31"]) == 0
    assert "history plan 2026-01-05 .. 2026-03-31" in capsys.readouterr().out
    assert cli.main(["history", "--dry-run", "--sources", "ecb", "--end", "2026-03-31"]) == 0
    assert "history plan 2024-03-31 .. 2026-03-31" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--end", "2026-11-19"], "--end must be before today"),
        (["--start", "2026-11-01", "--end", "2026-10-01"], "is after --end"),
        (["--sources", "nope"], "unknown or disabled sources"),
    ],
)
def test_cli_rejects_bad_ranges(
    local_cli: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    message: str,
) -> None:
    assert cli.main(["history", "--dry-run", *args]) == 2
    assert message in capsys.readouterr().err


def test_cli_run_history_is_still_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", "history"]) == 2
    assert "its own subcommand" in capsys.readouterr().err
