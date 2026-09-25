"""Fanout admission tests: bounded concurrency, honest sheds, ghost isolation.

Every fixture is chosen so the assertion *can* fail: the fakes hang past the
``timeout + headroom`` budget, because a fixture that always returns on time
cannot produce the ghost these properties are about.

Hermetic: ``ddgs.DDGS`` and the registry probe are patched, no network.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import josty.engine as eng


class _Tracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.entered = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.entered += 1
            self.peak = max(self.peak, self.active)

    def exit(self) -> None:
        with self.lock:
            self.active -= 1


def _hanging_ddgs(tracker: _Tracker, hang: float, *, answer: set[str] | None = None):
    """Fake ddgs: named engines sleep past any budget; others answer at once."""

    class FakeDDGS:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def text(self, query, backend=None, **kwargs):
            # answer=None means no engine answers (all hang); otherwise only the
            # named engines answer immediately.
            if answer is None or backend not in answer:
                tracker.enter()
                try:
                    time.sleep(hang)
                    return []
                finally:
                    tracker.exit()
            return [{"title": "t", "href": "https://example.com/x", "body": "b"}]

        news = text

    return FakeDDGS


def _engine(monkeypatch, tracker, *, backends, hang=0.5, budget=0.02, **kwargs):
    fake = _hanging_ddgs(tracker, hang, answer=kwargs.pop("answer", None))
    monkeypatch.setattr(eng, "DDGS", fake)
    monkeypatch.setattr(eng, "SEARCH_THREAD_TIMEOUT_HEADROOM", 0.0)
    monkeypatch.setattr(eng, "_engine_available", lambda category, backend: (True, None))
    return eng.Josty(
        backends=backends,
        timeout=budget,
        enable_cache=False,
        breaker_fail_threshold=100,
        **kwargs,
    )


def test_concurrency_cap_holds_while_calls_hang(monkeypatch):
    """max_search_concurrency must bound in-flight calls even when they hang.

    ``wait_for`` releases the asyncio semaphore when it fires while the worker
    keeps running, so the lease -- not the semaphore -- is what holds the count at
    the cap.
    """
    tracker = _Tracker()
    engine = _engine(
        monkeypatch,
        tracker,
        backends=(",".join(f"e{i}" for i in range(8)),),
        max_search_concurrency=1,
    )
    run = asyncio.run(engine.search_run("q", limit=5))

    assert tracker.peak == 1, f"cap violated: {tracker.peak} concurrent engine calls"
    assert run.scheduled_count == 8
    assert run.request_count == 1
    assert run.shed_count == 7
    assert set(run.shed_by_reason) == {"ghost_capacity"}


def test_a_call_that_never_opened_a_socket_is_never_a_network_failure(monkeypatch):
    """AGENTS.md invariant #4, made structural rather than incidental.

    A refused call must be reported as ``skipped``, never as a network failure for
    an engine that was never contacted.
    """
    tracker = _Tracker()
    engine = _engine(
        monkeypatch,
        tracker,
        backends=("e1,e2,e3,e4,e5,e6",),
        max_search_concurrency=2,
    )
    run = asyncio.run(engine.search_run("q", limit=5))

    network = [p for p in run.providers if p.error_kind == "network"]
    skipped = [p for p in run.providers if p.error_kind == "skipped"]
    assert tracker.entered == 2, "only the two admitted calls may reach ddgs"
    # Every provider that claims a network failure corresponds to a call that
    # actually entered ddgs. Nothing is invented.
    assert len(network) <= tracker.entered
    assert skipped, "the refused engines must be reported"
    for provider in skipped:
        assert provider.error is not None
        assert provider.error.startswith("skipped: not issued")


def test_fanout_accounting_is_balanced(monkeypatch):
    """The accounting identity, and the numbers reach the payload.

    This is the witness for AGENTS.md invariant 4, replacing a runtime check that
    could not fire: a fanout branch that stops accounting for itself breaks this
    equality, and a test catches that at review time. The registry/breaker skip
    path is covered by the not-attempted case below.
    """
    tracker = _Tracker()
    engine = _engine(monkeypatch, tracker, backends=("a,b,c,d",), max_search_concurrency=2)
    run = asyncio.run(engine.search_run("q", limit=5))

    assert run.scheduled_count == run.request_count + run.shed_count
    payload = run.dict()
    fanout = payload["fanout"]
    assert fanout["scheduled"] == 4
    assert fanout["issued"] == 2
    assert fanout["shed"] == 2
    assert fanout["shed_by_reason"] == {"ghost_capacity": 2}
    assert fanout["ghosts_outstanding"] == 2
    assert fanout["ghosts_peak"] == 2


def test_not_attempted_calls_are_accounted_not_counted_as_failures(monkeypatch):
    tracker = _Tracker()
    monkeypatch.setattr(eng, "DDGS", _hanging_ddgs(tracker, 0.5))
    monkeypatch.setattr(eng, "_engine_available", lambda category, backend: (False, "no engine"))
    engine = eng.Josty(backends=("a,b",), timeout=0.02, enable_cache=False)
    run = asyncio.run(engine.search_run("q", limit=5))

    assert run.scheduled_count == 2
    assert run.request_count == 0
    assert run.shed_count == 0
    assert tracker.entered == 0
    assert all(p.error_kind == "skipped" for p in run.providers)
    assert run.dict()["fanout"]["ghosts_outstanding"] == 0


def test_the_run_still_returns_at_the_wait_for_boundary(monkeypatch):
    """A dedicated search pool is not joined at shutdown.

    ``asyncio.run()`` joins the loop's default executor, so search must run on its
    own pool: the run returns at the ``wait_for`` boundary with the worker still
    blocked, instead of waiting the hang out.
    """
    hang = 1.5
    tracker = _Tracker()
    engine = _engine(
        monkeypatch,
        tracker,
        backends=("brave,duckduckgo",),
        hang=hang,
        budget=0.05,
        max_search_concurrency=1,
    )
    run = asyncio.run(engine.search_run("process block test", limit=1))

    assert run.status == "failed"
    assert tracker.active == 1, "the run returned before the ghost finished"
    assert run.ghosts_outstanding == 1


def test_search_ghosts_do_not_starve_page_text_extraction(monkeypatch):
    """Search work must not occupy the executor trafilatura needs.

    Extraction has to complete while a search ghost still holds a search worker;
    if the two shared a pool, the fetch could not run at all.
    """
    hang = 1.5
    tracker = _Tracker()
    engine = _engine(
        monkeypatch,
        tracker,
        backends=("brave,fast",),
        hang=hang,
        budget=0.05,
        answer={"fast"},
        max_search_concurrency=2,  # both engines admitted: one hangs, one answers
    )

    async def fake_download(self, client, url):
        return "<html><body><p>ok</p></body></html>", url

    monkeypatch.setattr(eng.Josty, "_download", fake_download)

    run = asyncio.run(engine.search_run("q", limit=1, fetch=True))

    assert run.results, "the healthy engine must still produce a result"
    assert run.ghosts_outstanding == 1, "the hanging engine should be a live ghost"
    assert run.results[0].content is not None
    assert run.fetch_ok == 1


def test_run_timeout_sheds_with_the_deadline_reason(monkeypatch):
    """The opt-in outer bound turns unbounded waves into a bounded, honest shed."""
    tracker = _Tracker()
    engine = _engine(
        monkeypatch,
        tracker,
        backends=("a,b,c,d,e,f",),
        hang=5.0,
        budget=5.0,
        max_search_concurrency=1,
        run_timeout=0.05,
    )
    run = asyncio.run(engine.search_run("q", limit=5))

    assert run.shed_count > 0
    assert {"ghost_capacity", "deadline"} & set(run.shed_by_reason)
    payload = run.dict()
    assert payload["fanout"]["shed"] == run.shed_count


def test_the_github_call_is_admitted_like_any_other(monkeypatch):
    """An included GitHub call is proposed and bounded, not a free extra request.

    It records on the same ledger, so counting it as issued without counting it as
    scheduled made fanout.issued exceed fanout.scheduled on every run with
    include_github=True, and left the call outside the concurrency cap.
    """
    tracker = _Tracker()
    engine = _engine(
        monkeypatch,
        tracker,
        backends=("brave",),
        hang=0.5,
        budget=0.02,
        max_search_concurrency=1,
    )
    run = asyncio.run(engine.research_run("q", limit=5, include_github=True))

    # Exactly one of the two proposed calls wins the single lease.
    assert run.scheduled_count == 2
    assert run.request_count == 1
    assert run.shed_count == 1
    assert run.scheduled_count == run.request_count + run.shed_count
    refused = [p for p in run.providers if p.error_kind == "skipped"]
    assert len(refused) == 1
    assert refused[0].error is not None
    assert refused[0].error.startswith("skipped: not issued")


def test_a_cached_run_reports_an_all_zero_fanout(tmp_path, monkeypatch):
    """A cache hit proposes nothing, so every fanout number is a real zero.

    The hydrator does not restore the block and scheduled_count defaults to None,
    so a cached envelope used to carry scheduled: null beside issued: 0 and no
    consumer could apply the documented identity.
    """
    tracker = _Tracker()
    monkeypatch.setattr(eng, "DDGS", _hanging_ddgs(tracker, 0.5, answer={"brave"}))
    monkeypatch.setattr(eng, "SEARCH_THREAD_TIMEOUT_HEADROOM", 0.0)
    monkeypatch.setattr(eng, "_engine_available", lambda category, backend: (True, None))
    engine = eng.Josty(backends=("brave",), timeout=0.5, cache_db=tmp_path / "c.db")

    first = asyncio.run(engine.search_run("q", limit=1))
    assert first.cached is False and first.request_count == 1

    second = asyncio.run(engine.search_run("q", limit=1))
    assert second.cached is True
    fanout = second.dict()["fanout"]
    assert fanout == {
        "scheduled": 0,
        "issued": 0,
        "shed": 0,
        "shed_by_reason": {},
        "ghosts_outstanding": 0,
        "ghosts_peak": 0,
    }


def test_a_later_run_is_not_shed_by_an_earlier_runs_ghost(monkeypatch):
    """The reproduced leak: HEAD's pool lived on the instance, so run 2 was shed.

    Measured on real HEAD code: capacity 1, a 1.5s hang, and run 2 was refused
    with "search executor saturated" until the ghost returned. A per-run pool with
    expiring leases means run 2 is unaffected and reports its own accounting.
    """
    tracker = _Tracker()
    engine = _engine(
        monkeypatch,
        tracker,
        backends=("brave",),
        hang=1.0,
        budget=0.05,
        max_search_concurrency=1,
    )

    run1 = asyncio.run(engine.search_run("run one", limit=1))
    assert run1.request_count == 1
    assert run1.ghosts_outstanding == 1

    # Run 2 starts while run 1's ghost is still blocked in its fake socket.
    run2 = asyncio.run(engine.search_run("run two", limit=1))
    assert run2.request_count == 1, "a ghost from the previous run shed this run"
    assert run2.shed_count == 0
    assert run2.ghosts_outstanding == 1


def test_run_timeout_must_be_positive():
    with pytest.raises(ValueError):
        eng.Josty(run_timeout=0, enable_cache=False)
    with pytest.raises(ValueError):
        eng.Josty(max_ghosts=-1, enable_cache=False)
