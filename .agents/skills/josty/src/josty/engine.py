from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import sqlite3
import ssl
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc

import httpx
from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

SearchMode = Literal["plain", "exact", "oss"]
SearchCategory = Literal["text", "news"]
SafeSearch = Literal["on", "moderate", "off"]
TimeLimit = Literal["d", "w", "m", "y"]
ErrorKind = Literal["network", "rate_limited", "empty", "parse", "unknown", "skipped"]
ProfileType = Literal["general", "dev", "academic"]

SCHEMA_VERSION = "1.0"
MAX_SITES = 5
CACHE_MAX_ROWS = 5000
CACHE_PRUNE_BATCH = 500
CACHE_MAX_BYTES = 50_000_000
CHALLENGED_HTTP_STATUSES = frozenset({401, 403, 429})
_ALLOWED_FETCH_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain"}
)
# Headroom over the DDGS client timeout so ddgs's own TimeoutException fires
# first where possible; asyncio.wait_for is the outer belt, not the inner one.
SEARCH_THREAD_TIMEOUT_HEADROOM = 2.0

# Single version source: the static literal doubles as the pre-install fallback and
# hatchling's build-time version; installed distributions override via importlib.metadata.
__version__ = "0.5.0"
with suppress(PackageNotFoundError):
    __version__ = version("josty")
USER_AGENT = f"josty/{__version__} (+https://github.com/Alih-b/josty)"
BROWSER_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}
TRACKING_QUERY_KEYS = {
    "dclid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
}

_EXTRACT_LOCK: threading.Lock = threading.Lock()

AUTHORITATIVE_DOMAINS_GENERAL = {
    "github.com",
    "gitlab.com",
    "stackoverflow.com",
    "superuser.com",
    "serverfault.com",
    "developer.mozilla.org",
    "wikipedia.org",
    "python.org",
    "pypi.org",
    "rust-lang.org",
    "crates.io",
    "go.dev",
    "golang.org",
    "archlinux.org",
    "kernel.org",
    "w3.org",
    "ietf.org",
}

# Retained for backwards compatibility
AUTHORITATIVE_DOMAINS = AUTHORITATIVE_DOMAINS_GENERAL

AUTHORITATIVE_DOMAINS_DEV = {
    "github.com",
    "github.io",
    "gitlab.com",
    "bitbucket.org",
    "codeberg.org",
    "stackoverflow.com",
    "superuser.com",
    "serverfault.com",
    "developer.mozilla.org",
    "python.org",
    "pypi.org",
    "rust-lang.org",
    "crates.io",
    "go.dev",
    "golang.org",
    "pkg.go.dev",
    "npmjs.com",
    "rubygems.org",
    "packagist.org",
    "nuget.org",
    "archlinux.org",
    "kernel.org",
    "w3.org",
    "ietf.org",
    "man7.org",
    "react.dev",
    "reactjs.org",
    "vuejs.org",
    "angular.dev",
    "angular.io",
    "svelte.dev",
    "nextjs.org",
    "djangoproject.com",
    "rubyonrails.org",
    "fastapi.tiangolo.com",
    "flask.palletsprojects.com",
    "spring.io",
    "docker.com",
    "kubernetes.io",
    "apache.org",
    "postgresql.org",
    "sqlite.org",
    "redis.io",
    "mongodb.com",
    "linux.die.net",
    # Modern AI/ML & LLM Frameworks & Hubs
    "huggingface.co",
    "hf.co",
    "pytorch.org",
    "tensorflow.org",
    "keras.io",
    "paperswithcode.com",
    "kaggle.com",
    "ollama.com",
    "vllm.ai",
    "unsloth.ai",
    "modal.com",
    "triton-lang.org",
    "qdrant.tech",
    "milvus.io",
    "weaviate.io",
    # Modern Language Toolchains, Web & Cloud
    "astral.sh",
    "bun.sh",
    "deno.com",
    "deno.land",
    "ziglang.org",
    "biomejs.dev",
    "tailwindcss.com",
    "shadcn.com",
    "prisma.io",
    "supabase.com",
    "trpc.io",
    "cloudflare.com",
    "tailscale.com",
    "fly.io",
    "val.town",
}

AUTHORITATIVE_DOMAINS_ACADEMIC = {
    "arxiv.org",
    "biorxiv.org",
    "medrxiv.org",
    "ncbi.nlm.nih.gov",
    "nih.gov",
    "nlm.nih.gov",
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
    "doi.org",
    "crossref.org",
    "jstor.org",
    "plos.org",
    "cell.com",
    "oup.com",
    "tandfonline.com",
    "wiley.com",
    "frontiersin.org",
    "mdpi.com",
    "pnas.org",
    "cambridge.org",
    "thelancet.com",
    # Top AI/ML Conferences & Preprints
    "openreview.net",
    "paperswithcode.com",
    "chemrxiv.org",
    "hal.science",
    "aclweb.org",
    "neurips.cc",
    "icml.cc",
    "iclr.cc",
}

SPAM_DOMAINS = {
    "pinterest.com",
    "quora.com",
    "softonic.com",
    "ehow.com",
    "geeksforgeeks.org",
    "experts-exchange.com",
}


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    sources: list[str] = field(default_factory=list)
    published_at: str | None = None
    publisher: str | None = None
    score: float = 0.0
    content: str | None = None
    extraction_method: str | None = None
    fetched_url: str | None = None
    fetched_at: str | None = None
    fetch_error: str | None = None
    engine_ranks: dict[str, int] = field(default_factory=dict)
    rank_contributions: dict[str, float] = field(default_factory=dict)
    score_weights: dict[str, float] = field(default_factory=dict)

    def dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderStatus:
    provider: str
    query: str
    ok: bool
    result_count: int = 0
    error: str | None = None
    error_kind: ErrorKind | None = None
    latency_ms: float | None = None
    circuit_state: str | None = None
    failures: int | None = None
    backoff_remaining: float | None = None

    def dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HostStatus:
    provider: str
    host: str
    ok: bool
    http_status: int | None = None
    error_kind: Literal["timeout", "dns", "tls", "network", "unknown", "skipped"] | None = None
    error: str | None = None
    challenged: bool = False
    latency_ms: float | None = None
    circuit_state: str | None = None
    failures: int | None = None
    backoff_remaining: float | None = None

    def dict(self) -> dict[str, Any]:
        return asdict(self)



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


_RATE_LIMIT_TOKENS = (
    "rate limit",
    "too many",
    "429",
    "too many requests",
    "403",
    "forbidden",
    "challenge",
    "captcha",
    "blocked",
)
_NETWORK_TOKENS = (
    "connecterror",
    "connection refused",
    "connection reset",
    "dns",
    "getaddrinfo",
    "name or service not known",
    "timed out",
    "timeout",
    "network",
)
_TLS_TOKENS = (
    "decodeerror",
    "invalid peer certificate",
    "certificate verify",
    "handshake failure",
    "ssl",
    "tls",
)
_PARSE_TOKENS = ("failed to fetch", "parse", "decode", "json")
_EMPTY_RESULTS_MESSAGE = "no results found"

try:
    from ddgs.engines import ENGINES as _DDGS_ENGINES
    try:
        # Re-register Google when an installed ddgs dropped it from its engine
        # table (9.15.0 deprecation). This mutates the ddgs global engine map
        # for the whole process — deliberate, since josty is the process's
        # search layer. Any layout change upstream is swallowed: registration
        # is best-effort, never a startup failure.
        from ddgs.engines.google import Google as _GoogleEngine

        if (
            _DDGS_ENGINES is not None
            and "text" in _DDGS_ENGINES
            and "google" not in _DDGS_ENGINES["text"]
        ):
            _DDGS_ENGINES["text"]["google"] = _GoogleEngine
    except Exception:
        pass
