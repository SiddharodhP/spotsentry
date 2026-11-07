"""Run an experiment and write one CSV row per trial to experiments/results/.

    python experiments/run.py --config experiments/configs/e1.yaml
    python experiments/run.py --config experiments/configs/e1.yaml --quick

Every config fixes its seeds and runs at least 5 repetitions. ``--quick`` uses the config's
``quick`` section, for checking that things work; quick results are not for the paper.
"""

from __future__ import annotations

import argparse
import copy
import csv
import itertools
import math
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from experiments.trial import TrialSpec, run_trial  # noqa: E402

BIG_STATE_MB = 500
BIG_STATE_SLOT = threading.Lock()

RESULTS_DIR = REPO_ROOT / "experiments" / "results"
RUNNERS: dict[str, Any] = {}

# Fast settings shared by trials, in scaled time.
FAST_LEASE = {"ttl_sec": 3, "heartbeat_sec": 1, "poll_sec": 0.2}
FAST_LOG = {"progress_every_steps": 5, "flush_interval_sec": 1.0}


def runner(name: str):
    def register(fn):
        RUNNERS[name] = fn
        return fn

    return register


def synthetic_job(job_id: str, params: dict[str, Any], **sections: Any) -> dict[str, Any]:
    job = {
        "job_id": job_id,
        "workload": {"class": "workloads.synthetic:SyntheticJob", "params": dict(params)},
        "store": {"type": "local"},
        "metadata": {"poll_interval_sec": 0.2},
        "lease": dict(FAST_LEASE),
        "logging": dict(FAST_LOG),
        "deadline": {"safety_margin_sec": 0.5},
        "checkpoint": {
            "min_interval_sec": 0.5,
            "max_interval_sec": 600,
            "rebalance_min_gap_sec": 1,
        },
    }
    for key, value in sections.items():
        job[key] = {**job.get(key, {}), **value}
    return job


def checkpoint_for(strategy: dict[str, Any], scale: float) -> dict[str, Any]:
    """Build the checkpoint section. Times in the config are real-world seconds, scaled here."""
    section = {"strategy": strategy["strategy"]}
    interval = strategy.get("interval_sec")
    if interval is not None:
        section["interval"] = interval / scale
    elif strategy["strategy"] in ("periodic", "hybrid"):
        section["interval"] = "auto"
    if strategy.get("mtbi_prior_sec") is not None:
        section["mtbi_seconds_prior"] = strategy["mtbi_prior_sec"] / scale
    return section


@runner("strategies")
def run_strategies(cfg: dict[str, Any], rep: int, case: dict[str, Any]) -> dict[str, Any]:
    """E1 and E6: how a strategy behaves under exponential interruptions."""
    scale = float(cfg["time_scale"])
    strategy, mtbi = case["strategy"], case["mtbi"]
    mean = float(mtbi["seconds"]) / scale
    section = checkpoint_for(strategy, scale)
    if section.get("interval") == "auto" and "mtbi_seconds_prior" not in section:
        section["mtbi_seconds_override"] = mean
    job = synthetic_job(
        f"{cfg['id']}-{strategy['name']}-{mtbi['name']}-{rep}",
        cfg["workload"],
        checkpoint={**section, **cfg.get("checkpoint", {})},
        store=cfg.get("store", {}),
    )
    params = cfg["workload"]
    ideal = params["total_steps"] * params["step_seconds"]
    spec = TrialSpec(
        job=job,
        mean_interruption_sec=mean,
        # Every strategy faces the same interruption times at a given M and repetition.
        seed=hash_seed(cfg["seed"], mtbi["name"], rep),
        notice_lead_sec=120.0 / scale,
        restart_delay_sec=180.0 / scale,
        max_wall_sec=ideal * float(cfg.get("give_up_factor", 8)),
    )
    result = run_trial(spec)
    spot = float(cfg.get("spot_price", 0.034))
    on_demand = float(cfg.get("on_demand_price", 0.085))
    row = result.row()
    row["overhead_percent"] = 100.0 * result.blocking_sec / max(result.wall_sec, 1e-9)
    row["time_vs_ideal"] = result.wall_sec / ideal
    row["cost_vs_on_demand"] = (result.busy_sec * spot) / (ideal * on_demand)
    row["chain_ok"] = chain_ok(result.result)
    return row


def chain_ok(result: dict[str, Any]) -> bool | str:
    if "chain" not in result:
        return ""
    from workloads.synthetic import expected_chain

    return result["chain"] == expected_chain(int(result["steps_done"]))


def hash_seed(*parts: Any) -> int:
    """A stable seed. Python's own hash() differs between runs."""
    return random.Random("|".join(str(p) for p in parts)).randint(0, 2**31)


