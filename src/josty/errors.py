"""Search and diagnose error classification, and provider status aggregation."""

from __future__ import annotations

import re
import socket
import ssl
from datetime import timezone

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc

import httpx
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from .models import ProviderStatus, SearchResult
from .ranking import canonical
from .status import ErrorKind


def _classify_probe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        if isinstance(exc.__cause__, (ssl.SSLError, ssl.SSLCertVerificationError)):
            return "tls"
        return "dns" if isinstance(exc.__cause__, socket.gaierror) else "network"
    if isinstance(exc, httpx.HTTPError):
        return "network"
    return "unknown"


# Word-boundary token matchers. Substring tokens like "blocked" must not match
# "unblocked", and broad fragments like "too many" / "challenge" are omitted
# because they misfire on unrelated network/parse errors.
_RATE_LIMIT_TOKEN_RE = re.compile(
    r"(?<![\w])(?:rate[\s_-]?limit|too many requests|429)(?![\w])",
    re.IGNORECASE,
)
_BLOCKED_TOKEN_RE = re.compile(
    r"(?<![\w])(?:403|401|forbidden|captcha|access denied|blocked)(?![\w])",
    re.IGNORECASE,
)
_NETWORK_TOKEN_RE = re.compile(
    r"(?<![\w])(?:connecterror|connection refused|connection reset|dns|"
    r"getaddrinfo|name or service not known|timed out|timeout|network)(?![\w])",
    re.IGNORECASE,
)
_TLS_TOKEN_RE = re.compile(
    r"(?<![\w])(?:decodeerror|invalid peer certificate|certificate verify|"
    r"handshake failure|ssl|tls)(?![\w])",
    re.IGNORECASE,
)
_PARSE_TOKEN_RE = re.compile(
    r"(?<![\w])(?:failed to fetch|parse|decode|json)(?![\w])",
    re.IGNORECASE,
)
_EMPTY_RESULTS_MESSAGE = "no results found"


# Severity order for aggregating real failure kinds across query variants.
# empty/skipped are not failures: they must not outrank a clean hit.
_ERROR_KIND_SEVERITY: dict[str | None, int] = {
    None: -1,
    "empty": 0,
    "skipped": 1,
    "unknown": 2,
    "parse": 3,
    "network": 4,
    "rate_limited": 5,
    "blocked": 5,
}
_FAILURE_ERROR_KINDS = frozenset({"unknown", "parse", "network", "rate_limited", "blocked"})


def _aggregate_engine_status(
    statuses: list[ProviderStatus], item_lists: list[list[SearchResult]]
) -> ProviderStatus:
    """Collapse one engine's per-variant statuses into a single per-engine entry.

    ``ok`` is true when any variant reached the engine; ``result_count`` counts
    distinct canonical URLs across all variants (a URL found by two variants is
    one result). Real failures (``unknown`` / ``parse`` / ``network`` /
    ``rate_limited``) win so partial throttling stays visible. ``empty`` is only
    set when the engine was reached and produced zero URLs. ``skipped`` is only
    set when no variant reached the engine. A clean hit does not inherit
    empty/skip from a sibling variant.
    """
    provider = statuses[0].provider
    query = statuses[0].query
    ok = any(status.ok for status in statuses)
    seen_urls: set[str] = set()
    for items in item_lists:
        for item in items:
            try:
                seen_urls.add(canonical(item.url))
            except ValueError:
                continue
    result_count = len(seen_urls)
    failures = [status for status in statuses if status.error_kind in _FAILURE_ERROR_KINDS]
    if failures:
        error_kind = max(
            (status.error_kind for status in failures),
            key=lambda kind: _ERROR_KIND_SEVERITY.get(kind, -1),
        )
        error = next((status.error for status in failures if status.error), None)
    elif result_count > 0:
        error_kind = None
        error = None
    elif ok:
        error_kind = "empty"
        error = None
    else:
        error_kind = "skipped"
        error = next((status.error for status in statuses if status.error), None)
    latencies = [s.latency_ms for s in statuses if s.latency_ms is not None]
    # Worst-case across concurrent query variants, not a representative sample.
    # Concurrent variants overlap, so a mean would understate the stall; max is
    # the time the slowest variant actually occupied.
    latency_ms = round(max(latencies), 2) if latencies else None
    # circuit_state/failures/backoff_remaining are intentionally NOT aggregated
    # from per-variant snapshots (they run concurrently, so snapshot order is
    # not recency): _search_parts stamps them from a fresh breaker.get_state()
    # after the gather.
    return ProviderStatus(
        provider,
        query,
        ok,
        result_count,
        error=error,
        error_kind=error_kind,
        latency_ms=latency_ms,
    )


def _classify_search_error(exc: BaseException) -> ErrorKind:
    """Map a ddgs-side (or GitHub-API) exception to an error_kind category.

    Status codes are classified first. Message tokens are word-boundary matched
    so fragments like ``blocked`` do not fire on ``unblocked``. HTTP 401/403 are
    ``blocked`` (auth/forbidden), not ``rate_limited`` — a persistently 403
    backend must not be treated as throttling. ddgs 9.15.0 wraps engine
    exceptions in a flat ``DDGSException`` whose ``str`` contains the original
    exception's repr but does not chain ``__cause__``/``__context__``.
    """
    if isinstance(exc, TimeoutException):
        return "network"
    if isinstance(exc, RatelimitException):
        return "rate_limited"
    if isinstance(exc, httpx.TimeoutException):
        return "network"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else 0
        if status == 429:
            return "rate_limited"
        if status in (401, 403):
            return "blocked"
        if 500 <= status < 600:
            return "network"
        return "parse"
    if isinstance(exc, httpx.HTTPError):
        return "network"
    if isinstance(exc, DDGSException):
        text = str(exc).lower()
        if _EMPTY_RESULTS_MESSAGE in text:
            return "empty"
        if _RATE_LIMIT_TOKEN_RE.search(text):
            return "rate_limited"
        if _BLOCKED_TOKEN_RE.search(text):
            return "blocked"
        if _TLS_TOKEN_RE.search(text):
            return "network"
        if _NETWORK_TOKEN_RE.search(text):
            return "network"
        if _PARSE_TOKEN_RE.search(text):
            return "parse"
    return "unknown"
