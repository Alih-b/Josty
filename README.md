# Deep Search

> **Small enough to audit. Broad enough to research.**

A shell-first, keyless metasearch primitive for AI agents. One command, three direct runtime
dependencies, structured JSON. No account, daemon, MCP server, hosted vendor, or search API key.

Deep Search searches independent DDGS backend groups, merges query rewrites without treating them
as extra provider votes, and combines the independent rankings with Reciprocal Rank Fusion. It can
optionally search GitHub repositories and fetch bounded page text.

## Install as an Agent Skill

```bash
npx skills add Alih-b/super-search --skill deep-search
```

The skill launcher creates a private `.venv` and installs its bounded dependencies on first use.

## Install as a CLI

```bash
pipx install .
# or
uv tool install .
```

```bash
deep-search "your query" --limit 10
deep-search "SearXNG integrations" --site github.com
deep-search "agent search tools" --mode oss --github
deep-search "AI regulation" --category news --time-limit w --region us-en
deep-search "company history and funding" --fetch
```

Output is versioned JSON. `status` is `complete`, `degraded`, or `failed`; `partial: true` means at
least one upstream branch failed. Query rewrites are visible in `providers`.

## Options

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

`GITHUB_TOKEN` is optional and only increases GitHub API limits. GitHub search is opt-in so ordinary
fact and news searches are not mixed with repository results or disclosed to GitHub unnecessarily.

## What it adds to DDGS

Deep Search does not operate a search index. It builds on the MIT-licensed
[DDGS](https://github.com/deedy5/ddgs) package and adds:

- independent backend groups with one auditable RRF pass;
- plain, exact-phrase, and OSS query rewrites without duplicate provider voting;
- strict domain filtering and canonical URL deduplication;
- merged source provenance and provider health reporting;
- opt-in official GitHub repository search;
- bounded concurrent extraction with public-network URL validation;
- a small, versioned JSON contract for shell and Python callers.

## Deliberately not included

Deep Search stays lean: no MCP server, HTTP daemon, browser automation, cache, vector store, claim
verdicts, LLM summarizer, or paid-provider upgrade path. For those features use a full search stack
such as [websearch-skill](https://github.com/hec-ovi/websearch-skill). Choose Deep Search when a
small auditable command with hard content bounds is the better fit. See the
[scope comparison](docs/COMPARISON.md).

## Trust boundary

Search queries go directly to selected upstream engines and, only with `--github`, GitHub. Providers
can throttle, block, log, or change behavior, and their terms apply. Fetched pages are untrusted data.
The fetcher allows public HTTP(S), revalidates redirects, rejects private/reserved destinations and
credential-bearing URLs, limits decoded downloads to 2 MB, and caps extracted text at 50,000
characters. DNS rebinding remains a documented residual because validation and connection resolve
separately.

## Development

```bash
python -m pip install -e ".[dev]"
pytest -q
ruff check .
python -m build
```

MIT licensed. Status: alpha.
