# refactor_probe

The repeatable form of the fanout/branch change set's **"Nothing else moved"** claim.

`envelope_probe.py` runs 13 hermetic engine scenarios (fake DDGS, fake `httpx`) and
writes a canonical JSON snapshot of each `SearchRun` envelope. `compare.py` diffs two
snapshots key by key. Run it on the base revision and on the refactor: structure may
move, behaviour must not — so every diff that survives comparison is either one of the
deliberate changes or a regression.

## Reproduce

```sh
V="$(pwd)/.venv/bin/python"

# 1. the refactor's snapshot
"$V" scripts/refactor_probe/envelope_probe.py /tmp/head.json

# 2. the base revision's snapshot. The probe is new in this change set, so it has to
#    be copied into the base worktree; PYTHONPATH makes `josty` resolve there.
git worktree add --detach /tmp/josty-base e886eff
mkdir -p /tmp/josty-base/scripts && cp -r scripts/refactor_probe /tmp/josty-base/scripts/
(cd /tmp/josty-base && PYTHONPATH=src:tests "$V" scripts/refactor_probe/envelope_probe.py /tmp/base.json)

# 3. diff. Exit 1 means a difference survived normalisation, which is the expected
#    result here -- the two deliberate changes are supposed to show up.
"$V" scripts/refactor_probe/compare.py /tmp/base.json /tmp/head.json

git worktree remove /tmp/josty-base
```

Measured on this change set: **11 of 13 scenarios identical**, and exactly the two
deliberate ones differ:

```
[github_empty]        github-api error_kind: None -> "empty"          # deliberate change 1
[github_saturated]    fanout.issued: 6 -> 7
                      fanout.shed: 1 -> 0 (was {capacity: 1})
                      request_count: 6 -> 7
                      coverage: 0.857 -> 1.0
                      status: degraded -> complete                    # deliberate change 4
```

## Normalisation, and its self-check

Two quantities are non-deterministic by construction and are masked **visibly**:

- `latency_ms` — real wall-clock jitter on a mock engine. Presence is still compared
  (`None` vs measured is a behaviour signal); the value is not.
- absolute cool-down stamps inside a breaker skip message (`until <ISO>Z`).

Everything else is compared verbatim. Because masking can hide a real difference, run
`compare.py` on two snapshots of the **same** code first: it must print `IDENTICAL`, or
the normalisation is wrong.

## Why this is not a pytest test

It needs two revisions at once. The per-change pins live in
`tests/test_deliberate_contract.py` and `tests/test_fanout_leases.py`; this probe is the
evidence for everything *not* covered by a pin.
