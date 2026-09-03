# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Per-engine search fanout: backend groups (`"brave,duckduckgo"`) now issue one ddgs call
  per engine instead of one blended group call. Group-level RRF fusion is preserved, with
  group-internal ranking now owned by Josty: a URL's position is its best rank across
  engines, without ddgs's hidden frequency voting or wikipedia pin. `providers[]` reports
  exactly one status per engine, aggregated across query variants (`result_count` = distinct
  canonical URLs; `error_kind` = most severe variant outcome, so partial throttling stays
  visible; `error` = first failure message) — an engine that silently returns empty or fails
  inside a healthy group is visible instead of absorbed. The circuit breaker is per-engine
  as a result. Unlike raw ddgs, which early-stops and can leave engines unqueried at small
  limits, Josty always queries every configured engine: complete per-engine breaker health
  is worth the extra page-1 requests (bounded by engine count × `max_query_variants`).
- Engine-availability gate: engines that are unknown or disabled in the installed ddgs
  (e.g. ddgs 9.16.0 has bing/yandex text engines disabled upstream) are skipped with
  `ok=false`, `error_kind="skipped"` and a named error, instead of triggering ddgs's
  silent fallback to `backend="auto"` (all engines) or being dropped without a trace.
- Default text backends updated to engines ddgs 9.16.0 actually serves:
  `("brave,duckduckgo", "google,mojeek,startpage", "yahoo")` — the previous defaults
  included bing and yandex, which ddgs has disabled, silently running 6 engines instead
  of the configured 8.
- Circuit-breaker cool-down skips now report `error_kind: "skipped"` (schema 1.0 additive)
  on the affected `providers[]` entries, so agents can distinguish a deliberate breaker
  skip from an unclassified `unknown` failure.
- `--diagnose` now skips unknown/disabled engines with `error_kind="skipped"` and a named
  error instead of probing their hosts and reporting a generic failure.

### Fixed

- `SearchRun.partial` now accounts for aggregated per-engine statuses whose query variants
  partially failed (`ok=true` with a failure `error_kind`): a run where one variant was
  throttled or errored reports `degraded` instead of a clean `complete`. `"empty"` remains a
  successful empty branch and does not degrade the run.
- Engines configured in multiple groups are queried once, in their first group; duplicate
  names across groups no longer double-call upstream or duplicate `providers[]` entries.

## [0.4.0] - 2026-09-02

### Added

- Envelope field `run_at` (ISO8601 UTC, optional, schema 1.0 compatible): the moment the
  search was executed. Cached hits preserve the original `run_at`, so agents can judge
  the age of a `cached: true` result instead of trusting it blindly.
- Freshness TTL floors for the search cache: `timelimit=d` entries expire after 30
  minutes, `timelimit=w` after 2 hours, and `category=news` after 1 hour (previously a
  flat 6 hours for everything). A caller-configured shorter `cache_ttl` is always
  respected.
- Scenario-eval staleness check (`require_run_at`, `max_age_s`) plus a frozen
  `stale_news_day_old_cache` fixture that pins the pre-fix behavior as
  `intended_misleading` — cached day-news staleness is now measured, not silent.
- Empty-ok search branches now set `providers[].error_kind` to `"empty"` (schema 1.0 compatible)
  so callers can tell a real empty result from an unclassified success.
- Diagnose envelope field `challenged`: true when a reachable probe returns HTTP 401, 403, or 429.
- Search cache access telemetry (`hit_count`, `last_accessed`), a 5,000-row prune ceiling, and
  envelope flag `cached` (true only on cache hit).
- `--cache-stats` CLI flag (plus `SearchCache.stats()` / `Josty.cache_stats()`) reporting
  aggregate cache telemetry — rows, payload bytes, cumulative hits — so the bounded cache is
  inspectable without opening the database.
- Issue taxonomy and offline scenario eval (`docs/ISSUE_TAXONOMY.md`,
  `tests/scenario_eval.py`) so live misses can be labeled as `contract_bug`,
  `intended_misleading`, `upstream_quality`, or `product_gap` without changing
  ranking. Frozen corpus is scored in `pytest`; optional live recapture requires
  `JOSTY_LIVE_EVAL=1`.
- `error_kind` field on each `providers[]` entry in the search JSON contract, classifying the
  failure as `network`, `rate_limited`, `empty`, `parse`, or `unknown` so callers can distinguish
  a TLS-layer rejection from a rate limit from a genuine empty result without manual bisection.
  ddgs 9.15.0 flattens engine exceptions into a single string instead of chaining them via
  `__cause__`/`__context__`, so classification uses `isinstance` for the outer class plus
  substring matching of the flattened inner message; the original exception's type and repr are
  still preserved in the existing `error` field.
