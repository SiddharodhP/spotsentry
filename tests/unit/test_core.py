"""Runtime behaviour, driven in-process so each rule can be checked directly."""

from __future__ import annotations

import json
import time

import pytest

from spotsentry import SpotInterrupted, SpotSentry
from spotsentry.adapters import Position
from spotsentry.config import parse_config
from spotsentry.events import read_events
from spotsentry.workload import run_loop
from workloads.synthetic import SyntheticJob, expected_chain

ROOT = "jobs/test-job/"


def new_job(total=50, state_mb=0.1, step_seconds=0.0):
    return SyntheticJob(total_steps=total, step_seconds=step_seconds, state_mb=state_mb)


def types(sentry: SpotSentry) -> list[str]:
    sentry.events.flush()
    return [e["type"] for e in read_events(sentry.store, sentry.job_root)]


def of_type(sentry: SpotSentry, name: str) -> list[dict]:
    sentry.events.flush()
    return [e for e in read_events(sentry.store, sentry.job_root) if e["type"] == name]


def expect_exit(sentry: SpotSentry, exc: SpotInterrupted) -> int:
    with pytest.raises(SystemExit) as info:
        sentry.exit_interrupted(exc)
    return info.value.code


def wait_for(event, timeout=2.0):
    assert event.wait(timeout), "signal never seen"


def test_full_run_completes(make_config, store):
    sentry = SpotSentry(make_config(), store=store)
    job = new_job()
    start = sentry.start(job.adapter())
    assert start == Position()
    run_loop(job, sentry, start)
    sentry.complete(job.result())
    status = json.loads(store.get_bytes(f"{ROOT}status.json"))
    assert status["state"] == "completed"
    assert status["result"]["chain"] == expected_chain(50)
    assert not store.exists(f"{ROOT}lease.json")


def test_completed_job_exits_zero_without_running(make_config, store):
    sentry = SpotSentry(make_config(), store=store)
    job = new_job()
    run_loop(job, sentry, sentry.start(job.adapter()))
    sentry.complete(job.result())

    second = SpotSentry(make_config(), store=store)
    with pytest.raises(SystemExit) as info:
        second.start(new_job().adapter())
    assert info.value.code == 0


def test_notice_saves_full_and_exit_75_then_resume_is_exact(make_config, store, imds):
    imds.schedule_notice(0, lead_sec=60)
    sentry = SpotSentry(make_config(), store=store)
    job = new_job()
    start = sentry.start(job.adapter())
    wait_for(sentry.watcher.notice_event)
    for i in range(10):
        job.train_step(None)
        if i < 9:
            sentry.position = Position(0, i + 1)
    with pytest.raises(SpotInterrupted) as info:
        sentry.step(epoch=0, step=10)
    assert info.value.saved
    assert start == Position()
    assert expect_exit(sentry, info.value) == 75

    names = types(sentry)
    assert "interruption_notice" in names
    assert "emergency_decision" in names
    saved = of_type(sentry, "emergency_saved")[0]
    assert saved["tier"] == "full"
    assert saved["deadline_met"] is True
    assert not store.exists(f"{ROOT}lease.json")

    imds.clear()
    second = SpotSentry(make_config(), store=store)
    resumed = new_job()
    position = second.start(resumed.adapter())
    assert position == Position(0, 10)
    run_loop(resumed, second, position)
    second.complete(resumed.result())
    assert resumed.chain.hex() == expected_chain(50)


def tight(make_config, seconds: float, mbps: float = 40):
    return make_config(
        deadline={"override_seconds": seconds, "safety_margin_sec": 0},
        store={"bandwidth_limit_mbps": mbps},
        checkpoint={"strategy": "reactive"},
    )


def emergency_with(make_config, tmp_path, imds, seconds: float, state_mb: float = 10):
    """Full 30 MB, light 10 MB, tiny 5 MB, at 40 Mbps = 5 MB/s."""
    imds.schedule_notice(0, lead_sec=300)
    config = tight(make_config, seconds)
    sentry = SpotSentry(config)
    job = new_job(state_mb=state_mb)
    sentry.start(job.adapter())
    wait_for(sentry.watcher.notice_event)
    job.train_step(None)
    with pytest.raises(SpotInterrupted) as info:
        sentry.step(epoch=0, step=1)
    return sentry, info.value


