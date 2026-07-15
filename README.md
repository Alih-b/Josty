# Deep Search

Keyless web search for AI agents. No MCP server, account, or search API key required.

## Install as an Agent Skill

From this repository:

```bash
npx skills add . --skill deep-search
```

The agent runs the bundled launcher. On first use it installs its Python dependencies into a private environment inside the skill.

## Install as a CLI

```bash
pipx install .
# or
uv tool install .
```

```bash
deep-search "your query" --limit 10
deep-search "SearXNG agent integrations" --site github.com
deep-search "agent search tools" --mode oss
deep-search "AI regulation" --category news --time-limit w --region us-en
deep-search "company history and funding" --fetch
```

Output is JSON. `partial: true` means at least one upstream provider failed.

## Options

```text
--limit N          maximum results
--site DOMAIN      add a site filter; repeatable
--mode MODE        plain, exact, or oss
--category TYPE    text or news
--region REGION    DDGS region code, such as us-en or de-de
--safe-search MODE on, moderate, or off
--time-limit RANGE d, w, m, or y
--fetch            extract bounded page text
--web-only         skip GitHub repository search
--results-only     output only the result array
```

`GITHUB_TOKEN` is optional and only increases GitHub API limits.

## What this project adds to DDGS

Deep Search does not operate a search index. It orchestrates the MIT-licensed
[DDGS](https://github.com/deedy5/ddgs) library and adds:

- parallel searches across independent backend groups;
- Reciprocal Rank Fusion and URL deduplication;
- optional official GitHub repository search;
- explicit provider health and partial-result reporting;
- bounded concurrent extraction with public-network URL validation;
- a stable JSON envelope for agent, CLI, Python, and HTTP integrations.

Search queries are sent to the selected upstream engines and, by default, GitHub.
Fetched pages are untrusted input. Providers can throttle, block, log, or change their
interfaces, and each provider's terms apply. This project does not claim unlimited search.

## Local HTTP API (optional)

```bash
pipx install ".[api]"
uvicorn deep_search.api:app --host 127.0.0.1 --port 8080
```

Keep the API on localhost unless you add authentication and network controls. Application-level
URL checks reduce SSRF risk, but DNS is resolved separately during validation and connection; use
network-level egress controls before accepting requests from untrusted users.

## Development

```bash
python -m pip install -e ".[dev]"
pytest -q
ruff check .
```

MIT licensed. Upstream search providers may throttle or block requests.
