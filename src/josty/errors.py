"""Search and diagnose error classification, and provider status aggregation."""

from __future__ import annotations

import re
import socket
import ssl

import httpx
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from .models import ProviderStatus, SearchResult, WireFacts
from .ranking import canonical
from .status import ErrorKind


class ProviderError(Exception):
    """A failure whose kind the adapter knows without any classification.

    A native source sees things the exception types cannot say: Wikipedia's
    ``list=search`` API reporting hits and returning no rows is a parse
    problem, not "nothing found" (#102). ``error_kind`` is final.
    """

    def __init__(self, error_kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


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
    "not_applicable": 1,
    "unknown": 2,
    "parse": 3,
    "network": 4,
    "filtered": 4,
    "rate_limited": 5,
    "blocked": 5,
    "geo": 5,
}
_FAILURE_ERROR_KINDS = frozenset(
    {"unknown", "parse", "network", "filtered", "rate_limited", "blocked", "geo"}
)

# -- wire evidence (#99) ---------------------------------------------------------
# ddgs answers every non-200 with None and a flat "No results found.", so the
# status itself decides what a zero-row call was. 503 sits with the blocks: an
# engine behind an anti-bot wall says "service unavailable" to a client it
# refuses, not to one it cannot serve.
_BLOCKED_HTTP_STATUSES = frozenset({202, 401, 403, 503})
#: A 200 body larger than this that parsed to zero rows is a parser miss, not an
#: index miss: a real "nothing found" page is small (Yahoo's second markup
#: variant arrives at 145 KB).
_PARSE_BODY_BYTES = 100_000
# Addresses a public search host only resolves to when a carrier's DNS filter
# answers for it; the connection then dies before any HTTP response (the
# Iran-direct Startpage route, 2026-11-26). Extend only from a capture.
_FILTER_SINKHOLES = frozenset({"10.10.34.36", "2001:4188:2:600:10:10:34:36"})
# Hosts whose 403 is a country decision, established by a two-egress probe
# (HN Algolia: 403 from Iran direct, 8/10 answered through a GB VPN). One 403
# alone is never read as geo: Google's is a wall for everyone.
_GEO_BLOCKED_HOSTS = frozenset({"hn.algolia.com"})
# An interstitial is recognised by its title or by challenge-specific markup.
# A bare "captcha" or "challenge" substring is not evidence: real result pages
# carry both words in snippets and script names.
_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.IGNORECASE)
_CHALLENGE_TITLE_RE = re.compile(
    r"captcha|just a moment|attention required|access denied|blocked|forbidden|"
    r"are you human|not a bot|security check|one more step|verifying (?:your|you are)",
    re.IGNORECASE,
)
_CHALLENGE_MARKUP_RE = re.compile(
    r"g-recaptcha|h-captcha|hcaptcha\.com|cf-chl|cf-turnstile|challenge-platform"
    r"|anubis_puzzle|anubis_challenge",
    re.IGNORECASE,
)


