# Harness integration

Deep Search is shell-first: an agent can call one command and parse one JSON object. MCP, an LLM, and hosted search credentials are not required.

## Portable skill launcher

Resolve the installed skill directory, then run:

```bash
python "$SKILL_DIR/scripts/run.py" "open source search for AI agents" --limit 10
```

The skill contains the canonical Python source and CLI requirements. The launcher uses available dependencies or creates a private skill virtual environment on first use. Copying only the skill directory remains supported.

## Installed CLI

```bash
deep-search "open source search for AI agents" --limit 10
deep-search "SearXNG MCP server" --site github.com --fetch
```

The CLI emits:

```json
{
  "query": "...",
  "count": 10,
  "partial": false,
  "providers": [],
  "results": []
}
```

Always inspect `partial` and `providers`. An upstream failure must not be interpreted as evidence that no information exists.

## HTTP

For clients making repeated calls:

```bash
uvicorn deep_search.api:app --host 127.0.0.1 --port 8080
```

```bash
curl --get http://127.0.0.1:8080/search \
  --data-urlencode 'q=site:github.com SearXNG agent skill' \
  --data-urlencode 'limit=10' \
  --data-urlencode 'mode=plain' \
  --data-urlencode 'fetch=false'
```

OpenAPI is available at:

- `http://127.0.0.1:8080/docs`
- `http://127.0.0.1:8080/openapi.json`

Supported request parameters:

- `q`: query, 2–500 characters
- `limit`: 1–100
- `mode`: `plain`, `exact`, or `oss`
- `category`: `text` or `news`
- `region`: DDGS region code such as `us-en`
- `safesearch`: `on`, `moderate`, or `off`
- `timelimit`: `d`, `w`, `m`, or `y`
- `site`: repeatable domain filter
- `fetch`: bounded text extraction, off by default
- `research`: include official GitHub repository search, on by default

## Production boundary

The default service is intended for a trusted local user. Before network exposure, add authentication, TLS, quotas, logs/metrics, process isolation, and egress policy. URL validation reduces SSRF risk but is not a substitute for network-level isolation. DNS is resolved separately during validation and connection, so network policy must enforce the same public-destination boundary. Retrieved content must always be treated as untrusted input.