@runner("deadline")
def run_deadline(cfg: dict[str, Any], rep: int, case: dict[str, Any]) -> dict[str, Any]:
    """E2: one notice with a fixed time budget. Does the save finish before the deadline?"""
    mode, state_mb, mbps, budget = case["mode"], case["state_mb"], case["mbps"], case["budget"]
    job = synthetic_job(
        f"e2-{mode}-{state_mb}-{mbps}-{budget}-{rep}",
        {
            "total_steps": 100000,
            "step_seconds": 0.02,
            "state_mb": state_mb,
            "optimizer_multiplier": 2,
        },
        checkpoint={"strategy": "reactive"},
        deadline={"mode": mode, "override_seconds": budget, "hard_kill": True},
        store={"bandwidth_limit_mbps": mbps},
    )
    spec = TrialSpec(
        job=job,
        fixed_interruptions_sec=[1.0],
        notice_lead_sec=float(budget) + 30.0,
        restart_delay_sec=0,
        max_wall_sec=float(budget) + 60.0,
    )
    # The largest states need several GB each, so run only one of them at a time.
    with BIG_STATE_SLOT if state_mb >= BIG_STATE_MB else nullcontext():
        result = _run_until_first_exit(spec)
    events = result.events
    decision = next((e for e in events if e.get("type") == "emergency_decision"), {})
    saved = next((e for e in events if e.get("type") == "emergency_saved"), None)
    skipped = next((e for e in events if e.get("type") == "emergency_skipped"), None)
    outcome = "saved" if saved else ("skipped" if skipped else "none")
    met = bool((saved or skipped or {}).get("deadline_met", False))
    full_mb = state_mb * 3
    chosen = decision.get("chosen", "")
    tier = (saved or {}).get("tier", chosen)
    predictions = decision.get("predictions") or {}
    predicted = predictions.get(tier) if tier in predictions else ""
    # Without a save, the work since the last checkpoint is gone. There was no earlier
    # checkpoint here, so report the whole run so far as lost.
    return {
        "chosen": chosen,
        "outcome": outcome,
        "deadline_met": met,
        "saved": outcome == "saved",
        "full_mb": full_mb,
        "predicted_s": predicted,
        "actual_s": (saved or {}).get("actual_s", ""),
        "time_left": decision.get("time_left", ""),
        "throughput_mbps": decision.get("throughput_mbps", ""),
        "reason": (skipped or {}).get("reason", ""),
    }


def _run_until_first_exit(spec: TrialSpec):
    """E2 only cares about the first worker's emergency save, so it stops there."""
    spec.single_worker = True
    return run_trial(spec)


@runner("resume")
def run_resume(cfg: dict[str, Any], rep: int, case: dict[str, Any]) -> dict[str, Any]:
    """E3: does a job interrupted N times end with the same result as an uninterrupted one?"""
    interruptions, tier = case["planned_interruptions"], case["tier"]
    params = dict(cfg["workload"])
    params.setdefault("seed", cfg["seed"] + rep)
    tiers = [tier]
    job = {
        "job_id": f"e3-{tier}-{interruptions}-{rep}",
        "workload": {"class": "workloads.cifar_cnn:CifarCNN", "params": params},
        "store": {"type": "local"},
        "metadata": {"poll_interval_sec": 0.2},
        "lease": dict(FAST_LEASE),
        "logging": {"progress_every_steps": 20, "flush_interval_sec": 1.0},
        "deadline": {"safety_margin_sec": 0.5},
        "checkpoint": {"strategy": "reactive", "tiers": tiers},
    }
    times = [float(cfg["interrupt_every_sec"])] * interruptions
    spec = TrialSpec(
        job=job,
        fixed_interruptions_sec=times,
        delay_from_job_start=True,
        notice_lead_sec=30.0,
        restart_delay_sec=0.5,
        max_wall_sec=float(cfg.get("max_wall_sec", 3600)),
    )
    result = run_trial(spec)
    if result.interruptions != interruptions:
        raise RuntimeError(
            f"planned {interruptions} interruptions, got {result.interruptions}: "
            "the job ended first, so lower interrupt_every_sec"
        )
    outcome = result.result
    history = outcome.get("history", [])
    return {
        **result.row(),
        "test_accuracy": outcome.get("test_accuracy", ""),
        "test_loss": outcome.get("test_loss", ""),
        "weights_abs_sum": outcome.get("weights_abs_sum", ""),
        "history": yaml.safe_dump(history, default_flow_style=True).strip(),
        "optimizer_resets": sum(
            1 for e in result.events if e.get("type") == "restore_ok" and e.get("optimizer_reset")
        ),
    }


