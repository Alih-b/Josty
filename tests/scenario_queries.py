"""Constraint specs for the live-scenario eval.

These are not TREC graded queries. Each case asserts host, token, status,
or fetch constraints against a frozen (or optionally live) JSON envelope.
When a constraint fails, emit ``label_if_fail`` from docs/ISSUE_TAXONOMY.md.

Do not reuse ``benchmark_grade.string_grade`` here: a news hit on ``"14"``
would false-pass token-collision cases.
"""

from __future__ import annotations

from typing import Any

# Subset of engine.AUTHORITATIVE_DOMAINS_ACADEMIC used as a hard host floor
# for the academic-profile scenario. Keep in sync when that set changes.
ACADEMIC_HOSTS: tuple[str, ...] = (
    "arxiv.org",
    "biorxiv.org",
    "medrxiv.org",
    "ncbi.nlm.nih.gov",
    "nih.gov",
    "ieee.org",
    "ieeexplore.ieee.org",
    "acm.org",
    "dl.acm.org",
    "nature.com",
    "science.org",
    "springer.com",
    "sciencedirect.com",
    "semanticscholar.org",
    "openalex.org",
    "paperswithcode.com",
    "openreview.net",
    "aclweb.org",
    "neurips.cc",
    "icml.cc",
    "iclr.cc",
)

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "news_token_collision",
        "layer": "news",
        "query": "Python 3.14",
        "flags": {"category": "news", "timelimit": "w", "limit": 5},
        "min_results": 1,
        "expect_status": "complete",
        "must_answer": ["python"],
        "label_if_fail": "upstream_quality",
        "pathway": "Lexical relevance gate or news-specific ranking; not a ddgs-call bug.",
        "notes": "Live capture returned District 14 / iPhone 14 / CD rates.",
    },
    {
        "id": "news_near_miss",
        "layer": "news",
        "query": "Python 3.14 official release notes",
        "flags": {"category": "news", "timelimit": "m", "limit": 5},
        "min_results": 1,
        "expect_status": "complete",
        "must_answer": ["3.14"],
        "forbid_if_missing_must": ["3.15"],
        "label_if_fail": "upstream_quality",
        "pathway": "Lexical relevance gate or news-specific ranking; not a ddgs-call bug.",
        "notes": "Live capture returned Python 3.15 only.",
    },
    {
        "id": "academic_profile_rag",
        "layer": "rank",
        "query": "retrieval augmented generation",
        "flags": {"profile": "academic", "limit": 5},
        "min_results": 1,
        "expect_status": "complete",
        "must_hosts": ACADEMIC_HOSTS,
        "label_if_fail": "product_gap",
        "pathway": "Stronger academic rerank or hard host floor; 1.4x cannot beat 3-group RRF.",
        "notes": "Live capture ranked Wikipedia / AWS / IBM / NVIDIA / Google Cloud.",
    },
    {
        "id": "dev_profile_fastapi",
        "layer": "rank",
        "query": "FastAPI dependency injection",
        "flags": {"profile": "dev", "limit": 5},
        "min_results": 1,
        "expect_status": "complete",
        "must_hosts": ("fastapi.tiangolo.com",),
        "label_if_fail": "product_gap",
        "pathway": "Stronger dev rerank or hard host floor.",
    },
    {
        "id": "site_filter_httpx",
        "layer": "cli",
        "query": "httpx connection reset",
        "flags": {
            "sites": ["github.com", "stackoverflow.com"],
            "limit": 5,
        },
        "min_results": 1,
        "expect_status": "complete",
        "allowed_hosts": ("github.com", "stackoverflow.com"),
        "label_if_fail": "contract_bug",
        "pathway": "Site post-filter already exists; a leak is a contract bug.",
    },
    {
        "id": "exact_free_threading",
        "layer": "rank",
        "query": "free-threaded Python",
        "flags": {"mode": "exact", "limit": 4},
        "min_results": 1,
        "expect_status": "complete",
        "must_hosts": ("docs.python.org", "py-free-threading.github.io"),
        "label_if_fail": "product_gap",
        "pathway": "Exact-mode ranking; investigate if docs.python.org drops out.",
    },
    {
        "id": "fetch_rrf",
        "layer": "fetch",
        "query": "RRF rank fusion algorithm",
        "flags": {"fetch": True, "limit": 2, "max_content_chars": 1500},
        "min_results": 1,
        "expect_status": "complete",
        "fetch_content_or_error": True,
        "label_if_fail": "contract_bug",
        "pathway": "Keep 403 / download-limit as fetch_error; skill retries the next URL.",
    },
    {
        "id": "diagnose_reachability",
        "layer": "diagnose",
        "query": "",
        "flags": {"diagnose": True},
        "diagnose": True,
        "min_hosts": 1,
        "require_reachable_field": True,
        "http_error_still_ok": True,
        "label_if_fail": "intended_misleading",
        "pathway": "Optional challenged bit or skill text on http_status; not a probe bug.",
        "notes": "Documents current contract: any HTTP response including 429 is ok=true.",
    },
    {
        "id": "linux_kernel_year",
        "layer": "fetch",
        "query": "Linux kernel first released year Linus Torvalds",
        "flags": {"fetch": True, "limit": 3, "max_content_chars": 1000},
        "min_results": 1,
        "expect_status": "complete",
        "must_answer": ["1991"],
        "search_content": True,
        "label_if_fail": "product_gap",
        "pathway": "Factual fetch; add RFC-1 if over-constrained queries go empty.",
    },
    {
        "id": "empty_provider_complete",
        "layer": "status",
        "query": "synthetic empty-success fixture",
        "flags": {},
        "live": False,
        "min_results": 0,
        "expect_status": "complete",
        "require_empty_ok_provider": True,
        "label_if_fail": "intended_misleading",
        "pathway": "Surface error_kind=empty on ProviderStatus (schema 1.0 compatible).",
        "notes": "Pins intended status: all ok, one result_count=0 → complete.",
    },
]


def scenario_by_id(case_id: str) -> dict[str, Any]:
    for spec in SCENARIOS:
        if spec["id"] == case_id:
            return spec
    raise KeyError(case_id)
