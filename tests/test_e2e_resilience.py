"""Comprehensive End-to-End Resilience & Fault-Tolerance Test Suite for Josty.

This test suite executes opaque-box tests against the public interfaces defined
in PROJECT.md § Interface Contracts and ORIGINAL_REQUEST.md:
  - Tier 1: Feature Coverage (Isolation)
  - Tier 2: Boundary & Corner Cases (Failure Simulation)
  - Tier 3: Cross-Feature Combinations (Interactions & Telemetry)
  - Tier 4: Real-World Scenarios (Workloads, Transient Outages, JSON Invariants)

All tests operate hermetically using isolated cache paths and mocked network
interactions to guarantee determinism in all execution environments.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
from josty.cli import main
from josty.engine import (
    SCHEMA_VERSION,
    CircuitBreaker,
    DiagnoseRun,
    Josty,
    SearchRun,
    _search_run_from_dict,
)


@pytest.fixture(autouse=True)
def isolate_test_cache(tmp_path, monkeypatch):
    """Ensure every test executes with an isolated, clean SQLite cache directory."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


class MockDDGSEngine:
    """Configurable mock for ddgs.DDGS supporting per-backend responses and exceptions."""

    def __init__(self, backend_responses: dict[str, Any] | None = None):
        self.responses: dict[str, Any] = backend_responses or {}
        self.call_log: list[dict[str, Any]] = []

    def __call__(self, **kwargs):
        return self

    def text(self, query: str, **kwargs):
        backend = kwargs.get("backend", "duckduckgo")
        self.call_log.append(
            {"method": "text", "query": query, "backend": backend, "kwargs": kwargs}
        )
        if backend in self.responses:
            resp = self.responses[backend]
            if isinstance(resp, BaseException):
                raise resp
            if isinstance(resp, type) and issubclass(resp, BaseException):
                raise resp(f"Simulated failure for {backend}")
            if callable(resp):
                return resp(query, **kwargs)
            return resp
        # Default mock response: 2 distinct items for this backend
        return [
            {
                "title": f"Result 1 from {backend}",
                "href": f"https://example.org/topic-{backend}-1",
                "body": f"Snippet 1 for {backend}",
                "date": "2026-09-01T12:00:00Z",
                "source": backend,
            },
            {
                "title": f"Common Topic from {backend}",
                "href": "https://example.org/common-consensus-item",
                "body": f"Consensus snippet from {backend}",
                "date": "2026-09-01T13:00:00Z",
                "source": backend,
            },
        ]

    def news(self, query: str, **kwargs):
        backend = kwargs.get("backend", "duckduckgo")
        self.call_log.append(
            {"method": "news", "query": query, "backend": backend, "kwargs": kwargs}
        )
        if backend in self.responses:
            resp = self.responses[backend]
            if isinstance(resp, BaseException):
                raise resp
            if isinstance(resp, type) and issubclass(resp, BaseException):
                raise resp(f"Simulated failure for {backend}")
            if callable(resp):
                return resp(query, **kwargs)
            return resp
        return [
            {
                "title": f"Breaking News from {backend}",
                "url": f"https://news.example.com/{backend}-headline",
                "body": f"News body from {backend}",
                "date": "2026-09-03T10:00:00Z",
                "source": backend,
            }
        ]


# ==============================================================================
# Tier 1: Feature Coverage (Isolation)
# ==============================================================================


