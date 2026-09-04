# Josty Test Infrastructure & Resilience Framework

## 1. Overview & Testing Philosophy

Josty is an auditable, zero-config, keyless metasearch engine designed for autonomous AI agent workflows. The test infrastructure guarantees high reliability, deterministic execution, and strict preservation of core architectural invariants:

1. **Opaque-Box Public Contract Verification**: Tests execute against external interfaces and schemas specified in `PROJECT.md § Interface Contracts` and `AGENTS.md`, never private or brittle implementation details.
2. **Deterministic Hermeticity**: Tests do not depend on external network connectivity or third-party search engine availability. All upstream engine responses and fault conditions are simulated deterministically. Cache persistence is strictly isolated per test using temporary `XDG_CACHE_HOME` environments.
3. **Core Architectural Invariants**:
   - **Pure JSON Stdout Contract**: `stdout` strictly emits valid, parseable JSON conforming to `schema_version: "1.0"` across search, diagnose, and error states.
   - **Stderr Diagnostic Routing**: All warnings, network diagnostics, rate limit alerts, and internal notes route exclusively to `stderr`.
   - **Zero-Daemon & Keyless Operation**: The test suite validates that no background daemons, servers, or persistent credentials are required.
   - **Mathematical Attribution Invariant**: Reciprocal rank fusion scores must exactly match the Cormack-Clarke mathematical formula across all surviving source backends.

---

## 2. Test Architecture & Tier Structure

The end-to-end resilience suite in `tests/test_e2e_resilience.py` is structured into four distinct verification tiers:

```
tests/
├── test_e2e_resilience.py       # Comprehensive 4-Tier E2E resilience suite
│   ├── TestTier1FeatureCoverage          # Tier 1: Feature isolation & contracts
│   ├── TestTier2BoundaryAndCornerCases   # Tier 2: Upstream failure simulation
│   ├── TestTier3CrossFeatureCombinations # Tier 3: Circuit breaker + RRF + cache
│   └── TestTier4RealWorldScenarios       # Tier 4: Multi-query workflows & recovery
├── test_engine.py               # Unit tests for core engine components
├── test_cli.py                  # CLI argument parsing & command dispatch
├── test_edge_cases.py           # 189 edge-case assertions across parsing/SSRF/modes
├── test_launcher.py             # Thin delegator runner tests
└── test_scenario_eval.py        # Scenario evaluation harness & corpus tests
```

### Tier 1: Feature Coverage (Isolation)
Tests each newly introduced feature in isolation:
- **`test_cli_diagnose_contract_and_schema`**: Validates CLI `--diagnose` JSON output schema, `HostStatus` fields (`latency_ms`, `circuit_state`, `failures`, `backoff_remaining`).
- **`test_multi_engine_search_execution`**: Validates asynchronous fanout across configured search backends with granular `ProviderStatus` telemetry.
- **`test_breaker_status_api_inspection`**: Validates programmatic inspection via `josty.breaker_status()` and `CircuitBreaker.get_state()`.
- **`test_search_result_attribution_telemetry`**: Validates presence of `engine_ranks`, `rank_contributions`, and `score_weights` on `SearchResult`.
- **`test_stderr_diagnostic_routing`**: Validates that errors and diagnostics route to `stderr`, leaving `stdout` as pure JSON.

### Tier 2: Boundary & Corner Cases (Failure Simulation)
Simulates upstream faults and adversarial conditions:
- **`test_failure_simulation_429_rate_limit`**: Simulates HTTP 429 throttling on individual backends; validates circuit breaker trip and graceful result fusion from survivors.
- **`test_failure_simulation_403_challenge`**: Simulates HTTP 403 / bot detection challenge; validates fault classification and non-crashing degradation.
- **`test_failure_simulation_timeout_protection`**: Simulates hanging read/connect operations; validates bounded event-loop timeout protection.
- **`test_failure_simulation_connection_drop`**: Simulates abrupt network drops / connection refused; validates error capture.
- **`test_all_backends_failing_boundary`**: Simulates complete backend outage; validates that engine returns `status: "failed"` with `results: []` without unhandled exceptions.
- **`test_empty_and_whitespace_queries`**: Validates empty, whitespace, and control character input validation.
- **`test_single_vs_multi_engine_attribution_boundary`**: Compares single-engine vs multi-engine runs, proving that multi-source consensus yields higher scores and richer attribution.

