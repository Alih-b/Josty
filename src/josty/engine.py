"""The Josty facade: wiring, search, fetch, and diagnose orchestration."""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from ddgs import DDGS

from .backends import _engine_available
from .breaker import CircuitBreaker, _cool_down_message
from .cache import (
    CACHE_MAX_BYTES,
    SearchCache,
    _search_run_from_dict,
    _stamp_fetch_stats,
    _strip_fetch_fields,
    _ttl_for,
)
from .errors import _aggregate_engine_status, _classify_probe_error, _classify_search_error
from .fetch import BROWSER_FETCH_HEADERS, download, extract, is_ad_redirect, validate_public_url
from .lease import LeasePool
from .models import DiagnoseRun, HostStatus, ProviderStatus, SearchResult, SearchRun
from .ranking import (
    _site_matches,
    merge_query_variants,
    normalize_sites,
    rrf,
)
from .status import (
    CHALLENGED_HTTP_STATUSES,
    SEARCH_THREAD_TIMEOUT_HEADROOM,
    USER_AGENT,
    ProfileType,
    SafeSearch,
    SearchCategory,
    SearchMode,
    SearchStatus,
    TimeLimit,
)


class _FanoutLedger:
    """Per-run count of search calls actually issued.

    Incremented by worker threads immediately before a ddgs/GitHub call, so it
    counts invocations, not sockets: a scheduled task that never reaches its
    call site (registry-missing, breaker-skipped) contributes nothing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.issued = 0

    def record(self) -> None:
        with self._lock:
            self.issued += 1


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
    # Shared by the CLI flag default and the library constructor so both entry
    # points cap extracted Markdown at the same per-page size (#59).
    DEFAULT_MAX_CONTENT_CHARS = 8000
    DEFAULT_BREAKER_FAIL_THRESHOLD = 3
    DEFAULT_BREAKER_WINDOW_SECONDS = 60
    DEFAULT_BREAKER_COOL_DOWN_SECONDS = 30

    def __init__(
        self,
        *,
        timeout: float = 8,
        max_search_concurrency: int = 6,
        max_fetch_concurrency: int = 4,
        max_concurrency: int | None = None,  # deprecated alias for max_search_concurrency
        max_download_bytes: int = 2_000_000,
        max_content_chars: int | None = DEFAULT_MAX_CONTENT_CHARS,
        max_query_variants: int | None = None,
        run_timeout: float | None = None,
        max_ghosts: int | None = None,
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
        if max_concurrency is not None:
            # Deprecated alias, applied before validation. Applying it afterwards
            # (as an earlier revision did) let Josty(max_concurrency=0) bypass the
            # check and build a zero-permit semaphore that hung every search.
            max_search_concurrency = max_concurrency
        if timeout <= 0 or max_search_concurrency < 1 or max_fetch_concurrency < 1:
            raise ValueError("timeout and concurrency limits must be positive")
        if max_query_variants is not None and max_query_variants < 1:
            raise ValueError("max_query_variants must be positive")
        if run_timeout is not None and run_timeout <= 0:
            raise ValueError("run_timeout must be positive when set")
        if max_ghosts is not None and max_ghosts < 0:
            raise ValueError("max_ghosts must not be negative")
        if max_download_bytes < 1 or (max_content_chars is not None and max_content_chars < 0):
            raise ValueError("content limits must be positive")
        if profile not in ("general", "dev"):
            raise ValueError(f"unsupported profile: {profile}")
        self.timeout = timeout
        self.max_search_concurrency = max_search_concurrency
        self.max_fetch_concurrency = max_fetch_concurrency
        self.max_download_bytes = max_download_bytes
        self.max_content_chars = max_content_chars
        self.max_query_variants = max_query_variants
        # Opt-in outer wall-clock bound for one run. None keeps today's behaviour
        # (each call gets its own full budget), which is what a healthy fanout
        # needs: a default tight enough to fire on 6 engines x 20 variants would
        # turn a slow-but-working run into a shed one.
        self.run_timeout = run_timeout
        # Ceiling on unreturned ("ghost") workers tolerated before new calls are
        # refused rather than admitted. Defaults to the concurrency cap, which
        # keeps live threads <= the cap.
        self.max_ghosts = max_ghosts
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

    def _engine_specs(self, category: SearchCategory) -> list[tuple[int, str]]:
        """Unique engines in first-seen group order (duplicates across groups dropped)."""
        groups = self.news_backends if category == "news" else self.backends
        specs: list[tuple[int, str]] = []
        seen: set[str] = set()
        for group_index, group in enumerate(groups):
            for name in group.split(","):
                name = name.strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                specs.append((group_index, name))
        return specs

    def _engine_names(self, category: SearchCategory) -> list[str]:
        return [name for _, name in self._engine_specs(category)]

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
        ledger: _FanoutLedger,
        pool: LeasePool,
        executor: ThreadPoolExecutor,
        deadline: float | None,
    ) -> tuple[list[SearchResult], ProviderStatus]:
        pool.note_scheduled()
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
        try:
            return await self._ddgs_execute(
                query,
                backend,
                limit,
                category=category,
                region=region,
                safesearch=safesearch,
                timelimit=timelimit,
                ledger=ledger,
                pool=pool,
                executor=executor,
                deadline=deadline,
            )
        finally:
            self.breaker.release_probe(backend, "search")

    async def _ddgs_execute(
        self,
        query: str,
        backend: str,
        limit: int,
        *,
        category: SearchCategory,
        region: str | None,
        safesearch: SafeSearch,
        timelimit: TimeLimit | None,
        ledger: _FanoutLedger,
        pool: LeasePool,
        executor: ThreadPoolExecutor,
        deadline: float | None,
    ) -> tuple[list[SearchResult], ProviderStatus]:
        async with self._search_semaphore():
            now = time.monotonic()
            lease = pool.acquire("search", backend, now=now, deadline=deadline)
            if lease is None:
                # Refused admission: no socket was opened, so this must never be
                # reported as an upstream failure. The reason names the cause.
                return [], ProviderStatus(
                    backend,
                    query,
                    False,
                    0,
                    error=pool.shed_message(pool.last_shed_reason or "capacity"),
                    error_kind="skipped",
                    latency_ms=None,
                    **self._breaker_telemetry(backend),
                )
            budget = self.timeout + SEARCH_THREAD_TIMEOUT_HEADROOM
            if deadline is not None:
                budget = min(budget, max(0.0, deadline - now))
            t_start = time.perf_counter()
            try:
                results, latency_ms, exc = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        executor,
                        functools.partial(
                            self._search_worker,
                            query,
                            backend,
                            limit,
                            category=category,
                            region=region,
                            safesearch=safesearch,
                            timelimit=timelimit,
                            ledger=ledger,
                            pool=pool,
                            lease_id=lease.lease_id,
                        ),
                    ),
                    timeout=budget,
                )
            except (asyncio.TimeoutError, TimeoutError):
                # The worker keeps running (a "ghost"): a blocked socket call
                # cannot be cancelled, so this branch reports the timeout now and
                # the thread ends on ddgs's own client timeout. Nothing reads its
                # return value, so a late success cannot revive the branch.
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

    def _search_worker(
        self,
        query: str,
        backend: str,
        limit: int,
        *,
        category: SearchCategory,
        region: str | None,
        safesearch: SafeSearch,
        timelimit: TimeLimit | None,
        ledger: _FanoutLedger,
        pool: LeasePool,
        lease_id: int,
    ) -> tuple[list[SearchResult], float, Exception | None]:
        """One blocking engine call, run on a worker thread.

        Always retires its own lease, including on the exception path. The release
        is idempotent, so returning *after* the arbiter already reaped this lease
        as a ghost is a safe no-op rather than a double free.
        """
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
            # The one request site: counted immediately before the call so the
            # ledger measures calls issued, not tasks scheduled.
            ledger.record()
            rows = method(query, **kwargs)
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
            return results, round((time.perf_counter() - t0) * 1000, 2), None
        except Exception as exc:
            return [], round((time.perf_counter() - t0) * 1000, 2), exc
        finally:
            pool.release(lease_id)

    async def _search_parts(
        self,
        queries: list[str],
        *,
        limit: int,
        category: SearchCategory,
        region: str | None,
        safesearch: SafeSearch,
        timelimit: TimeLimit | None,
        ledger: _FanoutLedger,
        pool: LeasePool,
        executor: ThreadPoolExecutor,
        deadline: float | None,
    ) -> tuple[list[list[SearchResult]], list[ProviderStatus]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if category not in ("text", "news"):
            raise ValueError(f"unsupported search category: {category}")
        if safesearch not in ("on", "moderate", "off"):
            raise ValueError(f"unsupported safe-search mode: {safesearch}")
        if timelimit not in (None, "d", "w", "m", "y"):
            raise ValueError(f"unsupported time limit: {timelimit}")
        groups = self.news_backends if category == "news" else self.backends
        # An engine listed in multiple groups is queried once, in its first
        # group: one call and one status per engine, contract-wide.
        engine_specs = self._engine_specs(category)
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
                    ledger=ledger,
                    pool=pool,
                    executor=executor,
                    deadline=deadline,
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
        return lists, statuses

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
                        item.fetched_at = datetime.now(timezone.utc).isoformat()
                    except Exception as exc:
                        item.content = None
                        item.fetch_error = f"{type(exc).__name__}: {exc}"

            await asyncio.gather(*(one(item) for item in results))

    async def _download(self, client: httpx.AsyncClient, url: str) -> tuple[str, str]:
        return await download(
            client, url, timeout=self.timeout, max_download_bytes=self.max_download_bytes,
            validate=self._validate_public_url,
        )

    async def _validate_public_url(self, url: str) -> None:
        await validate_public_url(url, timeout=self.timeout)

    @staticmethod
    def _is_ad_redirect(url: str) -> bool:
        return is_ad_redirect(url)

    @staticmethod
    def _extract(html: str, url: str) -> tuple[str, str]:
        return extract(html, url)

    async def _github_call_admitted(
        self,
        query: str,
        limit: int,
        *,
        ledger: _FanoutLedger,
        pool: LeasePool,
        deadline: float | None,
    ) -> tuple[list[SearchResult], ProviderStatus]:
        """Run the GitHub call through the same admission as a ddgs branch.

        The ledger counts this call at its issue site, so it must also be counted
        as proposed and be bounded by the pool. Otherwise ``fanout.issued`` can
        exceed ``fanout.scheduled`` and the call escapes both the concurrency cap
        and the run deadline -- the identity AGENTS.md invariant 4 promises would
        be false on every run that includes GitHub.
        """
        pool.note_scheduled()
        lease = pool.acquire("github", query, now=time.monotonic(), deadline=deadline)
        if lease is None:
            return [], ProviderStatus(
                "github-api",
                query,
                False,
                0,
                error=pool.shed_message(pool.last_shed_reason or "capacity"),
                error_kind="skipped",
                latency_ms=None,
                **self._breaker_telemetry("github-api"),
            )
        try:
            return await self.github_run(query, limit, ledger=ledger)
        finally:
            pool.release(lease.lease_id)

    async def github_run(
        self, query: str, limit: int = 20, *, ledger: _FanoutLedger | None = None
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
                if ledger is not None:
                    ledger.record()
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
        a status like 403/429 signals reachable-but-challenged, not blocked.

        OPEN circuits are not probed: ``--diagnose`` must not hit a backend that
        search already has in cool-down. HALF_OPEN/CLOSED still get a GET.
        Uses ``get_state()`` (read-only) so diagnose does not consume the
        single HALF_OPEN search probe slot.
        """
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
        snap = self.breaker.get_state(provider)
        if snap["state"] == "open":
            return HostStatus(
                provider,
                host,
                False,
                None,
                "skipped",
                _cool_down_message(snap["backoff_remaining"]),
                latency_ms=None,
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

        Reports bare HTTPS homepage reachability (``phase: transport``) so callers
        can distinguish network-unreachable hosts from reachable-but-challenged
        ones. This is not search-backend health: ddgs can succeed while a
        homepage GET fails. Probes only the backends the current category would
        use, plus api.github.com when ``include_github`` is set — mirroring
        ``research_run``.
        """
        targets: list[tuple[str, str]] = []
        skipped: list[HostStatus] = []
        for name in self._engine_names(category):
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
        if effective_profile not in ("general", "dev"):
            raise ValueError(f"unsupported profile: {effective_profile}")
        effective_max_variants = (
            max_query_variants if max_query_variants is not None else self.max_query_variants
        )
        if effective_max_variants is not None and effective_max_variants < 1:
            raise ValueError("max_query_variants must be positive")
        cache_key = None
        normalized_sites = normalize_sites(sites)
        # One expansion per run: the same variant list feeds the cache identity,
        # the telemetry, and the fanout below.
        queries = self.expand(
            query,
            normalized_sites,
            mode,
            max_query_variants=effective_max_variants,
        )
        variant_count = len(queries)
        ledger = _FanoutLedger()
        if self.enable_cache and self.cache:
            effective_backends = tuple(self.news_backends if category == "news" else self.backends)
            # SERP identity: fetch is a separate phase and must not bust the cache.
            cache_key = self.cache.hash_key(
                query,
                sites=normalized_sites,
                mode=mode,
                limit=limit,
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
                    run.query_variant_count = variant_count
                    # Cache hit: nothing was proposed, admitted, refused or left
                    # running, so the whole fanout block is a true zero. The
                    # hydrator does not restore these and scheduled_count defaults
                    # to None, which would break the accounting identity.
                    run.request_count = 0
                    run.scheduled_count = 0
                    run.shed_count = 0
                    run.shed_by_reason = {}
                    run.ghosts_outstanding = 0
                    run.ghosts_peak = 0
                    if fetch and any(result.content is None for result in run.results):
                        # Cached payload is SERP-only; rehydrate page content on demand.
                        await self.fetch_content(run.results)
                    _stamp_fetch_stats(run, requested=fetch)
                    run.cached = True
                    return run
                except Exception:
                    self.cache.delete(cache_key)

        # One arbiter and one dedicated worker pool per run. Per-run rather than
        # per-instance, so a worker that never returns cannot shed a later run;
        # dedicated rather than shared, so trafilatura extraction never queues
        # behind search workers. Sized to the cap because admission guarantees
        # held + ghosts <= capacity, so a submission is never queued.
        pool = LeasePool(
            self.max_search_concurrency,
            lease_seconds=self.timeout + SEARCH_THREAD_TIMEOUT_HEADROOM,
            max_ghosts=self.max_ghosts,
        )
        executor = ThreadPoolExecutor(
            max_workers=self.max_search_concurrency,
            thread_name_prefix="josty-search",
        )
        deadline = (
            time.monotonic() + self.run_timeout if self.run_timeout is not None else None
        )
        try:
            web_task = self._search_parts(
                queries,
                limit=limit,
                category=category,
                region=region,
                safesearch=safesearch,
                timelimit=timelimit,
                ledger=ledger,
                pool=pool,
                executor=executor,
                deadline=deadline,
            )
            if include_github:
                (lists, providers), (github, github_status) = await asyncio.gather(
                    web_task,
                    self._github_call_admitted(
                        query, limit, ledger=ledger, pool=pool, deadline=deadline
                    ),
                )
                if github:
                    lists.append(github)
                providers.append(github_status)
            else:
                lists, providers = await web_task
        finally:
            # Do not join: a ghost is exactly the thread we are choosing not to
            # wait for. Reaping records the ones still outstanding so the run
            # reports them instead of hiding them.
            executor.shutdown(wait=False)
        pool.reap(time.monotonic())
        results = self._filter_sites(
            rrf(lists, profile=effective_profile), normalized_sites
        )[:limit]
        if fetch:
            await self.fetch_content(results)
        run = SearchRun(
            query=query,
            results=results,
            providers=providers,
            run_at=datetime.now(timezone.utc).isoformat(),
            query_variant_count=variant_count,
            request_count=ledger.issued,
            scheduled_count=pool.scheduled,
            shed_count=pool.shed,
            shed_by_reason=pool.shed_by_reason,
            ghosts_outstanding=pool.ghosts,
            ghosts_peak=pool.ghosts_peak,
        )
        _stamp_fetch_stats(run, requested=fetch)
        if (
            cache_key
            and self.enable_cache
            and self.cache
            and run.status != SearchStatus.FAILED
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
