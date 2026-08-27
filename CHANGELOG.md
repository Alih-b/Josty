# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `--diagnose` CLI flag (and `DeepSearch.diagnose_run()`) that probes each search backend's
  upstream host with a bare HTTP request instead of running ddgs, reporting per-provider HTTPS
  reachability (`timeout` / `dns` / `network` / `unknown`) in the versioned JSON contract.
- `--search-concurrency` and `--fetch-concurrency` CLI flags (and `max_search_concurrency` /
  `max_fetch_concurrency` constructor params) to tune search and fetch ceilings independently.

### Changed

- Split the single shared semaphore into separate search and fetch semaphores so a slow page fetch
  can no longer block concurrent backend queries. Defaults remain 6 for search and 4 for fetch.

## [0.3.0] - 2026-07-15

### Changed

- Repositioned Deep Search as a small shell/Python primitive: no MCP, HTTP daemon, cache, or browser.
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
