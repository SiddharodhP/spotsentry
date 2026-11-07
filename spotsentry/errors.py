"""Exceptions used across SpotSentry."""

from __future__ import annotations


class SpotSentryError(Exception):
    """Base class for SpotSentry errors."""


class ConfigError(SpotSentryError):
    """The job configuration is invalid."""


class ObjectNotFound(SpotSentryError):
    """The requested object does not exist in the store."""


class PreconditionFailed(SpotSentryError):
    """A conditional write lost the race (ETag mismatch or object already exists)."""


class UploadAborted(SpotSentryError):
    """An upload was stopped on purpose (deadline passed, or cancelled)."""


class ChecksumMismatch(SpotSentryError):
    """A downloaded checkpoint does not match the SHA-256 in its manifest entry."""


class SpotInterrupted(SpotSentryError):
    """Raised at a safe point when the job must stop.

    ``saved`` says whether the job's latest progress is safe in the store; the
    runner turns it into exit code 75 (saved) or 76 (not saved).
    """

    def __init__(self, saved: bool, reason: str = "interruption_notice") -> None:
        super().__init__(f"interrupted ({reason}), saved={saved}")
        self.saved = saved
        self.reason = reason
