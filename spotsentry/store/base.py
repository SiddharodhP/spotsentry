"""Storage interface. Keeping it small is what would let GCS or Azure Blob be added later."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from spotsentry.errors import ObjectNotFound

AbortCheck = Callable[[], None]


class Store(ABC):
    """Key-value object store with atomic writes and conditional updates."""

    #: False when the backend rejects conditional writes (see lease.py for the fallback).
    supports_conditional: bool = True

    @abstractmethod
    def put_bytes(
        self, key: str, data: bytes, *, if_none_match: bool = False, if_match: str | None = None
    ) -> str:
        """Atomically write a small object and return its ETag. Not bandwidth limited.

        ``if_none_match`` creates only if the key is absent, ``if_match`` replaces only if the
        current ETag matches. A lost race raises PreconditionFailed.
        """

    @abstractmethod
    def get(self, key: str) -> tuple[bytes, str]:
        """Return (data, etag), or raise ObjectNotFound."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete a key. Deleting a missing key is not an error."""

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        """Return all keys under a prefix."""

    @abstractmethod
    def upload(self, key: str, data: bytes, abort_check: AbortCheck | None = None) -> None:
        """Write a checkpoint object, applying the bandwidth limit if there is one.

        ``abort_check`` is called often and may raise UploadAborted. An aborted upload leaves
        nothing visible at ``key``.
        """

    def get_bytes(self, key: str) -> bytes:
        return self.get(key)[0]

    def exists(self, key: str) -> bool:
        try:
            self.get(key)
            return True
        except ObjectNotFound:
            return False

    def delete_prefix(self, prefix: str) -> int:
        keys = self.list_keys(prefix)
        for key in keys:
            self.delete(key)
        return len(keys)
