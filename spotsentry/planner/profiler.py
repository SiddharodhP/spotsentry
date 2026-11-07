"""Run a few real steps of a job and measure what the plan needs."""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from spotsentry import serialize
from spotsentry.adapters.base import Tier
from spotsentry.config import SpotSentryConfig
from spotsentry.events import utc_now_iso
from spotsentry.store import make_store
from spotsentry.workload import load_workload

UPLOAD_TEST_BYTES = 20 * 1000 * 1000
HISTORY_FILE = Path(__file__).resolve().parent / "history.jsonl"


@dataclass
class Profile:
    """Measurements from one profiling run, on the machine it ran on."""

    job_id: str
    mean_step_sec: float
    std_step_sec: float
    steps_per_epoch: int
    epochs: int
    predicted_runtime_sec: float
    predicted_low_sec: float
    predicted_high_sec: float
    eval_sec_per_epoch: float
    tier_bytes: dict[str, int] = field(default_factory=dict)
    capture_sec: dict[str, float] = field(default_factory=dict)
    serialize_sec: dict[str, float] = field(default_factory=dict)
    upload_mbps: float | None = None
    upload_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def measure_upload(config: SpotSentryConfig) -> tuple[float | None, str]:
    """Upload a 20 MB test object, delete it, and return the speed in megabits per second."""
    key = f"{config.store.prefix}{config.job_id}/.upload-test"
    try:
        store = make_store(config.store)
        started = time.monotonic()
        store.upload(key, b"\0" * UPLOAD_TEST_BYTES)
        elapsed = time.monotonic() - started
        store.delete(key)
        return UPLOAD_TEST_BYTES * 8 / 1e6 / elapsed, "measured"
    except Exception as exc:  # noqa: BLE001
        return None, f"not measured: {type(exc).__name__}"


def profile_workload(
    config: SpotSentryConfig,
    warmup_steps: int = 10,
    profile_steps: int = 50,
    upload_test: bool = True,
) -> Profile:
    """Measure step time, checkpoint sizes and upload speed by running the real job briefly."""
    if config.workload is None:
        raise ValueError("this job has no workload section")
    workload = load_workload(config.workload)
    workload.bind(None)
    adapter = workload.adapter()

    wanted = warmup_steps + profile_steps
    durations: list[float] = []
    done = 0
    epoch = 0
    while done < wanted and epoch < workload.epochs:
        iterator = workload.iter_steps(epoch, 0)
        while done < wanted:
            started = time.perf_counter()
            try:
                _, batch = next(iterator)
            except StopIteration:
                break
            workload.train_step(batch)
            durations.append(time.perf_counter() - started)
            done += 1
        epoch += 1
    measured = durations[warmup_steps:] or durations
    mean_step = statistics.fmean(measured)
    std_step = statistics.pstdev(measured) if len(measured) > 1 else 0.0

    eval_started = time.perf_counter()
    workload.end_epoch(0)
    eval_sec = time.perf_counter() - eval_started

    tier_bytes: dict[str, int] = {}
    capture_sec: dict[str, float] = {}
    serialize_sec: dict[str, float] = {}
    for tier in adapter.supported_tiers():
        if not isinstance(tier, Tier):
            tier = Tier(tier)
        started = time.perf_counter()
        state = adapter.capture(tier)
        capture_sec[tier.value] = time.perf_counter() - started
        started = time.perf_counter()
        payload = serialize.dumps(state)
        serialize_sec[tier.value] = time.perf_counter() - started
        tier_bytes[tier.value] = len(payload)

    total_steps = workload.steps_per_epoch * workload.epochs
    predicted = mean_step * total_steps + eval_sec * workload.epochs
    upload_mbps, note = measure_upload(config) if upload_test else (None, "skipped")
    return Profile(
        job_id=config.job_id,
        mean_step_sec=mean_step,
        std_step_sec=std_step,
        steps_per_epoch=workload.steps_per_epoch,
        epochs=workload.epochs,
        predicted_runtime_sec=predicted,
        predicted_low_sec=(mean_step - std_step) * total_steps + eval_sec * workload.epochs,
        predicted_high_sec=(mean_step + std_step) * total_steps + eval_sec * workload.epochs,
        eval_sec_per_epoch=eval_sec,
        tier_bytes=tier_bytes,
        capture_sec=capture_sec,
        serialize_sec=serialize_sec,
        upload_mbps=upload_mbps,
        upload_note=note,
    )


def record_prediction(profile: Profile, instance_type: str, path: Path = HISTORY_FILE) -> None:
    """Add a prediction to the history, so experiment E4 can report prediction error."""
    entry = {
        "ts": utc_now_iso(),
        "job_id": profile.job_id,
        "instance_type": instance_type,
        "predicted_runtime_sec": profile.predicted_runtime_sec,
        "mean_step_sec": profile.mean_step_sec,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
