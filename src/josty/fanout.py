"""One run's admission, worker pool, ledger, deadline, and accounting.

Per-run rather than per-instance ownership is the point. A worker that never
returns cannot shed a later run, and the dedicated search pool keeps trafilatura
extraction on the event loop's default executor (AGENTS.md, "Code Layout &
Seams"). This module is the only place those four run-scoped objects are built,
so a fanout pass cannot accidentally share one of them with its neighbour.

``Fanout.search`` owns what used to be ``engine._search_parts``' post-gather
half: merge each group's variants, aggregate per ``(group, provider)``, and
restamp breaker telemetry on the aggregated statuses -- the per-variant
snapshots ran concurrently, so only a fresh read after the gather reflects the
final state.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .branch import BranchRunner, SearchCall
from .breaker import CircuitBreaker
from .errors import _aggregate_engine_status
from .lease import LeasePool
from .models import ProviderStatus, SearchResult
from .ranking import merge_query_variants


class _FanoutLedger:
    """Per-run count of search calls actually issued.

    Incremented by worker threads immediately before a ddgs/GitHub call, so it
    counts invocations, not sockets: a scheduled task that never reaches its
    call site (registry-missing, breaker-skipped) contributes nothing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.issued = 0

    def record(self) -> None:
        with self._lock:
            self.issued += 1


@dataclass(frozen=True)
class Plan:
    """One proposed call: which group it belongs to, and what to call."""

    group_index: int
    call: SearchCall


class Fanout:
    """One run's admission, worker pool, ledger, deadline, and accounting."""

    def __init__(
        self,
        *,
        capacity: int,
        search_timeout: float,
        headroom: float,
        max_ghosts: int | None,
        semaphore: asyncio.Semaphore,
        breaker: CircuitBreaker,
        run_timeout: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        executor_factory: Callable[..., ThreadPoolExecutor] = ThreadPoolExecutor,
    ) -> None:
        self._breaker = breaker
        self._semaphore = semaphore
        self._timeout = search_timeout
        self._headroom = headroom
        self._clock = clock
        self._ledger = _FanoutLedger()
        self._pool = LeasePool(
            capacity,
            lease_seconds=search_timeout + headroom,
            max_ghosts=max_ghosts,
        )
        # Sized to the cap because admission guarantees held + ghosts <= capacity,
        # so a submission is never queued.
        self._executor = executor_factory(
            max_workers=capacity,
            thread_name_prefix="josty-search",
        )
        self._deadline = clock() + run_timeout if run_timeout is not None else None

    def runner(self) -> BranchRunner:
        """A pipeline bound to this run's ledger, pool, deadline and worker pool."""
        return BranchRunner(
            breaker=self._breaker,
            ledger=self._ledger,
            pool=self._pool,
            semaphore=self._semaphore,
            timeout=self._timeout,
            headroom=self._headroom,
            clock=self._clock,
            executor=self._executor,
            deadline=self._deadline,
        )

    async def search(
        self,
        plans: list[Plan],
        invoke: Callable[[Plan], Awaitable[tuple[list[SearchResult], ProviderStatus]]],
    ) -> tuple[list[list[SearchResult]], list[ProviderStatus]]:
        """Gather every plan, then fuse and aggregate in plan order."""
        batches = await asyncio.gather(*(invoke(plan) for plan in plans))
        group_results: dict[int, list[list[SearchResult]]] = {}
        engine_statuses: dict[tuple[int, str], list[ProviderStatus]] = {}
        engine_items: dict[tuple[int, str], list[list[SearchResult]]] = {}
        for plan, (items, status) in zip(plans, batches, strict=True):
            group_results.setdefault(plan.group_index, []).append(items)
            key = (plan.group_index, plan.call.backend)
            engine_statuses.setdefault(key, []).append(status)
            engine_items.setdefault(key, []).append(items)
        statuses: list[ProviderStatus] = []
        for key, key_statuses in engine_statuses.items():
            agg = _aggregate_engine_status(key_statuses, engine_items[key])
            # Stamp breaker fields AFTER the gather: per-variant snapshots ran
            # concurrently, so only a fresh read reflects the final state.
            b_state = self._breaker.get_state(key[1])
            agg.circuit_state = b_state["state"]
            agg.failures = b_state["failures"]
            agg.backoff_remaining = b_state["backoff_remaining"]
            statuses.append(agg)
        lists: list[list[SearchResult]] = []
        for group_index in sorted(group_results):
            merged = merge_query_variants(
                [items for items in group_results[group_index] if items]
            )
            if merged:
                lists.append(merged)
        return lists, statuses

    def accounting(self) -> dict[str, Any]:
        """The six ``SearchRun`` fanout fields, measured after :meth:`close`."""
        return {
            "request_count": self._ledger.issued,
            "scheduled_count": self._pool.scheduled,
            "shed_count": self._pool.shed,
            "shed_by_reason": self._pool.shed_by_reason,
            "ghosts_outstanding": self._pool.ghosts,
            "ghosts_peak": self._pool.ghosts_peak,
        }

    async def close(self) -> None:
        """Retire the worker pool and reap expired leases into ghost accounting."""
        # Do not join: a ghost is exactly the thread we are choosing not to wait
        # for. Reaping records the ones still outstanding so the run reports them
        # instead of hiding them.
        self._executor.shutdown(wait=False)
        self._pool.reap(self._clock())
