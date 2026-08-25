# Deep Search

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Ruff](https://img.shields.io/badge/code%20style-ruff-261230)

A small, keyless metasearch CLI for AI agents. It queries independent
search-backend groups, fuses rankings with Reciprocal Rank Fusion, and
returns a versioned JSON object. No account, daemon, MCP server, hosted
vendor, or search API key.

## Install

```bash
pipx install .
# or
uv tool install .
```

## Usage

```bash
deep-search "your query" --limit 10
deep-search "AI regulation" --fetch
```

Output is versioned JSON. `status` is `complete`, `degraded`, or `failed`.

## What it does

- Fuses independent backend groups with one auditable RRF pass.
- Fetches bounded page text with public-network and size limits.
- Exposes a small, versioned JSON contract for shell and Python callers.

## When to use / when not to use

Use Deep Search when you want a small, auditable subprocess that
deduplicates, ranks, and reports failures.

For a full search stack with MCP, page store, SearXNG, and extraction
quality scoring, see [docs/COMPARISON_ARCHITECTURE.md](docs/COMPARISON_ARCHITECTURE.md).

## Options

<details>
<summary><code>deep-search --help</code></summary>

```text
--limit N          maximum results, 1–100
--site DOMAIN      strict hostname filter; repeatable up to five times
--mode MODE        plain, exact, or oss
--category TYPE    text or news
--region REGION    DDGS region code, such as us-en or de-de
--safe-search MODE on, moderate, or off
--time-limit RANGE d, w, m, or y
--github           also search official GitHub repositories
--fetch            extract bounded page text
--results-only     output only the result array
```

</details>

## Benchmark

See [docs/BENCHMARK.md](docs/BENCHMARK.md) for the canonical frozen-corpus
benchmark and reproducibility instructions.

## Development

```bash
python -m pip install -e ".[dev]"
pytest -q
ruff check .
python -m build
```

MIT licensed.
