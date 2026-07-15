from __future__ import annotations

import asyncio
import ipaddress
import random
import re
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from ddgs import DDGS

SearchMode = Literal["plain", "exact", "oss"]
SearchCategory = Literal["text", "news"]
SafeSearch = Literal["on", "moderate", "off"]
TimeLimit = Literal["d", "w", "m", "y"]

TRACKING_QUERY_KEYS = {
    "dclid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
}


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = "web"
    score: float = 0.0
    content: str | None = None
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
    attempts: int = 1
    error: str | None = None

    def dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchRun:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    providers: list[ProviderStatus] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return any(not provider.ok for provider in self.providers)

    def dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "count": len(self.results),
            "partial": self.partial,
            "providers": [provider.dict() for provider in self.providers],
            "results": [result.dict() for result in self.results],
        }


def canonical(url: str) -> str:
    """Normalize URL variants while preserving query parameters that identify resources."""
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
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


def rrf(ranked: list[list[SearchResult]], k: int = 60) -> list[SearchResult]:
    """Merge ranked lists using Reciprocal Rank Fusion and canonical URL deduplication."""
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
            scores[key] = scores.get(key, 0.0) + 1 / (k + rank)
            current = merged.get(key)
            if current is None or len(item.snippet) > len(current.snippet):
                merged[key] = item
    for key, item in merged.items():
        item.score = round(scores[key], 6)
    return sorted(merged.values(), key=lambda item: item.score, reverse=True)


