# TEST_READY: Josty E2E Resilience & Fault-Tolerance Test Suite

## 1. Test Suite Summary & Readiness

The End-to-End Resilience & Fault-Tolerance test suite has been designed, implemented, and verified in `tests/test_e2e_resilience.py`. It provides 20 opaque-box test cases across 4 tiers testing against public interfaces in `PROJECT.md § Interface Contracts` and `ORIGINAL_REQUEST.md`.

- **Test Suite Path**: `tests/test_e2e_resilience.py`
- **Test Infrastructure Document**: `TEST_INFRA.md`
- **Test Framework**: `pytest` (using project virtualenv `.venv/bin/pytest`)
- **Execution Target**: Deterministic, hermetic execution with zero real network dependencies and isolated SQLite cache per test.

---

## 2. Test Execution Commands

### Primary E2E Test Suite Runner
```bash
.venv/bin/pytest tests/test_e2e_resilience.py -q
```

### Verbose Execution
```bash
.venv/bin/pytest tests/test_e2e_resilience.py -v
```

### Full Repository Test Suite
```bash
.venv/bin/pytest tests/ -q
```

---

## 3. Test Coverage Matrix across Tiers

| Tier | Test Function | Target Interface / Requirement | Status Under Pre-Core Baseline | Target Under Core Milestone |
|:---|:---|:---|:---:|:---:|
| **Tier 1** | `test_cli_diagnose_contract_and_schema` | CLI `--diagnose`, pure JSON stdout, `HostStatus` telemetry (`latency_ms`, `circuit_state`, `failures`, `backoff_remaining`) | Fails (missing HostStatus telemetry) | PASS |
| **Tier 1** | `test_multi_engine_search_execution` | Multi-engine async fanout, `ProviderStatus` telemetry | Fails (missing ProviderStatus telemetry) | PASS |
| **Tier 1** | `test_breaker_status_api_inspection` | `Josty.breaker_status()`, `CircuitBreaker.get_state()` public schema | Fails (missing breaker API) | PASS |
| **Tier 1** | `test_search_result_attribution_telemetry` | `SearchResult` attribution fields (`engine_ranks`, `rank_contributions`, `score_weights`), RRF mathematical invariant | Fails (missing attribution fields) | PASS |
| **Tier 1** | `test_stderr_diagnostic_routing` | Strict diagnostic/warning routing to stderr, pure JSON on stdout | PASS | PASS |
| **Tier 2** | `test_failure_simulation_429_rate_limit` | Upstream HTTP 429 rate limit isolation, healthy backend fusion | Fails (missing attribution check on result) | PASS |
| **Tier 2** | `test_failure_simulation_403_challenge` | Upstream HTTP 403 / bot challenge handling without query crash | PASS | PASS |
| **Tier 2** | `test_failure_simulation_timeout_protection` | Concurrency hang protection on hanging backends | PASS | PASS |
| **Tier 2** | `test_failure_simulation_connection_drop` | Abrupt network connection refusal/drop handling | PASS | PASS |
| **Tier 2** | `test_all_backends_failing_boundary` | Total outage boundary (all backends failing) returning graceful failed run | PASS | PASS |
| **Tier 2** | `test_empty_and_whitespace_queries` | Query boundary validation for empty and whitespace inputs | PASS | PASS |
| **Tier 2** | `test_single_vs_multi_engine_attribution_boundary` | Single vs multi-engine RRF score boost and attribution richness | Fails (missing attribution fields) | PASS |
| **Tier 3** | `test_circuit_breaker_trip_with_rrf_attribution` | Tripped circuit breaker skipping backend, pure surviving RRF attribution | Fails (missing get_state & attribution) | PASS |
| **Tier 3** | `test_diagnose_during_open_circuit_breaker` | `--diagnose` surfacing open breaker trip states and backoff duration | Fails (missing get_state) | PASS |
| **Tier 3** | `test_cache_roundtrip_attribution_preservation` | Attribution persistence across SQLite SERP cache roundtrips | Fails (missing attribution fields) | PASS |
| **Tier 3** | `test_cache_backward_compatibility_legacy_payload` | Backward compatibility for legacy cache payloads without attribution | Fails (missing default dataclass fields) | PASS |
| **Tier 3** | `test_graceful_degradation_under_partial_outage` | Complex multi-failure outage (429 + timeout + connect drop) with lone survivor | Fails (missing attribution fields) | PASS |
| **Tier 4** | `test_sequential_multi_query_agent_workload` | Agent sequential query workload across session with cache reuse and filters | PASS | PASS |
| **Tier 4** | `test_transient_outage_automatic_recovery` | Complete circuit lifecycle: `CLOSED` -> `OPEN` -> `HALF_OPEN` -> `CLOSED` | Fails (missing get_state) | PASS |
| **Tier 4** | `test_pure_json_stdout_schema_1_end_to_end` | End-to-end CLI pure JSON verification across search, diagnose, cache commands | PASS | PASS |

---

## 4. Invariant Verification

The test suite enforces the following inviolable project invariants:
1. **Schema 1.0 JSON on stdout**: Every CLI command emitting payloads to stdout produces pure JSON parseable by `json.loads()` with `schema_version: "1.0"`.
2. **Zero Stderr Pollution on stdout**: Stderr is reserved for diagnostic logs, native SSL warnings, and rate limit notes; stdout is never contaminated.
3. **Cormack-Clarke RRF Formula**: $\text{score} = \text{round}\left( \text{domain\_weight} \times \sum \text{rank\_contributions}, 6 \right)$ with $\text{rank\_contributions}[e] = \text{round}(1.0 / (k + \text{engine\_ranks}[e]), 6)$.
4. **Zero-Daemon / Keyless**: Tests run in self-contained processes without requiring external daemons or API keys.

---

## 5. Next Steps for Core Integration

The E2E test suite is fully implemented and ready. As `worker_core_1` finishes Milestone Core (`engine_ranks`, `rank_contributions`, `score_weights`, `CircuitBreaker.get_state`, `Josty.breaker_status`, and latency/circuit fields on `HostStatus` and `ProviderStatus`), running `.venv/bin/pytest tests/test_e2e_resilience.py -q` will verify all 20 tests pass.
