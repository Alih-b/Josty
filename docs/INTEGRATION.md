# Integration

Josty is intentionally shell-first. An agent calls one command and parses one versioned JSON
object. MCP, an HTTP daemon, an LLM, and hosted credentials are not required.

## Running josty

Preferred: run the CLI straight from PyPI — no install step, no repo checkout needed:

```bash
uvx josty "open source search for AI agents" --limit 10
```

For repeated use, install it once (`pipx install josty` / `uv tool install josty`) and run
the `josty` console script. Agents working from a source checkout without `uvx` on PATH can
use the bundled thin delegator instead — it installs josty from PyPI on first use, then runs it:

```bash
python "$SKILL_DIR/scripts/run.py" "open source search for AI agents" --limit 10
```

The delegator falls back `uv tool install` → `pipx install` → `pip install --user` and needs
only `python3`, so it also covers sandboxes where `uvx` is not installed (for example
bare-Ubuntu cloud agents). It bundles no engine code — the single dependency manifest is
`pyproject.toml`.

## Installed CLI

```bash
josty "open source search for AI agents" --limit 10
josty "SearXNG skill" --site github.com --fetch
josty "agent search" --mode oss --github
```

The default output envelope is:

```json
{
  "schema_version": "1.0",
  "query": "...",
  "status": "complete",
  "count": 10,
  "partial": false,
  "cached": false,
  "provider_count": 6,
  "nonempty_provider_count": 2,
  "coverage": 0.333,
  "query_variant_count": 1,
  "request_count": 6,
  "fetch": {
    "requested": false,
    "attempted": 0,
    "ok": 0,
    "failed": 0,
    "status": "skipped"
  },
  "providers": [],
  "results": []
}
```

- `complete`: results are available and no search branch failed, or every successful branch returned zero. This is not multi-engine coverage: read `nonempty_provider_count` / `coverage`.
- `degraded`: at least one search branch failed, while another branch completed or results remain available; or `--fetch` was requested and every attempted extraction failed (`fetch.status=failed`).
- `failed`: no results and every attempted branch failed.
- `cached`: `true` only when the envelope was loaded from the local SQLite cache. `fetch` is not part of the SERP cache key: search then `--fetch` reuses the cached SERP and only downloads pages.
- `query_variant_count` / `request_count`: how many query strings were expanded, and how many upstream search calls that scheduled (engines × variants, plus GitHub when opted in). On cache hits nothing is scheduled, so `request_count` is 0. Payloads predating these fields report `null` (unknown), not 0.
- `nonempty_provider_count` / `coverage`: how many branches both succeeded (`ok`) and returned results, over total branches. A failed branch never counts, even with partial results.
- `--diagnose` envelopes set `phase: "transport"` and `probe: "https_host"`. That status is homepage HTTPS reachability, not search health.
- Empty-ok branches set `error_kind` to `"empty"` only when `result_count` is 0. Skipped
  branches set `error_kind` to `"skipped"` only when no call was made: a breaker cool-down
  (not evidence the engine is down), an engine unknown or disabled in the installed ddgs,
  an available engine with no mapped diagnose host, or `--diagnose` against an OPEN
  circuit. `error_kind: "blocked"` is HTTP 401/403 or an auth/forbidden challenge;
  `error_kind: "rate_limited"` is 429 / rate-limit tokens only. Diagnose probes set
  `challenged` when `http_status` is 401, 403, or 429. An empty-ok branch neither trips
  nor clears the circuit breaker; only a non-empty success clears failure history.
  HALF_OPEN admits one trial probe; other concurrent callers skip until it completes.

Always inspect `providers`; an upstream failure or empty branch must not be interpreted as evidence that no
information exists. Josty does not rewrite the query on empty results.

## Python

```python
import asyncio
from josty import Josty

run = asyncio.run(
    Josty().research_run(
        "agent search",
        mode="oss",
        include_github=True,
        limit=10,
    )
)
print(run.dict())
```

Supported controls:

- `mode`: `plain`, `exact`, or `oss`
- `category`: `text` or `news`
- `region`: DDGS region code such as `us-en`
- `safesearch`: `on`, `moderate`, or `off`
- `timelimit`: `d`, `w`, `m`, or `y`
- `sites`: strict repeatable hostname filters, maximum five
- `fetch`: bounded text extraction, off by default
- `include_github`: official repository search, off by default

## Trust boundary

Fetched content is untrusted. URL checks and hard byte/character limits reduce risk but do not
eliminate DNS rebinding because validation and connection resolve separately. Josty is a local
library and command, not a network service. Callers that wrap it in a service must add authentication,
quotas, cancellation, monitoring, process isolation, and network-level egress controls.
