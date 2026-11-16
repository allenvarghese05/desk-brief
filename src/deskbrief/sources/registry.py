"""Source registry (card T03).

Source modules register a factory under their config name; the runner asks for the sources
that are both configured and enabled::

    register("fred", FredSource)          # a SourceFactory: factory(config) -> Source
    sources = get_sources(config)

Real sources are registered by their own cards (``register`` is called at import time of the
source module or from the runner's wiring). Unknown (no factory) and disabled sources are
skipped with a log line; a factory that raises is logged and skipped so one broken source
cannot take the run down.
"""

from __future__ import annotations

from deskbrief.config import Config
from deskbrief.logging import get_logger
from deskbrief.sources.base import Source, SourceFactory

_FACTORIES: dict[str, SourceFactory] = {}


def register(name: str, factory: SourceFactory) -> None:
    """Register ``factory`` for source ``name``. Registering a name twice is a bug."""
    if name in _FACTORIES:
        raise ValueError(f"source {name!r} is already registered")
    _FACTORIES[name] = factory


def registered() -> list[str]:
    return sorted(_FACTORIES)


def clear() -> None:
    """Remove all registrations (tests)."""
    _FACTORIES.clear()


def get_sources(config: Config) -> list[Source]:
    """Build every configured, enabled, registered source as ``factory(config)`` (config order)."""
    log = get_logger()
    sources: list[Source] = []
    for name, settings in config.sources.items():
        if not settings.enabled:
            log.info("registry.skip", source=name, reason="disabled")
            continue
        factory = _FACTORIES.get(name)
        if factory is None:
            log.warning("registry.skip", source=name, reason="no factory registered")
            continue
        try:
            sources.append(factory(config))
        except Exception as exc:  # isolate a broken source from the others
            log.error("registry.skip", source=name, reason="factory failed", error=str(exc))
    return sources
