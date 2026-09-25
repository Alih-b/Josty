"""Per-run lease admission for blocking engine calls.

A blocked socket read cannot be cancelled from Python, so some worker threads
outlive their deadline. Tying slot ownership to worker completion (a
``threading.BoundedSemaphore`` released in the worker's own ``finally``) fails when
the pool lives on the ``Josty`` instance: a never-returning thread holds its slot
for the life of the process, so a ghost from one run sheds the next one entirely.
Measured: with capacity 1 and a 1.5s hang under a 0.1s deadline the next run was
refused with "search executor saturated", and the pool recovered only when the
ghost finally returned.

This pool grants a *lease* with an expiry instead of a slot that dies with its
worker, and each run builds its own. ``acquire`` never blocks: it grants or
refuses with a reason. A lease past its expiry whose worker has not returned is
moved to the ghost ledger, so capacity is accounted for explicitly and bounded
by ``max_ghosts`` rather than degrading silently or not at all.

Pure state: no threads, no clock, no asyncio. ``now`` is always a parameter,
which is what makes the whole policy replayable from a test with a fake clock.

Concurrency note. ``acquire``/``reap`` run on the event loop thread; ``release``
may run on a worker thread. Every mutation is a single ``dict`` operation on a
plain dict, which is atomic under the GIL, and ``reap`` snapshots before popping,
so a release that wins the race is indistinguishable from one that never needed
to run. That is why there is no lock here.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Why a scheduled call was never issued.
SHED_CAPACITY = "capacity"  # pool busy with live, non-ghost calls
SHED_GHOST_CAPACITY = "ghost_capacity"  # room exists only if live ghosts returned
SHED_GHOST_BUDGET = "ghost_budget"  # the explicit ghost ceiling was exceeded
SHED_DEADLINE = "deadline"  # the run budget was already spent

#: Human-readable prefix for a refused call. Kept on ``error_kind="skipped"`` so
#: a call that never opened a socket can never be rendered as an upstream
#: network failure.
SHED_ERROR_PREFIX = "skipped: not issued"


@dataclass(frozen=True)
class Lease:
    """One granted right to occupy a search worker."""

    lease_id: int
    provider: str
    query: str
    expires_at: float


class LeasePool:
    """Fixed-capacity admission with expiring leases and ghost accounting."""

    def __init__(
        self,
        capacity: int,
        *,
        lease_seconds: float,
        max_ghosts: int | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_ghosts is None:
            max_ghosts = capacity
        if max_ghosts < 0:
            raise ValueError("max_ghosts must not be negative")
        self.capacity = int(capacity)
        self.lease_seconds = float(lease_seconds)
        self.max_ghosts = int(max_ghosts)
        self._held: dict[int, Lease] = {}
        self._ghosts: dict[int, Lease] = {}
        self._next_id = 0
        self._issued = 0
        self._reclaimed = 0
        self._ghosts_peak = 0
        self._shed: dict[str, int] = {}
        self._last_shed: str | None = None
        self._scheduled = 0

    # -- read-only views -------------------------------------------------

    @property
    def held(self) -> int:
        return len(self._held)

    @property
    def ghosts(self) -> int:
        """Leases whose worker outlived its expiry and has not returned."""
        return len(self._ghosts)

    @property
    def effective_capacity(self) -> int:
        """Admission width: a live ghost still occupies a real OS thread."""
        return max(0, self.capacity - self.ghosts)

    @property
    def issued(self) -> int:
        return self._issued

    @property
    def reclaimed(self) -> int:
        return self._reclaimed

    @property
    def ghosts_peak(self) -> int:
        return self._ghosts_peak

    @property
    def shed_by_reason(self) -> dict[str, int]:
        return dict(self._shed)

    @property
    def shed(self) -> int:
        return sum(self._shed.values())

    @property
    def scheduled(self) -> int:
        return self._scheduled

    # -- accounting ------------------------------------------------------

    def note_scheduled(self) -> None:
        """One engine call was proposed to the fanout."""
        self._scheduled += 1

    # -- admission -------------------------------------------------------

    def acquire(
        self,
        provider: str,
        query: str,
        *,
        now: float,
        deadline: float | None = None,
    ) -> Lease | None:
        """Grant a lease, or return ``None`` having recorded a shed reason."""
        self.reap(now)
        # Strict comparison so the ceiling reads as "tolerate at most this many
        # ghosts": max_ghosts=0 means "admit nothing while a ghost exists", and
        # the default (= capacity) never fires because effective_capacity below
        # already refuses once ghosts consume the pool.
        if self.ghosts > self.max_ghosts:
            return self._refuse(SHED_GHOST_BUDGET)
        if deadline is not None and now >= deadline:
            return self._refuse(SHED_DEADLINE)
        if self.held >= self.effective_capacity:
            # Name the ghost explicitly: "the pool is busy" and "the pool is busy
            # because N threads are stuck in a socket read" call for different
            # operator responses, and only one of them recovers on its own.
            reason = SHED_GHOST_CAPACITY if self.ghosts else SHED_CAPACITY
            return self._refuse(reason)
        self._next_id += 1
        expires_at = now + self.lease_seconds
        if deadline is not None:
            expires_at = min(expires_at, deadline)
        lease = Lease(self._next_id, provider, query, expires_at)
        self._held[lease.lease_id] = lease
        self._issued += 1
        return lease

    def release(self, lease_id: int) -> bool:
        """Retire a lease. Idempotent by construction.

        A worker that was slow rather than dead returns *after* ``reap`` already
        moved its lease to the ghost ledger, so this must be a discard-if-absent
        lookup rather than a counter decrement: a second release, an unknown id,
        or a release racing a reap must all be safe no-ops. A double-decrement
        here would permanently inflate admission width and admit more sockets
        than the cap allows.
        """
        if self._held.pop(lease_id, None) is not None:
            return True
        return self._ghosts.pop(lease_id, None) is not None

    def reap(self, now: float) -> int:
        """Move expired outstanding leases to the ghost ledger."""
        expired = [lid for lid, lease in self._held.items() if lease.expires_at <= now]
        reclaimed = 0
        for lease_id in expired:
            lease = self._held.pop(lease_id, None)
            if lease is not None:  # a concurrent release may have won the race
                self._ghosts[lease_id] = lease
                self._reclaimed += 1
                reclaimed += 1
        if len(self._ghosts) > self._ghosts_peak:
            self._ghosts_peak = len(self._ghosts)
        return reclaimed

    # -- internals -------------------------------------------------------

    def _refuse(self, reason: str) -> None:
        self._shed[reason] = self._shed.get(reason, 0) + 1
        self._last_shed = reason
        return None

    @property
    def last_shed_reason(self) -> str | None:
        """Reason for the most recent refusal, set by :meth:`acquire`."""
        return self._last_shed

    def shed_message(self, reason: str) -> str:
        """Honest provider-status text for a refused call."""
        return f"{SHED_ERROR_PREFIX} ({reason})"
