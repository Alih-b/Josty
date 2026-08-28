# AGENTS.md

## Purpose & Protocol
`deep-search` is a zero-config, keyless metasearch tool and Agent Skill designed specifically for AI agent workflows.

## How to Install
If instructed to install this skill or tool:
1. Ensure the skill file `.agents/skills/deep-search/SKILL.md` is registered in your active agent skills folder (e.g., `.agents/skills/deep-search/` or `~/.claude/skills/deep-search/`).
2. Use `uvx deep-search-agent "<query>"` for instant, cached execution without persistent virtual environment overhead.

## Quick Invocations
```bash
# 1. Preferred runner (uvx):
uvx deep-search-agent "query" --limit 10

# 2. Extract clean markdown content:
uvx deep-search-agent "query" --fetch

# 3. Targeted domain search:
uvx deep-search-agent "query" --site github.com --site stackoverflow.com

# 4. Fallback execution via bundled script:
$(command -v python3 || command -v python) .agents/skills/deep-search/scripts/run.py "query"
```

## Output & Contract Guarantees
- `stdout` emits only pure, valid, parseable JSON conforming to `schema_version: "1.0"`.
- All errors, diagnostics, and third-party dependency warnings are strictly routed to `stderr`.
- Requires outbound internet access.
