"""Identifier helpers. All ids are ULIDs: sortable by creation time, unique, URL-safe."""

from ulid import ULID


def new_id() -> str:
    """Return a new ULID string (26 chars, Crockford base32)."""
    return str(ULID())
