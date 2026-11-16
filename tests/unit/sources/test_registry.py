"""Source registry tests (card T03). Dummy factory; real sources register later."""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator

import pandas as pd
import pytest

from deskbrief.config import Config, load_config
from deskbrief.logging import configure_logging
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext
from deskbrief.sources import registry
from deskbrief.sources.base import HttpClient, Source


class DummySource:
    parser_version = "1"

    def __init__(self, config: Config, name: str = "dummy") -> None:
        self.config = config
        self.name = name

    def plan(self, job: JobKind, ctx: RunContext) -> list[FetchRequest]:
        return []

    def fetch(self, req: FetchRequest, http: HttpClient) -> RawResponse:
        raise NotImplementedError

    def parse(self, raw: RawResponse) -> pd.DataFrame:
        raise NotImplementedError


def factory_for(name: str) -> Callable[[Config], Source]:
    return lambda config: DummySource(config, name)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    registry.clear()
    yield
    registry.clear()


@pytest.fixture
def config() -> Config:
    return load_config()


def with_enabled(config: Config, **flags: bool) -> Config:
    sources = {
        n: s.model_copy(update={"enabled": flags.get(n, s.enabled)})
        for n, s in config.sources.items()
    }
    return config.model_copy(update={"sources": sources})


def log_events(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_builds_enabled_sources_with_config(config: Config) -> None:
    registry.register("fred", factory_for("fred"))
    registry.register("ecb", factory_for("ecb"))
    built = registry.get_sources(config)
    assert [s.name for s in built] == [n for n in config.sources if n in ("fred", "ecb")]
    assert all(isinstance(s, DummySource) and s.config is config for s in built)
    assert all(isinstance(s, Source) for s in built)


def test_unknown_source_is_skipped_with_log(config: Config) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    registry.register("fred", factory_for("fred"))
    assert [s.name for s in registry.get_sources(config)] == ["fred"]
    skipped = {
        e["source"]: e["reason"] for e in log_events(stream) if e["event"] == "registry.skip"
    }
    assert skipped["treasury"] == "no factory registered"


def test_disabled_source_is_skipped_without_building(config: Config) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    built: list[str] = []

    def factory(c: Config) -> Source:
        built.append("fred")
        return DummySource(c, "fred")

    registry.register("fred", factory)
    registry.register("ecb", factory_for("ecb"))
    result = registry.get_sources(with_enabled(config, fred=False))
    assert [s.name for s in result] == ["ecb"]
    assert built == []
    fred_logs = [e for e in log_events(stream) if e.get("source") == "fred"]
    assert [(e["event"], e["reason"]) for e in fred_logs] == [("registry.skip", "disabled")]


def test_registered_but_not_configured_is_not_built(config: Config) -> None:
    registry.register("not_in_config", factory_for("not_in_config"))
    assert registry.get_sources(config) == []


def test_failing_factory_is_isolated(config: Config) -> None:
    def boom(c: Config) -> Source:
        raise RuntimeError("bad setup")

    registry.register("fred", boom)
    registry.register("ecb", factory_for("ecb"))
    assert [s.name for s in registry.get_sources(config)] == ["ecb"]


def test_duplicate_registration_rejected() -> None:
    registry.register("fred", factory_for("fred"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register("fred", factory_for("fred"))
    assert registry.registered() == ["fred"]


def test_name_mismatch_is_warned_but_kept(config: Config) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    registry.register("fred", factory_for("not_fred"))
    assert [s.name for s in registry.get_sources(config)] == ["not_fred"]
    events = [e for e in log_events(stream) if e["event"] == "registry.name_mismatch"]
    assert events[0]["config_key"] == "fred"
    assert events[0]["source_name"] == "not_fred"
