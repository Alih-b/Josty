# Test infrastructure

Hermetic tests patch `ddgs.DDGS` and isolate the SQLite cache with a per-test
`XDG_CACHE_HOME`. They do not call live search backends.

## Layout

```
tests/
├── mock_ddgs.py                 # Shared MockDDGSEngine + clock/hostname helpers
├── test_e2e_resilience.py       # Tier 1–4 opaque-box resilience suite
├── test_adversarial_challenger.py
├── test_engine.py
├── test_cli.py
├── test_edge_cases.py
├── test_launcher.py
└── test_scenario_eval.py
```

## Mocking

`MockDDGSEngine` implements `.text()`, `.news()`, and `.images()`. Per-backend
values may be a result list, an exception, or a callable. Circuit-breaker tests
advance `josty.engine.time.monotonic` instead of sleeping. Hang tests patch
`SEARCH_THREAD_TIMEOUT_HEADROOM` so `asyncio.wait_for` bounds are short.

## Invariants the suites pin

- stdout is one JSON document with `schema_version: "1.0"` on search/diagnose.
- `score = round(domain_weight * sum(rank_contributions), 6)` with
  `rank_contributions[e] = round(1/(k + engine_ranks[e]), 6)`.
- `error_kind: "rate_limited"` is 429 / rate-limit tokens; HTTP 401/403 is
  `"blocked"`.
- HALF_OPEN admits one trial probe; `--diagnose` does not probe OPEN circuits.

## Commands

```bash
pytest tests/test_e2e_resilience.py tests/test_adversarial_challenger.py -q
pytest -q
ruff check .
python tests/scenario_eval.py
```
