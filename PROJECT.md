# Project: Josty Resilient Orchestration & Transparent RRF

## Architecture
Josty is a keyless, zero-daemon search metasearch tool and Agent Skill.
The core architecture consists of:
- **CLI & Dispatch (`josty.cli`)**: Parses user flags, routes commands, formats output. Pure JSON conforming to `schema_version: "1.0"` is strictly emitted on stdout; all diagnostics, warnings, and error summaries route to stderr.
- **Search Engine Orchestration (`josty.engine.Josty`)**: Coordinates backend fanout, query execution, caching, content fetching, and reciprocal rank fusion.
- **Circuit Breaker (`josty.breaker.CircuitBreaker`)**: Tracks provider health, manages sliding-window failure thresholds, tri-state circuit lifecycle (`CLOSED`, `OPEN`, `HALF_OPEN`), dynamic backoff, and isolates failing backends (429, 403, timeouts, disconnects).
- **Reciprocal Rank Fusion (`josty.ranking.rrf`)**: Fuses results across disparate search backends using Cormack-Clarke RRF ($1/(k + r)$), incorporates domain weighting profiles, and preserves transparent attribution telemetry (`engine_ranks`, `rank_contributions`, `score_weights`).
- **Telemetry & Status Models**: `SearchRun`, `DiagnoseRun`, `ProviderStatus`, `HostStatus`, `SearchResult`.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Upstream Engine Drift Resolution | Register `Google` in `_DDGS_ENGINES['text']` to resolve upstream ddgs 9.15.0 deprecation | Core | Survey |
| 2 | Baseline Test Suite Parity | Ensure all 414 existing unit tests in `tests/` pass with zero failures | Core | Survey |
| 3 | Concurrency Hang Protection | Wrap `_ddgs` thread execution in `asyncio.wait_for(timeout=...)` to prevent thread/semaphore hangs | Core | Survey |
| 4 | Tri-State Circuit Breaker | Implement formal `CLOSED`, `OPEN`, `HALF_OPEN` states with trial probes | Core | Survey |
| 5 | Sliding-Window Failure Tracking & Backoff | Track failure counts in sliding window with exponential backoff on consecutive trips | Core | Survey |
| 6 | Robust Error Classification | Classify HTTP 429, 403 challenges, timeouts, and network disconnects into breaker triggers | Core | Survey |
| 7 | Per-Provider Latency Tracking | Measure high-resolution latency (`latency_ms`) for provider searches and host probes | Core | Survey |
| 8 | `ProviderStatus` Telemetry | Expose `latency_ms`, `circuit_state`, `failures`, `backoff_remaining` on provider fanout | Core | Survey |
| 9 | `HostStatus` Telemetry | Expose `latency_ms`, `circuit_state`, `failures`, `backoff_remaining` on diagnose probes | Core | Survey |
| 10 | Engine `breaker_status()` API | Expose programmatic inspection API for active circuit states and metrics | Core | Survey |
| 11 | `--diagnose` Enhanced Health Reporting | Surface backend availability and circuit trip states without leaking debug noise to stdout | Core | Survey |
| 12 | Pure JSON Stdout Invariant | Retain strictly valid JSON output conforming to `schema_version: "1.0"` | Core | Survey |
| 13 | Stderr Diagnostics Routing Invariant | Route all third-party warnings, native SSL warnings, and diagnostic notes to stderr | Core | Survey |
| 14 | `SearchResult` Attribution Extension | Add `engine_ranks`, `rank_contributions`, `score_weights` fields to `SearchResult` | Core | Survey |
| 15 | Origin Discovery Rank Capture | Record 1-indexed discovery rank in `_ddgs` and `github_run` | Core | Survey |
| 16 | Rank Preservation Across Merges | Merge engine discovery ranks (`min(rank)`) in `_merge_result` and `merge_query_variants` | Core | Survey |
| 17 | RRF Score & Contribution Computation | Calculate and record per-engine reciprocal rank terms and domain weights during RRF | Core | Survey |
| 18 | Cache Roundtrip Compatibility | Ensure new attribution fields survive SQLite SERP cache roundtrips and legacy cache entries deserialize | Core | Survey |
| 19 | Comprehensive E2E Test Suite (Tiers 1-4) | Opaque-box test suite covering feature coverage, boundaries, combinations, and real-world workloads | E2E | Survey |
| 20 | Failure Simulation & Adversarial Hardening | Verify graceful degradation when backends throttle (429), challenge (403), or time out | Final | Survey |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| E2E | E2E Testing Suite (Tiers 1-4) | Build opaque-box E2E test suite covering feature coverage, boundaries, pairwise combinations, and real-world workloads; publish TEST_READY.md | none | COMPLETE |
| Core | Core Engine Resilient Orchestration & Transparent RRF | Implement Google drift fix, tri-state circuit breaker, timeout protection, latency tracking, telemetry in `ProviderStatus`/`HostStatus`, `breaker_status()`, RRF transparent attribution, cache compatibility | none | COMPLETE |
| Final | Final Integration & Adversarial Hardening | Pass 100% of E2E test suite, perform Tier 5 white-box adversarial testing, verify zero-daemon/keyless invariants, complete forensic audit | E2E, Core | COMPLETE |