@runner("prediction")
def run_prediction(cfg: dict[str, Any], rep: int, case: dict[str, Any]) -> dict[str, Any]:
    """E4: predict a job's run time from a short profile, then run it and compare."""
    from spotsentry.config import parse_config
    from spotsentry.planner.profiler import profile_workload

    size = case["size"]
    if size["kind"] == "synthetic":
        job = synthetic_job(f"e4-{size['name']}-{rep}", size["params"],
                            checkpoint={"strategy": "none"})
    else:
        job = {
            "job_id": f"e4-{size['name']}-{rep}",
            "workload": {"class": "workloads.cifar_cnn:CifarCNN", "params": dict(size["params"])},
            "store": {"type": "local"},
            "metadata": {"enabled": False},
            "lease": dict(FAST_LEASE),
            "logging": {"progress_every_steps": 20},
            "checkpoint": {"strategy": "none"},
        }
    import tempfile

    from experiments.trial import TMP_DIR

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="e4-", dir=TMP_DIR) as tmp:
        profile_job = {**job, "store": {"type": "local", "root": tmp}}
        config = parse_config(profile_job, environ={})
        profile = profile_workload(
            config, warmup_steps=size.get("warmup", 5), profile_steps=size.get("profile", 30),
            upload_test=False,
        )
    spec = TrialSpec(job=job, max_wall_sec=float(cfg.get("max_wall_sec", 1800)))
    result = run_trial(spec)
    complete = next((e for e in result.events if e.get("type") == "job_complete"), {})
    actual = float(complete.get("runtime_sec") or result.wall_sec)
    predicted = profile.predicted_runtime_sec
    import socket

    return {
        "machine": socket.gethostname(),
        "workload": size["kind"],
        "predicted_sec": predicted,
        "actual_sec": actual,
        "error_percent": 100.0 * abs(predicted - actual) / actual,
        "completed": result.completed,
    }


@runner("async")
def run_async(cfg: dict[str, Any], rep: int, case: dict[str, Any]) -> dict[str, Any]:
    """E5: time the job is blocked per checkpoint, sync against async, with no interruptions."""
    mode, state_mb = case["mode"], case["state_mb"]
    params = {**cfg["workload"], "state_mb": state_mb, "optimizer_multiplier": 0}
    job = synthetic_job(
        f"e5-{mode}-{state_mb}-{rep}",
        params,
        checkpoint={
            "strategy": "periodic",
            "interval": float(cfg["interval_sec"]),
            "async": mode == "async",
        },
        store={"bandwidth_limit_mbps": cfg.get("bandwidth_mbps")},
    )
    ideal = params["total_steps"] * params["step_seconds"]
    result = run_trial(TrialSpec(job=job, max_wall_sec=float(cfg.get("max_wall_sec", 900))))
    done = [e for e in result.events if e.get("type") == "checkpoint_done"]
    blocks = [float(e.get("blocking_s") or 0.0) for e in done]
    complete = next((e for e in result.events if e.get("type") == "job_complete"), {})
    run_time = float(complete.get("runtime_sec") or result.wall_sec)
    return {
        **result.row(),
        "checkpoints_done": len(done),
        "blocking_per_checkpoint_s": sum(blocks) / len(blocks) if blocks else 0.0,
        "overhead_percent": 100.0 * sum(blocks) / max(run_time, 1e-9),
        "slowdown_percent": 100.0 * (run_time - ideal) / ideal,
    }


def expand_cases(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the config's ``grid`` into a list of cases (its cross product)."""
    grid = cfg["grid"]
    names = list(grid)
    return [dict(zip(names, values, strict=True)) for values in itertools.product(*grid.values())]


def case_columns(case: dict[str, Any]) -> dict[str, Any]:
    """Flatten a case into CSV columns."""
    columns = {}
    for key, value in case.items():
        if isinstance(value, dict):
            columns[key] = value.get("name")
            if key == "mtbi":
                columns["mtbi_seconds"] = value.get("seconds")
        else:
            columns[key] = value
    return columns


def load_experiment(path: str | Path, quick: bool) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if quick and cfg.get("quick"):
        cfg = {**copy.deepcopy(cfg), **cfg["quick"]}
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one experiment.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--parallel", type=int, default=None, help="trials to run at once")
    args = parser.parse_args()

    cfg = load_experiment(args.config, args.quick)
    fn = RUNNERS[cfg["runner"]]
    cases = expand_cases(cfg)
    repetitions = int(cfg.get("repetitions", 5))
    parallel = args.parallel or int(cfg.get("parallel", 1))
    trials = [(case, rep) for case in cases for rep in range(repetitions)]
    print(
        f"{cfg['id']}: {len(cases)} cases x {repetitions} repetitions = {len(trials)} trials, "
        f"{parallel} at a time",
        flush=True,
    )
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "_quick" if args.quick else ""
    output = RESULTS_DIR / f"{cfg['id']}{suffix}.csv"

    rows: list[dict[str, Any]] = []
    started = time.monotonic()

    def one(case: dict[str, Any], rep: int) -> dict[str, Any]:
        return {**case_columns(case), "rep": rep, **fn(cfg, rep, case)}

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(one, case, rep): (case, rep) for case, rep in trials}
        for index, future in enumerate(as_completed(futures), start=1):
            case, rep = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:  # noqa: BLE001
                print(f"  trial failed {case_columns(case)} rep {rep}: {exc}", flush=True)
                continue
            if index == len(trials) or index % max(len(trials) // 20, 1) == 0:
                minutes = (time.monotonic() - started) / 60
                print(f"  {index}/{len(trials)} done, {minutes:.1f} min", flush=True)
            _write_csv(output, rows)
    _write_csv(output, rows)
    print(f"Wrote {output} ({len(rows)} rows)")
    return 0 if len(rows) == len(trials) else 1


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {k: (v if not (isinstance(v, float) and math.isnan(v)) else "") for k, v in
                 row.items()}
            )


if __name__ == "__main__":
    sys.exit(main())
