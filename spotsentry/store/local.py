"""Filesystem store for unit tests and Docker-free runs. Emulates S3 semantics."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from spotsentry.errors import ObjectNotFound, PreconditionFailed
from spotsentry.store.base import AbortCheck, Store
from spotsentry.store.throttle import ThrottledReader, TokenBucket, mbps_to_bytes_per_sec


def _retry_os(fn, attempts: int = 40, delay: float = 0.05):
    """Windows refuses to replace or open a file another process has open, so retry briefly."""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


class LocalStore(Store):
    """Stores objects as files. Writes use temp file + os.replace, so they are all-or-nothing."""

    def __init__(self, root: str | Path, bandwidth_limit_mbps: float | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks = self.root / ".locks"
        self._locks.mkdir(exist_ok=True)
        self._bucket = (
            TokenBucket(mbps_to_bytes_per_sec(bandwidth_limit_mbps))
            if bandwidth_limit_mbps
            else None
        )

    def _path(self, key: str) -> Path:
        return self.root / Path(*key.split("/"))

    def _temp(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.parent / f".tmp-{uuid.uuid4().hex}"

    @contextmanager
    def _lock(self, key: str, stale_after: float = 5.0):
        """Cross-process lock using atomic mkdir. A lock left by a killed process goes stale."""
        name = hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()
        lock = self._locks / name
        while True:
            try:
                os.mkdir(lock)
                break
            except (FileExistsError, PermissionError):
                try:
                    if time.time() - lock.stat().st_mtime > stale_after:
                        os.rmdir(lock)
                        continue
                except OSError:
                    pass
                time.sleep(0.01)
        try:
            yield
        finally:
            try:
                os.rmdir(lock)
            except OSError:
                pass

    @staticmethod
    def _etag(data: bytes) -> str:
        return hashlib.md5(data, usedforsecurity=False).hexdigest()

    def _write_atomic(self, path: Path, data: bytes) -> None:
        temp = self._temp(path)
        temp.write_bytes(data)
        _retry_os(lambda: os.replace(temp, path))

    def put_bytes(
        self, key: str, data: bytes, *, if_none_match: bool = False, if_match: str | None = None
    ) -> str:
        path = self._path(key)
        if not if_none_match and if_match is None:
            self._write_atomic(path, data)
            return self._etag(data)
        with self._lock(key):
            exists = path.exists()
            if if_none_match and exists:
                raise PreconditionFailed(key)
            if if_match is not None:
                if not exists:
                    raise PreconditionFailed(key)
                current = _retry_os(path.read_bytes)
                if self._etag(current) != if_match:
                    raise PreconditionFailed(key)
            self._write_atomic(path, data)
        return self._etag(data)

    def get(self, key: str) -> tuple[bytes, str]:
        try:
            data = _retry_os(self._path(key).read_bytes)
        except FileNotFoundError as exc:
            raise ObjectNotFound(key) from exc
        return data, self._etag(data)

    def delete(self, key: str) -> None:
        try:
            _retry_os(self._path(key).unlink)
        except FileNotFoundError:
            pass

    def list_keys(self, prefix: str) -> list[str]:
        keys = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.name.startswith(".tmp-"):
                continue
            rel = path.relative_to(self.root)
            if rel.parts and rel.parts[0] == ".locks":
                continue
            key = "/".join(rel.parts)
            if key.startswith(prefix):
                keys.append(key)
        return sorted(keys)

    def upload(self, key: str, data: bytes, abort_check: AbortCheck | None = None) -> None:
        path = self._path(key)
        temp = self._temp(path)
        reader = ThrottledReader(data, self._bucket, abort_check)
        try:
            with open(temp, "wb") as fh:
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            _retry_os(lambda: os.replace(temp, path))
        finally:
            if temp.exists():
                temp.unlink()
