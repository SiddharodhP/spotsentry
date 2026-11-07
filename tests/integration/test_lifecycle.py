"""Whole-lifecycle tests, driven by the same orchestrator as the demo.

Each scenario runs on two backends:

* ``process`` (marker ``lifecycle``): local processes, a folder, a fake metadata service.
* ``docker`` (marker ``integration``): MinIO, the AWS metadata mock and worker containers.

``pytest -m "not integration"`` runs the first group. ``pytest -m integration`` runs the second.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local.orchestrator import (
    DockerBackend,
    ProcessBackend,
    check_expectations,
    docker_available,
    load_scenario,
    run_scenario,
)
from spotsentry.config import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent
SCENARIOS = ROOT / "local" / "scenarios"


@pytest.fixture(
    params=[
        pytest.param("process", marks=pytest.mark.lifecycle),
        pytest.param("docker", marks=pytest.mark.integration),
    ]
)
def backend(request):
    if request.param == "docker":
        if not docker_available():
            pytest.skip("Docker engine is not running")
        load_dotenv(ROOT / ".env")
        instance = DockerBackend()
    else:
        instance = ProcessBackend()
    yield instance
    instance.cleanup()


def shorten(scenario: dict, total_steps: int = 500) -> dict:
    """Make a scenario quicker without changing what it tests."""
    params = {"total_steps": total_steps, "epochs": 2, **(scenario.get("workload_params") or {})}
    params["total_steps"] = total_steps
    scenario["workload_params"] = params
    return scenario


def test_notice_saves_exits_75_and_replacement_resumes(backend):
    scenario = shorten(load_scenario(SCENARIOS / "demo.yaml"), total_steps=500)
    scenario["interruptions"] = [{"after_sec": 10, "notice_lead_sec": 60}]
    scenario["expect"] = {"exit_codes": [75, 0]}
    summary = run_scenario(scenario, backend, quiet=True)
    assert check_expectations(scenario, summary) == []
    saved = summary.of_type("emergency_saved")
    assert saved and saved[0]["tier"] == "full"
    restored = summary.of_type("restore_ok")
    assert restored
    assert (restored[0]["epoch"], restored[0]["step"]) == (saved[0]["epoch"], saved[0]["step"])
    assert summary.chain_ok is True


def test_two_interruptions_match_uninterrupted_result(backend):
    scenario = shorten(load_scenario(SCENARIOS / "demo.yaml"), total_steps=800)
    scenario["interruptions"] = [
        {"after_sec": 10, "notice_lead_sec": 60},
        {"after_sec": 10, "notice_lead_sec": 60},
    ]
    summary = run_scenario(scenario, backend, quiet=True)
    assert summary.exit_codes == [75, 75, 0]
    assert summary.status["result"]["steps_done"] == 800
    assert summary.chain_ok is True


def test_tight_deadline_picks_a_smaller_tier(backend):
    scenario = load_scenario(SCENARIOS / "tight_deadline.yaml")
    summary = run_scenario(scenario, backend, quiet=True)
    assert check_expectations(scenario, summary) == []
    decision = summary.of_type("emergency_decision")[0]
    assert decision["chosen"] in ("light", "tiny")
    assert decision["predictions"]["full"] > decision["time_left"]
    assert summary.of_type("emergency_saved")[0]["deadline_met"] is True


def test_nothing_fits_skips_and_exits_76(backend):
    scenario = load_scenario(SCENARIOS / "skip_deadline.yaml")
    summary = run_scenario(scenario, backend, quiet=True)
    assert check_expectations(scenario, summary) == []
    assert summary.of_type("emergency_skipped")


def test_rebalance_before_notice_takes_early_checkpoint(backend):
    scenario = shorten(load_scenario(SCENARIOS / "rebalance.yaml"), total_steps=500)
    scenario["interruptions"] = [{"after_sec": 12, "rebalance_lead_sec": 6, "notice_lead_sec": 60}]
    summary = run_scenario(scenario, backend, quiet=True)
    assert check_expectations(scenario, summary) == []
    signal = summary.of_type("rebalance_signal")[0]
    notice = summary.of_type("interruption_notice")[0]
    assert signal["ts"] < notice["ts"]
    assert summary.chain_ok is True


def test_killed_without_notice_still_resumes_from_last_checkpoint(backend):
    scenario = shorten(load_scenario(SCENARIOS / "demo.yaml"), total_steps=500)
    scenario["overrides"] = {
        "checkpoint": {"strategy": "periodic", "min_interval_sec": 2, "interval": 2},
        "lease": {"ttl_sec": 20, "heartbeat_sec": 2},
    }
    scenario["interruptions"] = [{"after_sec": 12, "notice_lead_sec": 0}]
    scenario["expect"] = {"exit_codes": [-9, 0]}
    summary = run_scenario(scenario, backend, quiet=True)
    assert check_expectations(scenario, summary) == []
    assert summary.of_type("waiting_for_lease")
    assert summary.of_type("restore_ok")
    assert summary.chain_ok is True


def test_corrupt_latest_checkpoint_falls_back_to_history(backend):
    """Damage the newest checkpoint between workers: the next worker must use an older one."""
    from spotsentry.store import make_store

    scenario = shorten(load_scenario(SCENARIOS / "demo.yaml"), total_steps=500)
    scenario["overrides"] = {"checkpoint": {"min_interval_sec": 2, "interval": 2}}
    scenario["interruptions"] = [{"after_sec": 10, "notice_lead_sec": 60}]
    scenario["replacement_delay_sec"] = 3
    holder: dict = {}
    corrupt_scenario = {**scenario, "_corrupt_before_worker": 2}

    def on_ready(config):
        holder["config"] = config

    import threading

    def corrupt_when_saved():
        import json
        import time

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            config = holder.get("config")
            if config is not None:
                store = make_store(config.store)
                root = f"{config.store.prefix}{config.job_id}/"
                try:
                    keys = store.list_keys(f"{root}events/")
                    bodies = [store.get_bytes(k).decode() for k in keys]
                except Exception:  # noqa: BLE001
                    bodies = []
                if any('"interrupted_exit"' in body for body in bodies):
                    manifest = json.loads(store.get_bytes(f"{root}latest.json"))
                    store.put_bytes(manifest["key"], b"corrupted")
                    holder["corrupted"] = manifest["key"]
                    return
            time.sleep(0.1)

    thread = threading.Thread(target=corrupt_when_saved, daemon=True)
    thread.start()
    summary = run_scenario(corrupt_scenario, backend, quiet=True, on_ready=on_ready)
    thread.join(timeout=5)
    assert holder.get("corrupted")
    assert summary.of_type("restore_fallback")
    assert summary.completed
    assert summary.chain_ok is True