def test_tight_deadline_picks_light(make_config, tmp_path, imds):
    sentry, exc = emergency_with(make_config, tmp_path, imds, seconds=3.5)
    assert exc.saved
    assert of_type(sentry, "emergency_decision")[0]["chosen"] == "light"
    expect_exit(sentry, exc)


def test_tighter_deadline_picks_tiny(make_config, tmp_path, imds):
    sentry, exc = emergency_with(make_config, tmp_path, imds, seconds=1.8)
    assert exc.saved
    assert of_type(sentry, "emergency_decision")[0]["chosen"] == "tiny"
    saved = of_type(sentry, "emergency_saved")[0]
    assert saved["actual_s"] < 1.8
    expect_exit(sentry, exc)


def test_impossible_deadline_skips_and_exits_76(make_config, tmp_path, imds, store):
    sentry, exc = emergency_with(make_config, tmp_path, imds, seconds=0.3)
    assert not exc.saved
    assert of_type(sentry, "emergency_decision")[0]["chosen"] == "skip"
    assert of_type(sentry, "emergency_skipped")
    assert not store.exists(f"{ROOT}latest.json")
    assert expect_exit(sentry, exc) == 76


def test_skip_keeps_previous_checkpoint(make_config, store, imds):
    config = make_config(deadline={"override_seconds": 0.0, "safety_margin_sec": 0})
    sentry = SpotSentry(config, store=store)
    job = new_job()
    sentry.start(job.adapter())
    job.train_step(None)
    sentry.position = Position(0, 1)
    sentry._checkpoint_background("periodic")
    sentry.uploader.wait(5)
    imds.schedule_notice(0, lead_sec=60)
    wait_for(sentry.watcher.notice_event)
    job.train_step(None)
    with pytest.raises(SpotInterrupted) as info:
        sentry.step(epoch=0, step=2)
    assert not info.value.saved
    expect_exit(sentry, info.value)
    assert json.loads(store.get_bytes(f"{ROOT}latest.json"))["step"] == 1


def test_naive_mode_with_hard_kill_misses_deadline(make_config, imds):
    imds.schedule_notice(0, lead_sec=300)
    config = make_config(
        deadline={"mode": "naive", "override_seconds": 1.0, "hard_kill": True},
        store={"bandwidth_limit_mbps": 8},
        checkpoint={"strategy": "reactive"},
    )
    sentry = SpotSentry(config)
    job = new_job(state_mb=2)  # 6 MB full at 1 MB/s needs 6 s
    sentry.start(job.adapter())
    wait_for(sentry.watcher.notice_event)
    job.train_step(None)
    started = time.monotonic()
    with pytest.raises(SpotInterrupted) as info:
        sentry.step(epoch=0, step=1)
    assert not info.value.saved
    assert time.monotonic() - started < 3
    skipped = of_type(sentry, "emergency_skipped")[0]
    assert skipped["deadline_met"] is False
    assert not sentry.store.exists(f"{ROOT}latest.json")
    expect_exit(sentry, info.value)


def test_periodic_checkpoints_happen(make_config, store):
    config = make_config(checkpoint={"strategy": "periodic", "interval": 0.05})
    sentry = SpotSentry(config, store=store)
    job = new_job(total=40, step_seconds=0.01)
    run_loop(job, sentry, sentry.start(job.adapter()))
    sentry.complete(job.result())
    assert len(of_type(sentry, "checkpoint_done")) >= 2


def test_none_strategy_never_checkpoints(make_config, store, imds):
    imds.schedule_notice(0, lead_sec=60)
    config = make_config(checkpoint={"strategy": "none", "interval": 0.01})
    sentry = SpotSentry(config, store=store)
    job = new_job(total=20, step_seconds=0.005)
    run_loop(job, sentry, sentry.start(job.adapter()))
    sentry.complete(job.result())
    assert not of_type(sentry, "checkpoint_done")


def test_periodic_ignores_notice(make_config, store, imds):
    imds.schedule_notice(0, lead_sec=60)
    config = make_config(checkpoint={"strategy": "periodic", "interval": 1000})
    sentry = SpotSentry(config, store=store)
    job = new_job(total=20, step_seconds=0.005)
    start = sentry.start(job.adapter())
    wait_for(sentry.watcher.notice_event)
    run_loop(job, sentry, start)
    sentry.complete(job.result())
    assert job.steps_done == 20


