# ruff: noqa: RUF001  (the true minus sign is intentional output)
"""T56: one shared dashboard connection, rebuilt in the background only when the listing changed,
and ``data_version`` keying the page caches.

SYNTHETIC FIXTURE: the store is built from ``brief_synth`` (made-up values, labelled there) plus
the made-up rows written below.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import streamlit as st

from brief_synth import MON, MON_0700, World, daily, monday_rows
from deskbrief.config import Config, load_config

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))  # `app` is a plain folder, not an installed package

from app.components import data  # noqa: E402

YH = {"source": "yahoo", "dataset": "history", "unit": "USD", "tier": 2}


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path / "w")
    w.observations(monday_rows())
    w.run(MON)
    return w


@pytest.fixture
def config(world: World) -> Iterator[Config]:
    text = (REPO / "config.yaml").read_text(encoding="utf-8")
    path = world.store.root.parent / "config.yaml"
    path.write_text(
        text.replace("local_root: ./.data", f"local_root: {world.store.root}"), encoding="utf-8"
    )
    cfg = load_config(path)
    yield cfg
    data.wait_for_refresh(cfg)
    slot = data._SLOTS.pop((cfg.storage.backend, data._where(cfg)), None)
    if slot is not None and slot.current is not None:
        slot.current.conn.close()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1e9]
    monkeypatch.setattr(data.time, "monotonic", lambda: now[0])
    return now


def add_file(world: World, d: date, value: float) -> None:
    world.observations([daily("SPY", d, value, **YH)])


def count_builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    built: list[str] = []
    real = data._build

    def spy(store: Any, config: Config, env: Any, listing: data.Listing) -> data._Shared:
        built.append(listing.fingerprint())
        return real(store, config, env, listing)

    monkeypatch.setattr(data, "_build", spy)
    return built


def test_no_listing_change_means_no_rebuild(
    config: Config, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    built = count_builds(monkeypatch)
    with data.shared_connection(config) as first:
        pass
    version = data.data_version(config)
    for _ in range(3):
        clock[0] += data.LISTING_CHECK_SECONDS + 1
        with data.shared_connection(config) as conn:
            assert conn is first
        assert data.wait_for_refresh(config)
    assert len(built) == 1
    assert data.data_version(config) == version


def test_listing_is_checked_at_most_once_a_minute(
    config: Config, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    data.data_version(config)
    checks: list[int] = []
    real = data.store_listing
    monkeypatch.setattr(data, "store_listing", lambda s: checks.append(1) or real(s))
    for _ in range(5):  # five clicks inside the first minute: no check
        clock[0] += 10
        data.data_version(config)
        assert data.wait_for_refresh(config)
    assert checks == []
    clock[0] += 10  # 60 s since the first build: one check
    data.data_version(config)
    assert data.wait_for_refresh(config)
    assert len(checks) == 1
    for _ in range(5):  # the next minute counts from that check
        clock[0] += 10
        data.data_version(config)
        assert data.wait_for_refresh(config)
    assert len(checks) == 1


def test_a_new_file_changes_the_version_and_the_connection(
    world: World, config: Config, clock: list[float]
) -> None:
    v1 = data.data_version(config)
    with data.shared_connection(config) as old:
        pass
    add_file(world, date(2026, 11, 2), 1.0)
    clock[0] += data.LISTING_CHECK_SECONDS + 1
    data.data_version(config)
    assert data.wait_for_refresh(config)
    v2 = data.data_version(config)
    assert v2 != v1
    with data.shared_connection(config) as new:
        assert new is not old
        assert new.execute("SELECT count(*) FROM obs_all WHERE value = 1.0").fetchone() == (1,)
    with pytest.raises(Exception):  # noqa: B017, PT011 (duckdb raises its own closed error)
        old.execute("SELECT 1")


def test_queries_during_a_rebuild_use_the_old_connection(
    world: World, config: Config, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    with data.shared_connection(config) as old:
        pass
    v1 = data.data_version(config)
    started, release = threading.Event(), threading.Event()
    real = data._build

    def slow(*args: Any) -> data._Shared:
        started.set()
        assert release.wait(30)
        return real(*args)

    monkeypatch.setattr(data, "_build", slow)
    add_file(world, date(2026, 11, 2), 1.0)
    clock[0] += data.LISTING_CHECK_SECONDS + 1
    with data.shared_connection(config) as conn:  # starts the check, does not wait for it
        assert conn is old
    assert started.wait(30)
    for _ in range(3):  # the rebuild is running: every query still gets the old connection
        with data.shared_connection(config) as conn:
            assert conn is old
            assert conn.execute("SELECT count(*) FROM obs_all WHERE value = 1.0").fetchone() == (0,)
        assert data.data_version(config) == v1
    release.set()
    assert data.wait_for_refresh(config)
    with data.shared_connection(config) as conn:
        assert conn is not old
        assert conn.execute("SELECT count(*) FROM obs_all WHERE value = 1.0").fetchone() == (1,)


def test_a_failed_check_keeps_the_old_connection_serving(
    world: World, config: Config, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    with data.shared_connection(config) as old:
        pass

    def boom(_store: object) -> data.Listing:
        raise OSError("/private/path")

    monkeypatch.setattr(data, "store_listing", boom)
    clock[0] += data.LISTING_CHECK_SECONDS + 1
    data.data_version(config)
    assert data.wait_for_refresh(config)
    with data.shared_connection(config) as conn:
        assert conn is old
        assert conn.execute("SELECT 1").fetchone() == (1,)
    slot = data._SLOTS[(config.storage.backend, data._where(config))]
    assert slot.error == "OSError"  # the type only, never the message


def test_build_snapshot_uses_the_shared_connection(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_second(*_a: object) -> None:
        raise AssertionError("build_snapshot opened its own connection")

    data.data_version(config)  # the shared connection exists
    monkeypatch.setattr(data, "open_connection", no_second)
    monkeypatch.setattr(data, "_build", no_second)
    snap = data.build_snapshot(config, {}, MON_0700, frozenset({1}))
    assert snap.model.rows()


def test_dashboard_connection_has_no_scorecard_files(config: Config) -> None:
    with data.shared_connection(config) as conn:
        assert conn.execute("SELECT count(*) FROM scorecard").fetchone() == (0,)
    listing = data.store_listing(data._store(config, {}))
    assert "scorecard_daily" not in dict(listing.keys)


def test_snapshot_cache_is_keyed_on_the_data_version(
    world: World, config: Config, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    st.cache_data.clear()
    calls: list[str] = []
    real: Callable[..., data.Snapshot] = data.build_snapshot

    def counting(*args: Any) -> data.Snapshot:
        calls.append(data.data_version(config))
        return real(*args)

    monkeypatch.setattr(data, "build_snapshot", counting)
    monkeypatch.setattr(data, "current_time", lambda: MON_0700)
    data.load_snapshot(config, frozenset({1}))
    data.load_snapshot(config, frozenset({1}))  # same version: cached
    assert len(calls) == 1
    add_file(world, date(2026, 11, 2), 1.0)
    clock[0] += data.LISTING_CHECK_SECONDS + 1
    data.data_version(config)
    assert data.wait_for_refresh(config)
    data.load_snapshot(config, frozenset({1}))  # new version: read again, well inside 15 min
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert data.DATA_CACHE_TTL_SECONDS == 15 * 60


def test_a_hung_check_is_abandoned_after_the_timeout(
    world: World, config: Config, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LIST that never returns must not stop every later check (review round 1, finding 2)."""
    with data.shared_connection(config) as old:
        pass
    hang, release = threading.Event(), threading.Event()
    real = data.store_listing

    def stuck(store: Any) -> data.Listing:
        if not hang.is_set():
            hang.set()
            assert release.wait(30)
        return real(store)

    monkeypatch.setattr(data, "store_listing", stuck)
    clock[0] += data.LISTING_CHECK_SECONDS + 1
    data.data_version(config)  # starts the check that hangs
    assert hang.wait(30)
    add_file(world, date(2026, 11, 2), 1.0)
    clock[0] += data.LISTING_CHECK_SECONDS + 1
    data.data_version(config)  # still within the timeout: no second check
    slot = data._SLOTS[(config.storage.backend, data._where(config))]
    assert slot.gen == 1
    clock[0] += data.REFRESH_TIMEOUT_SECONDS
    data.data_version(config)  # the hung check is abandoned; a new one runs
    assert slot.gen == 2
    assert data.wait_for_refresh(config)
    with data.shared_connection(config) as conn:
        assert conn is not old
        assert conn.execute("SELECT count(*) FROM obs_all WHERE value = 1.0").fetchone() == (1,)
    newest = conn
    release.set()  # the abandoned check finishes late: it must not swap or reopen anything
    for t in [t for t in threading.enumerate() if t.name == "deskbrief-listing"]:
        t.join(10)
    with data.shared_connection(config) as conn:
        assert conn is newest
    assert slot.gen == 2


