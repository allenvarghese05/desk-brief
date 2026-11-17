"""INJECTED FAULT: two rebuilds racing for ``clean/_CURRENT`` (card T06, spec §2.4).

Nothing here is a real incident; none of it belongs in INCIDENTS.md.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pipeline_fakes import FakeSource, make_env

from deskbrief.models import JobKind, RunStatus, Trigger
from deskbrief.pipeline import clean
from deskbrief.pipeline.runner import run_job, run_rebuild
from deskbrief.store.datasets import Datasets, current_clean_version


def test_inj_rebuild_overtaken_by_another_rebuild_never_flips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rebuild A has written v2 when rebuild B runs start to finish and flips to v3.

    A must notice the pointer moved and leave it on v3; exactly one flip happens, and readers
    keep seeing a complete version throughout.
    """
    env = make_env(tmp_path, [FakeSource("treasury", instrument="UST10Y")])
    run_job(env, JobKind.EOD, target_date=date(2026, 11, 16), trigger=Trigger.MANUAL)
    real = clean._carry_over
    outcome: dict[str, Any] = {}

    def race(store: Any, old: str, new: str, from_date: date | None) -> int:
        if "B" not in outcome:  # first call is rebuild A, between its last write and its flip
            outcome["B"] = None  # set before running B, whose own call must not race again
            outcome["B"] = run_rebuild(env)
        return real(store, old, new, from_date)

    monkeypatch.setattr(clean, "_carry_over", race)
    a = run_rebuild(env)
    b = outcome["B"]
    assert b.status is RunStatus.OK
    assert a.status is RunStatus.FAILED
    assert any("moved" in n for n in a.notes)
    assert current_clean_version(env.store) == "v3"  # B's flip stands; A did not overwrite it
    assert len(Datasets(env.store).read("observations")) == 1
