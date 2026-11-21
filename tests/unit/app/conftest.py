"""Fixtures for the dashboard tests.

SYNTHETIC FIXTURE: the store is built from ``brief_synth`` (made-up values, labelled there).
No real R2 and no network: the local backend only.
"""

from __future__ import annotations

import itertools
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import brief_synth
from brief_synth import MON_0700, World, monday_rows

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))  # `app` is a plain folder, not an installed package
HOME = REPO / "app" / "Home.py"

#: env vars the dashboard reads; cleared so a developer's shell cannot leak into a test
_ENV = (
    "DESKBRIEF_DISPLAY_MODE",
    "DESKBRIEF_STORAGE_BACKEND",
    "DESKBRIEF_STORAGE_BUCKET",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_READER_ACCESS_KEY_ID",
    "R2_READER_SECRET_ACCESS_KEY",
)


@pytest.fixture(autouse=True)
def _close_new_connections() -> Iterator[None]:
    """Close the shared DuckDB connections a test's stores opened (T56 keeps one per store for
    the process, with native threads), so the whole folder can run in one process (T58: the
    Home tests open many stores)."""
    from app.components import data

    before = set(data._SLOTS)
    yield
    for key in set(data._SLOTS) - before:
        slot = data._SLOTS.pop(key)
        if slot.current is not None and not slot.busy:
            slot.current.conn.close()


@pytest.fixture
def run_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., AppTest]:
    """``run_home(mode, rows=..., backend=...)`` runs Home.py against a synthetic local store."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    st.cache_data.clear()
    runs = itertools.count()

    def run(
        mode: str | None = "hosted",
        rows: list | None = None,  # type: ignore[type-arg]
        extra: Callable[[World], None] | None = None,
    ) -> AppTest:
        world = World(tmp_path / f"w{next(runs)}")
        world.observations(monday_rows() if rows is None else rows)
        if extra is not None:  # more tables: cross-checks, data checks, ...
            extra(world)
        world.dq(
            brief_synth.MON, [("fresh_a", "pass", None, None), ("fresh_b", "pass", None, None)]
        )
        world.run(brief_synth.MON)
        text = (REPO / "config.yaml").read_text(encoding="utf-8")
        text = text.replace("local_root: ./.data", f"local_root: {world.store.root}")
        cfg = world.store.root.parent / "config.yaml"
        cfg.write_text(text, encoding="utf-8")
        monkeypatch.setenv("DESKBRIEF_CONFIG", str(cfg))
        if mode is not None:
            monkeypatch.setenv("DESKBRIEF_DISPLAY_MODE", mode)
        from app.components import data

        monkeypatch.setattr(data, "current_time", lambda: MON_0700)
        return AppTest.from_file(str(HOME), default_timeout=60).run()

    return run
