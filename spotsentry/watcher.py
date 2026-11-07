"""Background thread that polls the metadata service for interruption signals."""

from __future__ import annotations

import threading

from spotsentry.events import EventLogger
from spotsentry.metadata import MetadataClient, Notice


class Watcher:
    """Sets thread-safe flags the job loop checks at each safe point."""

    def __init__(
        self, client: MetadataClient, events: EventLogger | None, poll_interval: float = 2.0
    ) -> None:
        self.client = client
        self.events = events
        self.poll_interval = poll_interval
        self.rebalance_event = threading.Event()
        self.notice_event = threading.Event()
        self.notice: Notice | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> None:
        """Check both metadata paths once. Each signal is logged the first time it appears."""
        if not self.notice_event.is_set():
            notice = self.client.get_interruption_notice()
            if notice is not None:
                self.notice = notice
                self.notice_event.set()
                if self.events:
                    self.events.log(
                        "interruption_notice",
                        flush=True,
                        action=notice.action,
                        deadline=notice.time.isoformat().replace("+00:00", "Z"),
                    )
        if not self.rebalance_event.is_set():
            when = self.client.get_rebalance()
            if when is not None:
                self.rebalance_event.set()
                if self.events:
                    self.events.log(
                        "rebalance_signal",
                        flush=True,
                        notice_time=when.isoformat().replace("+00:00", "Z"),
                    )

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            if self.notice_event.is_set() and self.rebalance_event.is_set():
                return
            self._stop.wait(self.poll_interval)

    def start(self) -> None:
        """Start polling in the background."""
        self._thread = threading.Thread(target=self._run, name="spotsentry-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop polling."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.client.timeout + 1.0)
