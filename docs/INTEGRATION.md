# Integration

Josty is intentionally shell-first. An agent calls one command and parses one versioned JSON
object. MCP, an HTTP daemon, an LLM, and hosted credentials are not required.

## Portable skill launcher

```bash
python "$SKILL_DIR/scripts/run.py" "open source search for AI agents" --limit 10
```

The launcher uses a private environment under the installed skill. If that environment is missing or
the bundled requirements change, it creates or refreshes it from `requirements.txt` under a setup lock.

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
      "providers": [],
  "results": []
}
```

- `complete`: results are available and no branch failed, or every successful branch returned zero.
- `degraded`: at least one branch failed, while another branch completed or results remain available.
- `failed`: no results and every attempted branch failed.
- `cached`: `true` only when the envelope was loaded from the local SQLite cache.
- Empty-ok branches set `error_kind` to `"empty"`. Diagnose probes set `challenged` when `http_status` is 401, 403, or 429.

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
