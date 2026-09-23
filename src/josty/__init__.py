"""Keyless search tool and bounded text extraction for agents and scripts."""

from ._version import __version__
from .breaker import CircuitBreaker
from .cache import SearchCache
from .engine import Josty
from .models import DiagnoseRun, HostStatus, ProviderStatus, SearchResult, SearchRun
from .ranking import canonical, domain_weight, merge_query_variants, normalize_sites, rrf
from .status import ProfileType

__all__ = [
    "CircuitBreaker",
    "DiagnoseRun",
    "HostStatus",
    "Josty",
    "ProfileType",
    "ProviderStatus",
    "SearchCache",
    "SearchResult",
    "SearchRun",
    "__version__",
    "canonical",
    "domain_weight",
    "merge_query_variants",
    "normalize_sites",
    "rrf",
]
