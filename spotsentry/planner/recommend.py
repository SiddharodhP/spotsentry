"""Combine the profile, risk data and prices into a recommendation and plan.json."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from spotsentry.config import SpotSentryConfig, load_config
from spotsentry.events import utc_now_iso
from spotsentry.planner.cost_model import (
    CostEstimate,
    CostInputs,
    estimate,
    young_interval_hours,
)
from spotsentry.planner.pricing import on_demand_price, spot_price_estimate, spot_prices_live
from spotsentry.planner.profiler import Profile, profile_workload, record_prediction
from spotsentry.planner.risk import RANGE_LABEL, AdvisorData, placement_scores
from spotsentry.store import make_store

RELATIVE_SPEED_FILE = Path(__file__).resolve().parent / "data" / "relative_speed.yaml"
HIGHEST_RANGE = 4
SPOT_WORTH_IT_RATIO = 0.9


@dataclass
class Candidate:
    """One place the job could run."""

    instance_type: str
    region: str
    az: str
    range_index: int
    spot_price: float
    on_demand_price: float
    placement_score: int | None
    price_source: str
    runtime_h: float
    mtbi_h: float
    interval_h: float
    estimate: CostEstimate
    risk_score: float


@dataclass
class Recommendation:
    """The outcome: what to run on, and whether spot is worth it."""

    use_spot: bool
    best: Candidate
    reason: str
    candidates: list[Candidate] = field(default_factory=list)


def risk_score(range_index: int, placement: int | None) -> float:
    """0 (safest) to 1 (riskiest). Advisor range counts most, placement score is secondary."""
    advisor = range_index / HIGHEST_RANGE
    if placement is None:
        return advisor
    return 0.7 * advisor + 0.3 * (1.0 - placement / 10.0)


def relative_speed(instance_type: str, path: Path = RELATIVE_SPEED_FILE) -> float:
    """How fast this type is relative to the machine that was profiled (1.0 if unknown)."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return float(data.get("relative_speed", {}).get(instance_type, 1.0))


def build_candidates(
    profile: Profile,
    types: list[str],
    regions: list[str],
    advisor: AdvisorData,
    offline: bool,
    config: SpotSentryConfig,
    mtbi_hours_override: float | None = None,
    live_prices=spot_prices_live,
    live_scores=placement_scores,
) -> list[Candidate]:
    """Estimate time, cost and risk for every instance type and zone."""
    runtime_base_h = profile.predicted_runtime_sec / 3600.0
    capture = profile.capture_sec.get("full", 0.0)
    block_h = capture / 3600.0
    min_h = config.checkpoint.min_interval_sec / 3600.0
    max_h = config.checkpoint.max_interval_sec / 3600.0
    candidates: list[Candidate] = []
    for region in regions:
        for instance_type in types:
            record = advisor.entry(region, instance_type)
            on_demand = on_demand_price(region, instance_type)
            if record is None or on_demand is None:
                continue
            prices = None if offline else live_prices(region, instance_type)
            source = "live"
            if not prices:
                prices = {region: spot_price_estimate(on_demand, record.savings_percent)}
                source = "estimated from advisor savings"
            scores = None if offline else live_scores(region, instance_type)
            runtime_h = runtime_base_h / relative_speed(instance_type)
            mtbi_h = mtbi_hours_override or record.mtbi_hours
            interval_h = young_interval_hours(block_h, mtbi_h, min_h, max_h)
            for az, price in prices.items():
                score = scores.get(az) if scores else None
                result = estimate(
                    CostInputs(runtime_h, block_h, mtbi_h, interval_h, price, on_demand)
                )
                candidates.append(
                    Candidate(
                        instance_type=instance_type,
                        region=region,
                        az=az,
                        range_index=record.range_index,
                        spot_price=price,
                        on_demand_price=on_demand,
                        placement_score=score,
                        price_source=source,
                        runtime_h=runtime_h,
                        mtbi_h=mtbi_h,
                        interval_h=interval_h,
                        estimate=result,
                        risk_score=risk_score(record.range_index, score),
                    )
                )
    return candidates


def recommend(
    candidates: list[Candidate], deadline_hours: float | None = None
) -> Recommendation:
    """Pick the cheapest acceptable candidate, then decide between spot and on-demand."""
    if not candidates:
        raise ValueError("no candidate has both advisor data and an on-demand price")
    acceptable = [c for c in candidates if c.range_index < HIGHEST_RANGE] or candidates
    # Rounded cost so near-ties are broken by placement score, then by lower risk.
    best = min(
        acceptable,
        key=lambda c: (round(c.estimate.cost_spot, 4), -(c.placement_score or 0), c.risk_score),
    )
    ratio = best.estimate.cost_spot / best.estimate.cost_on_demand
    if ratio >= SPOT_WORTH_IT_RATIO:
        return Recommendation(
            False, best, f"spot costs {ratio:.0%} of on-demand, not worth the risk", candidates
        )
    if deadline_hours is not None and best.estimate.wall_time_p90_h > deadline_hours:
        return Recommendation(
            False,
            best,
            f"spot would likely take {best.estimate.wall_time_p90_h:.2f} h (90th percentile), "
            f"over the {deadline_hours:g} h deadline",
            candidates,
        )
    return Recommendation(True, best, f"saves about {1 - ratio:.0%}", candidates)


