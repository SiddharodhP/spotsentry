"""Turn what is in the store into the numbers the dashboard shows. Reads only from the store."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from spotsentry.errors import ObjectNotFound
from spotsentry.events import read_events
from spotsentry.store.base import Store

# Used for the cost view when no plan exists: c5.large in ap-south-1.
DEFAULT_ON_DEMAND = 0.085
DEFAULT_SPOT = 0.034

TIMELINE_TYPES = {
    "job_start": "Worker started",
    "lease_acquired": "Lease acquired",
    "waiting_for_lease": "Waiting for lease",
    "restore_ok": "Restored",
    "restore_fallback": "Restore fallback",
    "restore_failed": "Restore failed",
    "checkpoint_done": "Checkpoint",
    "checkpoint_failed": "Checkpoint failed",
    "rebalance_signal": "Rebalance signal",
    "interruption_notice": "Interruption notice",
    "emergency_decision": "Emergency decision",
    "emergency_saved": "Emergency saved",
    "emergency_skipped": "Emergency skipped",
    "handover": "Handover",
    "interrupted_exit": "Worker exited",
    "job_complete": "Job complete",
    "error": "Error",
}


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _read_json(store: Store, key: str) -> dict[str, Any] | None:
    try:
        return json.loads(store.get_bytes(key).decode("utf-8"))
    except (ObjectNotFound, ValueError):
        return None


def list_jobs(store: Store, prefix: str = "jobs/") -> list[str]:
    """Return the ids of every job in the store."""
    jobs = set()
    for key in store.list_keys(prefix):
        rest = key[len(prefix) :]
        if "/" in rest:
            jobs.add(rest.split("/", 1)[0])
    return sorted(jobs)


@dataclass
class JobView:
    """A snapshot of one job."""

    job_id: str
    state: str = "not started"
    status: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    lease: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    epoch: int = 0
    step: int = 0
    progress: float | None = None
    instance_id: str | None = None
    instance_type: str | None = None
    az: str | None = None
    interval_sec: float | None = None
    last_loss: float | None = None
    instance_seconds: float = 0.0
    workers: int = 0
    interruptions: int = 0
    restarts: int = 0

    def timeline(self) -> list[dict[str, Any]]:
        """Events worth showing on the timeline, each with a short description."""
        rows = []
        for event in self.events:
            kind = event.get("type")
            if kind in TIMELINE_TYPES:
                rows.append(
                    {
                        "time": event["ts"],
                        "event": TIMELINE_TYPES[kind],
                        "instance": event.get("instance_id"),
                        "detail": describe(event),
                    }
                )
        return rows

    def loss_series(self) -> list[dict[str, Any]]:
        """Progress events that report a loss."""
        return [
            {"time": e["ts"], "loss": e["loss"]}
            for e in self.events
            if e.get("type") == "job_progress" and "loss" in e
        ]

    def prices(self) -> tuple[float, float, str]:
        """(spot, on-demand, where they came from) for the instance type actually running.

        In order: the plan's numbers for that type, the plan's own pick, the shipped price table
        with the Spot Advisor's typical saving, and last a fixed c5.large estimate.
        """
        plan = self.plan or {}
        running = self.instance_type
        for candidate in plan.get("candidates") or []:
            if candidate.get("instance_type") == running and candidate.get("spot_price"):
                if not self.az or candidate.get("az") in (None, self.az):
                    return (float(candidate["spot_price"]), float(candidate["on_demand_price"]),
                            "from the plan")
        if plan.get("spot_price") and running in (None, plan.get("instance_type")):
            return float(plan["spot_price"]), float(plan["on_demand_price"]), "from the plan"
        if running and self.az:
            from spotsentry.planner.pricing import on_demand_price, spot_price_estimate
            from spotsentry.planner.risk import AdvisorData

            region = self.az.rstrip("abcdefghijklmnopqrstuvwxyz")
            on_demand = on_demand_price(region, running)
            entry = AdvisorData.load(offline=True).entry(region, running)
            if on_demand and entry:
                spot = spot_price_estimate(on_demand, entry.savings_percent)
                return (spot, on_demand,
                        f"{running} in {region}: on-demand list price, spot estimated from the "
                        f"Spot Advisor's typical {entry.savings_percent}% saving")
        return DEFAULT_SPOT, DEFAULT_ON_DEMAND, "default c5.large estimate (no price data)"

    def cost(self) -> dict[str, float | bool | str]:
        """Spot cost so far against what the same run time costs on demand."""
        spot, on_demand, source = self.prices()
        hours = self.instance_seconds / 3600.0
        spot_cost = hours * spot
        od_cost = hours * on_demand
        return {
            "spot_price": spot,
            "on_demand_price": on_demand,
            "instance_hours": hours,
            "spot_cost": spot_cost,
            "on_demand_cost": od_cost,
            "saved_percent": (1 - spot / on_demand) * 100 if on_demand else 0.0,
            "from_plan": source == "from the plan",
            "source": source,
        }


def describe(event: dict[str, Any]) -> str:
    """One line for the timeline table."""
    kind = event.get("type")
    if kind == "checkpoint_done":
        size = (event.get("size_bytes") or 0) / 1e6
        return (
            f"{event.get('tier')} ({event.get('reason')}), {size:.1f} MB, "
            f"epoch {event.get('epoch')} step {event.get('step')}, "
            f"upload {event.get('upload_s')} s"
        )
    if kind == "emergency_decision":
        predictions = ", ".join(f"{k} {v} s" for k, v in (event.get("predictions") or {}).items())
        return (
            f"time left {event.get('time_left')} s; predicted {predictions}; "
            f"chose {event.get('chosen')}"
        )
    if kind == "emergency_saved":
        return (
            f"{event.get('tier')} saved in {event.get('actual_s')} s "
            f"(predicted {event.get('predicted_s')} s, time left {event.get('time_left')} s)"
        )
    if kind == "emergency_skipped":
        return f"{event.get('reason')}; time left {event.get('time_left')} s"
    if kind == "restore_ok":
        return f"{event.get('tier', '')} at epoch {event.get('epoch')} step {event.get('step')}"
    if kind == "interruption_notice":
        return f"{event.get('action')} at {event.get('deadline')}"
    if kind == "interrupted_exit":
        return f"exit code {event.get('exit_code')}, saved={event.get('saved')}"
    if kind == "job_start":
        resumed = "resumed" if event.get("resumed") else "fresh start"
        return f"{resumed} at epoch {event.get('epoch')} step {event.get('step')}"
    if kind == "error":
        return str(event.get("message"))
    return ""


def load_job(store: Store, job_id: str, prefix: str = "jobs/", now: float | None = None) -> JobView:
    """Read everything about a job from the store."""
    root = f"{prefix}{job_id}/"
    view = JobView(job_id=job_id)
    view.status = _read_json(store, f"{root}status.json")
    view.manifest = _read_json(store, f"{root}latest.json")
    view.lease = _read_json(store, f"{root}lease.json")
    view.plan = _read_json(store, f"{root}plan.json")
    view.events = read_events(store, root)
    now = time.time() if now is None else now

    steps_per_epoch = epochs = None
    spans: dict[str, tuple[datetime, datetime]] = {}
    for event in view.events:
        ts = parse_ts(event["ts"])
        first, last = spans.get(event["_file"], (ts, ts))
        spans[event["_file"]] = (min(first, ts), max(last, ts))
        kind = event.get("type")
        if "instance_id" in event and kind != "waiting_for_lease":
            view.instance_id = event["instance_id"]
            view.instance_type = event.get("instance_type")
            view.az = event.get("az")
        if kind == "job_start":
            steps_per_epoch = event.get("steps_per_epoch") or steps_per_epoch
            epochs = event.get("epochs") or epochs
            view.interval_sec = event.get("interval_sec")
            view.epoch, view.step = event.get("epoch", 0), event.get("step", 0)
        elif kind == "job_progress" and "epoch" in event:
            view.epoch, view.step = event["epoch"], event.get("step", 0)
            if "loss" in event:
                view.last_loss = event["loss"]
        elif kind == "interval_updated":
            view.interval_sec = event.get("interval_sec")
        elif kind == "interruption_notice":
            view.interruptions += 1
        elif kind == "job_complete" and "epoch" in event:
            view.epoch, view.step = event["epoch"], event.get("step", 0)
    view.instance_seconds = sum((b - a).total_seconds() for a, b in spans.values())
    view.workers = len(spans)
    view.restarts = max(view.workers - 1, 0)
    if steps_per_epoch and epochs:
        done = view.epoch * steps_per_epoch + view.step
        view.progress = min(done / (steps_per_epoch * epochs), 1.0)

    view.state = _derive_state(view, now)
    if view.state == "completed":
        view.progress = 1.0
    return view


def _derive_state(view: JobView, now: float) -> str:
    if view.status and view.status.get("state") == "completed":
        return "completed"
    if not view.events:
        return "not started"
    last = view.events[-1].get("type")
    if last == "waiting_for_lease":
        return "waiting for lease"
    lease_alive = view.lease is not None and view.lease.get("expires_at", 0) > now
    if lease_alive:
        return "running"
    if last == "error":
        return "error"
    return "interrupted"