except Exception:
    _DDGS_ENGINES = None

_KNOWN_TEXT_ENGINES = frozenset(
    {
        "bing",
        "brave",
        "duckduckgo",
        "google",
        "grokipedia",
        "mojeek",
        "startpage",
        "wikipedia",
        "yahoo",
        "yandex",
    }
)
_KNOWN_NEWS_ENGINES = frozenset({"bing", "duckduckgo", "yahoo"})
# Both frozensets are a best-effort fallback for the case where the
# ddgs.engines registry cannot be imported at all (non-standard ddgs layout).
# They can drift from ddgs releases; the live registry is authoritative
# whenever the import succeeds.


def _engine_available(category: SearchCategory, name: str) -> tuple[bool, str | None]:
    """Check engine availability without calling ddgs.

    ddgs silently drops unknown or disabled engine names inside a group call and
    falls back to ``backend="auto"`` (all engines) when none match — a silent
    amplification and downgrade trap. Checking availability here lets a dead or
    misspelled engine be skipped with a visible status instead.
    """
    if _DDGS_ENGINES is not None:
        if name in _DDGS_ENGINES.get(category, {}):
            return True, None
        return False, f"skipped: engine '{name}' is not enabled in the installed ddgs"
    known = _KNOWN_NEWS_ENGINES if category == "news" else _KNOWN_TEXT_ENGINES
    if name in known:
        return True, None
    return False, f"skipped: unknown engine '{name}'"


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
}
_FAILURE_ERROR_KINDS = frozenset({"unknown", "parse", "network", "rate_limited"})


