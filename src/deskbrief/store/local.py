"""Local-filesystem storage backend (dev and tests). Mirrors the R2 layout under a root dir.

Writes are atomic: bytes go to a temp file in the destination directory, then become visible
in one step. Write-once uses ``os.link`` (fails if the destination exists, with no
check-then-write race); overwrite uses ``os.replace``.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path

from deskbrief.store.backend import KeyExistsError, KeyNotFoundError
from deskbrief.store.raw import ensure_not_raw

_TMP_SUFFIX = ".deskbrief-tmp"


def _check_key(key: str) -> str:
    parts = key.split("/")
    if not key or "\\" in key or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid storage key: {key!r}")
    return key


class LocalBackend:
    """``StorageBackend`` on a directory. Keys are ``/``-separated paths under ``root``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def _path(self, key: str) -> Path:
        return self.root / _check_key(key)

    def put_bytes(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
        if overwrite:
            ensure_not_raw(key, "overwrite")
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}{_TMP_SUFFIX}")
        try:
            with tmp.open("wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if overwrite:
                os.replace(tmp, dest)
            else:
                try:
                    os.link(tmp, dest)
                except FileExistsError:
                    raise KeyExistsError(key) from None
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

    def get_bytes(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise KeyNotFoundError(key) from None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str) -> list[str]:
        if any(p in (".", "..") or "\\" in p for p in prefix.split("/")):
            raise ValueError(f"invalid storage prefix: {prefix!r}")
        # walk only the deepest directory that every match must live under
        base = self.root / prefix.rpartition("/")[0]
        if not base.is_dir():
            return []
        keys = []
        for path in base.rglob("*"):
            if not path.is_file() or path.name.endswith(_TMP_SUFFIX):
                continue
            key = path.relative_to(self.root).as_posix()
            if key.startswith(prefix):
                keys.append(key)
        return sorted(keys)

    def delete(self, key: str) -> None:
        ensure_not_raw(key, "delete")
        with contextlib.suppress(FileNotFoundError):
            self._path(key).unlink()
