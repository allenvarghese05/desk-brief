"""The runner writes ``events``/``filings`` per scope, all or nothing (card T65, review round 3).

Real ``Datasets`` on a ``LocalBackend``, the real runner, the real FRED and EDGAR sources; only
HTTP is faked. SYNTHETIC FIXTURE: every release date, filing and holding below is invented
(EDGAR documents from ``tests/fixtures/synthetic/edgar``, the fictional Example Corp).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pipeline_fakes import REPO_ROOT, FakeClock, load_test_config

from deskbrief.checks.table_freshness import TableFreshnessCheck, due_session
from deskbrief.config import Config, FilingsCompany, FilingsHolder, FredRelease
from deskbrief.ids import new_id
from deskbrief.models import (
    DQStatus,
    FetchRequest,
    JobKind,
    RawResponse,
    RunStatus,
    Trigger,
)
from deskbrief.pipeline.jobs import JobSteps
from deskbrief.pipeline.runner import RunEnv, execute_run, make_context
from deskbrief.schemas import current_rows, validate
from deskbrief.sources.edgar import EdgarSource
from deskbrief.sources.fiscaldata import FiscalDataSource
from deskbrief.sources.fred import RELEASE_DATASET, FredSource
from deskbrief.store.datasets import Datasets
from deskbrief.store.duck import connect
from deskbrief.store.local import LocalBackend
from deskbrief.timeutil import et_date, to_et

KEY = "-".join(["fake", "fred", "value"] * 3)
EDGAR_FIX = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "edgar"
FAKE_UA = " ".join(["Invented", "Tester", "tester@example.invalid"])
MON = datetime(2026, 11, 23, 11, 20, tzinfo=UTC)  # Monday 06:20 ET
TUE = MON + timedelta(days=1)
WED = MON + timedelta(days=2)
THU = MON + timedelta(days=3)


def _config(*, filings: bool = False) -> Config:
    cfg = load_test_config()
    events = cfg.events.model_copy(
        update={
            "fred_releases": (
                FredRelease(release_id=10, title="Consumer Price Index", time_et="08:30"),
                FredRelease(release_id=50, title="Employment Situation", time_et="08:30"),
            )
        }
    )
    update: dict[str, Any] = {"events": events}
    if filings:
        update["filings"] = cfg.filings.model_copy(
            update={
                "companies": (
                    FilingsCompany(instrument="AAPL", cik="0009999999", cusip="EXMP00010"),
                ),
                "holders": (FilingsHolder(cik="0007777777", name="Invented Asset Management"),),
            }
        )
    return cfg.model_copy(update=update)


Answer = Callable[[FetchRequest, str], tuple[int, bytes]]


class FakeHttp:
    """Answers each request with ``answer(req, url) -> (status, body)``, stamped by the clock."""

    def __init__(self, clock: Callable[[], datetime], answer: Answer) -> None:
        self.clock = clock
        self.answer = answer

    def get(
        self,
        req: FetchRequest,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        secret_params: Mapping[str, str] | None = None,
        secret_headers: Mapping[str, str] | None = None,
        units: int = 1,
    ) -> RawResponse:
        status, body = self.answer(req, url)
        at = self.clock()
        return RawResponse(
            request=req,
            fetch_id=new_id(),
            requested_at=at,
            responded_at=at,
            http_status=status,
            content_type="application/json",
            body=body,
            error=None if 200 <= status < 300 else f"HTTP {status}",
            url_redacted=url,
        )


def releases(offsets: Mapping[str, list[int]], fail: str | None = None) -> Answer:
    """Release ``rid`` lands ``offsets[rid]`` days after ``realtime_start``; ``fail`` = 500."""

    def answer(req: FetchRequest, url: str) -> tuple[int, bytes]:
        if req.dataset != RELEASE_DATASET:
            return 200, json.dumps({"observations": []}).encode()
        rid = req.params["release_id"]
        if rid == fail:
            return 500, b"invented upstream error"
        if fail == f"corrupt:{rid}":
            return 200, b"{not json"
        start = date.fromisoformat(req.params["realtime_start"])
        dates = [
            {
                "release_id": int(rid),
                "release_name": f"Invented {rid}",
                "date": str(start + timedelta(days=d)),
            }
            for d in offsets.get(rid, [])
        ]
        return 200, json.dumps(
            {"release_dates": dates, "count": len(dates), "limit": 10000}
        ).encode()

    return answer


def _env(
    tmp_path: Path, config: Config, sources: list[Any], answer: Answer, start: datetime
) -> RunEnv:
    clock = FakeClock(start)
    env = RunEnv(
        config=config,
        store=LocalBackend(tmp_path / "data"),
        http=FakeHttp(clock, answer),
        sources=sources,
        clock=clock,
        git_sha="t65",
    )
    env.steps_for = lambda job: JobSteps()
    return env


def _run(env: RunEnv, answer: Answer, at: datetime) -> Any:
    clock = FakeClock(at)
    env.clock = clock
    env.http = FakeHttp(clock, answer)
    ctx = make_context(env, JobKind.MORNING, Trigger.MANUAL, et_date(at), at)
    return ctx, execute_run(env, ctx)


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", KEY)
    monkeypatch.setenv("DESKBRIEF_SEC_USER_AGENT", FAKE_UA)


def _tables(env: RunEnv) -> tuple[pd.DataFrame, pd.DataFrame]:
    ds = Datasets(env.store)
    return ds.read("events"), ds.read("source_log")


# --------------------------------------------------------------------------------------------
# (a) events: several fetches feed one (source, dataset) scope
# --------------------------------------------------------------------------------------------


def test_one_failed_fetch_withholds_the_whole_scope_and_the_last_run_survives(
    tmp_path: Path,
) -> None:
    cfg = _config()
    good = releases({"10": [1, 3], "50": [2]})
    env = _env(tmp_path, cfg, [FredSource(cfg)], good, MON)
    ctx1, _m1 = _run(env, good, MON)
    events, log = _tables(env)
    assert len(events) == 3
    assert set(events["run_id"]) == {ctx1.run_id}

    moved = releases({"10": [2]}, fail="50")  # CPI rescheduled, the jobs fetch fails
    ctx2, m2 = _run(env, moved, TUE)
    events, log = _tables(env)
    assert ctx2.run_id not in set(events["run_id"])  # nothing written by run 2 for the scope
    assert m2.status is RunStatus.DEGRADED
    assert any("withheld" in n for n in m2.notes)
    cur = current_rows("events", events, log)
    assert set(cur["run_id"]) == {ctx1.run_id}
    assert sorted(cur["event_id"]) == [
        "release:10:2026-11-24",
        "release:10:2026-11-26",
        "release:50:2026-11-25",
    ]
    run2 = log[(log["run_id"] == ctx2.run_id) & (log["dataset"] == RELEASE_DATASET)]
    assert sorted(run2["status"]) == ["error", "ok"]


def test_a_quarantined_fetch_withholds_the_scope_too(tmp_path: Path) -> None:
    cfg = _config()
    good = releases({"10": [1], "50": [2]})
    env = _env(tmp_path, cfg, [FredSource(cfg)], good, MON)
    ctx1, _ = _run(env, good, MON)
    ctx2, _m2 = _run(env, releases({"10": [1], "50": [2]}, fail="corrupt:50"), TUE)
    events, log = _tables(env)
    assert set(events["run_id"]) == {ctx1.run_id}
    assert set(current_rows("events", events, log)["run_id"]) == {ctx1.run_id}
    q = Datasets(env.store).read("quarantine")
    assert list(q["dataset"]) == [RELEASE_DATASET]
    assert set(q["run_id"]) == {ctx2.run_id}


def test_complete_runs_replace_and_an_all_empty_run_empties(tmp_path: Path) -> None:
    cfg = _config()
    env = _env(tmp_path, cfg, [FredSource(cfg)], releases({}), MON)
    _run(env, releases({"10": [1], "50": [2]}), MON)
    ctx2, _ = _run(env, releases({"10": [3]}), TUE)  # 50 answers "nothing scheduled"
    events, log = _tables(env)
    cur = current_rows("events", events, log)
    assert set(cur["run_id"]) == {ctx2.run_id}
    assert list(cur["event_id"]) == ["release:10:2026-11-27"]
    run2 = log[(log["run_id"] == ctx2.run_id) & (log["dataset"] == RELEASE_DATASET)]
    assert sorted(run2["status"]) == ["empty", "ok"]  # a clean zero-row fetch is "empty"
    assert run2.set_index("status").loc["ok", "rows_parsed"] == 1
    ctx3, _ = _run(env, releases({}), WED)  # every release fetch: nothing scheduled
    events, log = _tables(env)
    assert ctx3.run_id not in set(events["run_id"])
    assert current_rows("events", events, log).empty


def test_events_rows_follow_the_writer_rules(tmp_path: Path) -> None:
    cfg = _config()
    answer = releases({"10": [0, 4], "50": [2]})
    env = _env(tmp_path, cfg, [FredSource(cfg)], answer, MON)
    ctx, _ = _run(env, answer, MON)
    events, log = _tables(env)
    validate("events", events)
    assert not events.duplicated(["source", "dataset", "event_id"]).any()  # once per run
    fetches = log.set_index("fetch_id")
    for row in events.to_dict("records"):  # source/dataset are the fetch's source_log ones
        assert (row["source"], row["dataset"]) == tuple(
            fetches.loc[row["fetch_id"], ["source", "dataset"]]
        )
        assert row["ingested_at"] == fetches.loc[row["fetch_id"], "responded_at"]
        assert to_et(row["scheduled_at"].to_pydatetime()).date() == row["event_date"]
    # partition = ET date of ingested_at
    keys = [k for k in env.store.list("derived/events/") if k.endswith(".parquet")]
    assert keys == [f"derived/events/date=2026-11-23/run={ctx.run_id}.parquet"]


# --------------------------------------------------------------------------------------------
# filings through the runner
# --------------------------------------------------------------------------------------------


def edgar_answer(fail: str | None = None) -> Answer:
    def answer(req: FetchRequest, url: str) -> tuple[int, bytes]:
        if fail and fail in url:
            return 500, b"invented upstream error"
        name = url.rsplit("/", 1)[-1]
        if "submissions/CIK" in url:
            return 200, (EDGAR_FIX / f"submissions_{name[3:-5]}.json").read_bytes()
        if name == "index.json":
            return 200, (EDGAR_FIX / "index.json").read_bytes()
        if name == "infotable.xml":
            return 200, (EDGAR_FIX / f"infotable_{url.rsplit('/', 2)[-2]}.xml").read_bytes()
        if name == "primary_doc.xml":
            return 200, (EDGAR_FIX / "cover_restatement.xml").read_bytes()
        return 200, (EDGAR_FIX / name).read_bytes()

    return answer


def test_filings_written_per_accession_and_a_failed_company_writes_nothing(
    tmp_path: Path,
) -> None:
    cfg = _config(filings=True)
    env = _env(tmp_path, cfg, [EdgarSource(cfg)], edgar_answer(), THU)
    ctx1, m1 = _run(env, edgar_answer(), THU)
    filings = Datasets(env.store).read("filings")
    validate("filings", filings)
    assert m1.status is RunStatus.OK
    assert {"company", "holders"} == set(filings["dataset"])
    assert "0007777777-26-000004" in set(filings["accession"])
    assert not filings.duplicated(["instrument_id", "accession", "seq"]).any()

    ctx2, _m2 = _run(env, edgar_answer(fail="form4_buy.xml"), THU + timedelta(days=1))
    filings = Datasets(env.store).read("filings")
    run2 = filings[filings["run_id"] == ctx2.run_id]
    assert set(run2["dataset"]) == {"holders"}  # the company fetch failed as a whole
    cur = current_rows("filings", filings)
    company = cur[cur["dataset"] == "company"]
    assert set(company["run_id"]) == {ctx1.run_id}  # yesterday's company rows stay current


# --------------------------------------------------------------------------------------------
# freshness of the two new datasets
# --------------------------------------------------------------------------------------------


def _freshness(env: RunEnv, job: JobKind, now: datetime) -> dict[tuple[str, str], Any]:
    ctx = make_context(env, job, Trigger.MANUAL, et_date(now), now)
    duck = connect(env.store, env.config)
    results = TableFreshnessCheck().evaluate(ctx, duck)
    return {(r.source or "", str(r.details["dataset"])): r for r in results}


def test_freshness_covers_events_and_filings_datasets(tmp_path: Path) -> None:
    cfg = _config(filings=True)
    answer = releases({"10": [1], "50": [2]})
    env = _env(tmp_path, cfg, [FredSource(cfg)], answer, MON)
    _run(env, answer, MON)
    eod = MON + timedelta(hours=11)  # 17:20 ET
    res = _freshness(env, JobKind.EOD, eod)
    assert set(res) == {
        ("fiscaldata", "upcoming_auctions"),
        ("fred", "release_dates"),
        ("edgar", "company"),
        ("edgar", "holders"),
    }
    assert res[("fred", "release_dates")].status is DQStatus.PASS
    assert res[("fiscaldata", "upcoming_auctions")].status is DQStatus.FAIL  # never fetched
    assert res[("edgar", "company")].details["table"] == "filings"

    _run(env, releases({"10": [1]}, fail="50"), TUE)  # Tuesday's scope is incomplete
    res = _freshness(env, JobKind.EOD, TUE + timedelta(hours=11))
    assert res[("fred", "release_dates")].status is DQStatus.FAIL
    assert res[("fred", "release_dates")].details["latest_complete_fetch"].startswith("2026-11-23")
    # in Tuesday's morning run itself the due session is still Monday: not yet late
    morning = _freshness(env, JobKind.MORNING, TUE + timedelta(minutes=5))
    assert morning[("fred", "release_dates")].status is DQStatus.PASS
    # within the grace after 07:00 ET: waiting
    waiting = _freshness(env, JobKind.EOD, TUE + timedelta(hours=1, minutes=10))
    assert waiting[("fred", "release_dates")].status is DQStatus.SKIP
    assert _freshness(env, JobKind.INTRADAY, TUE + timedelta(hours=4)) == {}


def test_due_session_rolls_back_over_weekends_and_before_seven() -> None:
    assert due_session(MON) == date(2026, 11, 20)  # 06:20 Monday: Friday's
    assert due_session(MON + timedelta(hours=1)) == date(2026, 11, 23)  # 07:20 Monday
    assert due_session(datetime(2026, 11, 28, 15, 0, tzinfo=UTC)) == date(2026, 11, 27)  # Sat


# --------------------------------------------------------------------------------------------
# PR #102 review batch 4: empty bodies, interrupted runs, past target dates
# --------------------------------------------------------------------------------------------


def _with_zero_byte(base: Answer, rid: str) -> Answer:
    """``base``, except release ``rid`` answers 200 with an empty body (an upstream glitch)."""

    def answer(req: FetchRequest, url: str) -> tuple[int, bytes]:
        if req.dataset == RELEASE_DATASET and req.params["release_id"] == rid:
            return 200, b""
        return base(req, url)

    return answer


def test_a_zero_byte_200_withholds_the_release_scope(tmp_path: Path) -> None:
    cfg = _config()
    good = releases({"10": [1], "50": [2]})
    env = _env(tmp_path, cfg, [FredSource(cfg)], good, MON)
    ctx1, _ = _run(env, good, MON)
    ctx2, m2 = _run(env, _with_zero_byte(releases({"10": [2], "50": [3]}), "50"), TUE)
    events, log = _tables(env)
    assert ctx2.run_id not in set(events["run_id"])  # release 10's rows held back too
    cur = current_rows("events", events, log)
    assert sorted(cur["event_id"]) == ["release:10:2026-11-24", "release:50:2026-11-25"]
    assert set(cur["run_id"]) == {ctx1.run_id}
    run2 = log[(log["run_id"] == ctx2.run_id) & (log["dataset"] == RELEASE_DATASET)]
    assert sorted(run2["status"]) == ["error", "ok"]  # never a clean "empty"
    bad = run2[run2["status"] == "error"].iloc[0]
    assert bad["bytes"] == 0
    assert "parse_error" in bad["error"]
    assert m2.status is RunStatus.DEGRADED
    q = Datasets(env.store).read("quarantine")
    assert list(q["run_id"]) == [ctx2.run_id]


def test_a_zero_byte_200_never_empties_the_auction_scope(tmp_path: Path) -> None:
    cfg = _config()
    record = {
        "record_date": "2026-11-20",
        "security_type": "Bill",
        "security_term": "4-Week",
        "reopening": "No",
        "cusip": "EXMPB0011",
        "offering_amt": "null",
        "announcemt_date": "2026-11-20",
        "auction_date": "2026-11-24",
        "issue_date": "2026-11-26",
    }
    body = json.dumps({"data": [record], "meta": {"total-pages": 1}}).encode()

    def full(req: FetchRequest, url: str) -> tuple[int, bytes]:
        return 200, body

    env = _env(tmp_path, cfg, [FiscalDataSource(cfg)], full, MON)
    ctx1, _ = _run(env, full, MON)
    events, log = _tables(env)
    assert len(current_rows("events", events, log)) == 1
    ctx2, m2 = _run(env, lambda req, url: (200, b""), TUE)
    events, log = _tables(env)
    cur = current_rows("events", events, log)
    assert list(cur["run_id"]) == [ctx1.run_id]  # yesterday's auction is still current
    assert list(log[log["run_id"] == ctx2.run_id]["status"]) == ["error"]
    assert m2.status is not RunStatus.OK
    # a well-formed answer with no rows is the only "nothing scheduled"
    nothing = json.dumps({"data": [], "meta": {"total-pages": 1}}).encode()
    ctx3, _ = _run(env, lambda req, url: (200, nothing), WED)
    events, log = _tables(env)
    assert list(log[log["run_id"] == ctx3.run_id]["status"]) == ["empty"]
    assert current_rows("events", events, log).empty


@pytest.mark.parametrize("stop_at", ["10", "50"])
@pytest.mark.parametrize("before", ["rows", "nothing scheduled"])
def test_an_interrupted_release_loop_writes_nothing_and_keeps_the_last_run(
    tmp_path: Path, stop_at: str, before: str
) -> None:
    """The job is cancelled (KeyboardInterrupt) while fetching release ``stop_at``: whatever
    the releases fetched before it said, the scope is not written and not emptied."""
    cfg = _config()
    good = releases({"10": [1], "50": [2]})
    env = _env(tmp_path, cfg, [FredSource(cfg)], good, MON)
    ctx1, _ = _run(env, good, MON)
    base = releases({"10": [2], "50": [3]} if before == "rows" else {})

    def cancel(req: FetchRequest, url: str) -> tuple[int, bytes]:
        if req.dataset == RELEASE_DATASET and req.params["release_id"] == stop_at:
            raise KeyboardInterrupt
        return base(req, url)

    with pytest.raises(KeyboardInterrupt):
        _run(env, cancel, TUE)
    events, log = _tables(env)
    assert set(events["run_id"]) == {ctx1.run_id}  # nothing partial was written
    cur = current_rows("events", events, log)
    assert sorted(cur["event_id"]) == ["release:10:2026-11-24", "release:50:2026-11-25"]
    run2 = log[(log["run_id"] != ctx1.run_id) & (log["dataset"] == RELEASE_DATASET)]
    aborted = run2[run2["status"] == "error"]
    unprocessed = ["10", "50"][["10", "50"].index(stop_at) :]
    assert len(aborted) == len(unprocessed)
    assert all("run aborted (KeyboardInterrupt)" in e for e in aborted["error"])
    runs = Datasets(env.store).read("runs")
    assert sorted(runs["status"]) == ["failed", "ok"]
    # freshness does not count the aborted run as a complete fetch
    res = _freshness(env, JobKind.EOD, TUE + timedelta(hours=11))
    assert res[("fred", "release_dates")].status is DQStatus.FAIL


def test_a_morning_run_for_a_past_date_leaves_the_calendar_alone(tmp_path: Path) -> None:
    cfg = _config()
    good = releases({"10": [1], "50": [2]})
    env = _env(tmp_path, cfg, [FredSource(cfg)], good, MON)
    ctx1, _ = _run(env, good, MON)
    seen: list[str] = []

    def record(req: FetchRequest, url: str) -> tuple[int, bytes]:
        seen.append(req.dataset)
        return releases({"10": [5]})(req, url)

    clock = FakeClock(WED)
    env.clock = clock
    env.http = FakeHttp(clock, record)
    ctx2 = make_context(env, JobKind.MORNING, Trigger.MANUAL, et_date(MON), WED)  # past date
    m2 = execute_run(env, ctx2)
    assert seen  # observations fetched
    assert RELEASE_DATASET not in seen  # release dates not
    events, log = _tables(env)
    assert set(events["run_id"]) == {ctx1.run_id}
    assert log[(log["run_id"] == ctx2.run_id) & (log["dataset"] == RELEASE_DATASET)].empty
    assert sorted(current_rows("events", events, log)["run_id"]) == [ctx1.run_id] * 2
    assert any("is not today" in n for n in m2.notes)
    assert m2.status is RunStatus.OK


def test_a_404_on_a_stored_original_never_counts_it_beside_its_amendment(
    tmp_path: Path,
) -> None:
    """PR #102 review B3: run 1 stores a Form 4 and the Form 4/A that restates it, so only the
    amendment counts. In run 2 the original's document answers 404: the amendment is skipped
    too, so run 1's row naming the original stays current and the totals do not change."""
    from deskbrief.query import company_filings_from_frame

    cfg = _config(filings=True)
    ea = edgar_answer()
    env = _env(tmp_path, cfg, [EdgarSource(cfg)], ea, THU)
    _run(env, ea, THU)
    first = company_filings_from_frame(
        Datasets(env.store).read("filings"), "AAPL", THU + timedelta(hours=1), 90
    )

    def original_404(req: FetchRequest, url: str) -> tuple[int, bytes]:
        if "form4_buy.xml" in url:  # the amended original's document is missing today
            return 404, b"not found"
        return ea(req, url)

    _run(env, original_404, THU + timedelta(days=1))
    after = company_filings_from_frame(
        Datasets(env.store).read("filings"), "AAPL", THU + timedelta(days=1, hours=1), 90
    )
    assert {t.accession for t in after.insiders.trades} == {
        t.accession for t in first.insiders.trades
    }
    assert (after.insiders.n_buys, after.insiders.buys_usd) == (
        first.insiders.n_buys,
        first.insiders.buys_usd,
    )