def _content_type_allowed(header: str | None) -> bool:
    """Return True when the Content-Type media type is an allowed fetch type."""
    if not header or not header.strip():
        return False
    media = header.split(";", 1)[0].strip().lower()
    return media in _ALLOWED_FETCH_CONTENT_TYPES


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

    ddgs 9.15.0 wraps engine exceptions in a flat ``DDGSException`` whose ``str``
    contains the original exception's repr (e.g. ``"ConnectError: ...(Connection refused)"``)
    but does not chain the original via ``__cause__``/``__context__``. We therefore
    use ``isinstance`` for the outer class where we can, and substring-match the
    flattened message otherwise. See issue #9 for the research behind this mapping.
    """
    if isinstance(exc, TimeoutException):
        return "network"
    if isinstance(exc, RatelimitException):
        return "rate_limited"
    if isinstance(exc, httpx.TimeoutException):
        return "network"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else 0
        if status in (429, 403) or 500 <= status < 600:
            return "rate_limited" if status in (429, 403) else "network"
        return "parse"
    if isinstance(exc, httpx.HTTPError):
        return "network"
    if isinstance(exc, DDGSException):
        text = str(exc).lower()
        if _EMPTY_RESULTS_MESSAGE in text:
            return "empty"
        if any(token in text for token in _RATE_LIMIT_TOKENS):
            return "rate_limited"
        if any(token in text for token in _TLS_TOKENS):
            return "network"
        if any(token in text for token in _NETWORK_TOKENS):
            return "network"
        if any(token in text for token in _PARSE_TOKENS):
            return "parse"
    return "unknown"


@dataclass
class DiagnoseRun:
    providers: list[HostStatus] = field(default_factory=list)

    @property
    def reachable(self) -> int:
        return sum(provider.ok for provider in self.providers)

    @property
    def status(self) -> str:
        if not self.providers or self.reachable == 0:
            return "failed"
        if self.reachable < len(self.providers):
            return "degraded"
        return "complete"

    def dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "reachable": self.reachable,
            "count": len(self.providers),
            "providers": [provider.dict() for provider in self.providers],
        }


@dataclass
class SearchRun:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    providers: list[ProviderStatus] = field(default_factory=list)
    cached: bool = False
    run_at: str | None = None  # ISO8601 UTC moment the search was executed

    @property
    def partial(self) -> bool:
        # A provider is a failed branch when its call failed outright, or when it
        # answered but an aggregated query variant failed (ok=true with a failure
        # error_kind). "empty" is a successful empty branch, not a failure.
        return any(
            not provider.ok or provider.error_kind not in (None, "empty")
            for provider in self.providers
        )

    @property
    def status(self) -> str:
        if not self.results and self.providers and all(not item.ok for item in self.providers):
            return "failed"
        if self.partial:
            return "degraded"
        return "complete"

    def dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "query": self.query,
            "status": self.status,
            "count": len(self.results),
            "partial": self.partial,
            "cached": self.cached,
            "providers": [provider.dict() for provider in self.providers],
            "results": [result.dict() for result in self.results],
        }
        if self.run_at is not None:
            payload["run_at"] = self.run_at
        return payload


def canonical(url: str) -> str:
    """Normalize URL variants while preserving resource-identifying query parameters."""
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or not hostname or parsed.username or parsed.password:
        raise ValueError("canonical URLs must be public-style HTTP(S) URLs")
    port = parsed.port
    hostname = f"[{hostname}]" if ":" in hostname else hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
        ],
        doseq=True,
    )
    return urlunsplit((scheme, hostname, path, query, ""))


def _clone(item: SearchResult) -> SearchResult:
    return replace(
        item,
        sources=list(item.sources),
        engine_ranks=dict(item.engine_ranks),
        rank_contributions=dict(item.rank_contributions),
        score_weights=dict(item.score_weights),
    )


def _merge_result(current: SearchResult, candidate: SearchResult) -> None:
    current.sources = list(dict.fromkeys([*current.sources, *candidate.sources]))
    if len(candidate.snippet) > len(current.snippet):
        current.snippet = candidate.snippet
        current.title = candidate.title or current.title
    current.published_at = current.published_at or candidate.published_at
    current.publisher = current.publisher or candidate.publisher
    # Only engine_ranks merge here; rank_contributions and score_weights are
    # derived from engine_ranks (and the profile weight) inside rrf().
    for engine, rank in candidate.engine_ranks.items():
        if engine not in current.engine_ranks or rank < current.engine_ranks[engine]:
            current.engine_ranks[engine] = rank


def domain_weight(url: str, profile: ProfileType = "general") -> float:
    """Return ranking multiplier for authoritative vs spam domains based on profile."""
    try:
        hostname = (urlsplit(url).hostname or "").lower()
    except Exception:
        return 1.0
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if not hostname:
        return 1.0

    def _matches_any(domains: set[str]) -> bool:
        return any(hostname == d or hostname.endswith("." + d) for d in domains)

    if _matches_any(SPAM_DOMAINS):
        return 0.5 if profile in ("dev", "academic") else 0.6

    if profile == "academic":
        if _matches_any(AUTHORITATIVE_DOMAINS_ACADEMIC):
            return 1.4
        if (
            hostname.startswith("docs.")
            or hostname.endswith(".readthedocs.io")
            or _matches_any(AUTHORITATIVE_DOMAINS_GENERAL)
        ):
            return 1.2
        return 1.0

    if profile == "dev":
        if (
            hostname.startswith("docs.")
            or hostname.endswith(".readthedocs.io")
            or _matches_any(AUTHORITATIVE_DOMAINS_DEV)
        ):
            return 1.3
        if _matches_any(AUTHORITATIVE_DOMAINS_GENERAL):
            return 1.2
        return 1.0

    # general (default)
    if (
        hostname.startswith("docs.")
        or hostname.endswith(".readthedocs.io")
        or _matches_any(AUTHORITATIVE_DOMAINS_GENERAL)
    ):
        return 1.2

    return 1.0


def rrf(
    ranked: list[list[SearchResult]],
    k: int = 60,
    profile: ProfileType = "general",
) -> list[SearchResult]:
    """Fuse independent backend-group ranked lists with Reciprocal Rank Fusion.

    Each engine contributes at most one vote per URL, taken from its 1-indexed
    discovery rank in ``engine_ranks`` (min-merged across lists). The fused
    score is ``round(domain_weight * sum(round(1/(k + rank_e), 6) for e), 6)``
    so a caller can verify the score from the recorded attribution alone.
    Engine agreement therefore counts: a URL found by two engines of the same
    group carries both votes. That is the deliberate per-engine fusion
    semantics (see PROJECT.md, "Transparent RRF Attribution Contract").
    """
    if k < 1:
        raise ValueError("k must be positive")
    merged: dict[str, SearchResult] = {}

    for results in ranked:
        seen_in_list: set[str] = set()
        for rank, item in enumerate(results, 1):
            try:
                key = canonical(item.url)
            except ValueError:
                continue
            if not key or key in seen_in_list:
                continue
            seen_in_list.add(key)
            # Rank-less sources adopt the position of their first occurrence
            # so every engine named in ``sources`` carries a rank.
            for s in item.sources:
                if s not in item.engine_ranks:
                    item.engine_ranks[s] = rank
            if key not in merged:
                merged[key] = _clone(item)
            else:
                _merge_result(merged[key], item)

    for item in merged.values():
        w = domain_weight(item.url, profile=profile)
        item.score_weights = {"k": float(k), "domain_weight": w}
        item.rank_contributions = {
            engine: round(1.0 / (k + rank), 6) for engine, rank in item.engine_ranks.items()
        }
        item.score = round(w * sum(item.rank_contributions.values()), 6)

    return sorted(
        merged.values(),
        key=lambda item: (-item.score, canonical(item.url)),
    )


def merge_query_variants(ranked: list[list[SearchResult]]) -> list[SearchResult]:
    """Merge query rewrites for one backend without counting them as independent votes."""
    merged: dict[str, tuple[int, int, SearchResult]] = {}
    for variant_index, results in enumerate(ranked):
        for rank, item in enumerate(results, 1):
            try:
                key = canonical(item.url)
            except ValueError:
                continue
            for s in item.sources:
                if s not in item.engine_ranks:
                    item.engine_ranks[s] = rank
            current = merged.get(key)
            if current is None:
                merged[key] = (rank, variant_index, _clone(item))
                continue
            best_rank, best_variant, saved = current
            _merge_result(saved, item)
            merged[key] = (min(rank, best_rank), min(variant_index, best_variant), saved)
    return [
        item
        for _, _, item in sorted(
            merged.values(),
            key=lambda row: (row[0], row[1], canonical(row[2].url)),
        )
    ]


def normalize_sites(sites: list[str] | None) -> list[str]:
    if not sites:
        return []
    if len(sites) > MAX_SITES:
        raise ValueError(f"at most {MAX_SITES} site filters are allowed")
    normalized: list[str] = []
    for raw in sites:
        site = raw.strip().lower().rstrip(".")
        if site.startswith("www."):
            site = site[4:]
        labels = site.split(".")
        if (
            not site
            or "://" in site
            or "/" in site
            or ":" in site
            or not re.fullmatch(r"[a-z0-9.-]+", site)
            or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in labels
            )
        ):
            raise ValueError(f"invalid site filter: {raw}")
        normalized.append(site)
    return list(dict.fromkeys(normalized))


def _site_matches(url: str, sites: list[str]) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return any(hostname == site or hostname.endswith(f".{site}") for site in sites)


class SearchCache:
    """Lightweight SQLite-backed cache for search runs with TTL."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        default_ttl: float = 21600.0,
        max_rows: int = CACHE_MAX_ROWS,
        prune_batch: int = CACHE_PRUNE_BATCH,
        max_bytes: int = CACHE_MAX_BYTES,
    ):
        self.disabled = False
        self.db_path: Path | None
        if db_path is None:
            cache_dir = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "josty"
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                self.db_path = cache_dir / "cache.db"
            except Exception:
                # Fail closed: never fall back to a world-shared /tmp path.
                self.disabled = True
                self.db_path = None
        else:
            self.db_path = Path(db_path)
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                self.disabled = True
                self.db_path = None
        self.default_ttl = default_ttl
        self.max_rows = max_rows
        self.prune_batch = prune_batch
        self.max_bytes = max_bytes
        if not self.disabled:
            self._init_db()
            self._restrict_db_mode()

    def _restrict_db_mode(self) -> None:
        if self.db_path is None:
            return
        with suppress(OSError):
            if self.db_path.exists():
                os.chmod(self.db_path, 0o600)

    def _get_conn(self) -> sqlite3.Connection:
        if self.disabled or self.db_path is None:
            raise sqlite3.OperationalError("search cache is disabled")
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        with suppress(Exception), self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS search_cache (
                    key TEXT PRIMARY KEY,
                    created_at REAL,
                    expires_at REAL,
                    payload TEXT,
                    hit_count INTEGER DEFAULT 0,
                    last_accessed REAL DEFAULT 0
                );
                """
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(search_cache)").fetchall()
            }
            if "hit_count" not in columns:
                conn.execute(
                    "ALTER TABLE search_cache ADD COLUMN hit_count INTEGER DEFAULT 0"
                )
            if "last_accessed" not in columns:
                conn.execute(
                    "ALTER TABLE search_cache ADD COLUMN last_accessed REAL DEFAULT 0"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_expires_at ON search_cache(expires_at);"
            )

    @staticmethod
    def hash_key(query: str, **kwargs: Any) -> str:
        serialized = json.dumps(
            {"q": query.strip().lower(), **kwargs}, sort_keys=True, default=str
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        if self.disabled:
            return None
        try:
            now = time.time()
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT payload, expires_at FROM search_cache WHERE key = ?",
                    (key,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                payload_str, expires_at = row
                if expires_at < now:
                    conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))
                    return None
                conn.execute(
                    """
                    UPDATE search_cache
                    SET hit_count = COALESCE(hit_count, 0) + 1, last_accessed = ?
                    WHERE key = ?
                    """,
                    (now, key),
                )
                try:
                    return json.loads(payload_str)
                except Exception:
                    conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))
                    return None
        except Exception:
            return None

    def set(self, key: str, payload: dict[str, Any], ttl: float | None = None) -> None:
        if self.disabled:
            return
        with suppress(Exception), self._get_conn() as conn:
            now = time.time()
            expires = now + (ttl if ttl is not None else self.default_ttl)
            payload_str = json.dumps(payload, ensure_ascii=False)
            conn.execute(
                """
                INSERT OR REPLACE INTO search_cache
                    (key, created_at, expires_at, payload, hit_count, last_accessed)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (key, now, expires, payload_str, now),
            )
            self._prune_if_needed(conn)

    def _sum_payload_bytes(self, conn: sqlite3.Connection) -> int:
        # LENGTH(CAST(... AS BLOB)) counts bytes; plain LENGTH(TEXT) counts
        # characters, which undercounts multi-byte UTF-8 (emoji, CJK) by up to 4x.
        return int(
            conn.execute(
                "SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))), 0) FROM search_cache"
            ).fetchone()[0]
        )

    def _prune_if_needed(self, conn: sqlite3.Connection) -> None:
        if self.max_rows < 1 or self.prune_batch < 1:
            return
        count = conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
        overflow = count - self.max_rows
        if overflow > 0:
            limit = min(self.prune_batch, overflow)
            # Nested subquery: SQLite cannot DELETE FROM a table while the same
            # table is used in a plain IN-select with ORDER BY/LIMIT.
            conn.execute(
                """
                DELETE FROM search_cache WHERE key IN (
                    SELECT key FROM (
                        SELECT key FROM search_cache
                        ORDER BY expires_at ASC, hit_count ASC
                        LIMIT ?
                    )
                )
                """,
                (limit,),
            )
        if self.max_bytes and self.max_bytes > 0:
            total = self._sum_payload_bytes(conn)
            while total > self.max_bytes:
                conn.execute(
                    """
                    DELETE FROM search_cache WHERE key IN (
                        SELECT key FROM (
                            SELECT key FROM search_cache
                            ORDER BY expires_at ASC, hit_count ASC
                            LIMIT ?
                        )
                    )
                    """,
                    (self.prune_batch,),
                )
                new_total = self._sum_payload_bytes(conn)
                if new_total >= total:
                    break
                total = new_total

    def stats(self) -> dict[str, int]:
        """Aggregate cache telemetry: row count, payload bytes, and cumulative hits."""
        if self.disabled:
            return {"rows": 0, "bytes": 0, "hits": 0}
        try:
            with self._get_conn() as conn:
                rows, payload_bytes, hits = conn.execute(
                    """
                    SELECT COUNT(*),
                           COALESCE(SUM(LENGTH(CAST(payload AS BLOB))), 0),
                           COALESCE(SUM(COALESCE(hit_count, 0)), 0)
                    FROM search_cache
                    """
                ).fetchone()
                return {"rows": int(rows), "bytes": int(payload_bytes), "hits": int(hits)}
        except Exception:
            return {"rows": 0, "bytes": 0, "hits": 0}

    def clear(self) -> None:
        if self.disabled:
            return
        with suppress(Exception), self._get_conn() as conn:
            conn.execute("DELETE FROM search_cache;")

    def delete(self, key: str) -> None:
        """Evict a specific cache entry (e.g. on corruption or invalidation)."""
        if self.disabled:
            return
        with suppress(Exception), self._get_conn() as conn:
            conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))


