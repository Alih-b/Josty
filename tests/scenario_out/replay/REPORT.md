# Josty scenario eval

Constraint checks against the frozen scenario corpus. Failures emit a taxonomy class from `docs/ISSUE_TAXONOMY.md`.

**7/10 constraints passed.**

| Case | Layer | Verdict | Class | Pathway |
|---|---|---|---|---|
| `news_token_collision` | news | **fail** | `upstream_quality` | Lexical relevance gate or news-specific ranking; not a ddgs-call bug. |
| `news_near_miss` | news | **fail** | `upstream_quality` | Lexical relevance gate or news-specific ranking; not a ddgs-call bug. |
| `academic_profile_rag` | rank | **fail** | `product_gap` | Stronger academic rerank or hard host floor; 1.4x cannot beat 3-group RRF. |
| `dev_profile_fastapi` | rank | **pass** | `—` | Stronger dev rerank or hard host floor. |
| `site_filter_httpx` | cli | **pass** | `—` | Site post-filter already exists; a leak is a contract bug. |
| `exact_free_threading` | rank | **pass** | `—` | Exact-mode ranking; investigate if docs.python.org drops out. |
| `fetch_rrf` | fetch | **pass** | `—` | Keep 403 / download-limit as fetch_error; skill retries the next URL. |
| `diagnose_reachability` | diagnose | **pass** | `—` | Optional challenged bit or skill text on http_status; not a probe bug. |
| `linux_kernel_year` | fetch | **pass** | `—` | Factual fetch; add RFC-1 if over-constrained queries go empty. |
| `empty_provider_complete` | status | **pass** | `—` | Surface error_kind=empty on ProviderStatus (schema 1.0 compatible). |

## Issues

- `news_token_collision` (upstream_quality): missing answer tokens ['python']
- `news_near_miss` (upstream_quality): missing answer tokens ['3.14']; near-miss: found '3.15' but missing ['3.14']
- `academic_profile_rag` (product_gap): no result on required hosts ('arxiv.org', 'biorxiv.org', 'medrxiv.org', 'ncbi.nlm.nih.gov', 'nih.gov', 'ieee.org')…
