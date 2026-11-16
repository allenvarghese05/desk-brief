"""Which source modules exist (card T19).

Each source module registers itself with ``registry.register(name, factory)`` at import time.
The runner calls :func:`load_all` once at startup so that every source that has been merged
is registered, without any shared file listing the factories. A module that is not there yet
is skipped (the runner works before every source card has merged); a module that exists but
fails to import is logged and skipped so one broken source cannot take the run down.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable

from deskbrief.logging import get_logger

KNOWN_SOURCE_MODULES: tuple[str, ...] = (
    "deskbrief.sources.treasury",
    "deskbrief.sources.fred",
    "deskbrief.sources.ecb",
    "deskbrief.sources.stooq",
    "deskbrief.sources.cboe",
    "deskbrief.sources.alpaca",
    "deskbrief.sources.twelvedata",
    "deskbrief.sources.yahoo",
)


def load_all(modules: Iterable[str] = KNOWN_SOURCE_MODULES) -> list[str]:
    """Import each source module; return the names that imported successfully."""
    log = get_logger()
    loaded: list[str] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:  # the module exists but one of *its* imports is missing
                log.error("catalog.import_failed", module=name, error=str(exc))
            else:
                log.info("catalog.skip", module=name, reason="module not present yet")
            continue
        except Exception as exc:  # isolate a broken source module from the others
            log.error("catalog.import_failed", module=name, error=str(exc))
            continue
        loaded.append(name)
    return loaded
