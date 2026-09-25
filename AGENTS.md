# AGENTS.md

Instructions for AI coding agents modifying or testing this codebase.

## Codebase Purpose & Scope

Josty is a small, keyless search tool and Python library for agents and scripts. It queries public backends via `ddgs`, fuses rankings with Reciprocal Rank Fusion (RRF), strips tracking query parameters, and can extract bounded page text.

Josty is **not** a search engine, web crawler, or privacy proxy. Keep the footprint minimal and keyless.

## Invariants You Must Never Break

1. **Pure JSON on stdout**: Normal CLI output on `stdout` must be parseable JSON only. All warnings, logs, and tracebacks must go to `stderr`.
2. **Hermetic Tests**: Automated tests must never make real network requests. Patch `ddgs.DDGS` using `tests/mock_ddgs.py` or `monkeypatch`. Isolate cache paths via temporary directories.
3. **Exit Code Contract**:
   - Exit `0`: `status` is `complete`, `degraded`, or `empty`; also `--diagnose`, `--cache-stats`, and `--clear-cache`.
   - Exit `1`: `status` is `failed` (all attempted backends failed with 0 results).
   - Exit `2`: Command-line syntax or argument validation errors.
4. **No Hidden Amplification**: `Josty.research_run()` executes exactly one fanout pass, counted by a per-run ledger at the ddgs/GitHub call site and reported as `request_count`. Never add automatic background retries or silent query rewriting to fix empty results; callers handle query broadening. Admission is bounded by a per-run `LeasePool`: a call that is refused before it opens a socket is reported as `error_kind="skipped"` with a shed reason and is **never** rendered as an upstream network failure. Every proposed call must end as issued, shed, or not-attempted; `tests/test_fanout_leases.py` pins that identity, and stdout stays pure JSON.
5. **SSRF Guard**: `fetch.py` validates destination IP addresses before connecting. Never relax checks for loopback, private subnets (RFC 1918), link-local, cloud metadata (`169.254.169.254`), or non-HTTP schemes.
6. **No Circumvention**: Do not add CAPTCHA solvers, paywall bypasses, or anti-bot evasions. Upstream provider failures must be surfaced faithfully in `ProviderStatus`.

## Verification Commands

Before reporting any coding task complete, run and pass all three:

```bash
# 1. Hermetic unit & resilience suite (no network, ~5s):
pytest -q

# 2. Formatting and linting:
ruff check .

# 3. Live search smoke. This is the only step that touches the network, so it is
#    a manual step and never a pytest test (invariant 2). Use the venv entry point:
#    a bare `josty` on PATH is the installed uv tool, i.e. the published version.
.venv/bin/josty "python packaging" | python3 -m json.tool > /dev/null
```

Step 3 passes on exit code `0` — which already covers `complete`, `degraded` and
`empty` — plus parseable JSON on stdout. Do not assert `status == "complete"`:
upstream engines throttle and block, and a gate that fails for their reasons is
not a gate.

Write unit tests for critical behaviour only — admission, SSRF, cache, status
transitions. The suite is a safety net for the hard parts, not a coverage target.

If modifying packaging or dependencies, also verify build:
```bash
python3 -m build
```

## Code Layout & Seams

When modifying behavior, edit the specific module rather than bloating the facade:

- **`src/josty/engine.py`**: Orchestration facade (`Josty`). Coordinates fanout, caching, and fetch phases. Each run creates one `LeasePool` and one dedicated `ThreadPoolExecutor`, so a worker that never returns cannot shed a later run and page-text extraction keeps the loop's default executor. `_FanoutLedger` counts calls at the issue site so `request_count` is measured, not scheduled.
- **`src/josty/lease.py`**: Per-run lease admission (`LeasePool`, `Lease`). Fixed capacity, expiring leases, idempotent release, and ghost accounting; refusal reasons (`capacity`, `ghost_capacity`, `ghost_budget`, `deadline`) never masquerade as upstream failures.
- **`src/josty/models.py`**: Dataclasses (`SearchRun`, `SearchResult`, `ProviderStatus`, `HostStatus`, `DiagnoseRun`).
- **`src/josty/status.py`**: Enums, constants (`SearchStatus`, `ErrorKind`, `ProfileType`, `SCHEMA_VERSION = "1.0"`).
- **`src/josty/ranking.py`**: URL canonicalization (`canonical`), tracking param stripping, Cormack-Clarke RRF fusion (`rrf`), domain weights.
- **`src/josty/breaker.py`**: In-process tri-state circuit breaker (`CircuitBreaker`) tracking failures per `(backend, error_class)`.
- **`src/josty/cache.py`**: SQLite WAL cache (`SearchCache`), tiered TTLs, byte-budget eviction, cache keys salted with `SCHEMA_VERSION`.
- **`src/josty/fetch.py`**: Streaming HTTP download, SSRF IP validation, Trafilatura text extraction.
- **`src/josty/errors.py`**: Provider error classification (`_classify_search_error`) and multi-variant aggregation.
- **`src/josty/backends.py`**: Checks backend engine availability against installed `ddgs` registry.
- **`src/josty/cli.py`**: Command-line interface (`parser`, `run`, `main`).
- **`src/josty/_version.py`**: Single source of truth for package version literal (`__version__`).
