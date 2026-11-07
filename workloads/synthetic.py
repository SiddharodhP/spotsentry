"""A fully controllable job: choose its step time and state size.

It keeps a hash chain over every step it performs. If a run skips or repeats any step, its final
chain differs from :func:`expected_chain`, which makes "resume is exact" directly checkable.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from typing import Any

import numpy as np

from spotsentry.adapters.base import Tier
from spotsentry.workload import Workload

_FLOAT32_BYTES = 4


def _advance(chain: bytes, step_number: int) -> bytes:
    return hashlib.sha256(chain + step_number.to_bytes(8, "big")).digest()


def expected_chain(total_steps: int) -> str:
    """The chain value an uninterrupted run of ``total_steps`` ends with."""
    chain = b"spotsentry"
    for number in range(total_steps):
        chain = _advance(chain, number)
    return chain.hex()


class SyntheticAdapter:
    """``full``: model + optimizer. ``light``: model. ``tiny``: model as float16."""

    def __init__(self, job: SyntheticJob) -> None:
        self.job = job
        self.last_restore_info: dict[str, Any] = {}

    def supported_tiers(self) -> list[Tier]:
        return [Tier.FULL, Tier.LIGHT, Tier.TINY]

    def capture(self, tier: Tier) -> dict:
        job = self.job
        state: dict[str, Any] = {"steps_done": job.steps_done, "chain": job.chain.hex()}
        if tier is Tier.TINY:
            state["model"] = job.model.astype(np.float16)
            return state
        state["model"] = job.model.copy()
        if tier is Tier.FULL:
            state["optimizer"] = job.optimizer.copy()
        return state

    def restore(self, state: dict) -> None:
        job = self.job
        job.steps_done = int(state["steps_done"])
        job.chain = bytes.fromhex(state["chain"])
        job.model = np.asarray(state["model"], dtype=np.float32).copy()
        reset = "optimizer" not in state
        if reset:
            job.optimizer = np.zeros_like(job.optimizer)
        else:
            job.optimizer = state["optimizer"]
        self.last_restore_info = {"optimizer_reset": reset}

    def estimate_tier_bytes(self) -> dict[Tier, int]:
        model = self.job.model.nbytes
        return {Tier.FULL: model + self.job.optimizer.nbytes, Tier.LIGHT: model,
                Tier.TINY: model // 2}


class SyntheticJob(Workload):
    """Each step waits ``step_seconds`` and updates a state of ``state_mb`` megabytes."""

    def __init__(
        self,
        total_steps: int = 1000,
        epochs: int = 1,
        step_seconds: float = 0.05,
        state_mb: float = 10.0,
        optimizer_multiplier: float = 2.0,
        compute: bool = False,
    ) -> None:
        self.epochs = epochs
        self.steps_per_epoch = total_steps // epochs
        self.total_steps = self.steps_per_epoch * epochs
        self.step_seconds = step_seconds
        self.compute = compute
        elements = max(int(state_mb * 1_000_000 / _FLOAT32_BYTES), 1)
        self.model = np.zeros(elements, dtype=np.float32)
        self.optimizer = np.zeros(int(elements * optimizer_multiplier), dtype=np.float32)
        self.steps_done = 0
        self.chain = b"spotsentry"
        self._adapter = SyntheticAdapter(self)

    def adapter(self) -> SyntheticAdapter:
        return self._adapter

    def iter_steps(self, epoch: int, start_step: int) -> Iterator[tuple[int, Any]]:
        for step in range(start_step, self.steps_per_epoch):
            yield step, None

    def train_step(self, batch: Any) -> dict[str, Any]:
        if self.compute:
            deadline = time.perf_counter() + self.step_seconds
            while time.perf_counter() < deadline:
                pass
        elif self.step_seconds > 0:
            time.sleep(self.step_seconds)
        self.chain = _advance(self.chain, self.steps_done)
        self.model[self.steps_done % self.model.size] += 1.0
        if self.optimizer.size:
            self.optimizer[self.steps_done % self.optimizer.size] += 1.0
        self.steps_done += 1
        return {"steps_done": self.steps_done}

    def result(self) -> dict[str, Any]:
        return {
            "steps_done": self.steps_done,
            "chain": self.chain.hex(),
            "model_sum": float(self.model.sum()),
            "optimizer_sum": float(self.optimizer.sum()),
        }
