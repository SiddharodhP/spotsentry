"""Run one experiment trial: workers as local processes, with a metadata service we control.

Experiments use ``FakeIMDS`` rather than the AWS metadata mock because time is scaled down, so
notices need sub-second precision, and because many trials run in parallel. The store is either a
folder or MinIO. See docs/NOTES.md for why this is a fair substitute.

Lost work is read from each worker's own stdout, which survives the worker being killed.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from spotsentry.testing.fake_imds import FakeIMDS  # noqa: E402

KILLED = -9
EXPECTED_EXIT_CODES = (0, 75, 76)
TMP_DIR = REPO_ROOT / "experiments" / "tmp"


class WorkerCrashed(RuntimeError):
    """A worker failed with an error. Retrying would only hide the bug until the time limit."""


@dataclass
class TrialSpec:
    """Everything that defines one trial."""

    job: dict[str, Any]
    mean_interruption_sec: float | None = None
    seed: int = 0
    notice_lead_sec: float = 4.0
    restart_delay_sec: float = 2.0
    max_wall_sec: float = 600.0
    fixed_interruptions_sec: list[float] | None = None
    store_env: dict[str, str] = field(default_factory=dict)
    single_worker: bool = False
    #: Count interruption delays from when training starts rather than when the process starts.
    #: Workers importing torch take seconds, so otherwise a short job may never get interrupted.
    delay_from_job_start: bool = False


@dataclass
class TrialResult:
    """What one trial measured."""

    completed: bool
    wall_sec: float
    busy_sec: float
    workers: int
    interruptions: int
    lost_steps: int
    lost_work_sec: float
    blocking_sec: float
    checkpoints: int
    uninterrupted_sec: float
    result: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("result")
        data.pop("events")
        return data


def _interruption_times(spec: TrialSpec) -> list[float]:
    """Time until each worker is interrupted, drawn from an exponential distribution."""
    if spec.fixed_interruptions_sec is not None:
        return list(spec.fixed_interruptions_sec)
    if spec.mean_interruption_sec is None:
        return []
    rng = random.Random(spec.seed)
    return [rng.expovariate(1.0 / spec.mean_interruption_sec) for _ in range(10000)]


def _read_lines(stream, sink: list[dict[str, Any]]) -> None:
    for raw in stream:
        line = raw.strip()
        if line.startswith("{"):
            try:
                sink.append(json.loads(line))
            except ValueError:
                pass


def position_of(event: dict[str, Any], steps_per_epoch: int) -> int | None:
    """Total steps implied by an event's epoch and step, if it has them."""
    if "epoch" not in event or "step" not in event:
        return None
    return int(event["epoch"]) * steps_per_epoch + int(event["step"])


