# Issue taxonomy

Use this document to label a live miss **before** changing engine code. The
canonical TREC benchmark ([BENCHMARK.md](BENCHMARK.md)) compares runners on
frozen factual queries. It does not classify news irrelevance, weak profile
ranking, or a `complete` status that an agent will misread.

Every finding gets exactly one class, plus a **confidence** and a **layer**.

## Classes

| Class | Means | Live-run example |
|---|---|---|
| `contract_bug` | Josty promised X in the schema, CLI, or docs and did Y | site leak; non-JSON stdout; search `count` ≠ `len(results)` |
| `intended_misleading` | Code and tests match, but an agent will treat the signal as the wrong kind of success or failure | `--diagnose` HTTP 429 → `ok=true` without reading `challenged`/`http_status`; empty backends → `status=complete` without reading `error_kind` |
| `upstream_quality` | ddgs or the host returned junk; Josty forwarded it faithfully | `ddgs.news("Python 3.14")` → Bay News 9 “District 14” |
| `product_gap` | Faithful and honest, but not useful enough for real agent work | academic profile loses to Wikipedia/AWS; no lexical relevance gate on news |

Do **not** treat a single live empty `google,mojeek,startpage` group as a bug.
That path is `ok=true`, `result_count=0`, `error_kind="empty"` in `_ddgs`.

## Decision tree

1. Did stdout, schema, site filters, or `status` **violate a documented contract**? → `contract_bug`.
2. Else, does the code **explicitly intend** this (comment plus a test)? → `intended_misleading` if an agent would treat it as the wrong success or failure; otherwise not an issue.
3. Else, would a **different wrapper around the same ddgs rows** look the same? → `upstream_quality`.
4. Else Josty could improve fusion or fetch fallback without new keys, daemons, or extra ddgs calls → `product_gap`.

## Confidence and layer

**Confidence**

| Value | Means |
|---|---|
| `reproduced` | Seen on more than one capture or pinned in the frozen scenario corpus |
| `once` | Single live observation |
| `flaky` | Empty or contradictory across repeats (throttle, rotation) |

**Layer** — `cli` / `status` / `rank` / `fetch` / `news` / `diagnose`.

A live flake (`once` or all providers empty) is `upstream_quality` + `flaky`,
never `contract_bug`.

## How to label a live miss

1. Capture the JSON envelope (`josty ...` or `JOSTY_LIVE_EVAL=1 python tests/scenario_eval.py --live`). Live recapture writes under `tests/scenario_out/live/` and does not overwrite the checked-in corpus or `replay/` report.
2. Walk the decision tree. Pick one class.
3. Add a spec to `tests/scenario_queries.py` and a row to `tests/scenario_corpus.jsonl`.
4. Run `python tests/scenario_eval.py` and `pytest -q`. Do not change `engine.py` in the same change unless the class is `contract_bug`.

## Seeded live findings (2026-08-29)

| Case | Layer | Observed | Class | Confidence |
|---|---|---|---|---|
| `news_token_collision` | news | “Python 3.14” news week → District 14, iPhone 14, CD rates | `upstream_quality` | `reproduced` |
| `news_near_miss` | news | “Python 3.14 official release notes” → Python 3.15 only | `upstream_quality` | `reproduced` |
| `academic_profile_rag` | rank | RAG + `--profile academic` → Wikipedia / AWS / IBM, no arXiv | `product_gap` | `reproduced` |
| `dev_profile_fastapi` | rank | FastAPI DI → official docs first | (pass) | `reproduced` |
| `site_filter_httpx` | cli | `--site github.com --site stackoverflow.com` held | (pass) | `reproduced` |
| `exact_free_threading` | rank | exact mode hit docs.python.org / py-free-threading | (pass) | `reproduced` |
| `fetch_rrf` | fetch | Learn.microsoft content + Medium `403` as `fetch_error` | (pass) | `reproduced` |
| `diagnose_reachability` | diagnose | Brave HTTP 429 still `ok=true` | documents contract; `challenged=true` | `reproduced` |
| `linux_kernel_year` | fetch | Wikipedia extract contains 1991 | (pass) | `reproduced` |
| `empty_provider_complete` | status | all `ok`, one `result_count=0` → `complete` | documents contract; `error_kind=empty` | `reproduced` |

## Pathways backlog

The scenario report’s `pathway` column is the improvement backlog. Do not
implement these in the same change as a new eval case unless the class is
`contract_bug`.

**Ship order matches [ROADMAP.md](../ROADMAP.md) (honest wrapper; no extra ddgs calls):**

| Rank | Finding | Pathway | Roadmap ID |
|---|---|---|---|
| done | Empty `complete` + swallowed `error_kind=empty` | Surface `empty` on `ProviderStatus` (schema 1.0 compatible). | RFC-0a |
| done | Hidden query rewrite on 0 hits | Removed. Agents rewrite; josty searches once. | — |
| done | Diagnose 429 `ok=true` | `challenged` bit plus skill text on `http_status`. | RFC-0d |
| done | Unbounded cache | Row cap, hit tracking, envelope `cached`. | RFC-4 |
| skill | News token-collision / near-miss | Skill: require subject tokens before citing. Not an engine filter. | — |
| — | Academic / dev profile losses | Leave ranking as RRF + soft weights. No hard host floor. | — |
| — | Over-constrained 0-hit queries | Caller rewrites the query. Do not implement RFC-1. | — |
| — | Fetch 403 / download-limit | Keep. Skill: retry the next URL. | — |

RFC-2 (`--extract-code`) stays deferred; it is not in the seed scenario set.

## What the scenario eval is not

- Not a replacement for [BENCHMARK.md](BENCHMARK.md) nDCG/MRR replay.
- Not an LLM judge and not a paid search API.
- Constraint checks, not graded relevance. News cases must not use the
  TREC string grader (a hit on `"14"` would false-pass).
