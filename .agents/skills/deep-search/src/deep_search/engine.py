from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
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
ErrorKind = Literal["network", "rate_limited", "empty", "parse", "unknown"]
ProfileType = Literal["general", "dev", "academic"]

SCHEMA_VERSION = "1.0"
MAX_SITES = 5
USER_AGENT = "deep-search/0.3 (+https://github.com/Alih-b/deep-search)"
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

    def dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HostStatus:
    provider: str
    host: str
    ok: bool
    http_status: int | None = None
    error_kind: Literal["timeout", "dns", "tls", "network", "unknown"] | None = None
    error: str | None = None

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


_RATE_LIMIT_TOKENS = ("rate limit", "too many", "429", "too many requests")
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
        if status in (429,) or 500 <= status < 600:
            return "rate_limited" if status == 429 else "network"
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

    @property
    def partial(self) -> bool:
        return any(not provider.ok for provider in self.providers)

    @property
    def status(self) -> str:
        if not self.results and self.providers and all(not item.ok for item in self.providers):
            return "failed"
        if self.partial:
            return "degraded"
        return "complete"

    def dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "query": self.query,
            "status": self.status,
            "count": len(self.results),
            "partial": self.partial,
            "providers": [provider.dict() for provider in self.providers],
            "results": [result.dict() for result in self.results],
        }


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
    return replace(item, sources=list(item.sources))


