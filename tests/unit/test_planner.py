from __future__ import annotations

import math

import pytest

from spotsentry.config import parse_config
from spotsentry.planner.cost_model import CostInputs, estimate, young_interval_hours
from spotsentry.planner.pricing import on_demand_price, spot_price_estimate
from spotsentry.planner.profiler import Profile, profile_workload
from spotsentry.planner.recommend import (
    build_candidates,
    build_plan,
    format_summary,
    recommend,
    risk_score,
)
from spotsentry.planner.risk import AdvisorData, mtbi_hours_from_range


def test_cost_model_formula():
    result = estimate(CostInputs(runtime_h=10, checkpoint_cost_h=0.01, mtbi_h=100, interval_h=0.5,
                                 spot_price=0.03, on_demand_price=0.1, restart_h=0.05))
    assert result.expected_interruptions == pytest.approx(0.1)
    assert result.wall_time_h == pytest.approx(10 + 20 * 0.01 + 0.1 * (0.25 + 0.05))
    assert result.cost_spot == pytest.approx(result.wall_time_h * 0.03)
    assert result.cost_on_demand == pytest.approx(1.0)
    e90 = 0.1 + 1.28 * math.sqrt(0.1)
    assert result.wall_time_p90_h == pytest.approx(10.2 + e90 * 0.3)
    assert result.savings_fraction == pytest.approx(1 - result.cost_spot)


def test_young_hours_clamped():
    assert young_interval_hours(0.0001, 100, 0.01, 0.5) == pytest.approx(math.sqrt(0.02))
    assert young_interval_hours(0.0001, 100, 0.5, 1) == 0.5
    assert young_interval_hours(1, 100, 0.01, 0.5) == 0.5


def test_mtbi_from_advisor_range():
    assert mtbi_hours_from_range(0) == pytest.approx(730 / -math.log(0.95))
    assert mtbi_hours_from_range(0) > mtbi_hours_from_range(3) > mtbi_hours_from_range(4)
    assert math.isfinite(mtbi_hours_from_range(4))


def test_snapshot_loads_real_data(monkeypatch, tmp_path):
    monkeypatch.setattr("spotsentry.planner.risk.CACHE_FILE", tmp_path / "none.json")
    advisor = AdvisorData.load(offline=True)
    assert advisor.source == "snapshot"
    entry = advisor.entry("ap-south-1", "c5.large")
    assert entry is not None
    assert 0 <= entry.range_index <= 4
    assert advisor.entry("ap-south-1", "not.a.type") is None


def test_prices_table():
    assert on_demand_price("ap-south-1", "c5.large") == 0.085
    assert on_demand_price("mars-1", "c5.large") is None
    assert spot_price_estimate(0.1, 60) == pytest.approx(0.04)


def test_risk_score_ordering():
    assert risk_score(0, 10) < risk_score(0, 1) < risk_score(4, 1)
    assert risk_score(2, None) == 0.5


def profile(runtime_sec=3600.0) -> Profile:
    return Profile(
        job_id="j", mean_step_sec=0.1, std_step_sec=0.01, steps_per_epoch=100, epochs=10,
        predicted_runtime_sec=runtime_sec, predicted_low_sec=runtime_sec * 0.9,
        predicted_high_sec=runtime_sec * 1.1, eval_sec_per_epoch=0,
        tier_bytes={"full": 3000000}, capture_sec={"full": 0.01}, serialize_sec={"full": 0.01},
    )


class FakeAdvisor:
    source = "test"

    def __init__(self, table):
        self.table = table

    def entry(self, region, instance_type):
        from spotsentry.planner.risk import AdvisorEntry

        record = self.table.get(instance_type)
        return AdvisorEntry(*record) if record else None


def config():
    return parse_config({"job_id": "j"}, environ={})


def candidates(table, types=("c5.large", "m5.large"), prices=None, scores=None, **kwargs):
    return build_candidates(
        profile(**kwargs),
        list(types),
        ["ap-south-1"],
        FakeAdvisor(table),
        offline=False,
        config=config(),
        live_prices=lambda r, t: (prices or {}).get(t),
        live_scores=lambda r, t: (scores or {}).get(t),
    )


