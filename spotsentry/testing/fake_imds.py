"""A small in-process stand-in for the EC2 metadata service.

Used by unit tests and by Docker-free runs, because it can schedule notices with sub-second
precision, which experiments need once time is scaled down. The Docker demo uses the real AWS
metadata mock instead.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _rfc3339(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class _QuietServer(ThreadingHTTPServer):
    """A worker killed mid-request resets its connection, which is expected here."""

    def handle_error(self, request, client_address) -> None:
        pass


class FakeIMDS:
    """Serves the IMDSv2 token, spot notice, rebalance and identity paths."""

    def __init__(
        self,
        imdsv2: bool = True,
        instance_id: str = "i-fake",
        instance_type: str = "c5.large",
        az: str = "ap-south-1a",
    ) -> None:
        self.imdsv2 = imdsv2
        self.identity = {"instance-id": instance_id, "instance-type": instance_type,
                         "placement/availability-zone": az}
        self.valid_tokens: set[str] = set()
        self.token_requests = 0
        self.fail_with: int | None = None
        self._notice_at: float | None = None
        self._notice_body: str | None = None
        self._rebalance_at: float | None = None
        self._rebalance_body: str | None = None
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send(self, status: int, body: str = "") -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_PUT(self) -> None:
                if self.path == "/latest/api/token":
                    token = secrets.token_hex(8)
                    with owner._lock:
                        owner.valid_tokens.add(token)
                        owner.token_requests += 1
                    self._send(200, token)
                else:
                    self._send(404)

            def do_GET(self) -> None:
                if owner.fail_with:
                    self._send(owner.fail_with)
                    return
                if owner.imdsv2:
                    token = self.headers.get("X-aws-ec2-metadata-token")
                    with owner._lock:
                        ok = token in owner.valid_tokens
                    if not ok:
                        self._send(401)
                        return
                body = owner._resolve(self.path)
                if body is None:
                    self._send(404)
                else:
                    self._send(200, body)

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _resolve(self, path: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            if path == "/latest/meta-data/spot/instance-action":
                if self._notice_at is not None and now >= self._notice_at:
                    return self._notice_body
                return None
            if path == "/latest/meta-data/events/recommendations/rebalance":
                if self._rebalance_at is not None and now >= self._rebalance_at:
                    return self._rebalance_body
                return None
        prefix = "/latest/meta-data/"
        if path.startswith(prefix):
            return self.identity.get(path[len(prefix) :])
        return None

    def start(self) -> FakeIMDS:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> FakeIMDS:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def expire_tokens(self) -> None:
        """Invalidate all tokens, so the next GET gets a 401."""
        with self._lock:
            self.valid_tokens.clear()

    def schedule_notice(
        self, delay_sec: float, lead_sec: float = 120.0, action: str = "terminate"
    ) -> None:
        """A notice appears after ``delay_sec``, with its deadline ``lead_sec`` after that."""
        deadline = datetime.now(UTC) + timedelta(seconds=delay_sec + lead_sec)
        with self._lock:
            self._notice_at = time.monotonic() + delay_sec
            self._notice_body = json.dumps({"action": action, "time": _rfc3339(deadline)})

    def schedule_rebalance(self, delay_sec: float) -> None:
        """A rebalance recommendation appears after ``delay_sec``."""
        when = datetime.now(UTC) + timedelta(seconds=delay_sec)
        with self._lock:
            self._rebalance_at = time.monotonic() + delay_sec
            self._rebalance_body = json.dumps({"noticeTime": _rfc3339(when)})

    def clear(self) -> None:
        """Remove any scheduled signals, like a brand new instance."""
        with self._lock:
            self._notice_at = self._notice_body = None
            self._rebalance_at = self._rebalance_body = None
