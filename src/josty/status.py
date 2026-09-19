"""Shared type aliases, run-level status values, and cross-cutting constants."""

from __future__ import annotations

from datetime import timezone
from typing import Literal

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc



from ._version import __version__

SearchMode = Literal["plain", "exact", "oss"]
SearchCategory = Literal["text", "news"]
SafeSearch = Literal["on", "moderate", "off"]
TimeLimit = Literal["d", "w", "m", "y"]
ErrorKind = Literal["network", "rate_limited", "blocked", "empty", "parse", "unknown", "skipped"]
ProfileType = Literal["general", "dev", "academic"]

SCHEMA_VERSION = "1.0"


class SearchStatus:
    """Run-level search status values; the wire schema stays 1.0."""

    COMPLETE = "complete"
    DEGRADED = "degraded"
    EMPTY = "empty"
    FAILED = "failed"


MAX_SITES = 5
CHALLENGED_HTTP_STATUSES = frozenset({401, 403, 429})
DIAGNOSE_NOTE = (
    "HTTPS homepage reachability only; not search-backend health. "
    "Search can succeed while this reports failed or degraded."
)
# Headroom over the DDGS client timeout so ddgs's own TimeoutException fires
# first where possible; asyncio.wait_for is the outer belt, not the inner one.
SEARCH_THREAD_TIMEOUT_HEADROOM = 2.0
USER_AGENT = f"josty/{__version__} (+https://github.com/Alih-b/Josty)"
