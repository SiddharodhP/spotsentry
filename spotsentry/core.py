"""The runtime that jobs use: the SpotSentry class."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from spotsentry.adapters.base import Checkpointable, Position, Tier
from spotsentry.checkpoint import CheckpointManager
from spotsentry.config import SpotSentryConfig, load_config
from spotsentry.deadline import DeadlineSelector, Decision
from spotsentry.errors import ObjectNotFound, SpotInterrupted, UploadAborted
from spotsentry.events import (
    EventLogger,
    Identity,
    last_checkpoint_cost,
    read_events,
    run_history,
)
from spotsentry.interval import IntervalTuner
from spotsentry.lease import Lease, LeaseStopped
from spotsentry.metadata import MetadataClient
from spotsentry.serialize import dumps
from spotsentry.store import Store, make_store
from spotsentry.uploader import SaveResult, Uploader
from spotsentry.watcher import Watcher

EXIT_COMPLETED = 0
EXIT_ERROR = 1
EXIT_SAVED = 75
EXIT_NOT_SAVED = 76
PROBE_BYTES = 5 * 1024 * 1024


class SpotSentry:
    """Keeps a long-running job's progress safe across spot interruptions.

    Typical use::

        sentry = SpotSentry.from_config("jobs/cifar_local.yaml")
        start = sentry.start(adapter)
        try:
            for epoch in range(start.epoch, EPOCHS):
                for step, batch in adapter.iter_batches(data, epoch, first_step):
                    train_step(batch)
                    sentry.step(epoch=epoch, step=step + 1)
                sentry.end_epoch(epoch=epoch + 1)
            sentry.complete(result={"accuracy": acc})
        except SpotInterrupted as e:
            sentry.exit_interrupted(e)
    """

    def __init__(
        self,
        config: SpotSentryConfig,
        store: Store | None = None,
        metadata: MetadataClient | None = None,
    ) -> None:
        self.config = config
        self.store = store if store is not None else make_store(config.store)
        self.job_root = f"{config.store.prefix}{config.job_id}/"
        self.metadata = metadata
        if self.metadata is None and config.metadata.enabled:
            self.metadata = MetadataClient(
                config.metadata.base_url,
                imdsv2=config.metadata.imdsv2,
                timeout=config.metadata.timeout_sec,
            )
        self.identity = self._resolve_identity()
        self.owner = f"{self.identity.instance_id}/{socket.gethostname()}/{os.getpid()}"
        self.events = EventLogger(
            self.store,
            self.job_root,
            config.job_id,
            self.identity,
            flush_interval=config.logging.flush_interval_sec,
            stdout=config.logging.stdout,
        )
        self.lease: Lease | None = None
        if config.lease.enabled:
            self.lease = Lease(
                self.store,
                self.job_root,
                self.owner,
                ttl_sec=config.lease.ttl_sec,
                heartbeat_sec=config.lease.heartbeat_sec,
                poll_sec=config.lease.poll_sec,
                events=self.events,
            )
        self.manager = CheckpointManager(
            self.store,
            self.job_root,
            self.identity.instance_id,
            keep_last=config.checkpoint.keep_last,
            events=self.events,
            fence=(lambda: self.lease.lost) if self.lease else None,
        )
        tiers = [Tier(t) for t in config.checkpoint.tiers]
        self.selector = DeadlineSelector(config.deadline, tiers)
        self.tuner = IntervalTuner(config.checkpoint)
        self.uploader = Uploader(
            self.manager,
            self.selector,
            self.events,
            on_saved=self._on_saved,
            compress=config.checkpoint.compress,
        )
        self.watcher: Watcher | None = None
        if self.metadata is not None:
            self.watcher = Watcher(self.metadata, self.events, config.metadata.poll_interval_sec)
        self.adapter: Checkpointable | None = None
        self.position = Position()
        self.plan: dict[str, Any] = {}
        self.total_steps: int | None = None
        self.steps_per_epoch: int | None = None
        self.epochs: int | None = None
        self._saved_position: Position | None = None
        self._last_checkpoint = time.monotonic()
        self._started = time.monotonic()
        self._steps_seen = 0
        self._rebalance_handled = False
        self._handover_pending = False
        self._next_handover_check = 0.0
        self._budget_started: float | None = None
        self._shut_down = False

    @classmethod
    def from_config(cls, path: str | Path) -> SpotSentry:
        """Create a SpotSentry from a YAML job file."""
        return cls(load_config(path))

    def _resolve_identity(self) -> Identity:
        identity = self.metadata.instance_identity() if self.metadata else Identity()
        override = os.environ.get("SPOTSENTRY_INSTANCE_ID")
        if override:
            identity = Identity(override, identity.instance_type, identity.az)
        return identity

    # Lifecycle ---------------------------------------------------------------------------

    def set_progress_total(self, epochs: int, steps_per_epoch: int) -> None:
        """Tell SpotSentry the job's size, so the dashboard can show a progress bar."""
        self.epochs = epochs
        self.steps_per_epoch = steps_per_epoch
        self.total_steps = epochs * steps_per_epoch

    def start(self, adapter: Checkpointable) -> Position:
        """Take the lease, restore the latest checkpoint if there is one, and start watching."""
        self.adapter = adapter
        self.events.start()
        if self.watcher is not None:
            self.watcher.start()
        self._exit_if_already_completed()
        if self.lease is not None:
            try:
                self.lease.acquire(
                    stop_check=(lambda: self.watcher.notice_event.is_set())
                    if self.watcher
                    else None
                )
            except LeaseStopped:
                self.events.log("interrupted_exit", saved=False, reason="notice_while_waiting")
                self._shutdown(release_lease=False)
                sys.exit(EXIT_NOT_SAVED)
            self.lease.start_heartbeat()
            self._exit_if_already_completed()
        self._load_plan()
        past_events = read_events(self.store, self.job_root)
        past_runtime, past_interruptions = run_history(past_events)
        self.tuner.set_history(
            past_runtime, past_interruptions, last_checkpoint_cost(past_events)
        )
        restored = self.manager.restore(adapter)
        self.position = restored.position
        if restored.entry is not None:
            self._saved_position = restored.position
        self._prepare_selector(adapter)
        self._started = time.monotonic()
        self._last_checkpoint = self._started
        self.manager.write_status("running", instance_id=self.identity.instance_id)
        self.events.log(
            "job_start",
            flush=True,
            epoch=self.position.epoch,
            step=self.position.step,
            resumed=restored.entry is not None,
            epochs=self.epochs,
            steps_per_epoch=self.steps_per_epoch,
            strategy=self.config.checkpoint.strategy,
            interval_sec=self.tuner.interval,
            mtbi_sec=round(self.tuner.mtbi(), 3),
            past_interruptions=past_interruptions,
        )
        return self.position

    def _exit_if_already_completed(self) -> None:
        status = self.manager.read_status()
        if status and status.get("state") == "completed":
            self.events.log("job_complete", flush=True, already_completed=True)
            self._scale_to_zero()
            self._shutdown(release_lease=False)
            sys.exit(EXIT_COMPLETED)

    def _load_plan(self) -> None:
        try:
            self.plan = json.loads(self.store.get_bytes(f"{self.job_root}plan.json"))
        except (ObjectNotFound, ValueError):
            return
        mtbi = self.plan.get("mtbi_seconds")
        if mtbi:
            self.tuner.prior = float(mtbi)
        self.events.log(
            "plan_loaded",
            mtbi_seconds=mtbi,
            instance_type=self.plan.get("instance_type"),
            predicted_runtime_sec=self.plan.get("predicted_runtime_sec"),
        )

    def _prepare_selector(self, adapter: Checkpointable) -> None:
        estimate = getattr(adapter, "estimate_tier_bytes", None)
        if estimate is not None:
            self.selector.set_hints(estimate())
        if self.config.checkpoint.strategy in ("reactive", "hybrid"):
            self._probe_throughput()

    def _probe_throughput(self) -> None:
        """Measure upload speed once, so the first emergency decision has real data.

        A small upload is mostly request overhead, which would make the link look slow and large
        tiers look impossible. So time an almost empty upload too and subtract it. The first
        upload only warms up the connection. The correction is capped so that a noisy overhead
        reading cannot make the link look far faster than it is.
        """
        key = f"{self.job_root}.probe-{os.getpid()}"
        try:
            self.store.upload(key, b"\0")
            started = time.monotonic()
            self.store.upload(key, b"\0")
            overhead = time.monotonic() - started
            started = time.monotonic()
            self.store.upload(key, b"\0" * PROBE_BYTES)
            elapsed = time.monotonic() - started
            self.selector.observe_upload(PROBE_BYTES, max(elapsed - overhead, elapsed * 0.25))
            self.store.delete(key)
        except Exception as exc:  # noqa: BLE001
            self.events.log("error", where="probe", message=str(exc))

    # Safe points -------------------------------------------------------------------------

    def step(self, epoch: int, step: int, **metrics: Any) -> None:
        """Mark a safe point. May checkpoint, and may raise SpotInterrupted."""
        self.position = Position(epoch, step)
        self._steps_seen += 1
        if self._steps_seen % self.config.logging.progress_every_steps == 0:
            self.events.log("job_progress", epoch=epoch, step=step, **metrics)
        self._safe_point()

    def end_epoch(self, epoch: int, **metrics: Any) -> None:
        """Optional extra safe point at the start of the next epoch."""
        self.position = Position(epoch, 0)
        self.events.log("job_progress", epoch=epoch, step=0, epoch_end=True, **metrics)
        self._safe_point()

    def _safe_point(self) -> None:
        if self.lease is not None and self.lease.lost:
            raise SpotInterrupted(saved=False, reason="lease_lost")
        strategy = self.config.checkpoint.strategy
        if strategy in ("reactive", "hybrid") and self.watcher is not None:
            if self.watcher.notice_event.is_set():
                self._emergency()
            if self.watcher.rebalance_event.is_set() and not self._rebalance_handled:
                self._on_rebalance()
            if self._handover_pending:
                self._maybe_handover()
        if strategy in ("periodic", "hybrid"):
            now = time.monotonic()
            if now - self._last_checkpoint >= self.tuner.interval and not self.uploader.busy:
                self._checkpoint_background("periodic")

    def _capture(self, tier: Tier) -> tuple[dict, float]:
        assert self.adapter is not None
        started = time.monotonic()
        state = self.adapter.capture(tier)
        return state, time.monotonic() - started

    def _checkpoint_background(self, reason: str) -> None:
        """Take a full checkpoint: async by default, on this thread when ``async: false``."""
        state, blocking = self._capture(Tier.FULL)
        self._last_checkpoint = time.monotonic()
        if self.config.checkpoint.async_:
            self.uploader.submit(state, Tier.FULL, self.position, reason, blocking)
        else:
            self.uploader.save_sync(state, Tier.FULL, self.position, reason, blocking)
            self._last_checkpoint = time.monotonic()

    def _on_saved(self, result: SaveResult) -> None:
        """Called after every successful checkpoint, possibly on the upload thread."""
        if self._saved_position is None or (
            (result.position.epoch, result.position.step)
            > (self._saved_position.epoch, self._saved_position.step)
        ):
            self._saved_position = result.position
        self.tuner.observe_cost(result.blocking_s)
        before = self.tuner.interval
        after = self.tuner.recompute(time.monotonic() - self._started)
        if abs(after - before) > 0.01 * max(before, 1e-9):
            self.events.log(
                "interval_updated",
                interval_sec=round(after, 3),
                cost_sec=round(self.tuner.cost or 0.0, 6),
                mtbi_sec=round(self.tuner.mtbi(time.monotonic() - self._started), 3),
            )

    def _on_rebalance(self) -> None:
        """Early warning: save now and offer to hand over, but keep working."""
        self._rebalance_handled = True
        self._handover_pending = True
        gap = time.monotonic() - self._last_checkpoint
        if self._saved_position == self.position or self.uploader.busy:
            return
        if self._saved_position is None or gap >= self.config.checkpoint.rebalance_min_gap_sec:
            self._checkpoint_background("rebalance")

    def _maybe_handover(self) -> None:
        """If a replacement is waiting, save and give it the job."""
        if self.lease is None:
            return
        now = time.monotonic()
        if now < self._next_handover_check:
            return
        self._next_handover_check = now + self.config.lease.poll_sec
        if not self.lease.other_waiting():
            return
        self.uploader.wait()
        saved = self._saved_position == self.position
        if not saved:
            state, blocking = self._capture(Tier.FULL)
            result = self.uploader.save_sync(
                state, Tier.FULL, self.position, "handover", blocking
            )
            saved = result.ok
        self.events.log(
            "handover",
            flush=True,
            epoch=self.position.epoch,
            step=self.position.step,
            saved=saved,
        )
        raise SpotInterrupted(saved=saved, reason="handover")

    # Emergency ---------------------------------------------------------------------------

    def _time_left(self) -> float:
        cfg = self.config.deadline
        if cfg.override_seconds is not None:
            if self._budget_started is None:
                self._budget_started = time.monotonic()
            return cfg.override_seconds - (time.monotonic() - self._budget_started)
        assert self.watcher is not None and self.watcher.notice is not None
        return self.selector.time_left(self.watcher.notice.time, datetime.now(UTC))

    def _ensure_sizes(self) -> None:
        """Measure any tier whose size is unknown by serializing it in memory."""
        for tier in self.selector.missing_sizes():
            state, _ = self._capture(tier)
            started = time.monotonic()
            payload = dumps(state, compress=self.config.checkpoint.compress)
            self.selector.observe_serialize(tier, len(payload), time.monotonic() - started)

    def _emergency(self) -> NoReturn:
        """Interruption notice: save what fits before the deadline, then stop."""
        budget_at_notice = self._time_left()
        started = time.monotonic()

        # 1. An upload already running might finish in time; if not, it only competes for
        #    bandwidth with the emergency save.
        if self.uploader.busy:
            if self.uploader.estimate_remaining() < self._time_left():
                self.uploader.wait(max(self._time_left(), 0.0))
            else:
                self.uploader.cancel()
                self.uploader.wait(2.0)
        if self._saved_position == self.position:
            self.events.log(
                "emergency_saved",
                flush=True,
                tier="previous",
                reused=True,
                epoch=self.position.epoch,
                step=self.position.step,
                time_left=round(budget_at_notice, 3),
                actual_s=round(time.monotonic() - started, 3),
                deadline_met=True,
            )
            raise SpotInterrupted(saved=True)

        # 2. Choose a tier.
        self._ensure_sizes()
        time_left = self._time_left()
        decision = self.selector.choose(time_left)
        self._log_decision(decision)
        if decision.chosen is None:
            self.events.log(
                "emergency_skipped",
                flush=True,
                reason="no_tier_fits",
                time_left=round(time_left, 3),
                deadline_met=True,
                epoch=self.position.epoch,
                step=self.position.step,
            )
            raise SpotInterrupted(saved=False)

        # 3. Save it synchronously.
        tier = decision.chosen
        state, blocking = self._capture(tier)
        abort_check = None
        if self.config.deadline.hard_kill:
            hard_stop = time.monotonic() + max(time_left, 0.0)

            def abort_check() -> None:
                if time.monotonic() > hard_stop:
                    raise UploadAborted("deadline passed")

        result = self.uploader.save_sync(
            state, tier, self.position, "emergency", blocking, abort_check=abort_check
        )
        actual = result.serialize_s + result.upload_s + blocking
        if result.ok:
            self.events.log(
                "emergency_saved",
                flush=True,
                tier=tier.value,
                reused=False,
                size_bytes=result.size_bytes,
                predicted_s=round(decision.predictions[tier.value], 3),
                actual_s=round(actual, 3),
                time_left=round(time_left, 3),
                deadline_met=actual <= time_left,
                epoch=self.position.epoch,
                step=self.position.step,
            )
            raise SpotInterrupted(saved=True)
        self.events.log(
            "emergency_skipped",
            flush=True,
            reason=result.error or "upload_failed",
            tier=tier.value,
            time_left=round(time_left, 3),
            predicted_s=round(decision.predictions[tier.value], 3),
            deadline_met=False,
            epoch=self.position.epoch,
            step=self.position.step,
        )
        raise SpotInterrupted(saved=False)

    def _log_decision(self, decision: Decision) -> None:
        self.events.log(
            "emergency_decision",
            time_left=round(decision.time_left, 3),
            predictions={k: round(v, 3) for k, v in decision.predictions.items()},
            chosen=decision.chosen.value if decision.chosen else "skip",
            mode=self.config.deadline.mode,
            throughput_mbps=round((self.selector.throughput or 0) * 8 / 1e6, 3),
        )

    # Exits -------------------------------------------------------------------------------

    def complete(self, result: dict[str, Any] | None = None) -> None:
        """Mark the job finished and release everything."""
        self.uploader.wait(timeout=60.0)
        self.manager.write_status(
            "completed",
            result=result or {},
            completed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            epoch=self.position.epoch,
            step=self.position.step,
        )
        self.events.log(
            "job_complete",
            flush=True,
            result=result or {},
            epoch=self.position.epoch,
            step=self.position.step,
            runtime_sec=round(time.monotonic() - self._started, 3),
            predicted_runtime_sec=self.plan.get("predicted_runtime_sec"),
        )
        self._scale_to_zero()
        self._shutdown()

    def exit_interrupted(self, exc: SpotInterrupted) -> NoReturn:
        """Release everything and exit with code 75 (saved) or 76 (not saved)."""
        code = EXIT_SAVED if exc.saved else EXIT_NOT_SAVED
        self.events.log(
            "interrupted_exit",
            flush=True,
            saved=exc.saved,
            reason=exc.reason,
            exit_code=code,
            epoch=self.position.epoch,
            step=self.position.step,
        )
        self.uploader.cancel()
        self._shutdown(release_lease=exc.reason != "lease_lost")
        sys.exit(code)

    def fail(self, exc: BaseException) -> int:
        """Record an unexpected error and release everything. Returns the exit code."""
        self.events.log(
            "error", flush=True, where="job", message=f"{type(exc).__name__}: {exc}"
        )
        self._shutdown()
        return EXIT_ERROR

    def _scale_to_zero(self) -> None:
        cfg = self.config.aws
        if not (cfg.scale_to_zero_on_complete and cfg.asg_name):
            return
        try:
            import boto3

            boto3.client("autoscaling", region_name=self.config.store.region).set_desired_capacity(
                AutoScalingGroupName=cfg.asg_name, DesiredCapacity=0, HonorCooldown=False
            )
            self.events.log("job_progress", scaled_to_zero=True, asg_name=cfg.asg_name)
        except Exception as exc:  # noqa: BLE001
            self.events.log("error", where="scale_to_zero", message=str(exc))

    def _shutdown(self, release_lease: bool = True) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        if self.watcher is not None:
            self.watcher.stop()
        if self.lease is not None and release_lease:
            self.lease.release()
        self.events.stop()