- `--diagnose` CLI flag (and `Josty.diagnose_run()`) that probes each search backend's
  upstream host with a bare HTTP request instead of running ddgs, reporting per-provider HTTPS
  reachability (`timeout` / `dns` / `tls` / `network` / `unknown`) in the versioned JSON contract.
- `SearchCache`: SQLite-backed local disk cache with configurable TTL (default 6 hours) to prevent redundant queries and upstream rate limits in agent loops. Supports `--no-cache` and `--clear-cache` CLI flags.
- Ranking profiles and subdomain-aware authority weighting (`general`, `dev`, `academic` via `--profile` flag and `profile=` API param): Suffix-matches subdomains (e.g. `api.github.com`, `pubmed.ncbi.nlm.nih.gov`, `pkg.go.dev`) and applies tailored weights to developer frameworks and scholarly research repositories.
- Domain-weighted Reciprocal Rank Fusion (RRF): Multiplies ranking scores for authoritative technical and documentation sources by 1.2x-1.4x and penalizes content farms by 0.5x-0.6x.
- Structured Markdown page extraction: `--fetch` uses Trafilatura's native Markdown format to preserve headings, code blocks, and links for token-efficient LLM consumption.
- Offline deterministic benchmark replay check integrated into CI test workflow.
- `--search-concurrency` and `--fetch-concurrency` CLI flags (and `max_search_concurrency` /
  `max_fetch_concurrency` constructor params) to tune search and fetch ceilings independently.
- Per-`(backend, error_class)` in-process circuit breaker: 3 failures within 60 s opens the
  breaker for 30 s. Skipped calls are reported with
  `skipped: backend in cool-down until <iso8601>` in `providers[].error`. Defaults exposed via
  `breaker_fail_threshold` / `breaker_window_seconds` / `breaker_cool_down_seconds` constructor
  params, or a custom `breaker` instance.

### Changed

- Version is resolved once via `importlib.metadata.version("josty")` with a static fallback for
  pre-install runs. `josty.__version__`, the GitHub API `User-Agent`, and hatchling build metadata
  (now `dynamic = ["version"]`) all derive from that single literal, so they can no longer drift
  between releases.
- The search cache now stores SERPs only: per-result fetch fields (`content`,
  `extraction_method`, `fetched_url`, `fetched_at`, `fetch_error`) are blanked before
  write, and a `fetch=True` cache hit re-fetches page content on demand. This turns the
  5,000-row cap into a real byte bound (~10-25 MB) instead of a potential multi-GB cache
  of page text.
- Added a byte-budget prune ceiling (`SearchCache(max_bytes=...)`, default 50 MB,
  `Josty(cache_max_bytes=...)`): when cached payload bytes exceed the budget, rows are
  evicted oldest-expiring / least-hit until back under it. The "bounded local state"
  claim is now true in bytes, not just rows.

- Removed the hidden query-rewrite fallback in `research_run` (strip quotes / drop last token).
  Empty fused results stay empty; callers rewrite. Matches the skill rule of no automatic retry.
- `v0.4.0` roadmap keep-list: empty/`challenged` signals and bounded cache.
  RFC-1 relaxation, news engine filters, and hard host floors are out of scope.
- Split the single shared semaphore into separate search and fetch semaphores so a slow page fetch
  can no longer block concurrent backend queries. Defaults remain 6 for search and 4 for fetch.

## [0.3.0] - 2026-07-15

### Changed

- Repositioned Josty as a small shell/Python primitive: no MCP, HTTP daemon, cache, or browser.
- Made GitHub repository search explicit with `--github` and switched it to best-match ordering.
- Made site filters strict and limited them to five validated hostnames.
- Collapse query rewrites per backend before one cross-backend RRF pass.
- Preserve source provenance plus news publication and publisher metadata.
- Removed automatic retries to avoid hidden request amplification.
- Added versioned result status: `complete`, `degraded`, or `failed`.
- Use only the private skill environment in the portable launcher.

### Fixed

- Prevent GitHub results from resetting web-fusion scores or contaminating normal factual searches.
- Fall back to noise-stripped HTML text when Trafilatura returns empty output.
- Add a timeout to hostname resolution and disable environment proxy inheritance for fetches.
- Restrict source-distribution contents with an explicit allowlist.

## [0.2.0] - 2026-07-15

### Added

- Portable Agent Skill launcher with a private virtual environment.
- Parallel DDGS backend groups with Reciprocal Rank Fusion.
- Optional GitHub repository search.
- Provider status and partial-result reporting.
- Bounded page extraction and public-network URL validation.
- CLI and Python API.
