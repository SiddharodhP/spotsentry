"""Two-phase checkpointing: snapshot on the job thread, serialize and upload in the background."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from spotsentry import serialize
from spotsentry.adapters.base import Position, Tier
from spotsentry.checkpoint import CheckpointManager, ManifestEntry, make_envelope
from spotsentry.deadline import DeadlineSelector
from spotsentry.errors import UploadAborted
from spotsentry.events import EventLogger
from spotsentry.store.base import AbortCheck

RETRIES = 3
BACKOFF_SEC = 0.5


@dataclass
class SaveResult:
    """Outcome of one checkpoint."""

    ok: bool
    tier: Tier
    position: Position
    reason: str
    entry: ManifestEntry | None = None
    size_bytes: int = 0
    serialize_s: float = 0.0
    upload_s: float = 0.0
    blocking_s: float = 0.0
    error: str | None = None


@dataclass
class _InFlight:
    tier: Tier
    position: Position
    started: float
    done: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    result: SaveResult | None = None


class Uploader:
    """Runs phase 2 of a checkpoint and feeds its measurements to the selector and tuner.

    At most one background upload runs at a time.
    """

    def __init__(
        self,
        manager: CheckpointManager,
        selector: DeadlineSelector,
        events: EventLogger | None = None,
        on_saved: Callable[[SaveResult], None] | None = None,
        compress: bool = False,
        backoff_sec: float = BACKOFF_SEC,
    ) -> None:
        self.manager = manager
        self.selector = selector
        self.events = events
        self.on_saved = on_saved
        self.compress = compress
        self.backoff_sec = backoff_sec
        self._in_flight: _InFlight | None = None
        self._lock = threading.Lock()

    def _log(self, event: str, **fields: Any) -> None:
        if self.events:
            self.events.log(event, **fields)

    @property
    def busy(self) -> bool:
        """True while a background upload is running."""
        with self._lock:
            return self._in_flight is not None and not self._in_flight.done.is_set()

    @property
    def in_flight_position(self) -> Position | None:
        with self._lock:
            return self._in_flight.position if self._in_flight else None

    def estimate_remaining(self) -> float:
        """Estimated seconds until the running upload finishes."""
        with self._lock:
            job = self._in_flight
        if job is None or job.done.is_set():
            return 0.0
        return max(self.selector.predict(job.tier) - (time.monotonic() - job.started), 0.0)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the running upload. Returns True if nothing is running any more."""
        with self._lock:
            job = self._in_flight
        return True if job is None else job.done.wait(timeout)

    def cancel(self) -> None:
        """Ask the running upload to stop. Its checkpoint will simply never be committed."""
        with self._lock:
            job = self._in_flight
        if job is not None:
            job.cancelled.set()

    def _process(
        self,
        state: dict,
        tier: Tier,
        position: Position,
        reason: str,
        blocking_s: float,
        abort_check: AbortCheck | None,
        blocks_job: bool = False,
    ) -> SaveResult:
        result = SaveResult(ok=False, tier=tier, position=position, reason=reason)
        result.blocking_s = blocking_s
        process_started = time.monotonic()
        try:
            started = time.monotonic()
            payload = serialize.dumps(make_envelope(state, tier, position), compress=self.compress)
            del state
            result.serialize_s = time.monotonic() - started
            result.size_bytes = len(payload)
            self.selector.observe_serialize(tier, len(payload), result.serialize_s)
        except Exception as exc:  # noqa: BLE001
            result.error = f"serialize: {type(exc).__name__}: {exc}"
            self._log("checkpoint_failed", tier=tier.value, reason=reason, error=result.error)
            return result
        for attempt in range(1, RETRIES + 1):
            try:
                entry, upload_s = self.manager.write_checkpoint(
                    payload, tier, position, abort_check=abort_check
                )
                result.upload_s = upload_s
                self.selector.observe_upload(len(payload), upload_s)
                committed = self.manager.commit(entry)
                result.entry = entry
                result.ok = committed
                if not committed:
                    result.error = "superseded"
                break
            except UploadAborted as exc:
                result.error = f"aborted: {exc}"
                break
            except Exception as exc:  # noqa: BLE001
                result.error = f"upload: {type(exc).__name__}: {exc}"
                if attempt < RETRIES:
                    time.sleep(self.backoff_sec * 2 ** (attempt - 1))
        if blocks_job:
            # A synchronous save blocks the job for all of it, not only the snapshot.
            result.blocking_s = blocking_s + (time.monotonic() - process_started)
        if result.ok:
            self._log(
                "checkpoint_done",
                tier=tier.value,
                reason=reason,
                seq=result.entry.seq if result.entry else None,
                epoch=position.epoch,
                step=position.step,
                size_bytes=result.size_bytes,
                blocking_s=round(result.blocking_s, 6),
                serialize_s=round(result.serialize_s, 6),
                upload_s=round(result.upload_s, 6),
            )
            if self.on_saved:
                self.on_saved(result)
        else:
            self._log(
                "checkpoint_failed",
                tier=tier.value,
                reason=reason,
                epoch=position.epoch,
                step=position.step,
                error=result.error,
            )
        return result

    def save_sync(
        self,
        state: dict,
        tier: Tier,
        position: Position,
        reason: str,
        blocking_s: float = 0.0,
        abort_check: AbortCheck | None = None,
    ) -> SaveResult:
        """Run phase 2 on the calling thread."""
        self._log("checkpoint_start", tier=tier.value, reason=reason, mode="sync")
        return self._process(
            state, tier, position, reason, blocking_s, abort_check, blocks_job=True
        )

    def submit(
        self, state: dict, tier: Tier, position: Position, reason: str, blocking_s: float
    ) -> bool:
        """Start phase 2 in the background. Returns False if an upload is already running."""
        with self._lock:
            if self._in_flight is not None and not self._in_flight.done.is_set():
                return False
            job = _InFlight(tier=tier, position=position, started=time.monotonic())
            self._in_flight = job

        def abort_check() -> None:
            if job.cancelled.is_set():
                raise UploadAborted("cancelled")

        def work() -> None:
            try:
                job.result = self._process(state, tier, position, reason, blocking_s, abort_check)
            finally:
                job.done.set()

        self._log("checkpoint_start", tier=tier.value, reason=reason, mode="async")
        threading.Thread(target=work, name="spotsentry-upload", daemon=True).start()
        return True
