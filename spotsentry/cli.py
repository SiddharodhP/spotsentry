"""Command line interface. Heavy imports stay inside commands so ``--help`` is instant."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _cmd_run(args: argparse.Namespace) -> int:
    from spotsentry.config import load_config
    from spotsentry.workload import run_job

    sys.path.insert(0, os.getcwd())
    return run_job(load_config(args.job))


def _cmd_status(args: argparse.Namespace) -> int:
    from spotsentry.checkpoint import CheckpointManager
    from spotsentry.config import load_config
    from spotsentry.events import read_events
    from spotsentry.store import make_store

    config = load_config(args.job)
    store = make_store(config.store)
    root = f"{config.store.prefix}{config.job_id}/"
    manager = CheckpointManager(store, root, "status")
    status = manager.read_status()
    manifest = manager.load_manifest()
    events = read_events(store, root)
    print(f"job:     {config.job_id}")
    print(f"status:  {json.dumps(status)}")
    if manifest:
        print(
            f"latest:  seq={manifest['seq']} tier={manifest['tier']} "
            f"epoch={manifest['epoch']} step={manifest['step']} "
            f"size={manifest['size_bytes']} bytes"
        )
    else:
        print("latest:  no checkpoint")
    print(f"events:  {len(events)}")
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    from spotsentry.planner.recommend import run_plan

    sys.path.insert(0, os.getcwd())
    return run_plan(args)


def _cmd_profile(args: argparse.Namespace) -> int:
    from spotsentry.config import load_config
    from spotsentry.planner.profiler import profile_workload

    sys.path.insert(0, os.getcwd())
    profile = profile_workload(
        load_config(args.job),
        warmup_steps=args.warmup_steps,
        profile_steps=args.profile_steps,
    )
    print(json.dumps(profile.to_dict(), indent=2))
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from spotsentry.config import load_config

    env = dict(os.environ)
    if args.job:
        env["SPOTSENTRY_DASHBOARD_JOB"] = str(Path(args.job).resolve())
    job_path = env.get("SPOTSENTRY_DASHBOARD_JOB", REPO_ROOT / "jobs" / "synthetic_demo.yaml")
    endpoint = load_config(job_path).store.endpoint_url or ""
    if "//minio:" in endpoint:
        # The hostname "minio" exists only inside Docker. From this machine it is localhost.
        env["SPOTSENTRY_STORE__ENDPOINT_URL"] = endpoint.replace("//minio:", "//127.0.0.1:")
    command = [
        sys.executable, "-m", "streamlit", "run", str(REPO_ROOT / "dashboard" / "app.py"),
        "--server.port", str(args.port), "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    return subprocess.call(command, env=env, cwd=REPO_ROOT)


def _cmd_experiment(args: argparse.Namespace) -> int:
    command = [sys.executable, str(REPO_ROOT / "experiments" / "run.py"), "--config", args.config]
    if args.quick:
        command.append("--quick")
    return subprocess.call(command, cwd=REPO_ROOT)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="spotsentry",
        description="Run long jobs on spot instances without losing progress.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a job, resuming from its latest checkpoint")
    run.add_argument("--job", required=True, help="path to a job YAML file")
    run.set_defaults(func=_cmd_run)

    plan = sub.add_parser("plan", help="recommend where to run a job and how often to checkpoint")
    plan.add_argument("--job", required=True)
    plan.add_argument("--candidates", default="c7i-flex.large,m7i-flex.large,t3.small",
                      help="comma-separated instance types")
    plan.add_argument("--regions", default="ap-southeast-2", help="comma-separated regions")
    plan.add_argument("--warmup-steps", type=int, default=10)
    plan.add_argument("--profile-steps", type=int, default=50)
    plan.add_argument("--mtbi-hours", type=float, default=None,
                      help="use this mean time between interruptions instead of the estimate")
    plan.add_argument("--deadline-hours", type=float, default=None,
                      help="recommend on-demand if spot would likely miss this deadline")
    plan.add_argument("--offline", action="store_true",
                      help="do not call AWS, use cached and shipped data")
    plan.add_argument("--no-upload-test", action="store_true",
                      help="skip the upload speed measurement")
    plan.add_argument("--write", action="store_true", help="save plan.json to the store")
    plan.set_defaults(func=_cmd_plan)

    profile = sub.add_parser("profile", help="measure a job's step time and checkpoint sizes")
    profile.add_argument("--job", required=True)
    profile.add_argument("--warmup-steps", type=int, default=10)
    profile.add_argument("--profile-steps", type=int, default=50)
    profile.set_defaults(func=_cmd_profile)

    dashboard = sub.add_parser("dashboard", help="open the live dashboard")
    dashboard.add_argument("--job", default=None, help="job YAML file to show")
    dashboard.add_argument("--port", type=int, default=8501)
    dashboard.set_defaults(func=_cmd_dashboard)

    experiment = sub.add_parser("experiment", help="run an experiment from experiments/configs")
    experiment.add_argument("--config", required=True)
    experiment.add_argument("--quick", action="store_true", help="fewer repetitions and sizes")
    experiment.set_defaults(func=_cmd_experiment)

    status = sub.add_parser("status", help="show a job's state and latest checkpoint")
    status.add_argument("--job", required=True)
    status.set_defaults(func=_cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``spotsentry`` command."""
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