class DeepSearch:
    """Bounded metasearch, RRF ranking, source routing, and safe text extraction."""

    DEFAULT_BACKENDS = (
        "bing,brave,duckduckgo",
        "google,mojeek,startpage",
        "yandex,yahoo",
    )
    DEFAULT_NEWS_BACKENDS = ("bing,duckduckgo,yahoo",)

    def __init__(
        self,
        *,
        timeout: float = 8,
        max_concurrency: int = 6,
        max_download_bytes: int = 2_000_000,
        max_content_chars: int = 50_000,
        github_token: str | None = None,
        backends: tuple[str, ...] | None = None,
        news_backends: tuple[str, ...] | None = None,
        max_retries: int = 1,
        retry_base_delay: float = 0.25,
    ):
        if timeout <= 0 or max_concurrency < 1:
            raise ValueError("timeout and max_concurrency must be positive")
        if max_retries < 0 or retry_base_delay < 0:
            raise ValueError("retry settings must not be negative")
        self.timeout = timeout
        self.max_concurrency = max_concurrency
        self.max_download_bytes = max_download_bytes
        self.max_content_chars = max_content_chars
        self.github_token = github_token
        self.backends = backends or self.DEFAULT_BACKENDS
        self.news_backends = news_backends or (
            backends if backends is not None else self.DEFAULT_NEWS_BACKENDS
        )
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._sem: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        # Construct lazily inside the active event loop.
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrency)
        return self._sem

    @staticmethod
    def expand(
        query: str,
        sites: list[str] | None = None,
        mode: SearchMode = "plain",
    ) -> list[str]:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if mode not in ("plain", "exact", "oss"):
            raise ValueError(f"unsupported search mode: {mode}")

        base = [query]
        if mode == "exact":
            base.append(f'"{query}"')
        elif mode == "oss":
            base.extend((f'"{query}"', f"{query} open source", f"{query} self-hosted"))

        for site in sites or []:
            site = site.strip().lower()
            if site and f"site:{site}" not in query.lower():
                base.append(f"site:{site} {query}")
        return list(dict.fromkeys(base))

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
        async with self._semaphore():

            def run() -> tuple[list[SearchResult], ProviderStatus]:
                last_error: Exception | None = None
                attempts = self.max_retries + 1
                for attempt in range(1, attempts + 1):
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
                            if result_url and not self._is_ad(result_url):
                                results.append(
                                    SearchResult(
                                        row.get("title", ""),
                                        result_url,
                                        row.get("body", ""),
                                        backend,
                                    )
                                )
                        return results, ProviderStatus(
                            backend, query, True, len(results), attempts=attempt
                        )
                    except Exception as exc:
                        last_error = exc
                        if attempt < attempts:
                            delay = self.retry_base_delay * (2 ** (attempt - 1))
                            time.sleep(delay + random.uniform(0, delay / 4 if delay else 0))
                assert last_error is not None
                return [], ProviderStatus(
                    backend,
                    query,
                    False,
                    attempts=attempts,
                    error=f"{type(last_error).__name__}: {last_error}",
                )

            return await asyncio.to_thread(run)

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
    ) -> SearchRun:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        queries = self.expand(query, sites, mode)
        backends = self.news_backends if category == "news" else self.backends
        batches = await asyncio.gather(
            *(
                self._ddgs(
                    q,
                    backend,
                    limit,
                    category=category,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                )
                for q in queries
                for backend in backends
            )
        )
        results = rrf([items for items, _ in batches if items])[:limit]
        if fetch:
            await self.fetch_content(results)
        return SearchRun(query=query, results=results, providers=[status for _, status in batches])

    async def search(
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
    ) -> list[SearchResult]:
        run = await self.search_run(
            query,
            sites=sites,
            mode=mode,
            limit=limit,
            fetch=fetch,
            category=category,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit,
        )
        return run.results

    async def fetch_content(self, results: list[SearchResult]) -> None:
        headers = {"User-Agent": "deep-search/0.2 (+local research tool)"}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:

            async def one(item: SearchResult) -> None:
                async with self._semaphore():
                    try:
                        html, final_url = await self._download(client, item.url)
                        item.content = await asyncio.to_thread(self._extract, html, final_url)
                        item.content = item.content[: self.max_content_chars]
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

    @staticmethod
    async def _validate_public_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("only public HTTP(S) URLs can be fetched")
        if parsed.username or parsed.password:
            raise ValueError("URLs containing credentials are blocked")
        default_port = 443 if parsed.scheme == "https" else 80
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                parsed.port or default_port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError("hostname could not be resolved") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ValueError("private or reserved network destinations are blocked")

    @staticmethod
    def _is_ad(url: str) -> bool:
        lowered = url.lower()
        return any(
            marker in lowered
            for marker in (
                "/aclick?",
                "bing.com/ck/",
                "googleadservices.com",
                "doubleclick.net",
            )
        )

    @staticmethod
    def _extract(html: str, url: str) -> str:
        try:
            import trafilatura

            return trafilatura.extract(html, url=url, include_links=True) or ""
        except Exception:
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

    async def github_run(
        self, query: str, limit: int = 20
    ) -> tuple[list[SearchResult], ProviderStatus]:
        url = "https://api.github.com/search/repositories"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "deep-search/0.2",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            try:
                response = await client.get(
                    url, params={"q": query, "sort": "stars", "per_page": min(limit, 100)}
                )
                response.raise_for_status()
                results = [
                    SearchResult(
                        item["full_name"],
                        item["html_url"],
                        item.get("description") or "",
                        "github-api",
                    )
                    for item in response.json().get("items", [])
                ]
                return results, ProviderStatus("github-api", query, True, len(results))
            except Exception as exc:
                return [], ProviderStatus(
                    "github-api", query, False, error=f"{type(exc).__name__}: {exc}"
                )

    async def research_run(
        self,
        query: str,
        *,
        sites: list[str] | None = None,
        mode: SearchMode = "plain",
        limit: int = 20,
        fetch: bool = False,
        include_github: bool = True,
        category: SearchCategory = "text",
        region: str | None = None,
        safesearch: SafeSearch = "moderate",
        timelimit: TimeLimit | None = None,
    ) -> SearchRun:
        web_task = self.search_run(
            query,
            sites=sites,
            mode=mode,
            limit=limit,
            fetch=False,
            category=category,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit,
        )
        if include_github:
            web, (github, github_status) = await asyncio.gather(
                web_task, self.github_run(query, limit)
            )
            results = rrf([web.results, github])[:limit]
            providers = [*web.providers, github_status]
        else:
            web = await web_task
            results = web.results
            providers = web.providers
        if fetch:
            await self.fetch_content(results)
        return SearchRun(query=query, results=results, providers=providers)

    async def research(
        self,
        query: str,
        *,
        sites: list[str] | None = None,
        mode: SearchMode = "plain",
        limit: int = 20,
        fetch: bool = False,
        include_github: bool = True,
        category: SearchCategory = "text",
        region: str | None = None,
        safesearch: SafeSearch = "moderate",
        timelimit: TimeLimit | None = None,
    ) -> list[SearchResult]:
        return (
            await self.research_run(
                query,
                sites=sites,
                mode=mode,
                limit=limit,
                fetch=fetch,
                include_github=include_github,
                category=category,
                region=region,
                safesearch=safesearch,
                timelimit=timelimit,
            )
        ).results
