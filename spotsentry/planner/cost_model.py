"""Expected time and cost of running a job on spot. All times here are in hours."""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_RESTART_HOURS = 0.05  # 3 minutes for boot and restore until measured


@dataclass
class CostInputs:
    runtime_h: float  # R: predicted run time on this instance type
    checkpoint_cost_h: float  # C: job-blocking time per checkpoint
    mtbi_h: float  # M: mean time between interruptions
    interval_h: float  # T: checkpoint interval
    spot_price: float
    on_demand_price: float
    restart_h: float = DEFAULT_RESTART_HOURS


@dataclass
class CostEstimate:
    expected_interruptions: float
    wall_time_h: float
    cost_spot: float
    cost_on_demand: float
    wall_time_p90_h: float

    @property
    def savings_fraction(self) -> float:
        if self.cost_on_demand <= 0:
            return 0.0
        return 1.0 - self.cost_spot / self.cost_on_demand


def young_interval_hours(cost_h: float, mtbi_h: float, min_h: float, max_h: float) -> float:
    """Young's interval, clamped."""
    return min(max(math.sqrt(2.0 * cost_h * mtbi_h), min_h), max_h)


def estimate(inputs: CostInputs) -> CostEstimate:
    """W_spot = R + (R / T) * C + E * (T / 2 + restart), with E = R / M."""
    r, c, m, t = inputs.runtime_h, inputs.checkpoint_cost_h, inputs.mtbi_h, inputs.interval_h
    expected = r / m
    base = r + (r / t) * c
    per_interruption = t / 2.0 + inputs.restart_h
    wall = base + expected * per_interruption
    e90 = expected + 1.28 * math.sqrt(expected)
    return CostEstimate(
        expected_interruptions=expected,
        wall_time_h=wall,
        cost_spot=wall * inputs.spot_price,
        cost_on_demand=r * inputs.on_demand_price,
        wall_time_p90_h=base + e90 * per_interruption,
    )
