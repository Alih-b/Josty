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

- **`src/josty/engine.py`**: Orchestration facade (`Josty`). Coordinates expansion, caching, fanout, fusion and the fetch phase, and owns no admission state: each run builds one `Fanout` and delegates to it. `_ddgs` and `github_run` survive as compatibility shims over the branch pipeline (the suite patches those names, plus the `DDGS` global and `SEARCH_THREAD_TIMEOUT_HEADROOM`, at call time) — put new behaviour in the modules below, not here.
- **`src/josty/branch.py`**: The provider-branch pipeline (`BranchRunner`): gate → admit → issue → classify → status. Holds the three policies that must exist exactly once — the empty-ok carve-out, the shed message, and the breaker-telemetry stamp — plus `SearchCall`, `IssueOutcome` and the `ProviderAdapter` protocol. Step order is load-bearing; see the module docstring.
- **`src/josty/fanout.py`**: One run's admission and accounting (`Fanout`, `Plan`, `_FanoutLedger`). Owns the `LeasePool`, the dedicated `ThreadPoolExecutor`, the deadline and the ledger; gathers plans, merges each group's query variants, aggregates per-engine statuses and restamps breaker telemetry after the gather.
- **`src/josty/providers.py`**: The adapters behind the seam (`DdgsSearchAdapter`, `GithubSearchAdapter`). An adapter knows only how to perform one upstream call, and calls `ledger.record()` at the network call site — which is what makes `request_count` a measurement of calls issued rather than tasks scheduled.
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

## Releasing

`src/josty/_version.py` is the single version source: hatchling reads that literal,
and `pyproject.toml` holds `dynamic = ["version"]` and must never gain a second copy.

1. Merge to `main`, then tag the merge commit: `git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z`
2. Publish a GitHub Release for that tag: `gh release create vX.Y.Z --generate-notes`.
   That triggers `.github/workflows/publish.yml`, which re-runs the suite and lint,
   builds, checks the artifact matches the tag, and uploads to PyPI by trusted
   publishing — no token, no `twine`, no local `dist/`.
3. Nothing is uploaded by hand. If the workflow fails before the upload step, fix and
   re-run it; PyPI refuses a second upload of the same version, so never rebuild and
   republish a version that already shipped.

The release workflow runs from the default branch's copy of the file, so it must be
merged to `main` before the first release that uses it.
