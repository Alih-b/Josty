"""Lightweight, keyless metasearch for AI-agent research."""

from .engine import (
    DeepSearch,
    ProviderStatus,
    SearchResult,
    SearchRun,
    canonical,
    merge_query_variants,
    normalize_sites,
    rrf,
)

__all__ = [
    "DeepSearch",
    "ProviderStatus",
    "SearchResult",
    "SearchRun",
    "canonical",
    "merge_query_variants",
    "normalize_sites",
    "rrf",
]
__version__ = "0.3.0"
