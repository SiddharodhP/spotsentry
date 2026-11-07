from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from spotsentry.adapters import Tier
from spotsentry.config import CheckpointConfig, DeadlineConfig
from spotsentry.deadline import DeadlineSelector
from spotsentry.events import last_checkpoint_cost
from spotsentry.interval import IntervalTuner, young_interval

MB = 1_000_000
ALL = [Tier.FULL, Tier.LIGHT, Tier.TINY]


def selector(mode: str = "aware") -> DeadlineSelector:
    s = DeadlineSelector(DeadlineConfig(mode=mode, speed_safety_factor=1.0), ALL)
    s.throughput = 10 * MB
    s.serialize_speed = 1000 * MB
    for tier, size in zip(ALL, [300, 100, 50], strict=True):
        s.sizes[tier] = size * MB
    return s


@pytest.mark.parametrize(
    "time_left,expected",
    [(100, Tier.FULL), (31, Tier.FULL), (29, Tier.LIGHT), (11, Tier.LIGHT), (9, Tier.TINY),
     (6, Tier.TINY)],
)
def test_picks_largest_tier_that_fits(time_left, expected):
    assert selector().choose(time_left).chosen is expected


def test_skips_when_nothing_fits():
    decision = selector().choose(2)
    assert decision.chosen is None
    assert set(decision.predictions) == {"full", "light", "tiny"}


def test_naive_mode_always_picks_full():
    assert selector("naive").choose(0.1).chosen is Tier.FULL


def test_restricted_tiers():
    s = DeadlineSelector(DeadlineConfig(), [Tier.FULL])
    s.throughput = 10 * MB
    s.sizes[Tier.FULL] = 100 * MB
    assert s.choose(1).chosen is None


def test_safety_factor_inflates_prediction():
    s = selector()
    s.cfg = DeadlineConfig(speed_safety_factor=1.3)
    assert s.predict(Tier.LIGHT) == pytest.approx(100 / 1000 + 10 * 1.3)


def test_throughput_ewma():
    s = DeadlineSelector(DeadlineConfig(ewma_alpha=0.3), ALL)
    s.observe_upload(100, 1)
    assert s.throughput == 100
    s.observe_upload(200, 1)
    assert s.throughput == pytest.approx(0.3 * 200 + 0.7 * 100)


def test_hints_scale_with_measured_full():
    s = DeadlineSelector(DeadlineConfig(), ALL)
    s.set_hints({Tier.FULL: 100, Tier.LIGHT: 40, Tier.TINY: 20})
    assert s.size_of(Tier.LIGHT) == 40
    s.sizes[Tier.FULL] = 200
    assert s.size_of(Tier.LIGHT) == 80


def test_unknown_size_predicts_infinity_and_is_reported():
    s = DeadlineSelector(DeadlineConfig(), ALL)
    assert math.isinf(s.predict(Tier.FULL))
    assert s.missing_sizes() == ALL


def test_time_left_subtracts_margin():
    s = DeadlineSelector(DeadlineConfig(safety_margin_sec=15), ALL)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert s.time_left(now + timedelta(seconds=120), now) == pytest.approx(105)


def test_young_formula():
    assert young_interval(2, 3600) == pytest.approx(math.sqrt(2 * 2 * 3600))


def tuner(**kwargs) -> IntervalTuner:
    return IntervalTuner(CheckpointConfig(**kwargs))


def test_replacement_worker_starts_at_learned_interval():
    t = tuner(min_interval_sec=1, max_interval_sec=10000, mtbi_seconds_prior=100)
    t.set_history(runtime_sec=0.0, interruptions=0, cost_sec=2.0)
    assert t.interval == pytest.approx(young_interval(2.0, 100))


def test_first_worker_starts_at_minimum_interval():
    t = tuner(min_interval_sec=1, max_interval_sec=10000, mtbi_seconds_prior=100)
    t.set_history(runtime_sec=0.0, interruptions=0, cost_sec=None)
    assert t.interval == 1


def test_history_does_not_change_a_fixed_interval():
    t = tuner(interval=120)
    t.set_history(runtime_sec=50.0, interruptions=2, cost_sec=2.0)
    assert t.interval == 120


def test_cost_history_ignores_emergency_saves():
    events = [
        {"type": "checkpoint_done", "reason": "periodic", "blocking_s": 0.5},
        {"type": "checkpoint_done", "reason": "emergency", "blocking_s": 40.0},
        {"type": "job_progress"},
    ]
    assert last_checkpoint_cost(events) == 0.5
    assert last_checkpoint_cost(events[1:]) is None


def test_tuner_clamps_low_and_high():
    low = tuner(min_interval_sec=30, max_interval_sec=1800, mtbi_seconds_override=3600)
    low.observe_cost(0.001)
    assert low.recompute() == 30
    high = tuner(min_interval_sec=30, max_interval_sec=100, mtbi_seconds_override=360000)
    high.observe_cost(50)
    assert high.recompute() == 100


def test_tuner_uses_young_between_bounds():
    t = tuner(min_interval_sec=1, max_interval_sec=10000, mtbi_seconds_override=3600)
    t.observe_cost(2)
    assert t.recompute() == pytest.approx(young_interval(2, 3600))


def test_fixed_interval_ignores_cost():
    t = tuner(interval=120)
    t.observe_cost(50)
    assert t.recompute() == 120


def test_cost_ewma():
    t = tuner()
    t.observe_cost(1)
    t.observe_cost(2)
    assert t.cost == pytest.approx(0.3 * 2 + 0.7 * 1)


def test_mtbi_priority():
    assert tuner(mtbi_seconds_override=10).mtbi() == 10
    assert IntervalTuner(CheckpointConfig(), plan_mtbi_sec=500).mtbi() == 500
    assert tuner(mtbi_seconds_prior=700).mtbi() == 700
    assert tuner().mtbi() == 3600


def test_mtbi_live_update_formula():
    t = tuner(mtbi_seconds_prior=1000)
    t.set_history(runtime_sec=600, interruptions=3)
    assert t.mtbi(current_runtime_sec=400) == pytest.approx((3 * 1000 + 1000) / 6)


def test_more_interruptions_shorten_interval():
    calm = tuner(min_interval_sec=1, max_interval_sec=100000, mtbi_seconds_prior=3600)
    calm.observe_cost(1)
    rough = tuner(min_interval_sec=1, max_interval_sec=100000, mtbi_seconds_prior=3600)
    rough.observe_cost(1)
    rough.set_history(0, 9)
    assert rough.recompute() < calm.recompute()
