# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