### Tier 3: Cross-Feature Combinations (Interactions & Telemetry)
Evaluates combinatorial feature interactions:
- **`test_circuit_breaker_trip_with_rrf_attribution`**: Verifies that when a breaker trips to `OPEN`, subsequent searches skip the engine immediately and surviving backends form pure, verifiable RRF attribution.
- **`test_diagnose_during_open_circuit_breaker`**: Verifies that `--diagnose` accurately surfaces `circuit_state: "open"`, failure count, and remaining backoff seconds.
- **`test_cache_roundtrip_attribution_preservation`**: Verifies that attribution telemetry (`engine_ranks`, `rank_contributions`, `score_weights`) survives SQLite cache roundtrips with 100% fidelity.
- **`test_cache_backward_compatibility_legacy_payload`**: Verifies that legacy cache payloads without attribution fields deserialize cleanly with backward-compatible defaults.
- **`test_graceful_degradation_under_partial_outage`**: Tests concurrent heterogeneous failures (429 + timeout + connect drop) where the lone healthy backend delivers results.

### Tier 4: Real-World Scenarios (Workloads & Invariant Stress)
Simulates real agent execution environments:
- **`test_sequential_multi_query_agent_workload`**: Exercises a multi-query pipeline (discovery -> cache hit -> exact phrase -> domain filter) across a single session.
- **`test_transient_outage_automatic_recovery`**: Executes the full circuit breaker lifecycle: `CLOSED` -> failure trip to `OPEN` -> cool-down expiry -> `HALF_OPEN` trial probe -> successful recovery to `CLOSED`.
- **`test_pure_json_stdout_schema_1_end_to_end`**: Rigorously verifies that all CLI commands emit parseable JSON conforming to `schema_version: "1.0"` with zero stdout contamination.

---

## 3. Failure Injection & Mocking Strategy

To test resilience without relying on flaky internet connections or violating search engine Terms of Service, the test suite utilizes `MockDDGSEngine`:

```python
class MockDDGSEngine:
    def __init__(self, backend_responses: dict[str, Any] | None = None):
        self.responses = backend_responses or {}
        self.call_log = []

    def text(self, query: str, **kwargs):
        backend = kwargs.get("backend", "duckduckgo")
        self.call_log.append({"backend": backend, "query": query})
        if backend in self.responses:
            resp = self.responses[backend]
            if isinstance(resp, BaseException):
                raise resp
            return resp
        return default_mock_results(backend)
```

Fault modes simulated:
- `RatelimitException("429 Too Many Requests")` -> Triggers `error_kind: "rate_limited"` and circuit breaker recording.
- `DDGSException("HTTP 403 Forbidden")` -> Triggers `error_kind: "rate_limited"` / `error_kind: "parse"`.
- `TimeoutException("Timed out")` -> Triggers `error_kind: "network"`.
- `httpx.ConnectError("Connection refused")` -> Triggers `error_kind: "network"`.

---

## 4. Mathematical Verification of RRF Attribution

The Cormack-Clarke Reciprocal Rank Fusion formula implemented in Josty is verified by tests against the following mathematical invariant:

$$\text{score} = \text{round}\left( \text{domain\_weight} \times \sum_{e \in \text{sources}} \text{rank\_contributions}[e], 6 \right)$$

where:
$$\text{rank\_contributions}[e] = \text{round}\left( \frac{1.0}{k + \text{engine\_ranks}[e]}, 6 \right)$$

Every test in `tests/test_e2e_resilience.py` that verifies search results evaluates:
1. `set(result.sources) == set(result.engine_ranks.keys()) == set(result.rank_contributions.keys())`
2. Every discovery rank is 1-indexed ($r \ge 1$).
3. The computed score matches the sum of rank contributions multiplied by domain weight within `1e-6` precision.

---

## 5. Coverage & Test Execution Commands

### Running E2E Resilience Tests:
```bash
.venv/bin/pytest tests/test_e2e_resilience.py -v
```

### Running Full Test Suite:
```bash
.venv/bin/pytest tests/ -q
```

### Running Scenario Evaluation Benchmark:
```bash
.venv/bin/python3 tests/scenario_eval.py
```

### Checking CLI Schema & Stderr Routing:
```bash
.venv/bin/python3 -m josty.cli --diagnose
```
