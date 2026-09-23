<!--
  name: Josty
  description: Keyless search tool and bounded text extraction for agents and scripts.
  repository: https://github.com/Alih-b/Josty
  license: MIT
  specification: .agents/skills/josty/SKILL.md
  compatibility: Python 3.10+, CLI, AI agent runtimes.
  keywords: search, agent, keyless-search, ddgs, rrf, trafilatura, cli
-->

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Alih-b/Josty/main/docs/assets/logo.svg">
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/Alih-b/Josty/main/docs/assets/logo-light.svg">
  <img alt="Josty Logo" src="https://raw.githubusercontent.com/Alih-b/Josty/main/docs/assets/logo.svg" width="320" height="76">
</picture>

<p>
  <strong>Keyless search and bounded text extraction for agents and scripts.</strong>
</p>

<p>
  <a href="https://github.com/Alih-b/Josty/actions/workflows/ci.yml"><img src="https://github.com/Alih-b/Josty/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+" /></a>
  <a href="https://github.com/Alih-b/Josty/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT" /></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/code%20style-ruff-261230" alt="Code Style: Ruff" /></a>
</p>

</div>

---

## What It Is

**Josty** queries public search backends in parallel through `ddgs`, fuses rankings with Cormack-Clarke Reciprocal Rank Fusion (RRF), canonicalizes URLs, and optionally extracts bounded page text.

It is designed for AI agents and developer scripts that need a self-contained search step without API keys, background daemons, or browser engines.

```text
┌──────────────────────────────┐
│  Local Agent / Script Step   │
└──────────────┬───────────────┘
               │ single search execution
               ▼
┌──────────────────────────────┐
│            Josty             │
│  - Bounded parallel fanout   │
│  - Domain-weighted RRF (k=60)│
│  - In-process breaker & cache│
│  - Trafilatura text extract  │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│   Public Backends via ddgs   │
│  (Brave, DuckDuckGo, Yahoo,  │
│   Mojeek, Startpage, Google) │
└──────────────────────────────┘
```

## Installation

```bash
# Run instantly with uvx (no install step needed):
uvx josty "Python 3.13 features" --limit 5

# Or install globally:
uv tool install josty
# or: pipx install josty
```

## Quickstart

```bash
# 1. Basic search (returns top 5 results):
josty "Python 3.13 release highlights" --limit 5

# 2. Boost technical documentation domains (dev profile):
josty "FastAPI dependency injection" --profile dev --limit 5

# 3. Restrict search to specific domains (up to 5):
josty "httpx connection pool timeout" --site github.com --site python-httpx.org

# 4. Extract bounded Markdown page text from top results:
josty "RRF rank fusion algorithm" --limit 3 --fetch

# 5. Unix pipeline (stream search results straight into fetch):
josty search "FastAPI dependency injection" --limit 3 | josty fetch --stdin
```

## Output & Status Contract

Normal searches emit one JSON document on `stdout`. Diagnostics and warnings route strictly to `stderr`.

```json
{
  "schema_version": "1.0",
  "query": "FastAPI dependency injection",
  "status": "complete",
  "count": 3,
  "partial": false,
  "cached": false,
  "provider_count": 6,
  "nonempty_provider_count": 2,
  "coverage": 0.333,
  "providers": [...],
  "results": [
    {
      "title": "Dependencies - FastAPI",
      "url": "https://fastapi.tiangolo.com/tutorial/dependencies/",
      "snippet": "FastAPI has a very powerful but intuitive Dependency Injection system...",
      "sources": ["duckduckgo", "brave"],
      "score": 0.039024,
      "content": null
    }
  ]
}
```

### Status Values & Exit Codes

| Status | Meaning | Exit Code |
|---|---|---|
| `complete` | Results found; no backend failed. | `0` |
| `empty` | No results found; no backend failed (`count=0`). | `0` |
| `degraded` | Results found, but at least one backend failed; or page extraction failed. | `0` |
| `failed` | Every attempted backend failed; zero results returned. | `1` |
| Validation Error | Invalid CLI options or empty query. | `2` |

## Limitations & Non-Goals

- **No Privacy or Anonymity Guarantees**: Josty does not proxy or anonymize traffic. Queries are sent directly to upstream search engines.
- **Best-Effort Availability**: Upstream public engines may throttle (HTTP 429), present anti-bot challenges, or change response formats. Josty isolates failures via circuit breakers and surfaces errors honestly rather than hiding them behind infinite retries.
- **Not a Search Engine**: Josty does not maintain an index or crawl the web. It is a lightweight client adapter over `ddgs`.
- **No Hidden Query Rewriting**: If a search returns empty, Josty reports `status: "empty"`. The caller decides whether to broaden terms or adjust site filters.

## Documentation

- **Agent Tool Specification**: [`.agents/skills/josty/SKILL.md`](.agents/skills/josty/SKILL.md) (full flag specifications, schemas, provider telemetry, and agent research rules).
- **Coding Agent Guide**: [`AGENTS.md`](AGENTS.md) (invariants, test commands, and module layout for agents working on this codebase).
- **Security Policy**: [`SECURITY.md`](SECURITY.md).
