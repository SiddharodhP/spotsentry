"""Upload bandwidth limiting in pure Python: portable to Windows and deterministic."""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Callable

CHUNK = 256 * 1024


def mbps_to_bytes_per_sec(mbps: float) -> float:
    """Convert megabits per second to bytes per second."""
    return mbps * 1_000_000 / 8


class TokenBucket:
    """Thread-safe rate limiter. Waiting callers are spaced out so their sum stays at the rate.

    When sleep() overshoots slightly (noticeable on Windows with small chunks) the next caller
    catches up, so the rate stays accurate. An idle period earns no credit: a burst after idling
    would make a short speed test read too fast.
    """

    CATCH_UP_SEC = 0.05

    def __init__(self, rate_bytes_per_sec: float) -> None:
        if rate_bytes_per_sec <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate_bytes_per_sec
        self._next_free = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n: int) -> None:
        """Block until ``n`` bytes may be sent."""
        with self._lock:
            now = time.monotonic()
            idle = now - self._next_free > self.CATCH_UP_SEC
            start = now if idle else self._next_free
            self._next_free = start + n / self.rate
            wait = self._next_free - now
        if wait > 0:
            time.sleep(wait)


class ThrottledReader(io.RawIOBase):
    """Seekable file-like wrapper that limits read speed and can abort mid-transfer."""

    def __init__(
        self,
        data: bytes,
        bucket: TokenBucket | None = None,
        abort_check: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._raw = io.BytesIO(data)
        self._bucket = bucket
        self._abort = abort_check

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        return self._raw.tell()

    def read(self, size: int = -1) -> bytes:
        parts = []
        remaining = size if size is not None and size >= 0 else None
        while remaining is None or remaining > 0:
            if self._abort is not None:
                self._abort()
            want = CHUNK if remaining is None else min(CHUNK, remaining)
            chunk = self._raw.read(want)
            if not chunk:
                break
            if self._bucket is not None:
                self._bucket.consume(len(chunk))
            parts.append(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        return b"".join(parts)
