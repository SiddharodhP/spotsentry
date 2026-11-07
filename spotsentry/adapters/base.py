"""The contract between SpotSentry and a job's state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class Tier(StrEnum):
    """How much state a checkpoint holds."""

    FULL = "full"
    LIGHT = "light"
    TINY = "tiny"


@dataclass(frozen=True)
class Position:
    """Where the job is. ``step`` counts batches or items completed in this epoch."""

    epoch: int = 0
    step: int = 0


@runtime_checkable
class Checkpointable(Protocol):
    """What a job must provide so SpotSentry can save and restore it."""

    def capture(self, tier: Tier) -> dict:
        """Return an in-memory snapshot. Must be fast: CPU copies only, no I/O."""
        ...

    def restore(self, state: dict) -> None:
        """Load a snapshot produced by :meth:`capture`."""
        ...

    def supported_tiers(self) -> list[Tier]:
        """Return the tiers this adapter can capture, largest first."""
        ...
