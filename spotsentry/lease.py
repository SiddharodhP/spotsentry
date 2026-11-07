"""Single-worker lease. Capacity Rebalancing can briefly run two instances; only one may train.

The lease is an object in the store, updated with S3 conditional writes so two workers cannot
both win. If the store does not support conditional writes the same code still works but has a
small race window (documented in docs/NOTES.md).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

from spotsentry.errors import ObjectNotFound, PreconditionFailed
from spotsentry.events import EventLogger, utc_now_iso
from spotsentry.store.base import Store


class LeaseStopped(Exception):
    """Waiting for the lease was abandoned because ``stop_check`` returned True."""


class Lease:
    """Owner-tracked lease with a heartbeat and a handover request object."""

    def __init__(
        self,
        store: Store,
        job_root: str,
        owner: str,
        ttl_sec: float = 60.0,
        heartbeat_sec: float = 15.0,
        poll_sec: float = 5.0,
        events: EventLogger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.key = f"{job_root}lease.json"
        self.request_key = f"{job_root}lease_request.json"
        self.owner = owner
        self.ttl_sec = ttl_sec
        self.heartbeat_sec = heartbeat_sec
        self.poll_sec = poll_sec
        self.events = events
        self.clock = clock
        self.lost = False
        self.held = False
        self._etag: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _log(self, event: str, **fields: Any) -> None:
        if self.events:
            self.events.log(event, **fields)

    def _body(self, acquired_at: str | None = None) -> bytes:
        now = self.clock()
        return json.dumps(
            {
                "owner": self.owner,
                "acquired_at": acquired_at or utc_now_iso(),
                "heartbeat_at": now,
                "expires_at": now + self.ttl_sec,
            }
        ).encode("utf-8")

    def read(self) -> tuple[dict[str, Any] | None, str | None]:
        """Return the current lease and its ETag, or (None, None)."""
        try:
            data, etag = self.store.get(self.key)
        except ObjectNotFound:
            return None, None
        try:
            return json.loads(data.decode("utf-8")), etag
        except ValueError:
            return {"owner": None, "expires_at": 0}, etag

    def try_acquire(self) -> bool:
        """Try once to take the lease. Returns True if this worker now holds it."""
        current, etag = self.read()
        try:
            if current is None:
                self._etag = self.store.put_bytes(self.key, self._body(), if_none_match=True)
            elif current.get("owner") == self.owner or current.get("expires_at", 0) <= self.clock():
                self._etag = self.store.put_bytes(self.key, self._body(), if_match=etag)
            else:
                return False
        except PreconditionFailed:
            return False
        self.held = True
        self.lost = False
        return True

    def acquire(self, stop_check: Callable[[], bool] | None = None) -> None:
        """Block until the lease is held. Waiting workers ask the owner to hand over."""
        logged = False
        while not self.try_acquire():
            if stop_check is not None and stop_check():
                raise LeaseStopped()
            self.store.put_bytes(
                self.request_key,
                json.dumps({"owner": self.owner, "requested_at": self.clock()}).encode("utf-8"),
            )
            if not logged:
                current, _ = self.read()
                self._log(
                    "waiting_for_lease",
                    current_owner=current.get("owner") if current else None,
                )
                logged = True
            time.sleep(self.poll_sec)
        self._clear_own_request()
        self._log("lease_acquired", owner=self.owner, waited=logged)

    def _clear_own_request(self) -> None:
        try:
            data = self.store.get_bytes(self.request_key)
            if json.loads(data.decode("utf-8")).get("owner") == self.owner:
                self.store.delete(self.request_key)
        except (ObjectNotFound, ValueError):
            pass

    def heartbeat(self) -> bool:
        """Extend the lease once. Sets ``lost`` and returns False if someone else took it."""
        try:
            self._etag = self.store.put_bytes(self.key, self._body(), if_match=self._etag)
            return True
        except PreconditionFailed:
            self.lost = True
            self.held = False
            self._log("error", where="lease", message="lease lost to another worker")
            return False

    def _run_heartbeat(self) -> None:
        last_ok = self.clock()
        while not self._stop.wait(self.heartbeat_sec):
            try:
                if not self.heartbeat():
                    return
                last_ok = self.clock()
            except Exception as exc:  # noqa: BLE001
                self._log("error", where="lease", message=f"heartbeat failed: {exc}")
                if self.clock() - last_ok > self.ttl_sec:
                    self.lost = True
                    self.held = False
                    return

    def start_heartbeat(self) -> None:
        """Keep the lease alive in the background."""
        self._thread = threading.Thread(target=self._run_heartbeat, name="spotsentry-lease",
                                        daemon=True)
        self._thread.start()

    def other_waiting(self) -> bool:
        """True if a different worker has asked for the lease recently."""
        try:
            request = json.loads(self.store.get_bytes(self.request_key).decode("utf-8"))
        except (ObjectNotFound, ValueError):
            return False
        return (
            request.get("owner") != self.owner
            and self.clock() - float(request.get("requested_at", 0)) < self.ttl_sec
        )

    def release(self) -> None:
        """Stop the heartbeat and give the lease up, if this worker still holds it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if not self.held:
            return
        try:
            current, _ = self.read()
            if current is not None and current.get("owner") == self.owner:
                self.store.delete(self.key)
        except Exception as exc:  # noqa: BLE001
            self._log("error", where="lease", message=f"release failed: {exc}")
        self.held = False
