"""Client for the EC2 instance metadata service (IMDSv2), or a mock of it."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import requests

from spotsentry.events import Identity

log = logging.getLogger(__name__)

TOKEN_PATH = "/latest/api/token"
NOTICE_PATH = "/latest/meta-data/spot/instance-action"
REBALANCE_PATH = "/latest/meta-data/events/recommendations/rebalance"
TOKEN_TTL_SEC = 21600
TOKEN_REFRESH_MARGIN_SEC = 60


@dataclass(frozen=True)
class Notice:
    """A spot interruption notice: the instance goes away at ``time``."""

    action: str
    time: datetime


def parse_time(value: str) -> datetime:
    """Parse an RFC 3339 timestamp into an aware UTC datetime."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class MetadataClient:
    """Reads interruption signals. Network problems mean "no notice", never a crash."""

    def __init__(self, base_url: str, imdsv2: bool = True, timeout: float = 1.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.imdsv2 = imdsv2
        self.timeout = timeout
        self._session = requests.Session()
        self._session.trust_env = False  # never send metadata requests through a proxy
        self._token: str | None = None
        self._token_expires = 0.0
        self._last_warning = 0.0

    def _warn(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_warning > 60:
            self._last_warning = now
            log.warning("metadata: %s", message)

    def _refresh_token(self) -> None:
        response = self._session.put(
            self.base_url + TOKEN_PATH,
            headers={"X-aws-ec2-metadata-token-ttl-seconds": str(TOKEN_TTL_SEC)},
            timeout=self.timeout,
        )
        response.raise_for_status()
        self._token = response.text.strip()
        self._token_expires = time.monotonic() + TOKEN_TTL_SEC

    def _headers(self) -> dict[str, str]:
        if not self.imdsv2:
            return {}
        if self._token is None or time.monotonic() > self._token_expires - TOKEN_REFRESH_MARGIN_SEC:
            self._refresh_token()
        return {"X-aws-ec2-metadata-token": self._token or ""}

    def _get(self, path: str) -> str | None:
        """GET a metadata path. Returns None on 404 or any failure."""
        try:
            response = self._session.get(
                self.base_url + path, headers=self._headers(), timeout=self.timeout
            )
            if response.status_code == 401 and self.imdsv2:
                self._token = None
                response = self._session.get(
                    self.base_url + path, headers=self._headers(), timeout=self.timeout
                )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            self._warn(f"could not read {path}: {exc}")
            return None

    def get_interruption_notice(self) -> Notice | None:
        """Return the interruption notice, or None if there is none yet."""
        import json

        body = self._get(NOTICE_PATH)
        if not body:
            return None
        try:
            data = json.loads(body)
            return Notice(action=str(data["action"]), time=parse_time(str(data["time"])))
        except (ValueError, KeyError, TypeError) as exc:
            self._warn(f"bad interruption notice {body!r}: {exc}")
            return None

    def get_rebalance(self) -> datetime | None:
        """Return the rebalance recommendation's time, or None if there is none yet."""
        import json

        body = self._get(REBALANCE_PATH)
        if not body:
            return None
        try:
            return parse_time(str(json.loads(body)["noticeTime"]))
        except (ValueError, KeyError, TypeError) as exc:
            self._warn(f"bad rebalance recommendation {body!r}: {exc}")
            return None

    def instance_identity(self) -> Identity:
        """Return instance id, type and zone, using "local" for anything unavailable."""
        return Identity(
            instance_id=(self._get("/latest/meta-data/instance-id") or "local").strip(),
            instance_type=(self._get("/latest/meta-data/instance-type") or "local").strip(),
            az=(self._get("/latest/meta-data/placement/availability-zone") or "local").strip(),
        )