def test_a_thread_that_cannot_start_releases_the_slot(
    config: Config, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    with data.shared_connection(config) as old:
        pass

    def no_start(_self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_start)
    clock[0] += data.LISTING_CHECK_SECONDS + 1
    with data.shared_connection(config) as conn:
        assert conn is old
    slot = data._SLOTS[(config.storage.backend, data._where(config))]
    assert not slot.busy
    assert slot.error == "RuntimeError"


def test_the_snapshot_cache_refreshes_its_clock_every_minute(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same data version, a minute later: the as-of and data-age text are recomputed."""
    from datetime import timedelta

    st.cache_data.clear()
    calls: list[object] = []
    real: Callable[..., data.Snapshot] = data.build_snapshot

    def counting(*args: Any) -> data.Snapshot:
        calls.append(args[2])
        return real(*args)

    now = [MON_0700]
    monkeypatch.setattr(data, "build_snapshot", counting)
    monkeypatch.setattr(data, "current_time", lambda: now[0])
    data.load_snapshot(config, frozenset({1}))
    now[0] = MON_0700 + timedelta(seconds=30)
    data.load_snapshot(config, frozenset({1}))  # same minute: cached
    now[0] = MON_0700 + timedelta(minutes=1)
    data.load_snapshot(config, frozenset({1}))
    assert calls == [MON_0700, MON_0700 + timedelta(minutes=1)]


# --- T60 bugs 4 and 5: weekend and holiday changes, the latest run ----------------------------
# SYNTHETIC: every row below is made up (brief_synth helpers).

from brief_synth import FRI, THU, et, tick  # noqa: E402

SAT_1000 = et(date(2026, 11, 14), "10:00")  # Saturday after the brief_synth Friday


def _config_for(world: World) -> Config:
    text = (REPO / "config.yaml").read_text(encoding="utf-8")
    path = world.store.root.parent / "config.yaml"
    path.write_text(
        text.replace("local_root: ./.data", f"local_root: {world.store.root}"), encoding="utf-8"
    )
    return load_config(path)


def _snapshot(world: World, now: Any, hidden: frozenset[int] = frozenset()) -> data.Snapshot:
    cfg = _config_for(world)
    try:
        return data.build_snapshot(cfg, {}, now, hidden)
    finally:
        data.wait_for_refresh(cfg)
        slot = data._SLOTS.pop((cfg.storage.backend, data._where(cfg)), None)
        if slot is not None and slot.current is not None:
            slot.current.conn.close()


def _row(snap: data.Snapshot, iid: str) -> Any:
    return next(r for r in snap.model.rows() if r.instrument_id == iid)


@pytest.mark.parametrize("hidden", [frozenset(), frozenset({1})], ids=["local", "hosted"])
def test_saturday_shows_fridays_change_labelled_fri(tmp_path: Path, hidden: frozenset[int]) -> None:
    """Bug 4: on a Saturday the strip and watchlist show Friday's close against Thursday's,
    labelled "Fri", not a dash. Friday ticks stored after the close (a tier-1 alpaca tick and a
    tier-2 yahoo quote) are compared with Thursday's close too, in both modes."""
    from app.components import panels

    w = World(tmp_path / "w")
    yh = {"source": "yahoo", "dataset": "history", "tier": 2, "unit": "index_pts"}
    w.observations(
        [
            daily("ES_F", THU, 6024.0, **yh),
            daily("ES_F", FRI, 6000.0, **yh),
            daily("SPY", THU, 600.0),
            tick("SPY", et(FRI, "17:30"), 594.0, unit="USD"),  # yahoo quote after the close
            tick("SPY", et(FRI, "17:31"), 595.0, tier=1, source="alpaca", dataset="iex_latest"),
        ]
    )
    snap = _snapshot(w, SAT_1000, hidden)
    es, spy = _row(snap, "ES_F"), _row(snap, "SPY")
    assert es.change_text.startswith("−0.4")
    assert es.basis == "Thu close"
    assert panels.move_label(es) == "Fri"
    assert spy.change.status == "ok"
    assert spy.change.previous.value == 600.0  # Thursday's close
    assert spy.change.last.tier == (2 if hidden else 1)
    assert panels.move_label(spy) == "Fri"


def test_mlk_monday_futures_tick_says_since_friday_close(tmp_path: Path) -> None:
    """Bug 4, S after L: ES futures traded on MLK Monday (an XNYS holiday): the move since
    Friday's close, labelled "since Fri close" (not "Mon", which the Saturday rule would say)."""
    from app.components import panels

    fri, mon = date(2026, 1, 16), date(2026, 1, 19)
    w = World(tmp_path / "w")
    yh = {"source": "yahoo", "dataset": "history", "tier": 2, "unit": "index_pts"}
    w.observations(
        [
            daily("ES_F", date(2026, 1, 15), 6100.0, **yh),
            daily("ES_F", fri, 6000.0, **yh),
            tick("ES_F", et(mon, "11:00"), 6030.0, unit="index_pts"),
        ]
    )
    es = _row(_snapshot(w, et(mon, "12:00")), "ES_F")
    assert es.change.previous.value == 6000.0
    assert es.basis == "Fri close"
    assert panels.move_label(es) == "since Fri close"
    assert "since Fri" in panels.basis_text(es)


def test_weekday_before_the_run_keeps_no_move_yet(tmp_path: Path) -> None:
    from app.components import panels

    w = World(tmp_path / "w")
    yh = {"source": "yahoo", "dataset": "history", "tier": 2, "unit": "index_pts"}
    w.observations([daily("ES_F", THU, 6024.0, **yh), daily("ES_F", FRI, 6000.0, **yh)])
    es = _row(_snapshot(w, et(MON, "05:00")), "ES_F")
    assert es.change.status == "no_move_yet"
    assert panels.move_label(es) == ""


def test_saturday_status_shows_the_latest_run(tmp_path: Path) -> None:
    """Bug 5: on a day without a run the status names the latest run and its checks."""
    from app.components import panels

    w = World(tmp_path / "w")
    w.observations(monday_rows())
    w.dq(FRI, [("fresh_a", "pass", None, None), ("fresh_b", "pass", None, None)], run_id="RF")
    w.run(FRI)
    snap = _snapshot(w, SAT_1000)
    assert snap.last_run is not None
    assert snap.last_run[0] == FRI
    assert snap.model.health.summary.target_date == FRI
    assert snap.model.health.status == "ok"
    ov = data.overview(snap, "local", frozenset())
    line = panels.status(ov, snap.last_run)
    assert "Last run Fri" in line
    assert "2 checks ok" in line
    assert panels.NO_CHECKS not in line


def test_status_without_any_run_says_no_checks_yet(tmp_path: Path) -> None:
    from app.components import panels

    w = World(tmp_path / "w")
    w.observations(monday_rows())
    snap = _snapshot(w, SAT_1000)
    assert snap.last_run is None
    ov = data.overview(snap, "local", frozenset())
    assert panels.NO_CHECKS in panels.status(ov, snap.last_run)


def test_now_before_the_only_run_reads_no_checks(tmp_path: Path) -> None:
    """Review round 2 #2: with no run known at ``now`` the health reads are still bounded.

    The first-ever run starts Fri 06:55 and records fails; read at Fri 06:00 the status line,
    the health dot and the per-instrument flags all agree there are no checks yet.
    """
    from app.components import panels

    w = World(tmp_path / "w")
    w.observations(monday_rows())
    w.dq(FRI, [("fresh_a", "fail", "yahoo", "SPY")], run_id="RF")  # Fri 07:00
    w.xchecks(FRI, [("sp500_return", "fail", 12.0)], run_id="RF", at="00:00")
    started = et(FRI, "06:55")
    run = {"run_id": "RF", "job": "morning", "trigger": "manual", "target_date": FRI,
           "started_at": started, "finished_at": started, "status": "ok", "git_sha": "deadbeef",
           "config_hash": "cafe", "n_fetch": 1, "n_fetch_failed": 0, "n_obs": 1,
           "n_dq_fail": 1, "n_dq_warn": 0}  # fmt: skip
    w._append("runs", [run], lambda r: r["target_date"])
    snap = _snapshot(w, et(FRI, "06:00"))
    assert snap.last_run is None
    assert snap.model.health.status == "unknown"
    assert {r.dq_flag for r in snap.model.rows()} == {"none"}
    ov = data.overview(snap, "local", frozenset())
    assert panels.NO_CHECKS in panels.status(ov, snap.last_run)


@pytest.mark.parametrize(
    ("iid", "rows", "now", "label"),
    [
        (  # FX traded on Thanksgiving (an XNYS holiday): since Wednesday's close
            "EURUSD",
            [
                daily("EURUSD", date(2026, 11, 24), 1.00, source="yahoo", dataset="history",
                      tier=2, unit="fx_rate"),
                daily("EURUSD", date(2026, 11, 25), 1.05, source="yahoo", dataset="history",
                      tier=2, unit="fx_rate"),
                tick("EURUSD", et(date(2026, 11, 26), "06:00"), 1.071, unit="fx_rate"),
            ],
            et(date(2026, 11, 26), "07:00"),
            "since Wed close",
        ),
        (  # N225 closed on Good Friday (an XNYS holiday), seen on Saturday: since Thursday
            "N225",
            [
                daily("N225", date(2026, 4, 1), 30000.0, unit="index_pts"),
                daily("N225", date(2026, 4, 2), 31000.0, unit="index_pts"),
                daily("N225", date(2026, 4, 3), 31620.0, unit="index_pts"),
            ],
            et(date(2026, 4, 4), "10:00"),
            "since Thu close",
        ),
    ],
    ids=["thanksgiving_fx", "good_friday_n225"],
)  # fmt: skip
def test_holiday_trades_say_since_the_last_session_close(
    tmp_path: Path, iid: str, rows: list[dict[str, Any]], now: Any, label: str
) -> None:
    """Bug 4, S after L: the label is "since <L's weekday> close", never S's own weekday
    ("Thu", "Fri"), which the S-on-or-before-L rule would give."""
    from app.components import panels

    w = World(tmp_path / "w")
    w.observations(rows)
    row = _row(_snapshot(w, now), iid)
    assert row.change.status == "ok"
    assert panels.move_label(row) == label
