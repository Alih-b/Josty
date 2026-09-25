"""Unit tests for :class:`josty.lease.LeasePool`.

Pure state, fake clock, no threads and no network: every case here is a
deterministic replay of a failure that was measured on real code.
"""

from __future__ import annotations

import pytest

from josty.lease import (
    SHED_CAPACITY,
    SHED_DEADLINE,
    SHED_GHOST_BUDGET,
    SHED_GHOST_CAPACITY,
    LeasePool,
)


def pool(capacity=1, *, lease_seconds=100.0, max_ghosts=None) -> LeasePool:
    return LeasePool(capacity, lease_seconds=lease_seconds, max_ghosts=max_ghosts)


def test_grant_then_release_restores_capacity():
    p = pool(capacity=2)
    a = p.acquire("brave", "q", now=0.0)
    assert a is not None
    assert p.held == 1 and p.effective_capacity == 2
    assert p.release(a.lease_id) is True
    assert p.held == 0
    assert p.issued == 1


def test_sheds_with_capacity_reason_when_full():
    p = pool(capacity=2)
    assert p.acquire("a", "q", now=0.0) is not None
    assert p.acquire("b", "q", now=0.0) is not None
    assert p.acquire("c", "q", now=0.0) is None
    assert p.shed_by_reason == {SHED_CAPACITY: 1}
    assert p.issued == 2 and p.shed == 1


def test_replays_the_measured_leak_without_leaking():
    """The reproduced HEAD failure: a ghost from one run refused the next run.

    capacity 1, lease 100ms, worker hangs past it. HEAD's BoundedSemaphore kept
    the slot for the ghost's whole life, so the next run was shed and the pool
    only recovered when the thread returned. Here the expired lease becomes an
    accounted ghost and a late release cannot double-free it.
    """
    p = pool(capacity=1, lease_seconds=0.1)
    slow = p.acquire("brave", "run-one", now=0.0)
    assert slow is not None

    # The run moves on (wait_for abandoned the await); the worker is still hung.
    assert p.reap(0.1) == 1
    assert p.held == 0
    assert p.ghosts == 1
    assert p.reclaimed == 1
    # A live ghost still owns a real OS thread, so admission width shrinks -- but
    # with max_ghosts defaulting to capacity this pool is now at its ceiling.
    assert p.effective_capacity == 0
    assert p.acquire("brave", "run-two", now=0.1) is None
    # refused because the live ghost owns the thread -- not because the pool is busy
    assert p.shed_by_reason == {SHED_GHOST_CAPACITY: 1}

    # The ghost finally returns and releases a lease it no longer holds.
    assert p.release(slow.lease_id) is True
    assert p.ghosts == 0
    assert p.effective_capacity == 1
    assert p.acquire("brave", "run-three", now=0.2) is not None


def test_release_is_idempotent_and_never_decrements_a_slot_it_does_not_hold():
    """Guard against the double-free that would inflate admission width."""
    p = pool(capacity=2, lease_seconds=0.1)
    a = p.acquire("brave", "q", now=0.0)
    b = p.acquire("brave", "q", now=0.0)
    assert a and b

    assert p.release(a.lease_id) is True
    assert p.release(a.lease_id) is False  # second release: no-op
    assert p.release(9999) is False  # never issued: no-op
    assert p.held == 1

    assert p.release(b.lease_id) is True
    # reap cannot resurrect a lease that was already released normally
    p.reap(0.1)
    assert p.ghosts == 0 and p.held == 0
    assert p.release(a.lease_id) is False


def test_release_after_reap_removes_ghost_and_cannot_be_counted_twice():
    p = pool(capacity=1, lease_seconds=0.1)
    lease = p.acquire("brave", "q", now=0.0)
    assert lease
    p.reap(0.1)
    assert p.ghosts == 1
    assert p.release(lease.lease_id) is True
    assert p.release(lease.lease_id) is False
    assert p.ghosts == 0 and p.held == 0
    # exactly one slot is available again -- not two
    assert p.acquire("brave", "1", now=0.2) is not None
    assert p.acquire("brave", "2", now=0.2) is None


def test_max_ghosts_zero_refuses_immediately_rather_than_oversubscribing():
    p = pool(capacity=1, lease_seconds=0.1, max_ghosts=0)
    lease = p.acquire("brave", "q", now=0.0)
    assert lease
    p.reap(0.1)
    assert p.acquire("brave", "q2", now=0.1) is None
    assert p.shed_by_reason == {SHED_GHOST_BUDGET: 1}


def test_expired_lease_does_not_grant_a_second_concurrent_slot():
    """max_ghosts > 0 must not silently exceed the physical cap."""
    p = pool(capacity=2, lease_seconds=0.1, max_ghosts=2)
    a = p.acquire("a", "q", now=0.0)
    b = p.acquire("b", "q", now=0.0)
    assert a and b
    p.reap(0.1)
    assert p.ghosts == 2
    # both real threads are still alive, so there is no room for more
    assert p.acquire("c", "q", now=0.1) is None
    assert p.shed_by_reason == {SHED_GHOST_CAPACITY: 1}
    assert p.held == 0


def test_deadline_refusal_has_its_own_reason():
    p = pool(capacity=4)
    assert p.acquire("a", "q", now=10.0, deadline=10.0) is None
    assert p.shed_by_reason == {SHED_DEADLINE: 1}
    assert p.issued == 0


def test_lease_expiry_is_clipped_to_the_run_deadline():
    p = pool(capacity=1, lease_seconds=100.0)
    lease = p.acquire("a", "q", now=0.0, deadline=5.0)
    assert lease is not None
    assert lease.expires_at == 5.0
    assert p.reap(4.9) == 0
    assert p.reap(5.0) == 1


def test_lease_ids_are_never_reused():
    p = pool(capacity=1, lease_seconds=0.1, max_ghosts=5)
    seen = []
    for i in range(4):
        lease = p.acquire("a", "q", now=float(i))
        assert lease is not None
        seen.append(lease.lease_id)
        p.reap(float(i) + 0.1)
        assert p.ghosts == 1
        # the slow worker finally returns and retires its own (already reaped) lease
        assert p.release(lease.lease_id) is True
        assert p.ghosts == 0
    assert seen == sorted(set(seen))


@pytest.mark.parametrize(
    "kwargs",
    [{"capacity": 0}, {"capacity": -1}, {"lease_seconds": 0}],
)
def test_pool_rejects_non_positive_limits(kwargs):
    with pytest.raises(ValueError):
        LeasePool(
            kwargs.get("capacity", 1), lease_seconds=kwargs.get("lease_seconds", 1.0)
        )


def test_shed_message_never_looks_like_an_upstream_failure():
    p = pool(capacity=1)
    assert p.acquire("a", "q", now=0.0) is not None
    assert p.acquire("b", "q", now=0.0) is None
    message = p.shed_message(SHED_CAPACITY)
    assert message.startswith("skipped: not issued")
    assert "timed out" not in message
    assert "TimeoutError" not in message