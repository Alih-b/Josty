# AGENTS.md

## Purpose & Protocol
`josty` is a zero-config, keyless metasearch tool and Agent Skill designed specifically for AI agent workflows.

## How to Install & Configure
If instructed to install this skill or tool:
1. Ensure the skill file `.agents/skills/josty/SKILL.md` is registered in your active agent skills folder (e.g., `.agents/skills/josty/`, `~/.claude/skills/josty/`, or your agent's runtime directory).
2. Use `uvx josty "<query>"` for instant, cached execution without persistent virtual environment overhead.

## Quick Invocations (Pi Agent / Claude Code / Codex / Antigravity)
```bash
# 1. Preferred runner (uvx):
uvx josty "query" --limit 10

# 2. Extract clean markdown content:
uvx josty "query" --fetch

# 3. Targeted domain search:
uvx josty "query" --site github.com --site stackoverflow.com

# 4. Fallback execution via bundled script:
$(command -v python3 || command -v python) .agents/skills/josty/scripts/run.py "query"
```

## Output & Contract Guarantees
- `stdout` emits only pure, valid, parseable JSON conforming to `schema_version: "1.0"`.
- All errors, diagnostics, and third-party dependency warnings are strictly routed to `stderr`.
- Requires outbound internet access.
