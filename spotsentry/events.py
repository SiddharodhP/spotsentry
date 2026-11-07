"""Structured events: JSON Lines to stdout, and buffered to the store for the dashboard."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from spotsentry.store.base import Store

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Identity:
    """Where this worker is running."""

    instance_id: str = "local"
    instance_type: str = "local"
    az: str = "local"


def utc_now_iso() -> str:
    """Return the current UTC time in RFC 3339 format."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class EventLogger:
    """Appends events to one store object per worker run, rewritten on each flush."""

    def __init__(
        self,
        store: Store,
        job_root: str,
        job_id: str,
        identity: Identity,
        flush_interval: float = 5.0,
        stdout: bool = True,
    ) -> None:
        self.store = store
        self.job_id = job_id
        self.identity = identity
        self.flush_interval = flush_interval
        self.stdout = stdout
        safe_id = _SAFE.sub("_", identity.instance_id)
        self.key = f"{job_root}events/{safe_id}_{int(time.time() * 1000)}.jsonl"
        self._lines: list[str] = []
        self._dirty = False
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background flush thread."""
        self._thread = threading.Thread(target=self._run, name="spotsentry-events", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.flush_interval):
            self.flush()

    def log(self, event_type: str, *, flush: bool = False, **fields: Any) -> None:
        """Record one event. ``flush=True`` writes it to the store immediately."""
        record = {
            "ts": utc_now_iso(),
            "job_id": self.job_id,
            "instance_id": self.identity.instance_id,
            "instance_type": self.identity.instance_type,
            "az": self.identity.az,
            "type": event_type,
            **fields,
        }
        line = json.dumps(record, default=str)
        with self._lock:
            self._lines.append(line)
            self._dirty = True
        if self.stdout:
            print(line, flush=True)
        if flush:
            self.flush()

    def flush(self) -> None:
        """Write buffered events to the store. A failure here never stops the job."""
        with self._flush_lock:
            with self._lock:
                if not self._dirty:
                    return
                body = ("\n".join(self._lines) + "\n").encode("utf-8")
                self._dirty = False
            try:
                self.store.put_bytes(self.key, body)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._dirty = True
                print(f"spotsentry: could not flush events: {exc}", file=sys.stderr, flush=True)

    def stop(self) -> None:
        """Flush and stop the background thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.flush()


def read_events(store: Store, job_root: str) -> list[dict[str, Any]]:
    """Read every worker's events for a job, oldest first. Damaged lines are skipped."""
    events: list[dict[str, Any]] = []
    for key in store.list_keys(f"{job_root}events/"):
        try:
            body = store.get_bytes(key).decode("utf-8")
        except Exception:  # noqa: BLE001
            continue
        for line in body.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            record["_file"] = key
            events.append(record)
    events.sort(key=lambda e: e.get("ts", ""))
    return events


def run_history(events: list[dict[str, Any]]) -> tuple[float, int]:
    """Return (observed runtime in seconds, interruptions seen) from earlier workers' events."""
    spans: dict[str, tuple[datetime, datetime]] = {}
    interruptions = 0
    for event in events:
        try:
            ts = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        key = event.get("_file", "")
        first, last = spans.get(key, (ts, ts))
        spans[key] = (min(first, ts), max(last, ts))
        if event.get("type") == "interruption_notice":
            interruptions += 1
    runtime = sum((last - first).total_seconds() for first, last in spans.values())
    return runtime, interruptions


def last_checkpoint_cost(events: list[dict[str, Any]]) -> float | None:
    """Return how long the latest routine checkpoint blocked the job, if there was one.

    Emergency and handover saves always wait for the upload, so they say nothing about the cost
    of a routine checkpoint.
    """
    for event in reversed(events):
        if event.get("type") == "checkpoint_done" and event.get("reason") in (
            "periodic",
            "rebalance",
        ):
            return float(event.get("blocking_s") or 0.0)
    return None
