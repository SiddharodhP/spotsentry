"""Checkpoint interval from Young's formula, re-estimated while the job runs.

``interval = clamp(sqrt(2 * C * M), min, max)``, all in seconds, where C is the time the job is
blocked per checkpoint and M is the mean time between interruptions.
"""

from __future__ import annotations

import math

from spotsentry.config import CheckpointConfig

DEFAULT_MTBI_SEC = 3600.0
PRIOR_WEIGHT = 3
C_ALPHA = 0.3


def young_interval(cost_sec: float, mtbi_sec: float) -> float:
    """Return Young's optimal checkpoint interval, in seconds."""
    return math.sqrt(2.0 * cost_sec * mtbi_sec)


class IntervalTuner:
    """Tracks C and M. M comes from, in order: an override, the plan, the configured prior."""

    def __init__(self, cfg: CheckpointConfig, plan_mtbi_sec: float | None = None) -> None:
        self.cfg = cfg
        self.fixed = None if cfg.interval == "auto" else float(cfg.interval)
        self.override = cfg.mtbi_seconds_override
        self.prior = plan_mtbi_sec or cfg.mtbi_seconds_prior or DEFAULT_MTBI_SEC
        self.cost: float | None = None
        self.past_runtime = 0.0
        self.past_interruptions = 0
        self.interval = self.fixed if self.fixed is not None else cfg.min_interval_sec

    def set_history(
        self, runtime_sec: float, interruptions: int, cost_sec: float | None = None
    ) -> None:
        """Record what earlier workers on this job experienced.

        With a known checkpoint cost the interval is set straight away. Otherwise a replacement
        worker would start at the minimum interval, and spend a whole checkpoint re-measuring it.
        """
        self.past_runtime = runtime_sec
        self.past_interruptions = interruptions
        if cost_sec is not None:
            self.cost = cost_sec
            self.recompute()

    def mtbi(self, current_runtime_sec: float = 0.0) -> float:
        """Current estimate of M, in seconds."""
        if self.override is not None:
            return float(self.override)
        observed = self.past_runtime + current_runtime_sec
        return (PRIOR_WEIGHT * self.prior + observed) / (PRIOR_WEIGHT + self.past_interruptions)

    def observe_cost(self, seconds: float) -> None:
        """Feed in how long the job was blocked by the latest checkpoint."""
        if self.cost is None:
            self.cost = seconds
        else:
            self.cost = C_ALPHA * seconds + (1 - C_ALPHA) * self.cost

    def recompute(self, current_runtime_sec: float = 0.0) -> float:
        """Recalculate the interval and return it."""
        if self.fixed is not None:
            return self.fixed
        if self.cost is None:
            return self.interval
        raw = young_interval(self.cost, self.mtbi(current_runtime_sec))
        self.interval = min(max(raw, self.cfg.min_interval_sec), self.cfg.max_interval_sec)
        return self.interval
