from __future__ import annotations

import json

import numpy as np
import pytest

from spotsentry import serialize
from spotsentry.adapters import Position, Tier
from spotsentry.checkpoint import CheckpointManager, make_envelope
from spotsentry.errors import SpotSentryError

ROOT = "jobs/j/"


class Dummy:
    def __init__(self):
        self.value = None
        self.fail_on = None

    def capture(self, tier):
        return {"value": self.value}

    def restore(self, state):
        if self.fail_on is not None and state["value"] == self.fail_on:
            raise ValueError("cannot load this one")
        self.value = state["value"]

    def supported_tiers(self):
        return [Tier.FULL]


def save(manager: CheckpointManager, value, position: Position, tier: Tier = Tier.FULL):
    payload = serialize.dumps(make_envelope({"value": value}, tier, position))
    entry, _ = manager.write_checkpoint(payload, tier, position)
    assert manager.commit(entry)
    return entry


def test_manifest_points_at_newest(store):
    manager = CheckpointManager(store, ROOT, "i-1")
    save(manager, 1, Position(0, 10))
    entry = save(manager, 2, Position(0, 20))
    manifest = json.loads(store.get_bytes(f"{ROOT}latest.json"))
    assert manifest["seq"] == entry.seq
    assert manifest["key"].endswith("000002_full_e0_s20.ckpt")
    assert manifest["history"][0]["seq"] == 1


def test_restore_returns_latest(store):
    manager = CheckpointManager(store, ROOT, "i-1")
    save(manager, 1, Position(0, 10))
    save(manager, 2, Position(1, 5))
    adapter = Dummy()
    result = CheckpointManager(store, ROOT, "i-2").restore(adapter)
    assert result.position == Position(1, 5)
    assert adapter.value == 2
    assert not result.fallback


def test_restore_with_no_checkpoint(store):
    result = CheckpointManager(store, ROOT, "i-1").restore(Dummy())
    assert result.position == Position()
    assert result.entry is None


def test_corrupt_latest_falls_back_to_history(store):
    manager = CheckpointManager(store, ROOT, "i-1")
    save(manager, 1, Position(0, 10))
    entry = save(manager, 2, Position(0, 20))
    store.put_bytes(entry.key, b"corrupted bytes")
    adapter = Dummy()
    result = CheckpointManager(store, ROOT, "i-2").restore(adapter)
    assert result.fallback
    assert result.position == Position(0, 10)
    assert adapter.value == 1


def test_unloadable_latest_falls_back(store):
    manager = CheckpointManager(store, ROOT, "i-1")
    save(manager, 1, Position(0, 10))
    save(manager, 2, Position(0, 20))
    adapter = Dummy()
    adapter.fail_on = 2
    assert CheckpointManager(store, ROOT, "i-2").restore(adapter).position == Position(0, 10)


def test_all_corrupt_starts_from_scratch(store):
    manager = CheckpointManager(store, ROOT, "i-1")
    entries = [save(manager, i, Position(0, i)) for i in range(1, 4)]
    for entry in entries:
        store.put_bytes(entry.key, b"bad")
    result = CheckpointManager(store, ROOT, "i-2").restore(Dummy())
    assert result.entry is None
    assert result.position == Position()


def test_corrupt_manifest_recovers_from_objects(store):
    manager = CheckpointManager(store, ROOT, "i-1")
    save(manager, 1, Position(0, 10))
    save(manager, 2, Position(0, 20))
    store.put_bytes(f"{ROOT}latest.json", b"{not json")
    adapter = Dummy()
    result = CheckpointManager(store, ROOT, "i-2").restore(adapter)
    assert result.position == Position(0, 20)
    assert adapter.value == 2


def test_retention_keeps_last_and_referenced(store):
    manager = CheckpointManager(store, ROOT, "i-1", keep_last=3)
    for i in range(1, 7):
        save(manager, i, Position(0, i))
    keys = store.list_keys(f"{ROOT}checkpoints/")
    assert len(keys) == 3
    assert keys[-1].endswith("000006_full_e0_s6.ckpt")
    manifest = json.loads(store.get_bytes(f"{ROOT}latest.json"))
    for item in [manifest, *manifest["history"]]:
        assert item["key"] in keys


def test_late_older_checkpoint_does_not_move_manifest_back(store):
    manager = CheckpointManager(store, ROOT, "i-1")
    payload = serialize.dumps(make_envelope({"value": 1}, Tier.FULL, Position(0, 1)))
    slow_entry, _ = manager.write_checkpoint(payload, Tier.FULL, Position(0, 1))
    fast = save(manager, 2, Position(0, 2))
    assert not manager.commit(slow_entry)
    assert json.loads(store.get_bytes(f"{ROOT}latest.json"))["seq"] == fast.seq


def test_fenced_worker_cannot_commit(store):
    manager = CheckpointManager(store, ROOT, "i-1", fence=lambda: True)
    payload = serialize.dumps(make_envelope({"value": 1}, Tier.FULL, Position(0, 1)))
    entry, _ = manager.write_checkpoint(payload, Tier.FULL, Position(0, 1))
    assert not manager.commit(entry)
    assert not store.exists(f"{ROOT}latest.json")


def test_failed_upload_leaves_manifest_unchanged(store):
    manager = CheckpointManager(store, ROOT, "i-1")
    save(manager, 1, Position(0, 1))
    original = store.upload

    def boom(*args, **kwargs):
        raise OSError("network down")

    store.upload = boom
    with pytest.raises(OSError):
        manager.write_checkpoint(b"data", Tier.FULL, Position(0, 2))
    store.upload = original
    assert json.loads(store.get_bytes(f"{ROOT}latest.json"))["step"] == 1


def test_seq_continues_across_workers(store):
    save(CheckpointManager(store, ROOT, "i-1"), 1, Position(0, 1))
    second = CheckpointManager(store, ROOT, "i-2")
    second.restore(Dummy())
    assert save(second, 2, Position(0, 2)).seq == 2


def test_status_round_trip(store):
    manager = CheckpointManager(store, ROOT, "i-1")
    assert manager.read_status() is None
    manager.write_status("completed", result={"a": 1})
    assert manager.read_status()["result"] == {"a": 1}


def test_numpy_serialize_round_trip():
    state = {
        "a": np.arange(10, dtype=np.float32),
        "nested": {"b": np.zeros((2, 3), dtype=np.int64), "c": [1, 2, {"d": "x"}]},
        "n": 5,
    }
    loaded = serialize.loads(serialize.dumps(state))
    assert np.array_equal(loaded["a"], state["a"])
    assert loaded["nested"]["b"].shape == (2, 3)
    assert loaded["nested"]["c"][2]["d"] == "x"
    loaded["a"][0] = 99  # must be writable


def test_json_serialize_and_compression():
    state = {"next_index": 5, "counters": {"processed": 5}}
    assert serialize.loads(serialize.dumps(state)) == state
    assert serialize.loads(serialize.dumps(state, compress=True)) == state


@pytest.mark.parametrize("data", [b"", b"xx", b"NOPE" + b"\0" * 20])
def test_garbage_is_rejected(data):
    with pytest.raises(SpotSentryError):
        serialize.loads(data)