class TestTier1FeatureCoverage:
    """Test all features in isolation against public interface specifications."""

    def test_cli_diagnose_contract_and_schema(self, capsys, monkeypatch):
        """CLI --diagnose must emit valid schema 1.0 JSON on stdout with HostStatus telemetry."""

        async def fake_get(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        monkeypatch.setattr("sys.argv", ["josty", "--diagnose"])

        main()
        captured = capsys.readouterr()

        # Pure JSON on stdout validation
        payload = json.loads(captured.out)
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["status"] in ("complete", "degraded", "failed")
        assert isinstance(payload["reachable"], int)
        assert isinstance(payload["count"], int)
        assert isinstance(payload["providers"], list)
        assert len(payload["providers"]) > 0

        # Validate HostStatus telemetry fields
        for provider in payload["providers"]:
            assert "provider" in provider
            assert "host" in provider
            assert "ok" in provider
            assert "http_status" in provider
            assert "error_kind" in provider
            assert "challenged" in provider
            assert "latency_ms" in provider
            assert "circuit_state" in provider
            assert "failures" in provider
            assert "backoff_remaining" in provider

            # Types and bounds
            if provider["latency_ms"] is not None:
                assert isinstance(provider["latency_ms"], (int, float))
                assert provider["latency_ms"] >= 0.0
            if provider["circuit_state"] is not None:
                assert provider["circuit_state"] in ("closed", "open", "half-open")
            if provider["failures"] is not None:
                assert isinstance(provider["failures"], int)
                assert provider["failures"] >= 0
            if provider["backoff_remaining"] is not None:
                assert isinstance(provider["backoff_remaining"], (int, float))
                assert provider["backoff_remaining"] >= 0.0

    def test_multi_engine_search_execution(self, monkeypatch):
        """Multi-engine search fans out across backends and records ProviderStatus telemetry."""
        mock_ddgs = MockDDGSEngine()
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        backends = ("brave", "duckduckgo", "mojeek")
        engine = Josty(backends=backends, timeout=2.0)
        run = asyncio.run(engine.search_run("test consensus query", limit=5))

        assert isinstance(run, SearchRun)
        assert run.status == "complete"
        assert run.partial is False
        assert len(run.results) > 0

        # Verify ProviderStatus entries for all queried backends
        provider_names = {p.provider for p in run.providers}
        for b in backends:
            assert b in provider_names

        for p in run.providers:
            assert p.ok is True
            assert p.result_count > 0
            assert p.error is None
            # Extended telemetry fields
            assert hasattr(p, "latency_ms")
            assert hasattr(p, "circuit_state")
            assert hasattr(p, "failures")
            assert hasattr(p, "backoff_remaining")
            if p.latency_ms is not None:
                assert p.latency_ms >= 0.0
            if p.circuit_state is not None:
                assert p.circuit_state in ("closed", "open", "half-open")

    def test_breaker_status_api_inspection(self):
        """Josty.breaker_status() and CircuitBreaker.get_state() return standard status schema."""
        backends = ("brave", "duckduckgo")
        engine = Josty(backends=backends)

        # Inspect all backends
        all_status = engine.breaker_status()
        assert isinstance(all_status, dict)
        for b in backends:
            assert b in all_status
            state_dict = all_status[b]
            assert "state" in state_dict
            assert state_dict["state"] in ("closed", "open", "half-open")
            assert "failures" in state_dict
            assert isinstance(state_dict["failures"], int)
            assert "backoff_remaining" in state_dict
            assert isinstance(state_dict["backoff_remaining"], (int, float))
            assert "last_latency_ms" in state_dict

        # Inspect specific backend
        brave_status = engine.breaker_status("brave")
        assert isinstance(brave_status, dict)
        assert brave_status["state"] in ("closed", "open", "half-open")
        assert isinstance(brave_status["failures"], int)
        assert brave_status["backoff_remaining"] >= 0.0

        # Direct CircuitBreaker.get_state() inspection
        cb_state = engine.breaker.get_state("brave")
        assert isinstance(cb_state, dict)
        assert cb_state["state"] == "closed"
        assert cb_state["failures"] == 0

    def test_search_result_attribution_telemetry(self, monkeypatch):
        """SearchResult contains engine_ranks, rank_contributions, and score_weights.

        Verifies the Cormack-Clarke RRF mathematical invariant holds across backends.
        """
        mock_ddgs = MockDDGSEngine(
            backend_responses={
                "brave": [
                    {
                        "title": "Brave Hit 1",
                        "href": "https://python.org/doc",
                        "body": "Brave snippet",
                    },
                    {
                        "title": "Brave Hit 2",
                        "href": "https://example.com/item-b",
                        "body": "Snippet B",
                    },
                ],
                "duckduckgo": [
                    {"title": "DDG Hit 1", "href": "https://python.org/doc", "body": "DDG snippet"},
                    {
                        "title": "DDG Hit 2",
                        "href": "https://example.com/item-d",
                        "body": "Snippet D",
                    },
                ],
            }
        )
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        engine = Josty(backends=("brave", "duckduckgo"), timeout=2.0)
        run = asyncio.run(engine.search_run("python documentation", limit=5))

        assert len(run.results) > 0
        consensus_item = next((r for r in run.results if "https://python.org/doc" in r.url), None)
        assert consensus_item is not None

        # Verify attribution fields
        assert hasattr(consensus_item, "engine_ranks")
        assert hasattr(consensus_item, "rank_contributions")
        assert hasattr(consensus_item, "score_weights")

        # Consensus item was ranked by both engines
        assert set(consensus_item.sources) == {"brave", "duckduckgo"}
        assert set(consensus_item.engine_ranks.keys()) == {"brave", "duckduckgo"}
        assert set(consensus_item.rank_contributions.keys()) == {"brave", "duckduckgo"}

        # Mathematical RRF invariant check (strict — the score must be
        # derivable from the recorded attribution alone):
        # rank_contribution[e] = round(1.0 / (k + rank[e]), 6)
        # score = round(domain_weight * sum(rank_contributions), 6)
        k = consensus_item.score_weights.get("k", 60.0)
        domain_weight = consensus_item.score_weights.get("domain_weight", 1.0)

        expected_sum = 0.0
        for eng, rank in consensus_item.engine_ranks.items():
            assert rank >= 1, f"Rank for engine {eng} must be >= 1"
            expected_contrib = round(1.0 / (k + rank), 6)
            assert consensus_item.rank_contributions[eng] == expected_contrib
            expected_sum += consensus_item.rank_contributions[eng]

        expected_score = round(domain_weight * expected_sum, 6)
        assert consensus_item.score == expected_score

    def test_stderr_diagnostic_routing(self, capsys, monkeypatch):
        """A loud backend failure stays in-band: stdout is exactly one pure JSON
        document and the failure is reported through providers, never as free
        text. (pytest emits warnings to stderr by default; nothing may leak to
        stdout instead.)
        """
        mock_ddgs = MockDDGSEngine(
            backend_responses={"brave": RatelimitException("429 Too Many Requests")}
        )
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)
        monkeypatch.setattr("sys.argv", ["josty", "resilience query"])

        main()
        captured = capsys.readouterr()

        # Stdout must be strictly valid JSON — json.loads rejects any stray
        # non-whitespace text before or after the document.
        payload = json.loads(captured.out)
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["status"] in ("complete", "degraded", "failed")

        # The backend failure is reported in-band (JSON providers), never on
        # stdout as diagnostics.
        brave = next(p for p in payload["providers"] if p["provider"] == "brave")
        assert brave["ok"] is False
        assert brave["error_kind"] == "rate_limited"