def test_rebalance_takes_early_checkpoint_and_keeps_running(make_config, store, imds):
    imds.schedule_rebalance(0)
    sentry = SpotSentry(make_config(), store=store)
    job = new_job(total=10)
    start = sentry.start(job.adapter())
    wait_for(sentry.watcher.rebalance_event)
    run_loop(job, sentry, start)
    sentry.complete(job.result())
    assert job.steps_done == 10
    assert any(e["reason"] == "rebalance" for e in of_type(sentry, "checkpoint_done"))
    assert of_type(sentry, "rebalance_signal")


def test_handover_when_replacement_is_waiting(make_config, store, imds):
    imds.schedule_rebalance(0)
    sentry = SpotSentry(make_config(), store=store)
    job = new_job(total=1000)
    sentry.start(job.adapter())
    wait_for(sentry.watcher.rebalance_event)
    job.train_step(None)
    sentry.step(epoch=0, step=1)
    sentry.uploader.wait(5)
    store.put_bytes(
        f"{ROOT}lease_request.json",
        json.dumps({"owner": "someone-else", "requested_at": time.time()}).encode(),
    )
    sentry._next_handover_check = 0
    job.train_step(None)
    with pytest.raises(SpotInterrupted) as info:
        sentry.step(epoch=0, step=2)
    assert info.value.saved
    assert info.value.reason == "handover"
    assert of_type(sentry, "handover")
    assert expect_exit(sentry, info.value) == 75
    assert json.loads(store.get_bytes(f"{ROOT}latest.json"))["step"] == 2


def test_lost_lease_stops_job(make_config, store):
    sentry = SpotSentry(make_config(), store=store)
    job = new_job()
    sentry.start(job.adapter())
    sentry.lease.lost = True
    job.train_step(None)
    with pytest.raises(SpotInterrupted) as info:
        sentry.step(epoch=0, step=1)
    assert info.value.reason == "lease_lost"
    assert not info.value.saved
    assert expect_exit(sentry, info.value) == 76


def test_async_blocking_is_less_than_sync(make_config, store):
    measured = {}
    for mode in (True, False):
        config = make_config(
            checkpoint={"strategy": "periodic", "interval": 1000, "async": mode},
            store={"bandwidth_limit_mbps": 80},
        )
        sentry = SpotSentry(config)
        job = new_job(state_mb=2)
        sentry.start(job.adapter())
        job.train_step(None)
        sentry.position = Position(0, 1)
        started = time.monotonic()
        sentry._checkpoint_background("periodic")
        measured[mode] = time.monotonic() - started
        sentry.uploader.wait(10)
        sentry.complete({})
        store.delete_prefix(ROOT)
    assert measured[True] < measured[False] / 5


def test_upload_retries_then_gives_up(make_config, store):
    sentry = SpotSentry(make_config(), store=store)
    sentry.uploader.backoff_sec = 0.001
    job = new_job()
    sentry.start(job.adapter())
    original = store.upload
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        raise OSError("network down")

    store.upload = flaky
    job.train_step(None)
    sentry.position = Position(0, 1)
    state, blocking = sentry._capture(sentry.selector.tiers[0])
    result = sentry.uploader.save_sync(
        state, sentry.selector.tiers[0], sentry.position, "test", blocking
    )
    store.upload = original
    assert not result.ok
    assert calls["n"] == 3
    assert of_type(sentry, "checkpoint_failed")
    assert not store.exists(f"{ROOT}latest.json")
    sentry.complete({})


def test_env_overrides_config():
    config = parse_config(
        {"job_id": "x", "store": {"bucket": "from-yaml"}},
        environ={
            "SPOTSENTRY_STORE__BUCKET": "from-env",
            "SPOTSENTRY_DEADLINE__OVERRIDE_SECONDS": "8",
            "SPOTSENTRY_INSTANCE_ID": "ignored-not-a-config-field",
        },
    )
    assert config.store.bucket == "from-env"
    assert config.deadline.override_seconds == 8


def test_bad_config_is_reported():
    from spotsentry.errors import ConfigError

    with pytest.raises(ConfigError):
        parse_config({"job_id": "x", "checkpoint": {"strategy": "sometimes"}}, environ={})
