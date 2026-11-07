"""What ``spotsentry run`` expects from a job, and the loop that drives it."""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from spotsentry.adapters.base import Checkpointable, Position
from spotsentry.config import SpotSentryConfig, WorkloadConfig
from spotsentry.errors import ConfigError, SpotInterrupted

if TYPE_CHECKING:
    from spotsentry.core import SpotSentry


class Workload(ABC):
    """A job that can be run, profiled and resumed by SpotSentry."""

    epochs: int = 1
    steps_per_epoch: int = 1

    def bind(self, sentry: SpotSentry | None) -> None:  # noqa: B027
        """Called before the job starts. Override to reach the store."""

    @abstractmethod
    def adapter(self) -> Checkpointable:
        """Return the adapter holding this job's state."""

    @abstractmethod
    def iter_steps(self, epoch: int, start_step: int) -> Iterator[tuple[int, Any]]:
        """Yield (step, batch) for the rest of an epoch."""

    @abstractmethod
    def train_step(self, batch: Any) -> dict[str, Any]:
        """Do one unit of work and return metrics for the log."""

    def end_epoch(self, epoch: int) -> dict[str, Any]:
        """Called after each epoch. Return metrics for the log."""
        return {}

    def result(self) -> dict[str, Any]:
        """Return the job's final result."""
        return {}


def load_workload(cfg: WorkloadConfig) -> Workload:
    """Instantiate the class named by ``module.path:ClassName``."""
    if ":" not in cfg.class_path:
        raise ConfigError(f"workload class must look like 'module:Class', got {cfg.class_path!r}")
    module_name, class_name = cfg.class_path.split(":", 1)
    return getattr(importlib.import_module(module_name), class_name)(**cfg.params)


def run_loop(workload: Workload, sentry: SpotSentry, start: Position) -> None:
    """Run the job from ``start`` to the end, calling a safe point after every step."""
    for epoch in range(start.epoch, workload.epochs):
        first = start.step if epoch == start.epoch else 0
        for step, batch in workload.iter_steps(epoch, first):
            metrics = workload.train_step(batch)
            sentry.step(epoch=epoch, step=step + 1, **metrics)
        metrics = workload.end_epoch(epoch)
        sentry.end_epoch(epoch=epoch + 1, **metrics)


def run_job(config: SpotSentryConfig) -> int:
    """Run a configured job to completion or interruption. Returns the process exit code."""
    from spotsentry.core import SpotSentry

    if config.workload is None:
        raise ConfigError("this job has no workload section")
    workload = load_workload(config.workload)
    sentry = SpotSentry(config)
    workload.bind(sentry)
    adapter = workload.adapter()
    sentry.set_progress_total(workload.epochs, workload.steps_per_epoch)
    try:
        start = sentry.start(adapter)
        run_loop(workload, sentry, start)
        sentry.complete(result=workload.result())
        return 0
    except SpotInterrupted as exc:
        sentry.exit_interrupted(exc)
    except Exception as exc:  # noqa: BLE001
        code = sentry.fail(exc)
        raise SystemExit(code) from exc
