# E2E resilience suite

`tests/test_e2e_resilience.py` covers public search, diagnose, cache, and
breaker contracts. `tests/test_adversarial_challenger.py` covers 429 floods,
timeouts, ghost threads, and HALF_OPEN flapping. Both are hermetic.

## Commands

```bash
pytest tests/test_e2e_resilience.py -q
pytest tests/test_adversarial_challenger.py -q
pytest -q
```

## Coverage

| Tier | Test | Contract |
|---|---|---|
| 1 | `test_cli_diagnose_contract_and_schema` | CLI `--diagnose` JSON + HostStatus telemetry |
| 1 | `test_multi_engine_search_execution` | Fanout + ProviderStatus telemetry |
| 1 | `test_breaker_status_api_inspection` | `breaker_status()` / `get_state()` schema |
| 1 | `test_search_result_attribution_telemetry` | RRF attribution invariant |
| 1 | `test_stderr_diagnostic_routing` | Failure in-band on stdout JSON |
| 1 | `test_news_category_uses_news_method` | News path through `DDGS.news()` |
| 2 | `test_failure_simulation_429_rate_limit` | 429 isolated; survivors fuse |
| 2 | `test_failure_simulation_403_challenge` | 403 is `blocked`, query does not crash |
| 2 | `test_failure_simulation_timeout_protection` | Hang bounded by wait_for |
| 2 | `test_failure_simulation_connection_drop` | ConnectError → `network` |
| 2 | `test_all_backends_failing_boundary` | Total outage → `failed` |
| 2 | `test_empty_and_whitespace_queries` | Empty query validation |
| 2 | `test_single_vs_multi_engine_attribution_boundary` | Multi-engine score boost |
| 3 | `test_circuit_breaker_trip_with_rrf_attribution` | OPEN skip + survivor attribution |
| 3 | `test_diagnose_during_open_circuit_breaker` | Diagnose skips OPEN hosts |
| 3 | `test_cache_roundtrip_attribution_preservation` | Cache keeps attribution |
| 3 | `test_cache_backward_compatibility_legacy_payload` | Legacy payload defaults |
| 3 | `test_graceful_degradation_under_partial_outage` | Mixed faults, one survivor |
| 4 | `test_sequential_multi_query_agent_workload` | Cache reuse + site filter |
| 4 | `test_transient_outage_automatic_recovery` | CLOSED → OPEN → HALF_OPEN → CLOSED |
| 4 | `test_pure_json_stdout_schema_1_end_to_end` | CLI JSON across commands |