# ==============================================================================
# Tier 2: Boundary & Corner Cases (Failure Simulation)
# ==============================================================================


class TestTier2BoundaryAndCornerCases:
    """Test engine behavior under simulated upstream failures and boundary inputs."""

    def test_failure_simulation_429_rate_limit(self, monkeypatch):
        """Simulating HTTP 429 isolates failing backend; surviving backends fuse cleanly."""
        mock_ddgs = MockDDGSEngine(
            backend_responses={
                "brave": RatelimitException("429 Rate limited by upstream server"),
                "duckduckgo": [
                    {
                        "title": "Healthy DDG Hit",
                        "href": "https://survivor.org/ddg",
                        "body": "DDG body",
                    },
                ],
                "mojeek": [
                    {
                        "title": "Healthy Mojeek Hit",
                        "href": "https://survivor.org/mojeek",
                        "body": "Mojeek body",
                    },
                ],
            }
        )
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        engine = Josty(backends=("brave", "duckduckgo", "mojeek"), timeout=2.0)
        run = asyncio.run(engine.search_run("distributed systems", limit=5))

        # Query degrades gracefully rather than raising unhandled exception
        assert run.status == "degraded"
        assert run.partial is True
        assert len(run.results) == 2

        # Verify failing backend provider status
        brave_provider = next(p for p in run.providers if p.provider == "brave")
        assert brave_provider.ok is False
        assert brave_provider.error_kind == "rate_limited"
        assert brave_provider.failures is not None and brave_provider.failures >= 1

        # Verify healthy backends succeeded
        ddg_provider = next(p for p in run.providers if p.provider == "duckduckgo")
        assert ddg_provider.ok is True
        assert ddg_provider.result_count == 1

        # Verify results contain only surviving backends in attribution
        for res in run.results:
            assert "brave" not in res.sources
            assert "brave" not in res.engine_ranks

    def test_failure_simulation_403_challenge(self, monkeypatch):
        """Simulating HTTP 403 bot challenge isolates backend without crashing the query."""
        mock_ddgs = MockDDGSEngine(
            backend_responses={
                "brave": DDGSException("HTTP 403 Forbidden - Cloudflare challenge required"),
                "duckduckgo": [
                    {"title": "DDG Result", "href": "https://example.com/ok", "body": "Valid hit"},
                ],
            }
        )
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        engine = Josty(backends=("brave", "duckduckgo"), timeout=2.0)
        run = asyncio.run(engine.search_run("challenge test", limit=5))

        assert run.status == "degraded"
        assert run.partial is True
        assert len(run.results) == 1

        brave_status = next(p for p in run.providers if p.provider == "brave")
        assert brave_status.ok is False

    def test_failure_simulation_timeout_protection(self, monkeypatch):
        """Simulating read/connect timeout completes promptly via concurrency hang protection."""
        mock_ddgs = MockDDGSEngine(
            backend_responses={
                "brave": TimeoutException("Socket timed out after 2.0s"),
                "duckduckgo": [
                    {"title": "Fast Result", "href": "https://fast.org/item", "body": "Fast body"},
                ],
            }
        )
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        engine = Josty(backends=("brave", "duckduckgo"), timeout=1.0)
        start_time = time.perf_counter()
        run = asyncio.run(engine.search_run("timeout test", limit=5))
        elapsed = time.perf_counter() - start_time

        # Bounded execution: should complete within small window
        assert elapsed < 3.5
        assert run.status == "degraded"
        brave_status = next(p for p in run.providers if p.provider == "brave")
        assert brave_status.ok is False
        assert brave_status.error_kind in ("network", "timeout")

    def test_failure_simulation_connection_drop(self, monkeypatch):
        """Simulating abrupt network connection drop is caught and gracefully handled."""
        mock_ddgs = MockDDGSEngine(
            backend_responses={
                "brave": httpx.ConnectError(
                    "Connection refused by peer", request=httpx.Request("GET", "https://api")
                ),
                "duckduckgo": [
                    {
                        "title": "Surviving Hit",
                        "href": "https://surviving.org/ok",
                        "body": "Ok snippet",
                    },
                ],
            }
        )
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        engine = Josty(backends=("brave", "duckduckgo"), timeout=2.0)
        run = asyncio.run(engine.search_run("drop test", limit=5))

        assert run.status == "degraded"
        brave_provider = next(p for p in run.providers if p.provider == "brave")
        assert brave_provider.ok is False
        assert brave_provider.error_kind == "network"

    def test_all_backends_failing_boundary(self, monkeypatch):
        """Total outage across all search backends returns failed SearchRun without crashing."""
        mock_ddgs = MockDDGSEngine(
            backend_responses={
                "brave": RatelimitException("429 rate limit"),
                "duckduckgo": TimeoutException("DDG timed out"),
            }
        )
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        engine = Josty(backends=("brave", "duckduckgo"), timeout=2.0)
        run = asyncio.run(engine.search_run("total outage test", limit=5))

        assert run.status == "failed"
        assert run.partial is True
        assert len(run.results) == 0
        for p in run.providers:
            assert p.ok is False

    def test_empty_and_whitespace_queries(self):
        """Empty, whitespace, and control character queries are safely validated."""
        engine = Josty()
        for empty_q in ("", "   ", "\t\n\r"):
            # Either raises ValueError or returns clean empty run without crashing
            try:
                run = asyncio.run(engine.search_run(empty_q, limit=5))
                assert run.status in ("complete", "failed")
                assert len(run.results) == 0
            except ValueError:
                pass  # Graceful input rejection is compliant

    def test_single_vs_multi_engine_attribution_boundary(self, monkeypatch):
        """Multi-engine consensus produces higher RRF scores and richer attribution."""
        mock_ddgs = MockDDGSEngine(
            backend_responses={
                "brave": [
                    {
                        "title": "Consensus Doc",
                        "href": "https://example.com/consensus",
                        "body": "Snippet 1",
                    },
                ],
                "duckduckgo": [
                    {
                        "title": "Consensus Doc",
                        "href": "https://example.com/consensus",
                        "body": "Snippet 2",
                    },
                ],
            }
        )
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        # Single engine search
        engine_single = Josty(backends=("brave",), timeout=2.0)
        run_single = asyncio.run(engine_single.search_run("consensus item", limit=5))
        assert len(run_single.results) == 1
        res_single = run_single.results[0]
        assert len(res_single.sources) == 1
        assert len(res_single.engine_ranks) == 1

        # Multi engine search on same consensus item
        engine_multi = Josty(backends=("brave", "duckduckgo"), timeout=2.0)
        run_multi = asyncio.run(engine_multi.search_run("consensus item", limit=5))
        assert len(run_multi.results) == 1
        res_multi = run_multi.results[0]
        assert len(res_multi.sources) == 2
        assert len(res_multi.engine_ranks) == 2

        # Multi-engine consensus strictly outscores single-engine
        assert res_multi.score > res_single.score


