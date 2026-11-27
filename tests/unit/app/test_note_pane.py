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

    assert data.brief_time_of(et(MON, "06:21"), et(MON, "15:00")) == et(MON, "06:21")
    assert data.brief_time_of(None, et(MON, "15:00")) == et(MON, "06:15")  # the schedule
    # the latest brief was Friday's at 06:20: today at its clock time, so "new since" is
    # since Friday 06:20, the brief that actually ran
    assert data.brief_time_of(et(FRI, "06:20"), et(MON, "05:00")) == et(MON, "06:20")


def test_the_latest_archived_brief_is_found_without_a_note(tmp_path: Path) -> None:
    # PR #102 review round 2: on Saturday (no cron) the heading names Friday's brief; a
    # model.json with no note still gives its report time
    from app.components import data

    store = LocalBackend(tmp_path / "data")
    store.put_bytes(report_key(FRI, 1, "model.json"),
                    json.dumps({"report_time": "2026-11-13T11:40:00+00:00"}).encode())  # fmt: skip
    config = World(tmp_path).config
    config = config.model_copy(
        update={"storage": config.storage.model_copy(update={"local_root": str(store.root)})}
    )
    sat = date(2026, 11, 14)
    assert data._archived_brief_time(config, {}, et(sat, "11:00")) == et(FRI, "06:40")
    assert data._archived_brief_time(config, {}, et(FRI, "06:00")) is None  # not run yet
    snap = data.build_snapshot(config, {}, et(sat, "11:00"))
    assert snap.archived == et(FRI, "06:40")
    assert snap.brief_time == et(sat, "06:40")


def test_the_pane_heading_names_the_brief_that_ran_and_labels_a_live_read() -> None:
    from app.components import brief

    def head(now: Any, archived: Any) -> str:
        pane = brief.pane(None, (), et(MON, "06:15"), et(FRI, "06:15"), now=now, archived=archived)
        return pane.split('<div class="db-label">', 1)[1].split("</div>", 1)[0]

    live = head(et(MON, "15:02"), et(MON, "06:15"))
    assert live == "Live read<span>15:02 ET · brief at 06:15 · new since Fri 06:15</span>"
    at_brief = head(et(MON, "06:15"), et(MON, "06:15"))
    assert at_brief == "The brief<span>Mon 06:15 ET · new since Fri 06:15</span>"
    weekend = head(et(date(2026, 11, 14), "11:00"), et(FRI, "06:15"))
    assert weekend == (
        "Live read<span>Sat 11:00 ET · last brief Fri 06:15 · new since Fri 06:15</span>"
    )
    assert "no brief archived yet" in head(et(MON, "05:00"), None)


def test_data_health_names_the_notes_state(run_home: Any) -> None:
    at = run_home("hosted")
    at.switch_page("pages/4_Data_health.py").run()
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert "Morning note: none today (no brief has been written today yet)." in captions


def test_a_note_without_figures_has_no_checked_line() -> None:
    from app.components import brief

    pane = brief.pane(None, (), MON_0700, None, Note(("S&P futures fell overnight.",), 0, 0))
    assert "every number checked" not in pane
