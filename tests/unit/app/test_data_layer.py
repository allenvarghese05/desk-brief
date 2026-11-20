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