def build_plan(profile: Profile, rec: Recommendation, advisor_source: str, offline: bool) -> dict:
    """Build the contents of plan.json, which the runtime reads for M and the interval."""
    best = rec.best
    return {
        "created_at": utc_now_iso(),
        "offline": offline,
        "advisor_source": advisor_source,
        "use_spot": rec.use_spot,
        "reason": rec.reason,
        "instance_type": best.instance_type,
        "region": best.region,
        "az": best.az,
        "interruption_range": RANGE_LABEL[best.range_index],
        "placement_score": best.placement_score,
        "mtbi_seconds": best.mtbi_h * 3600.0,
        "recommended_interval_sec": best.interval_h * 3600.0,
        "predicted_runtime_sec": best.runtime_h * 3600.0,
        "spot_price": best.spot_price,
        "on_demand_price": best.on_demand_price,
        "price_source": best.price_source,
        "expected_interruptions": best.estimate.expected_interruptions,
        "expected_wall_time_h": best.estimate.wall_time_h,
        "cost_spot": best.estimate.cost_spot,
        "cost_on_demand": best.estimate.cost_on_demand,
        "profile": profile.to_dict(),
        "candidates": [
            {
                **{k: v for k, v in asdict(c).items() if k != "estimate"},
                "interruption_range": RANGE_LABEL[c.range_index],
                "cost_spot": c.estimate.cost_spot,
                "cost_on_demand": c.estimate.cost_on_demand,
                "expected_interruptions": c.estimate.expected_interruptions,
            }
            for c in rec.candidates
        ],
    }


def format_duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f} s"


def format_summary(profile: Profile, rec: Recommendation, advisor_source: str) -> str:
    """The readable summary printed by ``spotsentry plan``."""
    best = rec.best
    speed = profile.predicted_runtime_sec / max(best.runtime_h * 3600.0, 1e-9)
    spread = (profile.predicted_high_sec - profile.predicted_low_sec) / 2.0 / max(speed, 1e-9)
    score = best.placement_score if best.placement_score is not None else "n/a"
    choice = "SPOT" if rec.use_spot else "ON-DEMAND"
    return "\n".join(
        [
            f"Predicted run time: {format_duration(best.runtime_h * 3600)} "
            f"(+/- {format_duration(spread)}) on {best.instance_type}",
            f"Recommended: {best.instance_type} in {best.az} - interruption range "
            f"{RANGE_LABEL[best.range_index]}, placement score {score}",
            f"Expected interruptions: ~{best.estimate.expected_interruptions:.2f}   "
            f"Checkpoint interval: {format_duration(best.interval_h * 3600)} (Young, clamped)",
            f"Estimated cost: spot ${best.estimate.cost_spot:.3f} vs on-demand "
            f"${best.estimate.cost_on_demand:.3f} -> use {choice} ({rec.reason})",
            "Checkpoint sizes: "
            + ", ".join(f"{k} {v / 1e6:.1f} MB" for k, v in profile.tier_bytes.items()),
            f"Data: advisor {advisor_source}, spot price {best.price_source}",
        ]
    )


def run_plan(args: argparse.Namespace) -> int:
    """Implements ``spotsentry plan``."""
    config = load_config(args.job)
    types = [t.strip() for t in args.candidates.split(",") if t.strip()]
    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    print(f"Profiling {config.job_id}: {args.warmup_steps} warm-up + {args.profile_steps} steps...")
    profile = profile_workload(
        config,
        warmup_steps=args.warmup_steps,
        profile_steps=args.profile_steps,
        upload_test=not args.no_upload_test and not args.offline,
    )
    advisor = AdvisorData.load(offline=args.offline)
    candidates = build_candidates(
        profile, types, regions, advisor, args.offline, config, args.mtbi_hours
    )
    rec = recommend(candidates, args.deadline_hours)
    print()
    print(format_summary(profile, rec, advisor.source))
    if args.offline or advisor.source == "snapshot":
        print("Mode: OFFLINE (shipped or cached data, no live AWS prices)")
    record_prediction(profile, rec.best.instance_type)
    plan = build_plan(profile, rec, advisor.source, args.offline)
    if args.write:
        key = f"{config.store.prefix}{config.job_id}/plan.json"
        make_store(config.store).put_bytes(key, json.dumps(plan, indent=1).encode("utf-8"))
        print(f"Wrote {key}")
    return 0
