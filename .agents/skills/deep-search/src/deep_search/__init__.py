"""Lightweight, keyless metasearch for AI-agent research."""

from .engine import DeepSearch, ProviderStatus, SearchResult, SearchRun, canonical, rrf

__all__ = ["DeepSearch", "ProviderStatus", "SearchResult", "SearchRun", "canonical", "rrf"]
__version__ = "0.2.0"
