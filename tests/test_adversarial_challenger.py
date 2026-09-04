"""Adversarial resilience tests: 429 floods, timeouts, ghost threads, HALF_OPEN flapping."""

from __future__ import annotations

import asyncio
import time

import pytest
from ddgs.exceptions import RatelimitException, TimeoutException
from josty.engine import CircuitBreaker, Josty
from mock_ddgs import MockDDGSEngine, freeze_monotonic


@pytest.fixture(autouse=True)
def isolate_test_cache(tmp_path, monkeypatch):
    """Ensure every test executes with an isolated, clean SQLite cache directory."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


class TestScenario1RateLimitFloods:
    """Scenario 1: Floods of 429 rate limit errors across multiple engines simultaneously."""

    def test_429_flood_all_backends_trips_to_open(self, monkeypatch):
        """Simultaneous 429 flood on all backends trips circuits and transitions to cool-down."""
        breaker = CircuitBreaker(fail_threshold=3, cool_down_seconds=0.2)
        engine = Josty(backends=("brave", "duckduckgo", "google"), breaker=breaker)

        mock_ddgs = MockDDGSEngine({
            "brave": RatelimitException("429 Brave Rate Limit"),
            "duckduckgo": RatelimitException("429 DuckDuckGo Rate Limit"),
            "google": RatelimitException("429 Google Rate Limit"),
        })
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        # Execute 3 searches to trip all circuits (fail_threshold = 3)
        for i in range(3):
            run = asyncio.run(engine.search_run(f"flood query {i}", limit=5))
            assert run.status == "failed"
            assert len(run.results) == 0
            assert len(run.providers) == 3
            for p in run.providers:
                assert p.ok is False
                assert p.error_kind == "rate_limited"

        # Verify all circuits are now OPEN
        for backend in ("brave", "duckduckgo", "google"):
            state = breaker.get_state(backend)
            assert state["state"] == "open"
            assert state["failures"] == 3
            assert state["backoff_remaining"] > 0.0

        # Subsequent search should skip querying backends immediately
        calls_before = len(mock_ddgs.call_log)
        run_skipped = asyncio.run(engine.search_run("skipped query", limit=5))
        calls_after = len(mock_ddgs.call_log)

        # Zero new calls to DDGS
        assert calls_after == calls_before
        assert run_skipped.status == "failed"
        assert len(run_skipped.results) == 0
        for p in run_skipped.providers:
            assert p.ok is False
            assert p.error_kind == "skipped"
            assert "cool-down" in (p.error or "")

    def test_429_flood_surviving_engine_clean_fusion(self, monkeypatch):
        """When 2 of 3 backends throttle (429), surviving engine cleanly fuses without exception."""
        breaker = CircuitBreaker(fail_threshold=3, cool_down_seconds=0.5)
        engine = Josty(backends=("brave", "duckduckgo", "google"), breaker=breaker)

        mock_ddgs = MockDDGSEngine({
            "brave": RatelimitException("429 Throttled"),
            "duckduckgo": RatelimitException("429 Throttled"),
            "google": [
                {
                    "title": "Google Surviving Item 1",
                    "href": "https://example.org/surviving-1",
                    "body": "High quality content 1",
                    "source": "google",
                },
                {
                    "title": "Google Surviving Item 2",
                    "href": "https://example.org/surviving-2",
                    "body": "High quality content 2",
                    "source": "google",
                },
            ],
        })
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        run = asyncio.run(engine.search_run("adversarial query", limit=5))

        # Status must be degraded (partial failure), not failed
        assert run.status == "degraded"
        assert run.partial is True
        assert len(run.results) == 2

        # Verify results come exclusively from surviving engine
        for res in run.results:
            assert res.sources == ["google"]
            assert "google" in res.engine_ranks
            assert res.engine_ranks["google"] >= 1
            assert "google" in res.rank_contributions
            # Mathematical RRF check (strict equality)
            rank = res.engine_ranks["google"]
            k = res.score_weights.get("k", 60.0)
            expected_contrib = round(1.0 / (k + rank), 6)
            assert res.rank_contributions["google"] == expected_contrib
            domain_w = res.score_weights.get("domain_weight", 1.0)
            assert res.score == round(domain_w * expected_contrib, 6)

        # Verify provider telemetry
        provider_map = {p.provider: p for p in run.providers}
        assert provider_map["brave"].ok is False
        assert provider_map["brave"].error_kind == "rate_limited"
        assert provider_map["duckduckgo"].ok is False
        assert provider_map["duckduckgo"].error_kind == "rate_limited"
        assert provider_map["google"].ok is True
        assert provider_map["google"].result_count == 2
        assert provider_map["google"].error_kind is None

    def test_429_high_concurrency_stress(self, monkeypatch):
        """Stress-test 25 concurrent queries under intermittent 429s without deadlock."""
        call_count = 0

        def flaky_duckduckgo(query, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 1:
                raise RatelimitException("429 Flaky")
            return [
                {
                    "title": f"Duck Hit {call_count}",
                    "href": f"https://example.org/duck-{call_count}",
                    "body": "Duck body",
                    "source": "duckduckgo",
                }
            ]

        breaker = CircuitBreaker(fail_threshold=5, cool_down_seconds=0.5)
        engine = Josty(backends=("brave", "duckduckgo"), breaker=breaker)

        mock_ddgs = MockDDGSEngine({
            "duckduckgo": flaky_duckduckgo,
            "brave": [
                {
                    "title": "Brave Hit Steady",
                    "href": "https://example.org/brave-steady",
                    "body": "Brave body",
                    "source": "brave",
                }
            ],
        })
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        async def run_concurrent():
            tasks = [engine.search_run(f"stress test query {i}", limit=3) for i in range(25)]
            return await asyncio.gather(*tasks, return_exceptions=False)

        runs = asyncio.run(run_concurrent())
        assert len(runs) == 25
        for r in runs:
            # Brave always survived, so status is either complete or degraded, never failed
            assert r.status in ("complete", "degraded")
            assert len(r.results) >= 1
            # Check pure attribution invariant
            for item in r.results:
                assert item.score > 0.0
                for eng in item.sources:
                    assert eng in item.engine_ranks
                    assert eng in item.rank_contributions


class TestScenario2TimeoutsAndHangs:
    """Scenario 2: Sudden upstream timeouts and socket hangs; verify wait_for bounds execution."""

    def test_upstream_timeout_exception_handling(self, monkeypatch):
        """Standard ddgs TimeoutException is caught, classified as network, and degraded."""
        mock_ddgs = MockDDGSEngine({
            "duckduckgo": TimeoutException("Upstream connection timeout"),
            "brave": [
                {
                    "title": "Brave Item",
                    "href": "https://example.org/brave-item",
                    "body": "Brave body",
                    "source": "brave",
                }
            ],
        })
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        breaker = CircuitBreaker(fail_threshold=2)
        engine = Josty(backends=("duckduckgo", "brave"), breaker=breaker)

        run = asyncio.run(engine.search_run("timeout query", limit=5))

        assert run.status == "degraded"
        assert len(run.results) == 1
        assert run.results[0].sources == ["brave"]

        provider_map = {p.provider: p for p in run.providers}
        assert provider_map["duckduckgo"].ok is False
        assert provider_map["duckduckgo"].error_kind == "network"
        assert "TimeoutException" in (provider_map["duckduckgo"].error or "")
        assert provider_map["brave"].ok is True

    def test_coroutine_execution_bounded_by_wait_for(self, monkeypatch):
        """asyncio.wait_for bounds search to timeout + headroom, not the hang duration."""
        monkeypatch.setattr("josty.engine.SEARCH_THREAD_TIMEOUT_HEADROOM", 0.05)

        def hanging_backend(query, **kwargs):
            time.sleep(0.3)
            return []

        breaker = CircuitBreaker(fail_threshold=2, cool_down_seconds=1.0)
        engine = Josty(backends=("duckduckgo", "brave"), breaker=breaker, timeout=0.05)

        mock_ddgs = MockDDGSEngine({
            "duckduckgo": hanging_backend,
            "brave": [
                {
                    "title": "Brave Fast Item",
                    "href": "https://example.org/brave-fast",
                    "body": "Fast body",
                    "source": "brave",
                }
            ],
        })
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        async def run_coroutine():
            t0 = time.perf_counter()
            run = await engine.search_run("coroutine bound test", limit=5)
            elapsed = time.perf_counter() - t0
            return run, elapsed

        run, elapsed = asyncio.run(run_coroutine())

        assert elapsed < 0.25, f"Coroutine took {elapsed:.2f}s, expected < 0.25s"
        assert run.status == "degraded"
        assert len(run.results) == 1

        provider_map = {p.provider: p for p in run.providers}
        ddg_status = provider_map["duckduckgo"]
        assert ddg_status.ok is False
        assert ddg_status.error_kind == "network"
        assert "TimeoutError" in (ddg_status.error or "")

    def test_bug_ghost_thread_success_overwrites_circuit_breaker(self, monkeypatch):
        """Ghost threads must not call record_success after wait_for has timed out."""
        monkeypatch.setattr("josty.engine.SEARCH_THREAD_TIMEOUT_HEADROOM", 0.05)
        finished = []

        def slow_success(query, **kwargs):
            time.sleep(0.25)
            finished.append(True)
            return [{"title": "Slow Hit", "href": "https://example.org/slow", "body": "Slow body"}]

        cb = CircuitBreaker(fail_threshold=1, cool_down_seconds=10.0)
        engine = Josty(backends=("brave",), breaker=cb, timeout=0.05)

        mock_ddgs = MockDDGSEngine({"brave": slow_success})
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        async def verify_ghost_overwrite():
            run = await engine.search_run("ghost test", limit=1)
            assert run.status == "failed"
            state_immediate = cb.get_state("brave")
            assert state_immediate["state"] == "open"
            assert state_immediate["failures"] == 1
            assert state_immediate["backoff_remaining"] > 5.0
            await asyncio.sleep(0.3)
            return state_immediate, cb.get_state("brave")

        immediate, after = asyncio.run(verify_ghost_overwrite())
        assert finished == [True]
        assert immediate["state"] == "open"
        assert after["state"] == "open"
        assert after["failures"] == 1
        assert after["backoff_remaining"] > 0.0

    def test_search_returns_at_wait_for_boundary_with_dedicated_executor(self, monkeypatch):
        """Dedicated search executor is not joined by asyncio.run(), so search
        returns at the wait_for boundary instead of waiting for the ghost hang.
        """
        monkeypatch.setattr("josty.engine.SEARCH_THREAD_TIMEOUT_HEADROOM", 0.05)

        def hanging_thread(query, **kwargs):
            time.sleep(0.3)
            return []

        engine = Josty(backends=("brave",), timeout=0.05)
        mock_ddgs = MockDDGSEngine({"brave": hanging_thread})
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        t0 = time.perf_counter()
        run = asyncio.run(engine.search_run("process block test", limit=1))
        elapsed = time.perf_counter() - t0

        assert run.status == "failed"
        assert elapsed < 0.25, f"search_run blocked on ghost thread ({elapsed:.2f}s)"


class TestScenario3HalfOpenFlappingBackends:
    """Scenario 3: Half-open trial probe state transitions under flapping backends."""

    def test_half_open_success_recovers_to_closed(self, monkeypatch):
        """In half-open state, a successful trial probe resets circuit to CLOSED."""
        clock = freeze_monotonic(monkeypatch, 1000.0)
        cb = CircuitBreaker(fail_threshold=3, cool_down_seconds=0.1)

        for _ in range(3):
            cb.record_failure("flapping_engine", "search")
        assert cb.get_state("flapping_engine")["state"] == "open"

        clock[0] = 1000.2
        assert cb.get_state("flapping_engine")["state"] == "half-open"

        cb.record_success("flapping_engine", "search")

        state = cb.get_state("flapping_engine")
        assert state["state"] == "closed"
        assert state["failures"] == 0
        assert state["backoff_remaining"] == 0.0

    def test_half_open_trial_probe_failure_reopens(self, monkeypatch):
        """A failed HALF_OPEN trial probe trips back to OPEN immediately."""
        clock = freeze_monotonic(monkeypatch, 1000.0)
        cb = CircuitBreaker(fail_threshold=3, cool_down_seconds=0.08)

        for _ in range(3):
            cb.record_failure("engine_x", "search")
        assert cb.get_state("engine_x")["state"] == "open"

        clock[0] = 1000.2
        assert cb.get_state("engine_x")["state"] == "half-open"

        cb.record_failure("engine_x", "search")

        state_after_failed_probe = cb.get_state("engine_x")["state"]
        allowed, skip_msg = cb.status("engine_x", "search")

        assert state_after_failed_probe == "open"
        assert allowed is False
        assert skip_msg is not None
        assert "skipped: engine in cool-down until" in skip_msg
        assert cb.get_state("engine_x")["backoff_remaining"] > 0.08

    def test_multi_engine_flapping_continuous_agent_workload(self, monkeypatch):
        """Agent query workload with 1 healthy, 1 flapping (fail/success), 1 down engine."""
        flip_state = False

        def flapping_ddgs(query, **kwargs):
            nonlocal flip_state
            flip_state = not flip_state
            if flip_state:
                raise RatelimitException("429 Flapping Throttle")
            return [
                {
                    "title": f"Flapping DDG Hit for {query}",
                    "href": f"https://example.org/ddg-hit-{hash(query) % 1000}",
                    "body": "Snippet from DDG",
                    "source": "duckduckgo",
                }
            ]

        cb = CircuitBreaker(fail_threshold=2, cool_down_seconds=0.05)
        engine = Josty(backends=("brave", "duckduckgo", "google"), breaker=cb)

        mock_ddgs = MockDDGSEngine({
            "brave": [
                {
                    "title": "Brave Solid Anchor",
                    "href": "https://example.org/brave-anchor",
                    "body": "Anchor snippet",
                    "source": "brave",
                }
            ],
            "duckduckgo": flapping_ddgs,
            "google": RatelimitException("429 Google Hard Down"),
        })
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        # Run 8 sequential agent queries simulating continuous interaction
        for i in range(8):
            run = asyncio.run(engine.search_run(f"agent step query {i}", limit=5))

            # Surviving engine ('brave') always ensures non-empty clean results
            assert len(run.results) >= 1
            assert run.status in ("complete", "degraded")

            # Mathematical invariant on all results (strict equality)
            for item in run.results:
                dw = item.score_weights.get("domain_weight", 1.0)
                sum_contrib = sum(item.rank_contributions.values())
                expected_score = round(dw * sum_contrib, 6)
                assert item.score == expected_score

            # Check that google is failing / in cool-down / half-open
            g_state = cb.get_state("google")
            assert g_state["state"] in ("open", "half-open") or g_state["failures"] >= 1
