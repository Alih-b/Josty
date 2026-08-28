# Deep Search

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Changelog](https://img.shields.io/badge/changelog-CHANGELOG.md-orange)](CHANGELOG.md)
[![Ruff](https://img.shields.io/badge/code%20style-ruff-261230)](https://github.com/astral-sh/ruff)

Deep Search queries keyless public search backends in parallel, fuses their rankings with Reciprocal Rank Fusion (RRF), strips tracking parameters, and returns a versioned JSON contract with structured Markdown extraction. It gives AI agents a predictable subprocess and Python interface for web search and page retrieval without requiring per-backend response parsing or ad-hoc deduplication.

---

## Installation

```bash
# Recommended for CLI / Agent runtimes
pipx install deep-search-agent
# or with uv
uv tool install deep-search-agent
# or standard pip
pip install deep-search-agent
```

Deep Search declares 3 direct dependencies in `pyproject.toml` (`ddgs`, `httpx`, `trafilatura`); see `uv.lock` for the exact resolved dependency closure.

---

## Quickstart

### 1. Simple Search (CLI)
```bash
# Basic web search with top 5 results
deep-search "Python 3.13 features" --limit 5

# Developer profile: boosts GitHub, PyPI, crates.io, MDN, framework docs
deep-search "FastAPI dependency injection" --profile dev --limit 5

# Academic profile: boosts arXiv, PubMed, IEEE, Nature, OpenAlex
deep-search "retrieval augmented generation" --profile academic --limit 5

# Focus on specific technical domains
deep-search "httpx connection reset" --site github.com --site stackoverflow.com

# Open Source discovery mode
deep-search "document indexing" --mode oss --github

# Extract clean, bounded Markdown from the top pages
deep-search "RRF rank fusion algorithm" --limit 3 --fetch
```

### 2. Output Format (Versioned JSON)
Deep Search returns a strict, self-describing contract that agents can easily branch on:

```json
{
  "schema_version": "1.0",
  "query": "Python 3.13 features",
  "status": "complete",
  "count": 3,
  "partial": false,
  "providers": [
    { "provider": "bing", "ok": true, "result_count": 5 },
    { "provider": "duckduckgo", "ok": true, "result_count": 5 }
  ],
  "results": [
    {
      "title": "What's New In Python 3.13 — Python 3.13.0 documentation",
      "url": "https://docs.python.org/3/whatsnew/3.13.html",
      "snippet": "Python 3.13 includes an experimental free-threaded build mode...",
      "sources": ["bing", "duckduckgo"],
      "score": 0.032787,
      "content": "## What's New In Python 3.13\n\nThis article explains the new features...",
      "extraction_method": "trafilatura"
    }
  ]
}
```

**Contract Versioning Policy (`schema_version`)**: The `schema_version` contract follows semantic versioning. Additive, backward-compatible fields will bump the minor version (e.g. `1.1`), while any breaking structural change or field removal will bump the major version (`2.0`).

---

## Typical Agent Scenarios

### 🔍 Scenario A: Direct Python / Async Lookup
When an agent needs to query search backends directly within an async workflow:
```python
import asyncio
from deep_search import DeepSearch

async def verify_fact(query: str):
    engine = DeepSearch()
    run = await engine.research_run(query, limit=3)
    if run.status == "failed":
        return "Search failed; try alternative formulation."
    return [(r.title, r.url, r.snippet) for r in run.results]

print(asyncio.run(verify_fact("Linux kernel initial release year")))
```

### 🛠️ Scenario B: LLM Tool Calling (OpenAI / Anthropic Schema)
Drop this directly into your agent's tool definitions:

```python
search_tool_definition = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for up-to-date information, documentation, and error solutions. Built on keyless backends and returns ranked sources.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query."
                },
                "fetch": {
                    "type": "boolean",
                    "description": "Set to true to fetch and extract bounded Markdown page text.",
                    "default": False
                },
                "mode": {
                    "type": "string",
                    "enum": ["plain", "exact", "oss"],
                    "description": "Search mode. 'oss' searches for open-source repositories.",
                    "default": "plain"
                }
            },
            "required": ["query"]
        }
    }
}
```

---

## What the Wrapper Logic Does

Deep Search sits between an agent runtime and underlying search backends (queried via `ddgs`). Its code path provides the following concrete operations:

1. **Parallel Group Fanout**: Queries backend groups concurrently via `asyncio.gather`, bounded by an explicit semaphore (`max_search_concurrency`).
2. **URL Canonicalization & Deduplication**: Normalizes URLs across engines (stripping tracking parameters like `utm_*`, `gclid`, `fbclid`, `msclkid`, lowercasing hostnames, removing trailing slashes) so identical pages from different backends merge into a single result with unified provenance (`sources: ["bing", "duckduckgo"]`).
3. **Deterministic Weighted RRF Fusion ($k=60$)**: Merges ranked lists from 2–3 active engines per query slot using Reciprocal Rank Fusion weighted by profile domain rules:
   $$\text{score} = \sum_{i} \frac{w_i}{k + r_i}$$
   where $k=60$ (Cormack et al. 2009), $r_i$ is the 1-based rank in provider $i$, and $w_i$ is a domain weight multiplier. Domain sets are explicitly defined per profile in `deep_search.engine` (`AUTHORITATIVE_DOMAINS_GENERAL`, `AUTHORITATIVE_DOMAINS_DEV`, `AUTHORITATIVE_DOMAINS_ACADEMIC` for official documentation, registries, and papers boosted at 1.2–1.4×; `SPAM_DOMAINS` penalized at 0.5–0.6× across all profiles). The default `general` profile boosts documentation domains and applies spam penalties.
4. **Structured Error & Status Taxonomy**: Categorizes provider-level errors into exact `ErrorKind` literals (`"network"`, `"rate_limited"`, `"empty"`, `"parse"`, `"unknown"`) and sets top-level run `status` (`"complete"`, `"degraded"`, `"failed"`) so callers can branch deterministically on partial failures.
5. **Bounded Content Extraction**: Fetches and converts HTML to Markdown via Trafilatura with strict byte limits (`max_download_bytes`, default 2MB) and character limits (`max_content_chars`, default 50k) to prevent context exhaustion in downstream models.

---

## Security & Safety

Deep Search includes built-in SSRF protection when `--fetch` is enabled:
- **IP Address Validation**: Resolves hostnames and blocks connections to private (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), loopback (`127.0.0.0/8`), link-local (cloud metadata `169.254.169.254`), multicast, and CGNAT IP ranges.
- **Redirect Validation**: Follows up to 6 redirects, validating every intermediate IP address before establishing the next TCP connection.
- **Resource Bounds**: Enforces hard byte download limits before parsing HTML.
- Read full details in [SECURITY.md](SECURITY.md).

---

## Why This Exists & What's Next

### Current Scope & Design Boundaries
Deep Search is intentionally a small, self-contained Python wrapper rather than a full search service or browser runtime:
- **When Not to Use This**: If you need client-side JavaScript execution (SPAs), automated CAPTCHA solving, or guaranteed high-QPS uptime from datacenter IPs, deep-search is not the right tool.
- **No Headless Browser**: Deep Search uses standard HTTP requests (`httpx`) and does not execute client-side JavaScript.
- **Upstream Rate Limits**: Because queries rely on public search scraping backends via `ddgs`, requests from shared datacenter IP ranges can be throttled or blocked by upstream engines.
- **Single Maintainer**: This is a pre-1.0 research project without multi-organization operational hardening.

### Roadmap
- **Near-Duplicate Content Hashing**: Adding MinHash/SimHash snippet deduplication for pages with different URLs that mirror the same syndicated text.
- **Direct Connector Plugins**: Allowing callers to register custom backend coroutines alongside default `ddgs` scrapers.
- **Pluggable Text Distillers**: Enabling alternative markdown converters for specialized document structures (e.g., API references or tabular data).

---

## Development

```bash
# Clone and set up dev environment
git clone https://github.com/Alih-b/deep-search.git
cd deep-search
python -m pip install -e ".[dev]"

# Run test suite
pytest -q

# Run linter
ruff check .
```

MIT Licensed. Release notes and version history are tracked in [CHANGELOG.md](CHANGELOG.md). Built by Ali Bayest.

