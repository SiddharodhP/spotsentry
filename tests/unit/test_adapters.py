from __future__ import annotations

import numpy as np
import pytest

from spotsentry import serialize
from spotsentry.adapters import ItemListAdapter, Tier
from workloads.synthetic import SyntheticJob, expected_chain


def test_item_list_round_trip():
    adapter = ItemListAdapter(items=["a", "b", "c"])
    adapter.next_index = 2
    adapter.counters = {"done": 2}
    state = serialize.loads(serialize.dumps(adapter.capture(Tier.FULL)))
    fresh = ItemListAdapter(items=["a", "b", "c"])
    fresh.restore(state)
    assert fresh.next_index == 2
    assert list(fresh.iter_items(fresh.next_index)) == [(2, "c")]


def run_steps(job: SyntheticJob, count: int) -> None:
    for _ in range(count):
        job.train_step(None)


def make_job() -> SyntheticJob:
    return SyntheticJob(total_steps=100, step_seconds=0, state_mb=0.01)


def test_synthetic_full_resume_is_exact():
    job = make_job()
    run_steps(job, 40)
    state = serialize.loads(serialize.dumps(job.adapter().capture(Tier.FULL)))
    resumed = make_job()
    resumed.adapter().restore(state)
    run_steps(resumed, 60)
    assert resumed.chain.hex() == expected_chain(100)
    assert resumed.result()["model_sum"] == 100


def test_synthetic_tier_sizes():
    job = SyntheticJob(total_steps=1, step_seconds=0, state_mb=1, optimizer_multiplier=2)
    sizes = {t: len(serialize.dumps(job.adapter().capture(t))) for t in Tier}
    assert sizes[Tier.FULL] == pytest.approx(3_000_000, rel=0.01)
    assert sizes[Tier.LIGHT] == pytest.approx(1_000_000, rel=0.01)
    assert sizes[Tier.TINY] == pytest.approx(500_000, rel=0.01)
    assert job.adapter().estimate_tier_bytes()[Tier.FULL] == 3_000_000


def test_synthetic_light_restore_resets_optimizer():
    job = make_job()
    run_steps(job, 10)
    adapter = make_job().adapter()
    adapter.restore(job.adapter().capture(Tier.LIGHT))
    assert adapter.last_restore_info["optimizer_reset"]
    assert adapter.job.optimizer.sum() == 0
    assert adapter.job.model.sum() == 10


def test_synthetic_capture_is_a_copy():
    job = make_job()
    state = job.adapter().capture(Tier.FULL)
    run_steps(job, 5)
    assert state["model"].sum() == 0


def test_expected_chain_differs_by_steps():
    assert expected_chain(10) != expected_chain(11)


torch = pytest.importorskip("torch", reason="torch not installed")


def build(seed: int = 1):
    from spotsentry.adapters.torch_trainer import TorchTrainerAdapter

    torch.manual_seed(seed)
    model = torch.nn.Sequential(torch.nn.Linear(4, 8), torch.nn.Dropout(0.5), torch.nn.Linear(8, 2))
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=5)
    return model, optimizer, scheduler, TorchTrainerAdapter(model, optimizer, scheduler, 42)


def train(model, optimizer, scheduler, steps: int) -> None:
    for _ in range(steps):
        optimizer.zero_grad()
        loss = model(torch.rand(8, 4)).pow(2).mean()
        loss.backward()
        optimizer.step()
        scheduler.step()


def weights(model) -> torch.Tensor:
    return torch.cat([p.detach().flatten() for p in model.parameters()])


def test_torch_full_resume_matches_uninterrupted():
    model_a, opt_a, sched_a, _ = build()
    train(model_a, opt_a, sched_a, 20)

    model_b, opt_b, sched_b, adapter_b = build()
    train(model_b, opt_b, sched_b, 10)
    state = serialize.loads(serialize.dumps(adapter_b.capture(Tier.FULL)))

    model_c, opt_c, sched_c, adapter_c = build(seed=99)
    adapter_c.restore(state)
    train(model_c, opt_c, sched_c, 10)
    assert torch.equal(weights(model_a), weights(model_c))