def test_picks_cheapest_with_live_price_per_zone():
    found = candidates(
        {"c5.large": (0, 60), "m5.large": (0, 60)},
        prices={"c5.large": {"ap-south-1a": 0.05, "ap-south-1b": 0.03},
                "m5.large": {"ap-south-1a": 0.04}},
    )
    rec = recommend(found)
    assert rec.use_spot
    assert rec.best.instance_type == "c5.large"
    assert rec.best.az == "ap-south-1b"


def test_excludes_highest_risk_range_when_alternative_exists():
    found = candidates({"c5.large": (4, 90), "m5.large": (1, 40)})
    assert recommend(found).best.instance_type == "m5.large"


def test_uses_highest_risk_if_only_option():
    found = candidates({"c5.large": (4, 90)}, types=("c5.large",))
    assert recommend(found).best.instance_type == "c5.large"


def test_prefers_higher_placement_score_on_ties():
    found = candidates(
        {"c5.large": (0, 60)},
        types=("c5.large",),
        prices={"c5.large": {"ap-south-1a": 0.03, "ap-south-1b": 0.03}},
        scores={"c5.large": {"ap-south-1a": 3, "ap-south-1b": 9}},
    )
    assert recommend(found).best.az == "ap-south-1b"


def test_recommends_on_demand_when_spot_is_not_cheap_enough():
    found = candidates(
        {"c5.large": (0, 5)}, types=("c5.large",), prices={"c5.large": {"ap-south-1a": 0.084}}
    )
    rec = recommend(found)
    assert not rec.use_spot
    assert "not worth" in rec.reason


def test_recommends_on_demand_for_tight_deadline():
    found = candidates({"c5.large": (3, 60)}, types=("c5.large",), runtime_sec=100 * 3600.0)
    rec = recommend(found, deadline_hours=100.0)
    assert not rec.use_spot
    assert "deadline" in rec.reason


def test_no_candidates_is_an_error():
    with pytest.raises(ValueError):
        recommend([])


def test_offline_estimates_price_and_plan_has_runtime_fields():
    found = build_candidates(
        profile(), ["c5.large"], ["ap-south-1"], FakeAdvisor({"c5.large": (1, 60)}),
        offline=True, config=config(),
    )
    assert found[0].spot_price == pytest.approx(0.085 * 0.4)
    assert "estimated" in found[0].price_source
    rec = recommend(found)
    plan = build_plan(profile(), rec, "snapshot", True)
    assert plan["offline"] is True
    assert plan["mtbi_seconds"] > 0
    assert plan["recommended_interval_sec"] > 0
    assert "SPOT" in format_summary(profile(), rec, "snapshot")


def test_profiler_measures_synthetic_job(tmp_path):
    cfg = parse_config(
        {
            "job_id": "p",
            "workload": {
                "class": "workloads.synthetic:SyntheticJob",
                "params": {"total_steps": 1000, "step_seconds": 0.002, "state_mb": 1},
            },
            "store": {"type": "local", "root": str(tmp_path / "store")},
        },
        environ={},
    )
    result = profile_workload(cfg, warmup_steps=2, profile_steps=10)
    assert result.mean_step_sec >= 0.002
    assert result.predicted_runtime_sec == pytest.approx(result.mean_step_sec * 1000, rel=0.01)
    assert result.tier_bytes["full"] == pytest.approx(3_000_000, rel=0.01)
    assert result.upload_mbps and result.upload_mbps > 0
    assert not (tmp_path / "store" / "jobs" / "p" / ".upload-test").exists()


def test_profiler_survives_unreachable_store(tmp_path):
    cfg = parse_config(
        {
            "job_id": "p",
            "workload": {
                "class": "workloads.synthetic:SyntheticJob",
                "params": {"total_steps": 10, "step_seconds": 0, "state_mb": 0.1},
            },
            "store": {"type": "s3", "endpoint_url": "http://127.0.0.1:9", "bucket": "x"},
        },
        environ={},
    )
    result = profile_workload(cfg, warmup_steps=1, profile_steps=2, upload_test=True)
    assert result.upload_mbps is None
    assert "not measured" in result.upload_note
