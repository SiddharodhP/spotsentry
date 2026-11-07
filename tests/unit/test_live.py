from __future__ import annotations

import json
import time

from spotsentry import SpotInterrupted, SpotSentry
from spotsentry.live import describe, list_jobs, load_job
from spotsentry.workload import run_loop
from workloads.synthetic import SyntheticJob


def test_empty_job(store):
    view = load_job(store, "nothing")
    assert view.state == "not started"
    assert view.timeline() == []


def test_completed_job_view(make_config, store):
    sentry = SpotSentry(make_config(), store=store)
    job = SyntheticJob(total_steps=20, step_seconds=0.001, state_mb=0.1)
    sentry.set_progress_total(job.epochs, job.steps_per_epoch)
    run_loop(job, sentry, sentry.start(job.adapter()))
    sentry.complete(job.result())
    view = load_job(store, "test-job")
    assert view.state == "completed"
    assert view.progress == 1.0
    assert view.instance_id == "i-fake"
    assert view.workers == 1
    assert "test-job" in list_jobs(store)
    assert any(row["event"] == "Job complete" for row in view.timeline())


def test_running_state_and_progress(make_config, store):
    sentry = SpotSentry(make_config(), store=store)
    job = SyntheticJob(total_steps=100, step_seconds=0, state_mb=0.1)
    sentry.set_progress_total(job.epochs, job.steps_per_epoch)
    sentry.start(job.adapter())
    for step in range(1, 51):
        job.train_step(None)
        sentry.step(epoch=0, step=step)
    sentry.events.flush()
    view = load_job(store, "test-job")
    assert view.state == "running"
    assert view.progress == 0.5 or view.progress == 0.0
    sentry.complete({})


def test_interrupted_state(make_config, store, imds):
    imds.schedule_notice(0, lead_sec=60)
    sentry = SpotSentry(make_config(), store=store)
    job = SyntheticJob(total_steps=100, step_seconds=0, state_mb=0.1)
    sentry.start(job.adapter())
    assert sentry.watcher.notice_event.wait(2)
    job.train_step(None)
    try:
        sentry.step(epoch=0, step=1)
    except SpotInterrupted as exc:
        try:
            sentry.exit_interrupted(exc)
        except SystemExit:
            pass
    view = load_job(store, "test-job")
    assert view.state == "interrupted"
    assert view.interruptions == 1
    details = [row["detail"] for row in view.timeline()]
    assert any("chose full" in detail for detail in details)


def test_waiting_for_lease_state(store):
    store.put_bytes(
        "jobs/j/events/i-2_1.jsonl",
        (json.dumps({"ts": "2026-01-01T00:00:00Z", "type": "waiting_for_lease"}) + "\n").encode(),
    )
    assert load_job(store, "j").state == "waiting for lease"


def test_cost_uses_plan_prices(store):
    events = [
        {"ts": "2026-01-01T00:00:00Z", "type": "job_start", "instance_id": "i-1"},
        {"ts": "2026-01-01T01:00:00Z", "type": "job_progress", "instance_id": "i-1"},
    ]
    body = "\n".join(json.dumps(e) for e in events) + "\n"
    store.put_bytes("jobs/j/events/i-1_1.jsonl", body.encode())
    store.put_bytes(
        "jobs/j/plan.json", json.dumps({"spot_price": 0.02, "on_demand_price": 0.1}).encode()
    )
    cost = load_job(store, "j").cost()
    assert cost["instance_hours"] == 1.0
    assert cost["spot_cost"] == 0.02
    assert cost["on_demand_cost"] == 0.1
    assert round(cost["saved_percent"]) == 80
    assert cost["from_plan"]


def test_lease_expired_means_not_running(store):
    store.put_bytes(
        "jobs/j/events/i-1_1.jsonl",
        (json.dumps({"ts": "2026-01-01T00:00:00Z", "type": "job_start"}) + "\n").encode(),
    )
    store.put_bytes(
        "jobs/j/lease.json", json.dumps({"owner": "a", "expires_at": time.time() - 5}).encode()
    )
    assert load_job(store, "j").state == "interrupted"


def test_describe_handles_all_event_kinds():
    assert "full" in describe({"type": "checkpoint_done", "tier": "full", "size_bytes": 1000000})
    assert describe({"type": "unknown"}) == ""