def _merge_result(current: SearchResult, candidate: SearchResult) -> None:
    current.sources = list(dict.fromkeys([*current.sources, *candidate.sources]))
    if len(candidate.snippet) > len(current.snippet):
        current.snippet = candidate.snippet
        current.title = candidate.title or current.title
    current.published_at = current.published_at or candidate.published_at
    current.publisher = current.publisher or candidate.publisher


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
    """Fuse independent backend-group ranked lists with Reciprocal Rank Fusion."""
    if k < 1:
        raise ValueError("k must be positive")
    merged: dict[str, SearchResult] = {}
    scores: dict[str, float] = {}
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
            scores[key] = (
                scores.get(key, 0.0)
                + (1 / (k + rank)) * domain_weight(item.url, profile=profile)
            )
            if key not in merged:
                merged[key] = _clone(item)
            else:
                _merge_result(merged[key], item)
    for key, item in merged.items():
        item.score = round(scores[key], 6)
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
    ):
        if db_path is None:
            cache_dir = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "deep-search"
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                self.db_path = cache_dir / "cache.db"
            except Exception:
                self.db_path = Path("/tmp/deep_search_cache.db")
        else:
            self.db_path = Path(db_path)
            with suppress(Exception):
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
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
                    payload TEXT
                );
                """
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
                return json.loads(payload_str)
        except Exception:
            return None

    def set(self, key: str, payload: dict[str, Any], ttl: float | None = None) -> None:
        with suppress(Exception), self._get_conn() as conn:
            now = time.time()
            expires = now + (ttl if ttl is not None else self.default_ttl)
            payload_str = json.dumps(payload, ensure_ascii=False)
            conn.execute(
                """
                INSERT OR REPLACE INTO search_cache (key, created_at, expires_at, payload)
                VALUES (?, ?, ?, ?)
                """,
                (key, now, expires, payload_str),
            )

    def clear(self) -> None:
        with suppress(Exception), self._get_conn() as conn:
            conn.execute("DELETE FROM search_cache;")


def _search_run_from_dict(payload: dict[str, Any]) -> SearchRun:
    results = [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("snippet", ""),
            sources=list(item.get("sources", [])),
            published_at=item.get("published_at"),
            publisher=item.get("publisher"),
            score=float(item.get("score", 0.0)),
            content=item.get("content"),
            extraction_method=item.get("extraction_method"),
            fetched_url=item.get("fetched_url"),
            fetched_at=item.get("fetched_at"),
            fetch_error=item.get("fetch_error"),
        )
        for item in payload.get("results", [])
    ]
    providers = [
        ProviderStatus(
            provider=p.get("provider", ""),
            query=p.get("query", ""),
            ok=bool(p.get("ok", True)),
            result_count=int(p.get("result_count", 0)),
            error=p.get("error"),
            error_kind=p.get("error_kind"),
        )
        for p in payload.get("providers", [])
    ]
    return SearchRun(
        query=payload.get("query", ""),
        results=results,
        providers=providers,
    )


class DeepSearch:
    """Small, bounded metasearch querying backend groups in parallel with
    group-level RRF fusion and safe text extraction."""

    DEFAULT_BACKENDS = (
        "bing,brave,duckduckgo",
        "google,mojeek,startpage",
        "yandex,yahoo",
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
        "github-api": "api.github.com",
    }

    DEFAULT_SEARCH_CONCURRENCY = 6
    DEFAULT_FETCH_CONCURRENCY = 4

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
            SearchCache(cache_db, default_ttl=cache_ttl) if enable_cache else None
        )
        self._search_sem: asyncio.Semaphore | None = None
        self._fetch_sem: asyncio.Semaphore | None = None

    def clear_cache(self) -> None:
        if self.cache:
            self.cache.clear()

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
        async with self._search_semaphore():

            def run() -> tuple[list[SearchResult], ProviderStatus]:
                try:
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
                    results = []
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
                                )
                            )
                    return results, ProviderStatus(backend, query, True, len(results))
                except Exception as exc:
                    return [], ProviderStatus(
                        backend,
                        query,
                        False,
                        error=f"{type(exc).__name__}: {exc}",
                        error_kind=_classify_search_error(exc),
                    )

            return await asyncio.to_thread(run)

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
        backends = self.news_backends if category == "news" else self.backends
        batches = await asyncio.gather(
            *(
                self._ddgs(
                    variant,
                    backend,
                    limit,
                    category=category,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                )
                for backend in backends
                for variant in queries
            )
        )
        width = len(queries)
        lists = []
        for index, _backend in enumerate(backends):
            variant_lists = [items for items, _ in batches[index * width : (index + 1) * width]]
            merged = merge_query_variants([items for items in variant_lists if items])
            if merged:
                lists.append(merged)
        return lists, [status for _, status in batches], normalized_sites

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
                content_type = response.headers.get("content-type", "").lower()
                if content_type and not any(
                    allowed in content_type
                    for allowed in ("text/html", "application/xhtml+xml", "text/plain")
                ):
                    raise ValueError(f"unsupported content type: {content_type}")
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
            if not ip.is_global:
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
        url = "https://api.github.com/search/repositories"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        async with httpx.AsyncClient(
            timeout=self.timeout, headers=headers, trust_env=False
        ) as client:
            try:
                response = await client.get(url, params={"q": query, "per_page": min(limit, 100)})
                response.raise_for_status()
                body = response.json()
                results = [
                    SearchResult(
                        title=item["full_name"],
                        url=item["html_url"],
                        snippet=item.get("description") or "",
                        sources=["github-api"],
                    )
                    for item in body.get("items", [])
                    if isinstance(item, dict) and item.get("full_name") and item.get("html_url")
                ]
                return results, ProviderStatus("github-api", query, True, len(results))
            except Exception as exc:
                return [], ProviderStatus(
                    "github-api",
                    query,
                    False,
                    error=f"{type(exc).__name__}: {exc}",
                    error_kind=_classify_search_error(exc),
                )

    async def _probe_host(self, provider: str, host: str) -> HostStatus:
        """Bare HTTPS probe; any HTTP response (even 3xx/4xx) means the host is reachable —
        a status like 403/429 signals reachable-but-challenged, not blocked."""
        if not host:
            return HostStatus(
                provider, host, False, None, "unknown", "no known upstream host"
            )
        async with self._search_semaphore():
            url = f"https://{host}/"
            headers = BROWSER_FETCH_HEADERS.copy()
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    headers=headers,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    response = await client.get(url)
                return HostStatus(provider, host, True, response.status_code)
            except Exception as exc:
                return HostStatus(
                    provider,
                    host,
                    False,
                    None,
                    _classify_probe_error(exc),
                    f"{type(exc).__name__}: {exc}",
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
        seen_providers: set[str] = set()
        for group in groups:
            for name in group.split(","):
                name = name.strip()
                if name in seen_providers:
                    continue
                seen_providers.add(name)
                targets.append((name, self.BACKEND_HOSTS.get(name, "")))
        if include_github:
            targets.append(("github-api", self.BACKEND_HOSTS["github-api"]))
        statuses = await asyncio.gather(*(self._probe_host(name, host) for name, host in targets))
        return DiagnoseRun(providers=list(statuses))

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
                return _search_run_from_dict(cached_data)

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
        run = SearchRun(query=query, results=results, providers=providers)
        if (
            cache_key
            and self.enable_cache
            and self.cache
            and run.status != "failed"
            and len(run.results) > 0
        ):
            self.cache.set(cache_key, run.dict())
        return run

    async def research(self, query: str, **kwargs: Any) -> list[SearchResult]:
        return (await self.research_run(query, **kwargs)).results