def test_torch_capture_is_a_copy():
    model, optimizer, scheduler, adapter = build()
    train(model, optimizer, scheduler, 1)
    state = adapter.capture(Tier.FULL)
    before = state["model"]["0.weight"].clone()
    train(model, optimizer, scheduler, 3)
    assert torch.equal(state["model"]["0.weight"], before)


def test_torch_light_resets_optimizer():
    model, optimizer, scheduler, adapter = build()
    train(model, optimizer, scheduler, 5)
    state = serialize.loads(serialize.dumps(adapter.capture(Tier.LIGHT)))
    assert "optimizer" not in state
    fresh_model, fresh_opt, _, fresh = build(seed=7)
    train(fresh_model, fresh_opt, scheduler, 1)
    fresh.restore(state)
    assert fresh.last_restore_info["optimizer_reset"]
    assert not fresh_opt.state
    assert torch.equal(weights(fresh_model), weights(model))


def test_torch_tiny_is_half_size_and_casts_back():
    model, optimizer, scheduler, adapter = build()
    train(model, optimizer, scheduler, 5)
    state = serialize.loads(serialize.dumps(adapter.capture(Tier.TINY)))
    assert state["model"]["0.weight"].dtype == torch.float16
    assert "rng" not in state
    fresh_model, _, _, fresh = build(seed=7)
    fresh.restore(state)
    assert fresh_model[0].weight.dtype == torch.float32
    assert torch.allclose(fresh_model[0].weight, model[0].weight, atol=0.01)


def test_torch_rng_restored():
    _, _, _, adapter = build()
    state = adapter.capture(Tier.FULL)
    expected_torch = torch.rand(3)
    expected_numpy = np.random.rand(3)
    adapter.restore(serialize.loads(serialize.dumps(state)))
    assert torch.equal(torch.rand(3), expected_torch)
    assert np.array_equal(np.random.rand(3), expected_numpy)


def test_iter_batches_is_reproducible_and_skips():
    from torch.utils.data import TensorDataset

    _, _, _, adapter = build()
    data = TensorDataset(torch.arange(100).float().unsqueeze(1))
    full = [b[0].flatten().tolist() for _, b in adapter.iter_batches(data, 3, 0, batch_size=10)]
    resumed = list(adapter.iter_batches(data, 3, 4, batch_size=10))
    assert resumed[0][0] == 4
    assert [b[0].flatten().tolist() for _, b in resumed] == full[4:]
    other_epoch = [b[0].flatten().tolist() for _, b in adapter.iter_batches(data, 4, 0, 10)]
    assert other_epoch != full


def test_iter_batches_does_not_touch_global_rng():
    from torch.utils.data import TensorDataset

    _, _, _, adapter = build()
    data = TensorDataset(torch.arange(20).float().unsqueeze(1))
    before = torch.get_rng_state()
    list(adapter.iter_batches(data, 0, 0, batch_size=5))
    assert torch.equal(torch.get_rng_state(), before)


def test_torch_restore_through_manager_logs_restore_ok(tmp_path):
    """The adapter's restore info repeats the tier, which must not clash in the event."""
    from spotsentry.adapters.base import Position
    from spotsentry.checkpoint import CheckpointManager, make_envelope
    from spotsentry.store.local import LocalStore

    logged = []

    class Log:
        def log(self, event, **fields):
            logged.append((event, fields))

    _, _, _, adapter = build()
    manager = CheckpointManager(LocalStore(str(tmp_path)), "jobs/j/", "i-1", events=Log())
    position = Position(1, 2)
    payload = serialize.dumps(make_envelope(adapter.capture(Tier.FULL), Tier.FULL, position))
    entry, _ = manager.write_checkpoint(payload, Tier.FULL, position)
    assert manager.commit(entry)
    _, _, _, fresh = build(seed=7)
    result = manager.restore(fresh)
    assert result.entry is not None
    assert [f for e, f in logged if e == "restore_ok"][0]["tier"] == "full"
