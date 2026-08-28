<!--
  name: Josty
  description: Zero-config, keyless metasearch engine and autonomous AI agent skill.
  repository: https://github.com/Alih-b/josty
  license: MIT
  specification: .agents/skills/josty/SKILL.md
  keywords: metasearch, ai-agent, search-engine, keyless-search, rrf, rank-fusion, trafilatura, pi-agent, claude-code, gemini-agent, cursor, llm-tool
-->

<div align="center">

![Josty Logo](docs/assets/logo.svg)

<p>
  <strong>Zero-config, keyless metasearch and bounded content extraction for AI agents.</strong>
</p>

<p>
  <a href="https://github.com/Alih-b/josty/actions/workflows/ci.yml"><img src="https://github.com/Alih-b/josty/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT" /></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/changelog-CHANGELOG.md-orange" alt="Changelog" /></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/code%20style-ruff-261230" alt="Code Style: Ruff" /></a>
</p>

</div>

---

> **AI Agent Skill Specification**:
> - **Cross-Agent Standard**: Conforms to the open `.agents/skills/` specification.
> - **Registered Skill Location**: `.agents/skills/josty/SKILL.md`
> - **Supported Runtimes**: Pi Agent, Claude Code, OpenAI Codex, Gemini Antigravity, and Cursor.
> - **Instant Execution**: `uvx josty "<query>" --limit 10`
> - **Page Extraction**: `uvx josty "<query>" --fetch`

---

## What It Is

**Josty** (from Persian *جستن* / *Jostan* — to seek) queries keyless public search backends in parallel, fuses rankings with **Reciprocal Rank Fusion (RRF)**, canonicalizes URLs, strips tracking telemetry, and extracts bounded Markdown from target pages. 

It provides autonomous AI agents, personal assistants, and developer workflows with a dependable, structured search subprocess and async Python API without requiring search API keys, background daemons, or heavy browser dependencies.

---

## Installation

```bash
# Recommended: Instant cached execution (zero persistent virtualenv overhead)
uvx josty "Python 3.13 changes" --limit 5

# Global CLI installation via uv:
uv tool install josty

# Alternative installation via pipx or standard pip:
pipx install josty
pip install josty
```

---

## Quickstart

### 1. CLI Usage

```bash
# Basic web search (top 5 results)
josty "Python 3.13 features" --limit 5

# Developer profile (boosts GitHub, PyPI, crates.io, MDN, StackOverflow)
josty "FastAPI dependency injection" --profile dev --limit 5

# Academic profile (boosts arXiv, PubMed, IEEE, Nature, OpenAlex)
josty "retrieval augmented generation" --profile academic --limit 5

# Domain filtering (up to 5 domains)
josty "httpx connection reset" --site github.com --site stackoverflow.com

# Open Source discovery mode
josty "document indexing" --mode oss --github

# Extract clean, bounded Markdown from top result pages
josty "RRF rank fusion algorithm" --limit 3 --fetch
```

### 2. Versioned JSON Output

`stdout` emits pure, parseable JSON conforming to a strict schema contract (`schema_version: "1.0"`):

```json
{
  "schema_version": "1.0",
  "query": "Python 3.13 features",
  "status": "complete",
  "count": 3,
  "partial": false,
  "providers": [
    { "provider": "bing,brave,duckduckgo", "ok": true, "result_count": 5 },
    { "provider": "google,mojeek,startpage", "ok": true, "result_count": 5 }
  ],
  "results": [
    {
      "title": "What's New In Python 3.13 — Python 3.13.0 documentation",
      "url": "https://docs.python.org/3/whatsnew/3.13.html",
      "snippet": "Python 3.13 includes an experimental free-threaded build mode...",
      "sources": ["bing,brave,duckduckgo", "google,mojeek,startpage"],
      "score": 0.032787,
      "content": "## What's New In Python 3.13\n\nThis article explains the new features...",
      "extraction_method": "trafilatura"
    }
  ]
}
```

---

## Verified Agent Framework Guides

### 🥧 Pi Agent (Open-Source Agent CLI)

Pi Agent can execute Josty either through bash subprocess tool execution or by registering the `.agents/skills/josty/SKILL.md` skill definition in its active skill directory:

```bash
# 1. Fact retrieval & web search from Pi:
uvx josty "latest release of pydantic" --limit 5

# 2. Extract clean page text for deeper reasoning:
uvx josty "how to implement reciprocal rank fusion in python" --fetch --limit 3

# 3. Targeted technical debugging:
uvx josty "asyncpg connection pool exhaustion" --site github.com --site stackoverflow.com
```

**Pi Agent Verification Rules**:
1. Run `uvx josty "<query>" --limit 5` for real-time web verification.
2. If snippet context is insufficient, append `--fetch` to extract full Markdown text.
3. Check `status` (`complete`, `partial`, `failed`); provider failure is not evidence of absence.
4. Cite primary source URLs directly in response output.

---

### 🔍 Direct Async Python API
```python
import asyncio
from josty import DeepSearch

async def main():
    engine = DeepSearch(profile="dev")
    run = await engine.research_run("Linux kernel initial release year", limit=3)
    
    if run.status != "failed":
        for result in run.results:
            print(f"[{result.title}]({result.url})\n{result.snippet}\n")

asyncio.run(main())
```

---

### 🛠️ LLM Tool Calling Schema (OpenAI / Anthropic / Gemini)
```python
search_tool_definition = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for up-to-date information, documentation, and technical solutions. Returns keyless ranked sources.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query."
                },
                "fetch": {
                    "type": "boolean",
                    "description": "Set to true to fetch and extract clean Markdown page content.",
                    "default": False
                },
                "profile": {
                    "type": "string",
                    "enum": ["general", "dev", "academic"],
                    "description": "Ranking profile boosting authoritative technical or academic domains.",
                    "default": "general"
                },
                "mode": {
                    "type": "string",
                    "enum": ["plain", "exact", "oss"],
                    "description": "Search mode ('oss' filters for open-source repositories).",
                    "default": "plain"
                }
            },
            "required": ["query"]
        }
    }
}
```

---

## Technical Specifications & Architecture

| Parameter / Feature | Code Value / Contract | Description |
| :--- | :--- | :--- |
| **Schema Version** | `1.0` | Output format contract on `stdout` |
| **Max Domain Filters** | `5` (`--site`) | Maximum concurrent site constraints per query |
| **Search Concurrency** | `6` (`--search-concurrency`) | Default bounded semaphore for search backends |
| **Fetch Concurrency** | `4` (`--fetch-concurrency`) | Default bounded semaphore for page content fetching |
| **Max Content Size** | `8,000 chars` (`--max-content-chars`) | Extracted Markdown character ceiling per page (0 for unlimited) |
| **Download Byte Limit** | `2,097,152 bytes` (2MB) | Hard ceiling on raw HTTP downloads before parsing |
| **RRF Parameter** | $k=60$ | Cormack et al. (2009) reciprocal rank smoothing factor |
| **SSRF Safeguards** | Verified | Blocks private subnets, loopback, RFC 1918, and `169.254.169.254` metadata |

---

## Development

```bash
# Clone repository
git clone https://github.com/Alih-b/josty.git
cd josty

# Install in editable mode with dev dependencies
python -m pip install -e ".[dev]"

# Run test suite
pytest -q

# Lint and check code style
ruff check .
```

---

## License

MIT © Ali Bayest. See [LICENSE](LICENSE) for details.
