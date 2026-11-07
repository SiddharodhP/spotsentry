"""Deadline-aware tier selection: save the most state that is sure to finish in time."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from spotsentry.adapters.base import Tier
from spotsentry.config import DeadlineConfig

DEFAULT_SERIALIZE_BYTES_PER_SEC = 300e6
FALLBACK_THROUGHPUT_BYTES_PER_SEC = 1e6


@dataclass
class Decision:
    """The selector's choice. ``chosen`` is None when nothing fits (skip)."""

    chosen: Tier | None
    time_left: float
    predictions: dict[str, float]


class DeadlineSelector:
    """Predicts save time per tier from measured sizes and upload speed."""

    def __init__(self, cfg: DeadlineConfig, tiers: list[Tier]) -> None:
        self.cfg = cfg
        self.tiers = [t for t in (Tier.FULL, Tier.LIGHT, Tier.TINY) if t in tiers]
        self.throughput: float | None = None
        self.serialize_speed: float | None = None
        self.sizes: dict[Tier, int] = {}
        self.hints: dict[Tier, int] = {}

    def _ewma(self, current: float | None, value: float) -> float:
        if current is None:
            return value
        alpha = self.cfg.ewma_alpha
        return alpha * value + (1 - alpha) * current

    def observe_upload(self, nbytes: int, seconds: float) -> None:
        """Feed in a completed upload."""
        if seconds > 0 and nbytes > 0:
            self.throughput = self._ewma(self.throughput, nbytes / seconds)

    def observe_serialize(self, tier: Tier, nbytes: int, seconds: float) -> None:
        """Feed in a measured serialization."""
        self.sizes[tier] = nbytes
        if seconds > 0 and nbytes > 0:
            self.serialize_speed = self._ewma(self.serialize_speed, nbytes / seconds)

    def set_hints(self, hints: dict[Tier, int]) -> None:
        """Provide sizes estimated from the state, used until a tier has been measured."""
        self.hints = hints

    def size_of(self, tier: Tier) -> int | None:
        """Best known size of a tier in bytes, measured first, then estimated."""
        if tier in self.sizes:
            return self.sizes[tier]
        if tier in self.hints:
            full = self.sizes.get(Tier.FULL)
            hint_full = self.hints.get(Tier.FULL)
            if full and hint_full:  # scale the estimate by how far off the full estimate was
                return int(self.hints[tier] * full / hint_full)
            return self.hints[tier]
        if tier is Tier.TINY and Tier.LIGHT in self.sizes:
            return self.sizes[Tier.LIGHT] // 2
        return None

    def missing_sizes(self) -> list[Tier]:
        """Tiers with no known or estimated size."""
        return [t for t in self.tiers if self.size_of(t) is None]

    def predict(self, tier: Tier) -> float:
        """Predicted seconds to serialize and upload a tier."""
        size = self.size_of(tier)
        if size is None:
            return math.inf
        serialize_speed = self.serialize_speed or DEFAULT_SERIALIZE_BYTES_PER_SEC
        throughput = self.throughput or FALLBACK_THROUGHPUT_BYTES_PER_SEC
        return size / serialize_speed + size / throughput * self.cfg.speed_safety_factor

    def time_left(self, deadline: datetime, now: datetime) -> float:
        """Usable seconds before ``deadline``, after the safety margin."""
        return (deadline - now).total_seconds() - self.cfg.safety_margin_sec

    def choose(self, time_left: float) -> Decision:
        """Pick the largest tier predicted to finish in time, or None to skip.

        In ``naive`` mode (the experiment baseline) this always picks the largest tier.
        """
        predictions = {t.value: self.predict(t) for t in self.tiers}
        if self.cfg.mode == "naive":
            return Decision(self.tiers[0], time_left, predictions)
        for tier in self.tiers:
            if predictions[tier.value] < time_left:
                return Decision(tier, time_left, predictions)
        return Decision(None, time_left, predictions)
