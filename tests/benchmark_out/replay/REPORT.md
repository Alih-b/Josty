# Deep Search benchmark — frozen-corpus replay

**Corpus:** `tests\benchmark_corpus.jsonl`

**Grader:** string predicates from `benchmark_grade.string_grade` (canonical URL = 3, answer string = 2, weak subject = 1, else 0).

**Runners:** deep_search, raw_ddgs, websearch_skill

**Total scored (runner × query × repeat) tuples:** 408

**Backend rotation:** each query slot uses one of three predefined backend groups cycled by index; `slot_backends` is logged per row in the JSONL.

**Statistical test:** paired Wilcoxon signed-rank, two-sided, deep_search as baseline. p-values are a normal approximation when n_nonzero ≥ 10; otherwise the test reports no p-value.


## Coverage matrix

| runner | distinct queries | runs | empty | errors |
|---|---:|---:|---:|---:|
| `deep_search` | 17 | 136 | 70 | 0 |
| `raw_ddgs` | 17 | 136 | 48 | 0 |
| `websearch_skill` | 17 | 136 | 55 | 0 |

## Aggregate IR metrics — ALL runs (mean ± std)

Empty runs (engine returned 0 results, usually throttled) count as zeros.

| runner | nDCG@10 | MRR | P@5 | graded_recall@10 | latency mean (s) |
|---|---|---|---|---|---|
| `deep_search` | 0.466 ± 0.480 (n=136) | 0.485 ± 0.500 (n=136) | 0.468 ± 0.485 (n=136) | 0.485 ± 0.500 (n=136) | 2.692 ± 2.770 (n=136) |
| `raw_ddgs` | 0.563 ± 0.430 (n=136) | 0.640 ± 0.476 (n=136) | 0.624 ± 0.465 (n=136) | 0.465 ± 0.399 (n=136) | 5.443 ± 3.981 (n=136) |
| `websearch_skill` | 0.568 ± 0.469 (n=136) | 0.588 ± 0.488 (n=136) | 0.577 ± 0.478 (n=136) | 0.596 ± 0.491 (n=136) | 4.409 ± 2.893 (n=136) |

## Aggregate IR metrics — SUCCESSFUL runs only

Filters out empty runs so engine quality is judged separately from reliability.

| runner | nDCG@10 | MRR | P@5 | graded_recall@10 | latency mean (s) |
|---|---|---|---|---|---|
| `deep_search` | 0.960 ± 0.041 (n=66) | 1.000 ± 0.000 (n=66) | 0.964 ± 0.077 (n=66) | 1.000 ± 0.000 (n=66) | 1.619 ± 0.812 (n=66) |
| `raw_ddgs` | 0.870 ± 0.136 (n=88) | 0.989 ± 0.074 (n=88) | 0.964 ± 0.077 (n=88) | 0.719 ± 0.253 (n=88) | 5.476 ± 4.068 (n=88) |
| `websearch_skill` | 0.954 ± 0.046 (n=81) | 0.988 ± 0.078 (n=81) | 0.968 ± 0.073 (n=81) | 1.000 ± 0.000 (n=81) | 3.219 ± 0.570 (n=81) |

## Paired Wilcoxon signed-rank tests vs `deep_search`

diffs = deep_search − other. Positive ΔnDCG@10 / ΔMRR means deep_search is *better*; positive Δlatency means deep_search is *slower*. Two views: `all_paired` includes empty runs (zero scores); `successful_paired` keeps only pairs where both runners returned >0 results.

### View: `successful_paired`

| runner | metric | mean diff | 95% CI | n_pairs | W | p (approx) | note |
|---|---|---:|---|---:|---:|---:|---|
| `raw_ddgs` | `ndcg_at_10` | +0.1055 | [+0.073, +0.141] | 66 | 50.0 | 0.0000 | normal approximation, n>=10 |
| `raw_ddgs` | `mrr` | +0.0152 | [+0.000, +0.038] | 66 | 0 | n/a | n too small for normal approximation; treat p as 'large' |
| `raw_ddgs` | `wall_clock_seconds` | -2.8530 | [-3.771, -2.040] | 66 | 70.0 | 0.0000 | normal approximation, n>=10 |
| `websearch_skill` | `ndcg_at_10` | +0.0339 | [+0.000, +0.080] | 66 | 214.0 | 0.7036 | normal approximation, n>=10 |
| `websearch_skill` | `mrr` | +0.0455 | [+0.008, +0.099] | 66 | 0 | n/a | n too small for normal approximation; treat p as 'large' |
| `websearch_skill` | `wall_clock_seconds` | -1.5603 | [-1.770, -1.342] | 66 | 55.0 | 0.0000 | normal approximation, n>=10 |

### View: `all_paired`

| runner | metric | mean diff | 95% CI | n_pairs | W | p (approx) | note |
|---|---|---:|---|---:|---:|---:|---|
| `raw_ddgs` | `ndcg_at_10` | -0.0969 | [-0.163, -0.036] | 136 | 1175.0 | 0.5551 | normal approximation, n>=10 |
| `raw_ddgs` | `mrr` | -0.1544 | [-0.221, -0.096] | 136 | 3.0 | 0.0000 | normal approximation, n>=10 |
| `raw_ddgs` | `wall_clock_seconds` | -2.7511 | [-3.471, -2.055] | 136 | 855.0 | 0.0000 | normal approximation, n>=10 |
| `websearch_skill` | `ndcg_at_10` | -0.1024 | [-0.164, -0.047] | 136 | 271.0 | 0.0019 | normal approximation, n>=10 |
| `websearch_skill` | `mrr` | -0.1029 | [-0.165, -0.044] | 136 | 27.0 | 0.0021 | normal approximation, n>=10 |
| `websearch_skill` | `wall_clock_seconds` | -1.7164 | [-2.200, -1.258] | 136 | 1186.0 | 0.0000 | normal approximation, n>=10 |

## Honest caveats

- **Frozen corpus, not live runs.** This report scores responses captured against the full 20-query set with 8 repeats each; any throttle-induced gap was logged with `error` or `result_count == 0`, never silently filled.

- **Engine rotation by query slot.** Each query uses one of `bing,brave,duckduckgo`, `google,mojeek,startpage`, or `yandex,yahoo`; the slot index is logged as `slot_backends` per row. This is the dominant confounder when comparing runs with uneven engine exposure.

- **String-grader bias.** Grading is conservative substring matching; bias affects all runners equally, so deltas are the durable signal.

- **Wilcoxon normal approximation** is reported for n_nonzero ≥ 10. For very small n the p-value is null and the table says so.

- **One network, one run window.** Engine state was identical across runners for each query within a slot; absolute numbers will shift on a different day but the rank order is the durable signal.

