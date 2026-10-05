"""One provider branch: gate -> admit -> issue -> classify -> status.

Before this module every provider repeated the same eleven steps inline, and the
three policies that are easy to get wrong lived in more than one place: whether a
zero-result branch is a success, what text a refused call carries, and when the
breaker telemetry is read. They live here exactly once now, and an adapter
(:mod:`josty.providers`) only knows how to perform one upstream call.

The step order is load-bearing, not incidental:

1. ``pool.note_scheduled()`` before any gate, so a call that is refused is still
   visible as proposed-but-not-issued.
2. ``adapter.precheck`` (registry/availability) -- skipped, never a network error.
3. ``breaker.status`` -- a cool-down skip is also ``error_kind="skipped"``.
4. ``release_probe`` in a ``finally`` around everything after admission, so a
   HALF_OPEN trial slot can never leak.
5. admission under the run's semaphore; a refusal is shed, with the pool's
   message, and is never rendered as an upstream failure.
6. the per-call budget is the smaller of ``timeout + headroom`` and whatever is
   left of the run deadline.
7. the lease is released here unless the adapter says a worker kept it (a ghost
   releases its own, idempotently).
8-10. classification: timeout, classified error, then the empty-ok carve-out.
11. every status is built by :meth:`BranchRunner._status`, the single place
   breaker telemetry is stamped from a fresh ``get_state`` read at status-return
   time.

``run`` returns ``(results, ProviderStatus)``: the callers that keep the old
``_ddgs`` / ``github_run`` shape need the per-call rows for group fusion, and a
``ProviderStatus`` does not carry them. ``IssueOutcome`` stays internal.

Two admission decisions are deliberate and pinned, because both are invisible to
outcome-only assertions:

* **Slot before lease.** A branch takes the run's concurrency slot (step 5) and
  only acquires its pool lease inside it, so no provider is ever parked holding an
  expiring, capacity-counted lease. The ordering decides who loses the last slot
  under saturation, which is why the GitHub branch no longer sheds a web engine:
  the base revision issued 6 and shed 1 (``capacity``) where this one issues 7 and
  sheds 0. Measured 3/3 each way with six 250ms engines plus a 300ms GitHub reply,
  and pinned by
  ``tests/test_fanout_leases.py::test_slot_is_taken_before_a_lease_no_branch_holds_a_lease_while_parked``.
* **One pool for every branch.** ``github-api`` shares the search pool and
  semaphore rather than getting its own, so ``include_github=True`` cannot add a
  seventh concurrent request outside the per-run budget that invariant 4 and
  ``tests/test_fanout_leases.py::test_the_github_call_is_admitted_like_any_other``
  exist to bound.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .breaker import CircuitBreaker
from .errors import _classify_search_error
from .health import known_error_kind
from .lease import Lease, LeasePool
from .models import ProviderStatus, SearchResult
from .status import ErrorKind, SafeSearch, SearchCategory, TimeLimit

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from .fanout import _FanoutLedger


@dataclass(frozen=True)
class SearchCall:
    """One proposed upstream call: everything an adapter needs, nothing more."""

    query: str
    backend: str
    limit: int
    category: SearchCategory
    region: str | None
    safesearch: SafeSearch
    timelimit: TimeLimit | None


@dataclass(frozen=True)
class IssueOutcome:
    """What one issued call produced, before any policy is applied to it."""

    results: list[SearchResult]
    latency_ms: float
    error: BaseException | None
    #: True when a worker thread is still running and will release its own lease.
    lease_retained: bool = False


@dataclass(frozen=True)
class IssueContext:
    """Everything an adapter needs to perform one admitted call.

    The five fields travel together by construction and mean nothing apart, so
    they travel as one value rather than as five keyword arguments repeated on
    the Protocol and every adapter.
    """

    ledger: _FanoutLedger
    pool: LeasePool | None
    lease: Lease | None
    budget: float
    executor: ThreadPoolExecutor | None


class ProviderAdapter(Protocol):
    """The half of a provider that only it can know: how to reach upstream."""

    provider: str

    def precheck(self, call: SearchCall) -> str | None:
        """Skip reason (engine not installed/enabled, no host), or None to proceed."""

    async def issue(self, call: SearchCall, ctx: IssueContext) -> IssueOutcome:
        """Perform the network call.

        MUST call ctx.ledger.record() immediately before the network call.
        MUST guarantee ctx.pool.release(ctx.lease.lease_id) exactly once: before
        returning, or by the worker thread it leaves behind (idempotent release).
        ctx.lease is None when the call is unadmitted (standalone use).
        """


class BranchRunner:
    """gate -> admit -> issue -> classify -> status, once for every provider."""

    def __init__(
        self,
        *,
        breaker: CircuitBreaker,
        ledger: _FanoutLedger,
        pool: LeasePool | None,
        semaphore: asyncio.Semaphore,
        timeout: float,
        headroom: float,
        clock: Callable[[], float] = time.monotonic,
        executor: ThreadPoolExecutor | None = None,
        deadline: float | None = None,
    ) -> None:
        self._breaker = breaker
        self._ledger = ledger
        self._pool = pool
        self._semaphore = semaphore
        self._timeout = timeout
        self._headroom = headroom
        self._clock = clock
        # Run-scoped bindings (AMENDMENT 1). Fanout.runner() passes both
        # explicitly; a standalone runner leaves them None: no dedicated worker
        # pool and no outer deadline, which is what the unadmitted github_run
        # path wants.
        self._executor = executor
        self._deadline = deadline

    @classmethod
    def unadmitted(
        cls,
        *,
        breaker: CircuitBreaker,
        semaphore: asyncio.Semaphore,
        timeout: float,
        headroom: float,
    ) -> BranchRunner:
        """A runner for a direct caller rather than a run: no pool, no deadline.

        Nothing is scheduled or shed (there is no pool to refuse a call), but the
        call still takes the shared concurrency slot -- a standalone provider call
        must not become a request outside the cap it shares with search.
        """
        from .fanout import _FanoutLedger  # local: fanout imports this module

        return cls(
            breaker=breaker,
            ledger=_FanoutLedger(),
            pool=None,
            semaphore=semaphore,
            timeout=timeout,
            headroom=headroom,
        )

    async def run(
        self,
        call: SearchCall,
        adapter: ProviderAdapter,
    ) -> tuple[list[SearchResult], ProviderStatus]:
        """Run one branch. Returns its result rows and its status; never raises
        for an upstream failure."""
        deadline = self._deadline
        pool = self._pool
        if pool is not None:
            # Proposed before any gate: a registry-missing or breaker-skipped call
            # still counts as scheduled, which is what makes the fanout identity
            # scheduled == issued + shed + not-attempted hold for every call.
            pool.note_scheduled()
        reason = adapter.precheck(call)
        if reason is not None:
            return [], self._status(
                call,
                adapter.provider,
                ok=False,
                error=reason,
                error_kind="skipped",
                latency_ms=None,
            )
        allowed, skip_message = self._breaker.status(call.backend, "search")
        if not allowed:
            return [], self._status(
                call,
                adapter.provider,
                ok=False,
                error=skip_message,
                error_kind="skipped",
                latency_ms=None,
            )
        try:
            async with self._semaphore:
                return await self._admitted(call, adapter, deadline=deadline)
        finally:
            self._breaker.release_probe(call.backend, "search")

    # -- internals -------------------------------------------------------

    async def _admitted(
        self,
        call: SearchCall,
        adapter: ProviderAdapter,
        *,
        deadline: float | None,
    ) -> tuple[list[SearchResult], ProviderStatus]:
        pool = self._pool
        lease: Lease | None = None
        now = self._clock()
        if pool is not None:
            lease = pool.acquire("search", call.query, now=now, deadline=deadline)
            if lease is None:
                # Refused admission: no socket was opened, so this must never be
                # reported as an upstream failure. The reason names the cause.
                return [], self._status(
                    call,
                    adapter.provider,
                    ok=False,
                    result_count=0,
                    error=pool.shed_message(pool.last_shed_reason or "capacity"),
                    error_kind="skipped",
                    latency_ms=None,
                )
        budget = self._timeout + self._headroom
        if deadline is not None:
            budget = min(budget, max(0.0, deadline - now))
        retained = False
        try:
            outcome = await adapter.issue(
                call,
                IssueContext(
                    ledger=self._ledger,
                    pool=pool,
                    lease=lease,
                    budget=budget,
                    executor=self._executor,
                ),
            )
            retained = outcome.lease_retained
        finally:
            # Covers the exception path too. Idempotent: a worker that already
            # released its own lease (or was reaped as a ghost) makes this a no-op.
            if lease is not None and not retained:
                pool.release(lease.lease_id)
        return outcome.results, self._finish(call, adapter.provider, outcome)

    def _finish(
        self, call: SearchCall, provider: str, outcome: IssueOutcome
    ) -> ProviderStatus:
        """Apply breaker policy to one outcome (steps 8-10 of the contract)."""
        breaker = self._breaker
        exc = outcome.error
        latency_ms = outcome.latency_ms
        if exc is not None and not isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            breaker.record_latency(provider, latency_ms)
            error_kind = _classify_search_error(exc)
            if error_kind != "empty":
                breaker.record_failure(provider, "search")
                return self._status(
                    call,
                    provider,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    error_kind=error_kind,
                    latency_ms=latency_ms,
                )
            # An "empty" classification is the same successful-empty branch as a
            # call that returned zero rows: fall through to the block below.
        elif exc is not None:
            breaker.record_latency(provider, latency_ms)
            breaker.record_failure(provider, "search")
            return self._status(
                call,
                provider,
                ok=False,
                error="TimeoutError: search backend timed out",
                error_kind="network",
                latency_ms=latency_ms,
            )
        else:
            breaker.record_latency(provider, latency_ms)
        # The empty-ok carve-out, one home. An engine that answered with zero
        # results is a successful branch, and it must not clear rate-limit
        # history: a throttled engine answering with zero results must not reset
        # its own trip window. Only a variant that actually produced results
        # resets the breaker.
        if outcome.results:
            breaker.record_success(provider, "search")
            return self._status(
                call,
                provider,
                ok=True,
                result_count=len(outcome.results),
                error_kind=None,
                latency_ms=latency_ms,
            )
        # Zero results and no exception. Usually that is a genuinely quiet
        # engine -- but ddgs returns an empty list for a captcha page, a consent
        # wall and a JavaScript shell too, so at this point a blocked backend is
        # indistinguishable from an idle one. josty --health proves which it is;
        # when that proof is fresh and says this backend is unreadable, report
        # what was proved and let the breaker count the failure.
        proven = known_error_kind(provider)
        if proven is not None:
            breaker.record_failure(provider, "search")
            return self._status(
                call,
                provider,
                ok=False,
                error="health probe: backend was unreadable at last check",
                error_kind=proven,
                latency_ms=latency_ms,
            )
        return self._status(
            call,
            provider,
            ok=True,
            result_count=0,
            error_kind="empty",
            latency_ms=latency_ms,
        )

    def _status(
        self,
        call: SearchCall,
        provider: str,
        *,
        ok: bool,
        result_count: int = 0,
        error: str | None = None,
        error_kind: ErrorKind | None = None,
        latency_ms: float | None = None,
    ) -> ProviderStatus:
        """The one status factory; every return path stamps telemetry here.

        Read fresh from the breaker at status-return time: per-variant snapshots
        run concurrently, so an earlier snapshot is not recency.
        """
        b_state = self._breaker.get_state(provider)
        return ProviderStatus(
            provider,
            call.query,
            ok,
            result_count,
            error=error,
            error_kind=error_kind,
            latency_ms=latency_ms,
            circuit_state=b_state["state"],
            failures=b_state["failures"],
            backoff_remaining=b_state["backoff_remaining"],
        )
