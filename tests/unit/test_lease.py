from __future__ import annotations

import threading
import time

import pytest

from spotsentry.lease import Lease, LeaseStopped

ROOT = "jobs/j/"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(store, owner, clock=None, **kwargs) -> Lease:
    defaults = {"ttl_sec": 60, "heartbeat_sec": 15, "poll_sec": 0.01}
    return Lease(store, ROOT, owner, clock=clock or time.time, **{**defaults, **kwargs})


def test_first_worker_acquires(store):
    assert make(store, "a").try_acquire()


def test_second_worker_is_refused(store):
    clock = Clock()
    assert make(store, "a", clock).try_acquire()
    assert not make(store, "b", clock).try_acquire()


def test_expired_lease_can_be_taken(store):
    clock = Clock()
    assert make(store, "a", clock).try_acquire()
    clock.now += 61
    assert make(store, "b", clock).try_acquire()


def test_heartbeat_extends_expiry(store):
    clock = Clock()
    owner = make(store, "a", clock)
    owner.try_acquire()
    clock.now += 50
    assert owner.heartbeat()
    clock.now += 50
    assert not make(store, "b", clock).try_acquire()


def test_heartbeat_detects_takeover(store):
    clock = Clock()
    old = make(store, "a", clock)
    old.try_acquire()
    clock.now += 61
    assert make(store, "b", clock).try_acquire()
    assert not old.heartbeat()
    assert old.lost


def test_release_lets_others_in(store):
    clock = Clock()
    owner = make(store, "a", clock)
    owner.try_acquire()
    owner.release()
    assert make(store, "b", clock).try_acquire()


def test_release_does_not_remove_someone_elses_lease(store):
    clock = Clock()
    old = make(store, "a", clock)
    old.try_acquire()
    clock.now += 61
    new = make(store, "b", clock)
    new.try_acquire()
    old.release()
    assert new.read()[0]["owner"] == "b"


def test_only_one_of_many_wins(store):
    winners = []

    def attempt(name):
        if make(store, name).try_acquire():
            winners.append(name)

    threads = [threading.Thread(target=attempt, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_waiter_gets_lease_after_release(store):
    owner = make(store, "a")
    owner.try_acquire()
    waiter = make(store, "b")
    thread = threading.Thread(target=waiter.acquire)
    thread.start()
    time.sleep(0.05)
    assert thread.is_alive()
    owner.release()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert waiter.held


def test_waiter_writes_handover_request(store):
    owner = make(store, "a")
    owner.try_acquire()
    assert not owner.other_waiting()
    waiter = make(store, "b")
    thread = threading.Thread(target=waiter.acquire)
    thread.start()
    time.sleep(0.05)
    assert owner.other_waiting()
    owner.release()
    thread.join(timeout=2)
    assert not store.exists(f"{ROOT}lease_request.json")


def test_own_request_does_not_count_as_waiting(store):
    worker = make(store, "a")
    worker.try_acquire()
    store.put_bytes(
        f"{ROOT}lease_request.json", b'{"owner": "a", "requested_at": %f}' % time.time()
    )
    assert not worker.other_waiting()


def test_old_request_does_not_count(store):
    clock = Clock()
    owner = make(store, "a", clock)
    owner.try_acquire()
    store.put_bytes(f"{ROOT}lease_request.json", b'{"owner": "b", "requested_at": 1000.0}')
    assert owner.other_waiting()
    clock.now += 61
    assert not owner.other_waiting()


def test_stop_check_abandons_waiting(store):
    make(store, "a").try_acquire()
    with pytest.raises(LeaseStopped):
        make(store, "b").acquire(stop_check=lambda: True)