def run_trial(spec: TrialSpec) -> TrialResult:
    """Run a job through worker after worker until it finishes or runs out of time."""
    # Next to the project rather than the system temp folder: checkpoints can be gigabytes.
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="trial-", dir=TMP_DIR))
    imds = FakeIMDS().start()
    job = json.loads(json.dumps(spec.job))
    job.setdefault("store", {"type": "local"})
    if job["store"].get("type", "local") == "local":
        job["store"]["type"] = "local"
        job["store"]["root"] = str(work_dir / "store")
    job.setdefault("metadata", {})["base_url"] = imds.base_url
    job_path = work_dir / "job.yaml"
    job_path.write_text(yaml.safe_dump(job), encoding="utf-8")

    params = job["workload"]["params"]
    all_events: list[dict[str, Any]] = []
    interruption_times = _interruption_times(spec)
    started = time.monotonic()
    busy = 0.0
    workers = interruptions = lost_steps = 0
    previous_end_position = 0
    steps_per_epoch = 0
    step_seconds = float(params.get("step_seconds", 0.0))
    completed = False

    env = {
        **os.environ,
        **spec.store_env,
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
        "PYTHONPATH": str(REPO_ROOT),
        # OpenBLAS reserves memory per thread when numpy is imported, in every worker. With many
        # trials at once that exhausted Windows' commit limit, even though the jobs never use it.
        "OPENBLAS_NUM_THREADS": "1",
    }
    try:
        while time.monotonic() - started < spec.max_wall_sec:
            imds.clear()
            workers += 1
            delay = interruption_times.pop(0) if interruption_times else None
            pending_delay = delay if spec.delay_from_job_start else None
            if delay is not None and pending_delay is None:
                imds.schedule_notice(delay, spec.notice_lead_sec)
            worker_env = {**env, "SPOTSENTRY_INSTANCE_ID": f"i-{workers:04d}"}
            worker_started = time.monotonic()
            stderr_path = work_dir / f"worker-{workers}.err"
            stderr_file = stderr_path.open("w", encoding="utf-8")
            process = subprocess.Popen(
                [sys.executable, "-m", "spotsentry.cli", "run", "--job", str(job_path)],
                env=worker_env,
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
            )
            events: list[dict[str, Any]] = []
            reader = threading.Thread(target=_read_lines, args=(process.stdout, events))
            reader.start()
            kill_at = None
            if delay is not None and pending_delay is None:
                kill_at = worker_started + delay + spec.notice_lead_sec
            killed = False
            while process.poll() is None:
                now = time.monotonic()
                if pending_delay is not None and any(e.get("type") == "job_start" for e in events):
                    imds.schedule_notice(pending_delay, spec.notice_lead_sec)
                    kill_at = now + pending_delay + spec.notice_lead_sec
                    pending_delay = None
                over_limit = now - started >= spec.max_wall_sec
                if (kill_at is not None and now >= kill_at) or over_limit:
                    process.kill()
                    killed = True
                    break
                time.sleep(0.02)
            process.wait()
            stderr_file.close()
            reader.join(timeout=5)
            if not killed and process.returncode not in EXPECTED_EXIT_CODES:
                tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-1500:]
                raise WorkerCrashed(f"worker exited {process.returncode}:\n{tail}")
            busy += time.monotonic() - worker_started
            all_events.extend(events)

            start_event = next((e for e in events if e.get("type") == "job_start"), None)
            if start_event and start_event.get("steps_per_epoch"):
                steps_per_epoch = int(start_event["steps_per_epoch"])
            restored_at = 0
            if start_event and steps_per_epoch:
                restored_at = position_of(start_event, steps_per_epoch) or 0
            lost_steps += max(previous_end_position - restored_at, 0)
            reached = restored_at
            if steps_per_epoch:
                for event in events:
                    position = position_of(event, steps_per_epoch)
                    if position is not None:
                        reached = max(reached, position)
                    if "steps_done" in event:
                        reached = max(reached, int(event["steps_done"]))
            if killed:
                # Steps between the last log line and the kill are not visible; estimate them.
                last = events[-1] if events else None
                if last is not None and step_seconds > 0 and last.get("type") == "job_progress":
                    last_time = _event_time(last)
                    reached += int(max(time.time() - last_time, 0.0) / step_seconds)
            previous_end_position = reached

            if any(e.get("type") == "interruption_notice" for e in events):
                interruptions += 1
            elif killed and delay is not None:
                interruptions += 1
            if process.returncode == 0 and any(e.get("type") == "job_complete" for e in events):
                completed = True
                break
            if spec.single_worker:
                break
            if time.monotonic() - started < spec.max_wall_sec:
                time.sleep(spec.restart_delay_sec)
    finally:
        imds.stop()
        shutil.rmtree(work_dir, ignore_errors=True)

    wall = time.monotonic() - started
    done = [e for e in all_events if e.get("type") == "checkpoint_done"]
    complete = next((e for e in all_events if e.get("type") == "job_complete"), {})
    total_steps = int(params.get("total_steps", 0))
    return TrialResult(
        completed=completed,
        wall_sec=wall,
        busy_sec=busy,
        workers=workers,
        interruptions=interruptions,
        lost_steps=lost_steps,
        lost_work_sec=lost_steps * step_seconds,
        blocking_sec=sum(float(e.get("blocking_s") or 0.0) for e in done),
        checkpoints=len(done),
        uninterrupted_sec=total_steps * step_seconds,
        result=complete.get("result") or {},
        events=all_events,
    )


def _event_time(event: dict[str, Any]) -> float:
    from datetime import datetime

    return datetime.fromisoformat(event["ts"].replace("Z", "+00:00")).timestamp()