# ==============================================================================
# Tier 3: Cross-Feature Combinations
# ==============================================================================


class TestTier3CrossFeatureCombinations:
    """Test cross-feature interactions: breakers, RRF attribution, cache, and telemetry."""

    def test_circuit_breaker_trip_with_rrf_attribution(self, monkeypatch):
        """Tripped circuit breaker skips backend and preserves pure surviving RRF attribution."""
        breaker = CircuitBreaker(fail_threshold=3, cool_down_seconds=30.0)
        engine = Josty(backends=("brave", "duckduckgo"), breaker=breaker)

        # Trip breaker for brave by recording 3 consecutive failures
        for _ in range(3):
            breaker.record_failure("brave", "search")

        state = breaker.get_state("brave")
        assert state["state"] == "open"
        assert state["failures"] >= 3
        assert state["backoff_remaining"] > 0.0

        # Run search: brave must be skipped via breaker, duckduckgo must succeed
        mock_ddgs = MockDDGSEngine(
            backend_responses={
                "duckduckgo": [
                    {
                        "title": "DDG Exclusive",
                        "href": "https://ddg.org/exclusive",
                        "body": "DDG snippet",
                    },
                ]
            }
        )
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        run = asyncio.run(engine.search_run("breaker rrf test", limit=5))

        # Check provider statuses
        brave_provider = next(p for p in run.providers if p.provider == "brave")
        assert brave_provider.ok is False
        assert brave_provider.error_kind == "skipped"
        assert brave_provider.circuit_state == "open"

        ddg_provider = next(p for p in run.providers if p.provider == "duckduckgo")
        assert ddg_provider.ok is True

        # Check attribution: only duckduckgo appears in attribution
        assert len(run.results) == 1
        res = run.results[0]
        assert res.sources == ["duckduckgo"]
        assert list(res.engine_ranks.keys()) == ["duckduckgo"]
        assert list(res.rank_contributions.keys()) == ["duckduckgo"]

    def test_diagnose_during_open_circuit_breaker(self, monkeypatch):
        """CLI --diagnose surfaces open circuit breaker state, trip metrics, and backoff timer."""
        breaker = CircuitBreaker(fail_threshold=2, cool_down_seconds=45.0)
        breaker.record_failure("brave", "probe")
        breaker.record_failure("brave", "probe")

        state = breaker.get_state("brave")
        assert state["state"] == "open"

        engine = Josty(backends=("brave", "duckduckgo"), breaker=breaker)

        async def fake_get(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        diag = asyncio.run(engine.diagnose_run())

        assert isinstance(diag, DiagnoseRun)
        brave_host = next((p for p in diag.providers if p.provider == "brave"), None)
        assert brave_host is not None
        assert brave_host.circuit_state == "open"
        assert brave_host.failures is not None and brave_host.failures >= 2
        assert brave_host.backoff_remaining is not None and brave_host.backoff_remaining > 0.0

    def test_cache_roundtrip_attribution_preservation(self, monkeypatch):
        """Attribution fields survive SQLite SERP cache roundtrips with 100% fidelity."""
        mock_ddgs = MockDDGSEngine(
            backend_responses={
                "brave": [
                    {"title": "Doc 1", "href": "https://cache.org/doc", "body": "Snippet 1"},
                ],
                "duckduckgo": [
                    {"title": "Doc 1", "href": "https://cache.org/doc", "body": "Snippet 2"},
                ],
            }
        )
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        engine = Josty(backends=("brave", "duckduckgo"))
        query = "cache attribution roundtrip test"

        # 1. Fresh search
        run_fresh = asyncio.run(engine.search_run(query, limit=5))
        assert run_fresh.cached is False
        res_fresh = run_fresh.results[0]
        assert set(res_fresh.engine_ranks.keys()) == {"brave", "duckduckgo"}

        # 2. Subsequent search hits cache
        run_cached = asyncio.run(engine.search_run(query, limit=5))
        assert run_cached.cached is True
        res_cached = run_cached.results[0]

        # Invariant: cached attribution matches fresh attribution perfectly
        assert res_cached.engine_ranks == res_fresh.engine_ranks
        assert res_cached.rank_contributions == res_fresh.rank_contributions
        assert res_cached.score_weights == res_fresh.score_weights
        assert res_cached.score == res_fresh.score
        assert res_cached.sources == res_fresh.sources

    def test_cache_backward_compatibility_legacy_payload(self):
        """Legacy cached JSON payloads lacking new attribution fields deserialize without error."""
        legacy_dict = {
            "schema_version": "1.0",
            "query": "legacy query",
            "category": "text",
            "timelimit": None,
            "region": None,
            "safesearch": "moderate",
            "profile": "general",
            "status": "complete",
            "partial": False,
            "cached": True,
            "results": [
                {
                    "title": "Legacy Hit",
                    "url": "https://legacy.org/item",
                    "snippet": "Legacy snippet",
                    "sources": ["duckduckgo"],
                    "score": 0.016393,
                }
            ],
            "providers": [
                {
                    "provider": "duckduckgo",
                    "query": "legacy query",
                    "ok": True,
                    "result_count": 1,
                }
            ],
        }

        reconstructed = _search_run_from_dict(legacy_dict)
        assert isinstance(reconstructed, SearchRun)
        assert len(reconstructed.results) == 1
        item = reconstructed.results[0]
        assert item.title == "Legacy Hit"
        assert item.url == "https://legacy.org/item"
        # Backward-compatible defaults
        assert isinstance(item.engine_ranks, dict)
        assert isinstance(item.rank_contributions, dict)
        assert isinstance(item.score_weights, dict)

    def test_graceful_degradation_under_partial_outage(self, monkeypatch):
        """Simultaneous 429, timeout, and connect error leave healthy backend to deliver results."""
        mock_ddgs = MockDDGSEngine(
            backend_responses={
                "brave": RatelimitException("429 rate limit"),
                "duckduckgo": TimeoutException("Request timeout"),
                "mojeek": httpx.ConnectError(
                    "Connection refused", request=httpx.Request("GET", "https://m")
                ),
                "yahoo": [
                    {
                        "title": "Yahoo Survivor 1",
                        "href": "https://yahoo.org/1",
                        "body": "Snippet 1",
                    },
                    {
                        "title": "Yahoo Survivor 2",
                        "href": "https://yahoo.org/2",
                        "body": "Snippet 2",
                    },
                ],
            }
        )
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        engine = Josty(backends=("brave", "duckduckgo", "mojeek", "yahoo"), timeout=1.5)
        run = asyncio.run(engine.search_run("severe degradation test", limit=5))

        assert run.status == "degraded"
        assert run.partial is True
        assert len(run.results) == 2

        # Verify each provider classification
        providers_by_name = {p.provider: p for p in run.providers}
        assert providers_by_name["brave"].error_kind == "rate_limited"
        assert providers_by_name["duckduckgo"].error_kind in ("network", "timeout")
        assert providers_by_name["mojeek"].error_kind == "network"
        assert providers_by_name["yahoo"].ok is True

        # Results only attribute yahoo
        for r in run.results:
            assert r.sources == ["yahoo"]
            assert list(r.engine_ranks.keys()) == ["yahoo"]


# ==============================================================================
# Tier 4: Real-World Scenarios
# ==============================================================================


class TestTier4RealWorldScenarios:
    """End-to-end multi-query workloads, automatic recovery cycles, and pure JSON invariants."""

    def test_sequential_multi_query_agent_workload(self, monkeypatch):
        """Simulate realistic AI agent session issuing sequential queries with cache reuse."""
        mock_ddgs = MockDDGSEngine()
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        engine = Josty(backends=("brave", "duckduckgo"), timeout=2.0)

        # Query 1: Broad discovery
        run1 = asyncio.run(engine.search_run("distributed vector clocks", limit=5))
        assert run1.status == "complete"
        assert run1.cached is False

        # Query 2: Identical repeated query (agent re-check) -> Cache hit
        run2 = asyncio.run(engine.search_run("distributed vector clocks", limit=5))
        assert run2.status == "complete"
        assert run2.cached is True
        assert len(run2.results) == len(run1.results)

        # Query 3: Specific phrase search
        run3 = asyncio.run(engine.search_run("Lamport timestamps causality", mode="exact", limit=3))
        assert run3.status == "complete"
        assert run3.cached is False

        # Query 4: Site-scoped search
        run4 = asyncio.run(
            engine.search_run("consensus algorithms", sites=("example.org",), limit=5)
        )
        assert run4.status == "complete"
        for r in run4.results:
            assert "example.org" in r.url

    def test_transient_outage_automatic_recovery(self, monkeypatch):
        """Full lifecycle: closed -> open trip -> cool-down expiry -> half-open -> closed."""
        breaker = CircuitBreaker(fail_threshold=2, cool_down_seconds=0.1)
        engine = Josty(backends=("brave", "duckduckgo"), breaker=breaker)

        # Step 1: Normal healthy state
        assert breaker.get_state("brave")["state"] == "closed"

        # Step 2: Simulate 2 failures to trip circuit
        breaker.record_failure("brave", "search")
        breaker.record_failure("brave", "search")
        assert breaker.get_state("brave")["state"] == "open"

        # Step 3: During trip, calls are blocked
        allowed, msg = breaker.status("brave", "search")
        assert allowed is False
        assert "cool-down" in (msg or "")

        # Step 4: Advance time past cool-down period
        time.sleep(0.15)

        # Step 5: Breaker enters half-open trial probe state
        state_after_sleep = breaker.get_state("brave")
        assert state_after_sleep["state"] in ("half-open", "closed")
        allowed_trial, _ = breaker.status("brave", "search")
        assert allowed_trial is True

        # Step 6: Backend succeeds on trial probe
        breaker.record_success("brave", "search")

        # Step 7: Breaker recovers to fully closed
        final_state = breaker.get_state("brave")
        assert final_state["state"] == "closed"
        assert final_state["failures"] == 0

        # Step 8: Verify search execution with recovered engine
        mock_ddgs = MockDDGSEngine()
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)
        run = asyncio.run(engine.search_run("recovered query", limit=5))
        assert run.status == "complete"

    def test_pure_json_stdout_schema_1_end_to_end(self, capsys, monkeypatch):
        """CLI emits strictly 100% parseable JSON schema 1.0 across diverse execution pathways."""
        mock_ddgs = MockDDGSEngine()
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        commands = [
            ["josty", "json invariant query"],
            ["josty", "json invariant exact", "--mode", "exact"],
            ["josty", "--diagnose"],
            ["josty", "--cache-stats"],
            ["josty", "--clear-cache"],
        ]

        async def fake_get(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        for cmd in commands:
            monkeypatch.setattr("sys.argv", cmd)
            main()
            captured = capsys.readouterr()

            # Stdout must parse as valid JSON
            out_str = captured.out.strip()
            assert out_str.startswith("{") and out_str.endswith("}"), (
                f"Non-JSON output on command {cmd}: {out_str}"
            )
            data = json.loads(out_str)
            assert isinstance(data, dict)
            # Search runs and diagnose runs must strictly adhere to schema 1.0
            if "--cache-stats" not in cmd and "--clear-cache" not in cmd:
                assert data.get("schema_version") == SCHEMA_VERSION

    def test_default_multi_engine_group_attribution_invariant(self, monkeypatch):
        """Verify RRF attribution on default backend groups without 2x discrepancy.

        Strict equality on EVERY result — not just the consensus item — so a
        position-vs-rank drift or a silently dropped engine contribution fails
        loudly. Items whose merged-list position differs from their engine rank
        (brave-only, ddg-only) are exactly where the old implementation lied.
        """
        backend_responses = {
            "brave": [
                {
                    "title": "Brave Hit 1",
                    "href": "https://example.com/shared",
                    "body": "Brave snippet",
                },
                {
                    "title": "Brave Hit 2",
                    "href": "https://example.com/brave-only",
                    "body": "Brave snippet 2",
                },
            ],
            "duckduckgo": [
                {
                    "title": "DDG Hit 1",
                    "href": "https://example.com/shared",
                    "body": "DDG snippet",
                },
                {
                    "title": "DDG Hit 2",
                    "href": "https://example.com/ddg-only",
                    "body": "DDG snippet 2",
                },
            ],
            "google": [
                {
                    "title": "Google Hit 1",
                    "href": "https://example.com/shared",
                    "body": "Google snippet",
                },
            ],
        }
        mock_ddgs = MockDDGSEngine(backend_responses)
        monkeypatch.setattr("josty.engine.DDGS", mock_ddgs)

        engine = Josty(backends=("brave,duckduckgo", "google"))
        run = asyncio.run(engine.search_run("group invariant test", limit=5))
        assert len(run.results) >= 3

        shared_item = next(r for r in run.results if r.url == "https://example.com/shared")
        assert "brave" in shared_item.sources
        assert "duckduckgo" in shared_item.sources
        assert "google" in shared_item.sources
        # Every engine that discovered the item contributes — none dropped.
        assert set(shared_item.rank_contributions) == set(shared_item.engine_ranks)

        for item in run.results:
            k = item.score_weights.get("k", 60.0)
            dw = item.score_weights.get("domain_weight", 1.0)
            # Contribution per engine derives from that engine's discovery rank.
            for eng, rank in item.engine_ranks.items():
                assert item.rank_contributions[eng] == round(1.0 / (k + rank), 6), (
                    f"{item.url}: contribution for {eng} is {item.rank_contributions[eng]}, "
                    f"expected round(1/(k+{rank}), 6)"
                )
            # Score derives from the recorded contributions — strict, no tolerance.
            expected_score = round(dw * sum(item.rank_contributions.values()), 6)
            assert item.score == expected_score, (
                f"{item.url}: score {item.score} != round(dw*sum(contribs), 6)={expected_score}"
            )

        # Position-vs-rank trap: the ddg-only item sits at merged position 3 but
        # engine rank 2 — its contribution must come from the RANK.
        ddg_only = next(r for r in run.results if r.url == "https://example.com/ddg-only")
        assert ddg_only.engine_ranks == {"duckduckgo": 2}
        assert ddg_only.rank_contributions == {"duckduckgo": round(1.0 / 62, 6)}
