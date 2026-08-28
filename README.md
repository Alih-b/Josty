# Deep Search

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Ruff](https://img.shields.io/badge/code%20style-ruff-261230)](https://github.com/astral-sh/ruff)

**A fast, keyless, zero-daemon search & page extraction primitive built specifically for AI agents.**

Stop giving your agents raw HTML, brittle scrapers, or requiring paid API keys for simple research tasks. Deep Search queries multiple independent search backends in parallel, fuses their rankings with **Reciprocal Rank Fusion (RRF)**, and returns a clean, versioned JSON contract with structured Markdown extraction.

---

## Why Deep Search?

| Feature | Raw Scraping / `ddgs` | Heavy Search SaaS (Tavily/Exa) | Self-Hosted (SearXNG) | **Deep Search** |
|---|---|---|---|---|
| **API Keys / Billing** | Keyless | ❌ Paid / Tiered Keys | Keyless | **✅ 100% Keyless** |
| **Infrastructure** | None | SaaS dependency | ❌ Docker / Redis / Host | **✅ Zero daemons** |
| **Output Contract** | Messy / Unranked | JSON | JSON / HTML | **✅ Versioned JSON + Clean Markdown** |
| **Ranking** | None (Raw list) | Proprietary | Score sum | **✅ Auditable RRF (k=60)** |
| **Footprint** | Script | Cloud | Heavy (>1 GB RAM) | **✅ Tiny (<1,000 LOC, 3 deps)** |
| **Agent Ready** | Manual parsing | Tool API | API | **✅ Subprocess / Python Primitive** |

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

# Open Source / Self-hosted discovery mode
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

---

## Typical Agent Scenarios

### 🔍 Scenario A: Fact-Checking & Grounding
When an agent needs to verify a fast factual question without hallucinating:
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
        "description": "Search the web for up-to-date information, documentation, and error solutions. Keyless and returns ranked sources.",
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

## Benchmark Highlights

Tested against a frozen corpus of factual queries using TREC-style evaluation (see [docs/BENCHMARK.md](docs/BENCHMARK.md)):

* **Speed**: **1.62s** mean latency (faster than `raw_ddgs` at 5.48s and `websearch-skill` at 3.22s, p < 0.0001).
* **Quality**: **0.960 nDCG@10** (statistically tied with full-stack enterprise search wrappers).
* **Footprint**: **< 1,000 lines of pure Python**, 3 dependencies (`ddgs`, `httpx`, `trafilatura`).

---

## Security & Safety

Deep Search includes built-in SSRF protection when `--fetch` is enabled:
- Blocks private, loopback, multicast, link-local (cloud metadata `169.254.169.254`), and CGNAT IP ranges.
- Follows up to 6 redirects with validation at each hop.
- Hard byte download limits (`max_download_bytes`) and extracted text limits (`max_content_chars`).
- Read full details in [SECURITY.md](SECURITY.md).

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

MIT Licensed. Built by Ali Bayest.

