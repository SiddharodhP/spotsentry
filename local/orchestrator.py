"""Simulates the AWS spot lifecycle on one machine.

For each planned interruption: start a worker, deliver a notice, kill the worker when the
deadline passes (nothing kills it automatically locally), then start a replacement that restores
from the store. Finally let the job complete and check the result.

Two backends:

* ``docker``: MinIO, the AWS metadata mock and worker containers, as in the spec.
* ``process``: workers are local processes, the store is a folder, the metadata service is
  ``FakeIMDS``. Needs no Docker. Used by the automated lifecycle tests.

Run it with ``python local/orchestrator.py --scenario local/scenarios/demo.yaml``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from spotsentry.config import load_dotenv, parse_config  # noqa: E402
from spotsentry.events import read_events  # noqa: E402
from spotsentry.store import make_store  # noqa: E402
from spotsentry.testing.fake_imds import FakeIMDS  # noqa: E402
from workloads.synthetic import expected_chain  # noqa: E402

AEMM_IMAGE = "public.ecr.aws/aws-ec2/amazon-ec2-metadata-mock:v1.13.0"
AEMM_NAME = "spotsentry-aemm"
BUCKET = "spotsentry-checkpoints"
COMPOSE_FILE = REPO_ROOT / "local" / "docker-compose.yml"
ENV_FILE = REPO_ROOT / ".env"
KILLED = -9


def flatten_env(overrides: dict[str, Any], prefix: str = "SPOTSENTRY_") -> dict[str, str]:
    """Turn {deadline: {override_seconds: 5}} into SPOTSENTRY_DEADLINE__OVERRIDE_SECONDS=5."""
    env: dict[str, str] = {}
    for key, value in overrides.items():
        name = f"{prefix}{key.upper()}"
        if isinstance(value, dict):
            env.update(flatten_env(value, name + "__"))
        else:
            env[name] = yaml.safe_dump(value, default_flow_style=True).strip().removesuffix("...")
            env[name] = env[name].strip()
    return env


@dataclass
class RunRecord:
    """One worker's lifetime."""

    name: str
    exit_code: int | None = None
    killed: bool = False
    seconds: float = 0.0


@dataclass
class Summary:
    """What a scenario did."""

    scenario: str
    backend: str
    runs: list[RunRecord] = field(default_factory=list)
    status: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    chain_ok: bool | None = None
    wall_seconds: float = 0.0

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == event_type]

    @property
    def exit_codes(self) -> list[int | None]:
        return [r.exit_code for r in self.runs]

    @property
    def completed(self) -> bool:
        return bool(self.status and self.status.get("state") == "completed")