_FETCH_ONLY_FIELDS = ("content", "extraction_method", "fetched_url", "fetched_at", "fetch_error")

# Freshness ceilings (seconds): the OLDEST a cached result may be when served.
# The effective TTL is min(configured default, ceiling) — the rule can only ever
# shorten, so a caller-configured shorter cache_ttl always wins.
CACHE_TTL_MAX_DAY = 1800.0    # timelimit=d: "today's news" must not be hours old
CACHE_TTL_MAX_WEEK = 7200.0   # timelimit=w
CACHE_TTL_MAX_NEWS = 3600.0   # category=news without timelimit


def _ttl_for(category: str, timelimit: str | None, default: float) -> float:
    if timelimit == "d":
        return min(default, CACHE_TTL_MAX_DAY)
    if timelimit == "w":
        return min(default, CACHE_TTL_MAX_WEEK)
    if category == "news":
        return min(default, CACHE_TTL_MAX_NEWS)
    return default


def _strip_fetch_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Blank per-result fetch fields so cached payloads stay small (SERPs, not page text).

    Mutates ``payload`` in place; callers pass a freshly built ``run.dict()``.
    Keys stay present as ``null`` so the schema contract is stable.
    """
    for result in payload.get("results", []):
        for field_name in _FETCH_ONLY_FIELDS:
            result[field_name] = None
    return payload


def _search_run_from_dict(payload: dict[str, Any]) -> SearchRun:
    if not isinstance(payload, dict):
        raise ValueError("Invalid payload: expected dict")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raw_results = []
    results: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        engine_ranks_raw = item.get("engine_ranks")
        engine_ranks: dict[str, int] = {}
        if isinstance(engine_ranks_raw, dict):
            for k, v in engine_ranks_raw.items():
                try:
                    engine_ranks[str(k)] = int(v)
                except (ValueError, TypeError):
                    continue
        rank_contribs_raw = item.get("rank_contributions")
        rank_contributions: dict[str, float] = {}
        if isinstance(rank_contribs_raw, dict):
            for k, v in rank_contribs_raw.items():
                try:
                    contrib = float(v)
                except (ValueError, TypeError):
                    continue
                if math.isfinite(contrib):
                    rank_contributions[str(k)] = contrib
        score_weights_raw = item.get("score_weights")
        score_weights: dict[str, float] = {}
        if isinstance(score_weights_raw, dict):
            for k, v in score_weights_raw.items():
                try:
                    weight = float(v)
                except (ValueError, TypeError):
                    continue
                if math.isfinite(weight):
                    score_weights[str(k)] = weight
        try:
            score = float(item.get("score", 0.0))
            if not math.isfinite(score):
                score = 0.0
        except (ValueError, TypeError):
            score = 0.0
        raw_sources = item.get("sources")
        if isinstance(raw_sources, list):
            sources = [str(s) for s in raw_sources]
        elif isinstance(raw_sources, str):
            sources = [raw_sources]
        else:
            sources = []
        results.append(
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("snippet") or ""),
                sources=sources,
                published_at=item.get("published_at"),
                publisher=item.get("publisher"),
                score=score,
                content=item.get("content"),
                extraction_method=item.get("extraction_method"),
                fetched_url=item.get("fetched_url"),
                fetched_at=item.get("fetched_at"),
                fetch_error=item.get("fetch_error"),
                engine_ranks=engine_ranks,
                rank_contributions=rank_contributions,
                score_weights=score_weights,
            )
        )
    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, list):
        raw_providers = []
    providers: list[ProviderStatus] = []
    for p in raw_providers:
        if not isinstance(p, dict):
            continue
        raw_ok = p.get("ok", True)
        if isinstance(raw_ok, str):
            ok = raw_ok.strip().lower() not in ("false", "0", "no", "")
        else:
            ok = bool(raw_ok)
        try:
            rc = int(p.get("result_count", 0))
        except (ValueError, TypeError):
            rc = 0
        try:
            lat = float(p["latency_ms"]) if p.get("latency_ms") is not None else None
            if lat is not None and not math.isfinite(lat):
                lat = None
        except (ValueError, TypeError):
            lat = None
        try:
            fails = int(p["failures"]) if p.get("failures") is not None else None
        except (ValueError, TypeError):
            fails = None
        try:
            bo = float(p["backoff_remaining"]) if p.get("backoff_remaining") is not None else None
            if bo is not None and not math.isfinite(bo):
                bo = None
        except (ValueError, TypeError):
            bo = None
        cs = p.get("circuit_state")
        if cs not in ("closed", "open", "half-open"):
            cs = "closed" if cs is not None else None
        providers.append(
            ProviderStatus(
                provider=str(p.get("provider", "")),
                query=str(p.get("query", "")),
                ok=ok,
                result_count=rc,
                error=p.get("error"),
                error_kind=p.get("error_kind"),
                latency_ms=lat,
                circuit_state=cs,
                failures=fails,
                backoff_remaining=bo,
            )
        )
    return SearchRun(
        query=str(payload.get("query", "")),
        results=results,
        providers=providers,
        cached=bool(payload.get("cached", False)),
        run_at=payload.get("run_at"),
    )


class CircuitBreaker:
    """In-process per-(backend, error_class) tri-state circuit breaker.

    State lifecycle:
    - CLOSED: Normal operation. If failures in sliding window reach ``fail_threshold``,
      the circuit trips to OPEN.
    - OPEN: Calls are blocked with a cool-down timestamp. Consecutive trips apply
      exponential backoff. When cool-down expires, transitions to HALF_OPEN.
    - HALF_OPEN: Allows trial probe(s). Success resets circuit to CLOSED and clears
      failure history and consecutive trips. Failure trips back to OPEN.

    ``error_class`` exists for contract compatibility: ``"rate_limit"`` and
    ``"search"`` are aliases sharing one failure namespace, and the default
    follows the PROJECT.md contract (``"rate_limit"``).
    """

    def __init__(
        self,
        *,
        fail_threshold: int = 3,
        window_seconds: float = 60.0,
        cool_down_seconds: float = 30.0,
    ):
        if fail_threshold < 1 or window_seconds <= 0 or cool_down_seconds <= 0:
            raise ValueError("breaker thresholds must be positive")
        self.fail_threshold = int(fail_threshold)
        self.window_seconds = float(window_seconds)
        self.cool_down_seconds = float(cool_down_seconds)
        self._state: dict[tuple[str, str], str] = {}
        self._failures: dict[tuple[str, str], list[float]] = {}
        self._open_until: dict[tuple[str, str], float] = {}
        self._consecutive_trips: dict[tuple[str, str], int] = {}
        self._latencies: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(backend: str, error_class: str) -> tuple[str, str]:
        if error_class == "rate_limit":
            error_class = "search"
        return (backend, error_class)

    def status(self, backend: str, error_class: str = "rate_limit") -> tuple[bool, str | None]:
        """Return ``(allowed, skip_message)`` for a backend/error pair.

        HALF_OPEN deliberately admits the full concurrent fanout as trial
        probes (no in-flight cap): a struggling backend gets one full probe
        round when its cool-down expires, and the first failure re-trips it.
        Complete per-engine visibility is preferred over conservative probe
        limiting.
        """
        now = time.monotonic()
        key = self._key(backend, error_class)

        with self._lock:
            if self._state.get(key) == "open":
                open_until = self._open_until.get(key, 0.0)
                if now < open_until:
                    until_iso = (
                        datetime.fromtimestamp(time.time() + (open_until - now), UTC)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                    return False, f"skipped: engine in cool-down until {until_iso}"
                # Cool down elapsed -> transition to half-open and reset failure window
                self._state[key] = "half-open"
                self._failures[key] = []
                return True, None

            if self._state.get(key) == "half-open":
                return True, None

            return True, None

    def record_failure(self, backend: str, error_class: str = "rate_limit") -> None:
        """Record a failure event for backend/error_class within sliding window."""
        now = time.monotonic()
        key = self._key(backend, error_class)

        with self._lock:
            # Freeze timer while open; do not extend on repeated failures during cool-down
            if self._state.get(key) == "open" and now < self._open_until.get(key, 0.0):
                return

            # If cool-down elapsed while open, transition to half-open and clear stale failures
            if self._state.get(key) == "open" and now >= self._open_until.get(key, 0.0):
                self._state[key] = "half-open"
                self._failures[key] = []

            is_half_open = self._state.get(key) == "half-open"

            events = [t for t in self._failures.get(key, []) if now - t <= self.window_seconds]
            events.append(now)
            self._failures[key] = events

            if is_half_open or len(events) >= self.fail_threshold:
                trips = self._consecutive_trips.get(key, 0) + 1
                self._consecutive_trips[key] = trips
                backoff = self.cool_down_seconds * (2 ** min(trips - 1, 6))
                self._open_until[key] = now + backoff
                self._state[key] = "open"

    def record_success(self, backend: str, error_class: str = "rate_limit") -> None:
        """Record a success event, resetting circuit state to closed and clearing history."""
        key = self._key(backend, error_class)
        with self._lock:
            self._state[key] = "closed"
            self._failures[key] = []
            self._open_until[key] = 0.0
            self._consecutive_trips[key] = 0

    def record_latency(self, backend: str, latency_ms: float) -> None:
        """Record the most recent execution latency for a backend."""
        with self._lock:
            self._latencies[backend] = float(latency_ms)

    def get_state(self, backend: str) -> dict[str, Any]:
        """Return standardized circuit breaker telemetry for a backend."""
        now = time.monotonic()
        with self._lock:
            matching_keys = [k for k in self._state if k[0] == backend]
            if not matching_keys:
                matching_keys = [k for k in self._failures if k[0] == backend]

            for k in matching_keys:
                if self._state.get(k) == "open" and now >= self._open_until.get(k, 0.0):
                    self._state[k] = "half-open"
                    self._failures[k] = []

            state = "closed"
            backoff_remaining = 0.0
            failures = 0

            for k in matching_keys:
                if self._state.get(k) == "open":
                    state = "open"
                    rem = max(0.0, self._open_until.get(k, 0.0) - now)
                    if rem > backoff_remaining:
                        backoff_remaining = rem
                    active_fails = len(
                        [t for t in self._failures.get(k, []) if now - t <= self.window_seconds]
                    )
                    if active_fails > failures:
                        failures = active_fails

            if state == "closed":
                for k in matching_keys:
                    if self._state.get(k) == "half-open":
                        state = "half-open"
                        break

            if failures == 0:
                for k in matching_keys:
                    active_fails = len(
                        [t for t in self._failures.get(k, []) if now - t <= self.window_seconds]
                    )
                    if active_fails > failures:
                        failures = active_fails

            return {
                "state": state,
                "failures": failures,
                "backoff_remaining": round(backoff_remaining, 2),
                "last_latency_ms": self._latencies.get(backend),
            }


class Josty:
    """Small, bounded metasearch querying backend groups in parallel with
    group-level RRF fusion and safe text extraction."""

    DEFAULT_BACKENDS = (
        "brave,duckduckgo",
        "google,mojeek,startpage",
        "yahoo",
    )
    DEFAULT_NEWS_BACKENDS = ("bing,duckduckgo,yahoo",)

    BACKEND_HOSTS = {
        "bing": "www.bing.com",
        "brave": "search.brave.com",
        "duckduckgo": "duckduckgo.com",
        "google": "www.google.com",
        "mojeek": "www.mojeek.com",
        "startpage": "www.startpage.com",
        "yandex": "yandex.com",
        "yahoo": "search.yahoo.com",
        "wikipedia": "en.wikipedia.org",
        "grokipedia": "grokipedia.com",
        "github-api": "api.github.com",
    }

    DEFAULT_SEARCH_CONCURRENCY = 6
    DEFAULT_FETCH_CONCURRENCY = 4
    DEFAULT_BREAKER_FAIL_THRESHOLD = 3
    DEFAULT_BREAKER_WINDOW_SECONDS = 60
    DEFAULT_BREAKER_COOL_DOWN_SECONDS = 30

    def __init__(
        self,
        *,
        timeout: float = 8,
        max_concurrency: int | None = None,
        max_search_concurrency: int = 6,
        max_fetch_concurrency: int = 4,
        max_download_bytes: int = 2_000_000,
        max_content_chars: int | None = 50_000,
        max_query_variants: int | None = None,
        github_token: str | None = None,
        backends: tuple[str, ...] | None = None,
        news_backends: tuple[str, ...] | None = None,
        profile: ProfileType = "general",
        enable_cache: bool = True,
        cache_ttl: float = 21600.0,
        cache_db: Path | str | None = None,
        cache_max_bytes: int = CACHE_MAX_BYTES,
        breaker: CircuitBreaker | None = None,
        breaker_fail_threshold: int = 3,
        breaker_window_seconds: float = 60,
        breaker_cool_down_seconds: float = 30,
    ):
        if timeout <= 0 or max_search_concurrency < 1 or max_fetch_concurrency < 1:
            raise ValueError("timeout and concurrency limits must be positive")
        if max_query_variants is not None and max_query_variants < 1:
            raise ValueError("max_query_variants must be positive")
        if max_concurrency is not None:
            max_search_concurrency = max_concurrency
        if max_download_bytes < 1 or (max_content_chars is not None and max_content_chars < 0):
            raise ValueError("content limits must be positive")
        if profile not in ("general", "dev", "academic"):
            raise ValueError(f"unsupported profile: {profile}")
        self.timeout = timeout
        self.max_search_concurrency = max_search_concurrency
        self.max_fetch_concurrency = max_fetch_concurrency
        self.max_download_bytes = max_download_bytes
        self.max_content_chars = max_content_chars
        self.max_query_variants = max_query_variants
        self.github_token = github_token
        self.backends = backends or self.DEFAULT_BACKENDS
        self.news_backends = news_backends or (
            backends if backends is not None else self.DEFAULT_NEWS_BACKENDS
        )
        self.profile = profile
        self.enable_cache = enable_cache
        self.cache_ttl = cache_ttl
        self.cache = (
            SearchCache(cache_db, default_ttl=cache_ttl, max_bytes=cache_max_bytes)
            if enable_cache
            else None
        )
        self._search_sem: asyncio.Semaphore | None = None
        self._fetch_sem: asyncio.Semaphore | None = None
        if breaker is not None:
            self.breaker = breaker
        else:
            self.breaker = CircuitBreaker(
                fail_threshold=breaker_fail_threshold,
                window_seconds=breaker_window_seconds,
                cool_down_seconds=breaker_cool_down_seconds,
            )

    def clear_cache(self) -> None:
        if self.cache:
            self.cache.clear()

    def cache_stats(self) -> dict[str, int]:
        """Aggregate cache telemetry; all zeros when the cache is disabled."""
        if self.cache:
            return self.cache.stats()
        return {"rows": 0, "bytes": 0, "hits": 0}

    def breaker_status(self, backend: str | None = None) -> dict[str, Any]:
        """Return circuit breaker status for a specific backend or all configured backends."""
        if backend is not None:
            return self.breaker.get_state(backend)
        all_backends: set[str] = set()
        for group in (*self.backends, *self.news_backends):
            for name in group.split(","):
                name = name.strip()
                if name:
                    all_backends.add(name)
        all_backends.add("github-api")
        return {b: self.breaker.get_state(b) for b in sorted(all_backends)}

    def _breaker_telemetry(self, backend: str) -> dict[str, Any]:
        """Return standardized circuit breaker telemetry kwargs for a backend."""
        b = self.breaker.get_state(backend)
        return {
            "circuit_state": b["state"],
            "failures": b["failures"],
            "backoff_remaining": b["backoff_remaining"],
        }

    def _search_semaphore(self) -> asyncio.Semaphore:
        if self._search_sem is None:
            self._search_sem = asyncio.Semaphore(self.max_search_concurrency)
        return self._search_sem

    def _fetch_semaphore(self) -> asyncio.Semaphore:
        if self._fetch_sem is None:
            self._fetch_sem = asyncio.Semaphore(self.max_fetch_concurrency)
        return self._fetch_sem

    @staticmethod
    def expand(
        query: str,
        sites: list[str] | None = None,
        mode: SearchMode = "plain",
        max_query_variants: int | None = None,
    ) -> list[str]:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if mode not in ("plain", "exact", "oss"):
            raise ValueError(f"unsupported search mode: {mode}")
        if max_query_variants is not None and max_query_variants < 1:
            raise ValueError("max_query_variants must be positive")
        variants = [query]
        if mode == "exact":
            variants.append(f'"{query}"')
        elif mode == "oss":
            variants.extend((f'"{query}"', f"{query} open source", f"{query} self-hosted"))
        normalized_sites = normalize_sites(sites)
        if normalized_sites:
            variants = [
                f"site:{site} {variant}"
                for site in normalized_sites
                for variant in variants
            ]
        deduped = list(dict.fromkeys(variants))
        if max_query_variants is not None:
            return deduped[:max_query_variants]
        return deduped

    async def _ddgs(
        self,
        query: str,
        backend: str,
        limit: int,
        *,
        category: SearchCategory,
        region: str | None,
        safesearch: SafeSearch,
        timelimit: TimeLimit | None,
    ) -> tuple[list[SearchResult], ProviderStatus]:
        available, unavailable_message = _engine_available(category, backend)
        if not available:
            return [], ProviderStatus(
                backend,
                query,
                False,
                error=unavailable_message,
                error_kind="skipped",
                **self._breaker_telemetry(backend),
            )
        allowed, skip_message = self.breaker.status(backend, "search")
        if not allowed:
            return [], ProviderStatus(
                backend,
                query,
                False,
                error=skip_message,
                error_kind="skipped",
                **self._breaker_telemetry(backend),
            )
        async with self._search_semaphore():

            cancelled = threading.Event()

            def run() -> tuple[list[SearchResult], float, Exception | None]:
                t0 = time.perf_counter()
                try:
                    # A fresh DDGS client per call is deliberate, not waste:
                    # ddgs engine instances carry a shared cached_property lxml
                    # parser, which is not thread-safe. Caching one client per
                    # backend would let concurrent query variants of the same
                    # engine parse HTML on one parser — the same C-level
                    # corruption class already fixed for trafilatura extraction.
                    ddgs = DDGS(timeout=self.timeout)
                    method = ddgs.news if category == "news" else ddgs.text
                    kwargs: dict[str, Any] = {
                        "backend": backend,
                        "max_results": limit,
                        "safesearch": safesearch,
                    }
                    if region:
                        kwargs["region"] = region
                    if timelimit:
                        kwargs["timelimit"] = timelimit
                    rows = method(query, **kwargs)
                    if cancelled.is_set():
                        return [], round((time.perf_counter() - t0) * 1000, 2), None
                    results = []
                    rank = 1
                    for row in rows:
                        result_url = row.get("href") or row.get("url") or ""
                        if result_url and not self._is_ad_redirect(result_url):
                            results.append(
                                SearchResult(
                                    title=row.get("title", ""),
                                    url=result_url,
                                    snippet=row.get("body", ""),
                                    sources=[backend],
                                    published_at=row.get("date"),
                                    publisher=row.get("source"),
                                    engine_ranks={backend: rank},
                                )
                            )
                            rank += 1
                    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                    return results, latency_ms, None
                except Exception as exc:
                    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                    return [], latency_ms, exc

            t_start = time.perf_counter()
            try:
                results, latency_ms, exc = await asyncio.wait_for(
                    asyncio.to_thread(run), timeout=self.timeout + SEARCH_THREAD_TIMEOUT_HEADROOM
                )
            except (asyncio.TimeoutError, TimeoutError):
                cancelled.set()
                latency_ms = round((time.perf_counter() - t_start) * 1000, 2)
                self.breaker.record_latency(backend, latency_ms)
                self.breaker.record_failure(backend, "search")
                return [], ProviderStatus(
                    backend,
                    query,
                    False,
                    0,
                    error="TimeoutError: search backend timed out",
                    error_kind="network",
                    latency_ms=latency_ms,
                    **self._breaker_telemetry(backend),
                )
            except BaseException:
                cancelled.set()
                raise

            if exc is not None:
                self.breaker.record_latency(backend, latency_ms)
                err_kind = _classify_search_error(exc)
                if err_kind == "empty":
                    # Empty-ok branches do not clear rate-limit history: a
                    # throttled engine answering with zero results must not
                    # reset its own trip window.
                    return [], ProviderStatus(
                        backend,
                        query,
                        True,
                        0,
                        error_kind="empty",
                        latency_ms=latency_ms,
                        **self._breaker_telemetry(backend),
                    )
                self.breaker.record_failure(backend, "search")
                return [], ProviderStatus(
                    backend,
                    query,
                    False,
                    error=f"{type(exc).__name__}: {exc}",
                    error_kind=err_kind,
                    latency_ms=latency_ms,
                    **self._breaker_telemetry(backend),
                )

            self.breaker.record_latency(backend, latency_ms)
            # Empty-ok branches do not clear rate-limit history (see above):
            # only a variant that actually produced results resets the breaker.
            if results:
                self.breaker.record_success(backend, "search")
            return results, ProviderStatus(
                backend,
                query,
                True,
                len(results),
                error_kind="empty" if not results else None,
                latency_ms=latency_ms,
                **self._breaker_telemetry(backend),
            )

    async def _search_parts(
        self,
        query: str,
        *,
        sites: list[str] | None,
        mode: SearchMode,
        limit: int,
        category: SearchCategory,
        region: str | None,
        safesearch: SafeSearch,
        timelimit: TimeLimit | None,
        max_query_variants: int | None = None,
    ) -> tuple[list[list[SearchResult]], list[ProviderStatus], list[str]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if category not in ("text", "news"):
            raise ValueError(f"unsupported search category: {category}")
        if safesearch not in ("on", "moderate", "off"):
            raise ValueError(f"unsupported safe-search mode: {safesearch}")
        if timelimit not in (None, "d", "w", "m", "y"):
            raise ValueError(f"unsupported time limit: {timelimit}")
        effective_max_variants = (
            max_query_variants if max_query_variants is not None else self.max_query_variants
        )
        if effective_max_variants is not None and effective_max_variants < 1:
            raise ValueError("max_query_variants must be positive")
        normalized_sites = normalize_sites(sites)
        queries = self.expand(
            query,
            normalized_sites,
            mode,
            max_query_variants=effective_max_variants,
        )
        groups = self.news_backends if category == "news" else self.backends
        engine_specs = []
        seen_engines: set[str] = set()
        for group_index, group in enumerate(groups):
            for name in group.split(","):
                name = name.strip()
                # An engine listed in multiple groups is queried once, in its
                # first group: one call and one status per engine, contract-wide.
                if not name or name in seen_engines:
                    continue
                seen_engines.add(name)
                engine_specs.append((group_index, name))
        batches = await asyncio.gather(
            *(
                self._ddgs(
                    variant,
                    engine,
                    limit,
                    category=category,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                )
                for _group_index, engine in engine_specs
                for variant in queries
            )
        )
        group_results: dict[int, list[list[SearchResult]]] = {}
        engine_statuses: dict[tuple[int, str], list[ProviderStatus]] = {}
        engine_items: dict[tuple[int, str], list[list[SearchResult]]] = {}
        call_specs = [
            (group_index, _engine)
            for group_index, _engine in engine_specs
            for _variant in queries
        ]
        for (group_index, engine), (items, status) in zip(
            call_specs, batches, strict=True
        ):
            group_results.setdefault(group_index, []).append(items)
            key = (group_index, engine)
            engine_statuses.setdefault(key, []).append(status)
            engine_items.setdefault(key, []).append(items)
        statuses = []
        for key in engine_statuses:
            agg = _aggregate_engine_status(engine_statuses[key], engine_items[key])
            # Stamp breaker fields AFTER the gather: per-variant snapshots ran
            # concurrently, so only a fresh read reflects the final state.
            engine_name = key[1]
            b_state = self.breaker.get_state(engine_name)
            agg.circuit_state = b_state["state"]
            agg.failures = b_state["failures"]
            agg.backoff_remaining = b_state["backoff_remaining"]
            statuses.append(agg)
        lists = []
        for group_index in range(len(groups)):
            merged = merge_query_variants(
                [items for items in group_results.get(group_index, []) if items]
            )
            if merged:
                lists.append(merged)
        return lists, statuses, normalized_sites

    @staticmethod
    def _filter_sites(results: list[SearchResult], sites: list[str]) -> list[SearchResult]:
        if not sites:
            return results
        return [result for result in results if _site_matches(result.url, sites)]

    async def search_run(
        self,
        query: str,
        *,
        sites: list[str] | None = None,
        mode: SearchMode = "plain",
        limit: int = 20,
        fetch: bool = False,
        category: SearchCategory = "text",
        region: str | None = None,
        safesearch: SafeSearch = "moderate",
        timelimit: TimeLimit | None = None,
        profile: ProfileType | None = None,
        max_query_variants: int | None = None,
    ) -> SearchRun:
        return await self.research_run(
            query,
            sites=sites,
            mode=mode,
            limit=limit,
            fetch=fetch,
            include_github=False,
            category=category,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit,
            profile=profile,
            max_query_variants=max_query_variants,
        )

    async def search(self, query: str, **kwargs: Any) -> list[SearchResult]:
        return (await self.search_run(query, **kwargs)).results

    async def fetch_content(self, results: list[SearchResult]) -> None:
        headers = BROWSER_FETCH_HEADERS.copy()
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers=headers,
            follow_redirects=False,
            trust_env=False,
        ) as client:

            async def one(item: SearchResult) -> None:
                async with self._fetch_semaphore():
                    try:
                        html, final_url = await self._download(client, item.url)
                        content, method = await asyncio.to_thread(self._extract, html, final_url)
                        if self.max_content_chars and self.max_content_chars > 0:
                            item.content = content[: self.max_content_chars]
                        else:
                            item.content = content
                        item.extraction_method = method
                        item.fetched_url = final_url
                        item.fetched_at = datetime.now(UTC).isoformat()
                    except Exception as exc:
                        item.content = None
                        item.fetch_error = f"{type(exc).__name__}: {exc}"

            await asyncio.gather(*(one(item) for item in results))

    async def _download(self, client: httpx.AsyncClient, url: str) -> tuple[str, str]:
        current = url
        for _ in range(6):
            await self._validate_public_url(current)
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect response has no location")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type")
                if not _content_type_allowed(content_type):
                    raise ValueError(
                        f"unsupported content type: {content_type or 'missing'}"
                    )
                content_length = response.headers.get("content-length")
                if (
                    content_length
                    and content_length.isdigit()
                    and int(content_length) > self.max_download_bytes
                ):
                    raise ValueError("response exceeds download limit")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_download_bytes:
                        raise ValueError("response exceeds download limit")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                return b"".join(chunks).decode(encoding, errors="replace"), str(response.url)
        raise ValueError("too many redirects")

    async def _validate_public_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("only public HTTP(S) URLs can be fetched")
        if parsed.username or parsed.password:
            raise ValueError("URLs containing credentials are blocked")
        default_port = 443 if parsed.scheme == "https" else 80
        try:
            addresses = await asyncio.wait_for(
                asyncio.to_thread(
                    socket.getaddrinfo,
                    parsed.hostname,
                    parsed.port or default_port,
                    type=socket.SOCK_STREAM,
                ),
                timeout=self.timeout,
            )
        except TimeoutError as exc:
            raise ValueError("hostname resolution timed out") from exc
        except socket.gaierror as exc:
            raise ValueError("hostname could not be resolved") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global or ip.is_multicast:
                raise ValueError("private or reserved network destinations are blocked")

    @staticmethod
    def _is_ad_redirect(url: str) -> bool:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()

        def is_domain(domain: str) -> bool:
            return host == domain or host.endswith(f".{domain}")

        return (
            (is_domain("google.com") and path.startswith("/aclick"))
            or (is_domain("bing.com") and path.startswith("/ck/"))
            or is_domain("googleadservices.com")
            or is_domain("doubleclick.net")
        )

    @staticmethod
    def _extract(html: str, url: str) -> tuple[str, str]:
        with _EXTRACT_LOCK:
            try:
                import trafilatura

                extracted = trafilatura.extract(
                    html, url=url, include_links=True, output_format="markdown"
                )
                if extracted and extracted.strip():
                    return extracted.strip(), "trafilatura"
            except Exception:
                pass
            without_noise = re.sub(
                r"<(script|style|noscript)\b[^>]*>.*?</\1>",
                " ",
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            fallback = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", without_noise)).strip()
            return fallback, "html-text-fallback"

    async def github_run(
        self, query: str, limit: int = 20
    ) -> tuple[list[SearchResult], ProviderStatus]:
        allowed, skip_message = self.breaker.status("github-api", "search")
        if not allowed:
            return [], ProviderStatus(
                "github-api",
                query,
                False,
                error=skip_message,
                error_kind="skipped",
                **self._breaker_telemetry("github-api"),
            )
        url = "https://api.github.com/search/repositories"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        t0 = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=self.timeout, headers=headers, trust_env=False
        ) as client:
            try:
                response = await client.get(url, params={"q": query, "per_page": min(limit, 100)})
                response.raise_for_status()
                body = response.json()
                results = []
                rank = 1
                for item in body.get("items", []):
                    if isinstance(item, dict) and item.get("full_name") and item.get("html_url"):
                        results.append(
                            SearchResult(
                                title=item["full_name"],
                                url=item["html_url"],
                                snippet=item.get("description") or "",
                                sources=["github-api"],
                                engine_ranks={"github-api": rank},
                            )
                        )
                        rank += 1
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                self.breaker.record_latency("github-api", latency_ms)
                self.breaker.record_success("github-api", "search")
                return results, ProviderStatus(
                    "github-api",
                    query,
                    True,
                    len(results),
                    latency_ms=latency_ms,
                    **self._breaker_telemetry("github-api"),
                )
            except Exception as exc:
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                self.breaker.record_latency("github-api", latency_ms)
                self.breaker.record_failure("github-api", "search")
                return [], ProviderStatus(
                    "github-api",
                    query,
                    False,
                    error=f"{type(exc).__name__}: {exc}",
                    error_kind=_classify_search_error(exc),
                    latency_ms=latency_ms,
                    **self._breaker_telemetry("github-api"),
                )

    async def _probe_host(self, provider: str, host: str) -> HostStatus:
        """Bare HTTPS probe; any HTTP response (even 3xx/4xx) means the host is reachable —
        a status like 403/429 signals reachable-but-challenged, not blocked."""
        if not host:
            return HostStatus(
                provider,
                host,
                False,
                None,
                "unknown",
                "no known upstream host",
                **self._breaker_telemetry(provider),
            )
        async with self._search_semaphore():
            url = f"https://{host}/"
            headers = BROWSER_FETCH_HEADERS.copy()
            t0 = time.perf_counter()
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    headers=headers,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    response = await client.get(url)
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                self.breaker.record_latency(provider, latency_ms)
                return HostStatus(
                    provider,
                    host,
                    True,
                    response.status_code,
                    challenged=response.status_code in CHALLENGED_HTTP_STATUSES,
                    latency_ms=latency_ms,
                    **self._breaker_telemetry(provider),
                )
            except Exception as exc:
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                self.breaker.record_latency(provider, latency_ms)
                return HostStatus(
                    provider,
                    host,
                    False,
                    None,
                    _classify_probe_error(exc),
                    f"{type(exc).__name__}: {exc}",
                    latency_ms=latency_ms,
                    **self._breaker_telemetry(provider),
                )

    async def diagnose_run(
        self,
        *,
        include_github: bool = False,
        category: SearchCategory = "text",
    ) -> DiagnoseRun:
        """Probe each configured backend's upstream host without running ddgs.

        Reports bare HTTPS reachability per provider so callers can distinguish
        network-unreachable hosts from reachable-but-challenged ones. Probes only
        the backends the current category would use, plus api.github.com when
        ``include_github`` is set — mirroring ``research_run``.
        """
        groups = self.news_backends if category == "news" else self.backends
        targets: list[tuple[str, str]] = []
        skipped: list[HostStatus] = []
        seen_providers: set[str] = set()
        for group in groups:
            for name in group.split(","):
                name = name.strip()
                if not name or name in seen_providers:
                    continue
                seen_providers.add(name)
                available, unavailable_message = _engine_available(category, name)
                if not available:
                    skipped.append(
                        HostStatus(
                            name,
                            "",
                            False,
                            None,
                            "skipped",
                            unavailable_message,
                            **self._breaker_telemetry(name),
                        )
                    )
                    continue
                host = self.BACKEND_HOSTS.get(name, "")
                if not host:
                    skipped.append(
                        HostStatus(
                            name,
                            "",
                            False,
                            None,
                            "skipped",
                            f"skipped: no known upstream host for '{name}'",
                        )
                    )
                    continue
                targets.append((name, host))
        if include_github:
            targets.append(("github-api", self.BACKEND_HOSTS["github-api"]))
        statuses = await asyncio.gather(*(self._probe_host(name, host) for name, host in targets))
        return DiagnoseRun(providers=[*statuses, *skipped])

    async def research_run(
        self,
        query: str,
        *,
        sites: list[str] | None = None,
        mode: SearchMode = "plain",
        limit: int = 20,
        fetch: bool = False,
        include_github: bool = False,
        category: SearchCategory = "text",
        region: str | None = None,
        safesearch: SafeSearch = "moderate",
        timelimit: TimeLimit | None = None,
        profile: ProfileType | None = None,
        max_query_variants: int | None = None,
    ) -> SearchRun:
        effective_profile = profile if profile is not None else self.profile
        if effective_profile not in ("general", "dev", "academic"):
            raise ValueError(f"unsupported profile: {effective_profile}")
        effective_max_variants = (
            max_query_variants if max_query_variants is not None else self.max_query_variants
        )
        if effective_max_variants is not None and effective_max_variants < 1:
            raise ValueError("max_query_variants must be positive")
        cache_key = None
        normalized_sites = normalize_sites(sites)
        if self.enable_cache and self.cache:
            effective_backends = tuple(self.news_backends if category == "news" else self.backends)
            cache_key = self.cache.hash_key(
                query,
                sites=normalized_sites,
                mode=mode,
                limit=limit,
                fetch=fetch,
                include_github=include_github,
                category=category,
                region=region,
                safesearch=safesearch,
                timelimit=timelimit,
                backends=effective_backends,
                profile=effective_profile,
                max_query_variants=effective_max_variants,
            )
            cached_data = self.cache.get(cache_key)
            if cached_data is not None:
                try:
                    run = _search_run_from_dict(cached_data)
                    if fetch and any(result.content is None for result in run.results):
                        # Cached payload is SERP-only; rehydrate page content on demand.
                        await self.fetch_content(run.results)
                    run.cached = True
                    return run
                except Exception:
                    self.cache.delete(cache_key)

        web_task = self._search_parts(
            query,
            sites=sites,
            mode=mode,
            limit=limit,
            category=category,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit,
            max_query_variants=effective_max_variants,
        )
        if include_github:
            (lists, providers, normalized_sites), (github, github_status) = await asyncio.gather(
                web_task, self.github_run(query, limit)
            )
            if github:
                lists.append(github)
            providers.append(github_status)
        else:
            lists, providers, normalized_sites = await web_task
        results = self._filter_sites(
            rrf(lists, profile=effective_profile), normalized_sites
        )[:limit]
        if fetch:
            await self.fetch_content(results)
        run = SearchRun(
            query=query,
            results=results,
            providers=providers,
            run_at=datetime.now(UTC).isoformat(),
        )
        if (
            cache_key
            and self.enable_cache
            and self.cache
            and run.status != "failed"
            and len(run.results) > 0
        ):
            payload = _strip_fetch_fields(run.dict())
            payload["cached"] = False
            self.cache.set(
                cache_key, payload, ttl=_ttl_for(category, timelimit, self.cache.default_ttl)
            )
        return run

    async def research(self, query: str, **kwargs: Any) -> list[SearchResult]:
        return (await self.research_run(query, **kwargs)).results
