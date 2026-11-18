"""Test helper: the real ``config.yaml`` with every source enabled.

Stooq is off in ``config.yaml`` (owner decision, T25), but its parser, its schedule entries and
the fake sources named ``stooq`` throughout the suite still need an enabled source to exercise.
"""

from __future__ import annotations

from deskbrief.config import Config


def enable_all_sources(config: Config) -> Config:
    sources = {
        name: s.model_copy(update={"enabled": True}) if not s.enabled else s
        for name, s in config.sources.items()
    }
    return config.model_copy(update={"sources": sources})