class ProcessBackend:
    """Workers as local processes, a folder as the store, FakeIMDS as the metadata service."""

    name = "process"

    def __init__(self, work_dir: Path | None = None) -> None:
        if work_dir is None:
            base = REPO_ROOT / ".spotsentry-store"
            base.mkdir(exist_ok=True)
            work_dir_path = Path(tempfile.mkdtemp(prefix="scenario-", dir=base))
        self.work_dir = work_dir or work_dir_path
        self._owns_dir = work_dir is None
        self.imds = FakeIMDS().start()

    def config_overrides(self) -> dict[str, Any]:
        return {
            "store": {"type": "local", "root": str(self.work_dir / "store")},
            "metadata": {"base_url": self.imds.base_url},
        }

    def prepare(self) -> None:
        pass

    def start_metadata(self, plan: dict[str, Any] | None) -> None:
        self.imds.clear()
        if plan:
            after = float(plan["after_sec"])
            self.imds.schedule_notice(after, float(plan.get("notice_lead_sec", 120)))
            if plan.get("rebalance_lead_sec") is not None:
                self.imds.schedule_rebalance(max(after - float(plan["rebalance_lead_sec"]), 0.0))

    def start_worker(self, name: str, env: dict[str, str], job_path: Path) -> subprocess.Popen:
        full_env = {**os.environ, **env, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
        log = open(self.work_dir / f"{name}.log", "w", encoding="utf-8")  # noqa: SIM115
        return subprocess.Popen(
            [sys.executable, "-m", "spotsentry.cli", "run", "--job", str(job_path)],
            env=full_env,
            cwd=REPO_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    def poll(self, handle: subprocess.Popen) -> int | None:
        return handle.poll()

    def kill(self, handle: subprocess.Popen) -> None:
        handle.kill()
        handle.wait()

    def finish_worker(self, handle: subprocess.Popen, name: str) -> None:
        pass

    def cleanup(self) -> None:
        self.imds.stop()
        if self._owns_dir:
            shutil.rmtree(self.work_dir, ignore_errors=True)


def ensure_bucket(endpoint_url: str, bucket: str, timeout_sec: float = 60.0) -> None:
    """Wait for an S3-compatible server to answer, then create the bucket if it is missing."""
    import boto3
    from botocore.exceptions import ClientError, EndpointConnectionError

    load_dotenv(ENV_FILE)
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=os.environ.get("MINIO_ROOT_USER", "spotsentry"),
        aws_secret_access_key=os.environ.get("MINIO_ROOT_PASSWORD", "change-me-local-only"),
        region_name="us-east-1",
    )
    deadline = time.monotonic() + timeout_sec
    while True:
        try:
            client.head_bucket(Bucket=bucket)
            return
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket"):
                client.create_bucket(Bucket=bucket)
                return
            error: Exception = exc
        except EndpointConnectionError as exc:
            error = exc
        if time.monotonic() > deadline:
            raise RuntimeError(f"no S3 server answered at {endpoint_url}: {error}")
        time.sleep(1)


class DockerBackend:
    """Compose-managed MinIO and workers, plus the AWS metadata mock container.

    Set SPOTSENTRY_DOCKER_STORE_URL to use another S3-compatible server instead of the compose
    one. It must also answer on 127.0.0.1:9000, where the orchestrator itself reads events.
    """

    name = "docker"

    def __init__(self, store_url: str | None = None) -> None:
        self.network = "spotsentry_default"
        self.store_url = store_url or os.environ.get("SPOTSENTRY_DOCKER_STORE_URL")

    def _compose(self, *args: str) -> list[str]:
        command = ["docker", "compose", "-f", str(COMPOSE_FILE)]
        if ENV_FILE.exists():
            command += ["--env-file", str(ENV_FILE)]
        return [*command, *args]

    def config_overrides(self) -> dict[str, Any]:
        return {"store": {"endpoint_url": self.store_url}} if self.store_url else {}

    def host_overrides(self) -> dict[str, Any]:
        """The orchestrator itself runs on the host, where MinIO is on 127.0.0.1."""
        return {"store": {"endpoint_url": "http://127.0.0.1:9000"}}

    def prepare(self) -> None:
        if not self.store_url:
            subprocess.run(self._compose("up", "-d", "minio"), check=True)
        ensure_bucket("http://127.0.0.1:9000", BUCKET)
        subprocess.run(self._compose("--profile", "worker", "build", "worker"), check=True)
        if self.store_url:
            # Compose normally creates the network when it starts MinIO.
            subprocess.run(
                self._compose("--profile", "worker", "run", "--rm", "--no-deps", "worker", "true"),
                check=True,
            )
        self._remove_aemm()

    def _remove_aemm(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", AEMM_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def start_metadata(self, plan: dict[str, Any] | None) -> None:
        """Restart the mock. With no plan its delay is set so far out that no notice appears."""
        self._remove_aemm()
        after = int(plan["after_sec"]) if plan else 86400
        lead = int(plan.get("notice_lead_sec", 120)) if plan else 120
        notice_time = datetime.now(UTC) + timedelta(seconds=after + lead)
        args = [
            "docker", "run", "-d", "--rm", "--name", AEMM_NAME, "--network", self.network,
            "--network-alias", "aemm", AEMM_IMAGE, "spot",
            "-a", "terminate",
            "-t", notice_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "-d", str(after),
            "-I",
        ]
        if plan and plan.get("rebalance_lead_sec") is not None:
            rebalance_delay = max(after - int(plan["rebalance_lead_sec"]), 0)
            rebalance_time = datetime.now(UTC) + timedelta(seconds=rebalance_delay)
            args += [
                "--rebalance-delay-sec", str(rebalance_delay),
                "-r", rebalance_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ]
        else:
            args += ["--rebalance-delay-sec", "86400"]
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL)

    def start_worker(self, name: str, env: dict[str, str], job_path: Path) -> str:
        container = f"spotsentry-{name}"
        subprocess.run(
            ["docker", "rm", "-f", container],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        env_args: list[str] = []
        for key, value in env.items():
            env_args += ["-e", f"{key}={value}"]
        relative = job_path.resolve().relative_to(REPO_ROOT).as_posix()
        subprocess.run(
            self._compose(
                "--profile", "worker", "run", "-d", "--no-deps", "--name", container,
                *env_args, "worker", "spotsentry", "run", "--job", relative,
            ),
            check=True,
            stdout=subprocess.DEVNULL,
        )
        return container

    def poll(self, handle: str) -> int | None:
        output = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", handle],
            capture_output=True, text=True,
        ).stdout.split()
        if not output or output[0] == "true":
            return None
        return int(output[1])

    def kill(self, handle: str) -> None:
        subprocess.run(["docker", "kill", handle], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    def finish_worker(self, handle: str, name: str) -> None:
        subprocess.run(["docker", "rm", "-f", handle], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    def cleanup(self) -> None:
        self._remove_aemm()


def _load_job(path: Path, overrides: dict[str, Any]) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return _merge(data, overrides)


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def run_scenario(
    scenario: dict[str, Any],
    backend,
    quiet: bool = False,
    on_ready=None,
) -> Summary:
    """Run a scenario end to end and return what happened."""
    say = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    job_path = REPO_ROOT / scenario["job"]
    scenario_overrides = scenario.get("overrides") or {}
    workload_params = scenario.get("workload_params") or {}
    if workload_params:
        scenario_overrides = _merge(scenario_overrides, {"workload": {"params": workload_params}})

    worker_overrides = _merge(scenario_overrides, backend.config_overrides())
    env = flatten_env(worker_overrides)

    # The config as the orchestrator sees it, for reading results from the store.
    host_overrides = _merge(worker_overrides, getattr(backend, "host_overrides", dict)())
    config = parse_config(_load_job(job_path, host_overrides), environ={})
    store = make_store(config.store)
    root = f"{config.store.prefix}{config.job_id}/"

    say(f"[{scenario.get('name', 'scenario')}] backend={backend.name} preparing...")
    backend.prepare()
    store.delete_prefix(root)
    if on_ready:
        on_ready(config)

    summary = Summary(scenario=scenario.get("name", "scenario"), backend=backend.name)
    plans = list(scenario.get("interruptions") or [])
    started = time.monotonic()
    for index, plan in enumerate([*plans, None]):
        name = f"worker-{index + 1}"
        record = RunRecord(name=name)
        worker_env = {**env, "SPOTSENTRY_INSTANCE_ID": f"i-local-{index + 1:04d}"}
        run_started = time.monotonic()
        handle = backend.start_worker(name, worker_env, job_path)
        backend.start_metadata(plan)
        if plan:
            say(
                f"  {name}: running; notice in {plan['after_sec']} s, "
                f"terminated {plan.get('notice_lead_sec', 120)} s after that"
            )
        else:
            say(f"  {name}: running to completion")
        kill_at = None
        if plan:
            kill_at = run_started + float(plan["after_sec"]) + float(
                plan.get("notice_lead_sec", 120)
            )
        limit = run_started + float(scenario.get("timeout_sec", 1200))
        while True:
            code = backend.poll(handle)
            if code is not None:
                record.exit_code = code
                break
            now = time.monotonic()
            if (kill_at is not None and now >= kill_at) or now >= limit:
                backend.kill(handle)
                record.exit_code = KILLED
                record.killed = True
                break
            time.sleep(0.2)
        record.seconds = time.monotonic() - run_started
        backend.finish_worker(handle, name)
        summary.runs.append(record)
        say(f"  {name}: exit {record.exit_code} after {record.seconds:.1f} s")
        if plan is None or record.exit_code == 0:
            break
        delay = float(scenario.get("replacement_delay_sec", 3))
        say(f"  replacement instance boots in {delay:.0f} s")
        time.sleep(delay)

    summary.wall_seconds = time.monotonic() - started
    raw_status = None
    try:
        raw_status = json.loads(store.get_bytes(f"{root}status.json").decode("utf-8"))
    except Exception:  # noqa: BLE001
        pass
    summary.status = raw_status
    summary.events = read_events(store, root)
    result = (raw_status or {}).get("result") or {}
    if "chain" in result:
        summary.chain_ok = result["chain"] == expected_chain(int(result["steps_done"]))
    return summary


def print_summary(summary: Summary) -> None:
    """Print a readable report."""
    print(f"\n=== {summary.scenario} ({summary.backend}) ===")
    print(f"Exit codes:        {summary.exit_codes}  (75 saved, 76 not saved, 0 done, -9 killed)")
    print(f"Final state:       {(summary.status or {}).get('state')}")
    print(f"Total time:        {summary.wall_seconds:.1f} s")
    for event in summary.of_type("emergency_decision"):
        print(
            f"Emergency decision: time left {event['time_left']} s, "
            f"predictions {event['predictions']} -> {event['chosen']}"
        )
    for event in summary.of_type("emergency_saved"):
        print(
            f"Emergency saved:   tier={event['tier']} at epoch {event.get('epoch')} "
            f"step {event.get('step')}, took {event.get('actual_s')} s "
            f"(predicted {event.get('predicted_s')} s)"
        )
    for event in summary.of_type("emergency_skipped"):
        print(f"Emergency skipped: {event.get('reason')}, time left {event.get('time_left')} s")
    for event in summary.of_type("restore_ok"):
        print(
            f"Restored:          {event['tier'] if 'tier' in event else '?'} checkpoint at "
            f"epoch {event['epoch']} step {event['step']} on {event['instance_id']}"
        )
    result = (summary.status or {}).get("result") or {}
    if "steps_done" in result:
        print(f"Steps done:        {result['steps_done']}")
    if summary.chain_ok is not None:
        verdict = "MATCHES an uninterrupted run" if summary.chain_ok else "DOES NOT MATCH"
        print(f"Hash chain:        {verdict}")


def check_expectations(scenario: dict[str, Any], summary: Summary) -> list[str]:
    """Return a list of problems (empty means the scenario passed)."""
    problems = []
    expect = scenario.get("expect") or {}
    if not summary.completed:
        problems.append(f"job did not complete: {summary.status}")
    if summary.chain_ok is False:
        problems.append("final hash chain differs from an uninterrupted run")
    if "exit_codes" in expect and summary.exit_codes != expect["exit_codes"]:
        problems.append(f"exit codes {summary.exit_codes}, expected {expect['exit_codes']}")
    tier = expect.get("emergency_tier")
    if tier:
        chosen = [e.get("chosen") for e in summary.of_type("emergency_decision")]
        allowed = tier if isinstance(tier, list) else [tier]
        if not set(allowed) & set(chosen):
            problems.append(f"expected a {allowed} emergency decision, saw {chosen}")
    if expect.get("rebalance_checkpoint"):
        reasons = [e.get("reason") for e in summary.of_type("checkpoint_done")]
        if "rebalance" not in reasons:
            problems.append(f"no rebalance checkpoint, saw {reasons}")
    return problems


def docker_available() -> bool:
    """True if the Docker engine is running."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def make_backend(choice: str):
    """Build a backend. ``auto`` prefers Docker and falls back to local processes."""
    if choice == "auto":
        choice = "docker" if docker_available() else "process"
        if choice == "process":
            print("Docker engine not running: using local processes instead.")
    return DockerBackend() if choice == "docker" else ProcessBackend()


def load_scenario(path: str | Path) -> dict[str, Any]:
    """Read a scenario file."""
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", default=str(REPO_ROOT / "local/scenarios/demo.yaml"))
    parser.add_argument("--backend", choices=["auto", "docker", "process"], default="auto")
    args = parser.parse_args()
    load_dotenv(REPO_ROOT / ".env")
    scenario = load_scenario(args.scenario)
    backend = make_backend(args.backend)
    try:
        summary = run_scenario(scenario, backend)
    finally:
        backend.cleanup()
    print_summary(summary)
    problems = check_expectations(scenario, summary)
    if problems:
        print("\nFAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nPASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
