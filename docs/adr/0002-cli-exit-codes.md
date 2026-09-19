# CLI exit codes follow the search status

`josty` printed a `status` field but always exited `0`, so an exit-code-only caller could not tell an outage from a successful search. Issue #42 locked the mapping: `status="failed"` exits `1`; `complete`, `degraded`, and the later `empty` exit `0` (partial and no-result runs stay visible in-band); usage and validation errors keep exit `2`. `cli.main` prints the JSON envelope to `stdout` first and only then raises, so the machine-readable result is never lost to the exit path.

The mapping deliberately covers the search envelope only. `--diagnose` is a transport probe whose `status` is homepage reachability, not search health, so it stays exit `0` even when every probe fails; `--results-only` emits an array with no status and also stays `0`. `degraded` staying `0` is the point of the gate: a run with one usable engine is still usable, and callers that need full coverage read `coverage` / `partial` themselves.

This is a deliberate CLI contract change, recorded under the `docs/ISSUE_TAXONOMY.md` guard-rail for deliberate contract changes. It is documented as a table in `README.md`, `docs/INTEGRATION.md`, and `.agents/skills/josty/SKILL.md`.