def _is_challenge_page(body: str, query: str | None = None) -> bool:
    """True when a 200 body is an interstitial (captcha, Anubis, Cloudflare).

    The query is cut out of the title first. A results page carries its query
    there, so a zero-row search for "nginx 403 forbidden" would otherwise read
    as a wall.
    """
    title = _TITLE_RE.search(body)
    words = title.group(1) if title else ""
    if query:
        words = re.sub(re.escape(query), "", words, flags=re.IGNORECASE)
    return bool(_CHALLENGE_TITLE_RE.search(words) or _CHALLENGE_MARKUP_RE.search(body))


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
        # No variant ran: keep why, a cool-down skip or a filter the source
        # cannot honour alike.
        error_kind = statuses[0].error_kind or "skipped"
        error = next((status.error for status in statuses if status.error), None)
    latencies = [s.latency_ms for s in statuses if s.latency_ms is not None]
    # Worst-case across concurrent query variants, not a representative sample.
    # Concurrent variants overlap, so a mean would understate the stall; max is
    # the time the slowest variant actually occupied.
    latency_ms = round(max(latencies), 2) if latencies else None
    # circuit_state/failures/backoff_remaining are intentionally NOT aggregated
    # from per-variant snapshots (they run concurrently, so snapshot order is
    # not recency): Fanout.search (fanout.py) stamps them from a fresh
    # breaker.get_state() after the gather.
    # Wire evidence (#99). http_status/body_bytes come from the first refused
    # variant, else the first that reached the wire; refusal_rate is the mean of
    # the variants' own rates, each the share of that call's HTTP sub-requests
    # that got a non-200.
    seen = [status for status in statuses if status.http_status is not None]
    rates = [status.refusal_rate for status in seen if status.refusal_rate is not None]
    witness = next((s for s in failures if s.http_status is not None), seen[0] if seen else None)
    return ProviderStatus(
        provider,
        query,
        ok,
        result_count,
        error=error,
        error_kind=error_kind,
        latency_ms=latency_ms,
        http_status=witness.http_status if witness else None,
        body_bytes=witness.body_bytes if witness else None,
        refusal_rate=round(sum(rates) / len(rates), 3) if rates else None,
    )


def _zero_row_kind(facts: WireFacts) -> ErrorKind | None:
    """Kind of a call that parsed no rows, judged by what the wire said.

    None when the wire has nothing to say (no HTTP response was observed).
    """
    if not facts.responses:
        return None
    codes = {response.status_code for response in facts.responses}
    if 429 in codes:
        return "rate_limited"
    if codes & _BLOCKED_HTTP_STATUSES:
        if 403 in codes and facts.host in _GEO_BLOCKED_HOSTS:
            return "geo"
        return "blocked"
    if any(500 <= code < 600 for code in codes):
        return "network"
    if codes - {200}:
        return "parse"
    if any(response.challenged for response in facts.responses):
        return "blocked"
    if (facts.body_bytes or 0) > _PARSE_BODY_BYTES:
        return "parse"
    return "empty"


def _describe_failure(exc: BaseException, facts: WireFacts | None, kind: ErrorKind) -> str:
    """The failure text: ddgs's exception, prefixed by what the wire said."""
    base = f"{type(exc).__name__}: {exc}"
    if facts is None:
        return base
    if kind == "filtered":
        where = ", ".join(facts.resolved)
        return f"no HTTP response from {facts.host}; resolved to {where} ({base})"
    if facts.http_status is not None:
        detail = f"HTTP {facts.http_status} from {facts.host} ({facts.body_bytes} bytes)"
        return f"{detail}, 0 rows parsed ({base})"
    return base


def _classify_search_error(
    exc: BaseException, facts: WireFacts | None = None
) -> ErrorKind:
    """Map a ddgs-side (or GitHub-API) exception to an error_kind category.

    Status codes are classified first. Message tokens are word-boundary matched
    so fragments like ``blocked`` do not fire on ``unblocked``. HTTP 401/403 are
    ``blocked`` (auth/forbidden), not ``rate_limited`` — a persistently 403
    backend must not be treated as throttling. ddgs 9.15.0 wraps engine
    exceptions in a flat ``DDGSException`` whose ``str`` contains the original
    exception's repr but does not chain ``__cause__``/``__context__``.

    ``facts`` is what the adapter saw on the wire (#99). It outranks the message
    tokens where it can: a host that resolved into a filter sinkhole is
    ``filtered`` whatever ddgs wrote, and a zero-row call is whatever the status
    said. Without facts the message alone decides, as before.
    """
    if isinstance(exc, ProviderError):
        return exc.error_kind
    if facts is not None and not facts.responses and set(facts.resolved) & _FILTER_SINKHOLES:
        return "filtered"
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
            if facts is None:
                return "empty"
            return _zero_row_kind(facts) or "empty"
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
