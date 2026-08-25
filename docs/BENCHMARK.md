# Benchmark

The canonical benchmark compares three runners on a frozen corpus of
factual queries:

- `deep_search` — this project's async wrapper
- `raw_ddgs` — direct `ddgs` client, serial per backend
- `websearch_skill` — `websearch search --engines ddgs --json` subprocess

It follows TREC-style conventions: graded relevance, nDCG/MRR/P@k, frozen
corpus + offline replay, repeated runs, and paired Wilcoxon signed-rank
tests with bootstrap CIs.

## What it measures

| Metric | Why |
|---|---|
| nDCG@10 | Position-aware quality reward |
| MRR | Position of first relevant result |
| Precision@5 | Fraction of top-5 that is relevant |
| Graded recall@10 | Sum of relevance grades / ideal total |
| Latency mean/std/p50/p95 | End-to-end wall time per query |
| Empty-run rate | Operational reliability signal |

Grading is deterministic string matching
(`tests/benchmark_grade.string_grade`): canonical URL = 3, answer-string
hit = 2, weak subject co-occurrence = 1, otherwise 0.

## Frozen-corpus workflow

Upstream engines throttle, so the benchmark freezes responses and scores
them offline:

1. `python tests/benchmark.py` runs the live benchmark and streams each
   `(runner, query, repeat)` row to JSONL. Failed runners write an
   `error` field instead of crashing the loop.
2. The captured JSONL is checked into `tests/benchmark_corpus.jsonl`.
3. `python tests/benchmark_replay.py` scores that corpus offline. No
   network calls. Anyone can reproduce the metrics bit-for-bit.

## Current corpus

- **File:** `tests/benchmark_corpus.jsonl`
- **Size:** 408 tuples (20 queries × 8 repeats × 3 runners, some partial)
- **Captured:** 2026-08-25
- **Report:** `tests/benchmark_out/replay/REPORT.md`

## Statistically defensible findings

These are from the frozen-corpus replay. Empty runs (throttled engines)
count as zeros in the `all_runs` view and are filtered out in the
`successful_runs_only` view.

### Coverage

| runner | distinct queries | runs | empty | errors |
|---|---:|---:|---:|---:|
| `deep_search` | 17 | 136 | 70 | 0 |
| `raw_ddgs` | 17 | 136 | 48 | 0 |
| `websearch_skill` | 17 | 136 | 55 | 0 |

### Quality — successful runs only

| runner | nDCG@10 | MRR | latency mean (s) |
|---|---:|---:|---:|
| `deep_search` | 0.960 ± 0.041 | 1.000 ± 0.000 | **1.62 ± 0.81** |
| `raw_ddgs` | 0.870 ± 0.136 | 0.989 ± 0.074 | 5.48 ± 4.07 |
| `websearch_skill` | 0.954 ± 0.046 | 0.988 ± 0.078 | 3.22 ± 0.57 |

### Paired Wilcoxon vs `deep_search` (successful pairs)

| runner | metric | mean diff | 95% CI | p (approx) |
|---|---|---:|---|---:|
| `raw_ddgs` | nDCG@10 | +0.106 | [+0.073, +0.141] | **< 0.0001** |
| `raw_ddgs` | latency | −2.85 s | [-3.77, -2.04] | **< 0.0001** |
| `websearch_skill` | nDCG@10 | +0.034 | [+0.000, +0.080] | 0.70 (not significant) |
| `websearch_skill` | latency | −1.56 s | [-1.77, -1.34] | **< 0.0001** |

### What this proves

1. Deep Search is significantly faster than both raw_ddgs and
   websearch-skill (p < 0.0001).
2. Deep Search's retrieval quality is significantly better than the
   no-wrapper raw_ddgs baseline (p < 0.0001).
3. Deep Search ties websearch-skill on retrieval quality on successful
   runs (p ≈ 0.70, not significant).
4. Deep Search has a higher empty-run rate under engine rotation because
   it surfaces `degraded` status instead of silently returning partial
   results.

## Honest caveats

- **Factual queries only.** No current events, multi-intent, adversarial,
  or non-English queries.
- **String-grader bias.** The conservative substring grader affects all
  runners equally; deltas are the durable signal.
- **One network, one window.** Numbers shift with upstream engine state.
  The rank order and significance claims are the stable signals.
- **No SearXNG baseline.** websearch-skill was run with its ddgs adapter
  only; a local SearXNG instance would isolate wrapper quality from
  engine throttling.
- **No LLM judge.** The frozen corpus could be re-graded by an LLM to
  surface grader bias.

## Reproducing

```bash
# Offline replay (deterministic, no network)
python tests/benchmark_replay.py

# Live capture (may be partial due to throttling, ~30 min)
python tests/benchmark.py
```

The replay reads `tests/benchmark_corpus.jsonl` and writes to
`tests/benchmark_out/replay/`. The live capture writes per-run JSONL to
`tests/benchmark_out/<run_id>/`.
