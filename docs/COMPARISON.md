# Deep Search and full search stacks

The closest solid open-source comparator is
[`hec-ovi/websearch-skill`](https://github.com/hec-ovi/websearch-skill). Both use DDGS,
Reciprocal Rank Fusion, URL deduplication, Trafilatura, optional GitHub repository search, and an
Agent Skill/CLI surface. They optimize for different shapes.

| Dimension | Deep Search | websearch-skill |
|---|---|---|
| Motto | Small enough to audit. Broad enough to research. | Contract-driven search and page-reading stack |
| Primary face | One JSON CLI plus Python API | CLI, MCP, Python, plugin manifests |
| Direct dependencies | 3 | Broader fetch, validation, and MCP closure |
| Search | Fixed independent DDGS groups | DDGS plus optional SearXNG/adapters |
| Fusion | One RRF pass; query rewrites collapsed per backend | Weighted, provenance-aware, correlation-group RRF |
| Site filtering | Strict hostname post-filter, max 5 | Strict include/exclude filtering |
| GitHub | Explicit `--github`, optional token | Dedicated typed GitHub tool |
| Fetch | Public HTTP(S), 2 MB download and 50k-character output caps | Tiered fetch, block detection, lossless pagination |
| Extraction | Trafilatura with text fallback | Trafilatura, metadata, page type, quality score |
| Storage | None | Optional page store and FTS/BM25 lookup |
| Agent transport | Shell | Shell and MCP |
| Scope | Small local primitive | Full local search subsystem |

Choose Deep Search when the requirements are:

- a small reviewable codebase;
- no daemon or MCP configuration;
- hard output bounds;
- one subprocess and one versioned JSON object;
- provider failures visible to the calling agent;
- no cache, browser impersonation, paid-provider path, or state.

Choose `websearch-skill` when you need SearXNG, MCP, arXiv, lossless pagination, cached page opening,
typed contracts, extraction quality scoring, or a larger adapter architecture.

Deep Search does not aim for feature parity. Its comparison claim is lower operational and audit
surface, not greater retrieval breadth.