## Interface Contracts

### Circuit Breaker & Health Telemetry
- `CircuitBreaker.status(backend: str, error_class: str = "rate_limit") -> tuple[bool, str | None]`
- `CircuitBreaker.get_state(backend: str) -> dict[str, Any]` returning:
  `{"state": "closed" | "open" | "half-open", "failures": int, "backoff_remaining": float, "last_latency_ms": float | None}`
- `Josty.breaker_status(backend: str | None = None) -> dict[str, Any]`
- `ProviderStatus` dataclass:
  - `provider: str`
  - `query: str`
  - `ok: bool`
  - `result_count: int`
  - `error: str | None = None`
  - `error_kind: str | None = None`
  - `latency_ms: float | None = None`
  - `circuit_state: str | None = None`
  - `failures: int | None = None`
  - `backoff_remaining: float | None = None`
- `HostStatus` dataclass:
  - `provider: str`
  - `host: str`
  - `ok: bool`
  - `http_status: int | None = None`
  - `error_kind: str | None = None`
  - `error: str | None = None`
  - `challenged: bool = False`
  - `latency_ms: float | None = None`
  - `circuit_state: str | None = None`
  - `failures: int | None = None`
  - `backoff_remaining: float | None = None`

### Transparent RRF Attribution Contract
- `SearchResult` dataclass:
  - `title: str`
  - `url: str`
  - `snippet: str = ""`
  - `sources: list[str] = field(default_factory=list)`
  - `published_at: str | None = None`
  - `publisher: str | None = None`
  - `score: float = 0.0`
  - `content: str | None = None`
  - `extraction_method: str | None = None`
  - `fetched_url: str | None = None`
  - `fetched_at: str | None = None`
  - `fetch_error: str | None = None`
  - `engine_ranks: dict[str, int] = field(default_factory=dict)`
  - `rank_contributions: dict[str, float] = field(default_factory=dict)`
  - `score_weights: dict[str, float] = field(default_factory=dict)`
- Mathematical invariant:
  $$\text{score} \approx \text{round}\left( \text{domain\_weight} \times \sum_{e \in \text{rank\_contributions}} \text{rank\_contributions}[e], 6 \right) \quad (\pm 10^{-6} \text{ float tolerance})$$
  where $\text{rank\_contributions}[e] = \text{round}(1.0 / (k + \text{rank}_e), 6)$ and $\text{score\_weights} = \{"k": 60.0, "domain\_weight": \dots\}$.
  Each configured engine is queried independently. For a canonical URL discovered by multiple engines, each discovering engine contributes one RRF term at its own discovery rank; there is no group-level vote or best-engine credit. The sum of per-engine rank contributions matches the fused score within $\pm 10^{-6}$.

## Code Layout
- `src/josty/engine.py`: The `Josty` facade; search storage, fetch, and diagnose orchestration.
- `src/josty/cli.py`: CLI commands, output formatting, diagnose display.
- `tests/test_e2e_resilience.py`: Comprehensive opaque-box E2E test suite.
- `tests/test_engine.py`: Existing engine tests.
- `tests/test_cli.py`: Existing CLI tests.
- `tests/test_edge_cases.py`: Existing edge case tests.
