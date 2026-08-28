"""Lightweight, keyless metasearch for AI-agent research."""

from .engine import (
    DeepSearch,
    ProfileType,
    ProviderStatus,
    SearchCache,
    SearchResult,
    SearchRun,
    canonical,
    domain_weight,
    merge_query_variants,
    normalize_sites,
    rrf,
)

__all__ = [
    "DeepSearch",
    "ProfileType",
    "ProviderStatus",
    "SearchCache",
    "SearchResult",
    "SearchRun",
    "canonical",
    "domain_weight",
    "merge_query_variants",
    "normalize_sites",
    "rrf",
]
__version__ = "0.3.0"
