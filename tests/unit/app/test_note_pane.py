"""The morning note on the dashboard (P-024, card T66): the stored note, its time, the silent
configuration states, and the brief the page is anchored to (PR #102 review).

SYNTHETIC FIXTURE: the archived briefs below are invented; no API is ever called.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from brief_synth import FRI, MON, MON_0700, World, et
from deskbrief.report.model import Note
from deskbrief.store.backend import report_key
from deskbrief.store.local import LocalBackend


def _archive(store: LocalBackend, d: date, note: dict[str, Any], report_time: str) -> None:
    body = json.dumps({"note": note, "report_time": report_time}).encode()
    store.put_bytes(report_key(d, 1, "model.json"), body)


def test_the_brief_pane_shows_the_stored_note_with_its_time() -> None:
    from app.components import brief

    note = Note(("S&P futures fell 1.6%.",), 1, 1, written_at=et(MON, "06:15"))
    pane = brief.pane(None, (), et(MON, "06:15"), None, note)
    assert '<p class="db-note-s">S&amp;P futures fell 1.6%.</p>' in pane
    assert "Note at 06:15 ET" in pane
    assert "every number checked (1 of 1)" in pane
    assert '<div class="db-label">The brief<span>Mon 06:15 ET</span></div>' in pane
    timed_out = brief.pane(None, (), MON_0700, None, Note(reason="the API timed out"))
    assert "No note today: the API timed out." in timed_out


def test_configuration_states_show_no_line_on_the_dashboard() -> None:
    from app.components import brief

    for why in ("no API key is configured", "the note is turned off in config"):
        pane = brief.pane(None, (), MON_0700, None, Note(reason=why))
        assert "No note today" not in pane
        assert why not in pane


def test_the_dashboard_reads_the_stored_note_and_never_the_api(tmp_path: Path) -> None:
    from app.components import data

    store = LocalBackend(tmp_path / "data")
    _archive(store, MON, {"sentences": ["Stored."], "n_checked": 0, "n_numbers": 0},
             "2026-11-16T11:15:00+00:00")  # fmt: skip
    config = World(tmp_path).config
    config = config.model_copy(
        update={"storage": config.storage.model_copy(update={"local_root": str(store.root)})}
    )
    stored = data._stored_note(config, {}, MON)
    assert stored.sentences == ("Stored.",)
    assert stored.written_at == et(MON, "06:15")
    assert data._stored_note(config, {}, FRI).reason == "no brief has been written today yet"
    src = Path(data.__file__).read_text(encoding="utf-8")
    assert "anthropic" not in src


def test_the_page_follows_the_brief_not_the_readers_clock() -> None:
    from app.components import data

    archived = Note(written_at=et(MON, "06:21"))
    assert data.brief_time_of(archived, et(MON, "15:00")) == et(MON, "06:21")
    assert data.brief_time_of(None, et(MON, "15:00")) == et(MON, "06:15")  # the schedule
    old = Note(written_at=et(FRI, "06:20"))  # yesterday's brief: today's schedule instead
    assert data.brief_time_of(old, et(MON, "15:00")) == et(MON, "06:15")


def test_data_health_names_the_notes_state(run_home: Any) -> None:
    at = run_home("hosted")
    at.switch_page("pages/4_Data_health.py").run()
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert "Morning note: none today (no brief has been written today yet)." in captions
